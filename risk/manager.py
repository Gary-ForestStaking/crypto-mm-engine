import logging
import uuid
from typing import Dict, Set
from models.events import StrategySignal, QuoteIntent, OrderIntent, FillEvent
from bus.event_bus import EventBus
from config.settings import settings

logger = logging.getLogger(__name__)

class RiskManager:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self.positions: Dict[str, float] = {}
        self.open_orders: Set[str] = set()
        
        # Guard limits
        self.max_position_size = settings.risk_max_position_size
        self.max_notional_exposure = settings.risk_max_notional_exposure
        self.max_open_orders = settings.risk_max_open_orders
        
        # Circuit Breaker state
        self.consecutive_errors = 0
        self.max_consecutive_errors = settings.risk_consecutive_errors_limit
        self.kill_switch_active = not settings.risk_global_active

        self.bus.subscribe(StrategySignal, self.on_strategy_signal)
        self.bus.subscribe(QuoteIntent, self.on_quote_intent)
        self.bus.subscribe(FillEvent, self.on_fill)

    def _get_notional_exposure(self, symbol: str, size: float, price: float = 100000.0) -> float:
        # Simplistic mapping of notional exposure (mock price if not provided to risk safely)
        return abs(size * price)

    async def on_quote_intent(self, quote: QuoteIntent):
        if self.kill_switch_active:
             logger.warning("Kill switch is active. Rejecting quote intent.")
             return
             
        if len(self.open_orders) >= self.max_open_orders:
             logger.warning(f"Risk rejected quote: Max open orders ({self.max_open_orders}) reached.")
             return
             
        current_pos = self.positions.get(quote.symbol, 0.0)
        
        # In a real system, quoting is highly stateful and issues Cancels first.
        # This translates the continuous quote layer into distinct limit orders for Executor tracking.
        orders_to_place = []
        
        # Bid Leg
        if quote.bid_size > 0 and abs(current_pos + quote.bid_size) <= self.max_position_size:
            bid_notional = self._get_notional_exposure(quote.symbol, current_pos + quote.bid_size, quote.bid_price)
            if bid_notional <= self.max_notional_exposure:
                orders_to_place.append(OrderIntent(
                    symbol=quote.symbol,
                    strategy_monotonic=quote.recv_monotonic, # Chain it through
                    side="BUY",
                    price=quote.bid_price,
                    size=quote.bid_size,
                    intent_id=str(uuid.uuid4())
                ))
                
        # Ask Leg
        if quote.ask_size > 0 and abs(current_pos - quote.ask_size) <= self.max_position_size:
            ask_notional = self._get_notional_exposure(quote.symbol, current_pos - quote.ask_size, quote.ask_price)
            if ask_notional <= self.max_notional_exposure:
                orders_to_place.append(OrderIntent(
                    symbol=quote.symbol,
                    strategy_monotonic=quote.recv_monotonic,
                    side="SELL",
                    price=quote.ask_price,
                    size=quote.ask_size,
                    intent_id=str(uuid.uuid4())
                ))
                
        for intent in orders_to_place:
             self.open_orders.add(intent.intent_id)
             await self.bus.publish(intent)

    async def on_strategy_signal(self, signal: StrategySignal):
            self.open_orders.add(intent_id)
            logger.info(f"Risk approved intent: {intent}")
            await self.bus.publish(intent)

    async def on_fill(self, fill: FillEvent):
        if fill.intent_id in self.open_orders:
            self.open_orders.remove(fill.intent_id)
            
        current = self.positions.get(fill.symbol, 0.0)
        # Update Position
        if fill.side == "BUY":
            self.positions[fill.symbol] = current + fill.size
        else:
            self.positions[fill.symbol] = current - fill.size
            
        logger.info(f"Position updated for {fill.symbol}: {self.positions[fill.symbol]}")
        
    def report_execution_error(self):
        """Called by Executor when a downstream placement fails catastrophically."""
        self.consecutive_errors += 1
        if self.consecutive_errors >= self.max_consecutive_errors:
             logger.critical("CIRCUIT BREAKER TRIGGERED: Max consecutive execution errors reached.")
             self.kill_switch_active = True
             
    def reset_execution_errors(self):
        self.consecutive_errors = 0
