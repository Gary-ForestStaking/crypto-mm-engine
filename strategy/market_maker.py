import logging
import math
import collections
from typing import Dict, Deque
from models.events import OrderBookUpdate, TradeEvent, QuoteIntent, MetricsEvent, FillEvent
from bus.event_bus import EventBus
from config.settings import settings

logger = logging.getLogger(__name__)

class MarketMakingStrategy:
    """
    Professional-grade microstructure-aware Expected Value Market Making Strategy.
    Computes fair value estimators, conditional drift models, and EV bounds 
    scaling natively to queue position mechanics and inventory dampening.
    """
    def __init__(self, bus: EventBus):
        self.bus = bus
        self.symbols = settings.symbols
        
        # Strategy Parameters
        self.base_size = settings.mm_base_size
        self.k1 = settings.mm_base_width_k1
        self.k2 = settings.mm_imbalance_k2
        self.k3 = settings.mm_inventory_k3
        self.max_position = settings.risk_max_position_size
        self.vol_window_size = settings.mm_volatility_window
        self.drift_threshold = settings.mm_drift_threshold
        self.cancel_threshold = settings.mm_cancel_threshold

        # Queue & EV Modeling Parameters
        self.fee_cost = settings.mm_fee_cost
        self.min_edge_threshold = settings.mm_min_edge_threshold
        self.beta_queue = settings.mm_beta_queue_size
        self.horizon_low_vol = settings.mm_horizon_low_vol
        self.horizon_high_vol = settings.mm_horizon_high_vol
        self.horizon_trend = settings.mm_horizon_trend
        self.horizon_mean_revert = settings.mm_horizon_mean_revert
        
        # Fair Value Modeling
        self.fv_w1 = settings.mm_fv_w1
        self.fv_w2 = settings.mm_fv_w2
        self.fv_w3 = settings.mm_fv_w3
        self.drift_gamma = settings.mm_drift_gamma
        self.ema_alpha = settings.mm_ema_alpha
        
        # Internal State tracking (isolated per symbol)
        self.positions: Dict[str, float] = {s: 0.0 for s in self.symbols}
        self.last_mid: Dict[str, float] = {}
        
        # Smoothers and Trackers
        self.ema_microprice: Dict[str, float] = {}
        self.ema_drift: Dict[str, float] = {s: 0.0 for s in self.symbols}
        self.ema_fair_value: Dict[str, float] = {}
        
        self.mid_history: Dict[str, Deque[float]] = {s: collections.deque(maxlen=self.vol_window_size) for s in self.symbols}
        self.aggressor_flow: Dict[str, Deque[float]] = {s: collections.deque(maxlen=100) for s in self.symbols}
        self.trade_rates: Dict[str, float] = {s: 0.001 for s in self.symbols} # Trades per second EWMA
        self.last_trade_ts: Dict[str, int] = {}
        
        # EV Drift Conditional
        self.post_hit_drift: Dict[str, collections.deque] = {s: collections.deque(maxlen=50) for s in self.symbols}
        
        # Active Logic
        self.active_quotes: Dict[str, dict] = {}

    def register(self):
        self.bus.subscribe(OrderBookUpdate, self.on_order_book_update)
        self.bus.subscribe(TradeEvent, self.on_trade)
        self.bus.subscribe(FillEvent, self.on_fill)
        from models.events import ParameterUpdate
        self.bus.subscribe(ParameterUpdate, self.on_parameter_update)
        logger.info("MarketMakingStrategy registered handlers")

    async def on_parameter_update(self, event):
        """ Dynamically scales engine properties bounded explicitly by the calibration feedback loops."""
        self.drift_gamma = event.new_drift_gamma
        self.beta_queue = event.new_beta_queue
        self.horizon_low_vol = event.new_horizon_low_vol
        self.horizon_high_vol = event.new_horizon_high_vol
        logger.info(f"Market Maker adjusted parameters internally: Gamma={self.drift_gamma:.4f}, BetaQueue={self.beta_queue:.4f}")

    async def on_fill(self, event: FillEvent):
        """Internal lightweight local position sync"""
        current = self.positions.get(event.symbol, 0.0)
        self.positions[event.symbol] = current + event.size if event.side == "BUY" else current - event.size

    async def on_trade(self, event: TradeEvent):
        """
        Track aggressor flows specifically for trade_rate_level updates
        and post-fill toxicity mappings.
        """
        # Updates global trade rate
        last_t = self.last_trade_ts.get(event.symbol, event.event_ts)
        self.last_trade_ts[event.symbol] = event.event_ts
        dt_sec = (event.event_ts - last_t) / 1000.0 if event.event_ts > last_t else 1.0
        
        # Simple EWMA rate: lambda = trade_qty / dt
        if dt_sec > 0:
             rate = event.size / dt_sec
             self.trade_rates[event.symbol] = (self.trade_rates[event.symbol] * 0.9) + (rate * 0.1)

        flow = -event.size if event.is_buyer_maker else event.size
        self.aggressor_flow[event.symbol].append(flow)
        
        # EV Drift Calculation (Simple assumption: Evaluate mid against previous mid upon aggressive print)
        hist = list(self.mid_history.get(event.symbol, []))
        if len(hist) > 0:
            drift = (event.price - hist[0]) / hist[0]
            if event.is_buyer_maker: # Aggressive Sell hitting our Bid
                self.post_hit_drift[event.symbol].append(-drift) # Positive value implies it went down (against us)
            else: # Aggressive Buy lifting our Ask
                self.post_hit_drift[event.symbol].append(drift) # Positive implies it went up (against us)

    def _get_expected_adverse_drift(self, symbol: str) -> float:
        drifts = list(self.post_hit_drift[symbol])
        if not drifts:
            return 0.00005 # Baseline min drift assumption
        return sum(drifts) / len(drifts)

    def _calc_volatility(self, symbol: str) -> float:
        hist = list(self.mid_history[symbol])
        if len(hist) < 2:
            return 0.0001
        returns = [(hist[i] - hist[i-1])/hist[i-1] for i in range(1, len(hist))]
        mean_ret = sum(returns) / len(returns)
        var = sum((r - mean_ret)**2 for r in returns) / len(returns)
        return math.sqrt(var)

    def _get_horizon_for_regime(self, short_vol: float) -> float:
        # Simplistic regime gating
        if short_vol < 0.001:
            return self.horizon_low_vol
        else:
            return self.horizon_high_vol
            
    def _compute_fair_value(self, symbol: str, bid: float, ask: float, bid_size: float, ask_size: float, imb: float, vol: float) -> tuple[float, float, float]:
        """
        Microstructure Fair Value Estimate replacing static Mid.
        fair_value = w1*micro + w2*drift_bias + w3*(bias_volatility_adj)
        """
        tot_size = bid_size + ask_size
        if tot_size == 0:
             # Fallback
             return (bid + ask) / 2.0, (bid + ask) / 2.0, 0.0
             
        # 1. Microprice
        raw_micro = (ask * bid_size + bid * ask_size) / tot_size
        
        # EWMA Smoothing for Microprice
        last_micro = self.ema_microprice.get(symbol, raw_micro)
        micro = (self.ema_alpha * raw_micro) + ((1 - self.ema_alpha) * last_micro)
        self.ema_microprice[symbol] = micro
        
        # 2. Imbalance Drift Component (EMA smoothed)
        raw_drift = self.drift_gamma * imb
        self.ema_drift[symbol] = (0.05 * raw_drift) + (0.95 * self.ema_drift[symbol])
        drift_comp = self.ema_drift[symbol]
        
        # 3. Volatility Shift Adjustment
        # In high volatilty, we rely more on mid pulling back. In low, micro takes precedence.
        vol_bias = -drift_comp if vol > 0.002 else drift_comp 
        
        # 4. Final Fair Value compilation
        raw_fv = (self.fv_w1 * micro) + (self.fv_w2 * drift_comp) + (self.fv_w3 * vol_bias)
        
        # Hysteresis Check on final array to avoid jitter
        last_fv = self.ema_fair_value.get(symbol, raw_fv)
        # Use Epsilon gate (0.5 bps)
        if abs(raw_fv - last_fv) / last_fv > 0.00005: 
             fv = (self.ema_alpha * raw_fv) + ((1 - self.ema_alpha) * last_fv)
             self.ema_fair_value[symbol] = fv
        else:
             fv = last_fv

        return fv, micro, raw_drift

    async def on_order_book_update(self, event: OrderBookUpdate):
        if event.bid == 0.0 or event.ask == 0.0:
            return
            
        mid = (event.bid + event.ask) / 2.0
        self.mid_history[event.symbol].append(mid)
        self.last_mid[event.symbol] = mid
        
        # Core Book State
        imb = (event.bid_size - event.ask_size) / (event.bid_size + event.ask_size)
        vol = self._calc_volatility(event.symbol)
        inv_ratio = self.positions.get(event.symbol, 0.0) / self.max_position
        
        # 1. Microstructure Fair Value calculation
        fv_res = self._compute_fair_value(event.symbol, event.bid, event.ask, event.bid_size, event.ask_size, imb, vol)
        if isinstance(fv_res, tuple):
             fv, micro, drift_comp = fv_res
        else:
             fv, micro, drift_comp = mid, mid, 0.0
             
        # 2. Extract specific components for the Model
        trade_rate = self.trade_rates.get(event.symbol, 0.001)
        adv_drift = self._get_expected_adverse_drift(event.symbol)
        horizon = self._get_horizon_for_regime(vol)

        # 3. Dynamic Quote Pricing & Queue Sizing Estimation
        base_width = self.k1 * vol
        bid_price = fv * (1 - base_width)
        ask_price = fv * (1 + base_width)
        
        # Queue estimating (assume we are posting behind the entire L1 if active)
        bid_queue_ahead = event.bid_size if bid_price >= event.bid else event.bid_size * 2
        ask_queue_ahead = event.ask_size if ask_price <= event.ask else event.ask_size * 2
        
        # Probability Modeling & EVs
        if bid_queue_ahead > 0:
            bid_prob = 1 - math.exp(-trade_rate * horizon / bid_queue_ahead)
        else:
            bid_prob = 1.0
            
        if ask_queue_ahead > 0:
            ask_prob = 1 - math.exp(-trade_rate * horizon / ask_queue_ahead)
        else:
            ask_prob = 1.0
            
        # 4. Expected Edge Engine (Gross Edge vs Fair Value, not Mid)
        bid_gross = (fv - bid_price) / fv
        ask_gross = (ask_price - fv) / fv
        
        bid_ev = bid_prob * (bid_gross - adv_drift - self.fee_cost)
        ask_ev = ask_prob * (ask_gross - adv_drift - self.fee_cost)

        # Shift thresholds based on inventory limits mechanically
        adj_bid_threshold = self.min_edge_threshold * (1 + max(0, inv_ratio))
        adj_ask_threshold = self.min_edge_threshold * (1 + max(0, -inv_ratio))

        # Size scaling utilizing Beta queue-aware dampeners + inventory skew
        bid_sz_mult = 1.0 / (1.0 + self.beta_queue * bid_queue_ahead)
        ask_sz_mult = 1.0 / (1.0 + self.beta_queue * ask_queue_ahead)
        
        bid_size = self.base_size * bid_sz_mult * max(0.01, (1 - inv_ratio))
        ask_size = self.base_size * ask_sz_mult * max(0.01, (1 + inv_ratio))
        
        # Strict EV Gatekeeping 
        if bid_ev < adj_bid_threshold:
             bid_size = 0.0
        if ask_ev < adj_ask_threshold:
             ask_size = 0.0

        # Replace active reference array
        self.active_quotes[event.symbol] = {
             "ev_bid": bid_ev,
             "ev_ask": ask_ev,
             "bid": bid_price,
             "ask": ask_price
        }

        # 5. Publish QuoteIntent only if sizes > 0 on either leg
        if bid_size > 0 or ask_size > 0:
            intent = QuoteIntent(
                symbol=event.symbol,
                event_ts=event.event_ts,
                feed_monotonic=event.recv_monotonic,
                bid_price=bid_price,
                bid_size=bid_size,
                ask_price=ask_price,
                ask_size=ask_size,
                confidence=1.0,
                regime="dynamic_ev",
                skew_applied=inv_ratio
            )
            await self.bus.publish(intent)
        
        # 6. Metrics Event loop
        metrics = [
            ("fv_mid", mid),
            ("fv_microprice", micro),
            ("fv_fair_value", fv),
            ("fv_micro_minus_mid", micro - mid),
            ("fv_imbalance", imb),
            ("fv_fair_value_shift", fv - mid),
            ("ev_queue_ahead_bid", bid_queue_ahead),
            ("ev_trade_rate", trade_rate),
            ("ev_expected_adverse_drift", adv_drift),
            ("ev_bid_expected_edge", bid_ev),
            ("ev_ask_expected_edge", ask_ev),
            ("ev_bid_fill_prob", bid_prob),
            ("ev_ask_fill_prob", ask_prob),
        ]
        
        for name, val in metrics:
             await self.bus.publish(MetricsEvent(
                 event_ts=event.event_ts, recv_monotonic=event.recv_monotonic, metric_name=name, value=val, labels={"symbol": event.symbol}
             ))
