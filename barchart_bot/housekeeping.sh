#!/usr/bin/env bash
# Daily disk housekeeping for the Oracle VM (UW bot + Barchart bot + keeper).
# Installed by install_housekeeping.sh as pal-housekeeping.timer (daily 17:15 ET, after the bot stops).
# Run by hand any time:  sudo bash /home/ubuntu/uw-options-bot/barchart_bot/housekeeping.sh
set -uo pipefail
BASE=/home/ubuntu/uw-options-bot
BB=$BASE/barchart_bot
KEEP_ARCHIVE_DAYS=${KEEP_ARCHIVE_DAYS:-7}    # Barchart CSV archive (bot only needs today's files)
KEEP_DEBUG_DAYS=${KEEP_DEBUG_DAYS:-7}        # failure screenshots/HTML, images
ALERT_PCT=${ALERT_PCT:-80}                   # Telegram alert when disk use >= this %
JOURNAL_MAX=${JOURNAL_MAX:-200M}

log(){ echo "$(date '+%F %T') $*"; }
used_mb(){ df --output=used -BM / | tail -1 | tr -dc '0-9'; }
before=$(used_mb)
log "=== housekeeping start: disk $(df -h / | awk 'NR==2{print $3" used of "$2" ("$5")"}')"

# 1. Barchart runtime folders
[ -d "$BB/archive" ]   && find "$BB/archive" -type f -mtime +"$KEEP_ARCHIVE_DAYS" -delete \
                       && find "$BB/archive" -mindepth 1 -type d -empty -delete
for d in debug images; do [ -d "$BB/$d" ] && find "$BB/$d" -type f -mtime +"$KEEP_DEBUG_DAYS" -delete; done
[ -d "$BB/downloads" ] && find "$BB/downloads" -type f -mtime +1 -delete   # stale/partial downloads

# 2. Chrome cache (only when the bot is not running; login cookies are kept)
if ! systemctl is-active --quiet barchartbot.service; then
  P="$BB/chrome_profile_bot"
  for c in "Default/Cache" "Default/Code Cache" "Default/GPUCache" "Default/Service Worker/CacheStorage" \
           "GrShaderCache" "ShaderCache" "GraphiteDawnCache" "component_crx_cache"; do
    rm -rf "$P/$c" 2>/dev/null
  done
fi

# 3. Old compressed logs beyond logrotate's window
find "$BASE" -name "*.log.*.gz" -mtime +35 -delete 2>/dev/null

# 4. System: journald cap, apt cache, /tmp leftovers; pip cache weekly (Sunday)
journalctl --vacuum-size="$JOURNAL_MAX" >/dev/null 2>&1
apt-get clean >/dev/null 2>&1
find /tmp -maxdepth 1 \( -name "chrome*" -o -name ".org.chromium.*" -o -name "*.crdownload" -o -name "bb_debug.tgz" \) \
     -mtime +1 -exec rm -rf {} + 2>/dev/null
if [ "$(date +%u)" = "7" ]; then
  sudo -u ubuntu "$BASE/venv/bin/pip" cache purge >/dev/null 2>&1
  sudo -u ubuntu "$BB/venv/bin/pip" cache purge >/dev/null 2>&1
fi

after=$(used_mb)
pct=$(df --output=pcent / | tail -1 | tr -dc '0-9')
log "freed $((before - after)) MB; disk now $(df -h / | awk 'NR==2{print $3" used of "$2" ("$5"), "$4" free"}')"
log "largest folders:"
du -xsh "$BB"/{archive,debug,images,downloads,chrome_profile_bot,venv} "$BASE/venv" "$BASE/logs" \
        /var/log/journal /var/cache/apt 2>/dev/null | sort -hr | sed 's/^/    /'

# 5. Alert if the disk is getting full
if [ "$pct" -ge "$ALERT_PCT" ]; then
  TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$BASE/.env" | cut -d= -f2- | tr -d '"'"'"' \r')
  CHAT=$(grep -E '^TELEGRAM_CHAT_ID=' "$BASE/.env" | cut -d= -f2- | tr -d '"'"'"' \r')
  if [ -n "$TOKEN" ] && [ -n "$CHAT" ]; then
    curl -s -m 20 "https://api.telegram.org/bot$TOKEN/sendMessage" \
      --data-urlencode "chat_id=$CHAT" \
      --data-urlencode "text=⚠️ Oracle VM disk ${pct}% full after cleanup ($(df -h / | awk 'NR==2{print $4}') free). Run: sudo du -xh / --max-depth=2 | sort -hr | head -20" >/dev/null
    log "ALERT sent: disk ${pct}%"
  fi
fi
log "=== housekeeping done"
