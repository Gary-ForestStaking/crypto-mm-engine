import logging
from models.events import OrderBookUpdate, StrategySignal, TradeEvent
from bus.event_bus import EventBus
from config.settings import settings

logger = logging.getLogger(__name__)

class SpreadCaptureStrategy:
    """
    A pure stateless strategy that looks for wide spreads to capture.
    It doesn't keep complex state or make API calls.
    """
    def __init__(self, bus: EventBus):
        self.bus = bus
        self.min_spread = settings.strategy_spread_threshold
        
    async def on_order_book_update(self, event: OrderBookUpdate):
        if event.bid == 0.0:
            return
            
        spread = (event.ask - event.bid) / event.bid
        
        if spread > self.min_spread:
            logger.debug(f"Wide spread detected on {event.symbol}: {spread:.5f}. Emitting spread capture signal.")
            # Example logic: if spread is wide, we want to provide liquidity
            signal = StrategySignal(
                symbol=event.symbol,
                feed_monotonic=event.recv_monotonic,
                signal_type="SPREAD_CAPTURE",
                strength=spread
            )
            await self.bus.publish(signal)
            
    async def on_trade(self, event: TradeEvent):
        # Additional logic based on trades could be implemented here
        pass

    def register(self):
        self.bus.subscribe(OrderBookUpdate, self.on_order_book_update)
        self.bus.subscribe(TradeEvent, self.on_trade)
        logger.info("SpreadCaptureStrategy registered handlers")
