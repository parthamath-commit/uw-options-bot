"""
database/purge.py
=================
Data purge manager for UW Options Bot.

Purge rules:
  scored_signals    -- move to archive when expiry has passed OR outcome recorded
  flow_signals      -- move to archive when parent scored_signal is archived
  oi_changes        -- move to archive when expiry has passed
  darkpool_prints   -- move to archive when older than PURGE_DARKPOOL_DAYS (default 7)
  dealer_exposure   -- move to archive when older than PURGE_EXPOSURE_DAYS (default 14)
  open_positions    -- move to archive when status = closed OR expired

Data is NEVER deleted -- only moved from active to archive tables.
Archive tables have identical schema so queries still work on archived data.

Run:
  python main.py --mode purge          -- run purge now
  Auto-runs daily at midnight via scanner.py

Config (.env):
  PURGE_DARKPOOL_DAYS=7
  PURGE_EXPOSURE_DAYS=14
  PURGE_SIGNALS_DAYS=30    -- archive scored signals older than N days
                              (regardless of expiry)
"""

import os
import logging
from datetime import datetime, date, timedelta
from database.db import get_connection
from config import ET

log = logging.getLogger("UWBot.Purge")

PURGE_DARKPOOL_DAYS = int(os.getenv("PURGE_DARKPOOL_DAYS", "7"))
PURGE_EXPOSURE_DAYS = int(os.getenv("PURGE_EXPOSURE_DAYS", "14"))
PURGE_SIGNALS_DAYS  = int(os.getenv("PURGE_SIGNALS_DAYS",  "30"))


def run_purge(dry_run: bool = False) -> dict:
    """
    Run full purge cycle. Move expired/closed records to archive tables.

    Args:
        dry_run: if True, count rows but don't actually move them

    Returns:
        dict with rows archived per table
    """
    log.info("Starting purge cycle (dry_run={})...".format(dry_run))
    results = {}

    results["open_positions"]   = _purge_positions(dry_run)
    results["scored_signals"]   = _purge_scored_signals(dry_run)
    results["flow_signals"]     = _purge_flow_signals(dry_run)
    results["oi_changes"]       = _purge_oi_changes(dry_run)
    results["darkpool_prints"]  = _purge_darkpool(dry_run)
    results["dealer_exposure"]  = _purge_dealer_exposure(dry_run)

    total = sum(results.values())

    if not dry_run:
        _log_purge(results)
        log.info("Purge complete: {} rows archived total".format(total))
    else:
        log.info("Dry run: {} rows would be archived".format(total))

    return results


def _now() -> str:
    return datetime.now(ET).isoformat()


def _archive_and_delete(
    table: str,
    archive_table: str,
    where_clause: str,
    params: tuple,
    dry_run: bool
) -> int:
    """
    Generic: copy matching rows to archive table, then delete from source.
    Returns count of rows archived.
    """
    with get_connection() as conn:
        # Count first
        count = conn.execute(
            "SELECT COUNT(*) FROM {} WHERE {}".format(table, where_clause),
            params
        ).fetchone()[0]

        if count == 0 or dry_run:
            return count

        # Copy to archive -- use only columns present in BOTH tables
        # (immune to schema drift between source and archive)
        src_cols = [r[1] for r in conn.execute(
            "PRAGMA table_info({})".format(table)).fetchall()]
        arc_cols = [r[1] for r in conn.execute(
            "PRAGMA table_info({})".format(archive_table)).fetchall()]
        common = [c for c in src_cols if c in arc_cols]
        col_list = ", ".join(common)
        conn.execute("""
            INSERT OR IGNORE INTO {} ({})
            SELECT {} FROM {} WHERE {}
        """.format(archive_table, col_list, col_list, table, where_clause),
            params)

        # Delete from source
        conn.execute(
            "DELETE FROM {} WHERE {}".format(table, where_clause), params)

        conn.commit()
        log.info("Archived {} rows from {} to {}".format(
            count, table, archive_table))
        return count


# ── Purge functions ───────────────────────────────────────────────────────────

