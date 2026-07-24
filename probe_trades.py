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
        try:
            r = client.get_transactions(h, start_date=start, end_date=end)
        except TypeError:
            r = client.get_transactions(h, start, end)
        if r.status_code == 200:
            txns_a = r.json()
            print("  account {}: {} txns (180d, unfiltered)".format(
                a.get("accountNumber", "?"), len(txns_a)))
            all_txns.extend(txns_a)
        else:
            print("  account {}: HTTP {} {}".format(
                a.get("accountNumber", "?"), r.status_code, r.text[:120]))

    txns = all_txns
    print("\nTotal transactions across all accounts (180d):", len(txns), "\n")
    if not txns:
        print("Still empty. Either these accounts have no trade history,")
        print("or trading happens in an account this token can't see.")
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