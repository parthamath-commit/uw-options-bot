"""
scoring/institutional.py
========================
Institutional flow confirmation engine.

Uses VEX (Vanna), CHEX (Charm), DEX (Delta), and flow_direction
from UW dealer-exposure data as a secondary confirmation layer.

Baseline: 50 (neutral). Output range: 0–100.
"""

import logging
from models import FlowSignal, DealerExposure

log = logging.getLogger("UWBot.InstitutionalScorer")


class InstitutionalFlowScorer:

    @staticmethod
    def score(signal: FlowSignal, exposure: DealerExposure) -> float:
        result, _ = InstitutionalFlowScorer.score_with_evidence(signal, exposure)
        return result

    @staticmethod
    def score_with_evidence(signal: FlowSignal, exposure: DealerExposure) -> tuple:
        """Returns (score_float, evidence_dict)."""
        ev = {"baseline": 50.0}
        s  = 50.0

        # VEX
        if signal.intent == "bullish" and exposure.net_vex > 0:     ev["vex"] = 10.0
        elif signal.intent == "bearish" and exposure.net_vex < 0:   ev["vex"] = 10.0
        elif signal.intent == "bullish" and exposure.net_vex < 0:   ev["vex"] = -5.0
        elif signal.intent == "bearish" and exposure.net_vex > 0:   ev["vex"] = -5.0
        else:                                                         ev["vex"] = 0.0
        s += ev["vex"]

        # CHEX
        if exposure.net_chex > 0 and signal.intent == "bullish":    ev["chex"] = 8.0
        elif exposure.net_chex < 0 and signal.intent == "bearish":  ev["chex"] = 8.0
        else:                                                         ev["chex"] = 0.0
        s += ev["chex"]

        # Flow direction
        fd = exposure.flow_direction
        if signal.intent == "bullish"  and fd == "amplifying":      ev["flow_dir"] = 12.0
        elif signal.intent == "bearish" and fd == "regime_flip":    ev["flow_dir"] = 15.0
        elif signal.intent == "bullish" and fd == "regime_flip":    ev["flow_dir"] = 10.0
        elif fd == "dampening":                                       ev["flow_dir"] = -8.0
        elif fd == "no_flow":                                         ev["flow_dir"] = -5.0   # reduced: most unenriched symbols return no_flow
        else:                                                         ev["flow_dir"] = 0.0
        s += ev["flow_dir"]

        # DEX
        # Positive DEX = dealers net long delta (supports upside)
        # Negative DEX = dealers net short delta (supports downside)
        if signal.intent == "bullish" and exposure.net_dex < 0:     ev["dex"] = 8.0   # dealers short = fuel for squeeze
        elif signal.intent == "bearish" and exposure.net_dex < 0:   ev["dex"] = 8.0   # dealers short = amplifies drop
        elif signal.intent == "bullish" and exposure.net_dex > 0:   ev["dex"] = 0.0   # dealers long = neutral for calls
        else:                                                          ev["dex"] = 0.0
        s += ev["dex"]

        # Ask-side
        ev["ask_side"] = 5.0 if signal.ask_side else 0.0
        s += ev["ask_side"]

        result = min(max(round(s, 1), 0.0), 100.0)
        log.debug(
            "Institutional  {} {}{}  vex={:.0f}  fd={}  ask={}  → {}  "
            "[vex={} chex={} fd={} dex={} ask={}]".format(
                signal.symbol, signal.strike, signal.right,
                exposure.net_vex, fd, signal.ask_side, result,
                ev["vex"], ev["chex"], ev["flow_dir"], ev["dex"], ev["ask_side"])
        )
        return result, ev