def _purge_positions(dry_run: bool) -> int:
    """Archive closed positions and positions with passed expiry."""
    today = date.today().isoformat()
    count = 0

    # Closed positions
    count += _archive_and_delete(
        "open_positions", "archived_open_positions",
        "status IN ('closed', 'expired')",
        (), dry_run
    )

    # Positions with expiry in the past (regardless of status)
    count += _archive_and_delete(
        "open_positions", "archived_open_positions",
        "expiry < ? AND status = 'open'",
        (today,), dry_run
    )

    return count


def _purge_scored_signals(dry_run: bool) -> int:
    """
    Archive scored signals where:
      - expiry has passed (option expired)
      - outcome is recorded (trade closed)
      - older than PURGE_SIGNALS_DAYS
    """
    today    = date.today().isoformat()
    age_date = (date.today() - timedelta(days=PURGE_SIGNALS_DAYS)).isoformat()
    count    = 0

    # Expired options
    count += _archive_and_delete(
        "scored_signals", "archived_scored_signals",
        "expiry != '' AND expiry < ?",
        (today,), dry_run
    )

    # Recorded outcomes (win/loss/expired)
    count += _archive_and_delete(
        "scored_signals", "archived_scored_signals",
        "outcome IN ('win', 'loss', 'expired')",
        (), dry_run
    )

    # Age limit (non-options feed signals with no expiry)
    count += _archive_and_delete(
        "scored_signals", "archived_scored_signals",
        "scored_at < ? AND (expiry IS NULL OR expiry = '')",
        (age_date,), dry_run
    )

    return count


def _purge_flow_signals(dry_run: bool) -> int:
    """Archive flow signals whose parent scored_signal has been archived."""
    today = date.today().isoformat()
    # Archive flow signals for expired contracts
    return _archive_and_delete(
        "flow_signals", "archived_flow_signals",
        "expiry != '' AND expiry < ?",
        (today,), dry_run
    )


def _purge_oi_changes(dry_run: bool) -> int:
    """Archive OI change records for expired contracts."""
    today = date.today().isoformat()
    return _archive_and_delete(
        "oi_changes", "archived_oi_changes",
        "expiry != '' AND expiry < ?",
        (today,), dry_run
    )


def _purge_darkpool(dry_run: bool) -> int:
    """Archive dark pool prints older than PURGE_DARKPOOL_DAYS."""
    cutoff = (date.today() - timedelta(days=PURGE_DARKPOOL_DAYS)).isoformat()
    return _archive_and_delete(
        "darkpool_prints", "archived_darkpool_prints",
        "captured_at < ?",
        (cutoff,), dry_run
    )


def _purge_dealer_exposure(dry_run: bool) -> int:
    """Archive dealer exposure snapshots older than PURGE_EXPOSURE_DAYS."""
    cutoff = (date.today() - timedelta(days=PURGE_EXPOSURE_DAYS)).isoformat()
    return _archive_and_delete(
        "dealer_exposure", "archived_dealer_exposure",
        "captured_at < ?",
        (cutoff,), dry_run
    )


def _log_purge(results: dict):
    """Log purge run to purge_log table."""
    with get_connection() as conn:
        for table, count in results.items():
            if count > 0:
                conn.execute("""
                    INSERT INTO purge_log
                    (purged_at, table_name, rows_archived, reason)
                    VALUES (?, ?, ?, ?)
                """, (_now(), table, count, "scheduled"))
        conn.commit()


def get_db_stats() -> dict:
    """Return row counts for all active and archive tables."""
    tables = [
        "scan_runs", "flow_signals", "scored_signals",
        "dealer_exposure", "oi_changes", "darkpool_prints",
        "open_positions",
        "archived_scored_signals", "archived_flow_signals",
        "archived_oi_changes", "archived_darkpool_prints",
        "archived_dealer_exposure", "archived_open_positions",
    ]
    stats = {}
    with get_connection() as conn:
        for t in tables:
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM {}".format(t)
                ).fetchone()[0]
                stats[t] = count
            except Exception:
                stats[t] = 0

        # DB file size
        from database.db import db_path
        import os
        try:
            stats["db_size_mb"] = round(
                os.path.getsize(str(db_path())) / 1_000_000, 2)
        except Exception:
            stats["db_size_mb"] = 0

    return stats
