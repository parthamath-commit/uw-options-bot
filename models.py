"""
models.py
=========
Shared dataclasses — DealerExposure, FlowSignal, ScoredSignal.
No vendor-specific fields. Pure data containers.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from config import ET


@dataclass
class DealerExposure:
    """
    Dealer greek exposure for a single symbol.
    Sourced from Unusual Whales greek-exposure + spot-exposures endpoints.
    """
    symbol: str
    net_gex: float = 0.0           # Net Gamma Exposure ($)
    net_dex: float = 0.0           # Net Delta Exposure ($)
    net_vex: float = 0.0           # Net Vanna Exposure ($)
    net_chex: float = 0.0          # Net Charm Exposure ($)
    gamma_flip: Optional[float] = None   # price where GEX sign flips
    call_wall: Optional[float] = None    # strike with max positive GEX
    put_wall: Optional[float] = None     # strike with max negative GEX
    regime: str = "unknown"        # positive_gamma | negative_gamma | transitional
    flow_direction: str = "no_flow"  # amplifying | dampening | regime_flip | neutral | no_flow
    # Bug fix: default was "neutral" which gave unenriched symbols a free pass
    # in the institutional scorer. "no_flow" correctly applies the -5 penalty.
    timestamp: str = field(default_factory=lambda: datetime.now(ET).isoformat())


@dataclass
class FlowSignal:
    """
    Single unusual options flow alert from Unusual Whales.
    Normalised from raw UW response into a vendor-neutral shape.
    """
    symbol: str
    strike: float
    expiry: str                    # YYYY-MM-DD
    right: str                     # C or P
    structure: str                 # sweep | block
    intent: str                    # bullish | bearish | neutral
    score: int                     # synthesised 0–100
    delta: float = 0.0
    iv: float = 0.0                # decimal (0.35 = 35%)
    moneyness: str = ""            # ITM | ATM | OTM
    premium: float = 0.0           # total premium ($)
    ask_side: bool = False         # True = aggressor paid ask or above
    oi: int = 0                    # open interest
    volume: int = 0                # contract volume
    raw: dict = field(default_factory=dict)


@dataclass
class ScoredSignal:
    """
    Fully scored and enriched signal ready for alerting and logging.
    Combines FlowSignal data, dealer exposure, live quote, and sizing.
    """
    symbol: str
    strike: float
    expiry: str
    right: str
    # Scores
    uw_score: int                  # synthesised UW score (0–100)
    additive_score: float          # additive scorer output
    institutional_score: float     # institutional flow scorer output
    composite_score: float         # weighted blend (0–100)
    # Signal metadata
    intent: str
    structure: str
    premium: float = 0.0
    ask_side: bool = False
    # Dealer context
    dealer_gex: float = 0.0
    dealer_dex: float = 0.0
    dealer_vex: float = 0.0
    dealer_chex: float = 0.0
    dealer_regime: str = "unknown"
    gamma_flip: Optional[float] = None
    call_wall: Optional[float] = None
    put_wall: Optional[float] = None
    gex_regime_blocked: bool = False
    flow_direction: str = "neutral"
    # Dark pool context
    darkpool_sentiment: str = "neutral"
    # IV
    iv_percentile: float = 0.0
    # Live quote (IBKR)
    live_bid: float = 0.0
    live_ask: float = 0.0
    live_delta: float = 0.0
    live_iv: float = 0.0
    # Position sizing
    suggested_contracts: int = 0
    max_loss_dollar: float = 0.0
    target_exit_premium: float = 0.0
    stop_premium: float = 0.0
    # Meta
    # Signal classification (set by hybrid rule+ML classifier)
    signal_type: str = "unidirectional"  # final type: unidirectional | straddle | strangle | hedging | conflict | noise
    rule_type: str = "unidirectional"    # rule-based type before ML validation
    ml_validation: str = "rule_only"     # rule_only | ml_confirmed | ml_uncertain | ml_override | ml_agree_low
    # ML scoring (populated after model training)
    ml_type: str = "unknown"             # ML-predicted signal type
    ml_confidence: float = 0.0           # ML classification confidence 0-100
    ml_win_prob: float = 50.0            # ML win probability 0-100
    ml_composite: float = 0.0           # ML composite score 0-100
    ml_available: bool = False           # True when models are trained
    # Cross-feed context (populated by aggregator)
    feeds_confirming: list = None
    cross_feed_score: float = 0.0
    cross_feed_summary: str = ""
    alert_sent: bool = False
    timestamp: str = field(default_factory=lambda: datetime.now(ET).isoformat())
