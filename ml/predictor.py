"""
ml/predictor.py
===============
Loads trained models and scores new signals in real time.

Used by scanner.py at the end of each scan cycle to:
  1. Override/confirm rule-based signal_type classification
  2. Add ml_confidence score (0-100) to each signal
  3. Add ml_win_prob (0-100) if outcome model is available

Falls back gracefully to rule-based classification if models
are not yet trained (during accumulation phase).
"""

import logging
import pickle
from pathlib import Path

from ml.trainer import TYPE_MODEL_PATH, OUTCOME_MODEL_PATH

log = logging.getLogger("UWBot.ML.Predictor")


class SignalPredictor:
    """
    Real-time ML scoring layer.
    Loaded once at bot startup, cached in memory.
    Models auto-reloaded if files are updated (after retraining).
    """

    def __init__(self):
        self._type_model    = None
        self._outcome_model = None
        self._type_mtime    = 0.0
        self._outcome_mtime = 0.0
        self._load_models()

    def _load_models(self):
        """Load models from disk if available."""
        # Type classifier
        if TYPE_MODEL_PATH.exists():
            try:
                mtime = TYPE_MODEL_PATH.stat().st_mtime
                if mtime != self._type_mtime:
                    with open(TYPE_MODEL_PATH, "rb") as f:
                        self._type_model = pickle.load(f)
                    self._type_mtime = mtime
                    log.info("Type classifier loaded ({} signals, trained {})".format(
                        self._type_model.get("n_signals", "?"),
                        self._type_model.get("trained_at", "?")[:10]
                    ))
            except Exception as e:
                log.warning("Could not load type classifier: {}".format(e))
                self._type_model = None

        # Outcome predictor
        if OUTCOME_MODEL_PATH.exists():
            try:
                mtime = OUTCOME_MODEL_PATH.stat().st_mtime
                if mtime != self._outcome_mtime:
                    with open(OUTCOME_MODEL_PATH, "rb") as f:
                        self._outcome_model = pickle.load(f)
                    self._outcome_mtime = mtime
                    log.info("Outcome predictor loaded (AUC={:.3f}, trained {})".format(
                        self._outcome_model.get("cv_auc", 0),
                        self._outcome_model.get("trained_at", "?")[:10]
                    ))
            except Exception as e:
                log.warning("Could not load outcome predictor: {}".format(e))
                self._outcome_model = None

    @property
    def type_model_available(self) -> bool:
        return self._type_model is not None

    @property
    def outcome_model_available(self) -> bool:
        return self._outcome_model is not None

    def predict_signal_type(self, feature_vector: list) -> tuple[str, float]:
        """
        Predict signal type from feature vector.
        Returns (type_label, confidence_0_to_1).
        Falls back to 'unidirectional' with 0.5 confidence if no model.
        """
        self._load_models()   # check for updated model files

        if not self._type_model or not feature_vector:
            return "unidirectional", 0.5

        try:
            kmeans  = self._type_model["kmeans"]
            scaler  = self._type_model["scaler"]
            labels  = self._type_model["cluster_labels"]

            X_scaled    = scaler.transform([feature_vector])
            cluster_id  = int(kmeans.predict(X_scaled)[0])
            type_label  = labels.get(cluster_id, "unidirectional")

            # Confidence = inverse of distance to nearest centroid
            distances   = kmeans.transform(X_scaled)[0]
            nearest_dist = distances[cluster_id]
            # Normalise: distance 0 = confidence 1.0, distance 5 = confidence 0.0
            confidence  = max(0.0, 1.0 - nearest_dist / 5.0)

            return type_label, round(confidence, 3)

        except Exception as e:
            log.debug("Type prediction error: {}".format(e))
            return "unidirectional", 0.5

    def predict_win_probability(self, feature_vector: list) -> float:
        """
        Predict probability of a winning trade (0.0 to 1.0).
        Returns 0.5 (neutral) if outcome model not available.
        """
        self._load_models()

        if not self._outcome_model or not feature_vector:
            return 0.5

        try:
            rf      = self._outcome_model["rf"]
            scaler  = self._outcome_model["scaler"]

            X_scaled = scaler.transform([feature_vector])
            proba    = rf.predict_proba(X_scaled)[0]
            # proba[1] = probability of class 1 = win
            return round(float(proba[1]), 3)

        except Exception as e:
            log.debug("Win probability prediction error: {}".format(e))
            return 0.5

    def score_signal(self, signal_id: int) -> dict:
        """
        Full ML scoring for a signal by DB ID.
        Returns dict with ml_type, ml_confidence, ml_win_prob, ml_composite.
        """
        from ml.features import extract_features_for_signal

        fv = extract_features_for_signal(signal_id)
        if not fv:
            return {
                "ml_type":        "unknown",
                "ml_confidence":  0.0,
                "ml_win_prob":    0.5,
                "ml_composite":   0.0,
                "ml_available":   False,
            }

        ml_type, ml_conf = self.predict_signal_type(fv)
        ml_win_prob      = self.predict_win_probability(fv)

        # ML composite: blend of confidence and win probability
        # Only meaningful when both models available
        if self.type_model_available and self.outcome_model_available:
            ml_composite = round((ml_conf * 0.4 + ml_win_prob * 0.6) * 100, 1)
        else:
            ml_composite = round(ml_win_prob * 100, 1)

        return {
            "ml_type":       ml_type,
            "ml_confidence": round(ml_conf * 100, 1),
            "ml_win_prob":   round(ml_win_prob * 100, 1),
            "ml_composite":  ml_composite,
            "ml_available":  True,
        }


# Singleton instance -- shared across scanner cycles
_predictor_instance = None

def get_predictor() -> SignalPredictor:
    """Return singleton predictor instance."""
    global _predictor_instance
    if _predictor_instance is None:
        _predictor_instance = SignalPredictor()
    return _predictor_instance
