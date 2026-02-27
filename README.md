# Professional High-Frequency Market Making Engine

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
A production-grade, event-driven cryptocurrency market making and arbitrage engine designed for Binance Futures. Built in Python using `asyncio` and `pydantic`, this system features a completely decoupled, modular architecture with real-time quantitative signal derivation, advanced expected-value (EV) bounds mathematical modeling, and enterprise-grade telemetry powered by ClickHouse and Grafana.

## 🚀 Key Features

* **Non-Monolithic Event-Driven Architecture:** Components communicate exclusively via a high-performance in-memory Event Bus, allowing seamless scaling and decoupling of execution, strategy, and persistence layers.
* **Microstructure Predictive Signals:** A robust pipeline of statistical microstructure signals tracking real-time order book and trade flow dynamics:
  * Order Book Imbalance (OBI)
  * Short-Term Microprice Slope (Z-score normalized)
  * Trade Flow Imbalance (Exponentially decayed)
  * Volatility State Gating (Dynamically dampening quotes during extreme variances)
  * Adverse Selection Predictor (Online Logistic Model penalizing EV for toxic momentum)
* **Expected Value (EV) Edge Engine:** Dynamically calculates localized Fair Value, quoting queue horizons, fill probability estimations, and net expected edge utilizing the signal pipeline. 
* **Statistical Validation & Attribution:** Built-in `scipy/numpy` rolling validation routines that asynchronously calculate Forward Return Pearson/Spearman correlations, T-stats, rolling OLS regression signal weights, and Information Coefficients (IC).
* **Deep Telemetry & Persistence:** Asynchronous, non-blocking ClickHouse writer buffering massive streams of orderbook ticks, trade events, quantitative metrics, and execution latency benchmarks.
* **Institutional Grafana Dashboards:** Directly provisioned Grafana templates mapping deep PnL constraints, exact executed EV bounds, and predictive signal Decile Monotonicity Histograms natively through ClickHouse arrays.

## 🛠 Tech Stack
* **Core:** Requires Python 3.12.x, `asyncio`, `aiohttp`, `websockets`
* **Data Modeling:** `pydantic`
* **Quantitative Computing:** `numpy`, `scipy`
* **Storage & Analytics:** ClickHouse (`clickhouse-connect`)
* **Visualization:** Grafana

## 📁 Project Structure

```text
├── bus/                # Core Event Bus routing architecture
├── config/             # Pydantic environment configuration limits
├── execution/          # Binance Futures order executor
├── feed/               # Asynchronous WebSocket handlers for L1/AggTrade
├── metrics/            # PnL Trackers and latency bounds
├── models/             # Pydantic schemas (events, payloads, metric frames)
├── persistence/        # ClickHouse buffered writing loops
├── risk/               # Kill switch and drawdown validation gating
├── strategy/           # EV Market Making Strategy & Modular Signal Pipelines
├── clickhouse_schema.sql # DDL constraints for metric tracking tables
├── docker-compose.yml  # Local scaling ClickHouse and Grafana
└── main.py             # Event loop bootstrapping
```

## 📊 Analytics & Dashboards

This engine natively hooks into Grafana to provide statistical transparency:

1. **Realized Profitability Analytics:** 
   * Fully bounded fractional edge performance.
   * "Stat" limits generating Profit Factors, Win Rates, and Maximum Drawdowns natively mapped out from trailing USD executions using CTE queries.
   * Conditional `ASOF JOIN` displays binding Realized PnL to predictive Queue Ahead bounds, theoretical Volatility states, and raw Fair Value drift pull.

2. **Signal Validation & Attribution:** 
   * Forward return correlation mapping over 100ms/250ms/500ms/1s delays.
   * Rolling T-Stats tracing feature importance.
   * Auto-grouped Realized PnL mapped by Deciles to mathematically highlight exact scale-monotonicity behind predictive logic combinations.

## ⚙️ Getting Started

### 1. Boot up the Backend Telemetry
Launch the ClickHouse instance and Grafana provisioning server.
```bash
docker-compose up -d
```

### 2. Install Requirements
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure the Engine
Copy standard `.env` configurations natively mapped to `config/settings.py`. Includes parameters adjusting Queue Base Scalers (`mm_beta_queue_size`), Drift Gamma scales (`mm_drift_gamma`), or running the `mm_active_signal` specific backtest isolated Kill Switch.

### 4. Run the Engine
```bash
python main.py
```

## ⚠️ Disclaimer

This codebase interacts with real financial market APIs. It is primarily built to measure and research specific micro-structure events, and any active configurations scaling sizes > `0.0` should be thoroughly tested on Testnets before committing real capital.
