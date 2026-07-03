-- ─────────────────────────────────────────────────────────────────────────────
-- UW Options Bot — SQLite Schema
-- Pal Initiatives LLC
--
-- Tables
-- ──────
--   scan_runs         audit trail for every scan cycle
--   flow_signals      raw UW flow alerts (one row per alert per scan)
--   scored_signals    fully scored + enriched signals
--   dealer_exposure   GEX/DEX/VEX/CHEX snapshot per symbol per scan
--   oi_changes        open interest delta per strike per scan
--   darkpool_prints   raw dark pool trades per symbol per scan
-- ─────────────────────────────────────────────────────────────────────────────

PRAGMA journal_mode = WAL;       -- safe concurrent writes
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

-- ── scan_runs ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scan_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT    NOT NULL,          -- ISO-8601 ET
    finished_at     TEXT,
    duration_sec    REAL,
    symbols_scanned INTEGER DEFAULT 0,
    signals_logged  INTEGER DEFAULT 0,
    alerts_fired    INTEGER DEFAULT 0,
    portfolio_value REAL,
    spy_gex_m       REAL,                      -- SPY GEX in $M at scan time
    spy_regime      TEXT,
    notes           TEXT
);

-- ── flow_signals ──────────────────────────────────────────────────────────────
-- One row per UW flow alert ingested.
-- Deduped on (symbol, strike, right, expiry, structure, alert_ts).
CREATE TABLE IF NOT EXISTS flow_signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_run_id     INTEGER REFERENCES scan_runs(id),
    ingested_at     TEXT    NOT NULL,
    -- Contract
    symbol          TEXT    NOT NULL,
    strike          REAL    NOT NULL,
    right           TEXT    NOT NULL CHECK (right IN ('C','P')),
    expiry          TEXT    NOT NULL,          -- YYYY-MM-DD
    dte             INTEGER,                   -- days to expiry at ingest
    moneyness       TEXT,
    -- Flow metadata
    structure       TEXT,                      -- sweep | block
    intent          TEXT,                      -- bullish | bearish | neutral
    premium         REAL,                      -- total premium $
    ask_side        INTEGER DEFAULT 0,         -- 1 = aggressor at/above ask
    volume          INTEGER,
    open_interest   INTEGER,
    iv              REAL,                      -- decimal
    delta           REAL,
    uw_score        INTEGER,                   -- synthesised 0-100
    -- Raw JSON for full replay
    raw_json        TEXT,
    UNIQUE (symbol, strike, right, expiry, structure, ingested_at)
);

CREATE INDEX IF NOT EXISTS idx_fs_symbol    ON flow_signals(symbol);
CREATE INDEX IF NOT EXISTS idx_fs_ingested  ON flow_signals(ingested_at);
CREATE INDEX IF NOT EXISTS idx_fs_intent    ON flow_signals(intent);
CREATE INDEX IF NOT EXISTS idx_fs_score     ON flow_signals(uw_score);
CREATE INDEX IF NOT EXISTS idx_fs_expiry    ON flow_signals(expiry);

-- ── scored_signals ────────────────────────────────────────────────────────────
-- One row per signal that passed MIN_SCORE_LOG threshold.
CREATE TABLE IF NOT EXISTS scored_signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_run_id         INTEGER REFERENCES scan_runs(id),
    flow_signal_id      INTEGER REFERENCES flow_signals(id),
    scored_at           TEXT    NOT NULL,
    -- Contract
    symbol              TEXT    NOT NULL,
    strike              REAL    NOT NULL,
    right               TEXT    NOT NULL,
    expiry              TEXT    NOT NULL,
    dte                 INTEGER,
    -- Scores
    uw_score            INTEGER,
    additive_score      REAL,
    institutional_score REAL,
    composite_score     REAL    NOT NULL,
    -- Signal context
    intent              TEXT,
    structure           TEXT,
    premium             REAL,
    ask_side            INTEGER DEFAULT 0,
    iv_percentile       REAL,
    darkpool_sentiment  TEXT,
    -- Dealer context
    dealer_gex_m        REAL,
    dealer_dex_m        REAL,
    dealer_vex_m        REAL,
    dealer_chex_m       REAL,
    dealer_regime       TEXT,
    gamma_flip          REAL,
    call_wall           REAL,
    put_wall            REAL,
    flow_direction      TEXT,
    gex_blocked         INTEGER DEFAULT 0,
    -- Live quote
    live_bid            REAL,
    live_ask            REAL,
    live_delta          REAL,
    live_iv             REAL,
    -- Sizing
    contracts           INTEGER,
    max_risk_usd        REAL,
    target_premium      REAL,
    stop_premium        REAL,
    -- Outcome tracking (filled in later)
    exit_premium        REAL,
    exit_date           TEXT,
    pnl_usd             REAL,
    outcome             TEXT    -- win | loss | open | expired
);

