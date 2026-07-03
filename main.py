#!/usr/bin/env python3
"""
main.py -- UW Options Bot v2.0
Pal Initiatives LLC

ALL mode functions defined before dispatch dict.
No function appending -- complete rewrite every time.

Modes:
  run           Full continuous scan loop (default)
  scan          Single cycle
  dealer        GEX/DEX snapshot for watchlist
  check         API health check          <- run first
  query         Recent scan runs from DB
  backtest      Win/loss summary from DB
  tokenage      Schwab token age
  apistats      UW API usage stats
  mlstatus      ML training data status
  train         Train ML models
  watchlist     Preview dynamic watchlist
  addposition   Add position for exit tracking
  positions     View open positions + P&L
  purge         Archive expired/closed data
  dbstats       Database row counts + size
"""

import argparse
import sys
import logging

# Windows UTF-8 fix
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

log = logging.getLogger("UWBot")


# ── 1: Full loop ──────────────────────────────────────────────────────────────
def run_loop():
    import subprocess
    import signal
    import threading
    from pathlib import Path
    from zoneinfo import ZoneInfo
    import datetime as _dt
    import time as _time

    ET = ZoneInfo("America/New_York")

    # ── NYSE holiday calendar (self-contained, no extra deps) ──────────────
    def _easter_sunday(year):
        # Meeus/Jones/Butcher Gregorian algorithm
        a = year % 19
        b = year // 100
        c = year % 100
        d = b // 4
        e = b % 4
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i = c // 4
        k = c % 4
        l = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * l) // 451
        month = (h + l - 7 * m + 114) // 31
        day = ((h + l - 7 * m + 114) % 31) + 1
        return _dt.date(year, month, day)

    def _nth_weekday(year, month, weekday, n):
        d0 = _dt.date(year, month, 1)
        offset = (weekday - d0.weekday()) % 7
        return d0 + _dt.timedelta(days=offset + 7 * (n - 1))

    def _last_weekday(year, month, weekday):
        nxt = _dt.date(year + 1, 1, 1) if month == 12 else _dt.date(year, month + 1, 1)
        d0 = nxt - _dt.timedelta(days=1)
        offset = (d0.weekday() - weekday) % 7
        return d0 - _dt.timedelta(days=offset)

    def _observed(d):
        # Sat -> observe Friday before; Sun -> observe Monday after
        if d.weekday() == 5:
            return d - _dt.timedelta(days=1)
        if d.weekday() == 6:
            return d + _dt.timedelta(days=1)
        return d

    _holiday_cache = {}
    def _nyse_holidays(year):
        if year in _holiday_cache:
            return _holiday_cache[year]
        hol = {
            _observed(_dt.date(year, 1, 1)),           # New Year's Day
            _nth_weekday(year, 1, 0, 3),                # MLK Day
            _nth_weekday(year, 2, 0, 3),                # Presidents Day
            _easter_sunday(year) - _dt.timedelta(days=2),  # Good Friday
            _last_weekday(year, 5, 0),                  # Memorial Day
            _observed(_dt.date(year, 6, 19)),           # Juneteenth
            _observed(_dt.date(year, 7, 4)),            # Independence Day
            _nth_weekday(year, 9, 0, 1),                # Labor Day
            _nth_weekday(year, 11, 3, 4),               # Thanksgiving
            _observed(_dt.date(year, 12, 25)),          # Christmas
        }
        _holiday_cache[year] = hol
        return hol

    def _is_trading_day(d):
        return d.weekday() < 5 and d not in _nyse_holidays(d.year)

    # ── Gate: only proceed if today is a trading day ────────────────────────
    now_et = _dt.datetime.now(ET)
    if not _is_trading_day(now_et.date()):
        log.info("{} is a weekend/market holiday -- nothing to run today. Exiting.".format(
            now_et.strftime("%a %Y-%m-%d")))
        sys.exit(0)

    market_close_mins = 16 * 60 + 5
    now_mins = now_et.hour * 60 + now_et.minute
    if now_mins > market_close_mins:
        log.info("Started after today's close ({}) -- nothing to run. Exiting.".format(
            now_et.strftime("%H:%M ET")))
        sys.exit(0)

    # ── If we're early, wait right up to open (Task Scheduler should launch
    #    this close to market open already; this is just a safety buffer) ──
    today_open = _dt.datetime.combine(now_et.date(), _dt.time(9, 25, 0), tzinfo=ET)
    if now_et < today_open:
        secs = (today_open - now_et).total_seconds()
        log.info("Waiting {:.0f}m for market open ({})...".format(
            secs / 60, today_open.strftime("%H:%M ET")))
        try:
            from telegram_client import TelegramClient as _TG
            _TG().send(
                "🕐 <b>Bot started -- waiting for market open</b>\n"
                "Open: {}".format(today_open.strftime("%a %b %d %H:%M ET"))
            )
        except Exception:
            pass
        _time.sleep(secs + 5)   # +5s buffer past open

    watchdog_proc = None
    watchdog_path = Path(__file__).parent / "watchdog.py"

    # Launch watchdog as a subprocess alongside the bot
    if watchdog_path.exists():
        try:
            watchdog_proc = subprocess.Popen(
                [sys.executable, str(watchdog_path)],
                cwd=str(Path(__file__).parent),
            )
            log.info("Watchdog started (pid={})".format(watchdog_proc.pid))
        except Exception as e:
            log.warning("Could not start watchdog: {}".format(e))
    else:
        log.warning("watchdog.py not found -- skipping watchdog")

    def _stop_watchdog():
        if watchdog_proc and watchdog_proc.poll() is None:
            try:
                watchdog_proc.terminate()
                watchdog_proc.wait(timeout=5)
                log.info("Watchdog stopped.")
            except Exception:
                watchdog_proc.kill()

    try:
        from scanner import UWOptionsBot
        log.info("Starting scanner session...")
        UWOptionsBot().run()
        log.info("Scanner session complete -- exiting process. "
                 "Task Scheduler will relaunch before next market open.")
    finally:
        _stop_watchdog()

    sys.exit(0)


