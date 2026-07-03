"""
data/feeds/lit_flow.py
======================
UW Lit Flow feed -- large exchange-lit (on-exchange) trades.
Endpoint: GET /api/lit-flow/recent

Verified field names from live API (2026-06-22):
  size, ticker, price, volume, premium (pre-computed price*size),
  nbbo_bid, nbbo_ask, executed_at, canceled, trade_code

Side determination: API has no 'side' field.
Use price vs nbbo midpoint: above mid = bullish, below = bearish.
"""

import logging
from models_feed import FeedSignal

log = logging.getLogger("UWBot.Feed.LitFlow")

MIN_LIT_FLOW_USD = 100_000


def fetch(client, limit: int = 100) -> list[FeedSignal]:
    """
    Fetch recent large lit-exchange prints.
    Groups by ticker, returns net sentiment per ticker.
    """
    raw    = client._get("/api/lit-flow/recent", {"limit": limit})
    trades = client._as_list(raw)

    if not trades:
        return []

    # Group by ticker
    groups: dict = {}
    for t in trades:
        try:
            ticker = (t.get("ticker") or t.get("symbol") or "").upper()
            if not ticker or len(ticker) > 5:
                continue

            # Skip canceled trades
            if str(t.get("canceled","False")).lower() == "true":
                continue

            price = float(t.get("price") or 0)
            size  = float(t.get("size") or t.get("volume") or 0)

            # Use pre-computed premium if available, else calculate
            usd = float(t.get("premium") or 0) or (price * size)
            if usd < MIN_LIT_FLOW_USD:
                continue

            # Determine side from price vs NBBO mid
            # API has no 'side' field — compare vs nbbo midpoint
            nbbo_bid = float(t.get("nbbo_bid") or 0)
            nbbo_ask = float(t.get("nbbo_ask") or 0)
            if nbbo_bid > 0 and nbbo_ask > 0:
                nbbo_mid = (nbbo_bid + nbbo_ask) / 2
                if price >= nbbo_ask:
                    intent = "bullish"   # paid ask or above = aggressor buying
                elif price <= nbbo_bid:
                    intent = "bearish"   # hit bid = aggressor selling
                else:
                    intent = "neutral"
            else:
                intent = "neutral"

            if ticker not in groups:
                groups[ticker] = {
                    "bull_usd": 0, "bear_usd": 0,
                    "total_usd": 0, "n_trades": 0, "raw": t
                }
            groups[ticker]["total_usd"] += usd
            groups[ticker]["n_trades"]  += 1
            if intent == "bullish":
                groups[ticker]["bull_usd"] += usd
            elif intent == "bearish":
                groups[ticker]["bear_usd"] += usd

        except Exception as e:
            log.debug("Lit flow parse: {}".format(e))

    signals = []
    for ticker, g in groups.items():
        total = g["total_usd"]
        if total < MIN_LIT_FLOW_USD:
            continue

        bull_ratio = g["bull_usd"] / total if total > 0 else 0.5
        intent = ("bullish"  if bull_ratio > 0.6
                  else "bearish" if bull_ratio < 0.4
                  else "neutral")

        score = _score(total, bull_ratio, g["n_trades"])
        if score < 30:
            continue

        signals.append(FeedSignal(
            feed_type = "lit_flow",
            symbol    = ticker,
            intent    = intent,
            premium   = total,
            size      = g["n_trades"],
            score     = score,
            notes     = "Lit: {} prints ${:.0f}k {:.0f}% bull".format(
                g["n_trades"], total / 1000, bull_ratio * 100),
            raw = g["raw"],
        ))

    signals.sort(key=lambda x: x.premium, reverse=True)
    log.info("Lit flow: {} ticker signals".format(len(signals)))
    return signals


def _score(total_usd, bull_ratio, n_trades) -> int:
    s = 30
    if total_usd >= 5_000_000:   s += 25
    elif total_usd >= 1_000_000: s += 15
    elif total_usd >= 500_000:   s += 8
    conviction = abs(bull_ratio - 0.5) * 2
    s += int(conviction * 20)
    if n_trades >= 5:   s += 10
    elif n_trades >= 3: s += 5
    return max(0, min(100, s))
