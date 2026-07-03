"""
schwab_auth.py -- Run this instead of broker/schwab.py --auth
Avoids the naming conflict between broker/schwab.py and the schwab-py package.

Usage:
    python schwab_auth.py
"""
import sys
import os
from pathlib import Path

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env")
except ImportError:
    pass

APP_KEY      = os.getenv("SCHWAB_APP_KEY", "")
APP_SECRET   = os.getenv("SCHWAB_APP_SECRET", "")
CALLBACK_URL = os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
TOKEN_PATH   = os.getenv("SCHWAB_TOKEN_PATH", "schwab_token.json")

print("")
print("Schwab OAuth Authentication")
print("-" * 40)
print("  App Key   : {}...".format(APP_KEY[:8] if APP_KEY else "NOT SET"))
print("  Callback  : {}".format(CALLBACK_URL))
print("  Token     : {}".format(TOKEN_PATH))
print("")

if not APP_KEY or not APP_SECRET:
    print("[ERR] SCHWAB_APP_KEY and SCHWAB_APP_SECRET must be set in .env")
    sys.exit(1)

try:
    import schwab
    print("schwab-py version: {}".format(
        getattr(schwab, "__version__", "unknown")))
    print("schwab-py path: {}".format(schwab.__file__))
    print("")
except ImportError:
    print("[ERR] schwab-py not installed. Run: pip install schwab-py")
    sys.exit(1)

try:
    from schwab import auth as schwab_auth
    print("Starting OAuth flow...")
    print("A browser window will open.")
    print("Log in to Schwab, approve the app.")
    print("After redirect, paste the full callback URL here.")
    print("")

    client = schwab_auth.client_from_manual_flow(
        api_key      = APP_KEY,
        app_secret   = APP_SECRET,
        callback_url = CALLBACK_URL,
        token_path   = TOKEN_PATH,
    )

    print("")
    print("[OK] Authentication successful!")
    print("[OK] Token saved to: {}".format(TOKEN_PATH))
    print("")
    print("Now restart the bot -- Schwab quotes will be active.")
    print("")

except Exception as e:
    print("[ERR] {}".format(e))
    print("")
    print("Redirect URL format: {}?code=...&session=...".format(CALLBACK_URL))
    sys.exit(1)
