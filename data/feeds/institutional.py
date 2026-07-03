"""
data/feeds/institutional.py
============================
UW Institutional + OI Change feeds.

Verified field names from live API (2026-06-22):

  /api/market/oi-change:
    underlying_symbol, option_symbol (OCC format), oi_change, curr_oi,
    last_oi, volume, curr_date, rnk

  /api/market/top-net-impact:
    ticker, net_premium

  OCC option_symbol format: TICKER + YYMMDD + C/P + 8-digit-strike
  e.g. WMB261120C00080000 → WMB, 2026-11-20, C, $80
"""

import logging
import re
from models_feed import FeedSignal

log = logging.getLogger("UWBot.Feed.Institutional")


def fetch(client) -> list[FeedSignal]:
    """Fetch all institutional signals and return combined list."""
    signals = []
    signals.extend(_fetch_oi_change(client))
    signals.extend(_fetch_net_impact(client))
    signals.sort(key=lambda x: x.score, reverse=True)
    log.info("Institutional: {} signals".format(len(signals)))
    return signals


def _parse_occ_symbol(option_symbol: str):
    """
    Parse OCC symbol into (right, strike).
    WMB261120C00080000 → ('C', 80.0)
    Returns (None, None) on failure.
    """
    if not option_symbol:
        return None, None
    try:
        m = re.match(r"[A-Z]+(\d{6})([CP])(\d{8})", option_symbol.upper())
        if not m:
            return None, None
        right  = m.group(2)
        strike = float(m.group(3)) / 1000.0
        return right, strike
    except Exception:
        return None, None


def _fetch_oi_change(client) -> list[FeedSignal]:
    """
    Market-wide OI change — tickers with largest OI shifts today.
    API returns one row per contract (option_symbol), with a single
    oi_change value. Aggregate per underlying to get net call/put OI change.
    """
    raw   = client._get("/api/market/oi-change")
    items = client._as_list(raw)
    signals = []

    # Aggregate by underlying_symbol
    by_ticker: dict = {}
    for item in items:
        try:
            # API: underlying_symbol (not ticker/symbol)
            ticker = (item.get("underlying_symbol") or
                      item.get("ticker") or
                      item.get("symbol") or "").upper()
            if not ticker or len(ticker) > 5:
                continue

            option_sym = item.get("option_symbol") or ""
            right, _ = _parse_occ_symbol(option_sym)

            oi_change = float(item.get("oi_change") or
                              item.get("oi_diff_plain") or 0)
            volume    = float(item.get("volume") or 0)

            if ticker not in by_ticker:
                by_ticker[ticker] = {"call_oi": 0, "put_oi": 0,
                                     "total_vol": 0, "raw": item}

            if right == "C":
                by_ticker[ticker]["call_oi"] += oi_change
            elif right == "P":
                by_ticker[ticker]["put_oi"] += oi_change
            else:
                # No right parsed — add to whichever side based on sign
                if oi_change > 0:
                    by_ticker[ticker]["call_oi"] += oi_change
                else:
                    by_ticker[ticker]["put_oi"] += abs(oi_change)

            by_ticker[ticker]["total_vol"] += volume

        except Exception as e:
            log.debug("OI change parse error: {}".format(e))

    for ticker, d in by_ticker.items():
        call_oi = d["call_oi"]
        put_oi  = d["put_oi"]
        net_oi  = call_oi - put_oi

        if abs(net_oi) < 500:
            continue

        intent = "bullish" if net_oi > 0 else "bearish"
        score  = _score_oi(abs(net_oi), call_oi, put_oi)
        if score < 30:
            continue

        signals.append(FeedSignal(
            feed_type = "oi_change",
            symbol    = ticker,
            intent    = intent,
            size      = int(abs(net_oi)),
            score     = score,
            notes     = "OI: calls {:+.0f}k  puts {:+.0f}k  net={:+.0f}k".format(
                call_oi / 1000, put_oi / 1000, net_oi / 1000),
            raw = d["raw"],
        ))

    return signals


def _fetch_net_impact(client) -> list[FeedSignal]:
    """
    Top net impact — tickers with most net premium flow pressure.
    API: ticker, net_premium (not net_impact/net_delta_impact)
    """
    raw   = client._get("/api/market/top-net-impact")
    items = client._as_list(raw)
    signals = []

    for item in items:
        try:
            ticker = (item.get("ticker") or item.get("symbol") or "").upper()
            if not ticker or len(ticker) > 5:
                continue

            # API: net_premium (not net_impact or net_delta_impact)
            net_impact = float(
                item.get("net_premium") or
                item.get("net_impact") or
                item.get("net_delta_impact") or 0)

            if abs(net_impact) < 100_000:
                continue

            intent = "bullish" if net_impact > 0 else "bearish"
            score  = min(int(abs(net_impact) / 1_000_000 * 30) + 40, 85)

            signals.append(FeedSignal(
                feed_type = "net_impact",
                symbol    = ticker,
                intent    = intent,
                premium   = abs(net_impact),
                score     = score,
                notes     = "Net premium: ${:.1f}M {}".format(
                    net_impact / 1_000_000,
                    "bullish" if net_impact > 0 else "bearish"),
                raw = item,
            ))

        except Exception as e:
            log.debug("Net impact parse error: {}".format(e))

    return signals


def _score_oi(abs_change, call_change, put_change) -> int:
    s = 30
    if abs_change >= 100_000: s += 30
    elif abs_change >= 50_000: s += 20
    elif abs_change >= 10_000: s += 10
    elif abs_change >= 1_000:  s += 5
    total = abs(call_change) + abs(put_change)
    if total > 0:
        ratio = max(abs(call_change), abs(put_change)) / total
        if ratio > 0.8:   s += 15
        elif ratio > 0.65: s += 8
    return max(0, min(100, s))
