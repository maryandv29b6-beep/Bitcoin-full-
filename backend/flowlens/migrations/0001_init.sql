-- FlowLens schema v1. All times are ms since epoch, UTC.
-- Prices in candles are integers scaled by 1e8; quantities are integers scaled by 1e8
-- (base units); notionals (quote units) are REAL and only used for display/statistics.

CREATE TABLE IF NOT EXISTS instruments (
    id            INTEGER PRIMARY KEY,
    exchange      TEXT NOT NULL,
    market        TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    base          TEXT NOT NULL,
    quote         TEXT NOT NULL,
    tick_size     TEXT NOT NULL,
    qty_step      TEXT NOT NULL,
    contract_spec TEXT NOT NULL DEFAULT '{}',
    meta          TEXT NOT NULL DEFAULT '{}',      -- storage steps (footprint/heatmap), first seen
    UNIQUE (exchange, market, symbol)
);

CREATE TABLE IF NOT EXISTS candles_1m (
    instrument_id INTEGER NOT NULL,
    t          INTEGER NOT NULL,
    o INTEGER, h INTEGER, l INTEGER, c INTEGER,
    v_buy      INTEGER, v_sell INTEGER, v_unknown INTEGER,
    q_buy      REAL,    q_sell REAL,    q_unknown REAL,
    n_buy      INTEGER, n_sell INTEGER, n_unknown INTEGER, n_total INTEGER,
    first_id   INTEGER, last_id INTEGER,
    source     TEXT NOT NULL,          -- trades | kline | kline_reconciled
    complete   INTEGER NOT NULL,       -- 1 = stream continuous for the whole minute
    gap_count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (instrument_id, t)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS depth_1m (
    instrument_id INTEGER NOT NULL,
    t           INTEGER NOT NULL,
    samples     INTEGER NOT NULL,      -- valid book samples
    ticks       INTEGER NOT NULL,      -- sampling ticks while the collector ran
    mid REAL, best_bid REAL, best_ask REAL, spread_bps REAL, lag_ms REAL,
    epoch_start INTEGER NOT NULL DEFAULT 0,  -- 1 = a new valid book sequence started in this minute
    bands       TEXT NOT NULL,         -- JSON {r: [bidQty, bidNotional, askQty, askNotional, covBid, covAsk]}
    PRIMARY KEY (instrument_id, t)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS heatmap_1m (
    instrument_id INTEGER NOT NULL,
    t        INTEGER NOT NULL,
    step     INTEGER NOT NULL,         -- bucket size, price scaled 1e8
    origin   INTEGER NOT NULL,         -- bucket index of element 0 (price = index * step)
    n        INTEGER NOT NULL,
    samples  INTEGER NOT NULL,
    data     BLOB NOT NULL,            -- zlib(float32[4n]): bid_mean, ask_mean, bid_max, ask_max (base qty, NaN = not covered)
    PRIMARY KEY (instrument_id, t)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS footprint_1m (
    instrument_id INTEGER NOT NULL,
    t        INTEGER NOT NULL,
    step     INTEGER NOT NULL,
    partial  INTEGER NOT NULL,
    data     BLOB NOT NULL,            -- zlib(JSON [[bucket, buyQty, sellQty, buyN, sellN], ...]) qty scaled 1e8
    PRIMARY KEY (instrument_id, t)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS deriv_1m (
    instrument_id INTEGER NOT NULL,
    t INTEGER NOT NULL,
    oi REAL, oi_value REAL, funding REAL, next_funding INTEGER, mark REAL,
    liq_long_q REAL, liq_short_q REAL, liq_long_v REAL, liq_short_v REAL,
    liq_long_n INTEGER, liq_short_n INTEGER,
    PRIMARY KEY (instrument_id, t)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS quality_events (
    id INTEGER PRIMARY KEY,
    instrument_id INTEGER NOT NULL,
    t_start INTEGER NOT NULL,
    t_end   INTEGER,
    kind    TEXT NOT NULL,             -- book_gap | resync | trade_gap | trade_gap_filled | disconnect | stale | refresh
    detail  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS quality_events_t ON quality_events (instrument_id, t_start);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_rules (
    id         INTEGER PRIMARY KEY,
    created    INTEGER NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    instrument TEXT NOT NULL,
    rule       TEXT NOT NULL,          -- JSON
    last_fired INTEGER
);

CREATE TABLE IF NOT EXISTS alert_log (
    id       INTEGER PRIMARY KEY,
    rule_id  INTEGER NOT NULL,
    t        INTEGER NOT NULL,
    message  TEXT NOT NULL,
    data     TEXT NOT NULL,            -- JSON: values and parameters at firing time
    delivery TEXT NOT NULL DEFAULT ''
);