# ── 2: Single scan ────────────────────────────────────────────────────────────
def run_single_scan():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from scanner import UWOptionsBot

    ET = ZoneInfo("America/New_York")
    now = datetime.now(ET)
    if not (now.weekday() < 5 and 9 <= now.hour < 16):
        ts = now.strftime("%H:%M %Z %a")
        print("")
        print("  [NOTE] Current ET time: " + ts)
        print("  [NOTE] Markets closed -- UW flow alerts only appear during market hours.")
        print("  [NOTE] Running anyway to test connectivity...")
        print("")

    bot = UWOptionsBot()
    bot.ibkr.connect()
    results = bot.scan_cycle()
    bot.ibkr.disconnect()

    sep = "-" * 80
    print("")
    print(sep)
    print("  " + str(len(results)) + " signals scored")
    print(sep)
    print("  {:<7} {:>9} {} {:<12} {:>6}  {:<9}  {:>6}  {:>6}  {:<8}  {}".format(
        "Symbol", "Strike", "R", "Expiry", "Score", "Intent", "Ask", "Delta", "DP", "Blk"
    ))
    print("  " + "-" * 76)
    for s in sorted(results, key=lambda x: x.composite_score, reverse=True)[:15]:
        blk = "[BLK]" if s.gex_regime_blocked else ""
        print("  {:<7} {:>9.2f} {}  {:<12} {:>5.1f}  {:<9}  {:>6.2f}  {:>6.2f}  {:<8}  {}".format(
            s.symbol, s.strike, s.right, s.expiry,
            s.composite_score, s.intent, s.live_ask,
            s.live_delta, s.darkpool_sentiment, blk
        ))
    print(sep)
    print("")


