#!/usr/bin/env python3
"""
check_schwab_token.py -- Schwab refresh-token expiry alerts via Telegram.
Pal Initiatives LLC -- UW Options Bot companion script.

Schwab policy: the refresh token lives exactly 7 days from the last full
OAuth login (creation_timestamp in schwab_token.json). schwab-py preserves
creation_timestamp across silent access-token refreshes, so
    expiry = creation_timestamp + 7 days.

Alerts sent (each once per token generation, deduped via state file):
    - 24h warning   ("re-auth today")
    - 4h warning    ("last call")
    - expired       ("bot is running on fallback data")

Install (cron, every 4 hours):
    crontab -e
    0 */4 * * * /home/ubuntu/uw-options-bot/venv/bin/python3 /home/ubuntu/uw-options-bot/check_schwab_token.py >> /home/ubuntu/uw-options-bot/output/token_check.log 2>&1

Test manually:
    python3 check_schwab_token.py --status   (prints status, sends nothing)
    python3 check_schwab_token.py --test     (sends a test Telegram message)
"""

import json
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

BOT_DIR      = Path(__file__).resolve().parent
TOKEN_PATH   = BOT_DIR / "schwab_token.json"
ENV_PATH     = BOT_DIR / ".env"
STATE_PATH   = BOT_DIR / "output" / "token_alert_state.json"

TOKEN_LIFETIME_SEC = 7 * 24 * 3600      # Schwab refresh token: 7 days, fixed
WARN_24H_SEC       = 24 * 3600
WARN_4H_SEC        = 4 * 3600

REAUTH_STEPS = (
    "Re-auth (2 min):\n"
    "1. ssh -i oracle_bot.key ubuntu@129.80.159.101\n"
    "2. cd ~/uw-options-bot && source venv/bin/activate\n"
    "3. python3 schwab_auth.py   (browser login, paste redirect URL)\n"
    "4. sudo systemctl restart uwbot"
)


def load_env(path: Path) -> dict:
    """Minimal .env parser -- no dependency on python-dotenv."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def send_telegram(env: dict, text: str) -> bool:
    token   = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[token-check] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env")
        return False
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=15) as resp:
            ok = resp.status == 200
            if not ok:
                print(f"[token-check] Telegram HTTP {resp.status}")
            return ok
    except Exception as e:
        print(f"[token-check] Telegram send failed: {e}")
        return False


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception as e:
        print(f"[token-check] Could not save state: {e}")


def token_expiry_info() -> dict:
    """Returns {created_at, expires_at, seconds_left} or {error}."""
    if not TOKEN_PATH.exists():
        return {"error": "token file missing"}
    try:
        data = json.loads(TOKEN_PATH.read_text())
        created = float(data["creation_timestamp"])
    except Exception as e:
        return {"error": f"token file unreadable: {e}"}

    expires_at = created + TOKEN_LIFETIME_SEC
    now        = datetime.now(timezone.utc).timestamp()
    return {
        "created_at":   created,
        "expires_at":   expires_at,
        "seconds_left": expires_at - now,
    }


def fmt_et(ts: float) -> str:
    """Format a unix timestamp in ET without external tz packages."""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(ts, ZoneInfo("America/New_York"))
        return dt.strftime("%a %b %d, %I:%M %p ET")
    except Exception:
        dt = datetime.fromtimestamp(ts, timezone.utc)
        return dt.strftime("%a %b %d, %H:%M UTC")


def main() -> int:
    env  = load_env(ENV_PATH)
    info = token_expiry_info()

    if "--test" in sys.argv:
        ok = send_telegram(env, "Schwab token monitor: test alert OK "
                                "(cron wiring works).")
        print("[token-check] test alert sent" if ok else "[token-check] test alert FAILED")
        return 0 if ok else 1

    if "error" in info:
        # Token file gone/corrupt -- that's alert-worthy in itself (once per day)
        state = load_state()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        print(f"[token-check] {info['error']}")
        if "--status" not in sys.argv and state.get("error_alerted_on") != today:
            send_telegram(env,
                          f"SCHWAB TOKEN PROBLEM\n{info['error']}\n\n{REAUTH_STEPS}")
            state["error_alerted_on"] = today
            save_state(state)
        return 1

    left       = info["seconds_left"]
    hours_left = left / 3600
    expiry_str = fmt_et(info["expires_at"])
    # Key alerts to this token generation so a re-auth resets the dedup
    gen        = str(int(info["created_at"]))

    print(f"[token-check] {hours_left:.1f}h left (expires {expiry_str})")

    if "--status" in sys.argv:
        return 0

    state   = load_state()
    alerted = state.get(gen, [])

    if left <= 0 and "expired" not in alerted:
        send_telegram(env,
                      f"SCHWAB TOKEN EXPIRED\n"
                      f"Expired at {expiry_str}. Live quotes are failing; "
                      f"bot is on fallback data.\n\n{REAUTH_STEPS}")
        alerted.append("expired")
    elif 0 < left <= WARN_4H_SEC and not ({"4h", "expired"} & set(alerted)):
        send_telegram(env,
                      f"SCHWAB TOKEN: LAST CALL\n"
                      f"Expires in {hours_left:.1f}h ({expiry_str}).\n\n{REAUTH_STEPS}")
        alerted.append("4h")
    elif 0 < left <= WARN_24H_SEC and not ({"24h", "4h", "expired"} & set(alerted)):
        send_telegram(env,
                      f"SCHWAB TOKEN EXPIRES IN ~{hours_left:.0f}H\n"
                      f"Expires {expiry_str}. Re-auth today to avoid an outage.\n\n"
                      f"{REAUTH_STEPS}")
        alerted.append("24h")

    # Keep only the current generation in state (old entries are useless)
    save_state({gen: alerted})
    return 0


if __name__ == "__main__":
    sys.exit(main())