#!/usr/bin/env python3
"""
probe_trades.py -- READ-ONLY probe of your real Schwab transaction history.

Pulls a small sample of executed trades so we can see the exact field names
and structure BEFORE writing the P&L analysis. Changes nothing, writes nothing.

Run:
    cd ~/uw-options-bot && source venv/bin/activate && python probe_trades.py
"""
import os
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env")
except ImportError:
    pass

TOKEN_PATH = os.getenv("SCHWAB_TOKEN_PATH", "schwab_token.json")
APP_KEY    = os.getenv("SCHWAB_APP_KEY", "")
APP_SECRET = os.getenv("SCHWAB_APP_SECRET", "")


def load_schwab():
    """Reuse the working loader pattern (registers module before exec)."""
    import importlib.util as ilu
    ALIAS = "_schwab_real"
    spec = ilu.find_spec("schwab")
    if spec is None or not spec.origin or "__init__" not in spec.origin:
        raise ImportError("schwab-py not found")
    ldr = ilu.spec_from_file_location(
        ALIAS, spec.origin,
        submodule_search_locations=spec.submodule_search_locations)
    mod = ilu.module_from_spec(ldr)
    sys.modules[ALIAS] = mod
    ldr.loader.exec_module(mod)
    client = mod.auth.client_from_token_file(
        token_path=TOKEN_PATH, api_key=APP_KEY, app_secret=APP_SECRET)
    return client, mod


def main():
    client, mod = load_schwab()
    print("Schwab client loaded.\n")

    # Resolve account hash
    resp = client.get_account_numbers()
    if resp.status_code != 200:
        print("get_account_numbers failed:", resp.status_code, resp.text[:200])
        return
    accounts = resp.json()
    print("Accounts found:", len(accounts))
    acct_hash = accounts[0].get("hashValue")
    print("Using first account hash:", acct_hash[:8], "...\n")

    # Pull last 180 days of transactions, NO type filter (catch everything)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=180)

    # Show ALL accounts, not just the first
    print("All account hashes:")
    for i, a in enumerate(accounts):
        print("  [{}] number={}  hash={}...".format(
            i, a.get("accountNumber", "?"), (a.get("hashValue") or "")[:8]))
    print()

    all_txns = []
    for a in accounts:
        h = a.get("hashValue")
        r = None
        # schwab-py requires transaction_types on this endpoint for many builds.
        # Try, in order: typed TRADE enum, then RECEIVE_AND_DELIVER+TRADE, then bare.
        attempts = []
        try:
            TT = client.Transactions.TransactionType
            attempts.append({"start_date": start, "end_date": end,
                             "transaction_types": [TT.TRADE]})
            attempts.append({"start_date": start, "end_date": end,
                             "transaction_types": list(TT)})
        except Exception as e:
            print("  (enum lookup failed: {})".format(e))
        attempts.append({"start_date": start, "end_date": end})

        for kw in attempts:
            try:
                r = client.get_transactions(h, **kw)
            except TypeError:
                try:
                    r = client.get_transactions(h, start, end)
                except Exception as e:
                    print("  call error:", e); continue
            except Exception as e:
                print("  call error:", e); continue
            if r is not None and r.status_code == 200 and r.json():
                break  # got data, stop trying variants

        if r is None:
            print("  account {}: no response".format(a.get("accountNumber", "?")))
            continue
        if r.status_code == 200:
            txns_a = r.json()
            print("  account {}: {} txns (180d)".format(
                a.get("accountNumber", "?"), len(txns_a)))
            all_txns.extend(txns_a)
        else:
            print("  account {}: HTTP {}  body: {}".format(
                a.get("accountNumber", "?"), r.status_code, r.text[:200]))

    txns = all_txns
    print("\nTotal transactions across all accounts (180d):", len(txns), "\n")

    # Positions check: if transactions are empty but positions exist,
    # it's a transaction-scope problem, not an empty account.
    print("=" * 60)
    print("CURRENT POSITIONS CHECK (distinguishes scope vs empty acct)")
    print("=" * 60)
    for a in accounts:
        h = a.get("hashValue")
        try:
            # This client build enforces enums; use the typed Fields enum,
            # falling back to no-fields (still returns positions on many builds).
            try:
                Fields = client.Account.Fields
                rp = client.get_account(h, fields=[Fields.POSITIONS])
            except Exception:
                rp = client.get_account(h)
            if rp.status_code == 200:
                acct = rp.json().get("securitiesAccount", {})
                positions = acct.get("positions", [])
                bal = acct.get("currentBalances", {}) or {}
                liq = bal.get("liquidationValue") or bal.get("cashBalance") or "?"
                print("  account {}: {} open positions, value~{}".format(
                    a.get("accountNumber", "?"), len(positions), liq))
                for p in positions[:8]:
                    instr = p.get("instrument", {})
                    print("      {} {}  qty={}".format(
                        instr.get("assetType", "?"),
                        instr.get("symbol", "?"),
                        p.get("longQuantity", 0) - p.get("shortQuantity", 0)))
            else:
                print("  account {}: positions HTTP {}".format(
                    a.get("accountNumber", "?"), rp.status_code))
        except Exception as e:
            print("  positions error:", e)
    print()

    if not txns:
        print("Transactions empty. If positions above are ALSO empty, the")
        print("account has no activity. If positions show holdings but")
        print("transactions are empty, the token lacks transaction-history")
        print("scope -- re-auth needed with that permission.")
        return

    # show the variety of transaction types present
    types_seen = {}
    for t in txns:
        ty = t.get("type") or t.get("transactionType") or "?"
        types_seen[ty] = types_seen.get(ty, 0) + 1
    print("Transaction types seen:", types_seen, "\n")

    # Show the structure of the first OPTION trade we can find
    def is_option(t):
        s = json.dumps(t).lower()
        return "option" in s

    opt = next((t for t in txns if is_option(t)), None)
    sample = opt or txns[0]

    print("=" * 70)
    print("SAMPLE TRANSACTION (option trade if found):" if opt
          else "SAMPLE TRANSACTION (no option found; showing first):")
    print("=" * 70)
    print(json.dumps(sample, indent=2, default=str)[:2500])
    print("\nTOP-LEVEL KEYS:", sorted(sample.keys()))

    # If there's a transferItems / positions array, show its shape too
    for key in ("transferItems", "positions", "orderLegCollection"):
        if key in sample and sample[key]:
            print("\n{} [0] KEYS:".format(key),
                  sorted(sample[key][0].keys())
                  if isinstance(sample[key][0], dict) else type(sample[key][0]))

    # Count how many look like options
    n_opt = sum(1 for t in txns if is_option(t))
    print("\nOf {} transactions, {} reference 'option'.".format(len(txns), n_opt))


if __name__ == "__main__":
    main()