# ── 3: Dealer snapshot ────────────────────────────────────────────────────────
def run_dealer_snapshot():
    from data.uw_client import UWClient
    from config import cfg

    uw = UWClient()
    print("")
    print("  Dealer Exposure -- " + str(len(cfg.WATCHLIST)) + " symbols")
    print("-" * 78)
    print("  {:<8} {:>10} {:>10}  {:<22}  {:>8}  {:>10}  {:>12}".format(
        "Symbol", "GEX ($M)", "DEX ($M)", "Regime", "Flip", "Call Wall", "Flow"
    ))
    print("  " + "-" * 74)

    exposures = uw.get_dealer_exposure_watchlist()
    for sym, e in sorted(exposures.items(), key=lambda x: x[1].net_gex):
        regime_tag = (
            "[+GEX]" if e.regime == "positive_gamma"
            else "[-GEX]" if e.regime == "negative_gamma"
            else "[ GEX]"
        )
        flip = "{:.2f}".format(e.gamma_flip) if e.gamma_flip else "N/A"
        cw   = "{:.2f}".format(e.call_wall)  if e.call_wall  else "N/A"
        print("  {:<8} {:>10.1f} {:>10.1f}  {} {:<18}  {:>8}  {:>10}  {:>12}".format(
            sym, e.net_gex / 1e6, e.net_dex / 1e6,
            regime_tag, e.regime, flip, cw, e.flow_direction
        ))
    print("")


# ── 4: Health check ───────────────────────────────────────────────────────────
def run_health_check():
    from data.uw_client import UWClient
    from broker.schwab_client import SchwabBroker, USE_SCHWAB_FALLBACK, SCHWAB_AVAILABLE

    print("")
    print("  UW Options Bot -- API Health Check")
    print("-" * 55)

    uw = UWClient()
    results = uw.health_check()

    all_ok = True
    for endpoint, ok in results.items():
        status = "[OK]  " if ok else "[FAIL]"
        print("  {}  {}".format(status, endpoint))
        if not ok:
            all_ok = False

    print("-" * 55)
    if all_ok:
        print("  [OK] All UW endpoints accessible -- bot is ready.")
    else:
        failed = [k for k, v in results.items() if not v]
        print("  [FAIL] Inaccessible: " + ", ".join(failed))
        print("  Check tier at: https://unusualwhales.com/settings/account")

    print("")
    print("  Schwab Fallback")
    print("-" * 55)
    if not USE_SCHWAB_FALLBACK:
        print("  [OFF] Disabled (USE_SCHWAB_FALLBACK=false)")
    elif not SCHWAB_AVAILABLE:
        print("  [FAIL] schwab-py not installed -- pip install schwab-py")
    else:
        sb = SchwabBroker()
        if sb.available:
            print("  [OK] Schwab fallback connected and ready")
        else:
            print("  [FAIL] Run: python broker/schwab.py --auth")
    print("")


# ── 5: Query DB ───────────────────────────────────────────────────────────────
def run_query():
    from database.models_db import get_recent_scan_runs, get_signal_history
    from config import cfg

    print("")
    print("  Recent Scan Runs")
    print("-" * 75)
    runs = get_recent_scan_runs(limit=10)
    if not runs:
        print("  No scan runs found. Run --mode scan first.")
        print("")
        return

    print("  {:>4}  {:^22}  {:>6}  {:>5}  {:>5}  {:>6}  {:>10}  {}".format(
        "ID", "Started", "Dur", "Syms", "Sigs", "Alerts", "SPY GEX", "Regime"
    ))
    print("  " + "-" * 71)
    for r in runs:
        dur = "{:.0f}s".format(r["duration_sec"]) if r["duration_sec"] else "--"
        gex = "${:.1f}M".format(r["spy_gex_m"])   if r["spy_gex_m"]    else "--"
        print("  {:>4}  {:^22}  {:>6}  {:>5}  {:>5}  {:>6}  {:>10}  {}".format(
            r["id"], str(r["started_at"])[:19], dur,
            r["symbols_scanned"], r["signals_logged"],
            r["alerts_fired"], gex, r["spy_regime"] or "--"
        ))

    print("")
    print("  Top Signals (last 7 days, score >= 50)")
    print("-" * 75)
    for sym in cfg.WATCHLIST[:5]:
        hist = get_signal_history(sym, lookback_days=7)
        for s in hist[:3]:
            out = s.get("outcome") or "open"
            print("  {:<6} {:>8.2f}{}  {}  score={:>5.1f}  {:<9}  ask={:>6.2f}  {}".format(
                sym, s["strike"], s["right"], s["expiry"],
                s["composite_score"], s["intent"], s["live_ask"], out
            ))
    print("")


