-- 1. Strategy Metrics Table
-- Tracks standard strategy outputs from MarketMaker
CREATE DATABASE IF NOT EXISTS strategy_db;

CREATE TABLE IF NOT EXISTS strategy_db.strategy_metrics (
    ts DateTime64(3),
    symbol LowCardinality(String),
    mid Float64,
    microprice Float64,
    fair_value Float64,
    expected_edge Float64,
    effective_edge_after_fees Float64,
    inventory_ratio Float64,
    queue_ahead_estimate Float64,
    fill_prob Float64,
    short_vol Float64,
    regime LowCardinality(String)
) ENGINE = MergeTree
PARTITION BY toDate(ts)
ORDER BY (symbol, ts);

-- 2. Calibration Metrics Table
-- Explicitly tracking bounded Tracker module EMA/Variance states
CREATE TABLE IF NOT EXISTS strategy_db.calibration_metrics (
    ts DateTime64(3),
    symbol LowCardinality(String),
    predicted_fill_prob Float64,
    realized_fill_rate Float64,
    expected_edge_mean Float64,
    realized_effective_edge_mean Float64,
    ev_bias Float64,
    adverse_drift_mean Float64,
    realized_fill_pnl_mean Float64
) ENGINE = MergeTree
PARTITION BY toDate(ts)
ORDER BY (symbol, ts);
