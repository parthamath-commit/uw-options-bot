"""
ml/trainer.py
=============
Trains two ML models from accumulated DB data:

Model 1: Signal Type Classifier (KMeans clustering)
  - Runs on all signals (no labels needed)
  - Minimum: 50 signals
  - Identifies 5 clusters → mapped to signal types:
    unidirectional_bullish, unidirectional_bearish,
    straddle/strangle, hedging, noise

Model 2: Outcome Predictor (Random Forest)
  - Runs on signals with win/loss outcomes
  - Minimum: 30 labeled outcomes
  - Predicts win probability (0-1) for new signals
  - Features: composite score, GEX, OI trend, dark pool, IV pct

Usage:
  python ml/trainer.py --model all     train both models
  python ml/trainer.py --model type    train type classifier only
  python ml/trainer.py --model outcome train outcome predictor only
  python ml/trainer.py --status        show training data stats
  python main.py --mode train          same as --model all
"""

import os
import sys
import pickle
import logging
from pathlib import Path

log = logging.getLogger("UWBot.ML.Trainer")

MODEL_DIR = Path(__file__).parent / "models"
MODEL_DIR.mkdir(exist_ok=True)

TYPE_MODEL_PATH    = MODEL_DIR / "signal_type_classifier.pkl"
OUTCOME_MODEL_PATH = MODEL_DIR / "outcome_predictor.pkl"
SCALER_PATH        = MODEL_DIR / "feature_scaler.pkl"

MIN_SIGNALS_FOR_CLUSTERING = 50
MIN_OUTCOMES_FOR_PREDICTOR = 30


def check_dependencies() -> bool:
    """Check sklearn is installed."""
    try:
        import sklearn
        return True
    except ImportError:
        log.error(
            "scikit-learn not installed.\n"
            "Run: pip install scikit-learn\n"
            "Then retrain: python main.py --mode train"
        )
        return False


def get_training_stats() -> dict:
    """Return current DB data counts for training readiness check."""
    from database.db import get_connection
    stats = {}
    try:
        with get_connection() as conn:
            stats["total_signals"] = conn.execute(
                "SELECT COUNT(*) FROM scored_signals"
            ).fetchone()[0]

            stats["labeled_signals"] = conn.execute(
                "SELECT COUNT(*) FROM scored_signals WHERE outcome IN ('win','loss')"
            ).fetchone()[0]

            stats["wins"] = conn.execute(
                "SELECT COUNT(*) FROM scored_signals WHERE outcome='win'"
            ).fetchone()[0]

            stats["losses"] = conn.execute(
                "SELECT COUNT(*) FROM scored_signals WHERE outcome='loss'"
            ).fetchone()[0]

            stats["scan_runs"] = conn.execute(
                "SELECT COUNT(*) FROM scan_runs"
            ).fetchone()[0]

            stats["symbols"] = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM scored_signals"
            ).fetchone()[0]

            # ML validation distribution
            val_rows = conn.execute("""
                SELECT ml_validation, COUNT(*) as cnt
                FROM scored_signals
                WHERE ml_validation IS NOT NULL
                GROUP BY ml_validation
            """).fetchall()
            stats["ml_validations"] = {r["ml_validation"]: r["cnt"] for r in val_rows}

            # Rule vs ML agreement rate
            agree_count = conn.execute("""
                SELECT COUNT(*) FROM scored_signals
                WHERE signal_type = rule_type
                  AND ml_validation != 'rule_only'
            """).fetchone()[0]
            total_ml    = conn.execute("""
                SELECT COUNT(*) FROM scored_signals
                WHERE ml_validation != 'rule_only'
            """).fetchone()[0]
            stats["ml_agreement_rate"] = (
                round(agree_count / total_ml * 100, 1) if total_ml > 0 else None
            )

    # Signal type distribution
            type_rows = conn.execute("""
                SELECT signal_type, COUNT(*) as cnt
                FROM scored_signals
                GROUP BY signal_type
            """).fetchall()
            stats["signal_types"] = {r["signal_type"]: r["cnt"] for r in type_rows}

    except Exception as e:
        log.error("Stats error: {}".format(e))
    return stats


