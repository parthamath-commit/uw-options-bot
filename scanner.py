"""
scanner.py
==========
UW Options Bot v2.0 -- Pal Initiatives LLC
Multi-feed architecture: options flow + dark pool + congress +
insider + institutional + lit flow.

Scan cycle:
  Phase 1 -- Run all feeds (~8 API calls total, entire market)
  Phase 2 -- Selective enrichment (dealer GEX for qualifying symbols)
  Phase 3 -- Score + cross-feed bonus + classify + alert
"""

import time
import logging
from datetime import date, datetime

from config import cfg, ET
from models import DealerExposure, ScoredSignal

from data.uw_client import UWClient
from data.feeds.aggregator import run_all_feeds, get_cross_feed_context
from data.position_tracker import PositionTracker
from broker.ibkr import IBKRClient
from broker.schwab_client import SchwabBroker, check_token_expiry
from scoring.additive import AdditiveScorer
from scoring.institutional import InstitutionalFlowScorer
from scoring.utils import composite_score, calculate_position_size
from alerts.telegram import TelegramAlerter
from persistence.excel import ExcelLogger
from ml.predictor import get_predictor

from database.db import init_db
from database.purge import run_purge
from database.models_db import (
    insert_scan_run, close_scan_run,
    insert_flow_signal, insert_scored_signal,
    insert_dealer_exposure, insert_oi_snapshot,
    insert_darkpool_prints,
    get_oi_buildup_score, get_gex_trend_score,
    get_dp_sentiment_trend,
)

log = logging.getLogger("UWBot.Scanner")


