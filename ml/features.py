"""
ml/features.py
==============
Feature engineering -- extracts and normalises features from
SQLite DB tables for ML training and inference.

Feature groups:
  1. Flow signal features    -- from flow_signals table
  2. Scoring features        -- from scored_signals table
  3. Dealer exposure         -- from dealer_exposure table
  4. OI buildup              -- from oi_changes table
  5. Dark pool               -- from darkpool_prints table
  6. Temporal                -- time of day, day of week, DTE

All features normalised to 0-1 range for model compatibility.
"""

import logging
import sqlite3
from datetime import datetime, date
from database.db import get_connection

log = logging.getLogger("UWBot.ML.Features")


# ── Feature definitions ───────────────────────────────────────────────────────
FEATURE_NAMES = [
    # Scoring
    "composite_score",       # 0-100
    "additive_score",        # 0-100
    "institutional_score",   # 0-100
    "uw_score",              # 0-100
    "iv_percentile",         # 0-100

    # Flow signal
    "premium_log",           # log10(premium) normalised
    "ask_side",              # 0 or 1
    "vol_oi_ratio",          # volume/OI ratio
    "is_sweep",              # 1 if structure=sweep
    "is_call",               # 1 if right=C, 0 if P

    # Dealer exposure
    "gex_m",                 # net GEX in $M (clipped -2000 to 2000)
    "dex_m",                 # net DEX in $M
    "vex_sign",              # sign of VEX: -1, 0, +1
    "chex_sign",             # sign of CHEX
    "is_positive_gamma",     # 1 if regime=positive_gamma
    "is_negative_gamma",     # 1 if regime=negative_gamma

    # OI buildup
    "oi_delta_pct",          # % OI change vs prior scan
    "oi_buildup_scans",      # number of consecutive positive OI scans

    # Dark pool
    "dp_bullish",            # 1 if darkpool_sentiment=bullish
    "dp_bearish",            # 1 if darkpool_sentiment=bearish

    # Temporal
    "dte_norm",              # DTE / 28 (normalised to 0-1)
    "hour_norm",             # hour of day / 24
    "day_of_week",           # 0=Mon to 4=Fri, normalised /4
]

N_FEATURES = len(FEATURE_NAMES)


def extract_features_for_signal(signal_id: int) -> list[float] | None:
    """
    Extract feature vector for a single scored_signal by ID.
    Returns list of N_FEATURES floats, or None if data insufficient.
    """
    try:
        with get_connection() as conn:
            # Main signal data
            row = conn.execute("""
                SELECT ss.*, fs.volume, fs.open_interest,
                       fs.volume_oi_ratio, fs.structure
                FROM scored_signals ss
                LEFT JOIN flow_signals fs ON fs.id = ss.flow_signal_id
                WHERE ss.id = ?
            """, (signal_id,)).fetchone()

            if not row:
                return None

            # OI trend
            oi_rows = conn.execute("""
                SELECT oi_delta_pct, oi_delta
                FROM oi_changes
                WHERE symbol=? AND strike=? AND right=? AND expiry=?
                ORDER BY captured_at DESC
                LIMIT 5
            """, (row["symbol"], row["strike"], row["right"],
                  row["expiry"])).fetchall()

        return _build_vector(row, oi_rows)

    except Exception as e:
        log.error("Feature extraction error for signal {}: {}".format(signal_id, e))
        return None


def extract_features_for_training() -> tuple[list, list, list]:
    """
    Extract features + labels for all signals with known outcomes.
    Used for training the outcome predictor.

    Returns:
        X      : list of feature vectors
        y      : list of labels (1=win, 0=loss)
        ids    : list of signal IDs
    """
    X, y, ids = [], [], []

    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT ss.id, ss.*, fs.volume, fs.open_interest,
                       fs.volume_oi_ratio, fs.structure
                FROM scored_signals ss
                LEFT JOIN flow_signals fs ON fs.id = ss.flow_signal_id
                WHERE ss.outcome IN ('win', 'loss')
                ORDER BY ss.scored_at
            """).fetchall()

            for row in rows:
                oi_rows = conn.execute("""
                    SELECT oi_delta_pct, oi_delta
                    FROM oi_changes
                    WHERE symbol=? AND strike=? AND right=? AND expiry=?
                    ORDER BY captured_at DESC LIMIT 5
                """, (row["symbol"], row["strike"], row["right"],
                      row["expiry"])).fetchall()

                fv = _build_vector(row, oi_rows)
                if fv:
                    X.append(fv)
                    y.append(1 if row["outcome"] == "win" else 0)
                    ids.append(row["id"])

    except Exception as e:
        log.error("Training feature extraction error: {}".format(e))

    log.info("Extracted {} training samples with {} features".format(
        len(X), N_FEATURES))
    return X, y, ids


def extract_features_for_clustering() -> tuple[list, list]:
    """
    Extract features for ALL scored signals (labeled or not).
    Used for signal type clustering.

    Returns:
        X   : list of feature vectors
        ids : list of signal IDs
    """
    X, ids = [], []

    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT ss.id, ss.*, fs.volume, fs.open_interest,
                       fs.volume_oi_ratio, fs.structure
                FROM scored_signals ss
                LEFT JOIN flow_signals fs ON fs.id = ss.flow_signal_id
                ORDER BY ss.scored_at DESC
                LIMIT 5000
            """).fetchall()

            for row in rows:
                oi_rows = conn.execute("""
                    SELECT oi_delta_pct, oi_delta
                    FROM oi_changes
                    WHERE symbol=? AND strike=? AND right=? AND expiry=?
                    ORDER BY captured_at DESC LIMIT 5
                """, (row["symbol"], row["strike"], row["right"],
                      row["expiry"])).fetchall()

                fv = _build_vector(row, oi_rows)
                if fv:
                    X.append(fv)
                    ids.append(row["id"])

    except Exception as e:
        log.error("Clustering feature extraction error: {}".format(e))

    log.info("Extracted {} signals for clustering".format(len(X)))
    return X, ids