# ── 6: Backtest ───────────────────────────────────────────────────────────────
def run_backtest():
    from database.models_db import get_outcome_stats

    print("")
    print("  Signal Outcome Summary")
    print("-" * 80)
    rows = get_outcome_stats()
    if not rows:
        print("  No completed trades found.")
        print("  Use update_signal_outcome() to record exits.")
        print("")
        return

    print("  {:<7} {:<9} {:<22} {:>6} {:>5} {:>7} {:>9} {:>9} {:>10}".format(
        "Symbol", "Intent", "Regime", "Total", "Wins", "Losses",
        "AvgScore", "AvgP&L", "TotalP&L"
    ))
    print("  " + "-" * 76)
    for r in rows:
        print("  {:<7} {:<9} {:<22} {:>6} {:>5} {:>7} {:>9.1f} ${:>8.2f} ${:>9.2f}".format(
            r["symbol"], r["intent"], r["dealer_regime"],
            r["total"], r["wins"], r["losses"],
            r["avg_score"], r["avg_pnl"], r["total_pnl"]
        ))
    print("")


# ── 7: Token age ──────────────────────────────────────────────────────────────
def run_token_age():
    from broker.schwab_client import _get_token_age_days, USE_SCHWAB_FALLBACK, SCHWAB_TOKEN_PATH

    print("")
    print("  Schwab Token Status")
    print("-" * 40)

    if not USE_SCHWAB_FALLBACK:
        print("  Schwab disabled (USE_SCHWAB_FALLBACK=false)")
        print("")
        return

    age = _get_token_age_days(SCHWAB_TOKEN_PATH)
    remaining = 7.0 - age
    print("  Token age  : {:.1f} days".format(age))
    print("  Days left  : {:.1f} days".format(max(remaining, 0)))

    if remaining <= 0:
        print("  Status     : [EXPIRED]")
        print("  Action     : python broker/schwab.py --auth")
    elif remaining <= 2:
        print("  Status     : [WARNING] Expires soon")
        print("  Action     : python broker/schwab.py --auth")
    else:
        print("  Status     : [OK]")
        print("  Note       : Re-auth only needed if stopped 7+ days")
    print("")


# ── 8: API stats ──────────────────────────────────────────────────────────────
def run_api_stats():
    from data.uw_client import UWClient

    print("")
    print("  UW API Usage Stats")
    print("-" * 55)

    uw = UWClient()
    uw._get("/api/news/headlines", {"limit": 1})
    u = uw.get_usage_stats()

    print("  Daily used     : {:>7,} / {:,}  [{}]".format(
        u["daily_used"], u["daily_limit"], u["daily_status"]))
    print("  Daily used %   : {:>7.1f}%".format(u["daily_pct"]))
    print("  Daily remaining: {:>7,}".format(u["daily_remaining"]))
    print("  Resets at      : {}  (in ~{} hrs)".format(
        u["reset_time_et"], u["resets_in_hours"]))
    print("")
    print("  Per-minute used    : {:>5}".format(u["minute_used"]))
    print("  Per-minute remaining: {:>4}".format(u["minute_remain"]))
    if u["minute_reset_sec"]:
        print("  Resets in          : {:>4.1f}s".format(u["minute_reset_sec"]))
    print("")

    scans_left = u["daily_remaining"] // 15
    print("  Est. scans remaining : {:,}  (~15 calls/scan)".format(scans_left))
    print("  At 3-min intervals   : {:.1f} hours".format(scans_left * 3 / 60))
    print("")

    if u["daily_remaining"] < 100:
        print("  [CRITICAL] Stop non-essential requests. Resets at 8 PM ET.")
    elif u["daily_pct"] >= 80:
        print("  [WARNING] Throttle to essential requests only.")
    elif u["daily_pct"] >= 50:
        print("  [INFO] Moderate usage -- consider batching.")
    else:
        print("  [OK] Plenty of headroom.")
    print("")


# ── 9: ML status ──────────────────────────────────────────────────────────────
def run_ml_status():
    from ml.trainer import print_training_status
    print_training_status()


# ── 10: ML train ──────────────────────────────────────────────────────────────
def run_ml_train():
    from ml.trainer import train_all
    train_all()


