# AGENTS.md

## Cursor Cloud specific instructions

### Overview
This is a Python 3.12 high-frequency market making engine for Binance Futures. It uses asyncio, connects to Binance WebSocket for live market data, computes microstructure signals, and persists to ClickHouse. Grafana provides dashboards.

### Services
| Service | Port | Purpose |
|---|---|---|
| Python Engine (`python main.py`) | N/A | Core trading engine |
| ClickHouse | 8123 (HTTP), 9000 (native) | Time-series storage |
| Grafana | 3000 | Dashboards (optional) |

### Running services
- Start Docker services: `docker compose up -d` (from repo root)
- The `grafana_data/` directory may need `chmod -R 777` on first run if Grafana fails to start with permission errors.
- Apply the full ClickHouse schema before running the engine: `clickhouse_schema.sql` contains tables (`market_events`, `strategy_signals`, `order_intents`, `fills`) not included in `clickhouse_init/clickhouse_ddl.sql`. Additional tables needed by the persistence layer (`fills_metrics`, `signal_forward_stats`, `signal_regression_stats`, `signal_ic_stats`) must also be created. Use multi-statement execution against the ClickHouse HTTP API or apply each statement individually.
- Activate the venv (`source venv/bin/activate`) and run `python main.py` to start the engine.
- The engine runs in **dummy execution mode** when `binance_api_key`/`binance_api_secret` are empty (the default). It still receives live public market data via WebSocket and processes signals/metrics.

### Lint / Test / Build
- No formal test suite or linting config exists in this repo. Use `pyflakes` for basic lint checking.
- No build step — the application runs directly via `python main.py`.
- Configuration is via `.env` file (mapped to `config/settings.py` via `pydantic-settings`).

### Gotchas
- The `clickhouse_init/clickhouse_ddl.sql` only creates `strategy_metrics` and `calibration_metrics` tables. The engine's `ClickHouseWriter` also writes to `market_events`, `strategy_signals`, `order_intents`, `fills`, `fills_metrics`, `signal_forward_stats`, `signal_regression_stats`, and `signal_ic_stats`. These must be created separately using `clickhouse_schema.sql` plus additional DDL.
- Grafana default login: admin/admin.
- Docker Compose file has an obsolete `version` attribute that produces a warning; it's harmless.
