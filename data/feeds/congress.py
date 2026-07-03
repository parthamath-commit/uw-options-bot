"""
data/feeds/congress.py
======================
UW Congress / Politician trades feed.

Endpoints:
  /api/congress/recent-trades            -- congressional trades
  /api/politician-portfolios/recent_trades -- politician portfolio trades

Verified field names from live API (2026-06-22):
  name             -- politician full name
  ticker           -- stock ticker
  txn_type         -- "Buy" | "Sell" | "Exchange"
  amounts          -- "$1,001 - $15,000" range string
  transaction_date -- YYYY-MM-DD
  security_type    -- "stock" | "option" etc
  member_type      -- "house" | "senate"
  filed_at_date    -- disclosure filing date

Congress members trade stocks and options, often ahead of legislation
or policy changes they're aware of. Large buys from relevant committee
members are strong directional signals.
"""

import logging
from models_feed import FeedSignal

log = logging.getLogger("UWBot.Feed.Congress")


def fetch(client, limit: int = 50) -> list[FeedSignal]:
    """
    Fetch recent congress + politician portfolio trades.
    Combines both endpoints, deduplicates, returns FeedSignal list.
    """
    signals = []

    signals.extend([s for s in _fetch_congress(client, limit) if s is not None])
    signals.extend([s for s in _fetch_politician_portfolios(client, limit) if s is not None])

    # Deduplicate by (symbol, politician, trade_date, intent)
    seen = set()
    unique = []
    for s in signals:
        key = "{}_{}_{}_{}" .format(
            s.symbol, s.politician, s.trade_date, s.intent)
        if key not in seen:
            seen.add(key)
            unique.append(s)

    unique.sort(key=lambda x: x.premium, reverse=True)
    log.info("Congress/Politician: {} trade signals".format(len(unique)))
    return unique


def _fetch_congress(client, limit: int) -> list[FeedSignal]:
    raw    = client._get("/api/congress/recent-trades", {"limit": limit})
    trades = client._as_list(raw)
    return [_parse_trade(t, "congress") for t in trades if t]


def _fetch_politician_portfolios(client, limit: int) -> list[FeedSignal]:
    raw    = client._get(
        "/api/politician-portfolios/recent_trades", {"limit": limit})
    trades = client._as_list(raw)
    return [_parse_trade(t, "politician_portfolio") for t in trades if t]


def _parse_trade(t: dict, source: str) -> FeedSignal | None:
    try:
        # ── Ticker ────────────────────────────────────────────────────────────
        ticker = (
            t.get("ticker") or t.get("symbol") or
            t.get("asset") or ""
        ).upper().strip()
        if not ticker or len(ticker) > 5:
            return None

        # ── Transaction type ──────────────────────────────────────────────────
        # Live API returns "txn_type": "Buy" | "Sell" | "Exchange"
        tx_type = (
            t.get("txn_type") or
            t.get("transaction_type") or
            t.get("transaction") or
            t.get("type") or ""
        ).lower()

        if any(x in tx_type for x in ("purchase", "buy", "bought", "exchange")):
            intent = "bullish"
        elif any(x in tx_type for x in ("sale", "sell", "sold")):
            intent = "bearish"
        else:
            intent = "neutral"

        # ── Amount ────────────────────────────────────────────────────────────
        # Live API returns "amounts": "$1,001 - $15,000" (range string)
        # Use UPPER bound -- it's the minimum guaranteed transaction size
        amount_str = (
            t.get("amounts") or
            t.get("amount") or t.get("value") or
            t.get("transaction_amount") or "0"
        )
        premium = _parse_amount_upper(str(amount_str))

        # ── Politician name ───────────────────────────────────────────────────
        # Live API returns "name" and "reporter"
        politician = (
            t.get("name") or
            t.get("reporter") or
            t.get("politician") or
            t.get("representative") or
            t.get("senator") or
            "{} {}".format(
                t.get("first_name", ""),
                t.get("last_name", "")
            ).strip()
        )

        # ── Date ─────────────────────────────────────────────────────────────
        trade_date = (
            t.get("transaction_date") or
            t.get("trade_date") or
            t.get("disclosure_date") or
            t.get("filed_at_date") or ""
        )[:10]

        # ── Asset type ────────────────────────────────────────────────────────
        # Live API returns "security_type": "stock" | "option" etc
        asset_type = (
            t.get("security_type") or
            t.get("asset_type") or
            t.get("type") or ""
        ).lower()
        is_option = "option" in asset_type

        # ── Member type bonus ─────────────────────────────────────────────────
        member_type = (t.get("member_type") or "").lower()
        is_senate   = member_type == "senate"   # senators trade less = higher signal

        score = _score(premium, intent, is_option, is_senate)
        if score == 0:
            return None

        # ── Notes ─────────────────────────────────────────────────────────────
        notes_raw = t.get("notes") or ""
        notes_short = notes_raw[:80] if notes_raw else ""
        chamber = "SEN" if is_senate else "REP"
        notes = "{} [{}] {} ${:.0f}k {}{}".format(
            politician, chamber, tx_type,
            premium / 1000, ticker,
            " [OPTION]" if is_option else ""
        )
        if notes_short:
            notes += "\n{}".format(notes_short)

        return FeedSignal(
            feed_type  = source,
            symbol     = ticker,
            intent     = intent,
            premium    = premium,
            score      = score,
            politician = politician,
            trade_date = trade_date,
            notes      = notes,
            raw        = t,
        )
    except Exception as e:
        log.debug("Congress parse error: {}".format(e))
        return None


def _parse_amount_upper(amount_str: str) -> float:
    """
    Parse UW amount ranges like '$1,001 - $15,000' -> upper bound ($15,000).
    Using the upper bound avoids filtering out legitimate trades that might
    have midpoints below the minimum score threshold.
    Falls back to the single value if no range is present.
    """
    try:
        import re
        nums = re.findall(r"[\d,]+", amount_str.replace("$", ""))
        if not nums:
            return 0.0
        vals = [float(n.replace(",", "")) for n in nums]
        return max(vals)   # use UPPER bound, not midpoint
    except Exception:
        return 0.0


def _score(premium: float, intent: str, is_option: bool, is_senate: bool = False) -> int:
    if intent == "neutral":
        return 0

    s = 40   # base for any directional congress trade

    # Premium tiers -- lowered floor to $1k to capture small trades
    if premium >= 1_000_000:  s += 30
    elif premium >= 250_000:  s += 20
    elif premium >= 50_000:   s += 10
    elif premium >= 15_000:   s += 5
    elif premium >= 1_000:    s += 2   # capture small disclosures
    else:
        return 0

    if is_option:   s += 15   # option trade = specific directional conviction
    if is_senate:   s += 5    # senators trade less often = higher signal quality

    return max(0, min(100, s))
