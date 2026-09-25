#!/usr/bin/env bash
# Installs: alpaca-news.service (always-on real-time news) + sec-insider.timer (every 5 min, weekdays 07-20 ET)
set -euo pipefail
BASE=/home/ubuntu/uw-options-bot
NW=$BASE/news_watcher
$BASE/venv/bin/pip install -q websocket-client requests python-dotenv
$BASE/venv/bin/python $NW/alpaca_news.py --selftest | tail -1
$BASE/venv/bin/python $NW/sec_insider.py --selftest | tail -1
sudo tee /etc/systemd/system/alpaca-news.service >/dev/null <<UNIT
[Unit]
Description=Real-time market news (Alpaca/Benzinga) -> Telegram
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=ubuntu
WorkingDirectory=$NW
Environment=PYTHONUNBUFFERED=1
ExecStart=$BASE/venv/bin/python $NW/alpaca_news.py
Restart=always
RestartSec=30
MemoryMax=150M
[Install]
WantedBy=multi-user.target
UNIT
sudo tee /etc/systemd/system/sec-insider.service >/dev/null <<UNIT
[Unit]
Description=SEC Form 4 insider buying -> Telegram
[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=$NW
ExecStart=$BASE/venv/bin/python $NW/sec_insider.py
TimeoutStartSec=240
UNIT
sudo tee /etc/systemd/system/sec-insider.timer >/dev/null <<'UNIT'
[Unit]
Description=SEC Form 4 check every 5 minutes, weekdays 07:00-20:00 ET
[Timer]
OnCalendar=Mon..Fri *-*-* 07..19:00/5:00 America/New_York
[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable alpaca-news.service
sudo systemctl restart alpaca-news.service
sudo systemctl enable --now sec-insider.timer
sleep 8
systemctl --no-pager status alpaca-news.service | head -5
journalctl -u alpaca-news -n 8 --no-pager
systemctl --no-pager list-timers sec-insider.timer | head -2
