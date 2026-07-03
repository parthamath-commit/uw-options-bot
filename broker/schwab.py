"""
broker/schwab.py
================
Schwab API fallback broker -- Pal Initiatives LLC.

Token lifecycle (Schwab policy -- cannot be changed):
  Access token  : 30 minutes  -- auto-refreshed by schwab-py invisibly
  Refresh token : 7 days      -- rolls forward on every access token refresh

Key rule: as long as the bot makes at least ONE Schwab API call every 7 days,
the refresh token keeps rolling and you never need to re-authenticate.
If the bot is stopped for more than 7 days, run:
  python broker/schwab.py --auth
to re-authenticate via browser (one-time, ~2 min).

Setup:
  1. Register free app at developer.schwab.com
     - App Name: UW Options Bot
     - Callback URL: https://127.0.0.1
  2. Add to .env:
       USE_SCHWAB_FALLBACK=true
       SCHWAB_APP_KEY=your_app_key
       SCHWAB_APP_SECRET=your_app_secret
       SCHWAB_TOKEN_PATH=schwab_token.json
       SCHWAB_CALLBACK_URL=https://127.0.0.1
  3. Run once: python broker/schwab.py --auth
  4. Never needs to be done again as long as bot runs weekly
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is on path when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import cfg

log = logging.getLogger("UWBot.Schwab")

# Schwab-specific config
SCHWAB_APP_KEY      = os.getenv("SCHWAB_APP_KEY", "")
SCHWAB_APP_SECRET   = os.getenv("SCHWAB_APP_SECRET", "")
SCHWAB_TOKEN_PATH   = os.getenv("SCHWAB_TOKEN_PATH", "schwab_token.json")
SCHWAB_CALLBACK_URL = os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1")
USE_SCHWAB_FALLBACK = os.getenv("USE_SCHWAB_FALLBACK", "false").lower() == "true"

# Warn at 5 days so user has 2 days buffer before 7-day expiry
TOKEN_WARNING_DAYS = 5

try:
    # Detect schwab-py by checking if the schwab package has an 'auth' submodule
    # which our broker/schwab.py does not have
    import importlib.util as _ilu
    _auth_spec = _ilu.find_spec("schwab.auth")
    SCHWAB_AVAILABLE = _auth_spec is not None
except Exception:
    SCHWAB_AVAILABLE = False


def _get_token_age_days(token_path: str) -> float:
    """
    Return age of refresh token in days by reading the token file.
    schwab-py stores token as JSON with 'creation_timestamp' or file mtime.
    """
    path = Path(token_path)
    if not path.exists():
        return 999.0
    try:
        with open(path) as f:
            data = json.load(f)
        # schwab-py stores expires_at for access token
        # Use file modification time as proxy for last refresh
        mtime = path.stat().st_mtime
        age = (datetime.now(timezone.utc).timestamp() - mtime) / 86400
        return round(age, 1)
    except Exception:
        return 0.0


class SchwabBroker:
    """
    Schwab API fallback for live option quotes.
    Activated when IBKR options data is unavailable (e.g. pending approval).

    Quote waterfall in scanner.py:
      1. IBKR   -- primary (needs options market data subscription)
      2. Schwab -- fallback (free, real-time, no subscription needed)
      3. UW     -- last resort

    Token auto-refreshes every 30 min invisibly.
    Re-auth needed only if bot stopped for 7+ days.
    """

    def __init__(self):
        self._client = None
        self._available = False

        if not USE_SCHWAB_FALLBACK:
            log.debug("Schwab fallback disabled (USE_SCHWAB_FALLBACK=false)")
            return
        if not SCHWAB_AVAILABLE:
            log.warning("schwab-py not installed. Run: pip install schwab-py")
            return
        if not SCHWAB_APP_KEY or not SCHWAB_APP_SECRET:
            log.warning("SCHWAB_APP_KEY / SCHWAB_APP_SECRET not set in .env")
            return

        token_path = Path(SCHWAB_TOKEN_PATH)
        if not token_path.exists():
            log.warning(
                "Schwab token not found: {}\n"
                "Run: python broker/schwab.py --auth".format(token_path)
            )
            return

        # Warn if token is getting old
        age = _get_token_age_days(SCHWAB_TOKEN_PATH)
        if age >= TOKEN_WARNING_DAYS:
            log.warning(
                "Schwab token is {:.1f} days old -- expires at 7 days. "
                "Run: python broker/schwab.py --auth to refresh before it expires.".format(age)
            )
        else:
            log.debug("Schwab token age: {:.1f} days (expires at 7)".format(age))

        try:
            # Load real schwab-py via __init__.py to avoid naming conflict
            import importlib.util as _ilu
            _s = _ilu.find_spec("schwab")
            _ldr = _ilu.spec_from_file_location("_schwab_real", _s.origin)
            _m = _ilu.module_from_spec(_ldr)
            _ldr.loader.exec_module(_m)
            self._client = _m.auth.client_from_token_file(
                token_path=str(token_path),
                api_key=SCHWAB_APP_KEY,
                app_secret=SCHWAB_APP_SECRET,
            )
            self._available = True
            log.info("Schwab fallback ready (token age: {:.1f}d)".format(age))
        except Exception as e:
            log.error("Schwab auth failed: {}".format(e))
            if "invalid_client" in str(e).lower() or "expired" in str(e).lower():
                log.error(
                    "Schwab refresh token has expired (7-day limit reached).\n"
                    "Run: python broker/schwab.py --auth to re-authenticate."
                )

    @property
    def available(self) -> bool:
        return self._available and self._client is not None

    def get_option_quote(self, symbol, expiry, strike, right):
        """
        Fetch real-time bid/ask + greeks from Schwab.
        expiry: YYYY-MM-DD
        right: C or P
        Returns same dict shape as IBKRClient for drop-in compatibility.
        """
        if not self.available:
            return {}
        try:
            from datetime import date
            contract_type = "CALL" if right == "C" else "PUT"
            exp_date = date.fromisoformat(expiry)

            resp = self._client.get_option_chain(
                symbol=symbol,
                contract_type=contract_type,
                strike=strike,
                from_date=exp_date,
                to_date=exp_date,
                include_underlying_quote=True,
            )
            resp.raise_for_status()
            chain = resp.json()

            underlying_px = float(
                chain.get("underlyingPrice") or
                chain.get("underlying", {}).get("last", 0) or 0
            )

            side_key = "callExpDateMap" if right == "C" else "putExpDateMap"
            for exp_key, strikes in chain.get(side_key, {}).items():
                if expiry in exp_key:
                    for strike_key, contracts in strikes.items():
                        try:
                            s = float(strike_key.split(":")[0])
                        except Exception:
                            continue
                        if abs(s - strike) < 0.01 and contracts:
                            c = contracts[0]
                            return {
                                "bid":           float(c.get("bid")   or 0),
                                "ask":           float(c.get("ask")   or 0),
                                "last":          float(c.get("last")  or 0),
                                "delta":         float(c.get("delta") or 0),
                                "gamma":         float(c.get("gamma") or 0),
                                "vega":          float(c.get("vega")  or 0),
                                "theta":         float(c.get("theta") or 0),
                                "iv":            float(c.get("volatility") or 0) / 100,
                                "underlying_px": underlying_px,
                            }
            return {}

        except Exception as e:
            # Handle expired token gracefully
            if "invalid_client" in str(e).lower() or "401" in str(e):
                log.error(
                    "Schwab token expired. Run: python broker/schwab.py --auth"
                )
                self._available = False
            else:
                log.error("Schwab quote error {} {}{}: {}".format(
                    symbol, strike, right, e))
            return {}

    def get_stock_quote(self, symbol):
        if not self.available:
            return {}
        try:
            resp = self._client.get_quote(symbol)
            resp.raise_for_status()
            data  = resp.json()
            quote = data.get(symbol, {}).get("quote", {})
            return {
                "symbol": symbol,
                "bid":    float(quote.get("bidPrice")  or 0),
                "ask":    float(quote.get("askPrice")  or 0),
                "last":   float(quote.get("lastPrice") or 0),
            }
        except Exception as e:
            log.error("Schwab stock quote error {}: {}".format(symbol, e))
            return {}

    def get_account_value(self, account_number=""):
        if not self.available:
            return 0.0
        try:
            resp = self._client.get_account_numbers()
            resp.raise_for_status()
            accounts = resp.json()
            target_hash = None
            for a in accounts:
                if not account_number or a.get("accountNumber") == account_number:
                    target_hash = a.get("hashValue")
                    break
            if not target_hash:
                return 0.0
            resp2 = self._client.get_account(target_hash, fields=["positions"])
            resp2.raise_for_status()
            acct = resp2.json()
            nlv = (
                acct.get("securitiesAccount", {})
                    .get("currentBalances", {})
                    .get("liquidationValue", 0)
            )
            return float(nlv or 0)
        except Exception as e:
            log.error("Schwab account value error: {}".format(e))
            return 0.0


# ── Token age checker (called from scanner every cycle) ───────────────────────
def check_token_expiry():
    """
    Called at the start of each scan cycle.
    Logs a warning when token age approaches 7-day limit.
    Returns days remaining (negative = expired).
    """
    if not USE_SCHWAB_FALLBACK:
        return 7.0
    age  = _get_token_age_days(SCHWAB_TOKEN_PATH)
    remaining = 7.0 - age
    if remaining <= 0:
        log.error(
            "Schwab refresh token EXPIRED. "
            "Run: python broker/schwab.py --auth to re-authenticate."
        )
    elif remaining <= 2:
        log.warning(
            "Schwab token expires in {:.1f} days. "
            "Run: python broker/schwab.py --auth soon.".format(remaining)
        )
    return remaining


# ── One-time auth CLI ─────────────────────────────────────────────────────────
def run_auth():
    if not SCHWAB_AVAILABLE:
        print("ERROR: schwab-py not installed. Run: pip install schwab-py")
        sys.exit(1)
    if not SCHWAB_APP_KEY or not SCHWAB_APP_SECRET:
        print("ERROR: SCHWAB_APP_KEY and SCHWAB_APP_SECRET must be set in .env")
        sys.exit(1)

    # Delete old token so fresh one is created
    token_path = Path(SCHWAB_TOKEN_PATH)
    if token_path.exists():
        token_path.unlink()
        print("Old token deleted.")

    print("")
    print("Starting Schwab OAuth flow...")
    print("  App Key    : {}...".format(SCHWAB_APP_KEY[:8]))
    print("  Token path : {}".format(SCHWAB_TOKEN_PATH))
    print("  Callback   : {}".format(SCHWAB_CALLBACK_URL))
    print("")
    print("A browser window will open.")
    print("Log in to Schwab and authorise the app.")
    print("After redirect, paste the full callback URL here.")
    print("")

    try:
        # Load real schwab package via its __init__.py path to avoid naming conflict
        import importlib.util, sys
        spec = importlib.util.find_spec("schwab")
        if spec is None or not spec.origin or "__init__" not in spec.origin:
            raise ImportError("schwab-py package not found. Run: pip install schwab-py")
        loader = importlib.util.spec_from_file_location(
            "schwab_real", spec.origin)
        schwab_mod = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(schwab_mod)
        schwab_auth = schwab_mod.auth

        print("Opening browser for Schwab login...")
        print("Log in, approve the app, then copy the full redirect URL")
        print("and paste it below when prompted.")
        print("")

        client = schwab_auth.client_from_manual_flow(
            api_key      = SCHWAB_APP_KEY,
            app_secret   = SCHWAB_APP_SECRET,
            callback_url = SCHWAB_CALLBACK_URL,
            token_path   = SCHWAB_TOKEN_PATH,
        )

        print("")
        print("[OK] Token saved to: {}".format(SCHWAB_TOKEN_PATH))
        print("[OK] Token will auto-refresh every 30 min.")
        print("[OK] Re-auth needed only if bot is stopped for 7+ days.")
        print("")
        print("Schwab fallback is ready. Restart the bot.")
        print("")

    except Exception as e:
        print("\n[ERR] Auth failed: {}".format(e))
        print("")
        print("The redirect URL should look like:")
        print("  https://127.0.0.1?code=...&session=...")
        print("")
        print("Make sure SCHWAB_APP_KEY and SCHWAB_APP_SECRET are set in .env")
        print("and the callback URL in your Schwab app matches: {}".format(
            SCHWAB_CALLBACK_URL))
        sys.exit(1)


if __name__ == "__main__":
    import argparse
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")

    parser = argparse.ArgumentParser(description="Schwab broker utility")
    parser.add_argument("--auth", action="store_true",
                        help="Run OAuth token generation (browser login)")
    parser.add_argument("--age", action="store_true",
                        help="Show current token age")
    parser.add_argument("--test", metavar="SYMBOL",
                        help="Test quote for SYMBOL e.g. NVDA")
    args = parser.parse_args()

    if args.auth:
        run_auth()
    elif args.age:
        age = _get_token_age_days(SCHWAB_TOKEN_PATH)
        remaining = 7.0 - age
        print("Token age    : {:.1f} days".format(age))
        print("Days left    : {:.1f} days".format(remaining))
        if remaining <= 2:
            print("WARNING: Re-auth soon -- run: python broker/schwab.py --auth")
        elif remaining <= 0:
            print("EXPIRED -- run: python broker/schwab.py --auth")
        else:
            print("Status: OK")
    elif args.test:
        broker = SchwabBroker()
        if broker.available:
            q = broker.get_stock_quote(args.test)
            print("{}: {}".format(args.test, q))
        else:
            print("Schwab not available -- check .env and run --auth first.")
    else:
        parser.print_help()
