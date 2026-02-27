import collections
import math
import time
import time
from typing import Dict, List, Tuple
from config.settings import settings
from models.events import OrderBookUpdate, TradeEvent

class Signal:
    def update_book(self, event: OrderBookUpdate):
        pass
    def update_trade(self, event: TradeEvent):
        pass
    def get_signal(self, symbol: str) -> float:
        return 0.0

class OrderBookImbalanceSignal(Signal):
    def __init__(self, depth: int = 5):
        self.depth = depth
        self.imbalances = collections.defaultdict(float)
        
    def update_book(self, event: OrderBookUpdate):
        # Using L1 depth as defined by the feed, handles scaling natively
        tot = event.bid_size + event.ask_size
        if tot > 0:
            imb = (event.bid_size - event.ask_size) / tot
            self.imbalances[event.symbol] = imb
            
    def get_signal(self, symbol: str) -> float:
        # Output strictly normalized [-1, 1]
        return self.imbalances.get(symbol, 0.0)

class MicropriceSlopeSignal(Signal):
    def __init__(self, window_ms: int = 1000):
        self.window_ms = window_ms
        self.history = collections.defaultdict(collections.deque)
        self.means = collections.defaultdict(float)
        self.vars = collections.defaultdict(float)
        self.counts = collections.defaultdict(int)
        
    def update_book(self, event: OrderBookUpdate):
        tot = event.bid_size + event.ask_size
        if tot > 0:
            micro = (event.ask * event.bid_size + event.bid * event.ask_size) / tot
            t = event.event_ts
            sym = event.symbol
            
            dq = self.history[sym]
            dq.append((t, micro))
            while dq and t - dq[0][0] > self.window_ms:
                dq.popleft()
                
    def get_signal(self, symbol: str) -> float:
        dq = self.history.get(symbol)
        if not dq or len(dq) < 2:
            return 0.0
            
        t0 = dq[0][0]
        dt = (dq[-1][0] - t0) / 1000.0
        if dt <= 0: return 0.0
        
        slope = (dq[-1][1] - dq[0][1]) / dt
        
        # Online Z-score normalization mapping back to bounded [-1, 1] 
        old_mean = self.means[symbol]
        self.counts[symbol] += 1
        n = self.counts[symbol]
        new_mean = old_mean + (slope - old_mean) / n
        self.means[symbol] = new_mean
        self.vars[symbol] += (slope - old_mean) * (slope - new_mean)
        
        var = self.vars[symbol] / n if n > 1 else 0.0
        std = math.sqrt(var) if var > 0 else 1.0
        
        z = (slope - new_mean) / std
        return max(-1.0, min(1.0, math.tanh(z)))

class TradeFlowImbalanceSignal(Signal):
    def __init__(self, decay_ms: int = 2000):
        self.decay_ms = decay_ms
        self.buy_vol = collections.defaultdict(float)
        self.sell_vol = collections.defaultdict(float)
        self.last_ts = collections.defaultdict(int)
        
    def update_trade(self, event: TradeEvent):
        sym = event.symbol
        t = event.event_ts
        last_t = self.last_ts.get(sym, t)
        dt = max(0, t - last_t)
        
        # Exponential decay weights
        decay_factor = math.exp(-dt / self.decay_ms)
        self.buy_vol[sym] *= decay_factor
        self.sell_vol[sym] *= decay_factor
        
        if not event.is_buyer_maker: # Aggressor was a buyer
            self.buy_vol[sym] += event.size
        else:
            self.sell_vol[sym] += event.size
            
        self.last_ts[sym] = t
        
    def get_signal(self, symbol: str) -> float:
        b = self.buy_vol.get(symbol, 0.0)
        s = self.sell_vol.get(symbol, 0.0)
        if b + s == 0:
            return 0.0
        return (b - s) / (b + s)

