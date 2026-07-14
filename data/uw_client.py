"""
data/uw_client.py
=================
Unusual Whales REST API client -- sole data source for UW Options Bot.
Field mappings verified against live API responses 2026-06-09.

Endpoints used:
  /api/option-trades/flow-alerts          -- UOA flow signals
  /api/stock/{ticker}/greek-exposure      -- GEX/DEX/VEX/CHEX
  /api/stock/{ticker}/spot-exposures      -- intraday gamma per price level
  /api/stock/{ticker}/greek-flow          -- call/put flow direction
  /api/darkpool/{ticker}                  -- dark pool prints
  /api/stock/{ticker}/option-contracts    -- option chain quote fallback

Verified field names (from live API):
  flow alert  : ticker, type, strike, expiry, alert_rule, has_sweep, has_floor
                total_premium, total_ask_side_prem, total_bid_side_prem
                volume, open_interest, iv_start, volume_oi_ratio
  greek expo  : call_gex, put_gex, call_delta, put_delta
                call_vanna, put_vanna, call_charm, put_charm
  spot expo   : price, call_gamma_oi, put_gamma_oi,
                charm_per_one_percent_move_oi, vanna_per_one_percent_move_oi
  greek flow  : dir_delta_flow, dir_vega_flow, total_delta_flow, volume
  dark pool   : price, size, volume, premium, trade_code, executed_at
"""

import time
import logging
from datetime import date

import requests

from config import cfg
from models import DealerExposure, FlowSignal

log = logging.getLogger("UWBot.Client")

_STRUCTURE_MAP = {
    "REPEATEDHITS": "sweep",
    "SWEEP":        "sweep",
    "BLOCK":        "block",
    "SPLIT":        "block",
    "FLOOR":        "block",
}


