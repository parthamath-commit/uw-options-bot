"""
ml/weight_optimizer.py
======================
Learns optimal scoring weights from score_evidence + outcome data.

APPROACH
--------
Phase 1 (enough data, no outcomes yet):
  Uses unsupervised analysis — correlation between component values and
  composite_score, plus distribution statistics — to flag components
  that may be over/under-weighted vs what the data shows.

Phase 2 (30+ labeled outcomes):
  Logistic Regression + Random Forest on score_evidence features
  to find the weight vector that best predicts win/loss.
  Translates learned coefficients back to scorer weights.

Phase 3 (100+ outcomes, cross-validated):
  Adds Bayesian optimization over the weight space to find
  the globally optimal weights, validated with k-fold CV.

USAGE
-----
  python ml/weight_optimizer.py              # auto-selects phase
  python ml/weight_optimizer.py --phase 1    # force phase
  python ml/weight_optimizer.py --apply      # write weights to config

OUTPUT
------
  Prints current vs recommended weights.
  Optionally patches scoring/additive.py and scoring/institutional.py
  with the learned values when --apply is passed.
"""
import os
import sys
import json
import sqlite3
import logging
import argparse
from pathlib import Path
from datetime import datetime

# Ensure project root on path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=ROOT / ".env")
except ImportError:
    pass

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] WeightOptimizer - %(message)s")
log = logging.getLogger("WeightOptimizer")

DB_PATH     = ROOT / "output" / "uw_bot.db"
MODELS_DIR  = ROOT / "ml" / "models"
MODELS_DIR.mkdir(exist_ok=True)

SEP  = "=" * 70
SEP2 = "-" * 70

# ── Component definitions ────────────────────────────────────────────────────
ADDITIVE_COMPONENTS = [
    ("structure",    "ev_structure",    20.0,  "sweep=20, block=12"),
    ("intent",       "ev_intent",       15.0,  "directional=15, neutral=5"),
    ("uw_score",     "ev_uw_score",     25.0,  "UW score scaled 0-25"),
    ("iv_bonus",     "ev_iv_bonus",     15.0,  "low IV=+15, high IV=-5"),
    ("gex_regime",   "ev_gex_regime",   10.0,  "aligned regime=+10"),
    ("darkpool",     "ev_darkpool",      8.0,  "confirms intent=+8"),
    ("premium_size", "ev_premium_size", 15.0,  "≥$5M=+15, ≥$1M=+10"),
]
INST_COMPONENTS = [
    ("vex",       "ev_vex",       10.0, "VEX aligned=+10"),
    ("chex",      "ev_chex",       8.0, "CHEX aligned=+8"),
    ("flow_dir",  "ev_flow_dir",  15.0, "amplifying=+12, regime_flip=+15"),
    ("dex",       "ev_dex",        8.0, "DEX aligned=+8"),
    ("ask_side",  "ev_ask_side",   5.0, "ask-side aggressor=+5"),
]

CURRENT_BLEND = {"additive": 0.55, "institutional": 0.45}


def load_evidence(conn, min_rows=50):
    """Load score_evidence table into lists."""
    rows = conn.execute("""
        SELECT *
        FROM score_evidence
        ORDER BY scored_at DESC
        LIMIT 10000
    """).fetchall()
    if len(rows) < min_rows:
        return None, "Need at least {} evidence rows (have {})".format(min_rows, len(rows))
    return rows, None


def load_labeled(conn, min_rows=30):
    """Load evidence rows that have outcome labels."""
    rows = conn.execute("""
        SELECT e.*, s.outcome, s.pnl_usd
        FROM score_evidence e
        JOIN scored_signals s ON (
            e.symbol  = s.symbol AND
            e.strike  = s.strike AND
            e.right   = s.right  AND
            e.expiry  = s.expiry AND
            date(e.scored_at) = date(s.scored_at)
        )
        WHERE s.outcome IN ('win', 'loss')
        ORDER BY e.scored_at
    """).fetchall()
    if not rows:
        # Fallback: try outcome column directly on score_evidence
        rows = conn.execute("""
            SELECT * FROM score_evidence
            WHERE outcome IN ('win','loss')
            ORDER BY scored_at
        """).fetchall()
    if len(rows) < min_rows:
        return None, "Need at least {} labeled outcomes (have {})".format(min_rows, len(rows))
    return rows, None


