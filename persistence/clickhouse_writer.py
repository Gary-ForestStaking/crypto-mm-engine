import asyncio
import logging
import time
import datetime
import clickhouse_connect
from models.events import OrderBookUpdate, TradeEvent, StrategySignal, OrderIntent, FillEvent, BaseEvent, MetricsEvent
from config.settings import settings
from bus.event_bus import EventBus
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

class ClickHouseWriter:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self.client: Any = None
        self._running = False
        self._task: Any = None
        self._buffer: Dict[str, List[List[Any]]] = {
            "market_events": [],
            "strategy_signals": [],
            "order_intents": [],
            "fills": [],
            "strategy_metrics": [],
            "calibration_metrics": [],
            "fills_metrics": []
        }
        self.batch_size = settings.clickhouse_batch_size
        self.flush_interval = settings.clickhouse_flush_interval
        self.max_buffer_size = settings.clickhouse_max_buffer_size
        
        # Exponential backoff properties
        self.error_count = 0
        self.circuit_breaker_active = False
        self.last_failed_ts = 0.0
        self.cb_duration = settings.clickhouse_circuit_breaker_sec

        self.bus.subscribe(OrderBookUpdate, self.on_market_event)
        self.bus.subscribe(TradeEvent, self.on_market_event)
        self.bus.subscribe(StrategySignal, self.on_strategy_signal)
        self.bus.subscribe(OrderIntent, self.on_order_intent)
        self.bus.subscribe(FillEvent, self.on_fill_event)
        from models.events import StrategyMetricsPayload, CalibrationMetricsPayload, FillMetricsPayload
        self.bus.subscribe(StrategyMetricsPayload, self.on_strategy_metrics)
        self.bus.subscribe(CalibrationMetricsPayload, self.on_calibration_metrics)
        self.bus.subscribe(FillMetricsPayload, self.on_fills_metrics)

    def _connect(self):
        try:
            kwargs = {
                "host": settings.clickhouse_host,
                "port": settings.clickhouse_port,
                "username": settings.clickhouse_user,
                "database": settings.clickhouse_database,
                "connect_timeout": 2
            }
            if settings.clickhouse_password:
                 kwargs["password"] = settings.clickhouse_password

            self.client = clickhouse_connect.get_client(**kwargs)
            logger.info("Connected to ClickHouse")
        except Exception as e:
            logger.error(f"Failed to connect to ClickHouse: {e}")

    def _drop_if_full(self):
        total_items = sum(len(v) for v in self._buffer.values())
        if total_items > self.max_buffer_size:
            # We enforce bounded buffer by clearing the oldest content chunks or clearing everything over limit.
            # To avoid OOM without complexity, we can just reject or clear lists when overwhelmed.
            logger.critical(f"ClickHouse buffer exceeded max ({self.max_buffer_size}). Dropping entire buffer to prevent OOM!")
            for k in self._buffer:
                self._buffer[k].clear()

    async def _emit_metrics(self):
        total_items = sum(len(v) for v in self._buffer.values())
        metric = MetricsEvent(
            metric_name="persistence_buffer_size",
            value=total_items
        )
        await self.bus.publish(metric)

    async def on_market_event(self, event: BaseEvent):
        # event_ts is from exchange (ms). Insert expects seconds.float
        if isinstance(event, OrderBookUpdate):
            row = [datetime.datetime.fromtimestamp(event.event_ts / 1000.0), event.symbol, 'orderbook', event.bid, event.ask, 0.0, 0.0]
        elif isinstance(event, TradeEvent):
            row = [datetime.datetime.fromtimestamp(event.event_ts / 1000.0), event.symbol, 'trade', 0.0, 0.0, event.price, event.size]
        else:
            return
            
        self._buffer["market_events"].append(row)
        self._drop_if_full()
        await self._check_flush()

    async def on_strategy_signal(self, event: StrategySignal):
        row = [datetime.datetime.fromtimestamp(event.event_ts / 1000.0), event.symbol, event.signal_type, event.strength]
        self._buffer["strategy_signals"].append(row)
        self._drop_if_full()
        await self._check_flush()

    async def on_order_intent(self, event: OrderIntent):
        row = [datetime.datetime.fromtimestamp(event.event_ts / 1000.0), event.symbol, event.side, event.price, event.size, event.intent_id]
        self._buffer["order_intents"].append(row)
        self._drop_if_full()
        await self._check_flush()

    async def on_fill_event(self, event: FillEvent):
        row = [datetime.datetime.fromtimestamp(event.event_ts / 1000.0), event.symbol, event.side, event.price, event.size, event.order_id]
        self._buffer["fills"].append(row)
        self._drop_if_full()
        await self._check_flush()
        
    async def on_strategy_metrics(self, event):
        row = [datetime.datetime.fromtimestamp(event.event_ts / 1000.0), event.symbol, event.mid, event.microprice, event.fair_value,
               event.expected_edge, event.effective_edge_after_fees, event.inventory_ratio,
               event.queue_ahead_estimate, event.fill_prob, event.short_vol, event.regime]
        self._buffer["strategy_metrics"].append(row)
        self._drop_if_full()
        await self._check_flush()
        
    async def on_calibration_metrics(self, event):
        row = [datetime.datetime.fromtimestamp(event.event_ts / 1000.0), event.symbol, event.predicted_fill_prob, event.realized_fill_rate,
               event.expected_edge_mean, event.realized_effective_edge_mean, event.ev_bias,
               event.adverse_drift_mean, event.realized_fill_pnl_mean]
        self._buffer["calibration_metrics"].append(row)
        self._drop_if_full()
        await self._check_flush()

    async def on_fills_metrics(self, event):
        row = [datetime.datetime.fromtimestamp(event.event_ts / 1000.0), event.symbol, event.side, event.fill_price, 
               event.fill_size, event.fair_value_at_fill, event.fee_paid, event.realized_edge_fractional, event.realized_pnl_usd]
        self._buffer["fills_metrics"].append(row)
        self._drop_if_full()
        await self._check_flush()

    async def _check_flush(self):
        total_items = sum(len(v) for v in self._buffer.values())
        if total_items >= self.batch_size and not self.circuit_breaker_active:
            # Create a non-blocking background task. If one is already running, wait for flush_loop.
            # Actually, to avoid queue congestion, run non-blocking fire-forget task
            asyncio.create_task(self._flush())

    async def _flush(self):
        now = time.time()
        
        # Circuit Breaker Logic
        if self.circuit_breaker_active:
             if now - self.last_failed_ts < self.cb_duration:
                  return # CB still active
             else:
                  self.circuit_breaker_active = False # Half-open, attempt flush

        if not self.client:
            self._connect()
            if not self.client:
                 self.circuit_breaker_active = True
                 self.last_failed_ts = now
                 return

        cache_copy = {k: list(v) for k, v in self._buffer.items()}
        total_items = sum(len(v) for v in cache_copy.values())
        
        if total_items == 0:
            return

        for k in self._buffer:
            self._buffer[k].clear()
            
        try:
            start_t = time.monotonic()
            
            # Since insert relies on IO, run in executor to prevent blocking the event loop
            def blocking_inserts():
                if cache_copy["market_events"]:
                    self.client.insert("market_events", cache_copy["market_events"], column_names=["ts", "symbol", "event_type", "bid", "ask", "price", "size"])
                if cache_copy["strategy_signals"]:
                    self.client.insert("strategy_signals", cache_copy["strategy_signals"], column_names=["ts", "symbol", "signal_type", "strength"])
                if cache_copy["order_intents"]:
                    self.client.insert("order_intents", cache_copy["order_intents"], column_names=["ts", "symbol", "side", "price", "size", "intent_id"])
                if cache_copy["fills"]:
                    self.client.insert("fills", cache_copy["fills"], column_names=["ts", "symbol", "side", "price", "size", "order_id"])
                if cache_copy["strategy_metrics"]:
                    self.client.insert("strategy_metrics", cache_copy["strategy_metrics"], 
                        column_names=["ts", "symbol", "mid", "microprice", "fair_value", "expected_edge", 
                                      "effective_edge_after_fees", "inventory_ratio", "queue_ahead_estimate", "fill_prob", "short_vol", "regime"])
                if cache_copy["calibration_metrics"]:
                    self.client.insert("calibration_metrics", cache_copy["calibration_metrics"], 
                        column_names=["ts", "symbol", "predicted_fill_prob", "realized_fill_rate", "expected_edge_mean", 
                                      "realized_effective_edge_mean", "ev_bias", "adverse_drift_mean", "realized_fill_pnl_mean"])
                if cache_copy["fills_metrics"]:
                    self.client.insert("fills_metrics", cache_copy["fills_metrics"], 
                        column_names=["ts", "symbol", "side", "fill_price", "fill_size", "fair_value_at_fill", 
                                      "fee_paid", "realized_edge_fractional", "realized_pnl_usd"])

            await asyncio.to_thread(blocking_inserts)

            end_t = time.monotonic()
            lag_ms = (end_t - start_t) * 1000
            
            # Reset error metrics on success
            self.error_count = 0
            
            metric_lag = MetricsEvent(
                metric_name="persistence_lag_ms",
                value=lag_ms
            )
            await self.bus.publish(metric_lag)

        except Exception as e:
             self.error_count += 1
             # Exponential backoff simulation for repeated failures
             self.cb_duration = min(60, settings.clickhouse_circuit_breaker_sec * (2 ** (self.error_count - 1)))
             self.circuit_breaker_active = True
             self.last_failed_ts = time.time()
             logger.error(f"ClickHouse insert error. Circuit breaker ON for {self.cb_duration}s. Error: {e}")
             
             # Instead of losing data immediately during CB, if buffer isn't full, put the items back at front.
             for k, items in cache_copy.items():
                 self._buffer[k] = items + self._buffer[k] 
             self._drop_if_full()

    async def _flush_loop(self):
        while self._running:
            await asyncio.sleep(self.flush_interval)
            await self._emit_metrics()
            if not self.circuit_breaker_active:
                await self._flush()

    async def start(self):
        self._connect()
        if not self._running:
            self._running = True
            logger.info("Starting ClickHouse buffered writer task")
            self._task = asyncio.create_task(self._flush_loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
        await self._flush()
        logger.info("ClickHouse writer stopped")
