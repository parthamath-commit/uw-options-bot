# Barchart Pro Bot on the Oracle VM

Same bot as `C:\BarchartBot\barchart_pro_bot.py`, adapted for the 1 GB Oracle VM:

- **Headless Chrome** (`google-chrome-stable`), Linux paths (everything lives next to the script).
- **Lazy browser**: Chrome starts only to download the Barchart CSVs and is closed while
  the bot sleeps, so Chrome and pandas/yfinance never hold RAM at the same time.
- Images + ad/tracker hosts blocked in Chrome; 4 GB swap as a safety net.
- Crash recovery: a dead Chrome is restarted and the download retried once; a fatal
  error exits with code 1 and systemd restarts the bot after 2 minutes.
- **Schedule: 09:00-16:30 ET on NYSE trading days.** After 16:30 (and on weekends/NYSE
  holidays) the bot closes Chrome and *sleeps* until 09:00 ET next trading day instead of
  exiting (v18.1; `SLEEP_UNTIL_NEXT_OPEN=true`, wake time `NEXT_OPEN_WAKE_HOUR/MINUTE` in `.env`).
  `barchartbot.timer` (09:00 Mon-Fri) is only a safety net if the process died.
  `barchartbot-stop.timer` must stay **disabled**, or it kills the sleeping bot at 16:40.
- **Schwab token**: `schwab-keeper.service` (`../schwab_token_keeper.py`) checks the token
  hourly and, 24h before the 7-day expiry, sends a Telegram login link. Log in, approve,
  paste the resulting `https://127.0.0.1...` address back into Telegram within ~30s; it saves
  the new token and restarts the bot. Telegram commands: `/token`, `/renew`.

Toggles in `.env` (defaults shown for Linux): `HEADLESS=true`, `BLOCK_AD_HOSTS=true`,
`DISABLE_IMAGES=true`, `RELEASE_CHROME_BETWEEN_CYCLES=true`. On Windows the defaults
keep the old visible-window behaviour, so the same file still runs on the laptop.

## Everyday commands (on the VM)
    journalctl -u barchartbot -f                 # live log
    journalctl -u schwab-keeper -f               # token keeper log
    sudo systemctl stop barchartbot              # stop for today
    sudo systemctl disable --now barchartbot.timer   # turn the bot off completely
    sudo systemctl enable --now barchartbot.timer    # turn it back on
    cd ~/uw-options-bot/barchart_bot && venv/bin/python barchart_pro_bot.py --test-download

## Updating
Edit in `C:\Unusual Whale Bot\uw-options-bot\barchart_bot`, push, then on the VM:
    cd ~/uw-options-bot && git pull && sudo systemctl restart barchartbot