class VolatilityStateGatingSignal(Signal):
    def __init__(self, window_ms: int = 3000):
        self.window_ms = window_ms
        self.history = collections.defaultdict(collections.deque)
        
        # Standard deviation boundaries
        self.HIGH_THRESH = 0.003
        self.EXTREME_THRESH = 0.008
        
    def update_book(self, event: OrderBookUpdate):
        mid = (event.bid + event.ask) / 2.0
        t = event.event_ts
        sym = event.symbol
        dq = self.history[sym]
        dq.append((t, mid))
        while dq and t - dq[0][0] > self.window_ms:
            dq.popleft()
            
    def get_signal(self, symbol: str) -> float:
        # Returns Gating Multiplier Constraints [1.0, 0.3, 0.0]
        dq = self.history.get(symbol)
        if not dq or len(dq) < 2:
            return 1.0
            
        returns = [(dq[i][1] - dq[i-1][1])/dq[i-1][1] for i in range(1, len(dq))]
        if not returns: return 1.0
        
        mean_ret = sum(returns) / len(returns)
        var = sum((r - mean_ret)**2 for r in returns) / len(returns)
        std = math.sqrt(var)
        
        if std > self.EXTREME_THRESH:
            return 0.0 # Disable Quoting completely
        elif std > self.HIGH_THRESH:
            return 0.3 # Dampen highly
        return 1.0     # Normal

class AdverseSelectionPredictor:
    def __init__(self, penalty_lambda: float = 0.0005): # penalty scaled to fractional edge basis mappings
        self.penalty_lambda = penalty_lambda
        # Online Logistic mapping attributes
        self.weights = {
            'imb': 2.0,
            'slope': 1.5,
            'tfi': 3.0,
            'spread': -0.5,
            'queue': -0.5
        }
        
    def predict(self, imb: float, slope: float, tfi: float, spread: float, queue_ratio: float, side: str) -> float:
        # Predict P(mid moves against us).
        # We model directional toxic momentum mapping direction scalars.
        dir_mult = 1.0 if side == "BUY" else -1.0
        
        z = (
            self.weights['imb'] * (-imb * dir_mult) + 
            self.weights['slope'] * (-slope * dir_mult) + 
            self.weights['tfi'] * (-tfi * dir_mult) + 
            self.weights['spread'] * spread + 
            self.weights['queue'] * queue_ratio
        )
        
        p = 1.0 / (1.0 + math.exp(-z))
        
        # Final penalty is negatively transformed bound
        return -self.penalty_lambda * p

class SignalAggregator:
    def __init__(self):
        self.imb = OrderBookImbalanceSignal()
        self.slope = MicropriceSlopeSignal()
        self.tfi = TradeFlowImbalanceSignal()
        self.vol_gate = VolatilityStateGatingSignal()
        self.adverse_model = AdverseSelectionPredictor()
        
    def on_order_book_update(self, event: OrderBookUpdate):
        self.imb.update_book(event)
        self.slope.update_book(event)
        self.vol_gate.update_book(event)
        
    def on_trade(self, event: TradeEvent):
        self.tfi.update_trade(event)
        
    def get_composite_signals(self, symbol: str, bid: float, ask: float, bid_queue_ahead: float, ask_queue_ahead: float) -> dict:
        imb_val = self.imb.get_signal(symbol)
        slope_val = self.slope.get_signal(symbol)
        tfi_val = self.tfi.get_signal(symbol)
        gate_val = self.vol_gate.get_signal(symbol)
        
        spread = (ask - bid) / ask if ask > 0 else 0
        tot_size = bid_queue_ahead + ask_queue_ahead 
        b_q_ratio = bid_queue_ahead / tot_size if tot_size > 0 else 0
        a_q_ratio = ask_queue_ahead / tot_size if tot_size > 0 else 0
        
        bid_adv_penalty = self.adverse_model.predict(imb_val, slope_val, tfi_val, spread, b_q_ratio, "BUY")
        ask_adv_penalty = self.adverse_model.predict(imb_val, slope_val, tfi_val, spread, a_q_ratio, "SELL")
        
        w_imb, w_slope, w_tfi = 0.4, 0.4, 0.2
        composite_directional = w_imb * imb_val + w_slope * slope_val + w_tfi * tfi_val
        
        if settings.mm_active_signal != "ALL":
            active = settings.mm_active_signal
            if active != 'sig_imb': imb_val = 0.0
            if active != 'sig_slope': slope_val = 0.0
            if active != 'sig_tfi': tfi_val = 0.0
            if active != 'sig_bid_adv_pen': bid_adv_penalty = 0.0
            if active != 'sig_ask_adv_pen': ask_adv_penalty = 0.0
            # If a composite is specifically tested, we use raw outputs above but gate the directional mapping
            if active != 'sig_composite': composite_directional = 0.0
            
        return {
            'composite_directional': composite_directional,
            'gating_multiplier': gate_val,
            'bid_adverse_penalty': bid_adv_penalty,
            'ask_adverse_penalty': ask_adv_penalty,
            'signal_imb': imb_val,
            'signal_slope': slope_val,
            'signal_tfi': tfi_val
        }
