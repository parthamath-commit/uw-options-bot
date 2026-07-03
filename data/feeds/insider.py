"""
data/feeds/insider.py
=====================
UW Insider trades feed -- corporate insider buy/sell transactions.
Endpoint: GET /api/insider/transactions

Verified field names from live API (2026-06-22):
  ticker           -- stock ticker
  amount           -- signed dollar amount string (negative = sell, e.g. '-10707950')
  transaction_code -- P=purchase, S=sale, D=disposition, A=award
  owner_name       -- insider full name
  officer_title    -- job title (may be empty)
  is_director      -- bool string 'True'/'False'
  is_officer       -- bool string 'True'/'False'
  is_ten_percent_owner -- bool string
  transaction_date -- YYYY-MM-DD
  filing_date      -- disclosure filing date
  security_title   -- 'Common Stock', 'Stock Option' etc
"""

import logging
from models_feed import FeedSignal

log = logging.getLogger("UWBot.Feed.Insider")

C_SUITE_TITLES = {"ceo","cfo","coo","cto","president","chairman","chief"}


def fetch(client, limit: int = 100) -> list[FeedSignal]:
    """
    Fetch recent insider transactions.
    Returns one FeedSignal per (ticker, intent), aggregating multiple insiders.
    """
    raw    = client._get("/api/insider/transactions", {"limit": limit})
    trades = client._as_list(raw)

    if not trades:
        return []

    # Group by (ticker, intent)
    groups: dict = {}
    for t in trades:
        try:
            ticker = (t.get("ticker") or t.get("symbol") or "").upper()
            if not ticker or len(ticker) > 5:
                continue

            # API: transaction_code — P=purchase, S=sale, D=disposition, A=award
            tx_code = (t.get("transaction_code") or
                       t.get("transaction_type") or
                       t.get("type") or "").upper().strip()

            if tx_code in ("P", "A") or "purchase" in tx_code.lower() or "buy" in tx_code.lower():
                intent = "bullish"
            elif tx_code in ("S", "D") or "sale" in tx_code.lower() or "sell" in tx_code.lower():
                intent = "bearish"
            else:
                continue

            # API: amount is a signed string e.g. '-10707950' or '250000'
            raw_amount = t.get("amount") or t.get("value") or t.get("transaction_amount") or "0"
            try:
                value = abs(float(str(raw_amount).replace(",", "")))
            except Exception:
                value = 0.0

            key = "{}_{}".format(ticker, intent)
            if key not in groups:
                groups[key] = {
                    "ticker": ticker, "intent": intent,
                    "insiders": [], "total_value": 0,
                    "has_csuite": False, "trades": []
                }

            # API: owner_name is single full name field
            name  = (t.get("owner_name") or
                     "{} {}".format(t.get("first_name",""), t.get("last_name","")).strip())

            # API: officer_title + is_director/is_officer booleans
            title = (t.get("officer_title") or t.get("title") or t.get("relationship") or "").lower()
            is_dir = str(t.get("is_director","False")).lower() == "true"
            is_off = str(t.get("is_officer","False")).lower() == "true"

            # Security type — options trades are higher conviction
            sec_title = (t.get("security_title") or "").lower()
            is_option = "option" in sec_title

            display = "{} ({})".format(name, title or ("Dir" if is_dir else "Off" if is_off else "Ins"))

            groups[key]["insiders"].append(display)
            groups[key]["total_value"] += value
            groups[key]["trades"].append(t)

            if is_dir or is_off or any(role in title for role in C_SUITE_TITLES):
                groups[key]["has_csuite"] = True

        except Exception as e:
            log.debug("Insider parse error: {}".format(e))

    signals = []
    for key, g in groups.items():
        if g["total_value"] < 10_000:
            continue

        n_insiders = len(g["insiders"])
        score = _score(g["total_value"], g["intent"], g["has_csuite"], n_insiders)
        if score < 30:
            continue

        signals.append(FeedSignal(
            feed_type    = "insider",
            symbol       = g["ticker"],
            intent       = g["intent"],
            premium      = g["total_value"],
            size         = n_insiders,
            score        = score,
            insider_name = ", ".join(g["insiders"][:3]),
            notes        = "{} insider(s) {}: ${:.0f}k{}{}".format(
                n_insiders,
                "buying" if g["intent"] == "bullish" else "selling",
                g["total_value"] / 1000,
                " [C-SUITE]" if g["has_csuite"] else "",
                " [CLUSTER]" if n_insiders >= 3 else ""
            ),
            raw = g["trades"][0] if g["trades"] else {},
        ))

    signals.sort(key=lambda x: x.score, reverse=True)
    log.info("Insider: {} signals".format(len(signals)))
    return signals


def _score(value, intent, has_csuite, n_insiders) -> int:
    s = 20
    if value >= 1_000_000:  s += 30
    elif value >= 500_000:  s += 20
    elif value >= 100_000:  s += 12
    elif value >= 50_000:   s += 6
    elif value >= 10_000:   s += 2
    if has_csuite:          s += 20
    if n_insiders >= 3:     s += 15
    elif n_insiders >= 2:   s += 8
    if intent == "bearish": s = int(s * 0.6)   # selling less reliable
    return max(0, min(100, s))
