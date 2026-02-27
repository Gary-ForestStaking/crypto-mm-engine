import asyncio
import json
import logging
import websockets
from typing import List, Any
from models.events import OrderBookUpdate, TradeEvent, TickerEvent
from bus.event_bus import EventBus
from config.settings import settings

logger = logging.getLogger(__name__)

class BinanceFuturesFeed:
    def __init__(self, bus: EventBus, symbols: List[str]):
        self.bus = bus
        self.symbols = [s.lower() for s in symbols]
        self.base_url = "wss://fstream.binance.com/stream?streams="
        if settings.binance_testnet:
            self.base_url = "wss://stream.binancefuture.com/stream?streams="
        self._running = False
        self._task: Any = None

    def _get_streams(self) -> str:
        streams = []
        for sym in self.symbols:
            streams.append(f"{sym}@bookTicker")
            streams.append(f"{sym}@depth@100ms")
            streams.append(f"{sym}@aggTrade")
        return "/".join(streams)

    async def _handle_message(self, message: str):
        try:
            raw_data = json.loads(message)
            data = raw_data.get('data', raw_data)
            event_type = data.get('e')
            
            if event_type == 'aggTrade':
                event = TradeEvent(
                    symbol=data['s'],
                    event_ts=data['E'],
                    price=float(data['p']),
                    size=float(data['q']),
                    is_buyer_maker=data['m']
                )
                await self.bus.publish(event)
                
            elif event_type == 'depthUpdate':
                pass
                
            elif 'u' in data and 'b' in data and 'a' in data: # bookTicker
                # Instead of having it in the payload time, we must parse 'E' if provided, else use current time
                E_ts = data.get('E', 0)
                if E_ts == 0:
                    import time
                    E_ts = int(time.time() * 1000)

                ob_event = OrderBookUpdate(
                    symbol=data['s'],
                    event_ts=E_ts,
                    bid=float(data['b']),
                    ask=float(data['a']),
                    bid_size=float(data['B']),
                    ask_size=float(data['A'])
                )
                await self.bus.publish(ob_event)
                
                mid_price = (ob_event.bid + ob_event.ask) / 2
                ticker_event = TickerEvent(
                    symbol=ob_event.symbol, 
                    event_ts=E_ts,
                    price=mid_price
                )
                await self.bus.publish(ticker_event)
                
        except Exception as e:
            logger.error(f"Error parsing message: {e}")

    async def _connect_and_listen(self):
        streams = self._get_streams()
        url = f"{self.base_url}{streams}"
        
        while self._running:
            try:
                logger.info(f"Connecting to {url}")
                async with websockets.connect(url, max_queue=2048) as ws:
                    logger.info("Connected to Binance WebSocket")
                    while self._running:
                        msg = await asyncio.wait_for(ws.recv(), timeout=60.0)
                        await self._handle_message(msg)
            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket closed, reconnecting...")
                await asyncio.sleep(1)
            except asyncio.TimeoutError:
                logger.warning("WebSocket timeout, reconnecting...")
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                await asyncio.sleep(5)

    async def start(self):
        if not self._running:
            self._running = True
            logger.info("Binance feed starting...")
            self._task = asyncio.create_task(self._connect_and_listen())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Binance feed stopped")
