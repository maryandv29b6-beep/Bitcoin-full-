"""Shared I/O plumbing for exchange adapters: rate-limited REST client,
reconnecting WebSocket loop with watchdog, status bookkeeping."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Deque, Dict, Optional, Tuple

import aiohttp

if TYPE_CHECKING:  # pragma: no cover
    from ..collector import InstrumentRuntime

log = logging.getLogger("flowlens.adapter")


def now_ms() -> int:
    return int(time.time() * 1000)


class RateLimited(Exception):
    pass


class RestClient:
    """Weight-aware REST client. Keeps the used weight of the last 60 s below
    ``budget`` and honours 429/418 Retry-After (Binance) or HTTP errors."""

    def __init__(self, session: aiohttp.ClientSession, base: str, budget: int = 4000,
                 weight_header: Optional[str] = None, name: str = "") -> None:
        self.session = session
        self.base = base.rstrip("/")
        self.budget = budget
        self.weight_header = weight_header
        self.name = name
        self.used: Deque[Tuple[float, int]] = deque()
        self.server_used: Optional[int] = None
        self.blocked_until = 0.0
        self.requests = 0
        self.errors = 0
        self._lock = asyncio.Lock()

    def _window_weight(self, now: float) -> int:
        while self.used and self.used[0][0] < now - 60:
            self.used.popleft()
        return sum(w for _, w in self.used)

    async def _throttle(self, weight: int) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                if self.blocked_until > now:
                    await asyncio.sleep(self.blocked_until - now)
                    continue
                used = max(self._window_weight(now), self.server_used or 0)
                if used + weight <= self.budget or not self.used:
                    self.used.append((now, weight))
                    return
                await asyncio.sleep(max(0.2, self.used[0][0] + 60 - now))

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None, weight: int = 1,
                  timeout: float = 15.0) -> Any:
        await self._throttle(weight)
        self.requests += 1
        try:
            async with self.session.get(self.base + path, params=params,
                                        timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if self.weight_header and r.headers.get(self.weight_header):
                    try:
                        self.server_used = int(r.headers[self.weight_header])
                    except ValueError:
                        pass
                if r.status in (418, 429):
                    retry = float(r.headers.get("Retry-After", "60"))
                    self.blocked_until = time.monotonic() + retry
                    self.errors += 1
                    raise RateLimited(f"{self.name} {r.status}, retry after {retry}s")
                if r.status >= 400:
                    self.errors += 1
                    text = (await r.text())[:300]
                    raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status, message=text)
                return await r.json(content_type=None)
        except asyncio.TimeoutError:
            self.errors += 1
            raise


class WsConn:
    """Reconnecting WebSocket with a message watchdog."""

    def __init__(self, rt: "InstrumentRuntime", name: str, url_fn: Callable[[], str],
                 on_open: Callable[[aiohttp.ClientWebSocketResponse], Awaitable[None]],
                 on_message: Callable[[Any, int], None],
                 on_close: Callable[[], None],
                 app_ping: Optional[Callable[[], Any]] = None, app_ping_s: float = 20.0) -> None:
        self.rt = rt
        self.name = name
        self.url_fn = url_fn
        self.on_open = on_open
        self.on_message = on_message
        self.on_close = on_close
        self.app_ping = app_ping
        self.app_ping_s = app_ping_s
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.last_msg = 0.0
        self.connected = False
        self.connects = 0
        self.messages = 0
        self.stopped = False

    async def run(self) -> None:
        backoff = 1.0
        while not self.stopped:
            url = self.url_fn()
            try:
                async with self.rt.session.ws_connect(url, autoping=True, heartbeat=None, max_msg_size=0,
                                                      timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
                    self.ws = ws
                    self.connected = True
                    self.connects += 1
                    self.last_msg = time.monotonic()
                    await self.on_open(ws)
                    backoff = 1.0
                    pinger = asyncio.create_task(self._ping_loop(ws)) if self.app_ping else None
                    watchdog = asyncio.create_task(self._watchdog(ws))
                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self.last_msg = time.monotonic()
                                self.messages += 1
                                try:
                                    data = json.loads(msg.data)
                                except ValueError:
                                    continue
                                self.on_message(data, now_ms())
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                    finally:
                        watchdog.cancel()
                        if pinger:
                            pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network errors, handshake errors
                self.rt.error(f"{self.name}: {type(e).__name__}: {e}")
            self.connected = False
            self.ws = None
            try:
                self.on_close()
            except Exception as e:  # pragma: no cover
                log.exception("on_close failed: %s", e)
            if self.stopped:
                break
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 60.0)

    async def _ping_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await asyncio.sleep(self.app_ping_s)
            try:
                await ws.send_json(self.app_ping())
            except Exception:
                return

    async def _watchdog(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        cfg = self.rt.cfg
        while True:
            await asyncio.sleep(1.0)
            idle = time.monotonic() - self.last_msg
            if idle > cfg.reconnect_after_s:
                self.rt.quality("stale", f"{self.name}: нет сообщений {idle:.0f} с, переподключение")
                await ws.close()
                return
            self.rt.set_stale(self.name, idle > cfg.stale_after_s)

    def stop(self) -> None:
        self.stopped = True
        if self.ws is not None:
            asyncio.ensure_future(self.ws.close())


class AdapterBase:
    exchange = ""
    supports_seq_trades = False

    def __init__(self, rt: "InstrumentRuntime") -> None:
        self.rt = rt
        self.conns: list = []
        self._snapshot_task: Optional[asyncio.Task] = None

    async def describe(self) -> Dict[str, Any]:
        """Fetch instrument metadata: base, quote, tick, step, contract."""
        raise NotImplementedError

    async def run(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        for c in self.conns:
            c.stop()

    # helpers
    def spawn(self, coro) -> asyncio.Task:
        return self.rt.spawn(coro)