# ── 11: Watchlist preview ─────────────────────────────────────────────────────
def run_watchlist():
    from data.uw_client import UWClient
    from config import cfg

    print("")
    print("  Dynamic Watchlist Preview")
    print("-" * 55)
    print("  Mode    : {}".format(cfg.WATCHLIST_MODE))
    print("  Size    : up to {} symbols".format(cfg.WATCHLIST_SIZE))
    print("  Anchors : {}".format(", ".join(cfg.WATCHLIST_ANCHORS)))
    print("  Min prem: ${:,.0f}".format(cfg.WATCHLIST_MIN_PREMIUM))
    print("")

    uw = UWClient()
    wl = uw.get_dynamic_watchlist(
        max_symbols=cfg.WATCHLIST_SIZE,
        min_premium=cfg.WATCHLIST_MIN_PREMIUM,
        anchor_symbols=cfg.WATCHLIST_ANCHORS,
    )
    print("  Symbols ({}):".format(len(wl)))
    for i, sym in enumerate(wl, 1):
        anchor = "[anchor]" if sym in cfg.WATCHLIST_ANCHORS else ""
        print("    {:>3}. {:<8} {}".format(i, sym, anchor))
    print("")
    print("  API calls this scan cycle : ~{}".format(len(wl) * 4))
    print("  Estimated daily usage     : ~{:,} / 20,000".format(len(wl) * 4 * 78))
    print("")


# ── 12: Add position ──────────────────────────────────────────────────────────
def run_add_position():
    from database.models_db import add_position

    print("")
    print("  Add Position for Exit Alert Tracking")
    print("-" * 45)
    print("  (Bot alerts when target/stop hit -- no orders placed)")
    print("")
    symbol    = input("  Symbol (e.g. QQQ): ").strip().upper()
    strike    = float(input("  Strike (e.g. 720): ").strip())
    right     = input("  Right C or P: ").strip().upper()
    expiry    = input("  Expiry YYYY-MM-DD: ").strip()
    contracts = int(input("  Contracts: ").strip())
    entry     = float(input("  Entry premium paid (e.g. 5.42): ").strip())

    target = round(entry * 1.65, 2)
    stop   = round(entry * 0.30, 2)

    print("")
    print("  Summary:")
    print("  {} {}{}  {}  x{}  entry={:.2f}".format(
        symbol, strike, right, expiry, contracts, entry))
    print("  Target: {:.2f}  (+65%)".format(target))
    print("  Stop:   {:.2f}  (-70%)".format(stop))
    print("")

    confirm = input("  Add this position? (y/n): ").strip().lower()
    if confirm == "y":
        pos_id = add_position(
            symbol, strike, right, expiry,
            contracts, entry, target, stop
        )
        print("  [OK] Position added (ID: {})".format(pos_id))
        print("  Bot will alert when target/stop is hit.")
    else:
        print("  Cancelled.")
    print("")


# ── 13: View positions ────────────────────────────────────────────────────────
def run_positions():
    from database.models_db import get_open_positions, get_position_summary
    from datetime import date

    print("")
    print("  Open Positions")
    print("-" * 70)
    positions = get_open_positions()
    if not positions:
        print("  No open positions.")
        print("  Use --mode addposition to add one.")
        print("")
    else:
        print("  {:>4}  {:<6} {:>8} {} {:<12} {:>5}  {:>7}  {:>7}  {:>7}  {}".format(
            "ID", "Symbol", "Strike", "R", "Expiry", "Qty",
            "Entry", "Target", "Stop", "DTE"))
        print("  " + "-" * 66)
        for p in positions:
            try:
                dte = (date.fromisoformat(p["expiry"]) - date.today()).days
            except Exception:
                dte = "?"
            print("  {:>4}  {:<6} {:>8.2f} {}  {:<12} {:>5}  {:>7.2f}  {:>7.2f}  {:>7.2f}  {}".format(
                p["id"], p["symbol"], p["strike"], p["right"],
                p["expiry"], p["contracts"],
                p["entry_premium"], p["target_premium"], p["stop_premium"],
                dte))
        print("")

    print("  Closed Position Summary")
    print("-" * 70)
    summary = get_position_summary()
    if not summary:
        print("  No closed positions yet.")
    else:
        print("  {:<7} {} {:<12} {:>6} {:>5} {:>7} {:>12}".format(
            "Symbol", "R", "Exit Reason", "Trades", "Wins", "Losses", "Total P&L"))
        print("  " + "-" * 60)
        for r in summary:
            print("  {:<7} {} {:<12} {:>6} {:>5} {:>7} ${:>10,.2f}".format(
                r["symbol"], r["right"], r["exit_reason"] or "--",
                r["trades"], r["wins"], r["losses"], r["total_pnl"] or 0))
    print("")


