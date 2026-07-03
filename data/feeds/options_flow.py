"""
data/feeds/options_flow.py
==========================
UW Options Flow feed -- unusual sweeps and blocks across entire market.
Endpoint: GET /api/option-trades/flow-alerts
Real-time, no ticker filter, full market coverage.
"""

import logging
from models_feed import FeedSignal

log = logging.getLogger("UWBot.Feed.OptionsFlow")

_STRUCTURE_MAP = {
    "REPEATEDHITS": "sweep",
    "SWEEP":        "sweep",
    "BLOCK":        "block",
    "SPLIT":        "block",
    "FLOOR":        "block",
}


def fetch(client, limit: int = 200, min_premium: float = 50_000) -> list[FeedSignal]:
    """
    Fetch unusual options flow across entire market.
    Primary:  /api/screener/option-contracts (hottest chains, pre-filtered)
    Fallback: /api/option-trades/flow-alerts (raw flow)
    """
    # Try screener first (higher quality, pre-filtered for unusual activity)
    screener_signals = _fetch_screener(client, min_premium)
    if screener_signals:
        return screener_signals

    # Fallback to raw flow alerts
    raw    = client._get("/api/option-trades/flow-alerts", {"limit": limit})
    alerts = client._as_list(raw)

    signals = []
    for a in alerts:
        try:
            ticker = (a.get("ticker") or a.get("underlying_symbol") or "").upper()
            if not ticker or len(ticker) > 5:
                continue

            raw_type   = (a.get("type") or "call").lower()
            right      = "C" if raw_type == "call" else "P"
            intent     = "bullish" if right == "C" else "bearish"

            raw_rule   = (a.get("alert_rule") or "").upper().replace(" ", "")
            structure  = _STRUCTURE_MAP.get(raw_rule, "block")
            if a.get("has_sweep"):
                structure = "sweep"

            premium    = client._f(a.get("total_premium"))
            if premium < min_premium:
                continue

            ask_p      = client._f(a.get("total_ask_side_prem"))
            bid_p      = client._f(a.get("total_bid_side_prem"))
            ask_side   = ask_p > bid_p
            volume     = int(a.get("volume") or 0)
            oi         = int(a.get("open_interest") or 0)
            iv         = client._f(a.get("iv_start"))
            strike     = client._f(a.get("strike"))
            vol_oi     = client._f(a.get("volume_oi_ratio"))

            raw_exp = a.get("expiry") or ""
            try:
                from datetime import date
                expiry = date.fromisoformat(raw_exp[:10]).isoformat()
            except Exception:
                expiry = raw_exp[:10]

            # Score
            score = _score(premium, structure, ask_side, vol_oi, a)

            signals.append(FeedSignal(
                feed_type = "options_flow",
                symbol    = ticker,
                intent    = intent,
                premium   = premium,
                size      = volume,
                score     = score,
                strike    = strike,
                expiry    = expiry,
                right     = right,
                structure = structure,
                ask_side  = ask_side,
                volume    = volume,
                oi        = oi,
                iv        = iv,
                source_id = a.get("id", ""),
                notes     = "{} {} ${:.0f}k".format(
                    structure.upper(), "ASK" if ask_side else "BID",
                    premium / 1000
                ),
                raw       = a,
            ))

        except Exception as e:
            log.debug("Options flow parse error: {}".format(e))

    signals.sort(key=lambda x: x.premium, reverse=True)
    log.info("Options flow: {} signals (>{:.0f}k premium)".format(
        len(signals), min_premium / 1000))
    return signals


def _score(premium, structure, ask_side, vol_oi, a) -> int:
    s = 0
    s += 30 if structure == "sweep" else 20
    s += 20   # always directional (call=bull, put=bear)
    if premium >= 5_000_000:   s += 30
    elif premium >= 1_000_000: s += 22
    elif premium >= 500_000:   s += 15
    elif premium >= 100_000:   s += 8
    elif premium >= 50_000:    s += 4
    if ask_side:               s += 10
    if vol_oi and float(vol_oi) > 5:  s += 10
    elif vol_oi and float(vol_oi) > 2: s += 5
    if a.get("has_floor"):     s += 5
    return max(0, min(100, s))


