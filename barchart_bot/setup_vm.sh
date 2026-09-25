#!/usr/bin/env bash
# One-time (safe to re-run) setup of Barchart Pro Bot on the Oracle VM.
# Run on the VM:  bash ~/uw-options-bot/barchart_bot/setup_vm.sh
set -euo pipefail
BOT=/home/ubuntu/uw-options-bot/barchart_bot
UW_ENV=/home/ubuntu/uw-options-bot/.env
cd "$BOT"

echo "=== 1/6 Swap (1 GB RAM VM needs it for Chrome) ==="
if [ "$(swapon --show --noheadings | wc -l)" -eq 0 ]; then
  sudo fallocate -l 4G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  echo 'vm.swappiness=20' | sudo tee /etc/sysctl.d/99-swap.conf >/dev/null
  sudo sysctl -q -p /etc/sysctl.d/99-swap.conf
  echo "4 GB swap created."
else
  echo "Swap already present:"; swapon --show
fi

echo "=== 2/6 Google Chrome ==="
if ! command -v google-chrome >/dev/null 2>&1; then
  wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q /tmp/chrome.deb >/dev/null
  rm -f /tmp/chrome.deb
fi
google-chrome --version

echo "=== 3/6 Python venv + packages ==="
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt
echo "packages ok"

echo "=== 4/6 .env and history ==="
if [ -f /tmp/barchart.env ]; then mv /tmp/barchart.env "$BOT/.env"; fi
if [ ! -f "$BOT/.env" ]; then echo "ERROR: $BOT/.env missing (copy it from the laptop)"; exit 1; fi
chmod 600 "$BOT/.env"
if [ -f /tmp/barchart_signals.xlsx ]; then
  if [ ! -f "$BOT/signals.xlsx" ]; then mv /tmp/barchart_signals.xlsx "$BOT/signals.xlsx"; echo "signals.xlsx history copied"; else rm -f /tmp/barchart_signals.xlsx; fi
fi
# Point Schwab at the UW bot's token when both use the same Schwab app (one token to re-auth).
venv/bin/python - "$BOT/.env" "$UW_ENV" <<'PY'
import sys, re, os
bb_path, uw_path = sys.argv[1], sys.argv[2]
def read(p):
    d = {}
    if os.path.exists(p):
        for l in open(p, encoding="utf-8-sig", errors="replace"):
            if "=" in l and not l.lstrip().startswith("#"):
                k, v = l.split("=", 1); d[k.strip()] = v.strip().strip('"').strip("'")
    return d
bb, uw = read(bb_path), read(uw_path)
same = bb.get("SCHWAB_APP_KEY") and bb.get("SCHWAB_APP_KEY") == uw.get("SCHWAB_APP_KEY")
tok = "/home/ubuntu/uw-options-bot/schwab_token.json" if same else "/home/ubuntu/uw-options-bot/barchart_bot/schwab_token.json"
lines = open(bb_path, encoding="utf-8-sig", errors="replace").read().splitlines()
lines = [l for l in lines if not re.match(r"\s*SCHWAB_TOKEN_PATH\s*=", l)]
lines.append(f"SCHWAB_TOKEN_PATH={tok}")
open(bb_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
def setenv(key, val):
    global lines
    lines = [l for l in lines if not re.match(rf"\s*{key}\s*=", l)]
    lines.append(f"{key}={val}")
# Daily window 09:00-16:30 ET (timer starts 09:00, bot stops itself at 16:30)
setenv("AFTER_HOURS_END_HOUR", "16")
setenv("AFTER_HOURS_END_MINUTE", "30")
# Token expiry alerts come from schwab_token_keeper.py when the token is shared
setenv("SCHWAB_TOKEN_ALERTS", "false" if same else "true")
open(bb_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
if same:
    print("Schwab: same app as UW bot -> sharing its token file (re-auth once for both).")
else:
    print("Schwab: DIFFERENT app key from UW bot -> bot will use Yahoo data until a token")
    print("        exists at", tok)
PY

echo "=== 5/6 Smoke test: headless Chrome login + one CSV download ==="
if venv/bin/python barchart_pro_bot.py --test-download; then
  echo "Smoke test PASSED"
else
  echo "Smoke test FAILED -- check screenshots in $BOT/debug/ ; service NOT enabled."
  exit 1
fi
free -m

echo "=== 6/6 systemd: bot (09:00-16:30 ET trading days) + Schwab token keeper ==="
sudo cp barchartbot.service barchartbot.timer barchartbot-stop.service barchartbot-stop.timer schwab-keeper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now barchartbot.timer barchartbot-stop.timer
/home/ubuntu/uw-options-bot/venv/bin/pip install -q "schwab-py>=1.4" python-dotenv requests
sudo systemctl enable schwab-keeper.service
sudo systemctl restart schwab-keeper.service
# Start today only if we're inside the 09:00-16:30 ET window
NOW_ET=$(TZ=America/New_York date +%H%M)
if [ "${START_NOW:-yes}" = "yes" ] && [ "$NOW_ET" -ge 900 ] && [ "$NOW_ET" -lt 1630 ]; then
  sudo systemctl restart barchartbot.service
else
  echo "Outside 09:00-16:30 ET -- bot will start at the next trading-day 09:00."
fi
sleep 3
systemctl --no-pager status barchartbot.service | head -6 || true
systemctl --no-pager status schwab-keeper.service | head -4 || true
systemctl --no-pager list-timers 'barchartbot*' | head -4
bash "$BOT/install_housekeeping.sh"
bash "$BOT/install_eval.sh"
echo "Logs: journalctl -u barchartbot -f"
