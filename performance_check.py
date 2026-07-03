"""
performance_check.py -- UW Options Bot v2.0 Performance Dashboard

Run from the bot folder:
    python performance_check.py

Shows:
  - Daily signal volume and score distribution (last 7 days)
  - Top symbols by conviction
  - Alert outcome tracking (if outcomes have been labeled)
  - Feed activity breakdown
  - ML training readiness
  - DB size / row counts
"""
import sqlite3
import os
from pathlib import Path
from datetime import date, datetime

DB_PATH = Path(__file__).parent / "output" / "uw_bot.db"

if not DB_PATH.exists():
    print("[ERR] Database not found at: {}".format(DB_PATH))
    print("      Make sure the bot has run at least one cycle.")
    raise SystemExit(1)

conn = sqlite3.connect(str(DB_PATH))

SEP  = "=" * 60
SEP2 = "-" * 60

def fmt_prem(v):
    if v is None or v == 0:
        return "n/a"
    if v >= 1_000_000:
        return "${:.1f}M".format(v / 1_000_000)
    return "${:.0f}k".format(v / 1_000)

print()
print(SEP)
print("  UW OPTIONS BOT v2.0 -- PERFORMANCE DASHBOARD")
print("  {}".format(datetime.now().strftime("%Y-%m-%d %H:%M")))
print(SEP)

# ── 1. Daily signal summary ────────────────────────────────────────────────
print()
print("DAILY SIGNAL SUMMARY (last 7 trading days)")
print(SEP2)
print("  {:<12} {:>7} {:>9} {:>10} {:>12}".format(
    "Date", "Scored", "Avg Score", "Alerts(≥70)", "Options Flow"))
print("  " + "-" * 52)
rows = conn.execute("""
    SELECT
        date(scored_at)                                         AS day,
        COUNT(*)                                                AS total,
        ROUND(AVG(composite_score), 1)                         AS avg_score,
        SUM(CASE WHEN composite_score >= 70 THEN 1 ELSE 0 END) AS alerts,
        SUM(CASE WHEN signal_type = 'unidirectional'
                   OR signal_type IS NULL THEN 1 ELSE 0 END)   AS options_ct
    FROM scored_signals
    WHERE date(scored_at) >= date('now', '-7 days', 'localtime')
    GROUP BY day
    ORDER BY day DESC
""").fetchall()

if not rows:
    print("  No data yet.")
else:
    for r in rows:
        print("  {:<12} {:>7} {:>9} {:>10} {:>12}".format(
            r[0], r[1], r[2] or 0, r[3] or 0, r[4] or 0))

# ── 2. Top symbols ──────────────────────────────────────────────────────────
print()
print("TOP 10 SYMBOLS BY CONVICTION (all time, composite_score ≥ 70)")
print(SEP2)
print("  {:<8} {:>8} {:>10} {:>12} {:>14}".format(
    "Symbol", "Signals", "Avg Score", "Best Score", "Total Premium"))
print("  " + "-" * 56)
rows = conn.execute("""
    SELECT
        symbol,
        COUNT(*)                           AS n,
        ROUND(AVG(composite_score), 1)     AS avg_s,
        ROUND(MAX(composite_score), 1)     AS best_s,
        SUM(COALESCE(premium, 0))          AS prem
    FROM scored_signals
    WHERE composite_score >= 70
    GROUP BY symbol
    ORDER BY n DESC
    LIMIT 10
""").fetchall()

if not rows:
    print("  No qualifying signals yet.")
else:
    for r in rows:
        print("  {:<8} {:>8} {:>10} {:>12} {:>14}".format(
            r[0], r[1], r[2] or 0, r[3] or 0, fmt_prem(r[4])))

# ── 3. Top contracts (options flow with strike) ─────────────────────────────
print()
print("TOP 10 OPTIONS CONTRACTS (strike > 0, all time)")
print(SEP2)
print("  {:<8} {:>8} {:>5} {:>12} {:>10} {:>14}".format(
    "Symbol", "Strike", "Type", "Expiry", "Best Scr", "Total Prem"))
