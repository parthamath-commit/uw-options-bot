"""
database/db.py
==============
SQLite connection manager for UW Options Bot.

- Single database file: output/uw_bot.db
- WAL mode for safe concurrent reads during analysis
- Auto-creates all tables + views on first run
- Thread-safe connection-per-call pattern (safe for single-threaded bot)
"""

import os
import sqlite3
import logging
from pathlib import Path
from config import cfg

log = logging.getLogger("UWBot.DB")

# Database lives alongside signals.xlsx in output/
_DB_PATH = Path(os.getenv("DB_PATH", str(Path(cfg.EXCEL_PATH).parent / "uw_bot.db")))
_SCHEMA  = Path(__file__).parent / "schema.sql"


def get_connection() -> sqlite3.Connection:
    """
    Return a new SQLite connection with sensible defaults.
    Row factory set to sqlite3.Row so columns are accessible by name.
    """
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db():
    """
    Create all tables and views from schema.sql if they don't exist.
    Also runs migrations for columns added after initial creation.
    Safe to call on every bot startup.
    """
    ddl = _SCHEMA.read_text()
    with get_connection() as conn:
        statements = [s.strip() for s in ddl.split(";") if s.strip()]
        for stmt in statements:
            try:
                conn.execute(stmt)
            except sqlite3.Error as e:
                if "already exists" not in str(e).lower():
                    log.warning("Schema error: {}\nStatement: {}".format(e, stmt[:80]))
        conn.commit()

        # ── Migrations: add columns that may be missing from older DBs ────────
        # Evidence log table (created once on startup)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS score_evidence (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                scored_at       TEXT    NOT NULL,
                symbol          TEXT    NOT NULL,
                strike          REAL,
                right           TEXT,
                expiry          TEXT,
                intent          TEXT,
                premium         REAL,
                structure       TEXT,
                -- Additive components
                ev_structure    REAL,
                ev_intent       REAL,
                ev_uw_score     REAL,
                ev_iv_pct       REAL,
                ev_iv_bonus     REAL,
                ev_gex_regime   REAL,
                ev_darkpool     REAL,
                ev_premium_size REAL,
                additive_total  REAL,
                -- Institutional components
                ev_vex          REAL,
                ev_chex         REAL,
                ev_flow_dir     REAL,
                ev_dex          REAL,
                ev_ask_side     REAL,
                institutional_total REAL,
                -- Context
                iv_pct          REAL,
                gex_regime      TEXT,
                flow_direction  TEXT,
                net_vex         REAL,
                net_chex        REAL,
                net_dex         REAL,
                darkpool_sent   TEXT,
                -- Final
                composite_score REAL,
                alerted         INTEGER DEFAULT 0,
                outcome         TEXT,
                pnl_pct         REAL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ev_symbol
            ON score_evidence(symbol, scored_at)
        """)

        migrations = [
            ("flow_signals",             "volume_oi_ratio",   "REAL"),
            ("flow_signals",             "structure",         "TEXT"),
            ("archived_flow_signals",    "volume_oi_ratio",   "REAL"),
            ("archived_flow_signals",    "structure",         "TEXT"),
            ("archived_scored_signals",  "rule_type",         "TEXT"),
            ("archived_scored_signals",  "ml_validation",     "TEXT"),
            ("archived_scored_signals",  "signal_type",       "TEXT"),
            ("scored_signals",  "rule_type",         "TEXT DEFAULT \'unidirectional\'"),
            ("scored_signals",  "ml_validation",     "TEXT DEFAULT \'rule_only\'"),
            ("scored_signals",  "signal_type",       "TEXT DEFAULT \'unidirectional\'"),
            ("open_positions",  "breakeven_premium", "REAL"),
            ("open_positions",  "last_price",        "REAL"),
            ("open_positions",  "last_checked_at",   "TEXT"),
        ]
        for table, col, col_type in migrations:
            try:
                conn.execute(
                    "ALTER TABLE {} ADD COLUMN {} {}".format(table, col, col_type))
                conn.commit()
                log.info("Migration: added {}.{}".format(table, col))
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    log.debug("Migration skip {}.{}: {}".format(table, col, e))

    log.info("Database ready: {}".format(_DB_PATH))


def db_path() -> Path:
    return _DB_PATH