def print_training_status():
    """Print current DB stats and training readiness."""
    stats = get_training_stats()

    print("")
    print("  ML Training Status")
    print("-" * 55)
    print("  Total signals in DB   : {:>6}  (need 50+ for type model)".format(
        stats.get("total_signals", 0)))
    print("  Labeled outcomes      : {:>6}  (need 30+ for outcome model)".format(
        stats.get("labeled_signals", 0)))
    print("  Wins / Losses         : {:>3} / {:<3}".format(
        stats.get("wins", 0), stats.get("losses", 0)))
    print("  Scan runs completed   : {:>6}".format(stats.get("scan_runs", 0)))
    print("  Unique symbols        : {:>6}".format(stats.get("symbols", 0)))
    print("")

    sig_types = stats.get("signal_types", {})
    if sig_types:
        print("  Signal type breakdown:")
        for stype, cnt in sorted(sig_types.items(), key=lambda x: -x[1]):
            print("    {:<22} {:>5}".format(stype, cnt))
        print("")

    ml_vals = stats.get("ml_validations", {})
    agreement = stats.get("ml_agreement_rate")
    if ml_vals:
        print("  ML validation breakdown:")
        for val, cnt in sorted(ml_vals.items(), key=lambda x: -x[1]):
            print("    {:<22} {:>5}".format(val, cnt))
        if agreement is not None:
            print("  Rule/ML agreement rate : {:.1f}%".format(agreement))
        print("")

    total = stats.get("total_signals", 0)
    labeled = stats.get("labeled_signals", 0)

    print("  Model readiness:")
    type_ready = total >= MIN_SIGNALS_FOR_CLUSTERING
    outcome_ready = labeled >= MIN_OUTCOMES_FOR_PREDICTOR
    print("    Type classifier  : {}  ({} / {} signals)".format(
        "[READY]" if type_ready else "[NOT READY]",
        total, MIN_SIGNALS_FOR_CLUSTERING
    ))
    print("    Outcome predictor: {}  ({} / {} labeled)".format(
        "[READY]" if outcome_ready else "[NOT READY]",
        labeled, MIN_OUTCOMES_FOR_PREDICTOR
    ))

    # Check existing models
    print("")
    print("  Saved models:")
    for name, path in [
        ("Type classifier ", TYPE_MODEL_PATH),
        ("Outcome predictor", OUTCOME_MODEL_PATH),
        ("Feature scaler  ", SCALER_PATH),
    ]:
        exists = path.exists()
        print("    {} : {}".format(
            name, "[OK] " + str(path.name) if exists else "[MISSING]"
        ))
    print("")


def train_type_classifier() -> bool:
    """
    Train KMeans clustering model to classify signal types.
    Uses all signals regardless of outcome labels.

    Cluster mapping (learned from data):
      The 5 clusters are automatically labelled by examining
      the centroid features after training:
      - High call ratio + high ask_side + high premium = bullish unidirectional
      - High put ratio + high ask_side + high premium  = bearish unidirectional
      - Balanced call/put + similar premium             = straddle/strangle
      - Mixed ask/bid side                              = hedging
      - Low score + low premium                         = noise
    """
    if not check_dependencies():
        return False

    from ml.features import extract_features_for_clustering, FEATURE_NAMES
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler
    import pickle

    log.info("Extracting features for type classifier training...")
    X, ids = extract_features_for_clustering()

    if len(X) < MIN_SIGNALS_FOR_CLUSTERING:
        log.warning(
            "Not enough signals for type classifier. "
            "Have {}, need {}. "
            "Continue accumulating data.".format(len(X), MIN_SIGNALS_FOR_CLUSTERING)
        )
        return False

    log.info("Training KMeans type classifier on {} signals...".format(len(X)))

    # Scale features
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Train KMeans with 5 clusters
    # n_init=20 for stable results
    kmeans = KMeans(n_clusters=5, n_init=20, random_state=42)
    kmeans.fit(X_scaled)
    labels = kmeans.labels_

    # Auto-label clusters by examining centroids
    centroids = scaler.inverse_transform(kmeans.cluster_centers_)
    cluster_labels = _auto_label_clusters(centroids, FEATURE_NAMES)

    log.info("Cluster assignments:")
    from collections import Counter
    counts = Counter(labels)
    for cluster_id, type_label in cluster_labels.items():
        log.info("  Cluster {}: {} ({} signals)".format(
            cluster_id, type_label, counts.get(cluster_id, 0)
        ))

    # Save model + scaler + cluster labels
    model_data = {
        "kmeans":         kmeans,
        "scaler":         scaler,
        "cluster_labels": cluster_labels,
        "feature_names":  FEATURE_NAMES,
        "n_signals":      len(X),
        "trained_at":     __import__("datetime").datetime.now().isoformat(),
    }

    with open(TYPE_MODEL_PATH, "wb") as f:
        pickle.dump(model_data, f)
    with open(SCALER_PATH, "wb") as f:
        pickle.dump(scaler, f)

    log.info("Type classifier saved to: {}".format(TYPE_MODEL_PATH))
    return True


