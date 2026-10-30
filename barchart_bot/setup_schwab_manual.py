"""
setup_schwab_manual.py
======================

Manual-paste OAuth flow for Schwab. Use this instead of --setup-schwab if the
auto-capture flow keeps timing out (the 30-second authorization-code window is
tight, and antivirus / 2FA / browser warnings can blow through it).

How it works:
  1. Script prints the Schwab authorization URL.
  2. You open it in your browser, log in, approve the app.
  3. Schwab redirects to https://127.0.0.1:8182/?code=XXXX&...
  4. The browser shows "this site can't be reached" - that's expected.
  5. Copy the WHOLE URL from the address bar.
  6. Paste it into this script when prompted.
  7. Script exchanges the code for tokens and saves to disk.

Run from C:\\BarchartBot:
    python setup_schwab_manual.py
"""

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from schwab.auth import client_from_manual_flow
except ImportError:
    print("ERROR: schwab-py not installed. Run: pip install schwab-py")
    sys.exit(1)


def main():
    app_key = os.getenv("SCHWAB_APP_KEY", "").strip()
    app_secret = os.getenv("SCHWAB_APP_SECRET", "").strip()
    callback_url = os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182").strip()
    token_path = os.getenv("SCHWAB_TOKEN_PATH", "schwab_token.json").strip()

    if not app_key or not app_secret:
        print("ERROR: SCHWAB_APP_KEY and SCHWAB_APP_SECRET must be set in .env")
        print("Current .env path being read from:", os.getcwd())
        sys.exit(1)

    print(f"App Key:      {app_key[:6]}...{app_key[-4:]}  (length {len(app_key)})")
    print(f"App Secret:   {'*' * 8}{app_secret[-4:]}  (length {len(app_secret)})")
    print(f"Callback URL: {callback_url}")
    print(f"Token path:   {token_path}")
    print()

    token_dir = os.path.dirname(token_path)
    if token_dir and not os.path.exists(token_dir):
        os.makedirs(token_dir, exist_ok=True)

    print("Starting MANUAL OAuth flow.")
    print()
    print("INSTRUCTIONS:")
    print("  1. The script will print a URL. Copy it.")
    print("  2. Open the URL in your browser.")
    print("  3. Log in to Schwab with your BROKERAGE account credentials")
    print("     (NOT your developer portal login).")
    print("  4. Click 'Allow' / 'Approve' on the consent page.")
    print("  5. Schwab will redirect to https://127.0.0.1:8182/?code=...")
    print("  6. Browser will show 'This site can't be reached' - this is FINE.")
    print("  7. Copy the FULL URL from your browser's address bar.")
    print("  8. Paste it here when prompted.")
    print()

    try:
        client = client_from_manual_flow(
            api_key=app_key,
            app_secret=app_secret,
            callback_url=callback_url,
            token_path=token_path,
        )
        print()
        print(f"SUCCESS: Token saved to {token_path}")
        print()

        # Quick test fetch.
        from schwab.client import Client as SchwabClient
        print("Testing with SPY 1m bars...")
        response = client.get_price_history(
            "SPY",
            period_type=SchwabClient.PriceHistory.PeriodType.DAY,
            period=SchwabClient.PriceHistory.Period.ONE_DAY,
            frequency_type=SchwabClient.PriceHistory.FrequencyType.MINUTE,
            frequency=SchwabClient.PriceHistory.Frequency.EVERY_MINUTE,
            need_extended_hours_data=False,
        )
        if response.status_code == 200:
            candles = response.json().get("candles", [])
            print(f"Got {len(candles)} bars. Latest close: {candles[-1].get('close') if candles else 'n/a'}")
            print()
            print("Schwab integration is ready. You can now run the bot normally:")
            print("    python barchart_pro_bot.py")
        else:
            print(f"WARNING: Test fetch returned status {response.status_code}")
            print(f"Response: {response.text[:300]}")
            print()
            print("Token was saved, but the API may need market hours or your")
            print("subscriptions may need a few minutes to propagate. Try the bot now.")

    except Exception as e:
        print(f"ERROR: {e}")
        print()
        print("Common causes:")
        print("  - Pasted URL was incomplete (must include the full ?code=... query string)")
        print("  - Authorization code already expired (took >30s between redirect and paste)")
        print("    -> just run this script again, faster this time")
        print("  - SCHWAB_APP_KEY or SCHWAB_APP_SECRET don't match the app you created")
        print("  - Callback URL in .env doesn't match the one in the Schwab developer portal")
        sys.exit(1)


if __name__ == "__main__":
    main()
