# News watchers

**alpaca_news.py** (service `alpaca-news`, always on) -- real-time Benzinga headlines via Alpaca.
Sends once a minute, in short sentences: 🏦 analyst calls from major firms, 🚀 big catalysts
(FDA approval, acquisitions, major contracts, guidance raises), ⚠️ red flags (offerings,
reverse splits, bankruptcy...) for tickers your bots care about. Writes `recent_news.json`,
which the Barchart news runner reads first, and `news_log.csv`.

**sec_insider.py** (timer `sec-insider`, every 5 min weekdays 07-20 ET) -- SEC Form 4 open-market
insider purchases of $100K+ (officers, directors, 10% owners).

`.env` settings: ALPACA_API_KEY, ALPACA_SECRET_KEY (required), SEC_CONTACT_EMAIL (SEC asks for it),
SEC_MIN_BUY=100000, SEC_ALERT_SELLS_OVER=0 (e.g. 10000000 to also flag $10M+ sales),
NEWS_WATCH_TICKERS=NVDA,MU,..., NEWS_TELEGRAM_CHAT_ID / SEC_TELEGRAM_CHAT_ID (optional other chat).

    journalctl -u alpaca-news -f          # live news log
    journalctl -u sec-insider -n 30       # last insider checks
