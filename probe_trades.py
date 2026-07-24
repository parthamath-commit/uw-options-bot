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

    # Pull last 60 days of transactions
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=60)

    # schwab-py: get_transactions(account_hash, start_date, end_date, transaction_types)
    # transaction_types is an enum; TRADE covers executed buys/sells.
    try:
        TxType = client.Transactions.TransactionType
        tx_types = [TxType.TRADE]
    except Exception:
        tx_types = None  # let API default

    try:
        if tx_types:
            r = client.get_transactions(acct_hash, start_date=start,
                                        end_date=end, transaction_types=tx_types)
        else:
            r = client.get_transactions(acct_hash, start_date=start, end_date=end)
    except TypeError:
        # older/newer signature fallback
        r = client.get_transactions(acct_hash, start, end)

    if r.status_code != 200:
        print("get_transactions failed:", r.status_code, r.text[:300])
        return

    txns = r.json()
    print("Transactions returned (last 60d):", len(txns), "\n")
    if not txns:
        print("No transactions in window. Try widening the date range.")
        return

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