import asyncio
from typing import Callable, Awaitable, Dict, List, Type, Any
from models.events import BaseEvent, MetricsEvent
from config.settings import settings
import logging

logger = logging.getLogger(__name__)

EventHandler = Callable[[BaseEvent], Awaitable[None]]

class SubscriberQueue:
    def __init__(self, handler: EventHandler, maxsize: int, policy: str):
        self.handler = handler
        self.queue = asyncio.Queue(maxsize=maxsize)
        self.policy = policy
        self.task = None
        self.dropped_count = 0

    async def put(self, event: BaseEvent):
        if self.policy == "block":
            await self.queue.put(event)
        else:
            if self.queue.full():
                self.dropped_count += 1
                if self.policy == "drop_oldest":
                    try:
                        self.queue.get_nowait()
                        self.queue.task_done()
                    except asyncio.QueueEmpty:
                        pass
                    await self.queue.put(event)
                elif self.policy == "drop_new":
                    pass
            else:
                await self.queue.put(event)

    async def consume(self):
        while True:
            try:
                event = await self.queue.get()
                try:
                    await self.handler(event)
                except Exception as e:
                    logger.error(f"Handler {self.handler.__name__} error: {e}")
                finally:
                    self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Consumer error in {self.handler.__name__}: {e}")

class EventBus:
    def __init__(self):
        self._subscribers: Dict[Type[BaseEvent], List[SubscriberQueue]] = {}
        self._running = False
        self._metrics_task: Any = None
        # Circular import prevention: pass reference to instance itself, or publish metric directly to queues
        # But publish is async, and we can just call self.publish

    def subscribe(self, event_type: Type[BaseEvent], handler: EventHandler):
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        
        sub_queue = SubscriberQueue(
            handler=handler,
            maxsize=settings.bus_queue_size,
            policy=settings.bus_backpressure_policy
        )
        self._subscribers[event_type].append(sub_queue)
        
        if self._running:
            sub_queue.task = asyncio.create_task(sub_queue.consume())
            
        logger.info(f"Subscribed {handler.__name__} to {event_type.__name__}")

    async def publish(self, event: BaseEvent):
        """Dispatches event directly to subscriber queues enforcing isolation and backpressure."""
        subs_to_notify = []
        for event_t, subs in self._subscribers.items():
            if isinstance(event, event_t):
                subs_to_notify.extend(subs)
                
        for sub in set(subs_to_notify): # Avoid duplicate deliveries
            await sub.put(event)

    async def _emit_metrics(self):
        while self._running:
            await asyncio.sleep(5.0)
            for event_t, subs in self._subscribers.items():
                for sub in subs:
                    q_size = sub.queue.qsize()
                    dropped = sub.dropped_count
                    if q_size > 0 or dropped > 0:
                        metric_depth = MetricsEvent(
                            metric_name="bus_queue_depth",
                            value=q_size,
                            labels={"handler": sub.handler.__name__}
                        )
                        metric_dropped = MetricsEvent(
                            metric_name="bus_dropped_events",
                            value=dropped,
                            labels={"handler": sub.handler.__name__}
                        )
                        await self.publish(metric_depth)
                        await self.publish(metric_dropped)

    async def start(self):
        if not self._running:
            self._running = True
            for event_t, subs in self._subscribers.items():
                for sub in subs:
                    sub.task = asyncio.create_task(sub.consume())
            self._metrics_task = asyncio.create_task(self._emit_metrics())
            logger.info("Event bus started with per-subscriber isolated queues")

    async def stop(self):
        self._running = False
        if self._metrics_task:
            self._metrics_task.cancel()
            
        logger.info("Draining event bus subscriber queues...")
        # Drain queues gracefully
        for event_t, subs in self._subscribers.items():
            for sub in subs:
                if not sub.queue.empty():
                    try:
                        await asyncio.wait_for(sub.queue.join(), timeout=3.0)
                    except asyncio.TimeoutError:
                        logger.warning(f"Timeout draining queue for {sub.handler.__name__}")
                if sub.task:
                    sub.task.cancel()
        logger.info("Event bus stopped")
