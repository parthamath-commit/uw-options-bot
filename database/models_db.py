"""
database/models_db.py
=====================
All database insert and query functions for UW Options Bot.

Design
──────
- Every public function accepts clean model objects or primitives
  (no raw SQL leaks into the rest of the codebase)
- Inserts use INSERT OR IGNORE for idempotency —
  re-running a scan never creates duplicates
- Query functions return typed dicts or lists for easy consumption
  in the scoring engine and main.py --mode query

Insert functions
────────────────
  insert_scan_run()        → int (run_id)
  close_scan_run()
  insert_flow_signal()     → int (signal_id)
  insert_scored_signal()
  insert_dealer_exposure()
  insert_oi_snapshot()     (computes delta vs prior automatically)
  insert_darkpool_prints()

Query functions (used by scoring engine for context)
─────────────────────────────────────────────────────
  get_prev_oi()            → prior OI for a contract
  get_oi_trend()           → OI growth history for a strike
  get_gex_history()        → GEX time series for a symbol
  get_dp_sentiment_trend() → DP sentiment over last N scans
  get_signal_history()     → prior scored signals for a symbol
  get_outcome_stats()      → win/loss summary by symbol/intent
"""

import json
import logging
import sqlite3
from datetime import datetime, date
from typing import Optional

from database.db import get_connection
from config import ET
from models import FlowSignal, ScoredSignal, DealerExposure

log = logging.getLogger("UWBot.DB.Models")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(ET).isoformat()

def _dte(expiry: str) -> Optional[int]:
    try:
        return (date.fromisoformat(expiry) - date.today()).days
    except Exception:
        return None


# ── scan_runs ─────────────────────────────────────────────────────────────────
def insert_scan_run(portfolio_value: float, spy_gex: float, spy_regime: str) -> int:
    """Open a new scan run. Returns run_id."""
    sql = """
        INSERT INTO scan_runs (started_at, portfolio_value, spy_gex_m, spy_regime)
        VALUES (?, ?, ?, ?)
    """
    with get_connection() as conn:
        cur = conn.execute(sql, (_now(), portfolio_value, spy_gex / 1e6, spy_regime))
        conn.commit()
        return cur.lastrowid


def close_scan_run(
    run_id: int,
    symbols_scanned: int,
    signals_logged: int,
    alerts_fired: int,
) -> None:
    """Mark scan run complete with summary stats."""
    sql = """
        UPDATE scan_runs
        SET finished_at      = ?,
            duration_sec     = ROUND(
                (JULIANDAY(?) - JULIANDAY(started_at)) * 86400, 2
            ),
            symbols_scanned  = ?,
            signals_logged   = ?,
            alerts_fired     = ?
        WHERE id = ?
    """
    now = _now()
    with get_connection() as conn:
        conn.execute(sql, (now, now, symbols_scanned, signals_logged, alerts_fired, run_id))
        conn.commit()


