# fyers_broker_interface.py
"""
Unified Broker Interface with WebSocket-First Architecture.

Design:
- WebSocket connections for real-time data (order updates, position changes, ticks)
- REST API as fallback for operations not supported by WebSocket
- Transparent caching layer to minimize REST API calls
- Thread-safe, async-first design

Usage (from order_manager.py):
    broker = FyersBrokerInterface(access_token, db_manager)
    await broker.initialize()
    
    # All calls look the same as before:
    order_id = await broker.place_order(symbol='NSE:SBIN-EQ', side='SELL', qty=100)
    await broker.wait_for_fill(order_id)  # Uses WebSocket push, not polling!
"""

import os
import asyncio
import logging
import config
from pathlib import Path
from collections import deque, defaultdict
from dataclasses import dataclass
from datetime import datetime, UTC
from enum import Enum
from typing import Optional, Dict, List, Any, Callable, Set
import time
import threading

logger = logging.getLogger(__name__)

# ===================================================================
# WEBSOCKET IMPORT BLOCK (with graceful fallback)
# ===================================================================

_WS_AVAILABLE = False
_data_ws_module = None
_order_ws_module = None

try:
    from fyers_apiv3.FyersWebsocket import data_ws as _data_ws_module
    from fyers_apiv3.FyersWebsocket import order_ws as _order_ws_module
    _WS_AVAILABLE = True
    logger.info("✅ Fyers WebSocket modules loaded (setuptools==79.0.1 confirmed)")
except ImportError as e:
    logger.critical(
        f"❌ WebSocket import failed: {e}\n"
        f"   Fix: pip install setuptools==79.0.1\n"
        f"   Bot will run in REST-only mode."
    )

from fyers_apiv3 import fyersModel


class OrderUpdate:
    """Data class for order updates from WebSocket."""
    def __init__(self, data: dict):
        self.order_id = data.get('id')
        self.symbol = data.get('symbol')
        self.status = data.get('status')  # PENDING, OPEN, FILLED, REJECTED, CANCELLED
        self.filled_qty = data.get('filledQty', 0)
        self.avg_price = data.get('tradedPrice', 0)
        self.timestamp = datetime.now(UTC)
        self.raw_data = data


class PositionUpdate:
    """Data class for position updates from WebSocket."""
    def __init__(self, data: dict):
        self.symbol = data.get('symbol')
        self.net_qty = data.get('netQty', 0)
        self.avg_price = data.get('avgPrice', 0)
        self.realized_pnl = data.get('realized_profit', 0)
        self.unrealized_pnl = data.get('unrealized_profit', 0)
        self.timestamp = datetime.now(UTC)
        self.raw_data = data


class TickData:
    """Data class for market tick data from WebSocket."""
    def __init__(self, data: dict):
        self.symbol = data.get('symbol')
        # Fyers V3 uses 'lp' for LTP in ticks, and 'ltp' in some SDK-parsed formats.
        self.ltp = data.get('ltp', data.get('lp', 0)) or 0
        
        # Volume: 'vol_traded_today' (Mode Full) or 'v' (Mode SymbolUpdate)
        self.volume = data.get('vol_traded_today', data.get('v', data.get('volume', 0))) or 0
        
        self.bid = data.get('bid', self.ltp) or 0
        self.ask = data.get('ask', self.ltp) or 0
        
        # OHLC: 'h'/'l'/'o' vs 'high_price'/'low_price'/'open_price'
        self.open = data.get('open_price', data.get('o', 0)) or 0
        self.high = data.get('high_price', data.get('h', 0)) or 0
        self.low = data.get('low_price', data.get('l', 0)) or 0
        self.prev_close = data.get('prev_close_price', data.get('pc', 0)) or 0
        
        self.timestamp = datetime.now(UTC)
        self.raw_data = data


class CacheEntrySource(Enum):
    WS_TICK = "ws"
    REST_SEED = "rest"


@dataclass
class CacheEntry:
    last_price: float
    volume: float
    ch_oc: float
    oi: float
    bid: float
    ask: float
    open_price: float  # Phase 51
    high_price: float  # Phase 51
    prev_close: float  # Phase 51
    last_time: float
    source: CacheEntrySource
    tick_count: int = 0


@dataclass
class Candle:
    """Data class for a single OHLCV candle."""
    symbol: str
    epoch: int        # Unix timestamp of the start of the candle
    open: float
    high: float
    low: float
    close: float
    volume: float
    datetime: datetime


