from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import List

class Settings(BaseSettings):
    binance_api_key: str = Field(default="")
    binance_api_secret: str = Field(default="")
    binance_testnet: bool = Field(default=True)
    
    symbols: List[str] = Field(default=["BTCUSDT", "ETHUSDT"])
    
    clickhouse_host: str = Field(default="localhost")
    clickhouse_port: int = Field(default=8123)
    clickhouse_user: str = Field(default="default")
    clickhouse_password: str = Field(default="admin")
    clickhouse_database: str = Field(default="strategy_db")
    
    # Event Bus Backpressure and Scaling
    bus_queue_size: int = Field(default=1000)
    bus_backpressure_policy: str = Field(default="drop_oldest") # drop_new, drop_oldest, block
    
    # Persistence scaling
    clickhouse_batch_size: int = Field(default=500)
    clickhouse_flush_interval: float = Field(default=1.0)
    clickhouse_max_buffer_size: int = Field(default=10000)
    clickhouse_circuit_breaker_sec: int = Field(default=30)
    
    # Risk Limits Kill Switch
    risk_global_active: bool = Field(default=True)
    risk_max_position_size: float = Field(default=1.0)
    risk_max_drawdown: float = Field(default=0.05)
    risk_max_notional_exposure: float = Field(default=100000.0)
    risk_max_open_orders: int = Field(default=10)
    risk_consecutive_errors_limit: int = Field(default=3)
    
    # Market Making Strategy Settings
    mm_base_size: float = Field(default=1.0)
    mm_base_width_k1: float = Field(default=2.0)
    mm_imbalance_k2: float = Field(default=0.5)
    mm_inventory_k3: float = Field(default=1.0)
    mm_volatility_window: int = Field(default=50)
    mm_drift_threshold: float = Field(default=0.0005)
    mm_cancel_threshold: float = Field(default=0.0001)

    # Queue & Expected Value Modeling
    mm_fee_cost: float = Field(default=0.0002) # 2 bps
    mm_min_edge_threshold: float = Field(default=0.0001)
    mm_beta_queue_size: float = Field(default=0.1)
    mm_horizon_low_vol: float = Field(default=10.0)
    mm_horizon_high_vol: float = Field(default=2.0)
    mm_horizon_trend: float = Field(default=1.0)
    mm_horizon_mean_revert: float = Field(default=5.0)
    
    # Fair Value Modeling
    mm_fv_w1: float = Field(default=1.0) # weight for microprice
    mm_fv_w2: float = Field(default=0.5) # weight for drift component
    mm_fv_w3: float = Field(default=1.0) # weight for volatility bias
    mm_drift_gamma: float = Field(default=0.01) # scale imbalance into drift
    mm_ema_alpha: float = Field(default=0.1) # hysteresis smoothing multiplier

    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8')

# Singleton instance
settings = Settings()
