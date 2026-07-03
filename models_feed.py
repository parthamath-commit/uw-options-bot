"""
models_feed.py
==============
FeedSignal -- unified signal model across all UW data feeds.

Every feed (options flow, dark pool, congress, insider, institutional)
produces FeedSignal objects with a common structure so the scoring
engine and scanner can handle them uniformly.

feed_type values:
  options_flow    -- unusual sweep/block from flow alerts
  dark_pool       -- large off-exchange print
  congress        -- politician trade disclosure
  insider         -- corporate insider buy/sell
  institutional   -- 13F institutional holding change
  oi_change       -- market-wide OI shift on a ticker
  lit_flow        -- exchange-lit large print
  net_impact      -- top net options impact mover
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from config import ET


@dataclass
class FeedSignal:
    """
    Unified signal from any UW data feed.
    All feeds normalise to this shape before scoring.
    """
    # Identity
    feed_type: str              # which feed generated this signal
    symbol: str                 # ticker
    intent: str                 # bullish | bearish | neutral

    # Strength indicators
    premium: float = 0.0        # dollar value of the activity
    size: int = 0               # share/contract count
    score: int = 0              # feed-specific raw score 0-100

    # Options-specific (populated for options_flow only)
    strike: float = 0.0
    expiry: str = ""
    right: str = ""             # C or P
    structure: str = ""         # sweep | block
    ask_side: bool = False
    volume: int = 0
    oi: int = 0
    iv: float = 0.0

    # Context
    politician: str = ""        # for congress feed
    insider_name: str = ""      # for insider feed
    institution: str = ""       # for institutional feed
    trade_date: str = ""        # when the trade occurred
    notes: str = ""             # human-readable description

    # Meta
    source_id: str = ""         # original ID from UW
    raw: dict = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(ET).isoformat()
    )