print("  " + "-" * 62)
rows = conn.execute("""
    SELECT
        symbol,
        strike,
        right,
        expiry,
        ROUND(MAX(composite_score), 1)     AS best_s,
        SUM(COALESCE(premium, 0))          AS prem
    FROM scored_signals
    WHERE strike > 0
    GROUP BY symbol, strike, right, expiry
    ORDER BY prem DESC
    LIMIT 10
""").fetchall()

if not rows:
    print("  No options contracts in DB yet.")
else:
    for r in rows:
        print("  {:<8} {:>8} {:>5} {:>12} {:>10} {:>14}".format(
            r[0], r[1] or 0, r[2] or "?", r[3] or "?", r[4] or 0,
            fmt_prem(r[5])))

# ── 4. Feed activity breakdown ──────────────────────────────────────────────
print()
print("FEED ACTIVITY (last 7 days, all scored signals)")
print(SEP2)
rows = conn.execute("""
    SELECT
        COALESCE(signal_type, 'unknown')   AS feed,
        COUNT(*)                           AS n,
        ROUND(AVG(composite_score), 1)     AS avg_s,
        SUM(CASE WHEN composite_score >= 70 THEN 1 ELSE 0 END) AS alerts
    FROM scored_signals
    WHERE date(scored_at) >= date('now', '-7 days', 'localtime')
    GROUP BY signal_type
    ORDER BY n DESC
""").fetchall()

if not rows:
    print("  No data yet.")
else:
    print("  {:<20} {:>8} {:>10} {:>10}".format("Feed", "Signals", "Avg Score", "Alerts(≥70)"))
    print("  " + "-" * 52)
    for r in rows:
        print("  {:<20} {:>8} {:>10} {:>10}".format(r[0], r[1], r[2] or 0, r[3] or 0))

# ── 5. Outcome tracking ─────────────────────────────────────────────────────
print()
print("ALERT OUTCOMES BY SCORE TIER")
print(SEP2)
total_outcomes = conn.execute(
    "SELECT COUNT(*) FROM scored_signals WHERE outcome IS NOT NULL"
).fetchone()[0]

if total_outcomes == 0:
    print("  No outcomes yet. Run: python outcome_tracker.py")
    print("  (requires Schwab token + at least 1-day-old signals)")
else:
    # Overall summary
    rows = conn.execute("""
        SELECT outcome, COUNT(*),
               ROUND(AVG(pnl_usd), 2),
               ROUND(SUM(pnl_usd), 2)
        FROM scored_signals
        WHERE outcome IS NOT NULL
        GROUP BY outcome ORDER BY 2 DESC
    """).fetchall()
    print("  OVERALL:")
    print("  {:<12} {:>8} {:>12} {:>12}".format("Outcome", "Count", "Avg PnL$", "Total PnL$"))
    print("  " + "-" * 48)
    wins = losses = 0
    for r in rows:
        if r[0] == "win":   wins = r[1]
        if r[0] == "loss":  losses = r[1]
        print("  {:<12} {:>8} {:>12} {:>12}".format(
            r[0], r[1],
            "${:.2f}".format(r[2]) if r[2] is not None else "n/a",
            "${:.2f}".format(r[3]) if r[3] is not None else "n/a"))
    if wins + losses > 0:
        print("  Win rate: {:.1f}%".format(wins / (wins + losses) * 100))

    # Breakdown by score tier (the KEY analysis)
    print()
    print("  BY SCORE TIER (win rate -- should scores < 70 still alert?):")
    print("  {:<14} {:>7} {:>7} {:>7} {:>10}".format(
        "Score Tier", "Signals", "Wins", "Losses", "Win Rate"))
    print("  " + "-" * 48)
    tiers = [
        ("50-59",  50, 59),
        ("60-69",  60, 69),
        ("70-79",  70, 79),
        ("80-89",  80, 89),
        ("90-100", 90, 100),
    ]
    for (label, lo, hi) in tiers:
        row = conn.execute("""
            SELECT COUNT(*),
                   SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END),
                   SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END)
            FROM scored_signals
            WHERE composite_score >= ? AND composite_score <= ?
            AND outcome IS NOT NULL AND outcome != 'open'
        """, (lo, hi)).fetchone()
        n, w, l = row[0] or 0, row[1] or 0, row[2] or 0
        wr = "{:.0f}%".format(w / (w + l) * 100) if (w + l) > 0 else "n/a"
        print("  {:<14} {:>7} {:>7} {:>7} {:>10}".format(label, n, w, l, wr))

    # By intent
    print()
    print("  BY DIRECTION (bullish vs bearish win rate):")
    print("  {:<12} {:>7} {:>7} {:>7} {:>10}".format(
        "Intent", "Signals", "Wins", "Losses", "Win Rate"))
    print("  " + "-" * 44)
    for intent in ("bullish", "bearish", "neutral"):
        row = conn.execute("""
            SELECT COUNT(*),
                   SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END),
                   SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END)
            FROM scored_signals
            WHERE intent = ? AND outcome IS NOT NULL AND outcome != 'open'
        """, (intent,)).fetchone()
        n, w, l = row[0] or 0, row[1] or 0, row[2] or 0
        wr = "{:.0f}%".format(w / (w + l) * 100) if (w + l) > 0 else "n/a"
        print("  {:<12} {:>7} {:>7} {:>7} {:>10}".format(intent, n, w, l, wr))

