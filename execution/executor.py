import aiohttp
import asyncio
import hashlib
import hmac
import time
import logging
from typing import Any, Dict, Set
from urllib.parse import urlencode
from models.events import OrderIntent, FillEvent, MetricsEvent
from bus.event_bus import EventBus
from config.settings import settings
from risk.manager import RiskManager

logger = logging.getLogger(__name__)

class BinanceExecutor:
    def __init__(self, bus: EventBus, risk_manager: RiskManager):
        self.bus = bus
        self.risk_manager = risk_manager
        self.api_key = settings.binance_api_key
        self.api_secret = settings.binance_api_secret.encode('utf-8')
        if settings.binance_testnet:
            self.base_url = "https://testnet.binancefuture.com"
        else:
            self.base_url = "https://fapi.binance.com"
        
        self.session: Any = None
        # Pending orders tracking to prevent duplication and detect ACKs
        self.pending_intents: Set[str] = set()
        self.acked_orders: Set[str] = set()
        
        self.bus.subscribe(OrderIntent, self.on_order_intent)

    def _generate_signature(self, query_string: str) -> str:
        return hmac.new(self.api_secret, query_string.encode('utf-8'), hashlib.sha256).hexdigest()

    async def _request(self, method: str, endpoint: str, params: dict) -> tuple[dict, int]:
        if not self.session:
            # We want short timeouts to fail fast
            timeout = aiohttp.ClientTimeout(total=2.0)
            self.session = aiohttp.ClientSession(headers={"X-MBX-APIKEY": self.api_key}, timeout=timeout)
            
        params['timestamp'] = int(time.time() * 1000)
        query_string = urlencode(params)
        signature = self._generate_signature(query_string)
        url = f"{self.base_url}{endpoint}?{query_string}&signature={signature}"
        
        async with self.session.request(method, url) as response:
            return await response.json(), response.status

    def _get_client_order_id(self, intent_id: str, attempt: int) -> str:
        # Deterministic generation using a hash
        raw = f"{intent_id}_{attempt}".encode('utf-8')
        return hashlib.md5(raw).hexdigest()

    async def on_order_intent(self, intent: OrderIntent):
        if intent.intent_id in self.pending_intents:
            logger.warning(f"Duplicate intent {intent.intent_id} detected, ignoring.")
            return

        self.pending_intents.add(intent.intent_id)

        if not self.api_key or not self.api_secret:
            logger.warning("API keys not set. Running in dummy mode.")
            await self._emit_mock_fill(intent)
            self.pending_intents.remove(intent.intent_id)
            return

        max_retries = 3
        for attempt in range(max_retries):
            client_oid = self._get_client_order_id(intent.intent_id, attempt)
            
            # Did we already get an ACK for this intent?
            if client_oid in self.acked_orders:
                logger.warning(f"Duplicate ACK detected for clientOrderId {client_oid}. Skipping retry.")
                break
                
            try:
                params = {
                    "symbol": intent.symbol,
                    "side": intent.side,
                    "type": "MARKET" if intent.price == 0.0 else "LIMIT",
                    "quantity": intent.size,
                    "newClientOrderId": client_oid
                }
                if intent.price != 0.0:
                    params["timeInForce"] = "GTC"
                    params["price"] = intent.price

                logger.info(f"Placing order attempt {attempt}: {client_oid}")
                res, status = await self._request("POST", "/fapi/v1/order", params)
                
                if status == 200:
                    self.acked_orders.add(client_oid)
                    self.risk_manager.reset_execution_errors()
                    
                    # Compute Latency
                    import time as tm
                    now_monotonic_ms = int(tm.monotonic() * 1000)
                    str_to_exe_ms = now_monotonic_ms - intent.strategy_monotonic
                    
                    await self.bus.publish(MetricsEvent(
                        metric_name="latency_strategy_to_execution_ms",
                        value=str_to_exe_ms,
                        labels={"client_oid": client_oid}
                    ))

                    fill_price = float(res.get('avgPrice', intent.price))
                    if fill_price == 0.0:
                        fill_price = float(res.get('price', intent.price))
                        if fill_price == 0.0: 
                            fill_price = 100.0

                    fill = FillEvent(
                        symbol=intent.symbol,
                        side=intent.side,
                        price=fill_price,
                        size=float(res.get('executedQty', intent.size)),
                        order_id=str(res.get('orderId')),
                        intent_id=intent.intent_id,
                        intent_monotonic=intent.strategy_monotonic
                    )
                    await self.bus.publish(fill)
                    break
                else:
                    logger.error(f"Failed to place order: {res}")
                    await asyncio.sleep(2 ** attempt)
                    
            except asyncio.TimeoutError:
                logger.error(f"Execution timeout on attempt {attempt}")
                # Depending on the exchange, a timeout could mean the order was placed or dropped.
                # Deterministic IDs allow us to send a query or a cancel-replace, but here we just
                # backoff and retry with a new clientOrderId because the exchange natively rejects dups.
                await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"Execution error on attempt {attempt}: {e}")
                self.risk_manager.report_execution_error()
                await asyncio.sleep(2 ** attempt)

        if intent.intent_id in self.pending_intents:
            self.pending_intents.remove(intent.intent_id)

    async def _emit_mock_fill(self, intent: OrderIntent):
        import time as tm
        now_monotonic_ms = int(tm.monotonic() * 1000)
        str_to_exe_ms = now_monotonic_ms - intent.strategy_monotonic

        await self.bus.publish(MetricsEvent(
            metric_name="latency_strategy_to_execution_ms",
            value=str_to_exe_ms,
            labels={"mock": "true"}
        ))

        fill = FillEvent(
            symbol=intent.symbol,
            side=intent.side,
            price=intent.price if intent.price != 0.0 else 100.0,
            size=intent.size,
            order_id=f"mock_{intent.intent_id}",
            intent_id=intent.intent_id,
            intent_monotonic=intent.strategy_monotonic
        )
        await self.bus.publish(fill)

    async def close(self):
        # Cancel all open intents
        self.pending_intents.clear()
        if self.session:
            await self.session.close()
            self.session = None
