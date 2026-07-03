"""
scoring/additive.py
===================
Additive scorer — primary signal quality engine.

Inputs  : FlowSignal, DealerExposure, iv_percentile (float)
Output  : float 0–100

Components
──────────
  Structure      sweep +20 | block +12
  Intent         directional +15 | neutral +5
  UW score       scaled 0–25 (the synthesised UW 0-100 score)
  IV percentile  low IV = bonus for BTO | high IV = penalty  (v17 bug fixed)
  GEX regime     intent aligned with regime +10 | transitional +5
  Dark pool      sentiment confirms intent +8
  Premium size   tiered 0–15
"""

import logging
from models import FlowSignal, DealerExposure

log = logging.getLogger("UWBot.AdditiveScorer")


class AdditiveScorer:

    @staticmethod
    def score(
        signal: FlowSignal,
        exposure: DealerExposure,
        iv_pct: float,
        darkpool_sentiment: str = "neutral",
    ) -> float:
        result, _ = AdditiveScorer.score_with_evidence(
            signal, exposure, iv_pct, darkpool_sentiment)
        return result

    @staticmethod
    def score_with_evidence(
        signal: FlowSignal,
        exposure: DealerExposure,
        iv_pct: float,
        darkpool_sentiment: str = "neutral",
    ) -> tuple:
        """
        Returns (composite_float, evidence_dict) where evidence_dict has
        one key per scoring component so every point is auditable.
        """
        ev = {}

        # Structure
        if signal.structure == "sweep":   ev["structure"] = 20.0
        elif signal.structure == "block": ev["structure"] = 12.0
        else:                             ev["structure"] = 0.0

        # Intent clarity
        ev["intent"] = 15.0 if signal.intent in ("bullish", "bearish") else 5.0

        # UW synthesised score (scaled 0–25)
        ev["uw_score"] = round((signal.score / 100.0) * 25, 2)

        # IV percentile
        ev["iv_pct_raw"] = iv_pct
        if iv_pct < 30:   ev["iv_bonus"] = 15.0
        elif iv_pct < 50: ev["iv_bonus"] = 10.0
        elif iv_pct < 70: ev["iv_bonus"] = 5.0
        else:             ev["iv_bonus"] = -5.0

        # GEX regime alignment
        if signal.intent == "bullish" and exposure.regime == "positive_gamma":   ev["gex_regime"] = 10.0
        elif signal.intent == "bearish" and exposure.regime == "negative_gamma": ev["gex_regime"] = 10.0
        elif signal.intent in ("bullish","bearish") and exposure.regime == "transitional": ev["gex_regime"] = 5.0
        else:                                                                    ev["gex_regime"] = 0.0

        # Dark pool confirmation
        if darkpool_sentiment == signal.intent:              ev["darkpool"] = 8.0
        elif darkpool_sentiment not in ("neutral", ""):      ev["darkpool"] = -4.0
        else:                                                ev["darkpool"] = 0.0

        # Premium size
        if signal.premium >= 5_000_000:   ev["premium_size"] = 15.0
        elif signal.premium >= 1_000_000: ev["premium_size"] = 10.0
        elif signal.premium >= 500_000:   ev["premium_size"] = 5.0
        else:                             ev["premium_size"] = 0.0

        total = sum([ev["structure"], ev["intent"], ev["uw_score"],
                     ev["iv_bonus"], ev["gex_regime"], ev["darkpool"],
                     ev["premium_size"]])
        result = min(max(round(total, 1), 0.0), 100.0)

        log.debug(
            "Additive  {} {}{}  iv={:.0f}  dp={}  → {}  "
            "[struct={} intent={} uw={} iv={} gex={} dp={} prem={}]".format(
                signal.symbol, signal.strike, signal.right,
                iv_pct, darkpool_sentiment, result,
                ev["structure"], ev["intent"], ev["uw_score"],
                ev["iv_bonus"], ev["gex_regime"], ev["darkpool"],
                ev["premium_size"])
        )
        return result, ev
