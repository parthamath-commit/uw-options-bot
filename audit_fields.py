#!/usr/bin/env python3
"""
audit_fields.py  --  Field-mismatch auditor for UW Options Bot.

For every endpoint a feed reads from, this fetches ONE live sample and compares
the field names the code tries to read against the field names UW actually
returns. Any field the code reads that UW does NOT send is a silent-default bug
(the exact class as the ask_side bug).

Run on the VM:
    cd ~/uw-options-bot && source venv/bin/activate && python audit_fields.py

Read-only: only GETs one small sample per endpoint. Changes nothing.
"""
import sys
from data.uw_client import UWClient

# What each feed reads, keyed by the endpoint it reads from.
# (field, is_fallback_chain) -- fallback means code has `.get(a) or .get(b)`,
# so it's OK if only ONE of the alternatives is present.
EXPECTATIONS = {
    "/api/option-trades/flow-alerts": {
        "params": {"limit": 3},
        "reads": ["ticker", "underlying_symbol", "type", "alert_rule", "has_sweep",
                  "total_premium", "total_ask_side_prem", "total_bid_side_prem",
                  "volume", "open_interest", "iv_start", "strike",
                  "volume_oi_ratio", "expiry", "has_floor", "id"],
        "fallback_groups": [["ticker", "underlying_symbol"]],
    },
    "/api/screener/option-contracts": {
        "via": "get_hottest_chains",
        "reads": ["ticker_symbol", "ticker", "option_symbol", "symbol",
                  "type", "strike", "strike_price", "expiry", "expiration_date",
                  "total_premium", "premium", "volume", "open_interest",
                  "implied_volatility", "iv",
                  "ask_side_pct", "min_ask_perc",           # <-- KNOWN BAD
                  "ask_side_volume", "bid_side_volume"],    # <-- real keys
        "fallback_groups": [["ticker_symbol", "ticker"], ["option_symbol", "symbol"],
                            ["strike", "strike_price"], ["expiry", "expiration_date"],
                            ["total_premium", "premium"], ["implied_volatility", "iv"],
                            ["ask_side_pct", "min_ask_perc"]],
    },
    "/api/darkpool/recent": {
        "params": {"limit": 3},
        "reads": ["ticker", "symbol", "price", "size", "volume"],
        "fallback_groups": [["ticker", "symbol"], ["size", "volume"]],
    },
    "/api/lit-flow/recent": {
        "params": {"limit": 3},
        "reads": ["ticker", "symbol", "size", "volume", "premium", "price",
                  "nbbo_ask", "nbbo_bid", "canceled"],
        "fallback_groups": [["ticker", "symbol"], ["size", "volume"]],
    },
    "/api/congress/recent-trades": {
        "params": {"limit": 3},
        "reads": ["ticker", "symbol", "amount", "value", "transaction_amount",
                  "transaction_type", "txn_type", "type", "transaction",
                  "trade_date", "transaction_date", "disclosure_date", "filed_at_date",
                  "name", "first_name", "last_name", "politician", "reporter",
                  "representative", "senator", "member_type", "asset_type",
                  "security_type", "notes"],
        "fallback_groups": [["ticker", "symbol"],
                            ["amount", "value", "transaction_amount"],
                            ["transaction_type", "txn_type", "type"]],
    },
    "/api/insider/transactions": {
        "params": {"limit": 3},
        "reads": ["ticker", "symbol", "amount", "value", "transaction_amount",
                  "transaction_code", "transaction_type", "type",
                  "officer_title", "title", "relationship",
                  "owner_name", "first_name", "last_name",
                  "is_director", "is_officer", "security_title"],
        "fallback_groups": [["ticker", "symbol"],
                            ["amount", "value", "transaction_amount"],
                            ["officer_title", "title", "relationship"]],
    },
    "/api/market/oi-change": {
        "params": {"limit": 3},
        "reads": ["ticker", "symbol", "underlying_symbol", "option_symbol",
                  "oi_change", "oi_diff_plain", "volume"],
        "fallback_groups": [["ticker", "symbol", "underlying_symbol"],
                            ["oi_change", "oi_diff_plain"]],
    },
    "/api/market/top-net-impact": {
        "params": {"limit": 3},
        "reads": ["ticker", "symbol", "net_impact", "net_delta_impact",
                  "net_premium"],
        "fallback_groups": [["ticker", "symbol"]],
    },
    "/api/news/headlines": {
        "params": {"limit": 3},
        "reads": ["headline", "title", "created_at", "published_at",
                  "source", "publisher", "sentiment", "tickers", "ticker",
                  "symbol", "is_major"],
        "fallback_groups": [["headline", "title"],
                            ["created_at", "published_at"],
                            ["source", "publisher"]],
    },
}


def as_list(raw):
    if isinstance(raw, dict):
        for k in ("data", "results", "chains", "flow_alerts"):
            if isinstance(raw.get(k), list):
                return raw[k]
        return [raw]
    return raw if isinstance(raw, list) else []


def audit():
    c = UWClient()
    print("=" * 72)
    print("UW FIELD AUDIT  --  code-expected fields vs. actual API response")
    print("=" * 72)

    total_bad = 0
    for ep, spec in EXPECTATIONS.items():
        print("\n" + "-" * 72)
        print("ENDPOINT:", ep)
        try:
            if spec.get("via") == "get_hottest_chains":
                sample = c.get_hottest_chains(min_premium=50000, limit=3)
            else:
                raw = c._get(ep, spec.get("params", {}))
                sample = as_list(raw)
        except Exception as e:
            print("  !! FETCH FAILED:", repr(e))
            continue

        if not sample:
            print("  (empty response -- endpoint returned no rows; can't audit)")
            continue

        actual = set()
        for row in sample[:3]:
            if isinstance(row, dict):
                actual |= set(row.keys())

        # group membership: a field in a fallback group only needs ONE present
        group_of = {}
        for grp in spec.get("fallback_groups", []):
            for f in grp:
                group_of[f] = grp

        missing_solo = []      # read, not in a fallback group, absent -> BUG
        dead_groups = []       # entire fallback group absent -> BUG
        checked_groups = set()

        for f in spec["reads"]:
            if f in group_of:
                grp = tuple(group_of[f])
                if grp in checked_groups:
                    continue
                checked_groups.add(grp)
                if not any(g in actual for g in grp):
                    dead_groups.append(list(grp))
            else:
                if f not in actual:
                    missing_solo.append(f)

        if not missing_solo and not dead_groups:
            print("  OK -- every field the code reads is present.")
        else:
            for f in missing_solo:
                print("  BUG  code reads '{}' -- NOT in API response".format(f))
                total_bad += 1
            for grp in dead_groups:
                print("  BUG  code reads {} -- NONE present in API response".format(grp))
                total_bad += 1

        # show what UW actually sends, so you can spot the real key name
        print("  API sends:", ", ".join(sorted(actual)))

    print("\n" + "=" * 72)
    print("TOTAL field-mismatch bugs found:", total_bad)
    print("=" * 72)


if __name__ == "__main__":
    audit()