def _auto_label_clusters(centroids, feature_names: list) -> dict:
    """
    Auto-assign cluster labels by reading centroid feature values.
    Returns {cluster_id: label_string}
    """
    feat_idx = {name: i for i, name in enumerate(feature_names)}

    def get(centroid, name):
        idx = feat_idx.get(name)
        return centroid[idx] if idx is not None else 0.0

    labels = {}
    for i, c in enumerate(centroids):
        is_call     = get(c, "is_call")
        ask_side    = get(c, "ask_side")
        prem_log    = get(c, "premium_log")
        composite   = get(c, "composite_score")
        is_sweep    = get(c, "is_sweep")
        gex         = get(c, "gex_m")

        # Low score + low premium = noise
        if composite < 0.4 and prem_log < 0.3:
            labels[i] = "noise"
        # High ask_side + high call ratio = bullish unidirectional
        elif ask_side > 0.6 and is_call > 0.6:
            labels[i] = "unidirectional_bullish"
        # High ask_side + high put ratio = bearish unidirectional
        elif ask_side > 0.6 and is_call < 0.4:
            labels[i] = "unidirectional_bearish"
        # Balanced call/put = straddle/strangle
        elif 0.4 <= is_call <= 0.6:
            labels[i] = "straddle_strangle"
        # Mixed signals = hedging
        else:
            labels[i] = "hedging"

    return labels


def train_outcome_predictor() -> bool:
    """
    Train Random Forest to predict win/loss probability.
    Requires signals with recorded outcomes (win/loss).

    Features ranked by importance (expected):
      1. composite_score
      2. gex_m (dealer positioning)
      3. oi_buildup_scans
      4. ask_side
      5. iv_percentile
      6. dp_bullish / dp_bearish
      7. premium_log
    """
    if not check_dependencies():
        return False

    from ml.features import extract_features_for_training, FEATURE_NAMES
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import cross_val_score
    from sklearn.preprocessing import StandardScaler
    import pickle

    log.info("Extracting labeled features for outcome predictor...")
    X, y, ids = extract_features_for_training()

    if len(X) < MIN_OUTCOMES_FOR_PREDICTOR:
        log.warning(
            "Not enough labeled outcomes. "
            "Have {}, need {}. "
            "Record trade outcomes using update_signal_outcome().".format(
                len(X), MIN_OUTCOMES_FOR_PREDICTOR
            )
        )
        return False

    wins   = sum(y)
    losses = len(y) - wins
    log.info("Training outcome predictor: {} wins, {} losses".format(wins, losses))

    # Scale
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Random Forest
    rf = RandomForestClassifier(
        n_estimators=200,
        max_depth=6,
        min_samples_leaf=3,
        class_weight="balanced",   # handles win/loss imbalance
        random_state=42,
        n_jobs=-1,
    )
    rf.fit(X_scaled, y)

    # Cross-validation accuracy
    cv_scores = cross_val_score(rf, X_scaled, y, cv=5, scoring="roc_auc")
    log.info("Cross-val ROC-AUC: {:.3f} (+/- {:.3f})".format(
        cv_scores.mean(), cv_scores.std()))

    # Feature importances
    importances = sorted(
        zip(FEATURE_NAMES, rf.feature_importances_),
        key=lambda x: -x[1]
    )
    log.info("Top 10 features by importance:")
    for name, imp in importances[:10]:
        log.info("  {:<25} {:.4f}".format(name, imp))

    # Save
    model_data = {
        "rf":               rf,
        "scaler":           scaler,
        "feature_names":    FEATURE_NAMES,
        "cv_auc":           cv_scores.mean(),
        "n_samples":        len(X),
        "win_rate":         wins / len(y),
        "trained_at":       __import__("datetime").datetime.now().isoformat(),
    }

    with open(OUTCOME_MODEL_PATH, "wb") as f:
        pickle.dump(model_data, f)

    log.info("Outcome predictor saved: {} (AUC={:.3f})".format(
        OUTCOME_MODEL_PATH, cv_scores.mean()))
    return True


def train_all():
    """Train both models."""
    log.info("Starting ML training run...")
    print_training_status()

    type_ok    = train_type_classifier()
    outcome_ok = train_outcome_predictor()

    print("")
    print("  Training Results")
    print("-" * 40)
    print("  Type classifier  : {}".format("[OK]" if type_ok else "[SKIPPED]"))
    print("  Outcome predictor: {}".format("[OK]" if outcome_ok else "[SKIPPED]"))
    print("")

    if not type_ok and not outcome_ok:
        print("  Accumulate more data and retrain.")
        print("  Type model needs {} signals.".format(MIN_SIGNALS_FOR_CLUSTERING))
        print("  Outcome model needs {} labeled trades.".format(MIN_OUTCOMES_FOR_PREDICTOR))
    print("")


if __name__ == "__main__":
    import argparse
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")

    # Setup logging
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    )

    parser = argparse.ArgumentParser(description="UW Options Bot -- ML Trainer")
    parser.add_argument(
        "--model",
        choices=["all", "type", "outcome"],
        default="all",
        help="Which model to train"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show training data stats only"
    )
    args = parser.parse_args()

    if args.status:
        print_training_status()
    elif args.model == "all":
        train_all()
    elif args.model == "type":
        train_type_classifier()
    elif args.model == "outcome":
        train_outcome_predictor()