def _build_vector(row, oi_rows: list) -> list[float] | None:
    """Build normalised feature vector from DB row + OI history."""
    try:
        import math

        def safe_float(val, default=0.0):
            try:
                return float(val) if val is not None else default
            except (ValueError, TypeError):
                return default

        # Scoring features
        composite   = safe_float(row["composite_score"]) / 100.0
        additive    = safe_float(row["additive_score"])   / 100.0
        inst        = safe_float(row["institutional_score"]) / 100.0
        uw_score    = safe_float(row["uw_score"])         / 100.0
        iv_pct      = safe_float(row["iv_percentile"])    / 100.0

        # Flow features
        premium     = safe_float(row["premium"])
        prem_log    = math.log10(max(premium, 1)) / 8.0   # log10(100M)=8
        ask_side    = 1.0 if row["ask_side"] else 0.0
        vol_oi      = min(safe_float(row.get("volume_oi_ratio")), 100.0) / 100.0
        structure   = str(row.get("structure") or "block")
        is_sweep    = 1.0 if "sweep" in structure.lower() else 0.0
        is_call     = 1.0 if row["right"] == "C" else 0.0

        # Dealer exposure
        gex_m       = max(min(safe_float(row["dealer_gex_m"]), 2000), -2000) / 2000.0
        dex_m       = max(min(safe_float(row["dealer_dex_m"]), 2000), -2000) / 2000.0
        vex_sign    = (1.0 if safe_float(row["dealer_vex_m"]) > 0
                      else -1.0 if safe_float(row["dealer_vex_m"]) < 0 else 0.0)
        chex_sign   = (1.0 if safe_float(row["dealer_chex_m"]) > 0
                      else -1.0 if safe_float(row["dealer_chex_m"]) < 0 else 0.0)
        regime      = str(row.get("dealer_regime") or "")
        is_pos_gex  = 1.0 if "positive" in regime else 0.0
        is_neg_gex  = 1.0 if "negative" in regime else 0.0

        # OI buildup
        oi_delta_pct    = 0.0
        oi_buildup_scans = 0.0
        if oi_rows:
            oi_delta_pct = min(
                safe_float(oi_rows[0]["oi_delta_pct"]), 200
            ) / 200.0
            oi_buildup_scans = sum(
                1 for r in oi_rows if safe_float(r["oi_delta"]) > 0
            ) / max(len(oi_rows), 1)

        # Dark pool
        dp = str(row.get("darkpool_sentiment") or "neutral")
        dp_bull = 1.0 if dp == "bullish" else 0.0
        dp_bear = 1.0 if dp == "bearish" else 0.0

        # Temporal
        dte = safe_float(row.get("dte"))
        dte_norm = min(dte, 28) / 28.0 if dte > 0 else 0.5

        scored_at = str(row.get("scored_at") or "")
        try:
            dt = datetime.fromisoformat(scored_at[:19])
            hour_norm    = dt.hour / 24.0
            day_of_week  = dt.weekday() / 4.0
        except Exception:
            hour_norm   = 0.5
            day_of_week = 0.5

        return [
            composite, additive, inst, uw_score, iv_pct,
            prem_log, ask_side, vol_oi, is_sweep, is_call,
            gex_m, dex_m, vex_sign, chex_sign, is_pos_gex, is_neg_gex,
            oi_delta_pct, oi_buildup_scans,
            dp_bull, dp_bear,
            dte_norm, hour_norm, day_of_week,
        ]

    except Exception as e:
        log.debug("Vector build error: {}".format(e))
        return None
