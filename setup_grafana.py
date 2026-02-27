import requests
import json
import time

GRAFANA_URL = "http://localhost:3000"
AUTH = ("admin", "admin")

# Check datastores
r = requests.get(f"{GRAFANA_URL}/api/datasources", auth=AUTH)
print("Datasources:", r.json())
datasources = r.json()

if not datasources:
    print("Clickhouse datasource not created. Creating...")
    ds_payload = {
        "name": "ClickHouse",
        "type": "grafana-clickhouse-datasource",
        "url": "http://clickhouse:8123",
        "access": "proxy",
        "isDefault": True,
        "jsonData": {
            "defaultDatabase": "strategy_db",
            "server": "clickhouse",
            "port": 8123,
            "username": "default",
            "tlsSkipVerify": True
        },
        "secureJsonData": {
            "password": "admin"
        }
    }
    r_ds = requests.post(f"{GRAFANA_URL}/api/datasources", json=ds_payload, auth=AUTH)
    print("Datasource Creation:", r_ds.json())

# Create dashboard payload
# Add the macro properly
dashboard = {
  "dashboard": {
    "id": None,
    "uid": "strategy_dash",
    "title": "Strategy Engine Analytics",
    "tags": [ "templated" ],
    "timezone": "browser",
    "schemaVersion": 16,
    "version": 0,
    "refresh": "5s",
    "panels": [
      {
        "type": "timeseries",
        "title": "Fair Value Matrix",
        "gridPos": { "h": 8, "w": 12, "x": 0, "y": 0 },
        "targets": [
          {
            "datasource": "ClickHouse",
            "format": "time_series",
            "query": "SELECT $__timeInterval(ts) AS time, avg(mid) AS \"Mid\", avg(microprice) AS \"Microprice\", avg(fair_value) AS \"Fair Value\" FROM strategy_metrics WHERE $__timeFilter(ts) AND symbol = '$symbol' GROUP BY time ORDER BY time"
          }
        ]
      },
      {
        "type": "timeseries",
        "title": "Expected vs Realized Edge",
        "gridPos": { "h": 8, "w": 12, "x": 12, "y": 0 },
        "targets": [
          {
            "datasource": "ClickHouse",
            "format": "time_series",
            "query": "SELECT $__timeInterval(ts) AS time, avg(expected_edge_mean) AS \"Expected Net Edge\", avg(realized_effective_edge_mean) AS \"Realized Edge\" FROM calibration_metrics WHERE $__timeFilter(ts) AND symbol = '$symbol' GROUP BY time ORDER BY time"
          }
        ]
      },
      {
        "type": "timeseries",
        "title": "Fill Probability Accuracy",
        "gridPos": { "h": 8, "w": 12, "x": 0, "y": 8 },
        "targets": [
          {
            "datasource": "ClickHouse",
            "format": "time_series",
            "query": "SELECT $__timeInterval(ts) AS time, avg(predicted_fill_prob) AS \"Predicted Prob\", avg(realized_fill_rate) AS \"Realized Rate\" FROM calibration_metrics WHERE $__timeFilter(ts) AND symbol = '$symbol' GROUP BY time ORDER BY time"
          }
        ]
      },
      {
        "type": "stat",
        "title": "EV Bias Leakage",
        "gridPos": { "h": 8, "w": 12, "x": 12, "y": 8 },
        "targets": [
          {
            "datasource": "ClickHouse",
            "format": "time_series",
            "query": "SELECT $__timeInterval(ts) AS time, avg(ev_bias) AS ev_bias FROM calibration_metrics WHERE $__timeFilter(ts) AND symbol = '$symbol' GROUP BY time ORDER BY time"
          }
        ],
        "fieldConfig": {
           "defaults": {
               "color": {"mode": "thresholds"}, 
               "thresholds": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": -0.0001}]}
           }
        }
      }
    ],
    "templating": {
      "list": [
        {
          "name": "symbol",
          "type": "query",
          "datasource": "ClickHouse",
          "query": "SELECT DISTINCT symbol FROM strategy_metrics",
          "refresh": 1
        }
      ]
    }
  },
  "overwrite": True
}

r2 = requests.post(f"{GRAFANA_URL}/api/dashboards/db", json=dashboard, auth=AUTH)
print("Dashboard Response:", r2.json())