def _parse_option_symbol(option_symbol: str) -> tuple:
    """
    Parse OCC option symbol into (ticker, expiry, right, strike).
    Format: TICKER + YYMMDD + C/P + 8-digit strike (strike * 1000)
    Example: NVDA250620P00120000 -> NVDA, 2025-06-20, P, 120.0
    Returns (ticker, expiry_iso, right, strike) or ("", "", "", 0)
    """
    import re
    if not option_symbol:
        return "", "", "", 0.0
    try:
        # OCC format: underlying (1-6 chars) + YYMMDD + C/P + 8 digit strike
        m = re.match(r"([A-Z]+)(\d{6})([CP])(\d{8})", option_symbol.upper())
        if not m:
            return "", "", "", 0.0
        ticker  = m.group(1)
        date_s  = m.group(2)   # YYMMDD
        right   = m.group(3)   # C or P
        strike  = float(m.group(4)) / 1000.0
        from datetime import date
        expiry  = date(2000 + int(date_s[:2]), int(date_s[2:4]), int(date_s[4:6])).isoformat()
        return ticker, expiry, right, strike
    except Exception:
        return "", "", "", 0.0


def _fetch_screener(client, min_premium: float) -> list[FeedSignal]:
    """
    Use /api/screener/option-contracts for highest quality unusual activity.
    Pre-filtered by UW for vol > OI, ask-side, and unusual premium.

    Strike/expiry parsed from option_symbol (OCC format) since screener
    may not return them as separate top-level fields.
    """
    data = client.get_hottest_chains(min_premium=min_premium, limit=100)
    if not data:
        return []

    signals = []
    for c in data:
        try:
            ticker = (c.get("ticker_symbol") or c.get("ticker") or "").upper()
            if not ticker or len(ticker) > 5:
                continue

            # Parse strike/expiry/right from option_symbol (OCC format)
            option_sym = c.get("option_symbol") or c.get("symbol") or ""
            sym_ticker, sym_expiry, sym_right, sym_strike = _parse_option_symbol(option_sym)

            # Fallback to direct fields if OCC parse failed
            right  = sym_right  or ("C" if "call" in (c.get("type") or "").lower() else "P")
            intent = "bullish" if right == "C" else "bearish"
            strike = sym_strike or client._f(c.get("strike") or c.get("strike_price"))
            expiry = sym_expiry or (c.get("expiry") or c.get("expiration_date") or "")[:10]

            premium  = client._f(c.get("total_premium") or c.get("premium"))
            volume   = int(c.get("volume") or 0)
            oi       = int(c.get("open_interest") or 0)
            iv       = client._f(c.get("implied_volatility") or c.get("iv"))
            ask_pct  = client._f(c.get("ask_side_pct") or c.get("min_ask_perc") or 0)
            ask_side = ask_pct >= 0.5

            if not strike or not expiry:
                log.debug("Screener: skipping {} -- no strike/expiry  sym={}".format(
                    ticker, option_sym))
                continue

            score = _score(premium, "sweep", ask_side,
                           volume / oi if oi > 0 else 0, c)

            signals.append(FeedSignal(
                feed_type = "options_flow",
                symbol    = ticker,
                intent    = intent,
                premium   = premium,
                size      = volume,
                score     = score,
                strike    = strike,
                expiry    = expiry,
                right     = right,
                structure = "sweep",
                ask_side  = ask_side,
                volume    = volume,
                oi        = oi,
                iv        = iv / 100 if iv > 1 else iv,
                source_id = option_sym,
                notes     = "SCREENER {} {} ${:.0f}k ask={:.0f}%".format(
                    "CALL" if right == "C" else "PUT",
                    ticker, premium / 1000, ask_pct * 100),
                raw       = c,
            ))
        except Exception as e:
            log.debug("Screener parse error: {}".format(e))

    signals.sort(key=lambda x: x.premium, reverse=True)
    log.info("Screener: {} signals (with strike/expiry)".format(len(signals)))
    return signals