CREATE INDEX IF NOT EXISTS idx_ss_symbol    ON scored_signals(symbol);
CREATE INDEX IF NOT EXISTS idx_ss_scored_at ON scored_signals(scored_at);
CREATE INDEX IF NOT EXISTS idx_ss_composite ON scored_signals(composite_score);
CREATE INDEX IF NOT EXISTS idx_ss_outcome   ON scored_signals(outcome);

-- ── dealer_exposure ───────────────────────────────────────────────────────────
-- GEX/DEX/VEX/CHEX snapshot per symbol per scan cycle.
-- Use for GEX trend analysis and regime history.
CREATE TABLE IF NOT EXISTS dealer_exposure (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_run_id     INTEGER REFERENCES scan_runs(id),
    captured_at     TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    -- Greek exposures ($)
    net_gex         REAL,
    net_dex         REAL,
    net_vex         REAL,
    net_chex        REAL,
    -- Key levels
    gamma_flip      REAL,
    call_wall       REAL,
    put_wall        REAL,
    -- Regime
    regime          TEXT,
    flow_direction  TEXT,
    UNIQUE (symbol, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_de_symbol     ON dealer_exposure(symbol);
CREATE INDEX IF NOT EXISTS idx_de_captured   ON dealer_exposure(captured_at);
CREATE INDEX IF NOT EXISTS idx_de_regime     ON dealer_exposure(regime);

-- ── oi_changes ────────────────────────────────────────────────────────────────
-- Open interest delta per symbol/strike/expiry/right between scans.
-- Tracks OI buildup — sustained OI growth at a strike = institutional conviction.
CREATE TABLE IF NOT EXISTS oi_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_run_id     INTEGER REFERENCES scan_runs(id),
    captured_at     TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    strike          REAL    NOT NULL,
    right           TEXT    NOT NULL,
    expiry          TEXT    NOT NULL,
    -- OI snapshot
    open_interest   INTEGER,
    prev_oi         INTEGER,                   -- OI from prior scan
    oi_delta        INTEGER,                   -- open_interest - prev_oi
    oi_delta_pct    REAL,                      -- % change
    volume          INTEGER,
    vol_oi_ratio    REAL,                      -- volume / OI (anomaly signal)
    iv              REAL,
    delta           REAL,
    UNIQUE (symbol, strike, right, expiry, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_oi_symbol    ON oi_changes(symbol);
CREATE INDEX IF NOT EXISTS idx_oi_captured  ON oi_changes(captured_at);
CREATE INDEX IF NOT EXISTS idx_oi_delta     ON oi_changes(oi_delta);
CREATE INDEX IF NOT EXISTS idx_oi_ratio     ON oi_changes(vol_oi_ratio);

-- ── darkpool_prints ───────────────────────────────────────────────────────────
-- Raw dark pool / off-exchange prints per symbol.
-- Use for DP trend analysis and cross-referencing flow signals.
CREATE TABLE IF NOT EXISTS darkpool_prints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_run_id     INTEGER REFERENCES scan_runs(id),
    captured_at     TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    price           REAL,
    size            INTEGER,
    volume_usd      REAL,                      -- price × size
    side            TEXT,                      -- above_mid | below_mid | mid
    sentiment       TEXT,                      -- bullish | bearish | neutral
    raw_json        TEXT,
    UNIQUE (symbol, price, size, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_dp_symbol    ON darkpool_prints(symbol);
CREATE INDEX IF NOT EXISTS idx_dp_captured  ON darkpool_prints(captured_at);
CREATE INDEX IF NOT EXISTS idx_dp_sentiment ON darkpool_prints(sentiment);

-- ── open_positions ──────────────────────────────────────────────────────────
-- Tracks active positions for exit alert monitoring.
-- Populated manually or via future semi-auto entry tracking.
CREATE TABLE IF NOT EXISTS open_positions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    scored_signal_id    INTEGER REFERENCES scored_signals(id),
    opened_at           TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    strike              REAL    NOT NULL,
    right               TEXT    NOT NULL,
    expiry              TEXT    NOT NULL,
    contracts           INTEGER NOT NULL DEFAULT 1,
    entry_premium       REAL    NOT NULL,   -- premium paid per contract
    target_premium      REAL    NOT NULL,   -- +65% target
    stop_premium        REAL    NOT NULL,   -- -70% stop
    breakeven_premium   REAL,               -- entry cost (after moving stop)
    status              TEXT    NOT NULL DEFAULT 'open',  -- open | closed | expired
    -- Exit tracking
    exit_premium        REAL,
    exit_date           TEXT,
    exit_reason         TEXT,   -- target_hit | stop_hit | manual | expired
    pnl_per_contract    REAL,
    pnl_total           REAL,
    -- Alert tracking
    target_alerted      INTEGER DEFAULT 0,
    stop_alerted        INTEGER DEFAULT 0,
    breakeven_alerted   INTEGER DEFAULT 0,
    last_checked_at     TEXT,
    last_price          REAL
);

CREATE INDEX IF NOT EXISTS idx_pos_symbol  ON open_positions(symbol);
CREATE INDEX IF NOT EXISTS idx_pos_status  ON open_positions(status);
CREATE INDEX IF NOT EXISTS idx_pos_expiry  ON open_positions(expiry);

-- ── Archive tables (purged data moved here, never deleted) ──────────────────
CREATE TABLE IF NOT EXISTS archived_scored_signals AS
    SELECT * FROM scored_signals WHERE 0;   -- same schema, empty

CREATE TABLE IF NOT EXISTS archived_flow_signals AS
    SELECT * FROM flow_signals WHERE 0;

CREATE TABLE IF NOT EXISTS archived_oi_changes AS
    SELECT * FROM oi_changes WHERE 0;

CREATE TABLE IF NOT EXISTS archived_darkpool_prints AS
    SELECT * FROM darkpool_prints WHERE 0;

CREATE TABLE IF NOT EXISTS archived_dealer_exposure AS
    SELECT * FROM dealer_exposure WHERE 0;

CREATE TABLE IF NOT EXISTS archived_open_positions AS
    SELECT * FROM open_positions WHERE 0;

-- Purge log -- tracks every purge run
CREATE TABLE IF NOT EXISTS purge_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    purged_at       TEXT    NOT NULL,
    table_name      TEXT    NOT NULL,
    rows_archived   INTEGER DEFAULT 0,
    rows_deleted    INTEGER DEFAULT 0,
    reason          TEXT    -- expired | closed | age_limit
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Useful views for analysis
-- ─────────────────────────────────────────────────────────────────────────────

-- GEX trend per symbol over last 20 scans
CREATE VIEW IF NOT EXISTS v_gex_trend AS
SELECT
    symbol,
    captured_at,
    net_gex / 1000000.0  AS gex_m,
    net_dex / 1000000.0  AS dex_m,
    regime,
    gamma_flip,
    flow_direction
FROM dealer_exposure
ORDER BY symbol, captured_at DESC;

-- OI buildup — strikes with consistent OI growth
CREATE VIEW IF NOT EXISTS v_oi_buildup AS
SELECT
    symbol,
    strike,
    right,
    expiry,
    COUNT(*)             AS scan_count,
    SUM(oi_delta)        AS total_oi_added,
    AVG(vol_oi_ratio)    AS avg_vol_oi_ratio,
    MAX(open_interest)   AS latest_oi,
    MAX(captured_at)     AS last_seen
FROM oi_changes
WHERE oi_delta > 0
GROUP BY symbol, strike, right, expiry
HAVING scan_count >= 2
ORDER BY total_oi_added DESC;

-- Top scored signals last 7 days
CREATE VIEW IF NOT EXISTS v_top_signals_7d AS
SELECT
    scored_at,
    symbol,
    strike,
    right,
    expiry,
    composite_score,
    intent,
    structure,
    premium,
    darkpool_sentiment,
    dealer_regime,
    gamma_flip,
    live_ask,
    live_delta,
    outcome
FROM scored_signals
WHERE scored_at >= datetime('now', '-7 days')
ORDER BY composite_score DESC;

-- Dark pool sentiment trend per symbol
CREATE VIEW IF NOT EXISTS v_dp_trend AS
SELECT
    symbol,
    DATE(captured_at) AS trade_date,
    sentiment,
    COUNT(*)          AS print_count,
    SUM(volume_usd)   AS total_usd
FROM darkpool_prints
GROUP BY symbol, DATE(captured_at), sentiment
ORDER BY symbol, trade_date DESC;

-- Signal outcome summary (for backtest analysis)
CREATE VIEW IF NOT EXISTS v_outcome_summary AS
SELECT
    symbol,
    intent,
    structure,
    dealer_regime,
    COUNT(*)                                       AS total_signals,
    SUM(CASE WHEN outcome = 'win'  THEN 1 ELSE 0 END) AS wins,
    SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) AS losses,
    ROUND(AVG(composite_score), 1)                 AS avg_score,
    ROUND(AVG(pnl_usd), 2)                         AS avg_pnl,
    ROUND(SUM(pnl_usd), 2)                         AS total_pnl
FROM scored_signals
WHERE outcome IS NOT NULL
GROUP BY symbol, intent, structure, dealer_regime
ORDER BY total_pnl DESC;
