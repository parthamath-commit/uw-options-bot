# Scheduler setup for schwab_scanner.py email reports

## Universe
Every run scans ALL NASDAQ-listed common stocks + S&P 500 constituents.
The scanner rebuilds this list once per day (cached in symbols.txt) and reuses
it for the rest of the day, so scheduled runs always use a current universe
with no manual step. Force a rebuild anytime with: python3 build_universe.py --force

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
    # Universe auto-rebuilds daily (NASDAQ-listed + S&P 500). No export needed.
    python3 schwab_scanner.py "$1" >> /home/ubuntu/uw-options-bot/scanner_email.log 2>&1

Make it executable:

    chmod +x run_email.sh

## Crontab (America/New_York)
Edit with: crontab -e   then add:

    # Mon-Thu 18:00 ET -> daily report (scans 1,2,4)
    0 18 * * 1-4  /home/ubuntu/uw-options-bot/run_email.sh --email-daily
    # Friday 18:00 ET -> weekly report (scans 1,2,3,4)
    0 18 * * 5    /home/ubuntu/uw-options-bot/run_email.sh --email-weekly

## Crontab (if VM stays on UTC) -- EDT (summer). Add 1 hour in winter (EST).
    0 22 * * 1-4  /home/ubuntu/uw-options-bot/run_email.sh --email-daily
    0 22 * * 5    /home/ubuntu/uw-options-bot/run_email.sh --email-weekly

## Delivery channel

### Telegram (recommended -- reuses the UW bot's existing setup)
No new credentials needed. The scanner reads the SAME keys the bot uses:
    TELEGRAM_BOT_TOKEN=...   (already in .env)
    TELEGRAM_CHAT_ID=...     (already in .env)
Reports go to the same chat as your bot alerts. Long reports are auto-split
into <=4000-char chunks. This is the default -- nothing to configure.

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
