CREATE DATABASE IF NOT EXISTS strategy_db;

CREATE TABLE IF NOT EXISTS strategy_db.market_events (
    ts DateTime64(6, 'UTC'),
    symbol String,
    event_type String,
    bid Float64,
    ask Float64,
    price Float64,
    size Float64
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol, ts);

CREATE TABLE IF NOT EXISTS strategy_db.strategy_signals (
    ts DateTime64(6, 'UTC'),
    symbol String,
    signal_type String,
    strength Float64
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol, ts);

CREATE TABLE IF NOT EXISTS strategy_db.order_intents (
    ts DateTime64(6, 'UTC'),
    symbol String,
    side String,
    price Float64,
    size Float64,
    intent_id String
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol, ts);

CREATE TABLE IF NOT EXISTS strategy_db.fills (
    ts DateTime64(6, 'UTC'),
    symbol String,
    side String,
    price Float64,
    size Float64,
    order_id String
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol, ts);
