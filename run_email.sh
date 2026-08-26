#!/usr/bin/env bash
set -euo pipefail
cd /home/ubuntu/uw-options-bot
source venv/bin/activate
# Universe is rebuilt automatically by the scanner (NASDAQ-listed + S&P 500,
# day-cached in symbols.txt). No SYMBOLS_FILE export needed.
python3 schwab_scanner.py "$1" >> /home/ubuntu/uw-options-bot/scanner_email.log 2>&1
