"""
Profitability analysis of UW Options Bot signals.
Run from bot directory: python analyze_pnl.py
"""
from database.db import get_connection
from datetime import date, datetime

with get_connection() as conn:

    print("=" * 60)
    print("UW OPTIONS BOT — SIGNAL PROFITABILITY ANALYSIS")
    print("=" * 60)
    print()

    # 1. Overall signal volume
    total = conn.execute("SELECT COUNT(*) FROM scored_signals").fetchone()[0]
    alerted = conn.execute("""
        SELECT COUNT(*) FROM scored_signals
        WHERE composite_score >= 70 AND live_ask > 0 AND strike > 0
    """).fetchone()[0]
    print(f"Total signals scored:     {total}")
    print(f"Alert-grade signals (≥70): {alerted}")
    print()

    # 2. Score distribution
    print("── Score Distribution ──────────────────────────────")
    buckets = conn.execute("""
        SELECT
            CASE
                WHEN composite_score >= 90 THEN '90-100'
                WHEN composite_score >= 80 THEN '80-89'
                WHEN composite_score >= 70 THEN '70-79'
                WHEN composite_score >= 60 THEN '60-69'
                ELSE '<60'
            END AS bucket,
            COUNT(*) AS cnt
        FROM scored_signals
        GROUP BY bucket
        ORDER BY MIN(composite_score) DESC
    """).fetchall()
    for b in buckets:
        print(f"  Score {b[0]}: {b[1]} signals")
    print()

    # 3. Feed breakdown
    print("── Signals by Feed Type ────────────────────────────")
    feeds = conn.execute("""
        SELECT
            COALESCE(signal_type, 'options_flow') AS feed,
            COUNT(*) AS cnt,
            ROUND(AVG(composite_score), 1) AS avg_score,
            COUNT(CASE WHEN composite_score >= 70 THEN 1 END) AS alerts
        FROM scored_signals
        GROUP BY feed
        ORDER BY cnt DESC
    """).fetchall()
    for f in feeds:
        print(f"  {f[0]:<20} total={f[1]:<5} avg_score={f[2]:<6} alerts={f[3]}")
    print()

    # 4. Intent breakdown
    print("── Intent Breakdown ────────────────────────────────")
    intents = conn.execute("""
        SELECT intent, COUNT(*) as cnt,
               ROUND(AVG(composite_score),1) as avg_score
        FROM scored_signals
        WHERE composite_score >= 70
        GROUP BY intent
        ORDER BY cnt DESC
    """).fetchall()
    for i in intents:
        print(f"  {i[0]:<12} count={i[1]:<5} avg_score={i[2]}")
    print()

    # 5. P&L on currently active signals (live price vs entry)
    print("── Active Signal P&L (entry price vs current) ──────")
    active = conn.execute("""
        SELECT symbol, strike, right, expiry, intent,
               live_ask AS entry, composite_score,
               target_premium, stop_premium,
               MIN(scored_at) AS first_seen
        FROM scored_signals
        WHERE live_ask > 0
          AND composite_score >= 70
          AND strike > 0
          AND expiry >= date('now','localtime')
        GROUP BY symbol, strike, right, expiry
        ORDER BY composite_score DESC
    """).fetchall()
    print(f"  Active non-expired signals: {len(active)}")
    print()

    # 6. Signals by symbol (top 20 most active)
    print("── Top 20 Most Signaled Symbols ────────────────────")
    syms = conn.execute("""
        SELECT symbol, COUNT(*) as cnt,
               ROUND(AVG(composite_score),1) as avg_score,
               SUM(CASE WHEN intent='bullish' THEN 1 ELSE 0 END) as bull,
               SUM(CASE WHEN intent='bearish' THEN 1 ELSE 0 END) as bear
        FROM scored_signals
        WHERE composite_score >= 70
        GROUP BY symbol
        ORDER BY cnt DESC
        LIMIT 20
    """).fetchall()
    for s in syms:
        direction = "MIXED" if s[3] > 0 and s[4] > 0 else ("BULL" if s[3] > 0 else "BEAR")
        print(f"  {s[0]:<8} signals={s[1]:<4} avg={s[2]:<6} {direction} (B{s[3]}/S{s[4]})")
    print()

    # 7. Signal quality issues
    print("── Signal Quality Issues ───────────────────────────")
    # How many signals have no live quote?
    no_quote = conn.execute("""
        SELECT COUNT(*) FROM scored_signals
        WHERE composite_score >= 70
          AND (live_ask IS NULL OR live_ask = 0)
    """).fetchone()[0]
    print(f"  Signals with no live quote: {no_quote}")

    # How many signals have size=0 contracts?
    no_size = conn.execute("""
        SELECT COUNT(*) FROM scored_signals
        WHERE composite_score >= 70
          AND (contracts IS NULL OR contracts = 0)
    """).fetchone()[0]
    print(f"  Signals with 0 contracts sized: {no_size}")

    # How many signals are OTM (strike far from price)?
    otm = conn.execute("""
        SELECT COUNT(*) FROM scored_signals
        WHERE composite_score >= 70
          AND live_ask < 0.50
    """).fetchone()[0]
    print(f"  Signals with entry price < $0.50 (cheap OTM): {otm}")

    # Average premium of alerted signals
    avg_prem = conn.execute("""
        SELECT ROUND(AVG(premium), 0), ROUND(AVG(live_ask), 2)
        FROM scored_signals
        WHERE composite_score >= 70
          AND live_ask > 0
    """).fetchone()
    print(f"  Avg UW premium flow: ${avg_prem[0]:,}")
    print(f"  Avg option ask price: ${avg_prem[1]}")
    print()

    # 8. Dark pool dominant signals
    print("── Context Signal Breakdown ────────────────────────")
    ctx = conn.execute("""
        SELECT
            CASE
                WHEN rule_type = 'unidirectional' THEN 'unidirectional'
                WHEN rule_type = 'conflict' THEN 'conflict'
                WHEN rule_type = 'hedging' THEN 'hedging'
                ELSE rule_type
            END as rtype,
            COUNT(*) as cnt,
            ROUND(AVG(composite_score),1) as avg
        FROM scored_signals
        WHERE composite_score >= 70
        GROUP BY rtype
        ORDER BY cnt DESC
    """).fetchall()
    for r in ctx:
        print(f"  {r[0]:<20} count={r[1]:<5} avg_score={r[2]}")
    print()

    # 9. Recent signals last 24h
    print("── Last 24h Signals ────────────────────────────────")
    recent = conn.execute("""
        SELECT symbol, strike, right, expiry, intent,
               composite_score, live_ask,
               SUBSTR(scored_at, 1, 16) as time
        FROM scored_signals
        WHERE composite_score >= 70
          AND scored_at >= datetime('now', '-24 hours', 'localtime')
          AND live_ask > 0 AND strike > 0
        ORDER BY composite_score DESC
        LIMIT 30
    """).fetchall()
    print(f"  Count: {len(recent)}")
    for r in recent:
        print(f"  {r[7]}  {r[0]:<6} {r[1]}{r[2]} {r[4]:<8} score={r[5]:.0f}  entry=${r[6]}")

print()
print("=" * 60)