# ── 14: Purge ─────────────────────────────────────────────────────────────────
def run_purge():
    from database.purge import run_purge as do_purge, get_db_stats

    print("")
    print("  DB Stats before purge")
    print("-" * 50)
    stats = get_db_stats()
    for table, count in stats.items():
        if table == "db_size_mb":
            print("  DB size: {} MB".format(count))
        else:
            label = "[ARCHIVE]" if "archived" in table else "        "
            print("  {} {:<35} {:>8,}".format(label, table, count))

    print("")
    confirm = input("  Run purge now? (y/n): ").strip().lower()
    if confirm != "y":
        print("  Cancelled.")
        print("")
        return

    print("")
    print("  Running purge...")
    results = do_purge(dry_run=False)

    print("")
    print("  Purge Results")
    print("-" * 50)
    total = 0
    for table, count in results.items():
        if count > 0:
            print("  {:<30} {:>8,} rows archived".format(table, count))
            total += count
    if total == 0:
        print("  Nothing to purge -- all data is current.")
    else:
        print("-" * 50)
        print("  Total archived: {:,} rows".format(total))
        print("  Data moved to archive tables (not deleted).")
    print("")


# ── 15: DB stats ──────────────────────────────────────────────────────────────
def run_dbstats():
    from database.purge import get_db_stats

    print("")
    print("  Database Statistics")
    print("-" * 55)
    stats = get_db_stats()
    active_total  = 0
    archive_total = 0

    print("  Active tables:")
    for table, count in stats.items():
        if table == "db_size_mb":
            continue
        if "archived" not in table:
            print("    {:<35} {:>8,}".format(table, count))
            active_total += count

    print("")
    print("  Archive tables:")
    for table, count in stats.items():
        if table == "db_size_mb":
            continue
        if "archived" in table:
            print("    {:<35} {:>8,}".format(table, count))
            archive_total += count

    print("")
    print("  Active rows  : {:,}".format(active_total))
    print("  Archive rows : {:,}".format(archive_total))
    print("  DB size      : {} MB".format(stats.get("db_size_mb", 0)))
    print("")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="UW Options Bot v2.0 -- Pal Initiatives LLC",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  run           Full continuous scan loop (default)
  scan          Single cycle
  dealer        GEX/DEX snapshot
  check         API health check          <- run first
  query         Recent scan runs from DB
  backtest      Win/loss summary from DB
  tokenage      Schwab token age
  apistats      UW API usage stats
  mlstatus      ML training data status
  train         Train ML models
  watchlist     Preview dynamic watchlist
  addposition   Add position for exit tracking
  positions     View open positions + P&L
  purge         Archive expired/closed data
  dbstats       Database row counts + size
        """,
    )
    parser.add_argument(
        "--mode",
        choices=[
            "run", "scan", "dealer", "check",
            "query", "backtest", "tokenage", "apistats",
            "mlstatus", "train", "watchlist",
            "addposition", "positions", "purge", "dbstats",
        ],
        default="run",
    )
    args = parser.parse_args()

    # All 15 functions defined above -- dispatch dict always resolves correctly
    {
        "run":          run_loop,
        "scan":         run_single_scan,
        "dealer":       run_dealer_snapshot,
        "check":        run_health_check,
        "query":        run_query,
        "backtest":     run_backtest,
        "tokenage":     run_token_age,
        "apistats":     run_api_stats,
        "mlstatus":     run_ml_status,
        "train":        run_ml_train,
        "watchlist":    run_watchlist,
        "addposition":  run_add_position,
        "positions":    run_positions,
        "purge":        run_purge,
        "dbstats":      run_dbstats,
    }[args.mode]()