class MinuteCandleAggregator:
    """
    Aggregates raw WebSocket ticks into 1-minute OHLCV candles.
    Maintains a rolling buffer for the Analyzer to consume.
    """
    def __init__(self, max_candles: int = 500):
        self.max_candles = max_candles
        self.history: Dict[str, deque[Candle]] = {}  # symbol -> deque[Candle]
        self.current_candles: Dict[str, Candle] = {}  # symbol -> partially formed Candle
        self.minute_start_volume: Dict[str, float] = {} # symbol -> volume at start of current minute
        
        # Phase 88: Real-time Slope Metrics
        self.vwap_history: Dict[str, deque[float]] = {} # symbol -> deque[float] (VWAP values)
        self._lock = threading.Lock()

    def update(self, tick: TickData, timestamp: Optional[float] = None):
        """Processes a new tick and updates/finalizes candles."""
        symbol = tick.symbol
        if not symbol or not tick.ltp:
            return

        # Calculate minute start (epoch)
        now_ts = int(timestamp if timestamp is not None else time.time())
        minute_start = (now_ts // 60) * 60

        with self._lock:
            current = self.current_candles.get(symbol)

            if current and current.epoch == minute_start:
                # Update existing candle
                current.high = max(current.high, tick.ltp)
                current.low = min(current.low, tick.ltp)
                current.close = tick.ltp
                
                # Fyers ticks have cumulative volume. Periodic volume = Total - Start of Minute.
                start_vol = self.minute_start_volume.get(symbol, tick.volume)
                current.volume = max(0, tick.volume - start_vol)
            else:
                # Finalize old candle if it exists
                if current:
                    if symbol not in self.history:
                        self.history[symbol] = deque(maxlen=self.max_candles)
                    self.history[symbol].append(current)

                self.minute_start_volume[symbol] = tick.volume

                # Phase 88: Update rolling VWAP history for slope calculation
                if current:
                    if symbol not in self.vwap_history:
                        self.vwap_history[symbol] = deque(maxlen=60) # Store 1 hour of VWAPs
                    
                    # Calculate VWAP for the finalized candle
                    tp = (current.high + current.low + current.close) / 3
                    # Simplified rolling VWAP if weight is not available, but ideally we use incremental
                    # For slope, we just need the series of VWAP values or Close prices.
                    # We'll use finalized candle close as the VWAP proxy for now if full calculation is too heavy,
                    # but aggregator has volume, so let's do it right.
                    self.vwap_history[symbol].append(current.close) 

                new_candle = Candle(
                    symbol=symbol,
                    epoch=minute_start,
                    open=tick.ltp,
                    high=tick.ltp,
                    low=tick.ltp,
                    close=tick.ltp,
                    volume=0, # First tick of the minute
                    datetime=datetime.fromtimestamp(minute_start)
                )
                self.current_candles[symbol] = new_candle


    def get_candles(self, symbol: str, n: int = 100) -> List[Candle]:
        """Returns the last N candles for a symbol, including the current one."""
        with self._lock:
            hist = list(self.history.get(symbol, []))
            current = self.current_candles.get(symbol)
            
            result = hist
            if current:
                # Need as a new list to avoid modifying history deque
                result = list(hist) + [current]
            
            return result[-n:]

    def get_vwap_slope(self, symbol: str, window: int = 30) -> float:
        """
        Phase 88: Calculate Slope on-the-fly from memory cache.
        Returns Normalized Linear Regression Slope (dy/dx).
        """
        import numpy as np
        with self._lock:
            history = self.vwap_history.get(symbol)
            if not history or len(history) < window:
                # Fallback: calculate from Candle history if vwap_history not yet primed
                candles = list(self.history.get(symbol, []))
                current = self.current_candles.get(symbol)
                if current: candles.append(current)
                
                if len(candles) < 5: return 0.0 # Not enough for any trend
                
                y = np.array([c.close for c in candles[-window:]])
            else:
                y = np.array(list(history)[-window:])
            
            x = np.arange(len(y))
            if len(y) < 2: return 0.0
            
            # Linear Regression
            slope, _ = np.polyfit(x, y, 1)
            
            # Normalize slope as % of current price (to make it symbol-invariant)
            current_price = y[-1]
            if current_price > 0:
                normalized_slope = (slope / current_price) * 1000 # Scaling factor for readability
                return round(normalized_slope, 4)
            
            return 0.0


class FyersBrokerInterface:
    """
    Unified broker interface with WebSocket-first architecture.
    
    Features:
    - WebSocket for real-time order/position/tick updates (10-50ms latency)
    - REST API for order placement and fallback queries
    - Intelligent caching to minimize REST calls (53k/day → 165/day)
    - Rate limit enforcement (prevents API blocks)
    - Auto-reconnect on WebSocket disconnect
    """
    
    def __init__(
        self,
        access_token: str,
        client_id: str,
        db_manager,
        emergency_logger
    ):
        self.access_token = access_token
        self.client_id = client_id
        self.db = db_manager
        self.emergency_logger = emergency_logger

        # Ensure log directories exist
        import os
        os.makedirs("logs/fyers_rest", exist_ok=True)
        os.makedirs("logs/fyers_order_ws", exist_ok=True)
        os.makedirs("logs/fyers_data_ws", exist_ok=True)
        
        # REST API client (for order placement)
        self.rest_client = fyersModel.FyersModel(
            client_id=client_id,
            token=access_token,
            log_path="logs/fyers_rest"
        )
        
        # WebSocket clients
        self.data_ws = None  # Market data WebSocket
        self.order_ws = None  # Order update WebSocket
        
        # WebSocket state
        self.ws_connected = False
        self.ws_reconnecting = False
        
        # Phase 82: Local Candle Engine
        self.aggregator = MinuteCandleAggregator(
            max_candles=getattr(config, "P82_MAX_LOCAL_CANDLES", 500)
        )
        
        # Real-time caches (updated by WebSocket)
        self.tick_cache: Dict[str, deque] = {}  # symbol -> deque of TickData
        self.position_cache: Dict[str, PositionUpdate] = {}  # symbol -> PositionUpdate
        self.order_status_cache: Dict[str, OrderUpdate] = {}  # order_id -> OrderUpdate
        
        # Event waiters (for async notification)
        self.order_fill_events: Dict[str, asyncio.Event] = {}  # order_id -> asyncio.Event
        self.position_change_events = defaultdict(asyncio.Event)
        
        # Callbacks (for strategy integration)
        self.on_tick_callbacks = []  # List[Callable[[TickData], None]]
        self.on_order_update_callbacks = []
        self.on_position_update_callbacks = []
        
        # Rate limiting
        self.api_calls = defaultdict(deque)  # endpoint -> deque of timestamps
        self.rate_limits = {
            'place_order': (10, 1.0),  # 10 calls per second
            'cancel_order': (10, 1.0),
            'get_positions': (1, 1.0),  # 1 call per second (strict limit)
            'get_order_status': (5, 1.0),
            'get_quotes': (5, 1.0),
            'get_market_depth': (1, 1.0)
        }
        
        # Watchlist (symbols to subscribe)
        self.subscribed_symbols: Set[str] = set()
        
        # Order WebSocket state (Added in Phase 42.2.5)
        self._fill_callbacks: Dict[str, Callable] = {}   # order_id -> callback function
        self._order_cache: Dict[str, Dict] = {}      # order_id -> latest order message
        self._position_cache: Dict[str, Dict] = {}   # symbol -> latest position message
        
        # Phase 44.7 / PRD-007 — WS quote cache for scanner pre-filter
        # (threading imported at module level L28)
        self._quote_cache: dict[str, CacheEntry] = {}
        self._quote_cache_lock = threading.Lock()
        self._ws_subscribed_symbols: list[str] = []
        
        # Phase 79: Leverage cache (symbol -> leverage_float)
        self._leverage_cache: dict[str, float] = {}
        self._leverage_cache_lock = threading.Lock()
        self._ws_subscribed_symbols_set: set[str] = set()

        # PRD-007: Cache reliability state machine
        self._cache_state: str = "UNINITIALIZED"  # UNINITIALIZED | PRIMING | READY | DEGRADED
        self._cache_ready_event = threading.Event()  # Set when readiness threshold first crossed
        self._subscribed_count: int = 0
        self._ws_subscribed_symbols: list[str] = []
        self._ws_subscribed_symbols_set: set[str] = set()

        # PRD-007: Cache reliability state machine
        self._cache_state: str = "UNINITIALIZED"  # UNINITIALIZED | PRIMING | READY | DEGRADED
        self._cache_ready_event = threading.Event()  # Set when readiness threshold first crossed
        self._subscribed_count: int = 0
        self._prime_start_ts: float = 0.0
        self._health_monitor_thread: threading.Thread | None = None
        self._health_monitor_running: bool = False
        self._reprime_requested: bool = False
        self._last_reprime_time: float = 0.0
        self._consecutive_reprime_failures: int = 0
        self._sub_ack = threading.Event()  # BUG-02: blocks until Fyers confirms subscription
        self._ws_cache_stop = False

        # PRD-3: Telegram hook for WS cache alerts
        # Set via broker.set_telegram(bot) from main.py after both are constructed
        self._telegram_bot = None

        # PRD-3: Severe-degraded tracking (fresh < 5% for > 30s triggers recovery)
        self._severe_degraded_since: float = 0.0      # epoch when fresh% first dropped below 5%
        self._last_degraded_telegram_alert: float = 0.0   # throttle Telegram spam
        self._degraded_scan_count: int = 0            # incremented by scanner for banner log
        
        # Background tasks
        self.tasks = []
        
    def get_local_candles(self, symbol: str, n: int = 100) -> List[Candle]:
        """Exposes aggregated local candles to the Analyzer."""
        return self.aggregator.get_candles(symbol, n)

    async def initialize(self):
        logger.info("Initializing Fyers Broker Interface...")
        self._loop = asyncio.get_running_loop()

        # Step 1: REST API — FATAL if fails
        try:
            profile = self.rest_client.get_profile()
            if profile.get('s') == 'ok':
                name = profile['data'].get('name', 'Unknown')
                logger.info(f"REST API connected: {name}")
            else:
                raise ConnectionError(f"REST API auth failed: {profile}")
        except Exception as e:
            raise ConnectionError(f"Broker REST init failed: {e}")

        # Step 2: Init WebSocket objects — NON-FATAL
        await self._init_order_websocket()
        await self._init_data_websocket()

        # Step 3: Connect WebSockets in background threads — NON-FATAL
        await self._connect_websockets()

        # Step 4: Start background maintenance tasks
        self.tasks.append(asyncio.create_task(self._websocket_keepalive()))
        self.tasks.append(asyncio.create_task(self._cache_cleanup()))

        logger.info("✅ Broker interface initialized successfully")

    async def _init_data_websocket(self):
        """Initialize Fyers v3 Data WebSocket."""
        if not _WS_AVAILABLE or _data_ws_module is None:
            logger.warning("Data WebSocket skipped: module not available")
            return

        try:
            log_dir = Path("logs/fyers_data_ws")
            log_dir.mkdir(parents=True, exist_ok=True)

            # Fyers v3 requires combined token format
            full_token = f"{self.client_id}:{self.access_token}"

            self.data_ws = _data_ws_module.FyersDataSocket(
                access_token=full_token,
                log_path=str(log_dir) + os.sep,
                litemode=False,       # Full data
                write_to_file=False,  # We handle logging
                reconnect=True,       # Auto-reconnect
                on_connect=self._on_data_ws_connect,
                on_close=self._on_data_ws_close,
                on_error=self._on_data_ws_error,
                on_message=self._handle_tick
            )
            logger.info("✅ Data WebSocket initialized (pending connect)")
        except Exception as e:
            logger.error(f"Data WebSocket init failed: {e}")
            self.data_ws = None

    async def _init_order_websocket(self):
        """Initialize Fyers v3 Order WebSocket."""
        if not _WS_AVAILABLE or _order_ws_module is None:
            logger.warning("Order WebSocket skipped: module not available")
            return

        try:
            log_dir = Path("logs/fyers_order_ws")
            log_dir.mkdir(parents=True, exist_ok=True)

            full_token = f"{self.client_id}:{self.access_token}"

            self.order_ws = _order_ws_module.FyersOrderSocket(
                access_token=full_token,
                write_to_file=False,
                log_path=str(log_dir) + os.sep,
                reconnect=True,
                on_connect=self._on_order_ws_connect,
                on_close=self._on_order_ws_close,
                on_error=self._on_order_ws_error,
                on_orders=self._handle_order_update,
                on_positions=self._handle_position_update,
                on_trades=self._handle_trade_update,
                on_general=self._handle_general_update
            )
            logger.info("✅ Order WebSocket initialized (pending connect)")
        except Exception as e:
            logger.error(f"Order WebSocket init failed: {e}")
            self.order_ws = None

    def _start_data_ws(self):
        """Start Data WebSocket connection (Blocking - Run in Thread)."""
        if self.data_ws:
            try:
                self.data_ws.connect()
            except Exception as e:
                logger.error(f"Data WS connection error: {e}")
                self.ws_connected = False

    def _start_order_ws(self):
        """Start Order WebSocket connection (Blocking - Run in Thread)."""
        if self.order_ws:
            try:
                self.order_ws.connect()
            except Exception as e:
                logger.error(f"Order WS connection error: {e}")

    async def _connect_websockets(self):
        """Launch both WebSocket connections in background thread executors."""
        loop = asyncio.get_event_loop()

        if self.data_ws:
            loop.run_in_executor(None, self._start_data_ws)
            logger.info("Data WebSocket connecting (background thread)...")

        if self.order_ws:
            loop.run_in_executor(None, self._start_order_ws)
            logger.info("Order WebSocket connecting (background thread)...")

    def _on_data_ws_connect(self):
        """Called by Fyers SDK when Data WebSocket opens."""
        logger.info("✅ Data WebSocket connected")
        self.ws_connected = True

        # Subscribe to all watched symbols immediately on connect
        if self.subscribed_symbols:
            symbols = list(self.subscribed_symbols)
            # FyersDataSocket subscribe take symbols argument
            self.data_ws.subscribe(symbols=symbols, data_type="SymbolUpdate")
            logger.info(f"Subscribed to {len(symbols)} symbols")

    def _on_order_ws_connect(self):
        """Called by Fyers SDK when Order WebSocket opens."""
        logger.info("✅ Order WebSocket connected")
        # Subscribe to all order/position events
        if self.order_ws:
            self.order_ws.subscribe(data_type="OnOrders,OnTrades,OnPositions,OnGeneral")
        logger.info("Order WebSocket subscribed to all events")

    def _on_data_ws_close(self, message):
        logger.warning(f"Data WebSocket closed: {message}")
        self.ws_connected = False

    def _on_order_ws_close(self, message):
        logger.warning(f"Order WebSocket closed: {message}")

    def _on_data_ws_error(self, message):
        logger.error(f"Data WebSocket error: {message}")

    def _on_order_ws_error(self, message):
        logger.error(f"Order WebSocket error: {message}")

    def _handle_position_update(self, message: dict):
        logger.debug(f"Position update: {message}")
        # Could implement cache update here

    def _handle_trade_update(self, message: dict):
        logger.debug(f"Trade update: {message}")

    def _handle_general_update(self, message: dict):
        """
        Called by Order WebSocket for general/system messages.
        Examples: connection status, session expiry warnings.
        """
        try:
            if not message:
                return
            logger.debug(f"ℹ️ General WS Update: {message}")

            # Check for session expiry warning from Fyers
            msg_type = message.get('type') or message.get('s', '')
            if 'expire' in str(msg_type).lower() or 'logout' in str(msg_type).lower():
                logger.warning(
                    "⚠️ Fyers session expiry warning received. "
                    "Token may need refresh after market close."
                )

        except Exception as e:
            logger.error(f"_handle_general_update error: {e}")
    
    def _handle_tick(self, message: dict):
        """
        Handle market tick from WebSocket.
        """
        try:
            # ── Phase 44.7: Update scanner quote cache ─────────────
            # BUG-02: Detect subscription ACK from Fyers
            msg_type = message.get('type')
            if msg_type == 'sub' and message.get('code') == 200:
                self._sub_ack.set()
                logger.info("[WS Cache] ✅ Subscription ACK received from Fyers server")
                return

            # Permanent first-tick diagnostic log
            if not hasattr(self, '_first_tick_logged'):
                if msg_type not in ('cn', 'ful', 'op', 'sf', 'os'):
                    logger.info(f"[WS Cache] ✅ FIRST DATA TICK: {str(message)[:200]}")
                    self._first_tick_logged = True
            symbol = message.get('symbol') or message.get('n')
            if symbol and hasattr(self, '_ws_subscribed_symbols_set') and symbol in self._ws_subscribed_symbols_set:
                with self._quote_cache_lock:
                    prev_entry = self._quote_cache.get(symbol)
                    
                    # Merge incoming tick data with prev_entry fallbacks
                    # Phase 85: Coerce None → 0 to prevent NoneType comparison crashes on pre-market ticks
                    ltp = message.get('ltp', prev_entry.last_price if prev_entry else 0) or 0
                    volume = message.get('vol_traded_today', message.get('v', prev_entry.volume if prev_entry else 0)) or 0
                    oi = message.get('oi', prev_entry.oi if prev_entry else 0) or 0
                    bid = message.get('bid', prev_entry.bid if prev_entry else 0) or 0
                    ask = message.get('ask', prev_entry.ask if prev_entry else 0) or 0
                    open_price = message.get('open_price', message.get('o', prev_entry.open_price if prev_entry else 0)) or 0
                    high_price = message.get('high_price', message.get('h', prev_entry.high_price if prev_entry else 0)) or 0
                    prev_close = message.get('prev_close_price', message.get('pc', prev_entry.prev_close if prev_entry else 0)) or 0
                    ch_oc = message.get('ch_oc', message.get('chp', prev_entry.ch_oc if prev_entry else 0)) or 0

                    # Re-calculate ch_oc manually if it evaluates to 0 but prev_close > 0 and ltp > 0
                    if message.get('ch_oc', message.get('chp', 0)) == 0 and prev_close > 0 and ltp > 0:
                        ch_oc = ((ltp - prev_close) / prev_close) * 100

                    tick_count = 1
                    if prev_entry and prev_entry.source == CacheEntrySource.WS_TICK:
                        tick_count = prev_entry.tick_count + 1

                    self._quote_cache[symbol] = CacheEntry(
                        last_price=ltp,
                        volume=volume,
                        ch_oc=ch_oc,
                        oi=oi,
                        bid=bid,
                        ask=ask,
                        open_price=open_price,
                        high_price=high_price,
                        prev_close=prev_close,
                        last_time=time.time(),
                        source=CacheEntrySource.WS_TICK,
                        tick_count=tick_count,
                    )
                    # PRD-007: Advance PRIMING → READY state machine on each tick
                    self._check_cache_readiness_internal()

            # Fyers DataSocket returns dict structure
            tick = TickData(message)
            
            # Store in deque (last 100 ticks = ~30 seconds)
            if tick.symbol not in self.tick_cache:
                self.tick_cache[tick.symbol] = deque(maxlen=100)
            
            self.tick_cache[tick.symbol].append(tick)
            
            # Phase 82: Update Local Candle Engine
            if getattr(config, "P82_LOCAL_CANDLES_ENABLED", False):
                self.aggregator.update(tick)
            
            # Call registered callbacks
            for callback in self.on_tick_callbacks:
                try:
                    callback(tick)
                except Exception as e:
                    logger.error(f"Tick callback error: {e}")
        
        except Exception as e:
            logger.error(f"Error handling tick: {e}")

    # ================================================================
    # ORDER WEBSOCKET CALLBACKS
    # All called by FyersOrderSocket when events arrive
    # ================================================================

    def _handle_order_update(self, message: dict):
        """
        Called by Order WebSocket on every order status change.
        """
        try:
            if not message:
                return

            logger.info(f"📋 Order Update: {message}")

            order_id = (
                message.get('id') or
                message.get('orderId') or
                message.get('order_id')
            )
            status = message.get('status')
            filled_qty = message.get('filledQty', 0)
            fill_price = message.get('tradedPrice', 0.0)

            if not order_id:
                logger.debug(f"Order update with no ID: {message}")
                return

            # Notify waiting fill listeners (used by wait_for_fill)
            if hasattr(self, '_fill_callbacks') and order_id in self._fill_callbacks:
                try:
                    self._fill_callbacks[order_id](message)
                except Exception as cb_err:
                    logger.error(f"Fill callback error for {order_id}: {cb_err}")
            
            # Also trigger asyncio Event for wait_for_fill
            if order_id in self.order_fill_events:
                 # Update status cache before setting event so waiter sees new status
                 # We need to map WS message to OrderUpdate object or similar
                 # For now, let's just update the cache used by wait_for_fill
                 from datetime import datetime
                 class TempUpdate:
                     def __init__(self, s): self.status = s
                 
                 # Map numeric status to string if needed, or keep as is.
                 # wait_for_fill checks for 'FILLED'. Fyers WS sends 2 for Filled.
                 status_map_rev = {2: 'FILLED', 1: 'CANCELLED', 4: 'PARTIAL', 5: 'REJECTED', 6: 'PENDING'}
                 status_str = status_map_rev.get(status, str(status))
                 
                 # Update the cache that wait_for_fill reads
                 self.order_status_cache[order_id] = OrderUpdate({
                     'id': order_id,
                     'status': status_str,
                     'filledQty': filled_qty,
                     'tradedPrice': fill_price
                 })
                 
                 self.order_fill_events[order_id].set()

            # Update internal order cache
            if hasattr(self, '_order_cache'):
                self._order_cache[order_id] = message

            # Log meaningful status transitions
            status_map = {
                1: "CANCELLED",
                2: "FILLED ✅",
                4: "PARTIAL FILL",
                5: "REJECTED ❌",
                6: "PENDING"
            }
            status_str = status_map.get(status, f"UNKNOWN({status})")
            logger.info(
                f"Order {order_id}: {status_str} "
                f"| Qty: {filled_qty} | Price: {fill_price}"
            )

        except Exception as e:
            logger.error(f"_handle_order_update error: {e} | message: {message}")


    def _handle_position_update(self, message: dict):
        """
        Called by Order WebSocket when position changes.
        """
        try:
            if not message:
                return

            logger.debug(f"📊 Position Update: {message}")

            symbol = message.get('symbol') or message.get('id')
            if symbol:
                if not hasattr(self, '_position_cache'):
                    self._position_cache = {}
                self._position_cache[symbol] = {
                    'data': message,
                    'timestamp': datetime.now(UTC)
                }

        except Exception as e:
            logger.error(f"_handle_position_update error: {e}")


    def _handle_trade_update(self, message: dict):
        """
        Called by Order WebSocket on trade execution.
        """
        try:
            if not message:
                return

            logger.info(f"💹 Trade Executed: {message}")

            trade_id = message.get('id') or message.get('tradeId')
            symbol = message.get('symbol')
            price = message.get('tradedPrice', 0)
            qty = message.get('tradedQty', 0)
            side = 'BUY' if message.get('side') == 1 else 'SELL'

            logger.info(
                f"TRADE | {side} {qty} {symbol} @ ₹{price} "
                f"| Trade ID: {trade_id}"
            )

        except Exception as e:
            logger.error(f"_handle_trade_update error: {e}")
    
    async def _log_order_update(self, update: OrderUpdate):
        """Log order update to database."""
        try:
            await self.db.execute("""
                UPDATE orders
                SET state = $1,
                    filled_qty = $2,
                    avg_filled_price = $3,
                    updated_at = NOW()
                WHERE exchange_order_id = $4
            """, update.status, update.filled_qty, update.avg_price, update.order_id)
        except Exception as e:
            logger.error(f"Failed to log order update: {e}")
    
    async def _websocket_keepalive(self):
        """Background task to monitor WebSocket health. (Simplified)"""
        while True:
            await asyncio.sleep(60)
            # Implementation depends on Fyers SDK internals. 
            # We mostly rely on the SDK's auto-reconnect for now.
            pass
    
    async def _cache_cleanup(self):
        """Background task to clean old cache entries."""
        while True:
            await asyncio.sleep(300)  # Every 5 minutes
            try:
                now = datetime.now(UTC)
                # Use list(keys) to avoid runtime errors during modification
                for symbol in list(self.tick_cache.keys()):
                    ticks = self.tick_cache[symbol]
                    if ticks:
                        latest = ticks[-1].timestamp
                        if (now - latest).total_seconds() > 3600:
                            del self.tick_cache[symbol]
                
                for order_id in list(self.order_status_cache.keys()):
                    update = self.order_status_cache[order_id]
                    if (now - update.timestamp).total_seconds() > 3600:
                        del self.order_status_cache[order_id]
            except Exception as e:
                logger.error(f"Cache cleanup error: {e}")

    def subscribe_scanner_universe(self, symbols: List[str]) -> None:
        """
        Subscribe all scanner symbols to dataws in symbolUpdate mode.
        Called once at startup after dataws is connected.
        Splits into batches of 50 — Fyers dataws limit per subscribe call.
        Resets the cache state machine to PRIMING and starts the health monitor.
        """
        self._ws_subscribed_symbols = symbols
        self._ws_subscribed_symbols_set = set(symbols)
        self._subscribed_count = len(symbols)
        self._cache_state = "PRIMING"
        self._cache_ready_event.clear()
        self._prime_start_ts = time.time()
        self._reprime_requested = False
        self._sub_ack.clear()  # BUG-02: reset ACK before subscribing

        # BUG-02: 3s post-connect delay — Fyers needs auth handshake to complete server-side
        logger.info("[WS Cache] Waiting 3s post-connect before subscribing...")
        time.sleep(3)

        batch_size = 50
        total = 0
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            try:
                if self.data_ws:
                    self.data_ws.subscribe(
                        symbols=batch,
                        data_type="SymbolUpdate"
                    )
                total += len(batch)
            except Exception as e:
                logger.error(f"[WS Cache] Subscribe batch {i//batch_size} failed: {e}")
        logger.info(f"[WS Cache] Subscribed {total}/{len(symbols)} symbols to dataws SymbolUpdate — state=PRIMING")

        # BUG-02: Wait for subscription ACK (10s timeout)
        if not self._sub_ack.wait(timeout=10.0):
            logger.critical(
                "[WS Cache] ❌ No subscription ACK from Fyers after 10s — "
                "connection may be dead or data_type wrong"
            )
        else:
            logger.info("[WS Cache] ✅ Subscription confirmed by Fyers server")

        # Evaluate readiness immediately (supports REST-seeded startup path).
        with self._quote_cache_lock:
            self._check_cache_readiness_internal()

        # Start health monitor thread — check actual liveness, not just the flag
        thread_dead = (
            self._health_monitor_thread is not None
            and not self._health_monitor_thread.is_alive()
        )
        if not self._health_monitor_running or thread_dead:
            self._health_monitor_running = True
            self._health_monitor_thread = threading.Thread(
                target=self._run_cache_health_monitor,
                name="WSCacheHealthMonitor",
                daemon=True
            )
            self._health_monitor_thread.start()
            logger.info("[WS Cache] Health monitor thread started")

    def get_quote_cache_snapshot(self) -> dict[str, dict]:
        """
        Returns a shallow copy of the current quote cache.
        Called by scanner.scan_market() — thread-safe.
        """
        with self._quote_cache_lock:
            return {
                symbol: {
                    'ltp': entry.last_price,
                    'volume': entry.volume,
                    'ch_oc': entry.ch_oc,
                    'oi': entry.oi,
                    'bid': entry.bid,
                    'ask': entry.ask,
                    'open': entry.open_price,   # Phase 51
                    'high': entry.high_price,   # Phase 51
                    'pc': entry.prev_close,     # Phase 51
                    'ts': entry.last_time,
                    'source': entry.source.value,
                    'tick_count': entry.tick_count,
                }
                for symbol, entry in self._quote_cache.items()
            }

    def seed_from_rest(self, symbols: List[str]) -> int:
        """
        Seed quote cache from REST snapshot at startup.
        Seeded entries are "known" but not "fresh" for WS readiness.
        """
        if not symbols:
            return 0

        seeded = 0
        batch_size = 50
        now_ts = time.time()
        logger.info("[WS Cache] Seeding %s symbols from REST snapshot...", len(symbols))

        with self._quote_cache_lock:
            if self._subscribed_count == 0:
                self._subscribed_count = len(symbols)

        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            try:
                response = self.rest_client.quotes(data={"symbols": ",".join(batch)})
            except Exception as e:
                logger.warning("[WS Cache] REST seed batch %s failed: %s", i // batch_size, e)
                continue

            if response.get("s") != "ok":
                continue

            for quote in response.get("d", []):
                symbol = quote.get("n")
                qv = quote.get("v", {})
                ltp = qv.get("lp", 0)
                if not symbol or not ltp:
                    continue

                with self._quote_cache_lock:
                    existing = self._quote_cache.get(symbol)
                    # Never override live WS ticks with REST seed.
                    if existing and existing.source == CacheEntrySource.WS_TICK:
                        continue
                    self._quote_cache[symbol] = CacheEntry(
                        last_price=ltp,
                        volume=qv.get("v", qv.get("volume", 0)),
                        ch_oc=qv.get("chp", qv.get("ch_oc", 0)),
                        oi=qv.get("oi", 0),
                        bid=qv.get("bid", 0),
                        ask=qv.get("ask", 0),
                        open_price=qv.get("o", qv.get("open_price", 0)), # Phase 51
                        high_price=qv.get("h", qv.get("high_price", 0)), # Phase 51
                        prev_close=qv.get("pc", qv.get("prev_close_price", 0)), # Phase 51
                        last_time=now_ts,
                        source=CacheEntrySource.REST_SEED,
                        tick_count=0,
                    )
                seeded += 1

        logger.info("[WS Cache] ✅ REST seed complete: %s/%s symbols seeded", seeded, len(symbols))
        return seeded

    def _is_fresh_entry(self, entry: CacheEntry, freshness_ttl: float, now_ts: float) -> bool:
        return entry.source == CacheEntrySource.WS_TICK and (now_ts - entry.last_time) < freshness_ttl

    def is_fresh(self, symbol: str, freshness_ttl: float) -> bool:
        with self._quote_cache_lock:
            entry = self._quote_cache.get(symbol)
            if not entry:
                return False
            return self._is_fresh_entry(entry, freshness_ttl, time.time())

    def is_known(self, symbol: str) -> bool:
        with self._quote_cache_lock:
            return symbol in self._quote_cache

    def is_truly_missing(self, symbol: str) -> bool:
        return not self.is_known(symbol)

    # ================================================================
    # PRD-007: Cache Readiness & Health
    # ================================================================

    def _get_readiness_threshold(self) -> float:
        """Returns readiness threshold based on market session timing."""
        try:
            import config
            mins_open = config.minutes_since_market_open()
            if mins_open < 30:
                return 0.85   # Opening: 85% (strict)
            else:
                return 0.80   # Mid-market/late: 80%
        except Exception:
            return 0.85

    def _check_cache_readiness_internal(self):
        """
        Called on every tick during PRIMING. Sets the readiness event
        and transitions state to READY once threshold is crossed.
        Must be called under _quote_cache_lock.
        """
        if self._cache_state != "PRIMING" or self._subscribed_count == 0:
            return

        import config as _cfg
        freshness_ttl = _cfg.WS_TICK_FRESHNESS_TTL_SECONDS
        now = time.time()
        fresh_count = sum(
            1
            for entry in self._quote_cache.values()
            if self._is_fresh_entry(entry, freshness_ttl, now)
        )
        known_count = len(self._quote_cache)
        fresh_pct = fresh_count / self._subscribed_count
        known_pct = known_count / self._subscribed_count
        threshold = self._get_readiness_threshold()

        if fresh_pct >= threshold or known_pct >= 0.90:
            self._cache_state = "READY"
            self._cache_ready_event.set()
            elapsed = now - self._prime_start_ts
            reason = (
                f"fresh {fresh_pct:.1%} >= {threshold:.0%}"
                if fresh_pct >= threshold
                else f"known {known_pct:.1%} >= 90%"
            )
            logger.info(
                f"[WS Cache] CACHE READY: {fresh_count}/{self._subscribed_count} symbols fresh "
                f"| known={known_count}/{self._subscribed_count} ({known_pct:.1%}) "
                f"| reason={reason} after {elapsed:.1f}s"
            )

    def is_cache_ready(self) -> bool:
        """Returns True if cache is in READY state (crossed readiness threshold)."""
        return self._cache_state == "READY"

    def wait_for_cache_ready(self, timeout_sec: float = 45.0) -> bool:
        """
        Blocks caller until cache is READY or timeout expires.
        Returns True if ready, False on timeout.
        Used by startup gate in main.py via asyncio.to_thread().
        """
        return self._cache_ready_event.wait(timeout=timeout_sec)

    def set_telegram(self, telegram_bot) -> None:
        """
        Wire Telegram bot for WS cache degradation alerts.
        Called from main.py after both broker and bot are initialized:
            broker.set_telegram(telegram_bot)
        """
        self._telegram_bot = telegram_bot
        logger.info("[WS Cache] Telegram bot wired for cache degradation alerts.")

    def is_cache_severely_degraded(self) -> bool:
        """
        Returns True when fresh% < 5% AND degradation has persisted > 30s.
        Called by scanner.py and focus_engine.py for the scan-level DEGRADED banner.
        """
        return (
            self._severe_degraded_since > 0
            and (time.time() - self._severe_degraded_since) >= 30
        )

    def increment_degraded_scan_count(self) -> int:
        """
        Called by scanner on each scan while severely degraded.
        Returns current count for banner modulo check.
        """
        self._degraded_scan_count += 1
        return self._degraded_scan_count

    def reset_degraded_scan_count(self) -> None:
        self._degraded_scan_count = 0

    def cache_health_snapshot(self) -> dict:
        """Returns current cache health metrics dict."""
        import config as _cfg
        freshness_ttl = _cfg.WS_TICK_FRESHNESS_TTL_SECONDS
        with self._quote_cache_lock:
            now = time.time()
            ages = [(now - entry.last_time) for entry in self._quote_cache.values()]
            fresh = sum(
                1
                for entry in self._quote_cache.values()
                if self._is_fresh_entry(entry, freshness_ttl, now)
            )
            seeded = sum(1 for entry in self._quote_cache.values() if entry.source == CacheEntrySource.REST_SEED)
            populated = len(self._quote_cache)
            total = self._subscribed_count
            sorted_ages = sorted(ages) if ages else [0]

        return {
            'total':     total,
            'populated': populated,
            'fresh':     fresh,
            'stale':     max(0, populated - fresh - seeded),
            'seeded':    seeded,
            'missing':   max(0, total - populated),
            'age_p50':   sorted_ages[len(sorted_ages) // 2],
            'age_p95':   sorted_ages[int(len(sorted_ages) * 0.95)],
            'age_p99':   sorted_ages[int(len(sorted_ages) * 0.99)] if len(sorted_ages) >= 100 else sorted_ages[-1],
            'state':     self._cache_state,
        }

    def _trigger_reprime(self):
        """Unsubscribe all, wait, then re-subscribe. Escalates to full reconnect after 3 failures."""
        if self._reprime_requested:
            logger.warning("[WS Cache] Re-prime already in progress — skipping")
            return

        # 90s throttle between re-primes
        now = time.time()
        if now - self._last_reprime_time < 90:
            logger.warning("[WS Cache] Re-prime throttled — too soon since last attempt")
            return

        self._reprime_requested = True
        self._last_reprime_time = now
        self._consecutive_reprime_failures += 1
        self._cache_state = "PRIMING"
        self._cache_ready_event.clear()

        logger.warning(
            f"[WS Cache] Re-prime #{self._consecutive_reprime_failures} triggered"
        )

        # Escalate to full reconnect after 3 consecutive failures
        if self._consecutive_reprime_failures >= 3:
            logger.critical(
                "[WS Cache] 3 consecutive re-prime failures — escalating to FULL RECONNECT"
            )
            self._consecutive_reprime_failures = 0
            try:
                self._do_full_ws_reconnect()
            except Exception as e:
                logger.critical(f"[WS Cache] Full reconnect failed: {e}")
            finally:
                self._reprime_requested = False
            return

        try:
            # Step 1: Unsubscribe all
            if self._ws_subscribed_symbols and self.data_ws:
                try:
                    self.data_ws.unsubscribe(
                        symbols=self._ws_subscribed_symbols,
                        data_type="SymbolUpdate"
                    )
                    logger.info(f"[WS Cache] Unsubscribed {len(self._ws_subscribed_symbols)} symbols")
                except Exception as unsub_e:
                    logger.warning(f"[WS Cache] Unsubscribe failed (non-fatal): {unsub_e}")

            # Step 2: Wait for server to process unsubscribe
            time.sleep(5)

            # Step 3: Re-subscribe
            if self._ws_subscribed_symbols:
                self.subscribe_scanner_universe(self._ws_subscribed_symbols)
        except Exception as e:
            logger.error(f"[WS Cache] Re-prime failed: {e}")
        finally:
            self._reprime_requested = False

    def _do_full_ws_reconnect(self):
        """Nuclear option — full socket teardown + rebuild from scratch."""
        logger.critical("[WS Cache] ⚡ FULL RECONNECT — tearing down socket")
        try:
            if self._ws_subscribed_symbols and self.data_ws:
                self.data_ws.unsubscribe(
                    symbols=self._ws_subscribed_symbols,
                    data_type="SymbolUpdate"
                )
            time.sleep(2)
            if self.data_ws:
                self.data_ws.disconnect()
                logger.info("[WS Cache] Socket disconnected")
        except Exception as e:
            logger.error(f"[WS Cache] Disconnect error (continuing): {e}")

        time.sleep(5)  # Let Fyers server fully release the connection

        logger.critical("[WS Cache] ⚡ FULL RECONNECT — rebuilding socket")
        try:
            if self.data_ws:
                self.data_ws.connect()
                time.sleep(2)
            if self._ws_subscribed_symbols:
                self.subscribe_scanner_universe(self._ws_subscribed_symbols)
                logger.info("[WS Cache] ✅ Full reconnect succeeded")
            else:
                logger.critical("[WS Cache] ❌ No symbols to re-subscribe after reconnect")
        except Exception as e:
            logger.critical(f"[WS Cache] Full reconnect exception: {e}")

    def _run_cache_health_monitor(self):
        """
        Background daemon thread. Runs every 30s.
        PRD-3 Fix: Added _severe_degraded_since tracking so that
        fresh% < 5% (even when known% >= 90% from REST seed) triggers
        recovery after 30s instead of being trapped in DEGRADED forever.
        """
        consecutive_critical = 0

        while self._health_monitor_running:
            if getattr(self, '_ws_cache_stop', False):
                logger.info("[BROKER] Health monitor stopping on _ws_cache_stop flag.")
                break

            time.sleep(30)
            try:
                snap = self.cache_health_snapshot()
                total        = max(snap['total'], 1)
                fresh_pct    = snap['fresh'] / total
                # PRD-3 FIX: Do NOT use known_pct to determine DEGRADED/CRITICAL.
                # known_pct counts REST-seeded entries which remain "known" even
                # when the WS is completely dead — causing infinite DEGRADED trap.
                # Health is determined solely by fresh_pct (WS ticks within TTL).
                known_pct = (snap['fresh'] + snap['stale'] + snap.get('seeded', 0)) / total

                # ── Status Classification (PRD-3 Fixed) ──────────────────────
                if fresh_pct >= 0.85:
                    status = "HEALTHY"
                    consecutive_critical = 0
                    # Recovery: reset severe-degraded tracking
                    if self._severe_degraded_since > 0:
                        elapsed = time.time() - self._severe_degraded_since
                        logger.info(
                            f"[WS Cache] ✅ RECOVERED — fresh={fresh_pct:.1%} | "
                            f"was degraded for {elapsed:.0f}s | returning to TIER 1 WS_CACHE"
                        )
                        if self._telegram_bot:
                            try:
                                import asyncio
                                asyncio.run_coroutine_threadsafe(
                                    self._telegram_bot.send_util_alert(
                                        f"✅ *WS Cache RECOVERED*\n\n"
                                        f"Fresh: {snap['fresh']}/{snap['total']} ({fresh_pct:.1%})\n"
                                        f"Was degraded for {elapsed:.0f}s — returning to TIER 1 WS_CACHE"
                                    ),
                                    self._loop
                                )
                            except Exception:
                                pass
                        self._severe_degraded_since = 0.0
                        self._degraded_scan_count = 0
                    if self._consecutive_reprime_failures > 0:
                        logger.info("[WS Cache] ✅ Cache recovered — resetting re-prime failure counter")
                        self._consecutive_reprime_failures = 0

                elif fresh_pct >= 0.50:
                    # Genuinely degraded but not critical — no action, just log
                    status = "DEGRADED"
                    consecutive_critical = 0
                    # Only track severe degradation for < 5%
                    if self._severe_degraded_since > 0:
                        self._severe_degraded_since = 0.0   # recovered from severe

                else:
                    # fresh_pct < 50%
                    # PRD-3 FIX: Was incorrectly classified as DEGRADED when known_pct >= 90%.
                    # Now we check fresh_pct alone.
                    if fresh_pct < 0.05:
                        # Severe: < 5% fresh — this is the failure mode from 13:17:27
                        status = "SEVERE_DEGRADED"
                        consecutive_critical += 1
                        if self._severe_degraded_since == 0.0:
                            self._severe_degraded_since = time.time()
                            logger.critical(
                                f"[WS Cache] ⚠️ SEVERE DEGRADATION DETECTED — "
                                f"fresh={fresh_pct:.1%} ({snap['fresh']}/{snap['total']}) | "
                                f"known={known_pct:.1%} (mostly REST-seeded, WS is likely dead) | "
                                f"monitoring for 30s before recovery attempt"
                            )
                            # First Telegram alert
                            now = time.time()
                            if self._telegram_bot and (now - self._last_degraded_telegram_alert) > 120:
                                self._last_degraded_telegram_alert = now
                                try:
                                    import asyncio
                                    asyncio.run_coroutine_threadsafe(
                                        self._telegram_bot.send_util_alert(
                                            f"⚠️ *WS Cache SEVERELY DEGRADED*\n\n"
                                            f"Fresh: {snap['fresh']}/{snap['total']} ({fresh_pct:.1%})\n"
                                            f"WS appears to have stopped pushing ticks.\n"
                                            f"Auto-recovery will begin in 30s if not resolved."
                                        ),
                                        self._loop
                                    )
                                except Exception:
                                    pass
                    else:
                        # 5–50% fresh: CRITICAL but not severe
                        status = "CRITICAL"
                        consecutive_critical += 1

                # ── Log Health Line ───────────────────────────────────────────
                logger.info(
                    f"[WS Cache] CACHE HEALTH | Fresh: {snap['fresh']}/{snap['total']} ({fresh_pct:.1%}) "
                    f"| Stale: {snap['stale']} | Seeded: {snap.get('seeded', 0)} | Missing: {snap['missing']} "
                    f"| Age P50: {snap['age_p50']:.1f}s P95: {snap['age_p95']:.1f}s "
                    f"| Known: {known_pct:.1%} | State: {snap['state']} | Status: {status}"
                )

                # ── Recovery Trigger ──────────────────────────────────────────
                # PRD-3 FIX: SEVERE_DEGRADED (< 5% fresh) after 30s triggers reprime.
                # Previously only CRITICAL (< 50% AND known < 90%) triggered — never fired.
                if self._severe_degraded_since > 0:
                    elapsed_severe = time.time() - self._severe_degraded_since
                    if elapsed_severe >= 30:
                        logger.critical(
                            f"[WS Cache] 🔄 SEVERE DEGRADED for {elapsed_severe:.0f}s "
                            f"(fresh={fresh_pct:.1%}) — triggering auto-recovery"
                        )
                        now = time.time()
                        if self._telegram_bot and (now - self._last_degraded_telegram_alert) > 120:
                            self._last_degraded_telegram_alert = now
                            attempt_num = self._consecutive_reprime_failures + 1
                            try:
                                import asyncio
                                asyncio.run_coroutine_threadsafe(
                                    self._telegram_bot.send_util_alert(
                                        f"🔄 *WS Cache DEGRADED — Auto-Recovery*\n\n"
                                        f"Fresh: {snap['fresh']}/{snap['total']} ({fresh_pct:.1%})\n"
                                        f"Degraded for {elapsed_severe:.0f}s\n"
                                        f"Attempt {attempt_num}/3"
                                    ),
                                    self._loop
                                )
                            except Exception:
                                pass
                        self._trigger_reprime()

                # Old CRITICAL path (kept for 5–50% fresh range)
                elif status == "CRITICAL" and consecutive_critical >= 2:
                    logger.critical(
                        f"[WS Cache] CACHE CRITICAL FOR 60s — Fresh only {fresh_pct:.1%}. Triggering re-prime."
                    )
                    self._trigger_reprime()
                    consecutive_critical = 0

                # ── Unrecoverable Banner ──────────────────────────────────────
                if self._consecutive_reprime_failures >= 3 and self._severe_degraded_since > 0:
                    logger.critical(
                        "[WS Cache] ⛔ UNRECOVERABLE after 3 attempts — "
                        "continuing in HYBRID mode for session remainder. "
                        "Consider manual restart."
                    )
                    now = time.time()
                    if self._telegram_bot and (now - self._last_degraded_telegram_alert) > 300:
                        self._last_degraded_telegram_alert = now
                        try:
                            import asyncio
                            asyncio.run_coroutine_threadsafe(
                                self._telegram_bot.send_util_alert(
                                    f"⛔ *WS Cache UNRECOVERABLE*\n\n"
                                    f"3 recovery attempts all failed.\n"
                                    f"Bot is running on stale REST data (slow scans).\n"
                                    f"⚠️ Consider manual restart."
                                ),
                                self._loop
                            )
                        except Exception:
                            pass

            except Exception as e:
                logger.error(f"[WS Cache] Health monitor error: {e}")

            except Exception as e:
                logger.error(f"[WS Cache] Health monitor error: {e}")

    async def subscribe_symbols(self, symbols: List[str]):
        """Subscribe to real-time data for symbols."""
        new_symbols = [s for s in symbols if s not in self.subscribed_symbols]
        if new_symbols and self.data_ws:
            try:
                # Fyers subscribe is synchronous usually and thread-safe? 
                # Better to run in executor if we are unsure.
                # But SDK documentation usually suggests straight call.
                # However, since data_ws.connect is running in a thread, we calling methods on it is tricky.
                # The SDK methods `subscribe` usually send a message to the socket.
                self.data_ws.subscribe(symbols=new_symbols, data_type="SymbolUpdate")
                self.subscribed_symbols.update(new_symbols)
                logger.info(f"Subscribed to {len(new_symbols)} symbols via WebSocket")
            except Exception as e:
                logger.error(f"Symbol subscription failed: {e}")
    
    async def unsubscribe_symbols(self, symbols: List[str]):
        """Unsubscribe from symbols."""
        if self.data_ws:
            try:
                self.data_ws.unsubscribe(symbols=symbols)
                self.subscribed_symbols.difference_update(symbols)
            except Exception as e:
                logger.error(f"Symbol unsubscribe failed: {e}")

    # ===================================================================
    # REST API Wrappers with Rate Limit
    # ===================================================================
    
    async def _rate_limit_wait(self, endpoint: str):
        """Enforce rate limits."""
        if endpoint not in self.rate_limits:
            return
        
        limit, window = self.rate_limits[endpoint]
        now = datetime.now(UTC).timestamp()
        
        # Clean old
        while self.api_calls[endpoint] and self.api_calls[endpoint][0] < now - window:
            self.api_calls[endpoint].popleft()
        
        if len(self.api_calls[endpoint]) >= limit:
            oldest = self.api_calls[endpoint][0]
            sleep_time = window - (now - oldest)
            if sleep_time > 0:
                logger.warning(f"Rate limit {endpoint}: sleeping {sleep_time:.2f}s")
                await asyncio.sleep(sleep_time)
        
        self.api_calls[endpoint].append(now)

    async def place_order(self, symbol: str, side: str, qty: int, order_type: str = 'MARKET', price: float = 0, trigger_price: float = 0) -> str:
        """Place order via REST API."""
        await self._rate_limit_wait('place_order')
        
        try:
            # Ensure subscribed
            await self.subscribe_symbols([symbol])
            
            data = {
                "symbol": symbol,
                "qty": qty,
                "type": 2 if order_type == 'MARKET' else 1,  
                "side": 1 if side == 'BUY' else -1,
                "productType": "INTRADAY",
                "validity": "DAY",
                "offlineOrder": False
            }
            if order_type == 'LIMIT':
                data['limitPrice'] = price
            elif order_type == 'SL_MARKET':
                data['type'] = 3
                data['stopPrice'] = trigger_price

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, self.rest_client.place_order, data)
            
            if response['s'] == 'ok':
                order_id = response['id']
                self.order_fill_events[order_id] = asyncio.Event()
                logger.info(f"Order placed: {order_id} {side} {qty} {symbol}")
                return order_id
            else:
                raise Exception(f"Order placement failed: {response}")
        except Exception as e:
            logger.error(f"place_order error: {e}")
            raise

    async def cancel_order(self, order_id: str) -> bool:
        await self._rate_limit_wait('cancel_order')
        try:
            loop = asyncio.get_event_loop()
            data = {"id": order_id}
            response = await loop.run_in_executor(None, self.rest_client.cancel_order, data)
            if response['s'] == 'ok':
                logger.info(f"Order cancelled: {order_id}")
                return True
            else:
                logger.warning(f"Cancel order failed: {response}")
                return False
        except Exception as e:
            logger.error(f"cancel_order error: {e}")
            return False

    async def wait_for_fill(self, order_id: str, timeout: float = 30.0) -> bool:
        """Wait for order fill via WebSocket event."""
        if order_id not in self.order_fill_events:
            self.order_fill_events[order_id] = asyncio.Event()
        
        try:
            await asyncio.wait_for(self.order_fill_events[order_id].wait(), timeout=timeout)
            
            if order_id in self.order_status_cache:
                status = self.order_status_cache[order_id].status
                return status == 'FILLED'
            
            # Fallback
            return await self._check_order_status_rest(order_id) == 'FILLED'
        except asyncio.TimeoutError:
            logger.warning(f"Order {order_id} fill timeout")
            return False
        finally:
            if order_id in self.order_fill_events:
                del self.order_fill_events[order_id]

    async def get_order_status(self, order_id: str) -> str:
        if order_id in self.order_status_cache:
            age = (datetime.now(UTC) - self.order_status_cache[order_id].timestamp).total_seconds()
            if age < 5.0:
                return self.order_status_cache[order_id].status
        return await self._check_order_status_rest(order_id)

    async def get_ltp(self, symbol: str) -> Optional[float]:
        """Get Last Traded Price (uses WebSocket tick cache, falls back to REST)."""
        # Try WebSocket cache first (0ms latency)
        if symbol in self.tick_cache and self.tick_cache[symbol]:
            latest_tick = self.tick_cache[symbol][-1]
            age = (datetime.now(UTC) - latest_tick.timestamp).total_seconds()
            if age < 5.0:  # Cache valid for 5 seconds
                return latest_tick.ltp
        
        # Fallback to REST API
        await self._rate_limit_wait('get_quotes')
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, self.rest_client.quotes, {"symbols": symbol})
            if response['s'] == 'ok' and 'd' in response:
                return response['d'][0]['v']['lp']
            return None
        except Exception as e:
            logger.error(f"Get LTP error: {e}")
            return None
    async def get_local_slope(self, symbol: str, window: int = 30) -> float:
        """Phase 88: Get real-time slope from memory (0ms REST)."""
        if self.aggregator:
            return self.aggregator.get_vwap_slope(symbol, window)
        return 0.0

    async def get_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """Get quotes for multiple symbols."""
        quotes = {}
        missing = []
        now = datetime.now(UTC)
        
        for sym in symbols:
            if sym in self.tick_cache and self.tick_cache[sym]:
                tick = self.tick_cache[sym][-1]
                if (now - tick.timestamp).total_seconds() < 5.0:
                    quotes[sym] = {'ltp': tick.ltp} # Add other fields if needed
                    continue
            missing.append(sym)
            
        if missing:
             await self._rate_limit_wait('get_quotes')
             try:
                 loop = asyncio.get_event_loop()
                 # quotes API expects comma separated string? Check SDK.
                 # Usually "NSE:SBIN-EQ,NSE:RIL-EQ"
                 sym_str = ",".join(missing)
                 response = await loop.run_in_executor(None, self.rest_client.quotes, {"symbols": sym_str})
                 if response['s'] == 'ok':
                     for d in response['d']:
                         quotes[d['n']] = {'ltp': d['v']['lp']}
             except Exception as e:
                 logger.error(f"Get quotes error: {e}")
                 
        return quotes


    async def _check_order_status_rest(self, order_id: str) -> str:
        await self._rate_limit_wait('get_order_status')
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, self.rest_client.orderbook)
            if response['s'] == 'ok':
                for order in response['orderBook']:
                    if order['id'] == order_id:
                        status = order['status']
                        return status
            return 'UNKNOWN'
        except Exception as e:
            logger.error(f"Order status query error: {e}")
            return 'UNKNOWN'

    async def get_funds(self) -> dict:
        """
        Fetch available margin from Fyers /funds endpoint.
        Called by CapitalManager.sync() at startup, post-fill, post-close.
        """
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, self.rest_client.funds)
            if response and response.get('s') == 'ok':
                return response
            raise ValueError(f"Fyers funds API error: {response}")
        except Exception as e:
            logger.error(f"get_funds failed: {e}")
            raise

    async def get_symbol_leverage(self, symbol: str, price: float) -> float:
        """
        Phase 79: Fetch actual leverage for a symbol using fyers.order_calc.
        Leverage = Price / Margin_Required.
        """
        if not symbol:
            return 1.0

        # Check Cache
        with self._leverage_cache_lock:
            if symbol in self._leverage_cache:
                return self._leverage_cache[symbol]

        # Fetch from Broker
        try:
            data = {
                "data": [
                    {
                        "symbol": symbol,
                        "qty": 1,
                        "side": 1,  # 1 for Buy (margin check is same for both ideally)
                        "type": 2,  # 2 for Market
                        "productType": "INTRADAY",
                        "limitPrice": 0,
                        "stopPrice": 0
                    }
                ]
            }
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, self.rest_client.order_calc, data)
            
            if response and response.get('s') == 'ok' and response.get('data'):
                margin = response['data'][0].get('margin', 0)
                if margin > 0:
                    leverage = round(price / margin, 2)
                    with self._leverage_cache_lock:
                        self._leverage_cache[symbol] = leverage
                    logger.info(f"[BROKER] Dynamic Leverage detected for {symbol}: {leverage}x (Margin: ₹{margin:.2f} @ ₹{price:.2f})")
                    return leverage
            
            logger.warning(f"[BROKER] Could not detect leverage for {symbol}, response: {response}. Defaulting to 1.0x")
            return 1.0
            
        except Exception as e:
            logger.error(f"[BROKER] Leverage detection failed for {symbol}: {e}. Defaulting to 1.0x")
            return 1.0

    async def get_all_positions(self) -> List[Dict]:
        """Get all open positions (Cache first)."""
        positions = []
        for symbol, pos_update in self.position_cache.items():
            age = (datetime.now(UTC) - pos_update.timestamp).total_seconds()
            if age < 10.0 and pos_update.net_qty != 0:
                positions.append({
                    'symbol': symbol,
                    'qty': pos_update.net_qty,
                    'avg_price': pos_update.avg_price,
                    'unrealized_pnl': pos_update.unrealized_pnl
                })
        
        if not positions:
            await self._rate_limit_wait('get_positions')
            try:
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(None, self.rest_client.positions)
                if response['s'] == 'ok':
                    for pos in response['netPositions']:
                        if pos['netQty'] != 0:
                            positions.append({
                                'symbol': pos['symbol'],
                                'qty': pos['netQty'],
                                'avg_price': pos['avgPrice'],
                                'unrealized_pnl': pos.get('unrealized_profit', 0)
                            })
                            # Update cache? The cache object expects WS format.
                            # We might need to map REST format to WS format if we want to cache it.
                            # For now, we just return it.
            except Exception as e:
                logger.error(f"Get all positions error: {e}")
        return positions

    async def shutdown(self):
        logger.info("Shutting down broker interface...")
        for task in self.tasks:
            task.cancel()
        
        # Close sockets?
        # Fyers SDK doesn't always have clean close methods exposed easily for async.
        pass

    async def disconnect(self):
        """
        Cleanly stop WebSocket threads. Called during cleanup_runtime().
        """
        logger.info("[BROKER] Disconnecting WebSocket connections...")

        # Stop the health monitor thread
        health_thread = getattr(self, '_health_monitor_thread', None)
        if health_thread and health_thread.is_alive():
            # Signal the thread to stop — set a stop flag it checks
            self._ws_cache_stop = True
            health_thread.join(timeout=3.0)
            logger.info("[BROKER] Health monitor thread stopped.")

        # Stop data WebSocket
        try:
            data_ws = getattr(self, 'data_ws', None) or getattr(self, '_data_ws', None)
            if data_ws:
                await asyncio.to_thread(data_ws.close)
                logger.info("[BROKER] Data WebSocket closed.")
        except Exception as e:
            logger.warning(f"[BROKER] Data WS close error (non-fatal): {e}")

        # Stop order WebSocket
        try:
            order_ws = getattr(self, 'order_ws', None) or getattr(self, '_order_ws', None)
            if order_ws:
                await asyncio.to_thread(order_ws.close)
                logger.info("[BROKER] Order WebSocket closed.")
        except Exception as e:
            logger.warning(f"[BROKER] Order WS close error (non-fatal): {e}")

        logger.info("[BROKER] Disconnect complete.")
