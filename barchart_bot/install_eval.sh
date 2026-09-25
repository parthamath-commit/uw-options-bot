#!/usr/bin/env bash
# Installs the daily accuracy evaluation (17:30 ET weekdays): labels every logged
# candidate with real price moves and posts a short summary to Telegram.
set -euo pipefail
BB=/home/ubuntu/uw-options-bot/barchart_bot
sudo tee /etc/systemd/system/barchart-eval.service >/dev/null <<UNIT
[Unit]
Description=Barchart bot accuracy evaluation
[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=$BB
ExecStart=$BB/venv/bin/python evaluate_signals.py --telegram --days 30
UNIT
sudo tee /etc/systemd/system/barchart-eval.timer >/dev/null <<'UNIT'
[Unit]
Description=Daily Barchart bot accuracy evaluation (17:30 ET weekdays)
[Timer]
OnCalendar=Mon..Fri *-*-* 17:30:00 America/New_York
Persistent=true
[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now barchart-eval.timer
systemctl --no-pager list-timers barchart-eval.timer | head -2