# ── flow_signals ──────────────────────────────────────────────────────────────
def insert_flow_signal(sig: FlowSignal, scan_run_id: int) -> int:
    """
    Insert a raw UW flow alert.
    Returns flow_signal_id (0 if duplicate ignored).
    """
    sql = """
        INSERT OR IGNORE INTO flow_signals (
            scan_run_id, ingested_at,
            symbol, strike, right, expiry, dte, moneyness,
            structure, intent, premium, ask_side,
            volume, open_interest, iv, delta, uw_score,
            raw_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    with get_connection() as conn:
        cur = conn.execute(sql, (
            scan_run_id, _now(),
            sig.symbol, sig.strike, sig.right, sig.expiry,
            _dte(sig.expiry), sig.moneyness,
            sig.structure, sig.intent, sig.premium,
            1 if sig.ask_side else 0,
            sig.volume, sig.oi, sig.iv, sig.delta, sig.score,
            json.dumps(sig.raw),
        ))
        conn.commit()
        return cur.lastrowid or 0


# ── scored_signals ────────────────────────────────────────────────────────────
def insert_scored_signal(sig: ScoredSignal, scan_run_id: int, flow_signal_id: int = 0) -> int:
    """Insert a fully scored signal. Returns scored_signal_id."""
    sql = """
        INSERT INTO scored_signals (
            scan_run_id, flow_signal_id, scored_at,
            symbol, strike, right, expiry, dte,
            uw_score, additive_score, institutional_score, composite_score,
            intent, structure, premium, ask_side,
            iv_percentile, darkpool_sentiment,
            dealer_gex_m, dealer_dex_m, dealer_vex_m, dealer_chex_m,
            dealer_regime, gamma_flip, call_wall, put_wall,
            flow_direction, gex_blocked,
            live_bid, live_ask, live_delta, live_iv,
            contracts, max_risk_usd, target_premium, stop_premium,
            outcome
        ) VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
    """
    with get_connection() as conn:
        cur = conn.execute(sql, (
            scan_run_id, flow_signal_id or None, _now(),
            sig.symbol, sig.strike, sig.right, sig.expiry, _dte(sig.expiry),
            sig.uw_score, sig.additive_score, sig.institutional_score, sig.composite_score,
            sig.intent, sig.structure, sig.premium,
            1 if sig.ask_side else 0,
            sig.iv_percentile, sig.darkpool_sentiment,
            sig.dealer_gex / 1e6, sig.dealer_dex / 1e6,
            sig.dealer_vex / 1e6, sig.dealer_chex / 1e6,
            sig.dealer_regime, sig.gamma_flip, sig.call_wall, sig.put_wall,
            sig.flow_direction, 1 if sig.gex_regime_blocked else 0,
            sig.live_bid, sig.live_ask, sig.live_delta, sig.live_iv,
            sig.suggested_contracts, sig.max_loss_dollar,
            sig.target_exit_premium, sig.stop_premium,
            "open",
        ))
        conn.commit()
        return cur.lastrowid


def update_signal_outcome(
    scored_signal_id: int,
    exit_premium: float,
    exit_date: str,
    outcome: str,   # win | loss | expired
) -> None:
    """Record trade outcome for backtest analysis."""
    pnl_placeholder = None   # scanner doesn't know contracts; update externally
    sql = """
        UPDATE scored_signals
        SET exit_premium = ?,
            exit_date    = ?,
            outcome      = ?
        WHERE id = ?
    """
    with get_connection() as conn:
        conn.execute(sql, (exit_premium, exit_date, outcome, scored_signal_id))
        conn.commit()


# ── dealer_exposure ───────────────────────────────────────────────────────────
def insert_dealer_exposure(exp: DealerExposure, scan_run_id: int) -> None:
    """Insert GEX/DEX/VEX/CHEX snapshot. Duplicates silently ignored."""
    sql = """
        INSERT OR IGNORE INTO dealer_exposure (
            scan_run_id, captured_at,
            symbol, net_gex, net_dex, net_vex, net_chex,
            gamma_flip, call_wall, put_wall,
            regime, flow_direction
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """
    with get_connection() as conn:
        conn.execute(sql, (
            scan_run_id, _now(),
            exp.symbol, exp.net_gex, exp.net_dex, exp.net_vex, exp.net_chex,
            exp.gamma_flip, exp.call_wall, exp.put_wall,
            exp.regime, exp.flow_direction,
        ))
        conn.commit()


# ── oi_changes ────────────────────────────────────────────────────────────────
def insert_oi_snapshot(
    scan_run_id: int,
    symbol: str,
    strike: float,
    right: str,
    expiry: str,
    open_interest: int,
    volume: int,
    iv: float,
    delta: float,
) -> None:
    """
    Insert an OI snapshot and automatically compute delta vs prior scan.
    Calculates vol/OI ratio for anomaly detection.
    """
    prev_oi = get_prev_oi(symbol, strike, right, expiry)
    oi_delta = (open_interest - prev_oi) if prev_oi is not None else 0
    oi_delta_pct = (
        round(oi_delta / prev_oi * 100, 2) if prev_oi and prev_oi > 0 else None
    )
    vol_oi_ratio = round(volume / open_interest, 4) if open_interest > 0 else None

    sql = """
        INSERT OR IGNORE INTO oi_changes (
            scan_run_id, captured_at,
            symbol, strike, right, expiry,
            open_interest, prev_oi, oi_delta, oi_delta_pct,
            volume, vol_oi_ratio, iv, delta
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    with get_connection() as conn:
        conn.execute(sql, (
            scan_run_id, _now(),
            symbol, strike, right, expiry,
            open_interest, prev_oi, oi_delta, oi_delta_pct,
            volume, vol_oi_ratio, iv, delta,
        ))
        conn.commit()


# ── darkpool_prints ───────────────────────────────────────────────────────────
def insert_darkpool_prints(
    scan_run_id: int,
    symbol: str,
    trades: list[dict],
    overall_sentiment: str,
) -> int:
    """
    Bulk-insert raw dark pool trades for a symbol.
    Returns count of rows inserted.
    """
    if not trades:
        return 0

    # Compute mid to classify individual prints
    prices = [float(t.get("price") or 0) for t in trades if t.get("price")]
    mid    = sum(prices) / len(prices) if prices else 0

    sql = """
        INSERT OR IGNORE INTO darkpool_prints (
            scan_run_id, captured_at, executed_at,
            symbol, price, size, volume_usd,
            side, sentiment, raw_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
    """
    rows = []
    for t in trades:
        price = float(t.get("price") or 0)
        size  = int(t.get("size") or t.get("volume") or 0)
        vol_usd = round(price * size, 2)
        side = "above_mid" if price > mid else "below_mid" if price < mid else "mid"
        rows.append((
            scan_run_id, _now(), t.get("executed_at"),
            symbol, price, size, vol_usd,
            side, overall_sentiment,
            json.dumps(t),
        ))

    with get_connection() as conn:
        conn.executemany(sql, rows)
        conn.commit()
    return len(rows)


# ── Query functions ───────────────────────────────────────────────────────────
def get_prev_oi(symbol: str, strike: float, right: str, expiry: str) -> Optional[int]:
    """Return most recent OI for a contract (used for delta calculation)."""
    sql = """
        SELECT open_interest FROM oi_changes
        WHERE symbol=? AND strike=? AND right=? AND expiry=?
        ORDER BY captured_at DESC LIMIT 1
    """
    with get_connection() as conn:
        row = conn.execute(sql, (symbol, strike, right, expiry)).fetchone()
        return int(row["open_interest"]) if row else None


def get_oi_trend(
    symbol: str,
    strike: float,
    right: str,
    expiry: str,
    lookback_scans: int = 10,
) -> list[dict]:
    """
    Return OI history for a specific contract.
    Useful in the scoring engine: sustained OI growth = institutional conviction.
    """
    sql = """
        SELECT captured_at, open_interest, oi_delta, oi_delta_pct,
               volume, vol_oi_ratio
        FROM oi_changes
        WHERE symbol=? AND strike=? AND right=? AND expiry=?
        ORDER BY captured_at DESC
        LIMIT ?
    """
    with get_connection() as conn:
        rows = conn.execute(sql, (symbol, strike, right, expiry, lookback_scans)).fetchall()
        return [dict(r) for r in rows]


def get_oi_buildup_score(
    symbol: str,
    strike: float,
    right: str,
    expiry: str,
) -> float:
    """
    Returns 0–20 bonus score based on OI buildup consistency.
    Called from AdditiveScorer to reward contracts with sustained OI growth.

    Logic:
      - 2+ consecutive scans with positive OI delta → base +5
      - 4+ scans → +10
      - Vol/OI ratio > 1.5 in latest scan → +5 (unusual volume vs OI)
      - Net OI added > 10,000 contracts → +5
    """
    trend = get_oi_trend(symbol, strike, right, expiry, lookback_scans=5)
    if not trend:
        return 0.0

    bonus = 0.0
    positive_scans = sum(1 for r in trend if (r["oi_delta"] or 0) > 0)
    if positive_scans >= 2:  bonus += 5
    if positive_scans >= 4:  bonus += 5

    latest = trend[0]
    if latest.get("vol_oi_ratio") and latest["vol_oi_ratio"] > 1.5:
        bonus += 5

    total_added = sum(r["oi_delta"] or 0 for r in trend)
    if total_added > 10_000:
        bonus += 5

    return min(bonus, 20.0)


def get_gex_history(symbol: str, lookback_scans: int = 20) -> list[dict]:
    """
    Return GEX time series for a symbol.
    Use for trend direction: is GEX improving or deteriorating?
    """
    sql = """
        SELECT captured_at, net_gex, net_dex, net_vex, net_chex,
               gamma_flip, call_wall, put_wall, regime, flow_direction
        FROM dealer_exposure
        WHERE symbol = ?
        ORDER BY captured_at DESC
        LIMIT ?
    """
    with get_connection() as conn:
        rows = conn.execute(sql, (symbol, lookback_scans)).fetchall()
        return [dict(r) for r in rows]


def get_gex_trend_score(symbol: str) -> float:
    """
    Returns −10 to +10 adjustment based on GEX trend direction.
    Called from InstitutionalFlowScorer.

    Logic:
      GEX improving (increasingly positive over last 3 scans) → +10 bullish boost
      GEX deteriorating (increasingly negative) → +10 bearish boost / −10 bullish
    """
    history = get_gex_history(symbol, lookback_scans=3)
    if len(history) < 2:
        return 0.0
    latest = history[0].get("net_gex") or 0
    prior  = history[-1].get("net_gex") or 0
    delta  = latest - prior
    if delta > 50_000_000:   return 10.0    # GEX rising → positive
    if delta < -50_000_000:  return -10.0   # GEX falling → negative
    return 0.0


def get_dp_sentiment_trend(symbol: str, lookback_scans: int = 5) -> str:
    """
    Return dominant DP sentiment across last N scans for a symbol.
    More reliable than a single-scan read.
    Returns: 'bullish' | 'bearish' | 'neutral'
    """
    sql = """
        SELECT sentiment, COUNT(*) as cnt
        FROM darkpool_prints
        WHERE symbol = ?
          AND captured_at >= datetime('now', ?)
        GROUP BY sentiment
        ORDER BY cnt DESC
        LIMIT 1
    """
    window = f"-{lookback_scans * 10} minutes"
    with get_connection() as conn:
        row = conn.execute(sql, (symbol, window)).fetchone()
        return row["sentiment"] if row else "neutral"


def get_signal_history(
    symbol: str,
    lookback_days: int = 30,
    min_score: float = 50,
) -> list[dict]:
    """
    Return prior scored signals for a symbol.
    Useful for checking if a signal is a repeat of a prior call.
    """
    sql = """
        SELECT scored_at, strike, right, expiry, composite_score,
               intent, structure, outcome, live_ask, live_delta
        FROM scored_signals
        WHERE symbol = ?
          AND scored_at >= datetime('now', ?)
          AND composite_score >= ?
        ORDER BY scored_at DESC
        LIMIT 20
    """
    window = f"-{lookback_days} days"
    with get_connection() as conn:
        rows = conn.execute(sql, (symbol, window, min_score)).fetchall()
        return [dict(r) for r in rows]


def get_outcome_stats(symbol: str = None) -> list[dict]:
    """
    Win/loss summary by symbol + intent + regime.
    Pass symbol=None for full market summary.
    Used in --mode backtest output.
    """
    where = "WHERE outcome IN ('win','loss')"
    params = []
    if symbol:
        where += " AND symbol = ?"
        params.append(symbol)

    sql = f"""
        SELECT
            symbol,
            intent,
            dealer_regime,
            COUNT(*)  AS total,
            SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END)  AS wins,
            SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses,
            ROUND(AVG(composite_score), 1)  AS avg_score,
            ROUND(AVG(pnl_usd), 2)          AS avg_pnl,
            ROUND(SUM(pnl_usd), 2)          AS total_pnl
        FROM scored_signals
        {where}
        GROUP BY symbol, intent, dealer_regime
        ORDER BY total_pnl DESC
    """
    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_recent_scan_runs(limit: int = 10) -> list[dict]:
    """Return recent scan run metadata for monitoring."""
    sql = """
        SELECT id, started_at, finished_at, duration_sec,
               symbols_scanned, signals_logged, alerts_fired,
               portfolio_value, spy_gex_m, spy_regime
        FROM scan_runs
        ORDER BY started_at DESC
        LIMIT ?
    """
    with get_connection() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
        return [dict(r) for r in rows]


# ── open_positions ────────────────────────────────────────────────────────────
def add_position(
    symbol: str,
    strike: float,
    right: str,
    expiry: str,
    contracts: int,
    entry_premium: float,
    target_premium: float = None,
    stop_premium: float = None,
    scored_signal_id: int = None,
) -> int:
    """
    Add a new open position for exit alert monitoring.
    Can be called manually or linked to a scored_signal.

    target_premium defaults to entry * 1.65 (+65%)
    stop_premium   defaults to entry * 0.30 (-70%)
    """
    if target_premium is None:
        target_premium = round(entry_premium * 1.65, 2)
    if stop_premium is None:
        stop_premium = round(entry_premium * 0.30, 2)
    breakeven = entry_premium  # initial stop = entry cost

    sql = """
        INSERT INTO open_positions (
            scored_signal_id, opened_at,
            symbol, strike, right, expiry, contracts,
            entry_premium, target_premium, stop_premium, breakeven_premium,
            status
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """
    with get_connection() as conn:
        cur = conn.execute(sql, (
            scored_signal_id, _now(),
            symbol, strike, right, expiry, contracts,
            entry_premium, target_premium, stop_premium, breakeven,
            "open"
        ))
        conn.commit()
        log.info("Position added: {} {} {} {} x{} entry={:.2f}".format(
            symbol, strike, right, expiry, contracts, entry_premium))
        return cur.lastrowid


def get_open_positions() -> list[dict]:
    """Return all open positions for exit monitoring."""
    sql = """
        SELECT * FROM open_positions
        WHERE status = 'open'
        ORDER BY opened_at DESC
    """
    with get_connection() as conn:
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]


def update_position_price(position_id: int, current_price: float):
    """Update last known price for a position."""
    sql = """
        UPDATE open_positions
        SET last_price = ?, last_checked_at = ?
        WHERE id = ?
    """
    with get_connection() as conn:
        conn.execute(sql, (current_price, _now(), position_id))
        conn.commit()


def mark_position_alerted(position_id: int, alert_type: str):
    """
    Mark that an alert has been sent for this position.
    alert_type: target | stop | breakeven
    """
    col = "{}_alerted".format(alert_type)
    sql = "UPDATE open_positions SET {} = 1 WHERE id = ?".format(col)
    with get_connection() as conn:
        conn.execute(sql, (position_id,))
        conn.commit()


def close_position(
    position_id: int,
    exit_premium: float,
    exit_reason: str,  # target_hit | stop_hit | manual | expired
):
    """Close a position and record P&L."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT entry_premium, contracts FROM open_positions WHERE id = ?",
            (position_id,)
        ).fetchone()
        if not row:
            return

        pnl_per = round(exit_premium - row["entry_premium"], 2)
        pnl_tot = round(pnl_per * row["contracts"] * 100, 2)

        conn.execute("""
            UPDATE open_positions
            SET status           = 'closed',
                exit_premium     = ?,
                exit_date        = ?,
                exit_reason      = ?,
                pnl_per_contract = ?,
                pnl_total        = ?
            WHERE id = ?
        """, (exit_premium, _now(), exit_reason, pnl_per, pnl_tot, position_id))
        conn.commit()
        log.info("Position {} closed: {} exit={:.2f} pnl=${:.0f}".format(
            position_id, exit_reason, exit_premium, pnl_tot))


def get_position_summary() -> list[dict]:
    """Return P&L summary for all closed positions."""
    sql = """
        SELECT symbol, right, exit_reason,
               COUNT(*) as trades,
               SUM(pnl_total) as total_pnl,
               AVG(pnl_per_contract) as avg_pnl_per,
               SUM(CASE WHEN pnl_total > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN pnl_total < 0 THEN 1 ELSE 0 END) as losses
        FROM open_positions
        WHERE status = 'closed'
        GROUP BY symbol, right, exit_reason
        ORDER BY total_pnl DESC
    """
    with get_connection() as conn:
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]
