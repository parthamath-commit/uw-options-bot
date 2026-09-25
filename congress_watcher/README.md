# Congress + analyst watchers

Free alerts for US Congress stock and **option** trades, straight from the official
House Clerk and Senate eFD sites, checked every 30 minutes (06:00-22:00 ET).
Options trades are listed first (🎯). Scanned/paper reports are sent as links.

Settings (optional, in uw-options-bot/.env or congress_watcher/.env):
- `CONGRESS_OPTIONS_ONLY=true` -- only send option trades
- `CONGRESS_WATCH_TICKERS=NVDA,MU,TSLA` -- mark these with ⭐ (tickers from the Barchart bot's
  recent alerts are starred automatically)
- `CONGRESS_TELEGRAM_CHAT_ID=...` -- send to a different chat (default TELEGRAM_CHAT_ID)

VM commands:
    journalctl -u congress-watcher -n 50          # last runs
    ~/uw-options-bot/venv/bin/python ~/uw-options-bot/congress_watcher/congress_watcher.py --dry-run --days 7
    sudo systemctl disable --now congress-watcher.timer   # turn off

## Analyst watcher (analyst_watcher.py)
Alerts when a major firm (Goldman, Morgan Stanley, JPMorgan, BofA, Citi, Barclays, UBS, ...)
starts coverage with a Buy/Sell-type rating, or upgrades/downgrades a stock. Checks the
S&P 500 + your watch tickers + the Barchart bot's recent tickers at 07:45, 09:15, 12:15 and
16:45 ET on weekdays. Data: Yahoo Finance (free).

- `ANALYST_FIRMS=Goldman Sachs,Morgan Stanley,...` -- override the firm list
- `ANALYST_WATCH_TICKERS=...` -- extra tickers (defaults to CONGRESS_WATCH_TICKERS)
- `ANALYST_TELEGRAM_CHAT_ID=...` -- send to a different chat
