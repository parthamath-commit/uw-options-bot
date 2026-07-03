"""
watchdog.py -- Self-healing monitor for UW Options Bot v2.0

Runs alongside main.py. Every 15 minutes it checks:
  1. Is the bot process still running?
  2. Has the heartbeat file been updated recently (bot not hung)?
  3. Are there fresh ERROR lines in the log since last check?

If the bot is dead or hung, it restarts main.py automatically.
If new errors appeared, it sends a Telegram alert (does not restart
for errors alone -- only for crash/hang).

Usage:
    python watchdog.py
    (leave running in its own terminal / background task)

Config via .env (optional):
    WATCHDOG_INTERVAL_SEC=900        # 15 minutes
    WATCHDOG_HEARTBEAT_MAX_AGE=1200  # 20 minutes -- hang threshold
"""
import os
import re
import sys
import time
import subprocess
import logging
from pathlib import Path
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env")
except ImportError:
    pass

ROOT          = Path(__file__).parent
LOG_FILE      = ROOT / "logs" / "uw_bot.log"
HEARTBEAT     = ROOT / "output" / "heartbeat.txt"
MAIN_SCRIPT   = ROOT / "main.py"
PYTHON        = sys.executable

CHECK_INTERVAL   = int(os.getenv("WATCHDOG_INTERVAL_SEC", "900"))      # 15 min
HEARTBEAT_MAX_AGE = int(os.getenv("WATCHDOG_HEARTBEAT_MAX_AGE", "1200"))  # 20 min

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] Watchdog - %(message)s",
)
log = logging.getLogger("watchdog")


def send_telegram(text: str):
    """Best-effort Telegram notification using the bot's own config."""
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
        import requests
        requests.post(
            "https://api.telegram.org/bot{}/sendMessage".format(token),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning("Telegram notify failed: {}".format(e))


def heartbeat_age_seconds() -> float:
    """Seconds since the bot last wrote its heartbeat file."""
    if not HEARTBEAT.exists():
        return float("inf")
    try:
        ts = datetime.fromisoformat(HEARTBEAT.read_text().strip())
        return (datetime.now() - ts).total_seconds()
    except Exception:
        return float("inf")


def find_bot_process():
    """
    Return True if a python process running main.py is alive.
    Uses tasklist on Windows, ps elsewhere.
    """
    try:
        if os.name == "nt":
            out = subprocess.check_output(
                ["wmic", "process", "where",
                 "name='python.exe' or name='pythonw.exe'",
                 "get", "CommandLine"],
                stderr=subprocess.DEVNULL, text=True, timeout=15,
            )
            return "main.py" in out
        else:
            out = subprocess.check_output(["ps", "aux"], text=True)
            return "main.py" in out
    except Exception as e:
        log.warning("Process check failed: {}".format(e))
        return True  # assume alive on check failure -- avoid restart storms


def restart_bot():
    """Launch main.py in the background, detached from the watchdog."""
    log.warning("Restarting main.py ...")
    send_telegram("🔧 <b>WATCHDOG</b>: Bot appears down/hung -- restarting now.")
    try:
        if os.name == "nt":
            subprocess.Popen(
                [PYTHON, str(MAIN_SCRIPT)],
                cwd=str(ROOT),
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        else:
            subprocess.Popen(
                [PYTHON, str(MAIN_SCRIPT)],
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        log.info("Restart command issued.")
    except Exception as e:
        log.error("Restart failed: {}".format(e))
        send_telegram(
            "❌ <b>WATCHDOG</b>: Restart attempt failed: {}".format(e))


def check_new_errors(last_pos: int) -> tuple[int, list[str]]:
    """
    Read any new ERROR-level lines appended to the log since last_pos.
    Returns (new_file_position, list_of_error_lines).
    """
    if not LOG_FILE.exists():
        return last_pos, []
    size = LOG_FILE.stat().st_size
    if size < last_pos:
        last_pos = 0   # log was rotated/truncated
    errors = []
    with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(last_pos)
        for line in f:
            if "[ERROR]" in line:
                errors.append(line.rstrip())
        new_pos = f.tell()
    return new_pos, errors


def summarize_errors(errors: list[str]) -> str:
    """Collapse repeated error patterns into counts for a compact alert."""
    from collections import Counter
    # Strip timestamps to group similar errors
    normalized = []
    for e in errors:
        m = re.search(r"\[ERROR\]\s+(.*)", e)
        normalized.append(m.group(1) if m else e)
    counts = Counter(normalized)
    lines = []
    for msg, n in counts.most_common(5):
        short = msg[:150]
        lines.append("  • {}{}  (x{})".format(
            short, "..." if len(msg) > 150 else "", n))
    return "\n".join(lines)


def main():
    log.info("Watchdog started. Checking every {}s, hang threshold {}s.".format(
        CHECK_INTERVAL, HEARTBEAT_MAX_AGE))
    log.info("Monitoring: {}".format(MAIN_SCRIPT))
    log.info("Heartbeat:  {}".format(HEARTBEAT))
    log.info("Log file:   {}".format(LOG_FILE))

    last_log_pos = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0

    while True:
        time.sleep(CHECK_INTERVAL)
        now = datetime.now().strftime("%H:%M:%S")

        # 1. Check for new errors in the log
        last_log_pos, new_errors = check_new_errors(last_log_pos)
        if new_errors:
            log.warning("{} new ERROR line(s) since last check".format(
                len(new_errors)))
            summary = summarize_errors(new_errors)
            send_telegram(
                "⚠️ <b>WATCHDOG</b>: {} new error(s) in the last {} min\n{}".format(
                    len(new_errors), CHECK_INTERVAL // 60, summary)
            )

        # 2. Check heartbeat freshness
        age = heartbeat_age_seconds()
        proc_alive = find_bot_process()

        if age > HEARTBEAT_MAX_AGE or not proc_alive:
            reason = []
            if age > HEARTBEAT_MAX_AGE:
                reason.append("heartbeat stale ({:.0f}s old)".format(age))
            if not proc_alive:
                reason.append("process not found")
            log.error("Bot unhealthy: {}".format(", ".join(reason)))
            restart_bot()
            # Give it time to come up before re-checking heartbeat
            time.sleep(30)
        else:
            log.info("OK at {} -- heartbeat {:.0f}s old, process alive".format(
                now, age))


if __name__ == "__main__":
    # ── WATCHDOG DISABLED ─────────────────────────────────────────────────────
    # Set WATCHDOG_ENABLED=true in .env (or environment) to re-enable.
    # Default is disabled so the bot can be restarted manually without
    # the watchdog immediately spawning a second instance.
    WATCHDOG_ENABLED = os.getenv("WATCHDOG_ENABLED", "false").strip().lower() == "true"
    if not WATCHDOG_ENABLED:
        log.info("Watchdog is DISABLED (WATCHDOG_ENABLED != true). Exiting.")
        send_telegram("ℹ️ <b>WATCHDOG</b>: Disabled — bot will not auto-restart.")
        sys.exit(0)
    # ─────────────────────────────────────────────────────────────────────────
    try:
        main()
    except KeyboardInterrupt:
        log.info("Watchdog stopped.")
