import time
import logging
import collections
import asyncio
import numpy as np
from scipy import stats
from bus.event_bus import EventBus
from models.events import OrderBookUpdate, MetricsEvent, SignalForwardStatsPayload, SignalRegressionStatsPayload, SignalICStatsPayload

logger = logging.getLogger(__name__)

class SignalValidationModule:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self.window_size = 500 # elements mapped dynamically
        self.horizons = [100, 250, 500, 1000]
        
        # State tracking
        self.last_signals = {}
        self.snapshots = collections.deque()
        self.mid_history = collections.defaultdict(list)
        self.completed_trials = collections.defaultdict(list)
        
        self.regression_samples = collections.defaultdict(list)
        self.ic_samples = collections.defaultdict(list)
        
        self.last_report_ts = time.time()
        self._task = None

    def register(self):
        self.bus.subscribe(OrderBookUpdate, self.on_order_book_update)
        self.bus.subscribe(MetricsEvent, self.on_metrics_event)
        self._task = asyncio.create_task(self._cron_publish())
        logger.info("Signal Validation Module registered.")
        
    async def _cron_publish(self):
        """Asynchronous execution detached to not block event loop."""
        while True:
            await asyncio.sleep(5.0)
            try:
                 await self._compute_and_publish()
            except Exception as e:
                 logger.error(f"Signal validation error: {e}")

    async def on_metrics_event(self, event: MetricsEvent):
        symbol = event.labels.get('symbol')
        if not symbol: return
        sn = event.metric_name
        req_signals = ['sig_composite', 'sig_imb', 'sig_slope', 'sig_tfi', 'sig_bid_adv_pen', 'sig_ask_adv_pen']
        if sn in req_signals:
            if symbol not in self.last_signals:
                self.last_signals[symbol] = {}
            self.last_signals[symbol][sn] = event.value

    async def on_order_book_update(self, event: OrderBookUpdate):
        mid = (event.bid + event.ask) / 2.0
        ts = event.event_ts
        sym = event.symbol
        
        self.mid_history[sym].append((ts, mid))
        
        # Keep last 10 seconds bounded
        while self.mid_history[sym] and ts - self.mid_history[sym][0][0] > 10000:
            self.mid_history[sym].pop(0)
            
        # Register a snapshot
        req = ['sig_composite', 'sig_imb', 'sig_slope', 'sig_tfi', 'sig_bid_adv_pen', 'sig_ask_adv_pen']
        signals = self.last_signals.get(sym, {})
        
        if all(r in signals for r in req):
            self.snapshots.append({
                'ts': ts,
                'sym': sym,
                'mid': mid,
                'signals': dict(signals)
            })

        self._process_snapshots(ts)
        
    def _process_snapshots(self, current_ts):
        # We need a copy of processed to remove since we iterate FIFO
        while self.snapshots:
            snap = self.snapshots[0]
            # Has absolute maximum horizon elapsed?
            if current_ts - snap['ts'] < max(self.horizons): 
                break
                
            snap = self.snapshots.popleft()
            sym = snap['sym']
            t0 = snap['ts']
            mid0 = snap['mid']
            sigs = snap['signals']
            
            mid_hist = self.mid_history[sym]
            
            for h in self.horizons:
                target_t = t0 + h
                future_mid = mid0
                for (th, mh) in mid_hist:
                    if th >= target_t:
                        future_mid = mh
                        break
                        
                ret = (future_mid - mid0) / mid0 if mid0 > 0 else 0
                
                # Assign correlation arrays 
                for sname, sval in sigs.items():
                    key = (sym, h, sname)
                    self.completed_trials[key].append((sval, ret))
                    if len(self.completed_trials[key]) > self.window_size:
                        self.completed_trials[key].pop(0)

                # Assign 250ms matrix arrays
                if h == 250:
                    X = [sigs['sig_imb'], sigs['sig_slope'], sigs['sig_tfi'], sigs['sig_bid_adv_pen'], sigs['sig_ask_adv_pen']]
                    self.regression_samples[sym].append((X, ret))
                    if len(self.regression_samples[sym]) > self.window_size:
                        self.regression_samples[sym].pop(0)

    async def _compute_and_publish(self):
        # Forward Stats & Information Coefficient
        for (sym, h, sname), trials in list(self.completed_trials.items()):
            if len(trials) < 50: continue
            
            arr = np.array(trials)
            X = arr[:, 0]
            Y = arr[:, 1]
            
            std_X = np.std(X)
            std_Y = np.std(Y)
            
            if std_X == 0 or std_Y == 0: continue
            
            r, _ = stats.pearsonr(X, Y)
            rho, _ = stats.spearmanr(X, Y)
            mean_ret = np.mean(Y)
            n_len = len(trials)
            
            t_s = r * np.sqrt(n_len - 2) / np.sqrt(1 - r**2) if r < 1.0 and r > -1.0 else 0.0
            
            await self.bus.publish(SignalForwardStatsPayload(
                symbol=sym,
                signal_name=sname,
                horizon_ms=h,
                correlation=float(r) if not np.isnan(r) else 0.0,
                mean_forward_return=float(mean_ret),
                t_stat=float(t_s) if not np.isnan(t_s) else 0.0,
                sample_size=n_len
            ))
            
            if h == 250:
                 await self.bus.publish(SignalICStatsPayload(
                      symbol=sym,
                      signal_name=sname,
                      horizon_ms=h,
                      rolling_ic=float(rho) if not np.isnan(rho) else 0.0,
                      rolling_mean_ic=float(rho) if not np.isnan(rho) else 0.0
                 ))

        # Regression Stats
        for sym, samps in list(self.regression_samples.items()):
            if len(samps) < 50: continue
            
            X_arr = np.array([s[0] for s in samps])
            Y_arr = np.array([s[1] for s in samps])
            
            try:
                X_design = np.column_stack((np.ones(len(X_arr)), X_arr))
                beta, res, rnk, sing = np.linalg.lstsq(X_design, Y_arr, rcond=None)
                
                pred = X_design @ beta
                residuals = Y_arr - pred
                
                df = len(Y_arr) - len(beta)
                if df <= 0: continue
                    
                sigma2 = np.sum(residuals**2) / df
                inv_xtx = np.linalg.inv(X_design.T @ X_design)
                var_beta = sigma2 * inv_xtx
                se = np.sqrt(np.diag(var_beta))
                t_stats = beta / np.maximum(se, 1e-10)
                
                await self.bus.publish(SignalRegressionStatsPayload(
                    symbol=sym,
                    horizon_ms=250,
                    sig_imb_coef=float(beta[1]),
                    sig_imb_se=float(se[1]),
                    sig_imb_t=float(t_stats[1]),
                    sig_slope_coef=float(beta[2]),
                    sig_slope_se=float(se[2]),
                    sig_slope_t=float(t_stats[2]),
                    sig_tfi_coef=float(beta[3]),
                    sig_tfi_se=float(se[3]),
                    sig_tfi_t=float(t_stats[3]),
                    sig_bid_adv_coef=float(beta[4]),
                    sig_bid_adv_se=float(se[4]),
                    sig_bid_adv_t=float(t_stats[4]),
                    sig_ask_adv_coef=float(beta[5]),
                    sig_ask_adv_se=float(se[5]),
                    sig_ask_adv_t=float(t_stats[5])
                ))
            except Exception:
                pass