class UWOptionsBot:

    def __init__(self):
        log.info("=" * 60)
        log.info("UW Options Bot v2.0 -- Pal Initiatives LLC")
        log.info("Feeds: Options Flow + Dark Pool + Congress +")
        log.info("       Insider + Institutional + Lit Flow")
        log.info("=" * 60)

        init_db()

        self.uw         = UWClient()
        self.ibkr       = IBKRClient()
        self.schwab     = SchwabBroker()
        self.telegram   = TelegramAlerter()
        self.excel      = ExcelLogger()
        self.additive   = AdditiveScorer()
        self.inst       = InstitutionalFlowScorer()
        self._predictor = get_predictor()

        self._exposure_cache: dict[str, DealerExposure] = {}
        self._spy_exposure  = DealerExposure("SPY")
        self._alerted: set[str] = set()
        self._market_tide: dict = {}
        self._last_purge_date = None    # tracks daily auto-purge
        self._summary_sent_date = None  # tracks end-of-day summary
        self._morning_sent_date = None  # tracks 7:30 AM morning report
        self._last_offhours_notify = None  # tracks 4-hourly off-hours digest
        self._budget_warned_date = None     # tracks daily budget warning
        self._last_flow_id: str = ""        # adaptive polling: last seen flow ID
        self._tracker = PositionTracker(
            self.uw, self.ibkr, self.schwab, self.telegram)

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _signal_key(self, sig: ScoredSignal) -> str:
        return "{}_{}_{}_{}_{}".format(
            sig.symbol, sig.strike, sig.right, sig.expiry, sig.structure)

    def _valid_dte(self, expiry: str) -> bool:
        try:
            dte = (date.fromisoformat(expiry) - date.today()).days
            return cfg.MIN_DTE <= dte <= cfg.MAX_DTE
        except Exception:
            return True

    def _gex_blocked(self, intent: str) -> bool:
        if intent != "bullish":
            return False
        return self._spy_exposure.net_gex < cfg.GEX_BLOCK_THRESHOLD * 1_000_000

    def _get_quote(self, symbol, expiry, strike, right) -> dict:
        q = self.ibkr.get_option_quote(symbol, expiry.replace("-", ""), strike, right)
        if q and q.get("ask", 0) > 0:
            return q
        if self.schwab.available:
            q = self.schwab.get_option_quote(symbol, expiry, strike, right)
            if q and q.get("ask", 0) > 0:
                return q
        return self.uw.find_contract_quote(symbol, expiry, strike, right)

    def _enrich_symbol(self, symbol: str, run_id: int) -> DealerExposure:
        if symbol in self._exposure_cache:
            return self._exposure_cache[symbol]
        exp = self.uw.get_dealer_exposure(symbol)
        self._exposure_cache[symbol] = exp
        insert_dealer_exposure(exp, run_id)
        return exp

    # ── ML ────────────────────────────────────────────────────────────────────
    ML_CONFIRM_THRESHOLD  = 70.0
    ML_OVERRIDE_THRESHOLD = 80.0

    def _validate_with_ml(self, sig, rule_type):
        if not self._predictor.type_model_available:
            return rule_type, "rule_only"
        from ml.features import extract_features_for_signal
        db_id = getattr(sig, "_db_id", 0) or 0
        if not db_id:
            return rule_type, "rule_only"
        fv = extract_features_for_signal(db_id)
        if not fv:
            return rule_type, "rule_only"
        ml_raw, ml_conf = self._predictor.predict_signal_type(fv)
        ml_conf_pct = ml_conf * 100 if ml_conf <= 1.0 else ml_conf
        ml_type = ("unidirectional" if "bullish" in ml_raw or "bearish" in ml_raw
                   else "straddle" if "straddle" in ml_raw
                   else "strangle" if "strangle" in ml_raw
                   else "hedging"  if "hedg"     in ml_raw
                   else "noise"    if "noise"    in ml_raw
                   else "unidirectional")
        agree = (ml_type == rule_type)
        if agree and ml_conf_pct >= self.ML_CONFIRM_THRESHOLD:
            return rule_type, "ml_confirmed"
        elif agree:
            return rule_type, "ml_agree_low"
        elif not agree and ml_conf_pct >= self.ML_OVERRIDE_THRESHOLD:
            return ml_type, "ml_override"
        return rule_type, "ml_uncertain"

    # ── Classifier ────────────────────────────────────────────────────────────
    def _classify_signals(self, signals):
        if not signals:
            return signals
        by_symbol = {}
        for sig in signals:
            by_symbol.setdefault(sig.symbol, []).append(sig)

        result = []
        for symbol, sym_signals in by_symbol.items():
            calls = [s for s in sym_signals if s.right == "C"]
            puts  = [s for s in sym_signals if s.right == "P"]

            if not calls or not puts:
                for s in sym_signals:
                    s.signal_type = "unidirectional"
                    s.rule_type   = "unidirectional"
                classified = sym_signals
            else:
                bc = max(calls, key=lambda x: x.composite_score)
                bp = max(puts,  key=lambda x: x.composite_score)
                sd = abs(bc.composite_score - bp.composite_score)
                pr = (min(bc.premium, bp.premium) / max(bc.premium, bp.premium)
                      if max(bc.premium, bp.premium) > 0 else 0)
                ss = abs(bc.strike - bp.strike) < 2.5

                if bc.ask_side != bp.ask_side:
                    rule = "hedging"
                elif ss and sd <= 15 and pr >= 0.6:
                    rule = "straddle"
                elif not ss and sd <= 15 and pr >= 0.5:
                    rule = "strangle"
                elif sd > 15:
                    winner = bc if bc.composite_score > bp.composite_score else bp
                    for s in sym_signals:
                        s.rule_type   = ("unidirectional"
                                         if s.right == winner.right else "conflict")
                        s.signal_type = s.rule_type
                    classified = sym_signals
                    rule = None
                else:
                    rule = "unidirectional"

                if rule is not None:
                    for s in sym_signals:
                        s.rule_type   = rule
                        s.signal_type = rule
                    classified = sym_signals

            for s in classified:
                rt = getattr(s, "rule_type", s.signal_type)
                ft, val = self._validate_with_ml(s, rt)
                s.signal_type   = ft
                s.ml_validation = val
                if val == "ml_confirmed":
                    s.composite_score = min(s.composite_score + 5.0, 100.0)
                elif val == "ml_override":
                    s.composite_score = max(s.composite_score - 5.0, 0.0)
                elif val == "ml_uncertain":
                    s.composite_score = max(s.composite_score - 3.0, 0.0)
            result.extend(classified)
        return result

    # ── Main scan cycle ───────────────────────────────────────────────────────
    def scan_cycle(self) -> list[ScoredSignal]:
        scored   = []
        n_alerts = 0
        now_str  = datetime.now(ET).strftime("%H:%M:%S ET")
        log.info("── Scan cycle {} ──".format(now_str))

        self.uw.clear_cache()
        # Bug fix: previously cleared _exposure_cache here (top of cycle),
        # which meant any symbol not in the top-N enrichment window this cycle
        # would get a blank DealerExposure. Now we keep prior-cycle exposure
        # for symbols not being re-enriched, and only force-refresh the top-N.
        check_token_expiry()

        # Daily auto-purge (runs once per day at first cycle after midnight)
        today = datetime.now(ET).date()
        if self._last_purge_date != today:
            try:
                results = run_purge(dry_run=False)
                total   = sum(results.values())
                if total > 0:
                    log.info("Auto-purge: {} rows archived".format(total))
                    detail = ", ".join(
                        "{}: {}".format(k, v)
                        for k, v in results.items() if v > 0
                    )
                    self.telegram.send("DB purge: {} rows archived  {}".format(total, detail))
                self._last_purge_date = today
            except Exception as e:
                log.error("Auto-purge error: {}".format(e))

        # SPY context        # SPY context
        self._spy_exposure = self.uw.get_dealer_exposure("SPY")
        portfolio = self.ibkr.get_account_value()
        run_id    = insert_scan_run(
            portfolio, self._spy_exposure.net_gex, self._spy_exposure.regime)
        insert_dealer_exposure(self._spy_exposure, run_id)

        # Market tide -- overall market sentiment
        tide = self.uw.get_market_tide() or {}
        log.info(
            "SPY GEX=${:.1f}M  regime={}  portfolio=${:,.0f}  "
            "MarketTide={} (net ${:.1f}M)".format(
                self._spy_exposure.net_gex / 1e6,
                self._spy_exposure.regime, portfolio,
                tide.get("sentiment", "n/a"), tide.get("net_m", 0.0)
            )
        )
        # Store tide for cross-feed context
        self._market_tide = tide

        usage = self.uw.get_usage_stats()
        log.info("UW API  daily={}/{} ({:.0f}%) [{}]  min_remain={}  resets_at={}".format(
            usage["daily_used"], usage["daily_limit"],
            usage["daily_pct"], usage["daily_status"],
            usage["minute_remain"], usage["reset_time_et"]))

        # Alert on critical daily usage
        if usage["daily_remaining"] < 100:
            self.telegram.send_error(
                "UW API CRITICAL: only {} requests remaining today. "
                "Resets at 8 PM ET.".format(usage["daily_remaining"])
            )

        # ── Phase 1: Run ALL feeds ────────────────────────────────────────────
        log.info("Running all feeds...")
        all_feed_signals, feed_stats = run_all_feeds(self.uw)

        log.info("Feeds complete: {} signals across {} tickers  API calls: ~{}".format(
            len(all_feed_signals),
            len(set(s.symbol for s in all_feed_signals)),
            feed_stats["api_calls"] + 3
        ))

        # Separate news signals -- send as digest, don't score as trade signals
        news_signals  = [s for s in all_feed_signals if s.feed_type == "news"]
        trade_signals = [s for s in all_feed_signals if s.feed_type != "news"]

        if news_signals:
            log.info("News: {} items -- sending digest".format(len(news_signals)))
            self.telegram.send_news_digest(news_signals)

        # Replace all_feed_signals with trade-only signals for scoring
        all_feed_signals = trade_signals

        if not all_feed_signals:
            log.info("No feed signals this cycle.")
            close_scan_run(run_id, 0, 0, 0)
            return scored

        # Get unique tickers with signals
        unique_symbols = list(dict.fromkeys(s.symbol for s in all_feed_signals))

        # ── Phase 2: Selective enrichment (budget + market-hours aware) ────────
        # Each enrichment = 3 API calls (greek-exposure + spot-exposures + darkpool)
        # Market hours: top 20 symbols (60 calls). Off-hours: top 5 (15 calls).
        # Budget guard: if daily usage > 85%, cap to 5; if > 95%, skip entirely.
        enrich_cap = 20 if self._is_market_hours() else 5
        try:
            usage = self.uw.get_usage_stats()
            pct_used = usage.get("daily_pct", 0)
            if pct_used >= 95:
                enrich_cap = 0
                log.warning("UW budget {}% used -- skipping enrichment this cycle".format(
                    pct_used))
            elif pct_used >= 85:
                enrich_cap = min(enrich_cap, 5)
                log.warning("UW budget {}% used -- enrichment capped at 5".format(
                    pct_used))
        except Exception:
            pass

        options_signals_sorted = sorted(
            [s for s in all_feed_signals if s.feed_type == "options_flow"],
            key=lambda x: x.score, reverse=True
        )
        options_symbols = list(dict.fromkeys(
            s.symbol for s in options_signals_sorted
        ))[:enrich_cap]

        log.info("Enriching top {} options-flow symbols with dealer exposure...".format(
            len(options_symbols)))
        for symbol in options_symbols:
            # Force-refresh: drop stale cache entry so _enrich_symbol fetches fresh data
            self._exposure_cache.pop(symbol, None)
            self._enrich_symbol(symbol, run_id)
            time.sleep(0.20)   # 200ms spacing = max 5 symbols/sec = 15 calls/sec

        # ── Phase 3: Score all feed signals ───────────────────────────────────
        for fsig in all_feed_signals:

            # Options flow signals go through full scoring pipeline
            if fsig.feed_type == "options_flow":
                sig = self._score_options_signal(
                    fsig, run_id, portfolio, all_feed_signals)
            else:
                # Non-options feeds: lighter scoring, no greeks needed
                sig = self._score_context_signal(
                    fsig, run_id, portfolio, all_feed_signals)

            if sig is None:
                continue

            # Cross-feed context
            ctx = get_cross_feed_context(fsig.symbol, all_feed_signals)
            sig.feeds_confirming  = ctx["feeds_confirming"]
            sig.cross_feed_score  = ctx["cross_feed_score"]
            sig.cross_feed_summary = ctx["summary"]

            # Apply cross-feed score bonus to composite
            if len(ctx["feeds_confirming"]) > 1:
                sig.composite_score = min(
                    sig.composite_score + ctx["cross_feed_score"], 100.0)

            scored.append(sig)

            log.info("SCORE  {} [{:<14}] {}{}  score={:.0f}  {}  feeds={}".format(
                fsig.symbol, fsig.feed_type,
                fsig.strike if fsig.strike else "",
                fsig.right  if fsig.right  else "",
                sig.composite_score, fsig.intent,
                len(ctx["feeds_confirming"])
            ))

        # ── Phase 4: Classify + alert ─────────────────────────────────────────
        scored = self._classify_signals(scored)

        # Separate options signals (individual alerts) from context signals (digest)
        options_alerts = [
            s for s in scored
            if getattr(s, "_feed_type", "") == "options_flow"
            and s.composite_score >= cfg.MIN_SCORE_ALERT
            and s.signal_type not in ("hedging", "conflict", "noise")
            and s.strike > 0
            and s.expiry
        ]
        context_signals = [
            s for s in scored
            if getattr(s, "_feed_type", "") != "options_flow"
            and s.composite_score >= cfg.MIN_SCORE_ALERT
        ]

        # Deduplicate options alerts by symbol+strike+right+expiry
        seen_options = set()
        deduped_options = []
        for s in sorted(options_alerts, key=lambda x: x.composite_score, reverse=True):
            key = "{}_{}_{}_{}".format(s.symbol, s.strike, s.right, s.expiry)
            if key not in seen_options:
                seen_options.add(key)
                deduped_options.append(s)

        # Send individual alerts for top options signals only (max 5 per cycle)
        # Off-hours: skip individual sends -- signals are stored in DB and
        # batched into the 4-hourly off-hours digest instead
        market_open = self._is_market_hours()
        for sig in deduped_options[:5]:
            key = self._signal_key(sig)
            if key in self._alerted:
                continue
            if not market_open:
                self._alerted.add(key)   # mark seen so it batches, not re-alerts
                log.info("OFF-HOURS (batched)  {} {}{}  {}  score={:.0f}".format(
                    sig.symbol, sig.strike, sig.right, sig.expiry,
                    sig.composite_score))
                continue

            # No live quote -- alert anyway with warning (signal still valuable)
            if sig.live_ask <= 0:
                sig._no_quote = True
                log.info("ALERT (no quote)  {} {}{}  {}  score={:.0f}".format(
                    sig.symbol, sig.strike, sig.right, sig.expiry,
                    sig.composite_score
                ))

            # Aggregate today's total premium + prints BEFORE alert gate
            try:
                from database.db import get_connection
                with get_connection() as conn:
                    row = conn.execute(
                        "SELECT SUM(COALESCE(premium, 0)), COUNT(*) "
                        "FROM scored_signals "
                        "WHERE date(scored_at) = date('now', 'localtime') "
                        "AND symbol = ? AND strike = ? AND right = ? AND expiry = ?",
                        (sig.symbol, sig.strike, sig.right, sig.expiry)
                    ).fetchone()
                    if row:
                        sig.total_premium_today = row[0] or sig.premium
                        sig.n_prints_today      = row[1] or 1
            except Exception as e:
                log.debug("Premium aggregation error: {}".format(e))
                sig.total_premium_today = sig.premium
                sig.n_prints_today      = 1

            # Gate: total premium across today's prints must exceed $500k
            if (sig.total_premium_today or 0) < cfg.MIN_ALERT_PREMIUM:
                prem_k = (sig.total_premium_today or 0) / 1000
                log.info("SKIP (low premium ${:.0f}k < ${:.0f}k)  {} {}{}  {}  score={:.0f}".format(
                    prem_k, cfg.MIN_ALERT_PREMIUM / 1000,
                    sig.symbol, sig.strike, sig.right, sig.expiry,
                    sig.composite_score))
                continue

            self._alerted.add(key)
            sig.alert_sent = True
            n_alerts += 1

            self.telegram.send(self.telegram.format_signal(sig))
            log.info("ALERT  {} {}{}  {}  type={}  score={:.0f}  feeds={}".format(
                sig.symbol, sig.strike, sig.right, sig.expiry,
                sig.signal_type, sig.composite_score,
                len(sig.feeds_confirming or [])
            ))

        # Context digest disabled -- too noisy
        # if context_signals:
        #     self.telegram.send_context_digest(context_signals, deduped_options)

        # Check open positions for exit alerts
        exit_alerts = self._tracker.check_all_positions()
        if exit_alerts:
            log.info("{} exit alert(s) fired this cycle.".format(len(exit_alerts)))

        close_scan_run(run_id, len(unique_symbols), len(scored), n_alerts)
        log.info("── Done: {} signals, {} alerts, ~{} API calls ──\n".format(
            len(scored), n_alerts,
            feed_stats["api_calls"] + 3 + len(options_symbols) * 3
        ))
        if n_alerts:
            self.telegram.send_cycle_summary(len(scored), n_alerts)
        return scored

    # ── Options flow scoring (full pipeline) ──────────────────────────────────
    def _log_evidence(self, fsig, exp, add_ev: dict, inst_ev: dict,
                       add: float, inst: float, comp: float,
                       iv_pct: float, dp_sentiment: str):
        """
        Write one row to score_evidence for every scored options_flow print.
        This enables post-hoc weight analysis:
          - Which components actually predict wins?
          - Are the weights (55/45 additive/institutional) optimal?
          - Does iv_bonus matter at all? Does darkpool confirm anything?
        """
        from database.db import get_connection
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO score_evidence (
                    scored_at, symbol, strike, right, expiry, intent,
                    premium, structure,
                    ev_structure, ev_intent, ev_uw_score, ev_iv_pct,
                    ev_iv_bonus, ev_gex_regime, ev_darkpool, ev_premium_size,
                    additive_total,
                    ev_vex, ev_chex, ev_flow_dir, ev_dex, ev_ask_side,
                    institutional_total,
                    iv_pct, gex_regime, flow_direction,
                    net_vex, net_chex, net_dex, darkpool_sent,
                    composite_score
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                now, fsig.symbol, fsig.strike, fsig.right, fsig.expiry, fsig.intent,
                fsig.premium, fsig.structure,
                add_ev.get("structure",    0),
                add_ev.get("intent",       0),
                add_ev.get("uw_score",     0),
                add_ev.get("iv_pct_raw",   0),
                add_ev.get("iv_bonus",     0),
                add_ev.get("gex_regime",   0),
                add_ev.get("darkpool",     0),
                add_ev.get("premium_size", 0),
                add,
                inst_ev.get("vex",      0),
                inst_ev.get("chex",     0),
                inst_ev.get("flow_dir", 0),
                inst_ev.get("dex",      0),
                inst_ev.get("ask_side", 0),
                inst,
                iv_pct,
                getattr(exp, "regime",         ""),
                getattr(exp, "flow_direction",  ""),
                getattr(exp, "net_vex",         0) or 0,
                getattr(exp, "net_chex",        0) or 0,
                getattr(exp, "net_dex",         0) or 0,
                dp_sentiment,
                comp,
            ))

    def _score_options_signal(
        self, fsig, run_id, portfolio, all_feed_signals
    ):
        from models import FlowSignal

        if not self._valid_dte(fsig.expiry):
            try:
                dte = (date.fromisoformat(fsig.expiry) - date.today()).days
            except Exception:
                dte = "?"
            log.debug("SKIP   {} {}{}  {}  DTE={} outside [{},{}]".format(
                fsig.symbol, fsig.strike, fsig.right, fsig.expiry,
                dte, cfg.MIN_DTE, cfg.MAX_DTE))
            return None

        exp = self._exposure_cache.get(fsig.symbol)
        # Bug fix: previously fell back to blank DealerExposure silently,
        # which inflated institutional scores for unenriched symbols.
        # Now we track whether enrichment happened and apply a penalty.
        unenriched = exp is None
        if unenriched:
            exp = DealerExposure(fsig.symbol)

        insert_oi_snapshot(
            scan_run_id=run_id, symbol=fsig.symbol,
            strike=fsig.strike, right=fsig.right, expiry=fsig.expiry,
            open_interest=fsig.oi, volume=fsig.volume,
            iv=fsig.iv, delta=0.0,
        )

        oi_bonus     = get_oi_buildup_score(fsig.symbol, fsig.strike, fsig.right, fsig.expiry)
        iv_pct       = self.uw.get_iv_percentile(fsig.symbol)
        dp_trend     = get_dp_sentiment_trend(fsig.symbol, lookback_scans=5)
        dp_now       = self.uw.get_darkpool_sentiment(fsig.symbol)
        dp_sentiment = dp_trend if dp_trend != "neutral" else dp_now
        gex_trend    = get_gex_trend_score(fsig.symbol)

        # Create FlowSignal for scorer compatibility
        flow_sig = FlowSignal(
            symbol=fsig.symbol, strike=fsig.strike, expiry=fsig.expiry,
            right=fsig.right, structure=fsig.structure, intent=fsig.intent,
            score=fsig.score, iv=fsig.iv, premium=fsig.premium,
            ask_side=fsig.ask_side, oi=fsig.oi, volume=fsig.volume,
        )

        add,  add_ev  = self.additive.score_with_evidence(flow_sig, exp, iv_pct, dp_sentiment)
        add             = min(add + oi_bonus, 100.0)
        inst, inst_ev   = self.inst.score_with_evidence(flow_sig, exp)
        inst            = min(max(inst + gex_trend, 0), 100.0)
        # Bug fix: unenriched symbols had no real dealer data — penalize to
        # prevent blank DealerExposure from silently inflating institutional score
        if unenriched:
            inst = max(inst - 10.0, 0.0)
            log.debug("UNENRICHED penalty -10 inst  {} → {:.1f}".format(
                fsig.symbol, inst))
        comp            = composite_score(add, inst)

        # Log full evidence breakdown for every print (for weight tuning)
        try:
            self._log_evidence(fsig, exp, add_ev, inst_ev, add, inst, comp,
                               iv_pct, dp_sentiment)
        except Exception as _ev_err:
            log.debug("Evidence log error: {}".format(_ev_err))

        if comp < cfg.MIN_SCORE_LOG:
            return None

        quote = self._get_quote(fsig.symbol, fsig.expiry, fsig.strike, fsig.right)
        ask   = quote.get("ask",   0.0)
        bid   = quote.get("bid",   0.0)
        delta = abs(quote.get("delta", 0.0))
        iv    = quote.get("iv", fsig.iv)

        # Delta range is informational only -- don't penalise OTM flow
        # Institutions frequently buy OTM options for leverage/hedging
        # if delta > 0 and not (cfg.TARGET_DELTA_MIN <= delta <= cfg.TARGET_DELTA_MAX):
        #     comp = max(comp - 10, 0.0)

        blocked = self._gex_blocked(fsig.intent)
        sz      = calculate_position_size(portfolio, ask, delta)

        sig = ScoredSignal(
            symbol=fsig.symbol, strike=fsig.strike, expiry=fsig.expiry,
            right=fsig.right, uw_score=fsig.score,
            additive_score=add, institutional_score=inst, composite_score=comp,
            intent=fsig.intent, structure=fsig.structure,
            premium=fsig.premium, ask_side=fsig.ask_side,
            dealer_gex=exp.net_gex, dealer_dex=exp.net_dex,
            dealer_vex=exp.net_vex, dealer_chex=exp.net_chex,
            dealer_regime=exp.regime, gamma_flip=exp.gamma_flip,
            call_wall=exp.call_wall, put_wall=exp.put_wall,
            gex_regime_blocked=blocked, flow_direction=exp.flow_direction,
            darkpool_sentiment=dp_sentiment, iv_percentile=iv_pct,
            live_bid=bid, live_ask=ask, live_delta=delta, live_iv=iv,
            suggested_contracts=sz["contracts"], max_loss_dollar=sz["max_loss"],
            target_exit_premium=sz["target_exit"], stop_premium=sz["stop_premium"],
        )
        sig._feed_type = "options_flow"

        # Persist
        flow_model = FlowSignal(
            symbol=fsig.symbol, strike=fsig.strike, expiry=fsig.expiry,
            right=fsig.right, structure=fsig.structure, intent=fsig.intent,
            score=fsig.score, premium=fsig.premium, ask_side=fsig.ask_side,
            oi=fsig.oi, volume=fsig.volume, iv=fsig.iv,
        )
        fs_id = insert_flow_signal(flow_model, run_id)
        db_id = insert_scored_signal(sig, run_id, fs_id)
        sig._db_id = db_id
        self.excel.log_signal(sig)

        ml = self._predictor.score_signal(db_id or 0)
        sig.ml_type = ml["ml_type"]; sig.ml_confidence = ml["ml_confidence"]
        sig.ml_win_prob = ml["ml_win_prob"]; sig.ml_available = ml["ml_available"]

        return sig

    # ── Non-options feed scoring (congress, insider, dark pool, etc.) ─────────
    def _score_context_signal(
        self, fsig, run_id, portfolio, all_feed_signals
    ):
        """
        Score non-options feed signals (congress, insider, dark pool, etc.).
        These don't have strikes/expiry -- scored as context signals.
        Composite = feed score directly (already 0-100).
        """
        if fsig.score < cfg.MIN_SCORE_LOG:
            return None

        comp = float(fsig.score)

        sig = ScoredSignal(
            symbol=fsig.symbol, strike=0.0, expiry="", right="",
            uw_score=fsig.score,
            additive_score=comp, institutional_score=comp,
            composite_score=comp,
            intent=fsig.intent,
            structure=fsig.feed_type,   # feed type as structure label
            premium=fsig.premium,
            ask_side=False,
            dealer_gex=0.0, dealer_dex=0.0, dealer_vex=0.0, dealer_chex=0.0,
            dealer_regime="unknown",
        )
        sig._feed_type = fsig.feed_type

        self.excel.log_signal(sig)
        return sig

    # ── Morning report (FlowPatrol-style) ─────────────────────────────────────
    def _maybe_send_morning_report(self):
        """
        Send a FlowPatrol-style briefing once per weekday at/after 7:30 AM ET
        (before market open at 9:30). Uses prior trading day's data from DB
        plus current SPY GEX and market tide.
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo

        now   = datetime.now(ZoneInfo("America/New_York"))
        today = now.strftime("%Y-%m-%d")

        if now.weekday() >= 5:                      # weekends off
            return
        if not (7 <= now.hour < 9 or (now.hour == 9 and now.minute < 30)):
            return                                   # outside 7:00-9:29 window
        if now.hour == 7 and now.minute < 30:
            return                                   # wait until 7:30
        if self._morning_sent_date == today:
            return                                   # already sent today

        # File-based gate -- survives watchdog restarts
        from pathlib import Path
        gate_path = Path(cfg.EXCEL_PATH).parent / "morning_report_sent.txt"
        try:
            if gate_path.exists() and gate_path.read_text().strip() == today:
                self._morning_sent_date = today
                return
        except Exception:
            pass

        self._morning_sent_date = today
        try:
            gate_path.write_text(today)
        except Exception:
            pass

        from database.db import get_connection

        def _pctile(values, x):
            """Percentile rank of x within values (0-100)."""
            if not values:
                return None
            below = sum(1 for v in values if v <= x)
            return round(100.0 * below / len(values))

        with get_connection() as conn:
            # Prior trading day = most recent scored date before today
            row = conn.execute(
                "SELECT MAX(date(scored_at)) FROM scored_signals "
                "WHERE date(scored_at) < date('now', 'localtime')"
            ).fetchone()
            prior_day = row[0] if row and row[0] else None

            conviction, top_premium = [], []
            if prior_day:
                # Multi-feed conviction names (score >= 80 best of day)
                rows = conn.execute(
                    "SELECT symbol, MAX(composite_score) AS best, intent "
                    "FROM scored_signals "
                    "WHERE date(scored_at) = ? "
                    "GROUP BY symbol HAVING best >= 80 "
                    "ORDER BY best DESC LIMIT 8",
                    (prior_day,)
                ).fetchall()
                for r in rows:
                    conviction.append({
                        "symbol": r[0], "score": r[1] or 0,
                        "intent": r[2] or "neutral", "feeds": "?",
                    })

                # Biggest premium contracts of prior day
                rows = conn.execute(
                    "SELECT symbol, strike, right, expiry, intent, "
                    "       SUM(COALESCE(premium,0)) AS prem, COUNT(*) AS prints "
                    "FROM scored_signals "
                    "WHERE date(scored_at) = ? AND strike > 0 "
                    "GROUP BY symbol, strike, right, expiry "
                    "ORDER BY prem DESC LIMIT 8",
                    (prior_day,)
                ).fetchall()
                for r in rows:
                    top_premium.append({
                        "symbol": r[0], "strike": r[1], "right": r[2],
                        "expiry": r[3], "intent": r[4] or "neutral",
                        "premium": r[5] or 0, "prints": r[6] or 1,
                    })

            # ── Dealer positioning per symbol (FlowPatrol style) ──────────────
            # Latest prior-day exposure per conviction/premium symbol,
            # with GEX percentile vs that symbol's own 10-day history.
            dealer_positions = []
            focus_syms = []
            for c in conviction:
                if c["symbol"] not in focus_syms:
                    focus_syms.append(c["symbol"])
            for t in top_premium:
                if t["symbol"] not in focus_syms:
                    focus_syms.append(t["symbol"])

            for sym in focus_syms[:10]:
                # Most recent snapshot before today
                row = conn.execute(
                    "SELECT net_gex, net_dex, regime, gamma_flip, captured_at "
                    "FROM dealer_exposure "
                    "WHERE symbol = ? AND date(captured_at) < date('now','localtime') "
                    "ORDER BY captured_at DESC LIMIT 1",
                    (sym,)
                ).fetchone()
                if not row or row[0] is None:
                    continue

                gex, dex, regime_s, flip_s = row[0], row[1], row[2], row[3]

                # 10-day GEX history (one value per day: last snapshot of day)
                hist = conn.execute(
                    "SELECT MAX(net_gex) FROM dealer_exposure "
                    "WHERE symbol = ? "
                    "AND date(captured_at) >= date('now','localtime','-10 days') "
                    "GROUP BY date(captured_at)",
                    (sym,)
                ).fetchall()
                gex_hist = [h[0] for h in hist if h[0] is not None]
                gex_pct  = _pctile(gex_hist, gex)

                dealer_positions.append({
                    "symbol":     sym,
                    "gex_m":      (gex or 0) / 1e6,
                    "dex_m":      (dex or 0) / 1e6,
                    "regime":     regime_s or "unknown",
                    "gamma_flip": flip_s,
                    "gex_pctile": gex_pct,
                })

        # Watch list = union of conviction + top premium symbols
        watch = focus_syms

        # Current SPY GEX + market tide (fresh, cheap calls)
        spy_gex_str, regime, flip = "n/a", "unknown", None
        try:
            self._spy_exposure = self.uw.get_dealer_exposure("SPY")
            gex_m = (self._spy_exposure.net_gex or 0) / 1e6
            spy_gex_str = "${:.0f}M".format(gex_m)
            regime = self._spy_exposure.regime or "unknown"
            flip   = self._spy_exposure.gamma_flip or None
        except Exception as e:
            log.debug("Morning report GEX fetch failed: {}".format(e))

        tide = "n/a"
        try:
            tide_data = self.uw.get_market_tide() or {}
            tide = tide_data.get("sentiment", "n/a")
        except Exception:
            tide = self._market_tide.get("sentiment", "n/a") if self._market_tide else "n/a"

        report = {
            "date":             now.strftime("%A, %B %d, %Y"),
            "spy_gex":          spy_gex_str,
            "regime":           regime,
            "gamma_flip":       flip,
            "market_tide":      tide,
            "conviction_names": conviction,
            "top_premium":      top_premium,
            "dealer_positions": dealer_positions,
            "watch_today":      watch,
            "scan_interval":    cfg.SCAN_INTERVAL_SEC,
        }
        self.telegram.send_morning_report(report)
        log.info("Morning report sent: {} conviction names, {} big-flow contracts".format(
            len(conviction), len(top_premium)))

    # ── Budget impact warning ──────────────────────────────────────────────────
    def _maybe_warn_budget_impact(self):
        """
        Checked on every cycle. If usage crosses 70% while market is still
        open (i.e. off-hours + intraday calls combined are eating into the
        budget faster than expected), send one Telegram warning per day so
        you know enrichment may get capped before the close.
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo

        now   = datetime.now(ZoneInfo("America/New_York"))
        today = now.strftime("%Y-%m-%d")

        if not self._is_market_hours():
            return
        if self._budget_warned_date == today:
            return

        try:
            usage = self.uw.get_usage_stats()
        except Exception:
            return

        pct  = usage.get("daily_pct", 0)
        used = usage.get("daily_used", 0)
        lim  = usage.get("daily_limit", 0)

        if pct >= 70:
            self._budget_warned_date = today
            mins_to_close = max(0, (16 * 60 + 5) - (now.hour * 60 + now.minute))
            self.telegram.send(
                "⚠️ <b>API BUDGET WARNING</b>\n"
                "Usage: {}/{} ({:.0f}%) at {} ET\n"
                "{} min remain to close.\n"
                "Enrichment may get capped (≥85%) or skipped (≥95%) "
                "for the rest of today's session.".format(
                    used, lim, pct, now.strftime("%H:%M"), mins_to_close)
            )
            log.warning("Budget warning sent: {}% used at {}".format(
                pct, now.strftime("%H:%M")))

    # ── Off-hours digest (every 4 hours while market closed) ──────────────────
    def _maybe_send_offhours_digest(self):
        """
        While the market is closed, send a consolidated digest every 4 hours
        covering signals scored since the last notification. Quiet if nothing
        new. First one fires ~8 PM ET (4h after close).
        """
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        if self._is_market_hours():
            return

        now = datetime.now(ZoneInfo("America/New_York"))

        # Anchor: market close today (or last close if overnight/weekend)
        if self._last_offhours_notify is None:
            anchor = now.replace(hour=16, minute=0, second=0, microsecond=0)
            if now < anchor:                       # pre-market -> last day 4 PM
                anchor -= timedelta(days=1)
            self._last_offhours_notify = anchor

        if now - self._last_offhours_notify < timedelta(hours=3, minutes=30):
            return   # cycle spacing (4h) is the primary gate

        # File-based gate -- survives watchdog restarts
        # Store last send time in file so restart doesn't re-send immediately
        from pathlib import Path
        gate_path = Path(cfg.EXCEL_PATH).parent / "offhours_digest_sent.txt"
        try:
            if gate_path.exists():
                saved_str = gate_path.read_text().strip()
                if saved_str:
                    saved_dt = datetime.fromisoformat(saved_str)
                    if now - saved_dt < timedelta(hours=3, minutes=30):
                        self._last_offhours_notify = saved_dt
                        return   # sent recently, skip
        except Exception:
            pass

        since_iso = self._last_offhours_notify.strftime("%Y-%m-%d %H:%M:%S")
        self._last_offhours_notify = now
        try:
            gate_path.write_text(now.isoformat())
        except Exception:
            pass

        from database.db import get_connection
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT symbol, strike, right, expiry, intent, "
                "       MAX(composite_score) AS best, "
                "       SUM(COALESCE(premium,0)) AS prem, COUNT(*) AS prints "
                "FROM scored_signals "
                "WHERE scored_at >= ? AND composite_score >= ? AND strike > 0 "
                "GROUP BY symbol, strike, right, expiry "
                "ORDER BY best DESC LIMIT 6",
                (since_iso, cfg.MIN_SCORE_ALERT)
            ).fetchall()

        if not rows:
            log.info("Off-hours digest: nothing new since {} -- skipping".format(
                since_iso))
            return

        sep   = "-" * 22
        lines = ["🌙 <b>OFF-HOURS FLOW DIGEST</b>",
                 "<i>last 4 hours</i>", sep]
        for r in rows:
            e = ("🟢" if r[4] == "bullish" else "🔴" if r[4] == "bearish" else "⚪")
            prem = r[6] or 0
            prem_str = ("${:.1f}M".format(prem/1e6) if prem >= 1e6
                        else "${:.0f}k".format(prem/1e3) if prem > 0 else "n/a")
            lines.append("{} <b>{}</b>  {}{}  {}".format(
                e, r[0], r[1], r[2], r[3]))
            lines.append("   score={:.0f}  {}  ({} prints)".format(
                r[5] or 0, prem_str, r[7] or 1))
        lines += [sep, "<i>{} ET -- next digest in 4h or at market open</i>".format(
            now.strftime("%H:%M"))]
        self.telegram.send("\n".join(lines))
        log.info("Off-hours digest sent: {} contracts".format(len(rows)))

    # ── Daily summary ─────────────────────────────────────────────────────────
    def _maybe_send_daily_summary(self):
        """
        Send end-of-day summary once per trading day after 4:00 PM ET.
        Pulls today's stats from the database.
        Uses a file-based gate so watchdog restarts don't re-send.
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from pathlib import Path

        now = datetime.now(ZoneInfo("America/New_York"))
        today = now.strftime("%Y-%m-%d")

        # Only weekdays, only after 4:00 PM ET, only once per day
        if now.weekday() >= 5:
            return
        if now.hour < 16:
            return
        if self._summary_sent_date == today:
            return

        # File-based gate -- survives watchdog restarts
        gate_path = Path(cfg.EXCEL_PATH).parent / "daily_summary_sent.txt"
        try:
            if gate_path.exists() and gate_path.read_text().strip() == today:
                self._summary_sent_date = today  # sync in-memory flag
                return
        except Exception:
            pass

        self._summary_sent_date = today
        try:
            gate_path.write_text(today)
        except Exception:
            pass

        # Pull today's stats from database (column is scored_at)
        from database.db import get_connection
        with get_connection() as conn:
            # Total signals scored today
            row = conn.execute(
                "SELECT COUNT(*) FROM scored_signals "
                "WHERE date(scored_at) = date('now', 'localtime')"
            ).fetchone()
            total_signals = row[0] if row else 0

            # Top 8 unique symbols by best score today
            # Includes total premium and number of prints (signals) per symbol
            rows = conn.execute(
                "SELECT symbol, strike, right, expiry, "
                "       MAX(composite_score) AS best_score, intent, signal_type, "
                "       SUM(COALESCE(premium, 0)) AS total_premium, "
                "       COUNT(*) AS n_prints "
                "FROM scored_signals "
                "WHERE date(scored_at) = date('now', 'localtime') "
                "GROUP BY symbol "
                "ORDER BY best_score DESC LIMIT 8"
            ).fetchall()

        # Alerts sent today tracked in memory (deduped alert keys)
        total_alerts = len(self._alerted)

        top_signals = []
        for r in rows:
            top_signals.append({
                "symbol":        r[0],
                "strike":        r[1] if r[1] and r[1] > 0 else None,
                "right":         r[2] or "",
                "expiry":        r[3] or "",
                "score":         r[4] or 0,
                "intent":        r[5] or "neutral",
                "feed":          r[6] or "?",
                "total_premium": r[7] or 0,
                "n_prints":      r[8] or 0,
            })

        tide = self._market_tide.get("sentiment", "n/a") if self._market_tide else "n/a"

        summary = {
            "date":          today,
            "total_signals": total_signals,
            "total_alerts":  total_alerts,
            "top_signals":   top_signals,
            "feed_counts":   {},
            "market_tide":   tide,
        }
        self.telegram.send_daily_summary(summary)
        log.info("Daily summary sent: {} signals, {} alerts".format(
            total_signals, total_alerts))

    # ── Active Signal P&L Digest (every 2 hours, market hours) ───────────────
    def _maybe_send_signal_pnl_digest(self):
        """
        Every 30 minutes during market hours, fetch current prices for all
        bot-alerted signals whose expiry has not yet passed.
        Filters: live_ask > 0, composite_score >= MIN_SCORE_ALERT,
                 strike > 0, expiry >= today.
        Sorted: latest signal date first.
        Zero UW API calls -- Schwab/IBKR quotes only.
        """
        import datetime as _dt

        if not self._is_market_hours():
            return

        if not hasattr(self, '_last_signal_pnl_time'):
            self._last_signal_pnl_time = None

        now = _dt.datetime.now()
        if (self._last_signal_pnl_time is not None and
                (now - self._last_signal_pnl_time).total_seconds() < 7200):
            return  # 2-hour gate

        self._last_signal_pnl_time = now

        try:
            from database.db import get_connection
            from datetime import datetime, date
            from zoneinfo import ZoneInfo

            today_str = date.today().isoformat()

            # Pull all alerted signals that haven't expired yet
            # Only unidirectional symbols (exclude if both bull+bear signals exist)
            with get_connection() as conn:
                # Step 1: find symbols with BOTH bullish and bearish signals (conflicts)
                conflict_rows = conn.execute("""
                    SELECT symbol
                    FROM scored_signals
                    WHERE live_ask > 0
                      AND composite_score >= ?
                      AND strike > 0
                      AND expiry >= ?
                    GROUP BY symbol
                    HAVING COUNT(DISTINCT intent) > 1
                       AND SUM(CASE WHEN intent='bullish' THEN 1 ELSE 0 END) > 0
                       AND SUM(CASE WHEN intent='bearish' THEN 1 ELSE 0 END) > 0
                """, (70, today_str)).fetchall()
                conflict_symbols = {r[0] for r in conflict_rows}

                # Step 2: fetch signals excluding conflict symbols
                rows = conn.execute("""
                    SELECT symbol, strike, right, expiry, intent,
                           live_ask AS entry_price, composite_score,
                           target_premium, stop_premium,
                           MAX(scored_at) AS last_seen
                    FROM scored_signals
                    WHERE live_ask > 0
                      AND composite_score >= ?
                      AND strike > 0
                      AND expiry != ''
                      AND expiry >= ?
                    GROUP BY symbol, strike, right, expiry
                    ORDER BY MAX(scored_at) DESC
                """, (70, today_str)).fetchall()

            # Filter out conflict symbols — only show unidirectional signals
            rows = [r for r in rows if r[0] not in conflict_symbols]

            if not rows:
                log.debug("Signal P&L digest: no active non-expired signals")
                return

            signals = [dict(r) for r in rows]

            # Fetch current prices and build digest
            sep   = "-" * 22
            conflict_note = ""
            if conflict_symbols:
                conflict_note = "  (⚔️ {} filtered)".format(len(conflict_symbols))
            lines = ["📊 <b>ACTIVE SIGNAL P&L — Unidirectional Only</b>",
                     "<i>{} signals{}</i>".format(len(signals), conflict_note),
                     sep]

            total_pnl_pct_sum = 0.0
            total_count       = 0
            winners           = 0
            losers            = 0
            no_quote_count    = 0

            for sig in signals[:15]:  # cap at 15 to avoid Telegram limits
                symbol = sig["symbol"]
                strike = sig["strike"]
                right  = sig["right"]
                expiry = sig["expiry"]
                entry  = sig["entry_price"]
                target = sig["target_premium"] or (entry * 1.65)
                stop   = sig["stop_premium"]   or (entry * 0.30)
                intent = sig["intent"] or "neutral"
                score  = sig["composite_score"] or 0
                last_seen = (sig["last_seen"] or "")[:10]  # date only

                # DTE
                try:
                    dte = (date.fromisoformat(expiry) - date.today()).days
                    dte_str = "{}d".format(dte)
                except Exception:
                    dte_str = "?"

                # Fetch current price via Schwab first, then IBKR
                current = 0.0
                try:
                    if self.schwab.available:
                        q = self.schwab.get_option_quote(symbol, expiry, strike, right)
                        if q and q.get("ask", 0) > 0:
                            current = round((q["bid"] + q["ask"]) / 2, 2)
                except Exception as e:
                    log.debug("P&L Schwab quote error {}: {}".format(symbol, e))

                if current <= 0:
                    try:
                        q = self.ibkr.get_option_quote(
                            symbol, expiry.replace("-", ""), strike, right)
                        if q and q.get("ask", 0) > 0:
                            current = round((q["bid"] + q["ask"]) / 2, 2)
                    except Exception:
                        pass

                intent_emoji = ("🟢" if intent == "bullish"
                                else "🔴" if intent == "bearish"
                                else "⚪")
                if current > 0 and entry > 0:
                    pnl_pct = (current - entry) / entry * 100

                    if current >= target:
                        status_emoji = "🎯"  # target hit
                    elif current <= stop:
                        status_emoji = "🛑"  # at stop
                    elif pnl_pct >= 30:
                        status_emoji = "🔥"  # strong gain
                    elif pnl_pct >= 0:
                        status_emoji = "✅"  # in profit
                    elif pnl_pct >= -30:
                        status_emoji = "⚠️"  # moderate loss
                    else:
                        status_emoji = "❌"  # heavy loss

                    lines.append(
                        "{}{} <b>{}</b>  {}{}  {}  ({}dte)".format(
                            status_emoji, intent_emoji,
                            symbol, strike, right, expiry, dte_str
                        )
                    )
                    lines.append(
                        "   Entry: {:.2f} → Now: {:.2f}  "
                        "<b>{:+.1f}%</b>  score={:.0f}  <i>{}</i>".format(
                            entry, current, pnl_pct, score, last_seen
                        )
                    )

                    total_pnl_pct_sum += pnl_pct
                    total_count       += 1
                    if pnl_pct >= 0:
                        winners += 1
                    else:
                        losers += 1
                else:
                    lines.append(
                        "⬜{} <b>{}</b>  {}{}  {}  ({}dte)  — no quote".format(
                            intent_emoji, symbol, strike, right, expiry, dte_str
                        )
                    )
                    lines.append(
                        "   Entry: {:.2f}  score={:.0f}  <i>{}</i>".format(
                            entry, score, last_seen
                        )
                    )
                    no_quote_count += 1

                lines.append(sep)

            # Summary footer
            if total_count > 0:
                avg_pnl = total_pnl_pct_sum / total_count
                lines.append(
                    "<b>Summary:</b>  {} quoted  |  "
                    "✅ {}W  ❌ {}L  |  "
                    "Avg P&L: <b>{:+.1f}%</b>".format(
                        total_count, winners, losers, avg_pnl
                    )
                )
                if no_quote_count:
                    lines.append(
                        "<i>{} signal(s) with no live quote</i>".format(
                            no_quote_count))
            else:
                lines.append("<i>No live quotes available</i>")

            if len(signals) > 15:
                lines.append(
                    "<i>Showing top 15 of {} active signals</i>".format(
                        len(signals)))

            et_now = datetime.now(ZoneInfo("America/New_York"))
            lines.append(
                "<i>{} ET — updates every 30 min</i>".format(
                    et_now.strftime("%H:%M")
                )
            )

            self.telegram.send("\n".join(lines))
            log.info("Signal P&L digest: {} active signals, {} quoted, avg {:.1f}%".format(
                len(signals), total_count,
                total_pnl_pct_sum / total_count if total_count else 0
            ))

        except Exception as e:
            log.error("Signal P&L digest error: {}".format(e))

    # ── Main loop ─────────────────────────────────────────────────────────────
    def _is_market_hours(self) -> bool:
        """True during regular session: weekdays 9:25 AM - 4:05 PM ET."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
        if now.weekday() >= 5:
            return False
        mins = now.hour * 60 + now.minute
        return (9 * 60 + 25) <= mins <= (16 * 60 + 5)

    def _current_interval(self) -> int:
        """
        Scan interval: cfg value during market hours, 4 hours off-hours.
        Off-hours cycles land ~8 PM, 12 AM, 4 AM, 8 AM ET -- each runs the
        feeds once (~12 calls) and sends the off-hours digest if there is
        new qualifying flow.
        """
        return cfg.SCAN_INTERVAL_SEC if self._is_market_hours() else 14400

    def _adaptive_wait(self):
        """
        Adaptive polling with budget guard:

        MARKET HOURS (9:25 AM - 4:05 PM ET)
          Poll UW every 30s with a 1-call lightweight check (get_latest_flow_id).
          Trigger a full scan when new flow is detected, BUT enforce a minimum
          gap of MIN_SCAN_GAP_SEC (120s) between full scans regardless.

          BUDGET MATH:
            Polls:      780/day × 1 call  =    780 calls
            Max scans:  195/day × 75 calls = 14,625 calls
            Off-hours:  4 cycles × 15 calls =    60 calls
            TOTAL:                          ~15,465 calls (77% of 20k budget)

          Without the MIN_SCAN_GAP guard, if UW updates every 30s (common
          at open/close), you'd trigger 780 full scans = 58,500 calls → 3×
          over budget.

        OFF-HOURS
          Sleep the full 4-hour interval -- no adaptive polling needed.
        """
        import datetime as _dt

        if not self._is_market_hours():
            from zoneinfo import ZoneInfo
            import datetime as _dt
            now_et = _dt.datetime.now(ZoneInfo("America/New_York"))
            # If market open is within the next 4-hour window, truncate sleep
            # so the bot wakes at 9:25 AM ET instead of missing the open.
            market_open_today = now_et.replace(hour=9, minute=25, second=0, microsecond=0)
            secs_to_open = (market_open_today - now_et).total_seconds()
            if 0 < secs_to_open < 14400 and now_et.weekday() < 5:
                interval = max(int(secs_to_open) - 10, 30)  # wake 10s early
                log.info("Sleeping {}s (off-hours -- waking at market open)...".format(interval))
            else:
                interval = self._current_interval()
                log.info("Sleeping {}s (off-hours)...".format(interval))
            self._write_heartbeat()
            time.sleep(interval)
            return

        POLL_SEC       = 30    # lightweight check cadence (1 API call each)
        MIN_SCAN_GAP   = 120   # minimum seconds between full scans (budget guard)
        MAX_WAIT       = 300   # force full scan after 5 min even with no new ID

        # Track when the last full scan fired (persisted on self)
        if not hasattr(self, '_last_scan_time'):
            self._last_scan_time = _dt.datetime.now() - _dt.timedelta(seconds=MIN_SCAN_GAP)

        waited = 0
        log.info("Adaptive polling every {}s (min scan gap {}s, max wait {}s)...".format(
            POLL_SEC, MIN_SCAN_GAP, MAX_WAIT))

        while waited < MAX_WAIT:
            time.sleep(POLL_SEC)
            waited += POLL_SEC
            self._write_heartbeat()

            # Check if we're within the minimum scan gap
            secs_since_scan = (_dt.datetime.now() - self._last_scan_time).total_seconds()
            if secs_since_scan < MIN_SCAN_GAP:
                remaining = int(MIN_SCAN_GAP - secs_since_scan)
                log.debug("Poll: {:.0f}s since last scan -- "
                          "min gap {}s, waiting {}s more...".format(
                              secs_since_scan, MIN_SCAN_GAP, remaining))
                continue

            # Lightweight UW check (1 API call)
            try:
                new_id = self.uw.get_latest_flow_id()
            except Exception:
                new_id = None

            if new_id and new_id != self._last_flow_id:
                self._last_flow_id = new_id
                self._last_scan_time = _dt.datetime.now()
                log.info("New flow detected (id={}) after {}s -- scanning now".format(
                    new_id[:20] if new_id else "?", waited))
                return

            log.debug("No new flow ({}s elapsed, last_scan {:.0f}s ago)...".format(
                waited, secs_since_scan))

        # Force scan after MAX_WAIT regardless
        self._last_scan_time = _dt.datetime.now()
        log.info("Max wait {}s reached -- scanning anyway".format(MAX_WAIT))

    def _write_heartbeat(self):
        """
        Write a small heartbeat file every cycle so an external watchdog
        can detect if the bot has hung or crashed.
        """
        try:
            from pathlib import Path
            from datetime import datetime
            hb_path = Path(cfg.EXCEL_PATH).parent / "heartbeat.txt"
            hb_path.parent.mkdir(parents=True, exist_ok=True)
            hb_path.write_text(datetime.now().isoformat())
        except Exception as e:
            log.debug("Heartbeat write failed: {}".format(e))

    def run(self):
        self.ibkr.connect()
        # Only send startup message once per day — suppress on watchdog restarts
        from pathlib import Path as _Path
        _startup_gate = _Path(cfg.EXCEL_PATH).parent / "startup_sent.txt"
        _today_str = __import__('datetime').date.today().isoformat()
        try:
            _already = (_startup_gate.exists() and
                        _startup_gate.read_text().strip() == _today_str)
        except Exception:
            _already = False
        if not _already:
            self.telegram.send_startup()
            try:
                _startup_gate.write_text(_today_str)
            except Exception:
                pass
        try:
            while True:
                self._write_heartbeat()
                try:
                    self.scan_cycle()
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    log.error("Cycle error: {}".format(e), exc_info=True)
                    self.telegram.send_error(str(e))

                # End-of-day summary -- once after 4:00 PM ET on weekdays
                try:
                    self._maybe_send_daily_summary()
                except Exception as e:
                    log.error("Daily summary error: {}".format(e))

                # Morning flow report -- once at/after 7:30 AM ET on weekdays
                try:
                    self._maybe_send_morning_report()
                except Exception as e:
                    log.error("Morning report error: {}".format(e))

                # Off-hours digest -- every 4 hours while market closed
                try:
                    self._maybe_send_offhours_digest()
                except Exception as e:
                    log.error("Off-hours digest error: {}".format(e))

                # Budget impact check -- warn once if off-hours usage is
                # eating into the market-hours budget
                try:
                    self._maybe_warn_budget_impact()
                except Exception as e:
                    log.error("Budget warning error: {}".format(e))

                # Active signal P&L digest -- every 2 hours during market hours
                try:
                    self._maybe_send_signal_pnl_digest()
                except Exception as e:
                    log.error("Signal P&L digest error: {}".format(e))

                # ── After-close shutdown ──────────────────────────────────────
                # Once market closes (after 4:05 PM ET), finish the cycle and
                # exit cleanly. main.py will sleep until next open and restart.
                from zoneinfo import ZoneInfo as _ZI
                import datetime as _dt2
                _now_et = _dt2.datetime.now(_ZI("America/New_York"))
                _mins = _now_et.hour * 60 + _now_et.minute
                _is_weekday = _now_et.weekday() < 5
                if _is_weekday and _mins >= (16 * 60 + 5):
                    log.info("Market closed — shutting down scanner until next open.")
                    break
                # ─────────────────────────────────────────────────────────────

                self._adaptive_wait()
        except KeyboardInterrupt:
            log.info("Shutdown requested.")
        finally:
            self.ibkr.disconnect()
            self.telegram.send_shutdown()
            log.info("Bot stopped.")