# ── 6. Dealer exposure history ──────────────────────────────────────────────
print()
print("DEALER EXPOSURE SNAPSHOTS (last 7 days)")
print(SEP2)
rows = conn.execute("""
    SELECT
        date(captured_at)      AS day,
        COUNT(DISTINCT symbol) AS symbols,
        COUNT(*)               AS snapshots
    FROM dealer_exposure
    WHERE date(captured_at) >= date('now', '-7 days', 'localtime')
    GROUP BY day
    ORDER BY day DESC
""").fetchall()

if not rows:
    print("  No dealer exposure data yet.")
else:
    print("  {:<12} {:>10} {:>12}".format("Date", "Symbols", "Snapshots"))
    print("  " + "-" * 36)
    for r in rows:
        print("  {:<12} {:>10} {:>12}".format(r[0], r[1], r[2]))

# ── 7. ML readiness ─────────────────────────────────────────────────────────
print()
print("ML TRAINING READINESS")
print(SEP2)
total    = conn.execute("SELECT COUNT(*) FROM scored_signals").fetchone()[0]
labeled  = conn.execute("SELECT COUNT(*) FROM scored_signals WHERE outcome IS NOT NULL").fetchone()[0]
high_q   = conn.execute("SELECT COUNT(*) FROM scored_signals WHERE composite_score >= 70").fetchone()[0]
archived = conn.execute("SELECT COUNT(*) FROM archived_scored_signals").fetchone()[0]

print("  Total signals scored  : {:,}".format(total))
print("  High-quality (≥70)    : {:,}".format(high_q))
print("  Labeled outcomes      : {:,}  (need 30+ for ML training)".format(labeled))
print("  Archived signals      : {:,}".format(archived))
ml_ready = labeled >= 30 and total >= 50
print("  ML training ready     : {}".format("YES -- run: python main.py --mode train" if ml_ready else "NO"))

# ── 8. DB size ───────────────────────────────────────────────────────────────
print()
print("DATABASE")
print(SEP2)
db_size = DB_PATH.stat().st_size / 1024 / 1024
print("  File: {}".format(DB_PATH))
print("  Size: {:.2f} MB".format(db_size))

tables = conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
).fetchall()
print("  Tables:")
for (tname,) in tables:
    try:
        n = conn.execute("SELECT COUNT(*) FROM '{}'".format(tname)).fetchone()[0]
        print("    {:<40} {:>8,} rows".format(tname, n))
    except Exception:
        pass

print()
print(SEP)
print("  Run 'python main.py --mode check' for live health check.")
print(SEP)
print()
conn.close()
