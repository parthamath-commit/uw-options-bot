#!/usr/bin/env python3
"""
schwab_token_keeper.py -- keeps the shared Schwab token alive on the Oracle VM.

Schwab tokens:
  * access token  (30 min)  -> renewed silently by schwab-py on every API call.
                               The keeper also exercises it hourly so it never idles out.
  * refresh token (7 days)  -> Schwab REQUIRES an interactive login + approval to
                               issue a new one. It cannot be renewed with no human at all.

So the keeper does everything except the Schwab login itself:
  1. Checks the token every hour (age + a live test call).
  2. RENEW_AHEAD_HOURS before expiry (default 24h) it sends a Telegram message with
     the Schwab login link. Reminders every REMIND_EVERY_HOURS until renewed.
  3. You tap the link, log in, approve. The browser lands on a page that "can't be
     reached" (https://127.0.0.1...). Copy that full address and send it to the
     Telegram chat -- within ~30 seconds (Schwab's codes are short-lived).
  4. The keeper exchanges it for a fresh 7-day token, saves it, verifies it,
     restarts barchartbot (if running) so it picks up the new token, and confirms.

Telegram commands (from your chat only): /token -> status, /renew -> send link now.

Config (uw-options-bot/.env): SCHWAB_APP_KEY, SCHWAB_APP_SECRET, SCHWAB_CALLBACK_URL,
SCHWAB_TOKEN_PATH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
Optional: KEEPER_RENEW_AHEAD_HOURS=24, KEEPER_REMIND_EVERY_HOURS=6,
KEEPER_RESTART_SERVICES=barchartbot.service
"""

import html
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

APP_KEY = os.getenv("SCHWAB_APP_KEY", "")
APP_SECRET = os.getenv("SCHWAB_APP_SECRET", "")
CALLBACK_URL = os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
TOKEN_PATH = Path(os.getenv("SCHWAB_TOKEN_PATH", "schwab_token.json"))
if not TOKEN_PATH.is_absolute():
    TOKEN_PATH = ROOT / TOKEN_PATH
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = str(os.getenv("TELEGRAM_CHAT_ID", "")).strip()

RENEW_AHEAD_H = float(os.getenv("KEEPER_RENEW_AHEAD_HOURS", "24"))
REMIND_EVERY_H = float(os.getenv("KEEPER_REMIND_EVERY_HOURS", "6"))
CHECK_EVERY_S = 3600
RESTART_SERVICES = [s for s in os.getenv("KEEPER_RESTART_SERVICES", "barchartbot.service").split(",") if s.strip()]
REFRESH_LIFETIME_S = 7 * 24 * 3600

(ROOT / "logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] keeper - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(ROOT / "logs" / "schwab_keeper.log", encoding="utf-8")],
)
log = logging.getLogger("keeper")
logging.getLogger("httpx").setLevel(logging.WARNING)

from schwab import auth as schwab_auth  # noqa: E402


# ── Telegram ─────────────────────────────────────────────────────────────────
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"


def tg_send(text):
    try:
        requests.post(f"{TG_API}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": "true"}, timeout=20)
    except Exception as e:
        log.error("Telegram send failed: %s", e)


def tg_updates(offset, timeout=50):
    try:
        r = requests.get(f"{TG_API}/getUpdates",
                         params={"offset": offset, "timeout": timeout,
                                 "allowed_updates": json.dumps(["message", "channel_post"])},
                         timeout=timeout + 15)
        if r.status_code == 409:
            log.error("Another program is polling this Telegram bot (409). Retrying in 60s.")
            time.sleep(60)
            return []
        return r.json().get("result", [])
    except Exception as e:
        log.warning("getUpdates failed: %s", e)
        time.sleep(10)
        return []


# ── Token helpers ────────────────────────────────────────────────────────────
def token_status():
    """Return dict(exists, created, expires_in_s, error)."""
    if not TOKEN_PATH.exists():
        return {"exists": False, "expires_in_s": -1}
    try:
        data = json.loads(TOKEN_PATH.read_text())
        created = float(data.get("creation_timestamp", TOKEN_PATH.stat().st_mtime))
        return {"exists": True, "created": created,
                "expires_in_s": created + REFRESH_LIFETIME_S - time.time()}
    except Exception as e:
        return {"exists": True, "expires_in_s": -1, "error": str(e)}


def live_check():
    """One real API call (refreshes the 30-min access token too). True if Schwab accepts it."""
    try:
        c = schwab_auth.client_from_token_file(str(TOKEN_PATH), APP_KEY, APP_SECRET)
        r = c.get_quote("SPY")
        return r.status_code == 200
    except Exception as e:
        log.warning("live check failed: %s", e)
        return False


