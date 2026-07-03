"""
alerts/telegram.py
==================
Telegram alerter for UW Options Bot.
Sends signal alerts, news digests, and status messages.
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config import cfg
from models import ScoredSignal

log = logging.getLogger("UWBot.Telegram")
ET = ZoneInfo("America/New_York")

try:
    import telegram
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    log.warning("python-telegram-bot not installed -- pip install python-telegram-bot")


class TelegramAlerter:

    def __init__(self):
        self._bot = None
        if not TELEGRAM_AVAILABLE:
            return
        if cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID:
            self._bot = telegram.Bot(token=cfg.TELEGRAM_BOT_TOKEN)
            log.info("Telegram ready.")
        else:
            log.warning("Telegram credentials missing -- alerts disabled.")

        # News deduplication -- tracks source_ids already sent this session
        # Resets daily so fresh news always gets through after overnight gap
        self._seen_news_ids: set = set()
        self._seen_news_date: str = datetime.now(ET).strftime("%Y-%m-%d")

        # Context digest stability -- prevents green/red flipping each cycle
        # Tracks last 3 cycle intents per symbol, requires 2/3 agreement
        self._context_history: dict = {}        # sym -> [intent, intent, ...]
        self._last_context_snapshot: dict = {}  # sym -> stable_intent (last sent)

    def _send_async(self, message: str):
        """Send via Telegram async, compatible with Python 3.10-3.12+."""
        import asyncio
        coro = self._bot.send_message(
            chat_id=cfg.TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="HTML",
        )
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Running inside async context -- create task
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(asyncio.run, coro)
                    future.result(timeout=15)
            elif loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(coro)
            else:
                loop.run_until_complete(coro)
        except RuntimeError:
            asyncio.run(coro)

    def send(self, message: str):
        if not self._bot:
            log.info("[TELEGRAM DISABLED] {}".format(message[:100]))
            return
        import time
        try:
            self._send_async(message)
        except Exception as e:
            err = str(e)
            if "Flood control" in err or "429" in err:
                # Extract retry seconds from error message
                import re
                m = re.search(r"Retry in (\d+)", err)
                wait = int(m.group(1)) + 1 if m else 30
                log.warning("Telegram flood control -- waiting {}s".format(wait))
                time.sleep(wait)
                try:
                    self._send_async(message)
                except Exception as e2:
                    log.error("Telegram retry failed: {}".format(e2))
            else:
                log.error("Telegram error: {}".format(e))

    # ── Signal alert ──────────────────────────────────────────────────────────
    @staticmethod
    def _fmt_premium(prem: float) -> str:
        """Format premium as $X.XM or $XXXk."""
        prem = prem or 0
        if prem >= 1_000_000:
            return "${:.1f}M".format(prem / 1_000_000)
        if prem > 0:
            return "${:.0f}k".format(prem / 1_000)
        return "n/a"

    def format_signal(self, sig: ScoredSignal) -> str:
        intent    = sig.intent
        emoji     = "🟢" if intent == "bullish" else "🔴" if intent == "bearish" else "⚪"
        r_emoji   = ("📈" if sig.dealer_regime == "positive_gamma"
                     else "📉" if sig.dealer_regime == "negative_gamma"
                     else "➡️")
        blocked   = " [GEX BLOCKED]" if sig.gex_regime_blocked else ""
        dp        = "  [DP {}]".format(sig.darkpool_sentiment.upper()) if sig.darkpool_sentiment != "neutral" else ""
        ask_tag   = "  [ASK-SIDE]" if sig.ask_side else ""

        sig_type  = sig.signal_type.upper() if hasattr(sig, "signal_type") else "UNIDIRECTIONAL"
        rule_type = sig.rule_type.upper()    if hasattr(sig, "rule_type")   else sig_type
        ml_val    = sig.ml_validation        if hasattr(sig, "ml_validation") else "rule_only"

        type_tag  = " -- VOL PLAY" if sig_type in ("STRADDLE", "STRANGLE") else ""
        val_tag   = {
            "ml_confirmed":  " [ML CONFIRMED +5]",
            "ml_override":   " [ML OVERRIDE -5]",
            "ml_uncertain":  " [ML UNCERTAIN -3]",
            "ml_agree_low":  "",
            "rule_only":     "",
        }.get(ml_val, "")

        type_display = sig_type
        if ml_val == "ml_override" and rule_type != sig_type:
            type_display = "{} (rule: {})".format(sig_type, rule_type)

        feeds = getattr(sig, "feeds_confirming", []) or []
        n_feeds = len(feeds)
        cross_tag = ""
        if n_feeds > 1:
            feed_names = [f.replace("_", " ").upper() for f in feeds[:4]]
            cross_tag = "\n[{} FEEDS: {}]".format(n_feeds, " + ".join(feed_names))

        sep = "-" * 20

        lines = [
            "{} <b>SIGNAL: {}</b>  [{}]{}{}{}{}".format(
                emoji, sig.symbol, type_display,
                type_tag, val_tag, blocked, cross_tag),
            sep,
            "<b>Strike / Exp:</b>  {} {}  {}".format(sig.strike, sig.right, sig.expiry),
            "<b>Structure:</b>     {}{}".format(sig.structure.upper(), ask_tag),
            "<b>Intent:</b>        {}{}".format(intent.upper(), dp),
            "<b>Premium:</b>       {}  |  <b>Prints:</b> {}".format(
                self._fmt_premium(getattr(sig, "total_premium_today", 0) or sig.premium),
                getattr(sig, "n_prints_today", 1)),
            sep,
            "<b>Composite Score:</b>  {:.0f} / 100".format(sig.composite_score),
            "  UW Score:        {} / 100".format(sig.uw_score),
            "  Additive:        {:.0f} / 100".format(sig.additive_score),
            "  Institutional:   {:.0f} / 100".format(sig.institutional_score),
            "<b>IV Percentile:</b>  {:.0f}th".format(sig.iv_percentile),
        ]

        if hasattr(sig, "ml_available") and sig.ml_available:
            lines.append("<b>ML Win Prob:</b>   {:.0f}%  (conf {:.0f}%)".format(
                sig.ml_win_prob, sig.ml_confidence))

        # GEX interpretation -- actionable signal instead of raw numbers
        from scoring.gex_interpreter import interpret, format_for_telegram
        from models import DealerExposure
        exp = DealerExposure(
            symbol       = sig.symbol,
            net_gex      = sig.dealer_gex,
            net_dex      = sig.dealer_dex,
            net_vex      = sig.dealer_vex,
            net_chex     = sig.dealer_chex,
            regime       = sig.dealer_regime,
            gamma_flip   = sig.gamma_flip,
            call_wall    = sig.call_wall,
            put_wall     = sig.put_wall,
            flow_direction = sig.flow_direction,
        )
        underlying_px = sig.live_ask / max(sig.live_delta, 0.01) if sig.live_delta > 0 else 0
        gex_interp    = interpret(exp, underlying_px, sig.intent)
        gex_block     = format_for_telegram(gex_interp)

        lines += [sep, gex_block, sep]

        # Quote + sizing -- show warning when no live quote available
        if getattr(sig, "_no_quote", False) or sig.live_ask <= 0:
            lines += [
                "⚠️ <b>NO LIVE QUOTE</b> -- check current price in your broker app",
                "(Enable Schwab fallback or open TWS for live quotes + sizing)",
                sep,
            ]
        else:
            lines += [
                "<b>Quote:</b>  Bid {:.2f}  /  Ask {:.2f}".format(sig.live_bid, sig.live_ask),
                "<b>Delta:</b>  {:.2f}".format(sig.live_delta),
                sep,
                "<b>Size:</b>    {} contracts".format(sig.suggested_contracts),
                "<b>Max Risk:</b>  ${:,.0f}".format(sig.max_loss_dollar),
                "<b>Target:</b>  {:.2f}  (+65%)".format(sig.target_exit_premium),
                "<b>Stop:</b>    {:.2f}  (-70%)".format(sig.stop_premium),
                sep,
            ]

        lines.append("<i>{}</i>".format(str(sig.timestamp)[:19].replace("T", " ")))

        # Cross-feed summary
        cross_summary = getattr(sig, "cross_feed_summary", "")
        if cross_summary:
            lines.append("")
            lines.append("<b>Multi-feed context:</b>")
            for line in cross_summary.split("\n")[:3]:
                lines.append(line)

        return "\n".join(lines)

    # ── News alert ────────────────────────────────────────────────────────────
    def format_news_alert(self, feed_signal) -> str:
        intent    = feed_signal.intent
        emoji     = "🟢" if intent == "bullish" else "🔴" if intent == "bearish" else "📰"
        symbol    = feed_signal.symbol
        sym_tag   = "" if symbol == "MARKET" else "  [{}]".format(symbol)
        score_tag = "  Score: {}/100".format(feed_signal.score)
        sep       = "-" * 20
        notes     = feed_signal.notes or "No details"
        ts        = feed_signal.timestamp[:19]
        return "\n".join([
            "{} <b>MARKET NEWS</b>{}{}".format(emoji, sym_tag, score_tag),
            sep, notes, sep, "<i>{}</i>".format(ts),
        ])

    def send_news_digest(self, news_signals: list):
        """
        Send digest of NEW market-moving news only.
        Tracks sent headlines by source_id to avoid repeating same news each cycle.
        Resets the seen set daily at midnight.
        """
        if not news_signals:
            return

        # Daily reset -- clear seen IDs at start of new trading day
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if today != self._seen_news_date:
            cleared_count = len(self._seen_news_ids)
            self._seen_news_ids.clear()
            self._seen_news_date = today
            log.info("News dedup: daily reset -- {} items cleared".format(
                cleared_count))

        # Filter to only genuinely new signals not seen before
        # Use source_id if available, else fall back to first 60 chars of notes
        def _news_key(s):
            if s.source_id:
                return s.source_id
            return (s.notes or "")[:60]

        new_signals = [
            s for s in news_signals
            if _news_key(s) not in self._seen_news_ids
        ]

        if not new_signals:
            log.debug("News digest: no new items since last send -- skipping")
            return

        # Mark all current news as seen (new + old, so repeats are suppressed)
        for s in news_signals:
            key = s.source_id if s.source_id else (s.notes or "")[:60]
            self._seen_news_ids.add(key)

        top = sorted(new_signals, key=lambda x: x.score, reverse=True)[:5]
        sep = "-" * 20
        lines = [
            "<b>NEWS</b>  ({} new items)".format(len(new_signals)),
            sep,
        ]
        for sig in top:
            e = "🟢" if sig.intent == "bullish" else "🔴" if sig.intent == "bearish" else "📰"
            sym = "" if sig.symbol == "MARKET" else " [{}]".format(sig.symbol)
            first_line = (sig.notes or "").split("\n")[0][:100]
            lines.append("{}{} {}".format(e, sym, first_line))
        lines += [
            sep,
            "<i>{}</i>".format(datetime.now(ET).strftime("%H:%M ET")),
        ]
        self.send("\n".join(lines))

    # ── Status messages ───────────────────────────────────────────────────────
    def send_context_digest(self, context_signals: list, options_alerts: list):
        """
        Send a consolidated digest of dark pool + lit flow context signals.

        Sentiment stability:
          Only shows a sentiment change (green->red or red->green) if the
          new direction has been consistent for 2+ consecutive cycles.
          Neutral/conflicted signals show ⚪ until conviction builds.
          Only sends the digest if something meaningful changed.
        """
        if not context_signals:
            return

        # Group context signals by symbol
        by_symbol = {}
        for sig in context_signals:
            sym = sig.symbol
            if sym not in by_symbol:
                by_symbol[sym] = []
            by_symbol[sym].append(sig)

        alert_syms = {s.symbol for s in options_alerts}

        # Update sentiment history and determine stable sentiment
        stable = {}
        for sym, sigs in by_symbol.items():
            best   = max(sigs, key=lambda x: x.composite_score)
            intent = best.intent   # current cycle intent

            # Init history for this symbol
            if sym not in self._context_history:
                self._context_history[sym] = []
            hist = self._context_history[sym]
            hist.append(intent)
            if len(hist) > 3:
                hist.pop(0)   # keep last 3 cycles only

            # Determine stable sentiment
            # Requires 2 of last 3 cycles to agree before showing a direction
            bull_count = hist.count("bullish")
            bear_count = hist.count("bearish")
            if bull_count >= 2:
                stable_intent = "bullish"
            elif bear_count >= 2:
                stable_intent = "bearish"
            else:
                stable_intent = "neutral"   # conflicted -- show grey

            feeds = list({s._feed_type if hasattr(s, "_feed_type") else "?" for s in sigs})
            stable[sym] = {
                "intent":    stable_intent,
                "score":     best.composite_score,
                "feeds":     feeds,
                "cross":     sym in alert_syms,
            }

        # Only send if at least one symbol has a confirmed direction
        confirmed = {s: d for s, d in stable.items() if d["intent"] != "neutral"}
        if not confirmed and not any(d["cross"] for d in stable.values()):
            log.debug("Context digest: no confirmed direction signals -- skipping")
            return

        # Check if anything changed from last sent digest
        current_snapshot = {
            s: d["intent"] for s, d in stable.items() if d["intent"] != "neutral"
        }
        if current_snapshot == self._last_context_snapshot:
            log.debug("Context digest: no change from last cycle -- skipping")
            return
        self._last_context_snapshot = current_snapshot

        # Build digest -- options-confirmed names first, then by score
        priority = [(s, d) for s, d in stable.items() if d["cross"]]
        other    = [(s, d) for s, d in stable.items()
                    if not d["cross"] and d["intent"] != "neutral"]
        ordered  = priority + sorted(other, key=lambda x: x[1]["score"], reverse=True)

        if not ordered:
            return

        sep   = "-" * 20
        lines = ["<b>CONTEXT SIGNALS</b>", sep]

        for sym, d in ordered[:8]:
            e        = ("🟢" if d["intent"] == "bullish"
                        else "🔴" if d["intent"] == "bearish"
                        else "⚪")
            cross    = " [OPTIONS]" if d["cross"] else ""
            feed_str = " + ".join(
                f.replace("_", " ").upper() for f in d["feeds"][:3])
            lines.append("{} <b>{}</b>{}  score={:.0f}".format(
                e, sym, cross, d["score"]))
            lines.append("  {}".format(feed_str))

        lines += [sep, "<i>{}</i>".format(
            __import__("datetime").datetime.now(
                __import__("zoneinfo").ZoneInfo("America/New_York")
            ).strftime("%H:%M ET")
        )]
        self.send("\n".join(lines))

    def send_morning_report(self, report: dict):
        """
        FlowPatrol-style morning briefing sent at 7:30 AM ET.

        report keys:
          date, spy_gex, regime, gamma_flip, market_tide,
          conviction_names (list), top_premium (list), watch_today (list)
        """
        sep = "=" * 24
        sub = "-" * 24
        lines = [
            "☀️ <b>MORNING FLOW REPORT</b>",
            "<b>{}</b>".format(report.get("date", "")),
            sep,
        ]

        # Market regime block
        regime = report.get("regime", "unknown")
        regime_emoji = ("📈" if regime == "positive_gamma"
                        else "📉" if regime == "negative_gamma"
                        else "➡️")
        lines += [
            "<b>MARKET SETUP</b>",
            "{} SPY GEX: {}  ({})".format(
                regime_emoji,
                report.get("spy_gex", "n/a"),
                regime.replace("_", " ").upper()),
        ]
        flip = report.get("gamma_flip")
        if flip:
            lines.append("Gamma flip: {}".format(flip))
        tide = report.get("market_tide", "n/a")
        tide_emoji = "🟢" if tide == "bullish" else "🔴" if tide == "bearish" else "⚪"
        lines.append("{} Market tide: {}".format(tide_emoji, tide.upper()))
        lines.append(sub)

        # Multi-feed conviction names from yesterday
        conviction = report.get("conviction_names", [])
        if conviction:
            lines.append("<b>HIGH CONVICTION (multi-feed)</b>")
            for c in conviction[:6]:
                e = ("🟢" if c.get("intent") == "bullish"
                     else "🔴" if c.get("intent") == "bearish"
                     else "⚪")
                lines.append("{} <b>{}</b>  score={:.0f}  feeds={}".format(
                    e, c.get("symbol"), c.get("score", 0), c.get("feeds", 0)))
            lines.append(sub)

        # Top premium flow from yesterday
        top_prem = report.get("top_premium", [])
        if top_prem:
            lines.append("<b>BIGGEST FLOW (yesterday)</b>")
            for t in top_prem[:6]:
                e = ("🟢" if t.get("intent") == "bullish"
                     else "🔴" if t.get("intent") == "bearish"
                     else "⚪")
                prem = t.get("premium", 0) or 0
                prem_str = ("${:.1f}M".format(prem/1e6) if prem >= 1e6
                            else "${:.0f}k".format(prem/1e3))
                strike_part = ""
                if t.get("strike"):
                    strike_part = " {}{} {}".format(
                        t.get("strike"), t.get("right", ""), t.get("expiry", ""))
                lines.append("{} <b>{}</b>{}  {}  ({} prints)".format(
                    e, t.get("symbol"), strike_part, prem_str,
                    t.get("prints", 1)))
            lines.append(sub)

        # Dealer positioning per symbol (FlowPatrol style)
        positions = report.get("dealer_positions", [])
        if positions:
            lines.append("<b>DEALER POSITIONING</b>")
            for p in positions[:8]:
                regime_p = p.get("regime", "unknown")
                pe = ("📈" if regime_p == "positive_gamma"
                      else "📉" if regime_p == "negative_gamma"
                      else "➡️")
                gex_m = p.get("gex_m", 0)
                pct   = p.get("gex_pctile")
                pct_s = "  ({}th %ile)".format(pct) if pct is not None else ""
                extreme = ""
                if pct is not None and pct >= 95:
                    extreme = "  🔥EXTREME"
                elif pct is not None and pct <= 5:
                    extreme = "  ❄️EXTREME LOW"
                lines.append("{} <b>{}</b>  GEX ${:.1f}M{}{}".format(
                    pe, p.get("symbol"), gex_m, pct_s, extreme))
                flip_p = p.get("gamma_flip")
                detail = "   {}".format(regime_p.replace("_", " "))
                if flip_p:
                    detail += "  flip={}".format(flip_p)
                lines.append(detail)
            lines.append(sub)

        # Names to watch today
        watch = report.get("watch_today", [])
        if watch:
            lines.append("<b>WATCH TODAY</b>")
            lines.append(", ".join(watch[:10]))
            lines.append(sub)

        lines.append("<i>Market opens 9:30 AM ET. Bot scanning every {}s.</i>".format(
            report.get("scan_interval", 180)))

        self.send("\n".join(lines))

    def send_daily_summary(self, summary: dict):
        """
        End-of-day wrap-up sent once after market close (4:00 PM ET).
        Shows the day's top signals, alert count, and feed activity.

        summary keys:
          date, total_signals, total_alerts, top_signals (list of dicts),
          feed_counts (dict), market_tide
        """
        sep = "-" * 24
        lines = [
            "📊 <b>DAILY SUMMARY -- {}</b>".format(summary.get("date", "")),
            sep,
            "<b>Signals scored:</b>  {}".format(summary.get("total_signals", 0)),
            "<b>Alerts sent:</b>    {}".format(summary.get("total_alerts", 0)),
            "<b>Market tide:</b>   {}".format(summary.get("market_tide", "n/a")),
            sep,
        ]

        top = summary.get("top_signals", [])
        if top:
            lines.append("<b>TOP SIGNALS TODAY</b>")
            for s in top[:8]:
                e = ("🟢" if s.get("intent") == "bullish"
                     else "🔴" if s.get("intent") == "bearish"
                     else "⚪")
                strike_part = ""
                if s.get("strike"):
                    strike_part = "  {}{}  {}".format(
                        s.get("strike"), s.get("right", ""), s.get("expiry", ""))
                lines.append("{} <b>{}</b>{}  score={:.0f}".format(
                    e, s.get("symbol", "?"), strike_part, s.get("score", 0)))

                # Premium formatted as $X.XM or $XXXk
                prem = s.get("total_premium", 0) or 0
                if prem >= 1_000_000:
                    prem_str = "${:.1f}M".format(prem / 1_000_000)
                elif prem > 0:
                    prem_str = "${:.0f}k".format(prem / 1_000)
                else:
                    prem_str = "n/a"
                lines.append("    Premium: {}  |  Prints: {}".format(
                    prem_str, s.get("n_prints", 0)))
        else:
            lines.append("<i>No qualifying signals today</i>")

        feed_counts = summary.get("feed_counts", {})
        if feed_counts:
            lines.append(sep)
            lines.append("<b>FEED ACTIVITY</b>")
            for feed, count in sorted(feed_counts.items(),
                                       key=lambda x: x[1], reverse=True):
                if count > 0:
                    lines.append("  {} : {}".format(
                        feed.replace("_", " ").title(), count))

        lines += [
            sep,
            "<i>Bot continues scanning. Next session: 9:30 AM ET.</i>",
        ]
        self.send("\n".join(lines))

    def send_startup(self):
        self.send(
            "🤖 <b>UW Options Bot v2.0 started</b>\n"
            "Feeds: Options + Dark Pool + Congress + Insider + Institutional + Lit Flow + News\n"
            "Alert threshold: {}/100\n"
            "Scan interval: {}s".format(cfg.MIN_SCORE_ALERT, cfg.SCAN_INTERVAL_SEC)
        )

    def send_shutdown(self):
        self.send("🛑 <b>UW Options Bot stopped.</b>")

    def send_error(self, err: str):
        self.send("⚠️ <b>Error:</b> {}".format(err[:300]))

    def send_cycle_summary(self, n_logged: int, n_alerted: int):
        if n_alerted > 0:
            self.send("📊 Cycle done -- {} signals logged, <b>{} alerts fired</b>.".format(
                n_logged, n_alerted))