# ── Phase 1: Statistical analysis ────────────────────────────────────────────
def phase1_stats(rows, conn):
    """
    Statistical analysis without outcome labels.
    Finds components that vary widely (good signal) vs those that are
    mostly zero or constant (low discriminative value).
    """
    print()
    print("PHASE 1 — STATISTICAL WEIGHT ANALYSIS")
    print(SEP2)
    print("(No outcomes needed — based on component distribution)")
    print()

    all_additive = [dict(r) for r in rows]

    print("  {:<16} {:>9} {:>9} {:>9} {:>9} {:>9}".format(
        "Component", "Mean", "Std", "Zero%", "Max", "Useful?"))
    print("  " + "-" * 60)

    recommendations = {}

    for name, col, max_pts, desc in ADDITIVE_COMPONENTS + INST_COMPONENTS:
        vals = [r[col] for r in all_additive if r.get(col) is not None]
        if not vals:
            continue
        mean  = sum(vals) / len(vals)
        var   = sum((v - mean)**2 for v in vals) / len(vals)
        std   = var ** 0.5
        zeros = sum(1 for v in vals if v == 0) / len(vals) * 100
        maxv  = max(vals)

        # Usefulness heuristic:
        # High std relative to mean = discriminating well
        # Very high zero% = rarely fires = low value
        if zeros > 85 and std < 1.5:
            useful = "⚠️  RARELY FIRES"
            recommendations[name] = max(max_pts * 0.5, 2)
        elif std / (max_pts + 0.001) > 0.25:
            useful = "✅  HIGH VARIANCE"
            recommendations[name] = max_pts
        else:
            useful = "   moderate"
            recommendations[name] = max_pts * 0.8

        print("  {:<16} {:>9.2f} {:>9.2f} {:>8.0f}% {:>9.1f}  {}".format(
            name, mean, std, zeros, maxv, useful))

    print()
    print("  Components marked '⚠️ RARELY FIRES' may not be earning their weight.")
    return recommendations