def write_token(token, *args, **kwargs):
    tmp = TOKEN_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(token))
    os.replace(tmp, TOKEN_PATH)          # atomic: readers never see half a file
    os.chmod(TOKEN_PATH, 0o600)


def fmt_left(seconds):
    if seconds <= 0:
        return "EXPIRED"
    h = seconds / 3600
    return f"{h / 24:.1f} days" if h >= 48 else f"{h:.1f} hours"


def restart_dependents():
    for svc in RESTART_SERVICES:
        svc = svc.strip()
        try:
            subprocess.run(["sudo", "-n", "systemctl", "try-restart", svc], timeout=60, check=False)
            log.info("try-restart %s", svc)
        except Exception as e:
            log.warning("could not restart %s: %s", svc, e)


# ── Main loop ────────────────────────────────────────────────────────────────
def main():
    for name, val in [("SCHWAB_APP_KEY", APP_KEY), ("SCHWAB_APP_SECRET", APP_SECRET),
                      ("TELEGRAM_BOT_TOKEN", TG_TOKEN), ("TELEGRAM_CHAT_ID", TG_CHAT)]:
        if not val:
            log.error("%s missing in %s/.env -- keeper cannot run.", name, ROOT)
            sys.exit(1)

    log.info("Schwab token keeper started. Token: %s  callback: %s", TOKEN_PATH, CALLBACK_URL)
    pending_ctx = None          # AuthContext of the link we last sent
    last_prompt = 0.0
    last_check = 0.0
    token_ok = True
    offset = None

    # skip any old Telegram messages
    old = tg_updates(None, timeout=0)
    if old:
        offset = old[-1]["update_id"] + 1

    def send_link(reason):
        nonlocal pending_ctx, last_prompt
        pending_ctx = schwab_auth.get_auth_context(APP_KEY, CALLBACK_URL)
        last_prompt = time.time()
        tg_send(
            f"🔑 <b>Schwab token renewal</b> -- {reason}\n\n"
            f"1️⃣ Tap: <a href=\"{html.escape(pending_ctx.authorization_url, quote=True)}\">Log in to Schwab</a>\n"
            "2️⃣ Log in and approve.\n"
            f"3️⃣ The page will fail to load ({CALLBACK_URL}...). Copy that <b>full address</b> "
            "and paste it here <b>within ~30 seconds</b>.\n\n"
            "I'll save the new 7-day token and restart the bot automatically."
        )
        log.info("Renewal link sent (%s)", reason)

    while True:
        now = time.time()

        # hourly health check
        if now - last_check >= CHECK_EVERY_S:
            last_check = now
            st = token_status()
            left = st["expires_in_s"]
            token_ok = st["exists"] and left > 0 and live_check()
            log.info("token check: exists=%s left=%s live_ok=%s", st["exists"], fmt_left(left), token_ok)
            need = (not token_ok) or left <= RENEW_AHEAD_H * 3600
            if need and (now - last_prompt >= REMIND_EVERY_H * 3600):
                reason = ("token is not working" if not token_ok
                          else f"expires in {fmt_left(left)}")
                send_link(reason)

        # Telegram messages (long poll)
        for u in tg_updates(offset):
            offset = u["update_id"] + 1
            msg = u.get("message") or u.get("channel_post") or {}
            if str(msg.get("chat", {}).get("id")) != TG_CHAT:
                continue                              # ignore anyone else
            text = (msg.get("text") or "").strip()

            if text.lower().startswith("/token"):
                st = token_status()
                tg_send(f"Schwab token: {fmt_left(st['expires_in_s'])} left "
                        f"(live check {'OK' if live_check() else 'FAILED'}).")
            elif text.lower().startswith("/renew"):
                send_link("requested")
            elif "code=" in text and text.startswith(CALLBACK_URL.split("?")[0].rstrip("/")):
                if pending_ctx is None:
                    tg_send("No renewal in progress -- send /renew first.")
                    continue
                try:
                    schwab_auth.client_from_received_url(
                        APP_KEY, APP_SECRET, pending_ctx, text, write_token)
                    ok = live_check()
                    pending_ctx = None
                    last_check = time.time()
                    token_ok = ok
                    restart_dependents()
                    st = token_status()
                    tg_send(f"✅ Schwab token renewed. Valid for {fmt_left(st['expires_in_s'])}. "
                            f"Live check: {'OK' if ok else 'FAILED'}.")
                    log.info("Token renewed; live_ok=%s", ok)
                except Exception as e:
                    log.error("Token exchange failed: %s", e)
                    tg_send("❌ Renewal failed (the code may have expired -- they last ~30s). "
                            "Send /renew for a fresh link and paste faster.\n"
                            f"<code>{str(e)[:200]}</code>")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