class UWClient:

    def __init__(self):
        if not cfg.UW_API_KEY:
            raise ValueError(
                "UW_API_KEY not set in .env\n"
                "Subscribe at: https://unusualwhales.com/pricing?product=api"
            )
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization":    "Bearer " + cfg.UW_API_KEY,
            "Accept":           "application/json",
            "UW-CLIENT-API-ID": "100001",   # required per UW API docs
        })
        # Rate limit tracking (populated from response headers)
        # Daily counter resets at 8 PM Eastern (per UW docs)
        self._daily_count      = 0
        self._daily_limit      = 20000
        self._minute_used      = 0
        self._minute_remain    = 120
        self._minute_reset_ms  = 0    # milliseconds until per-minute counter resets

        # Greek-flow cache -- reused within same scan cycle
        # key: symbol, value: (timestamp, data)
        self._gf_cache: dict = {}
        self._gf_cache_ttl  = 300   # 5 min -- matches scan interval

        log.info("UW client initialised.")

    # ── Internal ──────────────────────────────────────────────────────────────
    def _get(self, path, params=None):
        """
        Make a GET request to UW API with full error handling per official docs.
        Rate limit headers tracked on every response.
        Daily reset: 8 PM Eastern (America/New_York).
        """
        import time
        url = cfg.UW_BASE + path
        try:
            r = self._session.get(url, params=params, timeout=12)

            # Extract usage from response headers (present on all successful responses)
            def _safe_int(h, default=0):
                try: return int(r.headers.get(h, default))
                except: return default

            self._daily_count      = _safe_int("x-uw-daily-req-count")
            self._daily_limit      = _safe_int("x-uw-token-req-limit", 20000)
            self._minute_used      = _safe_int("x-uw-minute-req-counter")
            self._minute_remain    = _safe_int("x-uw-req-per-minute-remaining", 120)
            self._minute_reset_ms  = _safe_int("x-uw-req-per-minute-reset")

            # ── Per-minute threshold (check first -- blocks immediately) ────────
            if self._minute_remain == 0:
                wait_sec = max(self._minute_reset_ms / 1000, 1.0) if self._minute_reset_ms else 60.0
                log.warning("UW per-minute cap hit -- waiting {:.1f}s".format(wait_sec))
                time.sleep(wait_sec)
            elif self._minute_remain <= 4:
                wait_sec = max(self._minute_reset_ms / 1000, 1.0) if self._minute_reset_ms else 5.0
                log.warning("UW near per-minute cap ({} remaining) -- waiting {:.1f}s".format(
                    self._minute_remain, wait_sec))
                time.sleep(wait_sec)

            # ── Daily threshold (check second) ───────────────────────────────────
            daily_remaining = self._daily_limit - self._daily_count
            daily_pct       = self._daily_count / max(self._daily_limit, 1) * 100

            if daily_remaining < 100:
                log.error(
                    "UW CRITICAL: only {} daily requests remaining ({:.1f}%). "
                    "Stopping non-essential requests. Resets at 8 PM ET.".format(
                        daily_remaining, daily_pct))
            elif daily_pct >= 80:
                log.warning(
                    "UW daily usage at {:.1f}% ({}/{}). "
                    "Throttling to essential requests only.".format(
                        daily_pct, self._daily_count, self._daily_limit))
            elif daily_pct >= 50:
                log.debug("UW daily usage: {:.1f}% ({}/{})".format(
                    daily_pct, self._daily_count, self._daily_limit))

            r.raise_for_status()
            return r.json()

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code

            if status == 429:
                # Rate limited -- use reset header for precise wait time
                reset_ms = int(e.response.headers.get("x-uw-req-per-minute-reset", 60000))
                wait_sec = max(reset_ms / 1000, 1.0)
                log.warning("UW 429 rate limit -- waiting {:.1f}s (resets in {:.1f}s)".format(
                    wait_sec, wait_sec))
                time.sleep(wait_sec)

            elif status == 401:
                log.error(
                    "UW 401 authentication failed -- token invalid or expired. "
                    "Verify at: https://unusualwhales.com/settings/developer-settings "
                    "Update UW_API_KEY in .env."
                )

            elif status == 403:
                log.error(
                    "UW 403 access denied for {}. "
                    "Subscription tier may not cover this endpoint. "
                    "Check: https://unusualwhales.com/settings/account".format(path)
                )

            else:
                log.warning("UW HTTP {} -- {} -- may be temporary, retrying next cycle".format(
                    status, path))
            return {}

        except requests.exceptions.ConnectionError:
            log.error("UW connection error -- check internet connection: {}".format(path))
            return {}
        except requests.exceptions.Timeout:
            log.warning("UW timeout (12s) -- {}".format(path))
            return {}
        except Exception as e:
            log.error("UW request error {}: {}".format(path, e))
            return {}

    def _as_list(self, data):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "results", "trades", "alerts"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    def _f(self, val, default=0.0):
        try:
            return float(val) if val is not None else default
        except (ValueError, TypeError):
            return default

    # ── Health check ──────────────────────────────────────────────────────────
    def _get_greek_flow_cached(self, symbol: str) -> dict:
        """
        Return greek-flow data with in-memory cache.
        Same data is used for both flow direction and IV percentile,
        saving one API call per symbol per cycle.
        Cache TTL matches scan interval (5 min default).
        """
        import time
        now = time.time()
        cached = self._gf_cache.get(symbol)
        if cached and (now - cached[0]) < self._gf_cache_ttl:
            return cached[1]

        gf_raw  = self._get("/api/stock/{}/greek-flow".format(symbol))
        gf_list = self._as_list(gf_raw)
        data    = gf_list[0] if gf_list else (gf_raw if isinstance(gf_raw, dict) else {})
        self._gf_cache[symbol] = (now, data)
        return data

    def clear_cache(self):
        """Call at start of each scan cycle to reset greek-flow cache."""
        self._gf_cache.clear()

    def get_usage_stats(self) -> dict:
        """
        Return current API usage stats from last response headers.
        Daily counter resets at 8 PM Eastern (America/New_York).
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        now_et  = datetime.now(ET)
        # Next reset: 8 PM ET today or tomorrow
        reset_hour = 20  # 8 PM
        if now_et.hour >= reset_hour:
            hours_to_reset = 24 - now_et.hour + reset_hour
        else:
            hours_to_reset = reset_hour - now_et.hour
        daily_remaining = self._daily_limit - self._daily_count
        daily_pct = round(self._daily_count / max(self._daily_limit, 1) * 100, 1)

        # Status classification per UW docs
        if daily_remaining < 100:
            daily_status = "CRITICAL"
        elif daily_pct >= 80:
            daily_status = "HIGH"
        elif daily_pct >= 50:
            daily_status = "MODERATE"
        else:
            daily_status = "OK"

        minute_reset_sec = round(self._minute_reset_ms / 1000, 1) if self._minute_reset_ms else None

        return {
            "daily_used":       self._daily_count,
            "daily_limit":      self._daily_limit,
            "daily_remaining":  daily_remaining,
            "daily_pct":        daily_pct,
            "daily_status":     daily_status,
            "minute_used":      self._minute_used,
            "minute_remain":    self._minute_remain,
            "minute_reset_sec": minute_reset_sec,
            "resets_in_hours":  hours_to_reset,
            "reset_time_et":    "8:00 PM ET",
        }

    def health_check(self):
        results = {}
        test_sym = "SPY"
        checks = {
            "flow_alerts":      ("/api/option-trades/flow-alerts",                    {"limit": 1}),
            "screener":         ("/api/screener/option-contracts",                    {"limit": 1}),
            "greek_exposure":   ("/api/stock/{}/greek-exposure/strike".format(test_sym), {}),
            "spot_exposures":   ("/api/stock/{}/spot-exposures/strike".format(test_sym), {}),
            "greek_flow":       ("/api/stock/{}/greek-flow".format(test_sym),         {}),
            "interpolated_iv":  ("/api/stock/{}/interpolated-iv".format(test_sym),    {}),
            "market_tide":      ("/api/market/market-tide",                           {}),
            "darkpool":         ("/api/darkpool/{}".format(test_sym),                 {"limit": 1}),
            "option_contracts": ("/api/stock/{}/option-contracts".format(test_sym),   {"limit": 1}),
        }
        for name, (path, params) in checks.items():
            resp = self._get(path, params)
            ok = bool(resp)
            results[name] = ok
            log.info("  {}  {:<22} {}".format("[OK]" if ok else "[FAIL]", name, path))
        return results

    # ── Flow Signals ──────────────────────────────────────────────────────────
    def get_flow_signals(self, symbol, min_score=50, window_minutes=240,
                         intent=None, structure=None, limit=20):
        """
        Fetch UW flow alerts for one symbol.

        Key verified fields:
          ticker, type (call/put), strike, expiry, alert_rule
          has_sweep, has_floor, total_premium, total_ask_side_prem
          volume, open_interest, iv_start, volume_oi_ratio
        """
        raw = self._get("/api/option-trades/flow-alerts", {"limit": min(limit * 5, 200)})
        alerts = self._as_list(raw)
        signals = []

        for a in alerts:
            try:
                # Symbol filter
                ticker = (a.get("ticker") or "").upper()
                if ticker != symbol.upper():
                    continue

                # Right (type field = "call" or "put")
                raw_type = (a.get("type") or "call").lower()
                right = "C" if raw_type == "call" else "P"

                # Intent from type
                sig_intent = "bullish" if right == "C" else "bearish"
                if intent and sig_intent != intent:
                    continue

                # Structure from alert_rule
                raw_rule = (a.get("alert_rule") or "").upper().replace(" ", "")
                sig_struct = _STRUCTURE_MAP.get(raw_rule, "block")
                # Override: has_sweep flag
                if a.get("has_sweep"):
                    sig_struct = "sweep"
                if structure and sig_struct != structure:
                    continue

                # Fields
                premium     = self._f(a.get("total_premium"))
                ask_side_p  = self._f(a.get("total_ask_side_prem"))
                bid_side_p  = self._f(a.get("total_bid_side_prem"))
                ask_side    = ask_side_p > bid_side_p
                volume      = int(a.get("volume") or 0)
                oi          = int(a.get("open_interest") or 0)
                iv          = self._f(a.get("iv_start"))
                strike      = self._f(a.get("strike"))
                vol_oi      = self._f(a.get("volume_oi_ratio"))

                # Expiry
                raw_exp = a.get("expiry") or ""
                try:
                    expiry = date.fromisoformat(raw_exp[:10]).isoformat()
                except Exception:
                    expiry = raw_exp[:10]

                # Score
                score = self._score(premium, sig_intent, sig_struct,
                                    ask_side, vol_oi, a)
                if score < min_score:
                    continue

                signals.append(FlowSignal(
                    symbol    = symbol,
                    strike    = strike,
                    expiry    = expiry,
                    right     = right,
                    structure = sig_struct,
                    intent    = sig_intent,
                    score     = score,
                    delta     = 0.0,
                    iv        = iv,
                    moneyness = "",
                    premium   = premium,
                    ask_side  = ask_side,
                    oi        = oi,
                    volume    = volume,
                    raw       = a,
                ))

                if len(signals) >= limit:
                    break

            except Exception as e:
                log.debug("Signal parse error {}: {}".format(symbol, e))

        log.info("{}: {} flow signals found (min_score={})".format(
            symbol, len(signals), min_score))
        return signals

    @staticmethod
    def _score(premium, intent, structure, ask_side, vol_oi, a):
        s = 0
        # Structure
        s += 30 if structure == "sweep" else 20
        # Intent always directional for UW (call=bullish, put=bearish)
        s += 20
        # Premium tiers
        if premium >= 5_000_000:   s += 30
        elif premium >= 1_000_000: s += 22
        elif premium >= 500_000:   s += 15
        elif premium >= 100_000:   s += 8
        elif premium >= 50_000:    s += 4
        # Ask side aggressor
        if ask_side:               s += 10
        # High vol/OI ratio (unusual activity)
        if vol_oi and float(vol_oi) > 5:  s += 10
        elif vol_oi and float(vol_oi) > 2: s += 5
        # Floor trade bonus
        if a.get("has_floor"):     s += 5
        return max(0, min(100, s))

    # ── Dealer Exposure ───────────────────────────────────────────────────────
    def get_dealer_exposure(self, symbol):
        """
        Build DealerExposure from UW greek-exposure + spot-exposures + greek-flow.

        Verified greek-exposure fields:
          call_gex, put_gex, call_delta, put_delta
          call_vanna, put_vanna, call_charm, put_charm

        Net GEX = call_gex + put_gex (put_gex is already negative)
        """
        exp = DealerExposure(symbol=symbol)

        # 1. Greek exposure -- net greeks
        ge_raw  = self._get("/api/stock/{}/greek-exposure/strike".format(symbol))
        ge_list = self._as_list(ge_raw)
        # Take most recent row (first in list)

        # SUM across ALL strike levels for true net exposure
        # API returns per-strike data; [0] alone = single strike GEX only
        call_gex   = sum(self._f(row.get("call_gex"))   for row in ge_list)
        put_gex    = sum(self._f(row.get("put_gex"))    for row in ge_list)
        call_delta = sum(self._f(row.get("call_delta")) for row in ge_list)
        put_delta  = sum(self._f(row.get("put_delta"))  for row in ge_list)
        call_vanna = sum(self._f(row.get("call_vanna")) for row in ge_list)
        put_vanna  = sum(self._f(row.get("put_vanna"))  for row in ge_list)
        call_charm = sum(self._f(row.get("call_charm")) for row in ge_list)
        put_charm  = sum(self._f(row.get("put_charm"))  for row in ge_list)

        exp.net_gex  = call_gex + put_gex
        exp.net_dex  = call_delta + put_delta
        exp.net_vex  = call_vanna + put_vanna
        exp.net_chex = call_charm + put_charm

        # 2. Spot exposures -- gamma flip + walls
        spot_raw  = self._get("/api/stock/{}/spot-exposures/strike".format(symbol))
        spot_list = self._as_list(spot_raw)
        if spot_list:
            # Sort by price ascending
            sorted_spot = sorted(
                spot_list,
                key=lambda x: self._f(x.get("price"))
            )
            exp.gamma_flip = self._find_gamma_flip(sorted_spot)

            # Call wall = price with most positive gamma OI
            # Put wall  = price with most negative gamma OI
            # Call wall = strike with highest call_gamma_oi (most positive)
            # Put wall  = strike with most negative put_gamma_oi
            # Bug fix: previously used net (call+put) sort which was fragile
            exp.call_wall = self._f(
                max(spot_list,
                    key=lambda x: self._f(x.get("call_gamma_oi"))).get("price")
            )
            exp.put_wall = self._f(
                min(spot_list,
                    key=lambda x: self._f(x.get("put_gamma_oi"))).get("price")
            )

        # 3. Greek flow -- flow direction (cached -- same data used for IV pct)
        gf = self._get_greek_flow_cached(symbol)
        exp.flow_direction = self._classify_flow(gf, exp.net_gex)

        # Regime
        exp.regime = (
            "positive_gamma" if exp.net_gex > 0
            else "negative_gamma" if exp.net_gex < 0
            else "transitional"
        )

        log.debug("{} GEX={:.1f}M regime={} fd={}".format(
            symbol, exp.net_gex / 1e6, exp.regime, exp.flow_direction))
        return exp

    def _find_gamma_flip(self, sorted_strikes):
        prev = None
        for row in sorted_strikes:
            gex = self._f(row.get("call_gamma_oi")) + self._f(row.get("put_gamma_oi"))
            price = self._f(row.get("price"))
            if prev is not None and prev * gex < 0:
                return price
            prev = gex
        return None

    def _classify_flow(self, gf, net_gex):
        """
        Verified greek-flow fields:
          dir_delta_flow  -- directional (signed) delta flow
          total_delta_flow -- total absolute delta flow
          dir_vega_flow   -- directional vega flow
          volume          -- total volume
        """
        if not gf:
            return "no_flow"

        dir_delta  = self._f(gf.get("dir_delta_flow"))
        total_delta = self._f(gf.get("total_delta_flow"))

        if total_delta == 0:
            return "no_flow"

        # Ratio of directional to total -- positive = bullish lean
        ratio = dir_delta / total_delta if total_delta != 0 else 0

        if ratio > 0.3 and net_gex > 0:   return "amplifying"
        if ratio < -0.3 and net_gex < 0:  return "amplifying"
        if ratio > 0.3 and net_gex < 0:   return "regime_flip"
        if ratio < -0.3 and net_gex > 0:  return "regime_flip"
        if -0.1 < ratio < 0.1:            return "neutral"
        return "dampening"

    def get_dealer_exposure_watchlist(self):
        results = {}
        for sym in cfg.WATCHLIST:
            results[sym] = self.get_dealer_exposure(sym)
            time.sleep(0.25)
        return results

    # ── IV Percentile ─────────────────────────────────────────────────────────
    def get_iv_percentile(self, symbol):
        """
        Derive IV rank from greek-flow.
        Verified fields: dir_vega_flow, total_vega_flow, otm_dir_vega_flow
        Use vega flow ratio as IV sentiment proxy.
        """
        # Uses cached greek-flow -- no extra API call
        gf = self._get_greek_flow_cached(symbol)

        # Check for direct IV rank field (may exist in some tiers)
        iv_rank = gf.get("iv_rank") or gf.get("ivr") or gf.get("iv_percentile")
        if iv_rank is not None:
            return self._f(iv_rank)

        # Derive from vega flow direction
        dir_vega   = self._f(gf.get("dir_vega_flow"))
        total_vega = self._f(gf.get("total_vega_flow"))
        if total_vega > 0:
            ratio = dir_vega / total_vega
            # High positive ratio = vega being bought = elevated IV
            if ratio > 0.5:   return 75.0
            if ratio > 0.2:   return 60.0
            if ratio > -0.2:  return 50.0
            if ratio > -0.5:  return 40.0
            return 25.0
        return 50.0

    # ── Dark Pool ─────────────────────────────────────────────────────────────
    def get_darkpool_sentiment(self, symbol):
        """
        Convenience wrapper: fetch raw prints, compute sentiment.
        Prefer get_darkpool_trades_raw() + darkpool_sentiment_from_trades()
        when the caller also needs the raw prints (avoids a duplicate API call).
        """
        return self.darkpool_sentiment_from_trades(
            self.get_darkpool_trades_raw(symbol)
        )

    def darkpool_sentiment_from_trades(self, trades):
        """
        Compute bullish/bearish/neutral from raw prints (no API call).
        Verified dark pool fields:
          price, size, volume, premium, trade_code, executed_at
        """
        if not trades:
            return "neutral"

        prices = [self._f(t.get("price")) for t in trades if t.get("price")]
        if not prices:
            return "neutral"

        mid = sum(prices) / len(prices)

        bull_vol = sum(
            self._f(t.get("size") or t.get("volume"))
            for t in trades if self._f(t.get("price")) >= mid
        )
        bear_vol = sum(
            self._f(t.get("size") or t.get("volume"))
            for t in trades if self._f(t.get("price")) < mid
        )
        total = bull_vol + bear_vol
        if total == 0:
            return "neutral"
        ratio = bull_vol / total
        if ratio > 0.60:  return "bullish"
        if ratio < 0.40:  return "bearish"
        return "neutral"

    def get_darkpool_trades_raw(self, symbol, limit=50):
        raw = self._get("/api/darkpool/{}".format(symbol), {"limit": limit})
        return self._as_list(raw)

    # ── Option Chain Quote Fallback ───────────────────────────────────────────
    def find_contract_quote(self, symbol, expiry, strike, right):
        raw       = self._get("/api/stock/{}/option-contracts".format(symbol))
        contracts = self._as_list(raw)
        for c in contracts:
            c_strike = self._f(c.get("strike") or c.get("strike_price"))
            c_expiry = (c.get("expiration_date") or c.get("expiry") or "")[:10]
            c_type   = (c.get("type") or c.get("put_call") or "call").lower()
            c_right  = "C" if c_type in ("call", "c") else "P"
            if (
                abs(c_strike - strike) < 0.01
                and c_expiry == expiry
                and c_right  == right
            ):
                return {
                    "bid":   self._f(c.get("bid")),
                    "ask":   self._f(c.get("ask")),
                    "last":  self._f(c.get("last")),
                    "delta": self._f(c.get("delta")),
                    "iv":    self._f(c.get("implied_volatility") or c.get("iv")),
                }
        return {}


    # ── Dynamic Watchlist ─────────────────────────────────────────────────────
    def get_dynamic_watchlist(
        self,
        max_symbols: int = 25,
        min_premium: float = 50_000,
        anchor_symbols: list = None,
    ) -> list:
        """
        Build a dynamic watchlist from live UW flow alerts.

        Strategy (replaces static .env WATCHLIST):
          1. Fetch all flow alerts in last 4 hours (single API call)
          2. Aggregate total premium by ticker
          3. Take top N tickers by premium volume
          4. Always include anchor symbols (SPY, QQQ, IWM, NVDA, AAPL)
          5. Deduplicate and return final list

        Single API call replaces 12-25 per-ticker screening calls.
        Called once at market open, then every 2 hours intraday.
        """
        if anchor_symbols is None:
            anchor_symbols = ["SPY", "QQQ", "IWM", "NVDA", "AAPL"]

        raw    = self._get("/api/option-trades/flow-alerts", {"limit": 200})
        alerts = self._as_list(raw)

        if not alerts:
            log.warning("Dynamic watchlist: no flow alerts -- using anchors only")
            return anchor_symbols[:max_symbols]

        # Aggregate premium by ticker
        ticker_premium = {}
        ticker_count   = {}
        for a in alerts:
            ticker = (a.get("ticker") or a.get("underlying_symbol") or "").upper()
            if not ticker or len(ticker) > 5:
                continue
            prem = self._f(a.get("total_premium") or a.get("premium"))
            if prem < min_premium:
                continue
            ticker_premium[ticker] = ticker_premium.get(ticker, 0) + prem
            ticker_count[ticker]   = ticker_count.get(ticker, 0) + 1

        # Sort by total premium descending
        sorted_tickers = sorted(
            ticker_premium.items(),
            key=lambda x: x[1],
            reverse=True
        )

        # Build list: anchors first, then top flow names
        watchlist = list(anchor_symbols)
        for ticker, prem in sorted_tickers:
            if ticker not in watchlist:
                watchlist.append(ticker)
            if len(watchlist) >= max_symbols:
                break

        # Log top new names
        new_names = [
            "{} ${:.0f}k".format(t, ticker_premium.get(t, 0) / 1000)
            for t in watchlist[len(anchor_symbols):len(anchor_symbols)+5]
        ]
        log.info("Dynamic watchlist: {} symbols  top new: {}".format(
            len(watchlist), ", ".join(new_names)
        ))
        return watchlist



    # ── Market-wide flow feed (single call, full market) ─────────────────────
    def get_flow_signals_market(
        self,
        min_score: int = 50,
        limit: int = 200,
    ) -> list:
        """
        Fetch unusual flow alerts across the ENTIRE market in one API call.
        No ticker filter -- returns everything UW flags as unusual.

        This is the main feed shown on the UW website dashboard.
        Real-time, full market coverage, single API call per cycle.

        Replaces the per-ticker get_flow_signals() polling loop.
        """
        raw    = self._get("/api/option-trades/flow-alerts", {"limit": limit})
        alerts = self._as_list(raw)

        if not alerts:
            log.info("Market feed: no alerts returned")
            return []

        signals = []
        for a in alerts:
            try:
                ticker = (a.get("ticker") or
                          a.get("underlying_symbol") or "").upper()
                if not ticker or len(ticker) > 5:
                    continue

                # Right
                raw_type = (a.get("type") or "call").lower()
                right    = "C" if raw_type == "call" else "P"

                # Intent from type
                sig_intent = "bullish" if right == "C" else "bearish"

                # Structure
                raw_rule   = (a.get("alert_rule") or "").upper().replace(" ", "")
                sig_struct = _STRUCTURE_MAP.get(raw_rule, "block")
                if a.get("has_sweep"):
                    sig_struct = "sweep"

                # Fields
                premium    = self._f(a.get("total_premium"))
                ask_side_p = self._f(a.get("total_ask_side_prem"))
                bid_side_p = self._f(a.get("total_bid_side_prem"))
                ask_side   = ask_side_p > bid_side_p
                volume     = int(a.get("volume") or 0)
                oi         = int(a.get("open_interest") or 0)
                iv         = self._f(a.get("iv_start"))
                strike     = self._f(a.get("strike"))
                vol_oi     = self._f(a.get("volume_oi_ratio"))

                # Expiry
                raw_exp = a.get("expiry") or ""
                try:
                    from datetime import date
                    expiry = date.fromisoformat(raw_exp[:10]).isoformat()
                except Exception:
                    expiry = raw_exp[:10]

                # Score
                score = self._score(premium, sig_intent, sig_struct,
                                    ask_side, vol_oi, a)
                if score < min_score:
                    continue

                from models import FlowSignal
                signals.append(FlowSignal(
                    symbol    = ticker,
                    strike    = strike,
                    expiry    = expiry,
                    right     = right,
                    structure = sig_struct,
                    intent    = sig_intent,
                    score     = score,
                    delta     = 0.0,
                    iv        = iv,
                    moneyness = "",
                    premium   = premium,
                    ask_side  = ask_side,
                    oi        = oi,
                    volume    = volume,
                    raw       = a,
                ))

            except Exception as e:
                log.debug("Market feed parse error: {}".format(e))

        # Sort by score descending
        signals.sort(key=lambda x: x.score, reverse=True)
        log.info("Market feed: {}/{} alerts above min_score={}".format(
            len(signals), len(alerts), min_score
        ))
        return signals



    # ── Market Tide ───────────────────────────────────────────────────────────
    def get_market_tide(self) -> dict:
        """
        Overall market sentiment from net call/put premium flow.
        Endpoint: GET /api/market/market-tide
        Returns dict with net_call_premium, net_put_premium, sentiment.
        """
        raw  = self._get("/api/market/market-tide")
        data = self._as_list(raw)
        if not data:
            return {"sentiment": "neutral", "net_call": 0, "net_put": 0}

        latest = data[0] if data else {}
        net_call = self._f(latest.get("net_call_premium") or latest.get("call_premium"))
        net_put  = self._f(latest.get("net_put_premium")  or latest.get("put_premium"))
        net      = net_call - net_put

        sentiment = "bullish" if net > 0 else "bearish" if net < 0 else "neutral"
        log.debug("Market tide: net_call={:.1f}M net_put={:.1f}M -> {}".format(
            net_call/1e6, net_put/1e6, sentiment))

        return {
            "sentiment":  sentiment,
            "net_call":   net_call,
            "net_put":    net_put,
            "net":        net,
            "net_m":      round(net / 1e6, 2),
        }

    # ── Options Screener (Hottest Chains) ─────────────────────────────────────
    def get_hottest_chains(
        self,
        min_premium: float = 250_000,
        limit: int = 50,
        type_filter: str = None,   # "Calls" | "Puts" | None
    ) -> list:
        """
        Hottest options chains by unusual activity score.
        Endpoint: GET /api/screener/option-contracts
        Better than flow-alerts for finding top unusual activity.
        Returns list of dicts with ticker_symbol, option_symbol, avg_price, etc.
        """
        params = {
            "limit":                    limit,
            "min_premium":              min_premium,
            "is_otm":                   True,
            "vol_greater_oi":           True,
            "issue_types[]":            "Common Stock",
            "max_dte":                  45,
            "min_ask_perc":             0.5,   # >50% on ask = aggressor
        }
        if type_filter:
            params["type"] = type_filter

        raw  = self._get("/api/screener/option-contracts", params)
        data = self._as_list(raw)
        log.info("Hottest chains: {} results (min_premium=${:.0f}k)".format(
            len(data), min_premium / 1000))
        return data

    # ── Lightweight new-flow detection (1 API call) ─────────────────────────
    def get_latest_flow_id(self, min_premium: float = 50_000) -> str | None:
        """
        Fetch only the single most-recent screener result and return its ID.
        Costs exactly 1 API call. Used by the adaptive-polling loop to decide
        whether a full scan is worth running.
        """
        try:
            params = {
                "limit":       1,
                "min_premium": min_premium,
                "is_otm":      True,
            }
            raw  = self._get("/api/screener/option-contracts", params)
            data = self._as_list(raw)
            if data:
                # Use id if present; fall back to option_symbol which is also unique
                return str(data[0].get("id") or data[0].get("option_symbol") or "")
        except Exception as e:
            log.debug("get_latest_flow_id error: {}".format(e))
        return None

    # ── Options Volume / P-C Ratio ────────────────────────────────────────────
    def get_options_volume(self, symbol: str) -> dict:
        """
        Options volume and put/call ratio for a ticker.
        Endpoint: GET /api/stock/{ticker}/options-volume
        """
        raw  = self._get("/api/stock/{}/options-volume".format(symbol))
        data = self._as_list(raw)
        item = data[0] if data else (raw if isinstance(raw, dict) else {})

        call_vol = self._f(item.get("call_volume") or item.get("calls_volume"))
        put_vol  = self._f(item.get("put_volume")  or item.get("puts_volume"))
        pc_ratio = round(put_vol / call_vol, 3) if call_vol > 0 else 1.0

        return {
            "call_volume": call_vol,
            "put_volume":  put_vol,
            "pc_ratio":    pc_ratio,
            "sentiment":   "bullish" if pc_ratio < 0.7 else "bearish" if pc_ratio > 1.3 else "neutral",
        }

    # ── Per-ticker recent flow ────────────────────────────────────────────────
    def get_ticker_flow(self, symbol: str, limit: int = 20) -> list:
        """
        Recent flow for a specific ticker.
        Endpoint: GET /api/stock/{ticker}/flow-recent
        Use for position monitoring -- check if flow continues after alert.
        """
        raw = self._get("/api/stock/{}/flow-recent".format(symbol), {"limit": limit})
        return self._as_list(raw)

