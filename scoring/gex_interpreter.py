"""
scoring/gex_interpreter.py
===========================
Translates raw GEX/dealer exposure data into actionable
plain-English trading guidance.

Replaces raw number display in Telegram alerts with
context that directly informs trade decisions.

Output structure:
  regime_action   -- what the GEX regime means for your trade
  size_guidance   -- how to size based on dealer positioning
  key_levels      -- actionable price levels to watch
  conviction      -- overall conviction modifier (0.5 - 1.5)
  summary         -- single-line actionable summary
"""

from models import DealerExposure


def interpret(
    exp: DealerExposure,
    underlying_price: float,
    intent: str,   # bullish | bearish
) -> dict:
    """
    Convert dealer exposure into actionable trading guidance.

    Returns dict with:
      summary        -- one-line action (shown in Telegram)
      regime_action  -- what the regime means
      size_guidance  -- position sizing implication
      key_levels     -- price levels to watch
      conviction     -- score multiplier (0.5 to 1.5)
      alert_color    -- GREEN | YELLOW | RED
    """
    gex    = exp.net_gex
    flip   = exp.gamma_flip
    cwall  = exp.call_wall
    pwall  = exp.put_wall
    regime = exp.regime
    fd     = exp.flow_direction
    gex_m  = gex / 1_000_000

    # ── Determine proximity to gamma flip ────────────────────────────────────
    near_flip = False
    flip_pct  = None
    if flip and underlying_price and underlying_price > 0:
        flip_pct  = abs(underlying_price - flip) / underlying_price * 100
        near_flip = flip_pct < 1.5   # within 1.5% of flip level

    # ── Classify GEX regime strength ─────────────────────────────────────────
    gex_strong   = abs(gex_m) > 500
    gex_moderate = abs(gex_m) > 100

    # ── Build actionable output ───────────────────────────────────────────────

    # CASE 1: Near gamma flip -- highest priority
    if near_flip:
        return _near_flip(gex_m, flip, underlying_price, flip_pct, intent)

    # CASE 2: Regime flip in progress (flow_direction)
    if fd == "regime_flip":
        return _regime_flip(gex_m, intent, cwall, pwall)

    # CASE 3: Strong positive gamma
    if regime == "positive_gamma" and gex_strong:
        return _strong_positive(gex_m, intent, cwall, pwall, flip, fd)

    # CASE 4: Moderate positive gamma
    if regime == "positive_gamma" and gex_moderate:
        return _moderate_positive(gex_m, intent, cwall, pwall, fd)

    # CASE 5: Strong negative gamma
    if regime == "negative_gamma" and gex_strong:
        return _strong_negative(gex_m, intent, cwall, pwall, flip, fd)

    # CASE 6: Moderate negative gamma
    if regime == "negative_gamma" and gex_moderate:
        return _moderate_negative(gex_m, intent, cwall, pwall, fd)

    # CASE 7: Transitional / weak GEX
    return _transitional(gex_m, intent, flip)


# ── Case handlers ─────────────────────────────────────────────────────────────

def _near_flip(gex_m, flip, price, flip_pct, intent):
    direction = "above" if price > flip else "below"
    return {
        "summary":       "ACCELERATION ZONE -- price {:.1f}% from gamma flip {:.2f}".format(
            flip_pct, flip),
        "regime_action": "Price near gamma flip level. Moves can accelerate sharply once {:.2f} breaks.".format(flip),
        "size_guidance": "Reduce size 30-50% -- flip zones have unpredictable dealer hedging.",
        "key_levels":    "Watch: {:.2f} (gamma flip)".format(flip),
        "conviction":    0.7,   # lower conviction -- too close to flip
        "alert_color":   "YELLOW",
    }

def _regime_flip(gex_m, intent, cwall, pwall):
    new_regime = "positive" if gex_m > 0 else "negative"
    levels = ""
    if cwall: levels += "  Call wall: {:.2f}".format(cwall)
    if pwall: levels += "  Put wall: {:.2f}".format(pwall)
    return {
        "summary":       "REGIME CHANGE -- GEX flipped to {} gamma. High conviction setup.".format(new_regime),
        "regime_action": "Gamma regime just changed. Dealers are re-hedging -- directional momentum likely.",
        "size_guidance": "Full size justified -- regime flips create strong directional moves.",
        "key_levels":    "New levels:{}".format(levels) if levels else "Levels being established.",
        "conviction":    1.4,   # regime flips = high conviction
        "alert_color":   "GREEN",
    }

def _strong_positive(gex_m, intent, cwall, pwall, flip, fd):
    levels = ""
    if cwall: levels += "  Resistance: {:.2f} (call wall)".format(cwall)
    if pwall: levels += "  Support: {:.2f} (put wall)".format(pwall)
    if flip:  levels += "  Flip: {:.2f}".format(flip)

    if intent == "bullish":
        action  = "Dealer tailwind -- dips bought, calls favored. Target call wall {:.2f}.".format(cwall) if cwall else "Dealer tailwind -- dips bought, calls favored."
        sizing  = "Full size -- positive gamma supports upside momentum."
        color   = "GREEN"
        conv    = 1.3
    else:
        action  = "Counter-trend put. Dealers will fight downside. Strong put wall at {:.2f}.".format(pwall) if pwall else "Counter-trend put. Dealers will fight downside."
        sizing  = "Reduce size 25% -- buying puts into strong positive gamma is counter-trend."
        color   = "YELLOW"
        conv    = 0.8

    if fd == "dampening":
        action += " Flow dampening -- momentum may slow near key levels."
        conv   *= 0.9

    return {
        "summary":       "{} -- GEX ${:.0f}M POSITIVE  {}".format(
            "DEALER TAILWIND" if intent == "bullish" else "COUNTER-TREND WARNING",
            abs(gex_m), "Dips supported." if intent == "bullish" else "Puts fighting dealers."),
        "regime_action": action,
        "size_guidance": sizing,
        "key_levels":    levels,
        "conviction":    conv,
        "alert_color":   color,
    }

