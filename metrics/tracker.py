import time
import logging
from collections import defaultdict, deque
from bus.event_bus import EventBus
from models.events import OrderBookUpdate, MetricsEvent, StrategySignal, FillEvent, QuoteIntent, ParameterUpdate
import models.events
from typing import Dict, List
from config.settings import settings

logger = logging.getLogger(__name__)
# ... (rest of imports and WelfordStats)

class WelfordStats:
    def __init__(self):
        self.count = 0
        self.mean = 0.0
        self.m2 = 0.0

    def update(self, val: float):
        self.count += 1
        delta = val - self.mean
        self.mean += delta / self.count
        delta2 = val - self.mean
        self.m2 += delta * delta2

    @property
    def variance(self):
        return self.m2 / self.count if self.count > 1 else 0.0
        
    def reset(self):
        self.count = 0
        self.mean = 0.0
        self.m2 = 0.0

class MetricsTracker:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self.latencies: Dict[str, WelfordStats] = defaultdict(WelfordStats)
        
        # Calibration state tracking
        self.current_fv: Dict[str, float] = {}
        self.current_mid: Dict[str, float] = {}
        self.last_ev: Dict[str, Dict[str, float]] = defaultdict(dict)
        
        # Adaptive Coefficients Initial State
        self.adaptive_drift_gamma = settings.mm_drift_gamma
        self.adaptive_beta_queue = settings.mm_beta_queue_size
        self.adaptive_horizon_low_vol = settings.mm_horizon_low_vol
        self.adaptive_horizon_high_vol = settings.mm_horizon_high_vol
        
        # Pending fills queue to measure adverse drift after dt (e.g. 5 seconds)
        # deque payload: (ts_ms, symbol, side, price, fv_at_fill, expected_edge, prob)
        self.pending_fills: deque = deque()
        self.drift_horizon_ms = 5000  # 5 seconds to realize drift

        # Rolling Stats for 5m windows
        self.stats = defaultdict(WelfordStats)
        self.fill_count = 0
        self.quote_intent_count = 0
        self.last_reset_ts = time.time()
        self.window_sec = 5 # 5 seconds

        self.bus.subscribe(OrderBookUpdate, self.on_market_data)
        self.bus.subscribe(StrategySignal, self.on_strategy_signal)
        self.bus.subscribe(FillEvent, self.on_fill)
        self.bus.subscribe(MetricsEvent, self.on_metrics_event)
        self.bus.subscribe(QuoteIntent, self.on_quote_intent)
        
        logger.info("MetricsTracker initialized with Welford O(1) rolling estimators and calibration.")

    async def on_market_data(self, event: OrderBookUpdate):
        now_mono = time.monotonic() * 1000
        latency = now_mono - event.recv_monotonic
        if latency > 0:
            self.latencies['market_data_processing'].update(latency)

        mid = (event.bid + event.ask) / 2.0
        self.current_mid[event.symbol] = mid

        # Evaluate pending fills for adverse drift
        now = time.time() * 1000
        while self.pending_fills and (now - self.pending_fills[0][0]) >= self.drift_horizon_ms:
            fill = self.pending_fills.popleft()
            fill_symbol = fill[1]
            drift_mid = self.current_mid.get(fill_symbol, mid)
            await self._realize_fill_pnl(fill, drift_mid)

        await self._check_rolling_window()

    async def _realize_fill_pnl(self, fill_data: tuple, current_mid: float):
        ts, symbol, side, fill_price, fill_size, fv_at_fill, expected_edge, prob_at_fill, sig_composite, sig_imb, sig_slope, sig_tfi = fill_data
        
        if fv_at_fill == 0.0:
            fv_at_fill = current_mid
            
        fee_cost = settings.mm_fee_cost
        fee_paid = fill_size * fill_price * fee_cost
        
        # Realized metrics
        if side == "BUY":
            gross_edge = (fv_at_fill - fill_price) / fv_at_fill
            adverse_drift = (fv_at_fill - current_mid) / fv_at_fill
            realized_pnl_usd = fill_size * (fv_at_fill - fill_price) - fee_paid
        else:
            gross_edge = (fill_price - fv_at_fill) / fv_at_fill
            adverse_drift = (current_mid - fv_at_fill) / fv_at_fill
            realized_pnl_usd = fill_size * (fill_price - fv_at_fill) - fee_paid
            
        realized_edge = gross_edge - adverse_drift - fee_cost
        
        await self.bus.publish(models.events.FillMetricsPayload(
            symbol=symbol,
            side=side,
            fill_price=fill_price,
            fill_size=fill_size,
            fair_value_at_fill=fv_at_fill,
            fee_paid=fee_paid,
            realized_edge_fractional=gross_edge,
            realized_pnl_usd=realized_pnl_usd,
            sig_composite=sig_composite,
            sig_imb=sig_imb,
            sig_slope=sig_slope,
            sig_tfi=sig_tfi
        ))
        
        self.stats['realized_gross_edge'].update(gross_edge)
        self.stats['realized_adverse_drift'].update(adverse_drift)
        self.stats['effective_edge_after_fees'].update(realized_edge)
        self.stats['realized_fill_pnl'].update(realized_edge)
        
        # Calibration diagnostic arrays directly bound to fills
        if expected_edge is not None:
             ev_bias = realized_edge - expected_edge
             self.stats['EV_bias'].update(ev_bias)

    async def on_quote_intent(self, intent: QuoteIntent):
        self.quote_intent_count += 1
        
        # Prob bounds mapping directly off intents stream
        bid_prob = self.last_ev[intent.symbol].get('ev_bid_fill_prob', 0.1)
        ask_prob = self.last_ev[intent.symbol].get('ev_ask_fill_prob', 0.1)
        
        if intent.bid_size > 0:
            self.stats['fill_probability_estimate'].update(bid_prob)
        if intent.ask_size > 0:
            self.stats['fill_probability_estimate'].update(ask_prob)

    async def on_fill(self, fill: FillEvent):
        latency = fill.recv_monotonic - fill.intent_monotonic
        if latency > 0:
            self.latencies['execution_fill_ms'].update(latency)

        self.fill_count += 1
        fv_at_fill = self.current_fv.get(fill.symbol, self.current_mid.get(fill.symbol, fill.price))
        
        ev_at_fill = self.last_ev[fill.symbol].get('ev_bid_expected_edge') if fill.side == "BUY" else self.last_ev[fill.symbol].get('ev_ask_expected_edge')
        prob = self.last_ev[fill.symbol].get('ev_bid_fill_prob') if fill.side == "BUY" else self.last_ev[fill.symbol].get('ev_ask_fill_prob')
        
        sig_composite = self.last_ev[fill.symbol].get('sig_composite', 0.0)
        sig_imb = self.last_ev[fill.symbol].get('sig_imb', 0.0)
        sig_slope = self.last_ev[fill.symbol].get('sig_slope', 0.0)
        sig_tfi = self.last_ev[fill.symbol].get('sig_tfi', 0.0)
        
        # Append tuple correctly isolated: (ts, smb, side, px, sz, fv, ev, prb, sigs...)
        self.pending_fills.append((
            time.time() * 1000, fill.symbol, fill.side, fill.price, fill.size, fv_at_fill, ev_at_fill, prob,
            sig_composite, sig_imb, sig_slope, sig_tfi
        ))

    async def on_strategy_signal(self, signal: StrategySignal):
        latency = signal.recv_monotonic - signal.feed_monotonic
        if latency > 0:
            self.latencies['feed_to_strategy_ms'].update(latency)

    async def on_metrics_event(self, event: MetricsEvent):
        symbol = event.labels.get('symbol')
        if not symbol:
             return
             
        # Listen natively to FV and EV mapping channels from MM.py
        if event.metric_name == 'fv_fair_value':
            self.current_fv[symbol] = event.value
        elif 'expected_edge' in event.metric_name or 'fill_prob' in event.metric_name or event.metric_name in ['sig_composite', 'sig_imb', 'sig_slope', 'sig_tfi', 'sig_bid_adv_pen', 'sig_ask_adv_pen']:
            self.last_ev[symbol][event.metric_name] = event.value
            
        if 'expected_edge' in event.metric_name:
            self.stats['expected_edge_before_filter'].update(event.value)

    async def _check_rolling_window(self):
        if time.time() - self.last_reset_ts >= self.window_sec:
            self._print_calibration_report()
            
            # 1. Output Bounded DB Strategy Payload correctly matching Schema requirements for Grafana
            # Loop current symbol map
            for symbol, fv in self.current_fv.items():
                ev_bid = self.last_ev[symbol].get('ev_bid_expected_edge', 0.0)
                ev_ask = self.last_ev[symbol].get('ev_ask_expected_edge', 0.0)
                mid = self.current_mid.get(symbol, fv)
                
                await self.bus.publish(models.events.StrategyMetricsPayload(
                    symbol=symbol,
                    mid=mid,
                    microprice=fv,  # Assuming convergence for simplified table array rendering
                    fair_value=fv,
                    expected_edge=max(ev_bid, ev_ask), 
                    effective_edge_after_fees=self.stats['effective_edge_after_fees'].mean,
                    inventory_ratio=0.0, # Filled upstream natively or assumed mean-revert
                    queue_ahead_estimate=self.last_ev[symbol].get('ev_queue_ahead_bid', 0.0), # Assuming tracking added
                    fill_prob=self.last_ev[symbol].get('ev_bid_fill_prob', 0.0),
                    short_vol=0.0,
                    regime="dynamic_calibration"
                ))

            # 2. Output Bounded Calibration Payload
            pred_prob = self.stats['fill_probability_estimate'].mean if self.stats['fill_probability_estimate'].count > 0 else 0.0
            
            opportunities = self.quote_intent_count
            fills = self.fill_count
            realized_fill_rate = fills / max(1, opportunities)
            
            for symbol in self.current_fv.keys():
                 await self.bus.publish(models.events.CalibrationMetricsPayload(
                     symbol=symbol,
                     predicted_fill_prob=pred_prob,
                     realized_fill_rate=realized_fill_rate,
                     expected_edge_mean=self.stats['expected_edge_before_filter'].mean,
                     realized_effective_edge_mean=self.stats['effective_edge_after_fees'].mean,
                     ev_bias=self.stats['EV_bias'].mean,
                     adverse_drift_mean=self.stats['realized_adverse_drift'].mean,
                     realized_fill_pnl_mean=self.stats['realized_fill_pnl'].mean
                 ))
                 
            await self._run_adaptive_controller()
            self._reset_stats()
            self.last_reset_ts = time.time()

    async def _run_adaptive_controller(self):
        """
        Lightweight O(1) EMA-based Regime Controller scaling 
        engine execution constraints dynamically across tumble checks
        """
        opportunities = self.quote_intent_count
        fills = self.fill_count
        fill_rate = fills / max(1, opportunities)
        pred_fill_prob = self.stats['fill_probability_estimate'].mean if self.stats['fill_probability_estimate'].count > 0 else 0.0
        
        fill_prob_error = fill_rate - pred_fill_prob
        ev_bias = self.stats['EV_bias'].mean
        realized_drift = self.stats['realized_adverse_drift'].mean
        
        # Heuristic Bounds (Preventing aggressive runaway)
        # 1. EV Bias Controller (Shift Microstructure Impact)
        if ev_bias < -0.0001:  # Material negative expectation leak (toxic structure assumed)
            self.adaptive_drift_gamma = min(0.1, self.adaptive_drift_gamma + 0.005) # Step up drift sensitivity
            logger.info(f"Adaptive Shift: EV Bias deeply negative ({ev_bias:.6f}). Stepping Drift Gamma UP to {self.adaptive_drift_gamma:.4f}")
        elif ev_bias > 0.0001 and realized_drift < 0.00005: 
            self.adaptive_drift_gamma = max(0.001, self.adaptive_drift_gamma - 0.005) # Ease off to capture more aggressive spreads
            logger.info(f"Adaptive Shift: EV Bias heavily positive. Stepping Drift Gamma DOWN to {self.adaptive_drift_gamma:.4f}")

        # 2. Fill Rate & Queue Dampener Controller
        if fill_prob_error > 0.15: # Filling way more than expected probability bound 
             # Implies queue estimates are under-penalizing bad depths
             self.adaptive_beta_queue = min(1.0, self.adaptive_beta_queue * 1.1)
             logger.info(f"Adaptive Shift: Fill Rate wildly exceeding bounds (+{fill_prob_error:.2f}). Increasing Queue Penalty Beta to {self.adaptive_beta_queue:.4f}")
        elif fill_prob_error < -0.15: # Not filling anywhere near expected boundary
             # Horizon estimators are falsely optimistic, drop dampener 
             self.adaptive_beta_queue = max(0.01, self.adaptive_beta_queue * 0.9)
             logger.info(f"Adaptive Shift: Fill Rate falling short ({fill_prob_error:.2f}). Dropping Queue Penalty Beta to {self.adaptive_beta_queue:.4f}")

        # 3. Horizon Time Decay 
        if realized_drift > 0.0005: # High toxicity regime globally
             # Shorten allowed prediction horizons dynamically
             self.adaptive_horizon_low_vol = max(2.0, self.adaptive_horizon_low_vol * 0.9)
             self.adaptive_horizon_high_vol = max(0.5, self.adaptive_horizon_high_vol * 0.9)
             logger.info(f"Adaptive Shift: High Adverse Drift detected. Shortening Vol Horizons: (Low:{self.adaptive_horizon_low_vol:.2f}s, High:{self.adaptive_horizon_high_vol:.2f}s)")
        else:
             # Relax
             self.adaptive_horizon_low_vol = min(20.0, self.adaptive_horizon_low_vol * 1.05)
             self.adaptive_horizon_high_vol = min(10.0, self.adaptive_horizon_high_vol * 1.05)

        # Broadcast the parameter shifts completely isolated back to MM engine structurally
        await self.bus.publish(ParameterUpdate(
            new_drift_gamma=self.adaptive_drift_gamma,
            new_beta_queue=self.adaptive_beta_queue,
            new_horizon_low_vol=self.adaptive_horizon_low_vol,
            new_horizon_high_vol=self.adaptive_horizon_high_vol
        ))

    def _print_calibration_report(self):
        logger.info(f"--- 5m Rolling Calibration Diagnostics ---")
        
        # 1. Fill Rate Errors
        predicted_fill_prob = self.stats['fill_probability_estimate'].mean if self.stats['fill_probability_estimate'].count > 0 else 0.0
        # Simple heuristic mapping the flow fraction for realized rate bounds
        opportunities = self.quote_intent_count
        fills = self.fill_count
        realized_fill_rate = fills / max(1, opportunities) 
        fill_prob_error = realized_fill_rate - predicted_fill_prob
        
        logger.info(f"Fill Prob Error = Predicted: {predicted_fill_prob:.4f} vs Realized Rate: {realized_fill_rate:.4f} -> Error: {fill_prob_error:.4f}")
        
        # 2. EV Distro Comparisons
        mean_exp = self.stats['expected_edge_before_filter'].mean
        mean_realized = self.stats['effective_edge_after_fees'].mean
        ev_bias = self.stats['EV_bias'].mean
        
        logger.info(f"Expected Edge (pre-filter): mean={mean_exp:.6f} | Realized Effective Edge: mean={mean_realized:.6f}")
        logger.info(f"EV_bias (Realized - Expected Error Boundary): mean={ev_bias:.6f}, var={self.stats['EV_bias'].variance:.8f}")
        
        # 3. Microstructure Cost Tracking
        mean_drift = self.stats['realized_adverse_drift'].mean
        var_drift = self.stats['realized_adverse_drift'].variance
        logger.info(f"Conditional Adverse Drift: mean={mean_drift:.6f}, variance={var_drift:.8f}")
        
        mean_pnl = self.stats['realized_fill_pnl'].mean
        logger.info(f"Realized Fill PnL (net edge map): mean={mean_pnl:.6f}")
        logger.info("------------------------------------------")

    def _reset_stats(self):
        for k in self.stats:
            self.stats[k].reset()
        for k in self.latencies:
            self.latencies[k].reset()
        self.fill_count = 0
        self.quote_intent_count = 0
