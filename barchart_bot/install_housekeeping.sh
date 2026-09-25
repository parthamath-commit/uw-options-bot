#!/usr/bin/env bash
# Installs daily disk housekeeping, log rotation and a journald size cap. Safe to re-run.
set -euo pipefail
BB=/home/ubuntu/uw-options-bot/barchart_bot

# journald: never more than 200 MB of logs
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=200M\nSystemMaxFileSize=20M\n' | sudo tee /etc/systemd/journald.conf.d/pal-size.conf >/dev/null
sudo systemctl restart systemd-journald

# logrotate for the bots' own log files (weekly, keep 4, max 20 MB)
sudo tee /etc/logrotate.d/pal-bots >/dev/null <<'LR'
/home/ubuntu/uw-options-bot/logs/*.log /home/ubuntu/uw-options-bot/*.log /home/ubuntu/uw-options-bot/barchart_bot/*.log {
    weekly
    maxsize 20M
    rotate 4
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su ubuntu ubuntu
}
LR

# daily housekeeping timer, 17:15 ET (bot stops at 16:30)
sudo tee /etc/systemd/system/pal-housekeeping.service >/dev/null <<UNIT
[Unit]
Description=Daily disk housekeeping for trading bots
[Service]
Type=oneshot
ExecStart=/bin/bash $BB/housekeeping.sh
UNIT
sudo tee /etc/systemd/system/pal-housekeeping.timer >/dev/null <<'UNIT'
[Unit]
Description=Daily disk housekeeping (17:15 ET)
[Timer]
OnCalendar=*-*-* 17:15:00 America/New_York
Persistent=true
[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now pal-housekeeping.timer
echo "Housekeeping installed. Next run:"
systemctl --no-pager list-timers pal-housekeeping.timer | head -2
