#!/usr/bin/env bash
# Installs the Congress watcher: every 30 min, 06:00-22:00 ET, every day.
set -euo pipefail
BASE=/home/ubuntu/uw-options-bot
CW=$BASE/congress_watcher
$BASE/venv/bin/pip install -q pdfplumber requests python-dotenv yfinance pandas
$BASE/venv/bin/python $CW/congress_watcher.py --selftest | tail -1
$BASE/venv/bin/python $CW/analyst_watcher.py --selftest | tail -1
sudo tee /etc/systemd/system/congress-watcher.service >/dev/null <<UNIT
[Unit]
Description=Congress trades watcher (House + Senate PTRs -> Telegram)
After=network-online.target
[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=$CW
ExecStart=$BASE/venv/bin/python $CW/congress_watcher.py
TimeoutStartSec=600
UNIT
sudo tee /etc/systemd/system/congress-watcher.timer >/dev/null <<'UNIT'
[Unit]
Description=Congress trades watcher every 30 min, 06:00-22:00 ET
[Timer]
OnCalendar=*-*-* 06..21:00,30:00 America/New_York
OnCalendar=*-*-* 22:00:00 America/New_York
Persistent=false
[Install]
WantedBy=timers.target
UNIT
# Analyst watcher: 07:45, 09:15, 12:15, 16:45 ET on weekdays
sudo tee /etc/systemd/system/analyst-watcher.service >/dev/null <<UNIT
[Unit]
Description=Analyst rating watcher (major firms: initiations, upgrades, downgrades -> Telegram)
After=network-online.target
[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=$CW
ExecStart=$BASE/venv/bin/python $CW/analyst_watcher.py
TimeoutStartSec=1200
UNIT
sudo tee /etc/systemd/system/analyst-watcher.timer >/dev/null <<'UNIT'
[Unit]
Description=Analyst rating watcher, weekdays 07:45 09:15 12:15 16:45 ET
[Timer]
OnCalendar=Mon..Fri *-*-* 07:45:00 America/New_York
OnCalendar=Mon..Fri *-*-* 09:15:00 America/New_York
OnCalendar=Mon..Fri *-*-* 12:15:00 America/New_York
OnCalendar=Mon..Fri *-*-* 16:45:00 America/New_York
[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now congress-watcher.timer analyst-watcher.timer
systemctl --no-pager list-timers congress-watcher.timer analyst-watcher.timer | head -3