def _moderate_positive(gex_m, intent, cwall, pwall, fd):
    if intent == "bullish":
        return {
            "summary":       "MILD DEALER SUPPORT -- positive GEX ${:.0f}M, calls favored".format(abs(gex_m)),
            "regime_action": "Moderate dealer support. Dips likely bought. Calls have tailwind.",
            "size_guidance": "Standard size.",
            "key_levels":    "Call wall: {:.2f}".format(cwall) if cwall else "",
            "conviction":    1.1,
            "alert_color":   "GREEN",
        }
    else:
        return {
            "summary":       "MILD COUNTER-TREND -- GEX positive ${:.0f}M, puts fighting dealers".format(abs(gex_m)),
            "regime_action": "Moderate dealer resistance to downside. Puts are counter-trend.",
            "size_guidance": "Reduce size 15-20%.",
            "key_levels":    "Put wall: {:.2f}".format(pwall) if pwall else "",
            "conviction":    0.9,
            "alert_color":   "YELLOW",
        }

def _strong_negative(gex_m, intent, cwall, pwall, flip, fd):
    levels = ""
    if cwall: levels += "  Resistance: {:.2f}".format(cwall)
    if pwall: levels += "  Support: {:.2f}".format(pwall)
    if flip:  levels += "  Flip: {:.2f}  -- recovery above this = regime change".format(flip)

    if intent == "bearish":
        action  = "Dealer amplifier -- declines accelerate. Puts have full tailwind."
        sizing  = "Full size -- negative gamma amplifies downside moves."
        color   = "GREEN"
        conv    = 1.3
    else:
        action  = "High risk call. Dealers amplify drops. Calls need strong catalyst to overcome."
        sizing  = "Reduce size 30-40% -- calls fighting negative gamma regime."
        color   = "RED"
        conv    = 0.6

    if fd == "amplifying" and intent == "bearish":
        action += " Flow amplifying -- momentum building."
        conv   = min(conv * 1.15, 1.5)
        color  = "GREEN"

    return {
        "summary":       "{} -- GEX ${:.0f}M NEGATIVE  {}".format(
            "DEALER AMPLIFIER" if intent == "bearish" else "HIGH RISK CALL",
            abs(gex_m),
            "Puts amplified." if intent == "bearish" else "Avoid or small size only."),
        "regime_action": action,
        "size_guidance": sizing,
        "key_levels":    levels,
        "conviction":    conv,
        "alert_color":   color,
    }

def _moderate_negative(gex_m, intent, cwall, pwall, fd):
    if intent == "bearish":
        return {
            "summary":       "DEALER TAILWIND -- moderate negative GEX ${:.0f}M, puts favored".format(abs(gex_m)),
            "regime_action": "Moderate dealer amplification. Downside moves get exaggerated.",
            "size_guidance": "Standard size.",
            "key_levels":    "Put wall: {:.2f}".format(pwall) if pwall else "",
            "conviction":    1.1,
            "alert_color":   "GREEN",
        }
    else:
        return {
            "summary":       "CAUTION -- negative GEX ${:.0f}M, calls face dealer headwind".format(abs(gex_m)),
            "regime_action": "Moderate negative gamma. Upside recoveries can reverse sharply.",
            "size_guidance": "Reduce size 20%. Use tighter stop.",
            "key_levels":    "Call wall: {:.2f}".format(cwall) if cwall else "",
            "conviction":    0.85,
            "alert_color":   "YELLOW",
        }

def _transitional(gex_m, intent, flip):
    return {
        "summary":       "NEUTRAL GEX -- transitional zone, no strong dealer positioning",
        "regime_action": "Dealers near-neutral. Price action driven by flow, not dealer hedging.",
        "size_guidance": "Standard size. Watch for regime change.",
        "key_levels":    "Flip: {:.2f}".format(flip) if flip else "No clear key levels.",
        "conviction":    1.0,
        "alert_color":   "YELLOW",
    }


def format_for_telegram(interpretation: dict) -> str:
    """
    Format GEX interpretation for Telegram signal alert.
    Replaces raw GEX number display.
    """
    color_emoji = {
        "GREEN":  "🟢",
        "YELLOW": "🟡",
        "RED":    "🔴",
    }.get(interpretation["alert_color"], "⚪")

    lines = [
        "{} <b>GEX SIGNAL:</b> {}".format(
            color_emoji, interpretation["summary"]),
    ]
    if interpretation["regime_action"]:
        lines.append("<b>Context:</b> {}".format(interpretation["regime_action"]))
    if interpretation["size_guidance"]:
        lines.append("<b>Sizing:</b> {}".format(interpretation["size_guidance"]))
    if interpretation["key_levels"]:
        lines.append("<b>Levels:</b> {}".format(interpretation["key_levels"]))

    return "\n".join(lines)
