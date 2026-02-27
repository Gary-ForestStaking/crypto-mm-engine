from pydantic import BaseModel, ConfigDict, Field
import time

def current_milli_time() -> int:
    return int(time.time() * 1000)

def current_monotonic_time() -> int:
    return int(time.monotonic() * 1000)

class BaseEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

class MarketEvent(BaseEvent):
    symbol: str
    event_ts: int
    recv_ts: int = Field(default_factory=current_milli_time)
    recv_monotonic: int = Field(default_factory=current_monotonic_time)

class OrderBookUpdate(MarketEvent):
    bid: float
    ask: float
    bid_size: float
    ask_size: float

class TradeEvent(MarketEvent):
    price: float
    size: float
    is_buyer_maker: bool

class TickerEvent(MarketEvent):
    price: float

class StrategySignal(BaseEvent):
    symbol: str
    event_ts: int = Field(default_factory=current_milli_time)
    recv_ts: int = Field(default_factory=current_milli_time)
    recv_monotonic: int = Field(default_factory=current_monotonic_time)
    feed_monotonic: int = 0
    signal_type: str
    strength: float

class QuoteIntent(BaseEvent):
    symbol: str
    event_ts: int = Field(default_factory=current_milli_time)
    recv_ts: int = Field(default_factory=current_milli_time)
    recv_monotonic: int = Field(default_factory=current_monotonic_time)
    feed_monotonic: int = 0
    bid_price: float
    bid_size: float
    ask_price: float
    ask_size: float
    confidence: float
    regime: str
    skew_applied: float

class OrderIntent(BaseEvent):
    symbol: str
    event_ts: int = Field(default_factory=current_milli_time)
    recv_ts: int = Field(default_factory=current_milli_time)
    recv_monotonic: int = Field(default_factory=current_monotonic_time)
    strategy_monotonic: int = 0
    side: str
    price: float
    size: float
    intent_id: str

class FillEvent(BaseEvent):
    symbol: str
    event_ts: int = Field(default_factory=current_milli_time)
    recv_ts: int = Field(default_factory=current_milli_time)
    recv_monotonic: int = Field(default_factory=current_monotonic_time)
    intent_id: str
    intent_monotonic: int = 0
    side: str
    price: float
    size: float
    order_id: str

class MetricsEvent(BaseEvent):
    event_ts: int = Field(default_factory=current_milli_time)
    recv_ts: int = Field(default_factory=current_milli_time)
    recv_monotonic: int = Field(default_factory=current_monotonic_time)
    metric_name: str
    value: float
    labels: dict[str, str] = Field(default_factory=dict)

class ParameterUpdate(BaseEvent):
    event_ts: int = Field(default_factory=current_milli_time)
    recv_ts: int = Field(default_factory=current_milli_time)
    recv_monotonic: int = Field(default_factory=current_monotonic_time)
    new_drift_gamma: float
    new_beta_queue: float
    new_horizon_low_vol: float
    new_horizon_high_vol: float

class StrategyMetricsPayload(BaseEvent):
    event_ts: int = Field(default_factory=current_milli_time)
    symbol: str
    mid: float
    microprice: float
    fair_value: float
    expected_edge: float
    effective_edge_after_fees: float
    inventory_ratio: float
    queue_ahead_estimate: float
    fill_prob: float
    short_vol: float
    regime: str
    
class CalibrationMetricsPayload(BaseEvent):
    event_ts: int = Field(default_factory=current_milli_time)
    symbol: str
    predicted_fill_prob: float
    realized_fill_rate: float
    expected_edge_mean: float
    realized_effective_edge_mean: float
    ev_bias: float
    adverse_drift_mean: float
    realized_fill_pnl_mean: float

class FillMetricsPayload(BaseEvent):
    event_ts: int = Field(default_factory=current_milli_time)
    symbol: str
    side: str
    fill_price: float
    fill_size: float
    fair_value_at_fill: float
    fee_paid: float
    realized_edge_fractional: float
    realized_pnl_usd: float
    sig_composite: float
    sig_imb: float
    sig_slope: float
    sig_tfi: float

class SignalForwardStatsPayload(BaseEvent):
    event_ts: int = Field(default_factory=current_milli_time)
    symbol: str
    signal_name: str
    horizon_ms: int
    correlation: float
    mean_forward_return: float
    t_stat: float
    sample_size: int

class SignalRegressionStatsPayload(BaseEvent):
    event_ts: int = Field(default_factory=current_milli_time)
    symbol: str
    horizon_ms: int
    sig_imb_coef: float
    sig_imb_se: float
    sig_imb_t: float
    sig_slope_coef: float
    sig_slope_se: float
    sig_slope_t: float
    sig_tfi_coef: float
    sig_tfi_se: float
    sig_tfi_t: float
    sig_bid_adv_coef: float
    sig_bid_adv_se: float
    sig_bid_adv_t: float
    sig_ask_adv_coef: float
    sig_ask_adv_se: float
    sig_ask_adv_t: float

class SignalICStatsPayload(BaseEvent):
    event_ts: int = Field(default_factory=current_milli_time)
    symbol: str
    signal_name: str
    horizon_ms: int
    rolling_ic: float
    rolling_mean_ic: float
