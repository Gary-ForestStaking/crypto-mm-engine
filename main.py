import asyncio
import logging
import signal
import sys
from bus.event_bus import EventBus
from config.settings import settings
from feed.binance_ws import BinanceFuturesFeed
from strategy.market_maker import MarketMakingStrategy
from risk.manager import RiskManager
from execution.executor import BinanceExecutor
from persistence.clickhouse_writer import ClickHouseWriter
from metrics.tracker import MetricsTracker

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

async def main():
    logger.info("Starting Strategy Engine")
    
    # Initialize Event Bus
    bus = EventBus()
    
    # Initialize Components
    feed = BinanceFuturesFeed(bus, settings.symbols)
    strategy = MarketMakingStrategy(bus)
    strategy.register()
    
    risk = RiskManager(bus)
    executor = BinanceExecutor(bus, risk_manager=risk)
    persistence = ClickHouseWriter(bus)
    metrics = MetricsTracker(bus)
    
    # Start Background Tasks
    await bus.start()
    await persistence.start()
    await feed.start()
    
    logger.info("All engine components started. Waiting for events...")
    
    # Setup graceful shutdown
    stop_event = asyncio.Event()

    def handle_exception(*args):
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        try:
            loop.add_signal_handler(signal.SIGINT, handle_exception)
            loop.add_signal_handler(signal.SIGTERM, handle_exception)
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
    finally:
        logger.info("Initiating graceful shutdown sequence...")
        # 1. Stop feed so no new events come in
        await feed.stop()
        
        # 2. Cancel open orders
        await executor.close()
        
        # 3. Drain and stop event bus so all inflight events process to queues
        await bus.stop()
        
        # 4. Flush persistence cleanly
        await persistence.stop()
        
        logger.info("Graceful shutdown complete. Exiting.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
