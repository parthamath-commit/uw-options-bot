"""
weight_analysis.py -- Evidence-based scoring weight optimizer
=============================================================
Reads score_evidence and (once outcomes are labeled) correlates each
scoring component against win/loss to recommend optimal weights.

Run anytime:
    python weight_analysis.py

Or export to CSV for Excel analysis:
    python weight_analysis.py --csv

Output:
  1. Evidence distribution -- how often each component fires and at what value
  2. Correlation with outcome -- which components actually predict wins
  3. Current vs recommended weights
  4. Score distribution by tier (how many prints per tier)
"""
import sqlite3
import argparse
import csv
import sys
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "output" / "uw_bot.db"
SEP  = "=" * 68
SEP2 = "-" * 68


def pct(n, total):
    return "{:.1f}%".format(100 * n / total) if total else "n/a"


def avg(vals):
    return sum(vals) / len(vals) if vals else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", action="store_true",
                        help="Export raw evidence rows to evidence_export.csv")
    parser.add_argument("--days", type=int, default=30,
                        help="Look-back window in days (default 30)")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print("[ERR] DB not found: {}".format(DB_PATH))
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Check evidence table exists and has rows
    total = conn.execute("SELECT COUNT(*) FROM score_evidence").fetchone()[0]
    if total == 0:
        print()
        print("No evidence data yet.")
        print("Run the bot for at least one market-hours cycle, then re-run this script.")
        sys.exit(0)

    print()
    print(SEP)
    print("  SCORING WEIGHT ANALYSIS")
    print("  {} evidence rows  |  last {} days".format(total, args.days))
    print("  {}".format(datetime.now().strftime("%Y-%m-%d %H:%M")))
    print(SEP)

    # ── 1. Score distribution ─────────────────────────────────────────────
    print()
    print("COMPOSITE SCORE DISTRIBUTION")
    print(SEP2)
    print("  {:<14} {:>8} {:>8} {:>10} {:>10}".format(
        "Tier", "Prints", "%", "Avg Add", "Avg Inst"))
    print("  " + "-" * 52)
    tiers = [
        ("0-49",   0,  49),
        ("50-59",  50, 59),
        ("60-69",  60, 69),
        ("70-79",  70, 79),
        ("80-89",  80, 89),
        ("90-100", 90, 100),
    ]
    for label, lo, hi in tiers:
        row = conn.execute("""
            SELECT COUNT(*), AVG(additive_total), AVG(institutional_total)
            FROM score_evidence
            WHERE composite_score >= ? AND composite_score <= ?
            AND scored_at >= date('now', '-{} days', 'localtime')
        """.format(args.days), (lo, hi)).fetchone()
        n = row[0] or 0
        print("  {:<14} {:>8} {:>8} {:>10} {:>10}".format(
            label, n, pct(n, total),
            "{:.1f}".format(row[1]) if row[1] else "n/a",
            "{:.1f}".format(row[2]) if row[2] else "n/a"))

    # ── 2. Component contribution analysis ───────────────────────────────
    print()
    print("ADDITIVE COMPONENT CONTRIBUTIONS (avg points each component adds)")
    print(SEP2)
    components = [
        ("Structure",    "ev_structure"),
        ("Intent",       "ev_intent"),
        ("UW Score",     "ev_uw_score"),
        ("IV Bonus",     "ev_iv_bonus"),
        ("GEX Regime",   "ev_gex_regime"),
        ("Dark Pool",    "ev_darkpool"),
        ("Premium Size", "ev_premium_size"),
    ]
    print("  {:<16} {:>10} {:>10} {:>10} {:>10}".format(
        "Component", "Avg Pts", "Max Pts", "% Non-Zero", "Max Possible"))
    print("  " + "-" * 58)
    max_possible = {
        "ev_structure": 20, "ev_intent": 15, "ev_uw_score": 25,
        "ev_iv_bonus": 15,  "ev_gex_regime": 10, "ev_darkpool": 8,
        "ev_premium_size": 15
    }
    for label, col in components:
        row = conn.execute("""
            SELECT AVG({}), MAX({}),
                   SUM(CASE WHEN {} != 0 THEN 1 ELSE 0 END)
            FROM score_evidence
            WHERE scored_at >= date('now', '-{} days', 'localtime')
        """.format(col, col, col, args.days)).fetchone()
        print("  {:<16} {:>10} {:>10} {:>10} {:>10}".format(
            label,
            "{:.2f}".format(row[0]) if row[0] is not None else "n/a",
            "{:.1f}".format(row[1]) if row[1] is not None else "n/a",
            pct(row[2] or 0, total),
            max_possible.get(col, "?")))

    print()
    print("INSTITUTIONAL COMPONENT CONTRIBUTIONS")
    print(SEP2)
    inst_components = [
        ("VEX (Vanna)",   "ev_vex",      10),
        ("CHEX (Charm)",  "ev_chex",     8),
        ("Flow Dir",      "ev_flow_dir", 15),
        ("DEX (Delta)",   "ev_dex",      8),
        ("Ask-Side",      "ev_ask_side", 5),
    ]
    print("  {:<16} {:>10} {:>10} {:>10} {:>10}".format(
        "Component", "Avg Pts", "Fires %", "Helps %", "Hurts %"))
    print("  " + "-" * 58)
    for label, col, max_p in inst_components:
        row = conn.execute("""
            SELECT AVG({}),
                   SUM(CASE WHEN {} > 0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN {} < 0 THEN 1 ELSE 0 END)
            FROM score_evidence
            WHERE scored_at >= date('now', '-{} days', 'localtime')
        """.format(col, col, col, args.days)).fetchone()
        print("  {:<16} {:>10} {:>10} {:>10} {:>10}".format(
            label,
            "{:.2f}".format(row[0]) if row[0] is not None else "n/a",
            pct(row[1] or 0, total),
            pct(row[1] or 0, total),
            pct(row[2] or 0, total)))

    # ── 3. Outcome correlation (only if outcomes exist) ───────────────────
    outcome_count = conn.execute("""
        SELECT COUNT(*) FROM score_evidence WHERE outcome IS NOT NULL
    """).fetchone()[0]

    if outcome_count > 0:
        print()
        print("COMPONENT CORRELATION WITH WIN OUTCOME ({} labeled)".format(outcome_count))
        print(SEP2)
        print("  Components where wins had higher average value than losses:")
        print()
        print("  {:<16} {:>10} {:>10} {:>10}".format(
            "Component", "Wins Avg", "Losses Avg", "Δ (signal)"))
        print("  " + "-" * 50)

        all_cols = [c for _, c in components] + [c for _, c, _ in inst_components]
        all_labels = [l for l, _ in components] + [l for l, _, _ in inst_components]

        rows_data = []
        for label, col in zip(all_labels, all_cols):
            row = conn.execute("""
                SELECT
                    AVG(CASE WHEN outcome='win'  THEN {} END),
                    AVG(CASE WHEN outcome='loss' THEN {} END)
                FROM score_evidence
                WHERE outcome IS NOT NULL
            """.format(col, col)).fetchone()
            w = row[0]
            l = row[1]
            if w is None and l is None:
                continue
            delta = (w or 0) - (l or 0)
            rows_data.append((label, w, l, delta))

        # Sort by signal strength (biggest delta first)
        rows_data.sort(key=lambda x: abs(x[3]), reverse=True)
        for label, w, l, delta in rows_data:
            arrow = "↑ RAISE WEIGHT" if delta > 2 else ("↓ LOWER WEIGHT" if delta < -2 else "  ok")
            print("  {:<16} {:>10} {:>10} {:>10}  {}".format(
                label,
                "{:.2f}".format(w) if w is not None else "n/a",
                "{:.2f}".format(l) if l is not None else "n/a",
                "{:+.2f}".format(delta),
                arrow))

        # Win rate by score tier
        print()
        print("WIN RATE BY SCORE TIER (closed trades only)")
        print(SEP2)
        print("  {:<14} {:>8} {:>8} {:>8} {:>10}".format(
            "Tier", "Closed", "Wins", "Losses", "Win Rate"))
        print("  " + "-" * 52)
        for label, lo, hi in tiers:
            row = conn.execute("""
                SELECT COUNT(*),
                       SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END),
                       SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END)
                FROM score_evidence
                WHERE composite_score >= ? AND composite_score <= ?
                AND outcome IN ('win','loss')
            """, (lo, hi)).fetchone()
            n, w, l = row[0] or 0, row[1] or 0, row[2] or 0
            wr = "{:.0f}%".format(100 * w / (w+l)) if (w+l) > 0 else "n/a"
            note = ""
            if (w+l) >= 10:
                if w/(w+l) >= 0.70: note = "  ← GOOD TIER"
                elif w/(w+l) < 0.45: note = "  ← REVIEW THRESHOLD"
            print("  {:<14} {:>8} {:>8} {:>8} {:>10}{}".format(
                label, n, w, l, wr, note))
    else:
        print()
        print("  (outcome correlation available once outcome_tracker.py has labeled data)")

    # ── 4. IV percentile distribution ────────────────────────────────────
    print()
    print("IV PERCENTILE DISTRIBUTION (are signals mostly high or low IV?)")
    print(SEP2)
    iv_buckets = [(0,30,"Low (<30)"),(30,50,"Mid (30-50)"),(50,70,"Elevated (50-70)"),(70,100,"High (>70)")]
    for lo, hi, label in iv_buckets:
        n = conn.execute("""
            SELECT COUNT(*) FROM score_evidence
            WHERE iv_pct >= ? AND iv_pct < ?
            AND scored_at >= date('now','-{} days','localtime')
        """.format(args.days), (lo, hi)).fetchone()[0]
        print("  {:<22} {:>8}  {}".format(label, n, pct(n, total)))

    # ── 5. Dark pool confirmation rate ────────────────────────────────────
    print()
    print("DARK POOL CONFIRMATION RATE")
    print(SEP2)
    rows = conn.execute("""
        SELECT darkpool_sent,
               COUNT(*),
               AVG(composite_score)
        FROM score_evidence
        WHERE scored_at >= date('now','-{} days','localtime')
        GROUP BY darkpool_sent
        ORDER BY 2 DESC
    """.format(args.days)).fetchall()
    for r in rows:
        print("  {:<16} {:>8} signals  avg_score={:.1f}".format(
            r[0] or "none", r[1], r[2] or 0))

    # ── 6. Current weights vs data-suggested ─────────────────────────────
    print()
    print("CURRENT WEIGHTS (scoring/utils.py)")
    print(SEP2)
    print("  Additive:      55%  (structure, intent, UW score, IV, GEX, darkpool, premium)")
    print("  Institutional: 45%  (VEX, CHEX, flow_dir, DEX, ask_side)")
    print()
    print("  COMPONENTS BY MAXIMUM POSSIBLE POINTS:")
    print("  Additive max   = 20+15+25+15+10+8+15 = 108 pts (uncapped, then / 100)")
    print("  Institutional  = 50 baseline +10+8+15+8+5 = max 96 pts")
    print()
    if outcome_count >= 30:
        print("  ✅ You have {} labeled outcomes -- run outcome_tracker.py --redo".format(outcome_count))
        print("     then re-run this script for data-driven weight recommendations.")
    else:
        print("  ⏳ Need {} more labeled outcomes for weight recommendations.".format(
            max(0, 30 - outcome_count)))
        print("     Run: python outcome_tracker.py")

    # ── 7. CSV export ─────────────────────────────────────────────────────
    if args.csv:
        csv_path = Path(__file__).parent / "output" / "evidence_export.csv"
        rows = conn.execute("""
            SELECT * FROM score_evidence
            ORDER BY scored_at DESC
            LIMIT 10000
        """).fetchall()
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([d[0] for d in conn.execute(
                "SELECT * FROM score_evidence LIMIT 0").description])
            w.writerows(rows)
        print()
        print("  CSV exported to: {}".format(csv_path))

    print()
    print(SEP)
    print()
    conn.close()


if __name__ == "__main__":
    main()