# ── Phase 2: Logistic Regression ─────────────────────────────────────────────
def phase2_logistic(labeled_rows):
    """
    Fit Logistic Regression on evidence components → win/loss.
    Returns coefficient dict and accuracy.
    """
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        from sklearn.model_selection import cross_val_score
        import numpy as np
    except ImportError:
        log.error("scikit-learn not installed. Run: pip install scikit-learn --break-system-packages")
        return None, None

    feature_cols = [col for _, col, _, _ in ADDITIVE_COMPONENTS + INST_COMPONENTS]
    names        = [name for name, _, _, _ in ADDITIVE_COMPONENTS + INST_COMPONENTS]

    X, y = [], []
    for r in labeled_rows:
        row = dict(r)
        fv = [row.get(col, 0) or 0 for col in feature_cols]
        X.append(fv)
        y.append(1 if row.get("outcome") == "win" else 0)

    X = np.array(X, dtype=float)
    y = np.array(y)

    # Standardize
    scaler = StandardScaler()
    Xs     = scaler.fit_transform(X)

    # Fit
    model = LogisticRegression(max_iter=1000, C=1.0, random_state=42)
    model.fit(Xs, y)

    # Cross-validated accuracy
    cv_scores = cross_val_score(model, Xs, y, cv=min(5, len(y)//5 or 2),
                                 scoring="roc_auc")
    auc = cv_scores.mean()

    # Coefficients = relative importance
    coefs = dict(zip(names, model.coef_[0]))
    return coefs, auc


def phase2_forest(labeled_rows):
    """
    Random Forest feature importances as a second opinion.
    """
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import cross_val_score
        import numpy as np
    except ImportError:
        return None, None

    feature_cols = [col for _, col, _, _ in ADDITIVE_COMPONENTS + INST_COMPONENTS]
    names        = [name for name, _, _, _ in ADDITIVE_COMPONENTS + INST_COMPONENTS]

    X, y = [], []
    for r in labeled_rows:
        row = dict(r)
        fv = [row.get(col, 0) or 0 for col in feature_cols]
        X.append(fv)
        y.append(1 if row.get("outcome") == "win" else 0)

    X = np.array(X, dtype=float)
    y = np.array(y)

    model = RandomForestClassifier(n_estimators=200, max_depth=6,
                                    random_state=42, n_jobs=-1)
    model.fit(X, y)

    cv_scores = cross_val_score(model, X, y, cv=min(5, len(y)//5 or 2),
                                 scoring="roc_auc")
    auc = cv_scores.mean()
    importances = dict(zip(names, model.feature_importances_))
    return importances, auc


# ── Phase 3: Bayesian weight optimization ────────────────────────────────────
def phase3_bayesian(labeled_rows):
    """
    Directly optimize the additive + institutional weights using scipy
    to maximize AUC of the resulting composite score vs win/loss.
    """
    try:
        import numpy as np
        from scipy.optimize import minimize
        from sklearn.metrics import roc_auc_score
    except ImportError:
        log.error("scipy or scikit-learn not installed.")
        return None

    add_cols  = [col for _, col, _, _ in ADDITIVE_COMPONENTS]
    inst_cols = [col for _, col, _, _ in INST_COMPONENTS]

    records = [dict(r) for r in labeled_rows]
    y = np.array([1 if r.get("outcome") == "win" else 0 for r in records])

    # Current max points for normalization
    add_maxes  = np.array([mp for _, _, mp, _ in ADDITIVE_COMPONENTS])
    inst_maxes = np.array([mp for _, _, mp, _ in INST_COMPONENTS])

    def score_with_weights(w_add, w_inst, blend):
        """Compute composite score for all samples given weight vectors."""
        scores = []
        for r in records:
            add_vals  = np.array([r.get(c, 0) or 0 for c in add_cols])
            inst_vals = np.array([r.get(c, 0) or 0 for c in inst_cols])
            # Weighted sum (weights scale each component's contribution)
            add_score  = np.clip(np.dot(add_vals,  w_add)  / add_maxes.sum()  * 100, 0, 100)
            inst_score = np.clip(np.dot(inst_vals, w_inst) / inst_maxes.sum() * 100, 0, 100)
            comp       = add_score * blend[0] + inst_score * blend[1]
            scores.append(comp)
        return np.array(scores)

    def neg_auc(params):
        n_add  = len(add_cols)
        n_inst = len(inst_cols)
        w_add  = np.abs(params[:n_add])
        w_inst = np.abs(params[n_add:n_add+n_inst])
        blend_a = max(0.01, min(0.99, params[-1]))
        blend   = (blend_a, 1 - blend_a)
        scores  = score_with_weights(w_add, w_inst, blend)
        if len(np.unique(scores)) < 2:
            return 1.0
        return -roc_auc_score(y, scores)

    # Initial params: current weights + blend
    x0 = np.array(
        [mp for _, _, mp, _ in ADDITIVE_COMPONENTS] +
        [mp for _, _, mp, _ in INST_COMPONENTS] +
        [CURRENT_BLEND["additive"]]
    )

    log.info("Running Bayesian weight optimization (this may take 30-60s)...")
    result = minimize(neg_auc, x0, method="Nelder-Mead",
                      options={"maxiter": 5000, "xatol": 0.01, "fatol": 0.001})

    if not result.success:
        log.warning("Optimizer did not fully converge: {}".format(result.message))

    n_add  = len(ADDITIVE_COMPONENTS)
    n_inst = len(INST_COMPONENTS)
    opt_add   = np.abs(result.x[:n_add])
    opt_inst  = np.abs(result.x[n_add:n_add+n_inst])
    opt_blend = max(0.01, min(0.99, result.x[-1]))
    best_auc  = -result.fun

    return {
        "add_weights":   dict(zip([n for n,_,_,_ in ADDITIVE_COMPONENTS], opt_add.tolist())),
        "inst_weights":  dict(zip([n for n,_,_,_ in INST_COMPONENTS],     opt_inst.tolist())),
        "blend_add":     opt_blend,
        "blend_inst":    1 - opt_blend,
        "auc":           best_auc,
    }


# ── Formatting helpers ────────────────────────────────────────────────────────
def print_weight_comparison(current_weights: dict, learned_weights: dict,
                             label: str, source_desc: str):
    print()
    print("RECOMMENDED WEIGHT CHANGES  [{}]".format(label))
    print("Source: {}".format(source_desc))
    print(SEP2)
    print("  {:<18} {:>12} {:>12} {:>12}".format(
        "Component", "Current Pts", "Learned", "Change"))
    print("  " + "-" * 58)
    for name, pts in current_weights.items():
        learned = learned_weights.get(name, pts)
        delta   = learned - pts
        arrow   = (" ↑" if delta > 1 else " ↓" if delta < -1 else "  ")
        print("  {:<18} {:>12.1f} {:>12.1f} {:>+12.1f}{}".format(
            name, pts, learned, delta, arrow))


def apply_weights(opt_result: dict):
    """Patch scoring/additive.py and scoring/institutional.py with new weights."""
    add_path  = ROOT / "scoring" / "additive.py"
    inst_path = ROOT / "scoring" / "institutional.py"

    add_content  = add_path.read_text(encoding="utf-8")
    inst_content = inst_path.read_text(encoding="utf-8")

    aw = opt_result["add_weights"]
    iw = opt_result["inst_weights"]
    ba = round(opt_result["blend_add"],  2)
    bi = round(opt_result["blend_inst"], 2)

    # Patch additive.py -- replace each numeric literal in the weight assignments
    replacements_add = [
        ('if signal.structure == "sweep":   ev["structure"] = 20.0',
         'if signal.structure == "sweep":   ev["structure"] = {:.1f}'.format(aw.get("structure",20))),
        ('elif signal.structure == "block": ev["structure"] = 12.0',
         'elif signal.structure == "block": ev["structure"] = {:.1f}'.format(aw.get("structure",20)*0.6)),
        ('ev["intent"] = 15.0 if signal.intent in ("bullish", "bearish") else 5.0',
         'ev["intent"] = {:.1f} if signal.intent in ("bullish", "bearish") else 5.0'.format(aw.get("intent",15))),
        ('if iv_pct < 30:   ev["iv_bonus"] = 15.0',
         'if iv_pct < 30:   ev["iv_bonus"] = {:.1f}'.format(aw.get("iv_bonus",15))),
        ('elif iv_pct < 50: ev["iv_bonus"] = 10.0',
         'elif iv_pct < 50: ev["iv_bonus"] = {:.1f}'.format(aw.get("iv_bonus",15)*0.67)),
        ('elif iv_pct < 70: ev["iv_bonus"] = 5.0',
         'elif iv_pct < 70: ev["iv_bonus"] = {:.1f}'.format(aw.get("iv_bonus",15)*0.33)),
        ('if darkpool_sentiment == signal.intent:              ev["darkpool"] = 8.0',
         'if darkpool_sentiment == signal.intent:              ev["darkpool"] = {:.1f}'.format(aw.get("darkpool",8))),
        ('if signal.premium >= 5_000_000:   ev["premium_size"] = 15.0',
         'if signal.premium >= 5_000_000:   ev["premium_size"] = {:.1f}'.format(aw.get("premium_size",15))),
    ]
    for old, new in replacements_add:
        add_content = add_content.replace(old, new)

    # Patch institutional.py
    replacements_inst = [
        ('if signal.intent == "bullish" and exposure.net_vex > 0:     ev["vex"] = 10.0',
         'if signal.intent == "bullish" and exposure.net_vex > 0:     ev["vex"] = {:.1f}'.format(iw.get("vex",10))),
        ('if exposure.net_chex > 0 and signal.intent == "bullish":    ev["chex"] = 8.0',
         'if exposure.net_chex > 0 and signal.intent == "bullish":    ev["chex"] = {:.1f}'.format(iw.get("chex",8))),
        ('if signal.intent == "bullish"  and fd == "amplifying":      ev["flow_dir"] = 12.0',
         'if signal.intent == "bullish"  and fd == "amplifying":      ev["flow_dir"] = {:.1f}'.format(iw.get("flow_dir",12))),
        ('ev["ask_side"] = 5.0 if signal.ask_side else 0.0',
         'ev["ask_side"] = {:.1f} if signal.ask_side else 0.0'.format(iw.get("ask_side",5))),
    ]
    for old, new in replacements_inst:
        inst_content = inst_content.replace(old, new)

    # Patch blend in utils.py
    utils_path    = ROOT / "scoring" / "utils.py"
    utils_content = utils_path.read_text(encoding="utf-8")
    utils_content = utils_content.replace(
        'return round(additive * 0.55 + institutional * 0.45, 1)',
        'return round(additive * {} + institutional * {}, 1)'.format(ba, bi)
    )

    # Write
    add_path.write_text(add_content, encoding="utf-8")
    inst_path.write_text(inst_content, encoding="utf-8")
    utils_path.write_text(utils_content, encoding="utf-8")

    print()
    print("✅ Weights applied to:")
    print("   scoring/additive.py")
    print("   scoring/institutional.py")
    print("   scoring/utils.py  (blend: {:.0f}% additive / {:.0f}% institutional)".format(
        ba*100, bi*100))
    print()
    print("Restart the bot to use the new weights.")

    # Save applied weights to JSON for audit trail
    applied = {
        "applied_at":   datetime.now().isoformat(),
        "additive":     aw,
        "institutional": iw,
        "blend_add":    ba,
        "blend_inst":   bi,
        "auc":          opt_result.get("auc"),
    }
    audit_path = MODELS_DIR / "applied_weights.json"
    with open(audit_path, "w") as f:
        json.dump(applied, f, indent=2)
    print("Audit trail saved to: {}".format(audit_path))


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Weight optimizer for UW Options Bot")
    parser.add_argument("--phase", type=int, choices=[1,2,3], default=0,
                        help="Force a specific phase (default: auto)")
    parser.add_argument("--apply", action="store_true",
                        help="Apply learned weights to scorer files")
    parser.add_argument("--save", action="store_true",
                        help="Save optimal weights to ml/models/optimal_weights.json")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print("[ERR] DB not found: {}".format(DB_PATH))
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Check if score_evidence table exists yet
    table_exists = conn.execute("""
        SELECT COUNT(*) FROM sqlite_master
        WHERE type='table' AND name='score_evidence'
    """).fetchone()[0]

    if not table_exists:
        print()
        print("[INFO] score_evidence table not found.")
        print()
        print("  This table is created when the bot runs its first scan cycle.")
        print("  Fix option 1 -- start the bot and wait one cycle:")
        print("      python main.py")
        print()
        print("  Fix option 2 -- create the table immediately (no bot run needed):")
        print("      python init_db.py")
        print()
        conn.close()
        sys.exit(0)

    total_evidence = conn.execute(
        "SELECT COUNT(*) FROM score_evidence").fetchone()[0]
    total_labeled  = conn.execute(
        "SELECT COUNT(*) FROM score_evidence WHERE outcome IS NOT NULL"
    ).fetchone()[0]

    # Also check scored_signals for joined outcomes
    try:
        total_labeled_joined = conn.execute("""
            SELECT COUNT(*) FROM score_evidence e
            JOIN scored_signals s ON (
                e.symbol=s.symbol AND e.strike=s.strike AND
                e.right=s.right AND e.expiry=s.expiry AND
                date(e.scored_at)=date(s.scored_at))
            WHERE s.outcome IN ('win','loss')
        """).fetchone()[0]
        total_labeled = max(total_labeled, total_labeled_joined)
    except Exception:
        pass

    print()
    print(SEP)
    print("  UW OPTIONS BOT — WEIGHT OPTIMIZER")
    print("  {}".format(datetime.now().strftime("%Y-%m-%d %H:%M")))
    print(SEP)
    print("  Evidence rows  : {:,}".format(total_evidence))
    print("  Labeled outcomes: {:,}".format(total_labeled))
    print()

    # Auto-select phase
    phase = args.phase
    if phase == 0:
        if total_evidence < 10:
            print("[ERR] Need at least 50 evidence rows (have {}).".format(total_evidence))
            print("      Run a few more scan cycles (or wait for market open), then try again.")
            sys.exit(0)
        if total_labeled >= 100:
            phase = 3
        elif total_labeled >= 30:
            phase = 2
        else:
            phase = 1

    print("  Running Phase {} optimizer".format(phase))
    print(SEP2)

    opt_result = None

    # ── Phase 1 ──────────────────────────────────────────────────────────────
    evidence, err = load_evidence(conn, min_rows=10)
    if err:
        print("[ERR] {}".format(err))
        sys.exit(0)

    stats_recs = phase1_stats(evidence, conn)

    if phase == 1:
        print()
        print("RECOMMENDATION (Phase 1 — statistical, no outcome labels yet)")
        print(SEP2)
        print("  Components to consider reducing:")
        current = {n: mp for n, _, mp, _ in ADDITIVE_COMPONENTS + INST_COMPONENTS}
        for name, rec in stats_recs.items():
            curr = current.get(name, 0)
            if rec < curr * 0.7:
                print("  {:<18} current={:.0f}  suggested={:.0f}".format(
                    name, curr, rec))
        print()
        print("  Run outcome_tracker.py nightly to accumulate labeled outcomes,")
        print("  then re-run this script for Phase 2/3 optimization.")
        conn.close()
        return

    # ── Phase 2 ──────────────────────────────────────────────────────────────
    labeled, err = load_labeled(conn, min_rows=30)
    if err:
        print("[ERR] {}".format(err))
        sys.exit(0)

    wins   = sum(1 for r in labeled if dict(r).get("outcome") == "win")
    losses = len(labeled) - wins
    print()
    print("LABELED SAMPLE: {} rows  ({} wins, {} losses, {:.0f}% win rate)".format(
        len(labeled), wins, losses,
        100*wins/len(labeled) if labeled else 0))

    log_coefs, log_auc = phase2_logistic(labeled)
    rf_imp,    rf_auc  = phase2_forest(labeled)

    current_pts = {n: mp for n, _, mp, _ in ADDITIVE_COMPONENTS + INST_COMPONENTS}

    if log_coefs and log_auc:
        print()
        print("LOGISTIC REGRESSION  (AUC={:.3f})".format(log_auc))
        print(SEP2)
        # Translate LR coefficients → new point values
        # Higher positive coef = more predictive of win → raise weight
        max_coef = max(abs(v) for v in log_coefs.values()) or 1
        lr_weights = {}
        for name, coef in sorted(log_coefs.items(), key=lambda x: -x[1]):
            current = current_pts.get(name, 10)
            # Scale: keep within 50%-150% of current to avoid wild swings
            scale    = 1 + (coef / max_coef) * 0.5  # ±50% adjustment
            new_pts  = round(current * scale, 1)
            lr_weights[name] = new_pts
            arrow = "↑" if coef > 0.2 else ("↓" if coef < -0.2 else " ")
            print("  {:<18} coef={:+.3f}  current={:.0f}  suggested={:.1f} {}".format(
                name, coef, current, new_pts, arrow))

    if rf_imp and rf_auc:
        print()
        print("RANDOM FOREST IMPORTANCES  (AUC={:.3f})".format(rf_auc))
        print(SEP2)
        for name, imp in sorted(rf_imp.items(), key=lambda x: -x[1]):
            current = current_pts.get(name, 10)
            print("  {:<18} importance={:.3f}  current_pts={:.0f}".format(
                name, imp, current))

    if phase == 2:
        print()
        print("Phase 2 complete. For full Bayesian optimization, accumulate 100+")
        print("labeled outcomes then re-run (Phase 3 will be auto-selected).")
        conn.close()
        return

    # ── Phase 3 ──────────────────────────────────────────────────────────────
    if phase >= 3:
        opt_result = phase3_bayesian(labeled)

        if opt_result:
            print()
            print("BAYESIAN OPTIMIZATION RESULT  (AUC={:.3f})".format(opt_result["auc"]))
            print(SEP2)
            print("  Blend: {:.0f}% additive / {:.0f}% institutional".format(
                opt_result["blend_add"]*100, opt_result["blend_inst"]*100))
            print("  (current: 55% additive / 45% institutional)")
            print()

            print_weight_comparison(
                {n: mp for n, _, mp, _ in ADDITIVE_COMPONENTS},
                opt_result["add_weights"],
                "ADDITIVE", "Bayesian optimizer")
            print_weight_comparison(
                {n: mp for n, _, mp, _ in INST_COMPONENTS},
                opt_result["inst_weights"],
                "INSTITUTIONAL", "Bayesian optimizer")

            if args.save:
                save_path = MODELS_DIR / "optimal_weights.json"
                with open(save_path, "w") as f:
                    json.dump(opt_result, f, indent=2)
                print("  Weights saved to: {}".format(save_path))

            if args.apply:
                confirm = input("\nApply these weights to scorer files? [y/N] ")
                if confirm.lower() == "y":
                    apply_weights(opt_result)
                else:
                    print("Not applied. Re-run with --apply to apply later.")

    conn.close()


if __name__ == "__main__":
    main()
