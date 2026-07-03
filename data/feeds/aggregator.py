"""
data/feeds/aggregator.py
========================
Multi-feed aggregator for UW Options Bot.

Runs all enabled feeds in sequence and returns a unified list
of FeedSignal objects sorted by score descending.

Feeds run per cycle:
  options_flow   -- 1 API call  (primary, always enabled)
  dark_pool      -- 1 API call  (always enabled)
  congress       -- 2 API calls (always enabled)
  insider        -- 1 API call  (always enabled)
  institutional  -- 2 API calls (always enabled)
  lit_flow       -- 1 API call  (always enabled)

Total: ~8 API calls for entire market across all feeds.
(vs 76 calls for 12-symbol watchlist polling)

Feed configuration in .env:
  FEED_OPTIONS_FLOW=true
  FEED_DARK_POOL=true
  FEED_CONGRESS=true
  FEED_INSIDER=true
  FEED_INSTITUTIONAL=true
  FEED_LIT_FLOW=true
  FEED_MIN_SCORE=40          min score to include in unified output
"""

import os
import logging
import time

from models_feed import FeedSignal

log = logging.getLogger("UWBot.Feeds")

# Feed enable flags from .env
FEED_CONFIG = {
    "options_flow":  os.getenv("FEED_OPTIONS_FLOW",  "true").lower()  == "true",
    "dark_pool":     os.getenv("FEED_DARK_POOL",     "true").lower()  == "true",
    "congress":      os.getenv("FEED_CONGRESS",      "true").lower()  == "true",
    "insider":       os.getenv("FEED_INSIDER",       "true").lower()  == "true",
    "institutional": os.getenv("FEED_INSTITUTIONAL", "true").lower()  == "true",
    "lit_flow":      os.getenv("FEED_LIT_FLOW",      "true").lower()  == "true",
    "news":          os.getenv("FEED_NEWS",          "true").lower()  == "true",
}
FEED_MIN_SCORE = int(os.getenv("FEED_MIN_SCORE", "40"))


def run_all_feeds(client) -> tuple[list[FeedSignal], dict]:
    """
    Run all enabled feeds and return unified signal list + stats.

    Returns:
      signals  : list[FeedSignal] sorted by score desc
      stats    : dict with per-feed counts and API call count
    """
    from data.feeds import options_flow, dark_pool, congress
    from data.feeds import insider, institutional, lit_flow
    from data.feeds import news as news_feed

    all_signals: list[FeedSignal] = []
    stats = {"api_calls": 0, "per_feed": {}}

    feed_map = {
        "options_flow":  (options_flow.fetch,  {"limit": 200}),
        "dark_pool":     (dark_pool.fetch,     {"limit": 100}),
        "congress":      (congress.fetch,      {"limit": 50}),
        "insider":       (insider.fetch,       {"limit": 100}),
        "institutional": (institutional.fetch, {}),
        "lit_flow":      (lit_flow.fetch,      {"limit": 100}),
        "news":          (news_feed.fetch,     {"lookback_hours": 4}),
    }

    # API call estimates per feed
    api_calls_per_feed = {
        "options_flow":  1,
        "dark_pool":     1,
        "congress":      2,
        "insider":       1,
        "institutional": 2,
        "lit_flow":      1,
        "news":          4,   # headlines + econ cal + fda + earnings
    }

    for feed_name, (fetch_fn, kwargs) in feed_map.items():
        if not FEED_CONFIG.get(feed_name, True):
            log.debug("{}: disabled".format(feed_name))
            continue
        try:
            feed_signals = fetch_fn(client, **kwargs)
            # Filter by min score
            qualifying = [s for s in feed_signals if s.score >= FEED_MIN_SCORE]
            all_signals.extend(qualifying)
            stats["per_feed"][feed_name] = len(qualifying)
            stats["api_calls"] += api_calls_per_feed.get(feed_name, 1)
            time.sleep(0.15)   # gentle rate limiting between feeds
        except Exception as e:
            log.error("{} feed error: {}".format(feed_name, e))
            stats["per_feed"][feed_name] = 0

    # Sort by score descending
    all_signals.sort(key=lambda x: x.score, reverse=True)

    log.info(
        "All feeds complete: {} total signals, ~{} API calls  |  {}".format(
            len(all_signals),
            stats["api_calls"],
            "  ".join("{}: {}".format(k, v) for k, v in stats["per_feed"].items())
        )
    )
    return all_signals, stats


def get_tickers_from_feeds(signals: list[FeedSignal]) -> list[str]:
    """
    Extract unique tickers from feed signals, sorted by
    number of feeds confirming the ticker (cross-feed conviction).
    Tickers appearing in multiple feeds get higher priority.
    """
    ticker_feeds: dict = {}
    ticker_score: dict = {}

    for sig in signals:
        t = sig.symbol
        if t not in ticker_feeds:
            ticker_feeds[t] = set()
            ticker_score[t] = 0
        ticker_feeds[t].add(sig.feed_type)
        ticker_score[t] += sig.score

    # Sort: first by number of confirming feeds, then by total score
    sorted_tickers = sorted(
        ticker_feeds.keys(),
        key=lambda t: (len(ticker_feeds[t]), ticker_score[t]),
        reverse=True
    )
    return sorted_tickers


def get_cross_feed_context(
    symbol: str,
    all_signals: list[FeedSignal]
) -> dict:
    """
    Return cross-feed context for a symbol.
    Used to enrich Telegram alerts with multi-source confirmation.

    Returns dict with:
      feeds_confirming  : list of feed names with signals for symbol
      cross_feed_score  : bonus score from multi-feed confirmation
      summary           : human-readable multi-feed summary
    """
    sym_signals = [s for s in all_signals if s.symbol == symbol]
    if not sym_signals:
        return {
            "feeds_confirming":  [],
            "cross_feed_score":  0,
            "summary":           "",
        }

    feeds = list({s.feed_type for s in sym_signals})
    n_feeds = len(feeds)

    # Cross-feed score bonus
    # Each additional confirming feed beyond options_flow adds score
    extra_feeds = [f for f in feeds if f != "options_flow"]
    cross_score = min(len(extra_feeds) * 8, 25)   # max +25

    # Summary notes from each feed
    notes = []
    for sig in sym_signals:
        if sig.notes:
            notes.append("[{}] {}".format(sig.feed_type.upper(), sig.notes))

    return {
        "feeds_confirming": feeds,
        "cross_feed_score": cross_score,
        "n_feeds":          n_feeds,
        "summary":          "\n".join(notes[:5]),   # top 5 notes
    }
