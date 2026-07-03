"""
schwab_auth_auto.py -- Selenium-automated Schwab OAuth re-authentication

Uses YOUR EXISTING CHROME PROFILE (saved login + cookies) so no credentials
are stored anywhere in this script or in .env. If Chrome already has your
Schwab session/cookies saved and "stay signed in" enabled, this completes
the OAuth flow with zero typing.

WHAT IT DOES:
  1. Opens a Chrome window using your real Chrome user profile
     (so saved passwords/cookies apply).
  2. Navigates to the Schwab OAuth authorization URL.
  3. If Schwab auto-logs you in via saved session/cookies, it proceeds.
     If manual login/MFA is needed, complete it in the visible window --
     the script waits up to 3 minutes.
  4. Clicks "Allow" / "Accept" on the consent screen automatically.
  5. Captures the redirect URL, exchanges it for a Schwab token, and
     saves to schwab_token.json -- same file main.py reads.

REQUIREMENTS:
    pip install selenium --break-system-packages
    Chrome + matching chromedriver (selenium 4.6+ auto-manages this)

CHROME PROFILE PATH (Windows):
    Open Chrome -> chrome://version -> "Profile Path", e.g.
    C:\\Users\\parth\\AppData\\Local\\Google\\Chrome\\User Data\\Default

    In .env set (WITHOUT the trailing \\Default):
        CHROME_USER_DATA_DIR=C:\\Users\\parth\\AppData\\Local\\Google\\Chrome\\User Data
        CHROME_PROFILE_DIR=Default

USAGE:
    Close ALL other Chrome windows first (profile lock), then:
    python schwab_auth_auto.py
"""
import os
import sys
import time
import json
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env")
except ImportError:
    pass

APP_KEY      = os.getenv("SCHWAB_APP_KEY", "")
APP_SECRET   = os.getenv("SCHWAB_APP_SECRET", "")
CALLBACK_URL = os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
TOKEN_PATH   = os.getenv("SCHWAB_TOKEN_PATH", "schwab_token.json")

CHROME_USER_DATA_DIR = os.getenv("CHROME_USER_DATA_DIR", "")
CHROME_PROFILE_DIR   = os.getenv("CHROME_PROFILE_DIR", "Default")

TOTAL_WAIT_TIMEOUT = 180  # seconds total to wait for redirect to CALLBACK_URL


def _write_token(token, *args, **kwargs):
    """Write the token dict to TOKEN_PATH in schwab-py's expected format."""
    with open(TOKEN_PATH, "w") as f:
        json.dump(token, f)


def main():
    if not APP_KEY or not APP_SECRET:
        print("[ERR] SCHWAB_APP_KEY / SCHWAB_APP_SECRET not set in .env")
        sys.exit(1)

    if not CHROME_USER_DATA_DIR:
        print("[ERR] CHROME_USER_DATA_DIR not set in .env")
        print("")
        print("To find it:")
        print("  1. Open Chrome -> go to chrome://version")
        print("  2. Copy the 'Profile Path' value, e.g.")
        print(r"     C:\Users\parth\AppData\Local\Google\Chrome\User Data\Default")
        print("  3. In .env add (WITHOUT the trailing \\Default):")
        print(r"     CHROME_USER_DATA_DIR=C:\Users\parth\AppData\Local\Google\Chrome\User Data")
        print(r"     CHROME_PROFILE_DIR=Default")
        sys.exit(1)

    import schwab.auth as schwab_auth
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By

    print("")
    print("Schwab Auto Re-Auth (Selenium)")
    print("-" * 40)
    print("  Chrome profile: {}\\{}".format(CHROME_USER_DATA_DIR, CHROME_PROFILE_DIR))
    print("  Token path    : {}".format(TOKEN_PATH))
    print("")
    print("IMPORTANT: Close ALL other Chrome windows now (profile lock).")
    print("Press Enter when ready...")
    input()

    # Build the Schwab OAuth authorization URL -- single use, tied to 'state'
    auth_context = schwab_auth.get_auth_context(APP_KEY, CALLBACK_URL)
    auth_url = auth_context.authorization_url

    # Remove Chrome's lock files for this profile -- if Chrome was closed
    # improperly (or is still running), these stale locks cause
    # "DevToolsActivePort file doesn't exist" failures.
    for lockname in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lockpath = Path(CHROME_USER_DATA_DIR) / lockname
        try:
            if lockpath.exists():
                lockpath.unlink()
                print("  Removed stale lock: {}".format(lockpath))
        except Exception as e:
            print("  Could not remove {}: {}".format(lockpath, e))

    opts = Options()
    opts.add_argument(r"--user-data-dir={}".format(CHROME_USER_DATA_DIR))
    opts.add_argument(r"--profile-directory={}".format(CHROME_PROFILE_DIR))
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--remote-debugging-port=0")  # let Chrome pick a free port
    opts.add_experimental_option("detach", True)  # keep window open after script ends

    try:
        driver = webdriver.Chrome(options=opts)
    except Exception as e:
        print("")
        print("[ERR] Failed to start Chrome: {}".format(e))
        print("")
        print("Most common causes:")
        print("  1. Chrome (or chromedriver from a previous run) is still")
        print("     running. Open Task Manager and end ALL 'Google Chrome'")
        print("     and 'chromedriver' processes, then re-run this script.")
        print("  2. Antivirus blocking chromedriver -- check Windows Defender")
        print("     exclusions.")
        sys.exit(1)

    print("Opening Schwab login...")
    driver.get(auth_url)
    print("Waiting for login + consent (up to {} min)...".format(
        TOTAL_WAIT_TIMEOUT // 60))
    print("If a manual login / 2FA prompt appears, complete it in the window.")

    deadline = time.time() + TOTAL_WAIT_TIMEOUT
    received_url = None

    while time.time() < deadline:
        current = driver.current_url

        if current.startswith(CALLBACK_URL):
            received_url = current
            break

        # Auto-click common consent buttons if present
        for text in ["Allow", "Accept", "Continue", "Submit", "I Agree"]:
            try:
                btns = driver.find_elements(
                    By.XPATH,
                    "//button[contains(., '{}')] | "
                    "//input[@type='submit' and contains(@value, '{}')]".format(
                        text, text)
                )
                for b in btns:
                    if b.is_displayed() and b.is_enabled():
                        b.click()
                        print("  Clicked '{}'".format(text))
                        time.sleep(1.5)
            except Exception:
                pass

        time.sleep(1)

    if not received_url and driver.current_url.startswith(CALLBACK_URL):
        received_url = driver.current_url

    if not received_url:
        print("")
        print("[ERR] Timed out waiting for redirect to {}".format(CALLBACK_URL))
        print("Current URL: {}".format(driver.current_url))
        print("Browser window left open -- complete login manually, then")
        print("re-run this script (the session will be remembered).")
        sys.exit(1)

    print("")
    print("[OK] Captured redirect URL")

    # Exchange code for token, write directly to TOKEN_PATH
    schwab_auth.client_from_received_url(
        api_key=APP_KEY,
        app_secret=APP_SECRET,
        auth_context=auth_context,
        received_url=received_url,
        token_write_func=_write_token,
    )

    print("[OK] Token saved to: {}".format(TOKEN_PATH))
    print("[OK] Re-authentication successful!")
    print("")
    print("Restart the bot to use the fresh token.")


if __name__ == "__main__":
    main()
