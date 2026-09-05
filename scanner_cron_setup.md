# Scheduler setup for schwab_scanner.py email reports

## Universe
Every run scans ALL NASDAQ-listed common stocks + S&P 500 constituents.
The list is rebuilt every Saturday by a dedicated cron job (build_universe.py
--force). Scan runs in between simply reuse symbols.txt, so the universe is
stable Sat-to-Sat and mid-week scans don't re-fetch. Force a rebuild anytime with: python3 build_universe.py --force

## What runs
- Mon-Thu, after US close: scans 1, 2, 4  -> email
- Friday, after US close:  scans 1, 2, 3, 4 -> email

## Timing
US market closes 16:00 America/New_York. We wait until 18:00 ET so the
finalized daily candle is available. cron on the VM runs in the VM's local
timezone -- check it first:

    timedatectl            # note the "Time zone" line

If the VM is UTC (Oracle default), 18:00 ET = 22:00 UTC (23:00 during EST).
To avoid DST math, set the VM tz to New York so the cron times are literal:

    sudo timedatectl set-timezone America/New_York

(If you prefer to leave the VM on UTC, use the UTC column in the crontab below.)

## Wrapper script
Cron has a minimal environment, so use a wrapper that loads .env, activates the
venv, points at the full universe, and runs the scanner. Save as run_email.sh:

    #!/usr/bin/env bash
    set -euo pipefail
    cd /home/ubuntu/uw-options-bot
    source venv/bin/activate
    # Universe auto-rebuilds weekly (NASDAQ-listed + S&P 500). No export needed.
    python3 schwab_scanner.py "$1" >> /home/ubuntu/uw-options-bot/scanner_email.log 2>&1

Make it executable:

    chmod +x run_email.sh

## Crontab (America/New_York)
Edit with: crontab -e   then add:

    # Saturday 08:00 ET -> rebuild ticker universe (also writes sp500.txt)
    0 8 * * 6     cd /home/ubuntu/uw-options-bot && venv/bin/python3 build_universe.py --force >> universe_build.log 2>&1
    # Mon-Fri 15:30 ET -> intraday, S&P 500 only (scans 1,6,7)
    30 15 * * 1-5 /home/ubuntu/uw-options-bot/run_email.sh --report-330
    # Mon-Fri 18:00 ET -> after close, S&P 500 only (scans 1,2)
    0 18 * * 1-5  /home/ubuntu/uw-options-bot/run_email.sh --report-close
    # Saturday 09:00 ET -> weekend: scan 3 (S&P 500) + scan 4 (whole universe)
    0 9 * * 6     /home/ubuntu/uw-options-bot/run_email.sh --report-weekend

## Crontab (if VM stays on UTC) -- EDT (summer). Add 1 hour in winter (EST).
    0 12 * * 6    cd /home/ubuntu/uw-options-bot && venv/bin/python3 build_universe.py --force >> universe_build.log 2>&1
    30 19 * * 1-5 /home/ubuntu/uw-options-bot/run_email.sh --report-330
    0 22 * * 1-5  /home/ubuntu/uw-options-bot/run_email.sh --report-close
    0 13 * * 6    /home/ubuntu/uw-options-bot/run_email.sh --report-weekend

## Delivery channel

### Telegram (recommended -- reuses the UW bot's existing setup)
No new credentials needed. The scanner reads the SAME keys the bot uses:
    TELEGRAM_BOT_TOKEN=...   (already in .env)
    TELEGRAM_CHAT_ID=...     (already in .env)
Reports go to the same chat as your bot alerts. Long reports are auto-split
into <=4000-char chunks. This is the default -- nothing to configure.

### Bollinger Band + volume tuning (optional)
Defaults shown; set in .env to change. Affects scans 1/2/3:
    BB_WINDOW=20           # Bollinger SMA period
    BB_STD=2               # standard deviations for the bands
    BB_OFFSET=0.5          # $ proximity of high/low to a band (scans 1,3)
    RSI_PERIOD=14          # RSI lookback (scan 1)
    RSI_OVERBOUGHT=70      # RSI overbought level (scan 1)
    RSI_OVERSOLD=30        # RSI oversold level (scan 1)
    MIN_AVG_VOL=1000000    # min avg volume gate for daily scans (1,2)
    MIN_AVG_VOL_WEEKLY=5000000  # min avg volume gate for weekly scan (3)
    VOL_WINDOW=30          # lookback for the avg-volume gate
    MIN_PRICE=30           # min price ($); applies to scans 1,2,3,6,7 AND SEPA
    RS_MIN=90              # SEPA: required RS Rating floor (1-99), Minervini pref 90

### SMA-proximity scan tuning (optional)
Options 6/7 use these; defaults shown. Set in .env to change:
    SMA_PROX_PERIOD=20     # SMA lookback in days
    SMA_PROX_BAND=2        # dollar band above/below the SMA

### Email (optional -- only if you ALSO want email)
Add these to .env; if present, reports are emailed in addition to Telegram:
    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USER=youraddress@gmail.com
    SMTP_APP_PASSWORD=your16charapppassword
    EMAIL_TO=partha_pal_1999@yahoo.com
Gmail app password: enable 2FA, then Google Account -> Security ->
App passwords -> generate for "Mail", paste the 16-char value (no spaces).

## Test before trusting the schedule
    ./run_email.sh --email-daily        # runs now, emails, check inbox + log
    tail -n 40 scanner_email.log
