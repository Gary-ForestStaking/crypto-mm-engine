import asyncio
import logging
import time
from bus.event_bus import EventBus
from models.events import OrderBookUpdate
from config.settings import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("StressTest")

async def mock_handler(event: OrderBookUpdate):
    # Simulate a tiny bit of processing latency
    await asyncio.sleep(0.001)

async def test_burst_traffic():
    # Make queues small so we easily trigger backpressure
    settings.bus_queue_size = 100
    settings.bus_backpressure_policy = "drop_oldest"
    
    bus = EventBus()
    bus.subscribe(OrderBookUpdate, mock_handler)
    
    await bus.start()
    
    num_events = 2000
    logger.info(f"Simulating burst of {num_events} events...")
    start_time = time.monotonic()
    
    # Pump traffic as fast as possible in tight async loop
    for i in range(num_events):
        ob = OrderBookUpdate(
            symbol="BTCUSDT",
            event_ts=int(time.time() * 1000),
            bid=1000.0,
            ask=1001.0,
            bid_size=1.0,
            ask_size=1.0
        )
        await bus.publish(ob)
        
    publish_duration = time.monotonic() - start_time
    logger.info(f"Published {num_events} events in {publish_duration:.3f}s")
    
    # Wait for processing queues to stabilize
    await asyncio.sleep(2)
    
    # The metrics task logged queue drops automatically. Let's gracefully terminate.
    await bus.stop()
    logger.info("Stress test complete. Notice in the logs above how backpressure metrics fired correctly if drops occurred!")

if __name__ == "__main__":
    try:
        asyncio.run(test_burst_traffic())
    except KeyboardInterrupt:
        pass
