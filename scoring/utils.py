"""
scoring/utils.py
================
Composite score blender and position sizing calculator.
"""

import logging
from config import cfg

log = logging.getLogger("UWBot.ScoringUtils")


def composite_score(additive: float, institutional: float) -> float:
    """55% additive + 45% institutional."""
    return round(additive * 0.55 + institutional * 0.45, 1)


def calculate_position_size(
    portfolio_value: float,
    option_ask: float,
    delta: float,
) -> dict:
    """
    1% portfolio risk rule.

    contracts   = floor( portfolio × MAX_RISK_PCT / (ask × 100) )
    target_exit = ask × (1 + TARGET_GAIN_PCT)     → +65%
    stop        = ask × 0.30                       → exit at −70%

    Returns: contracts, max_loss, target_exit, stop_premium
    """
    if option_ask <= 0:
        return {
            "contracts": 0, "max_loss": 0.0,
            "target_exit": 0.0, "stop_premium": 0.0
        }

    # Use provided delta or default to 0.5 (ATM assumption) if not available
    # UW flow signals often don't include greeks -- don't let delta=0 kill sizing
    effective_delta = delta if delta > 0 else 0.5

    max_risk   = portfolio_value * cfg.MAX_RISK_PCT
    contracts  = int(max_risk / (option_ask * 100))
    max_loss   = round(contracts * option_ask * 100, 2)
    target     = round(option_ask * (1 + cfg.TARGET_GAIN_PCT), 2)
    stop       = round(option_ask * 0.30, 2)

    log.debug(
        f"Sizing  ask={option_ask:.2f}  portfolio=${portfolio_value:,.0f}  "
        f"→ {contracts} cts  risk=${max_loss:,.0f}  "
        f"target={target:.2f}  stop={stop:.2f}"
    )
    return {
        "contracts":    contracts,
        "max_loss":     max_loss,
        "target_exit":  target,
        "stop_premium": stop,
    }
