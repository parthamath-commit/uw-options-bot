"""
data/feeds/dark_pool.py
=======================
UW Dark Pool feed -- recent off-exchange / dark pool prints.
Endpoint: GET /api/darkpool/recent

Large dark pool prints indicate institutional accumulation or distribution
happening outside the lit exchange. When dark pool prints cluster above
the mid price = institutional buying = bullish signal.
"""

import logging
from models_feed import FeedSignal

log = logging.getLogger("UWBot.Feed.DarkPool")

MIN_DARK_POOL_SIZE = 50_000   # minimum dollar value to consider


def fetch(client, limit: int = 100, min_size_usd: float = MIN_DARK_POOL_SIZE) -> list[FeedSignal]:
    """
    Fetch recent dark pool / off-exchange prints across entire market.
    Groups prints by ticker and determines net sentiment.
    Returns one FeedSignal per ticker with net bullish/bearish read.
    """
    raw    = client._get("/api/darkpool/recent", {"limit": limit})
    trades = client._as_list(raw)

    if not trades:
        log.debug("Dark pool: no recent trades")
        return []

    # Aggregate by ticker
    ticker_data: dict = {}
    for t in trades:
        ticker = (t.get("ticker") or t.get("symbol") or "").upper()
        if not ticker or len(ticker) > 5:
            continue

        price  = client._f(t.get("price"))
        size   = client._f(t.get("size") or t.get("volume"))
        usd    = price * size
        if usd < min_size_usd:
            continue

        if ticker not in ticker_data:
            ticker_data[ticker] = {
                "prices": [], "sizes": [], "usd": 0,
                "trades": [], "raw": []
            }
        ticker_data[ticker]["prices"].append(price)
        ticker_data[ticker]["sizes"].append(size)
        ticker_data[ticker]["usd"] += usd
        ticker_data[ticker]["trades"].append(t)
        ticker_data[ticker]["raw"].append(t)

    signals = []
    for ticker, data in ticker_data.items():
        if not data["prices"]:
            continue

        mid   = sum(data["prices"]) / len(data["prices"])
        above = sum(s for p, s in zip(data["prices"], data["sizes"]) if p >= mid)
        below = sum(s for p, s in zip(data["prices"], data["sizes"]) if p < mid)
        total = above + below

        if total == 0:
            continue

        ratio  = above / total
        intent = "bullish" if ratio > 0.6 else "bearish" if ratio < 0.4 else "neutral"
        score  = _score(data["usd"], ratio, len(data["trades"]))

        signals.append(FeedSignal(
            feed_type  = "dark_pool",
            symbol     = ticker,
            intent     = intent,
            premium    = data["usd"],
            size       = int(sum(data["sizes"])),
            score      = score,
            notes      = "DP: {} prints, ${:.0f}k, {:.0f}% above mid".format(
                len(data["trades"]), data["usd"] / 1000, ratio * 100
            ),
            raw        = data["raw"][0] if data["raw"] else {},
        ))

    signals.sort(key=lambda x: x.premium, reverse=True)
    log.info("Dark pool: {} ticker signals".format(len(signals)))
    return signals


def _score(total_usd, above_ratio, n_trades) -> int:
    s = 50   # neutral baseline
    # Premium size
    if total_usd >= 10_000_000: s += 20
    elif total_usd >= 1_000_000: s += 12
    elif total_usd >= 500_000:   s += 6
    # Directional conviction
    conviction = abs(above_ratio - 0.5) * 2   # 0 = neutral, 1 = all one side
    s += int(conviction * 20)
    # Multiple prints = institutional accumulation
    if n_trades >= 5:  s += 10
    elif n_trades >= 3: s += 5
    return max(0, min(100, s))
