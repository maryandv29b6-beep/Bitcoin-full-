"""Mock exchange speaking a subset of the Binance Spot and USD-M public API,
backed by the synthetic MarketSim. Used for integration tests, fault injection
and load tests (no real exchange is contacted). All data is synthetic.

Fault injection: POST /admin/fault {"kind": ..., "n": N, "symbol": "BTCUSDT"}
  drop_depth   skip the next N depth events (creates a sequence gap)
  drop_trades  skip the next N aggTrades on the stream (REST still has them)
  dup_trades   send the next N aggTrades twice
  disconnect   close all WebSocket connections
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from aiohttp import web

from .simulator import MarketSim

PRICES = {"BTCUSDT": 77000.0, "ETHUSDT": 2300.0, "SOLUSDT": 180.0, "BNBUSDT": 650.0, "XRPUSDT": 2.4,
          "ADAUSDT": 0.8, "DOGEUSDT": 0.2, "AVAXUSDT": 35.0, "LINKUSDT": 20.0, "LTCUSDT": 90.0}


class MockExchange:
    def __init__(self, symbols: List[str], levels: int = 1500, seed: int = 1) -> None:
        self.sims: Dict[str, MarketSim] = {}
        self.trades: Dict[str, Deque[dict]] = {}
        self.last_u: Dict[str, int] = {}
        for i, s in enumerate(symbols):
            price = PRICES.get(s, 100.0)
            tick = "0.01" if price > 10 else "0.0001"
            step = "0.00001" if price > 1000 else "0.001"
            self.sims[s] = MarketSim(symbol=s, price=price, tick=tick, step=step, seed=seed + i, levels=levels,
                                     spacing=price * 0.05 / levels, base_qty=max(0.5, 60000 / price / 50))
            self.trades[s] = deque(maxlen=200000)
            self.last_u[s] = self.sims[s].update_id
        self.clients: List[tuple] = []   # (ws, set(streams), kind)
        self.faults: Dict[str, Dict[str, int]] = {s: {} for s in symbols}
        self.weight = 0
        self.sent = 0
        self.task: Optional[asyncio.Task] = None

    # ---- simulation loop -----------------------------------------------------
    async def loop(self) -> None:
        nxt = time.monotonic()
        while True:
            nxt += 0.1
            now = int(time.time() * 1000)
            for s, sim in self.sims.items():
                ev, trades = sim.step_ms(now, 100)
                f = self.faults[s]
                lo = s.lower()
                if ev is not None:
                    ev["pu"] = self.last_u[s]
                    self.last_u[s] = ev["u"]
                    if f.get("drop_depth", 0) > 0:
                        f["drop_depth"] -= 1
                    else:
                        await self._broadcast(f"{lo}@depth@100ms", ev)
                for d in trades:
                    self.trades[s].append(d)
                    if f.get("drop_trades", 0) > 0:
                        f["drop_trades"] -= 1
                        continue
                    await self._broadcast(f"{lo}@aggTrade", d)
                    if f.get("dup_trades", 0) > 0:
                        f["dup_trades"] -= 1
                        await self._broadcast(f"{lo}@aggTrade", d)
                if now % 1000 < 100:
                    await self._broadcast(f"{lo}@markPrice@1s", {"e": "markPriceUpdate", "E": now, "s": s,
                                                                 "p": f"{sim.mid:.2f}", "r": "0.00010000",
                                                                 "T": now - now % 28800000 + 28800000})
            await asyncio.sleep(max(0.0, nxt - time.monotonic()))

    async def _broadcast(self, stream: str, data: dict) -> None:
        msg = json.dumps({"stream": stream, "data": data})
        for ws, streams, _ in list(self.clients):
            if stream in streams:
                try:
                    await ws.send_str(msg)
                    self.sent += 1
                except Exception:
                    pass

    # ---- helpers ------------------------------------------------------------------
    def _sym(self, request: web.Request) -> str:
        s = request.query.get("symbol", "")
        if s not in self.sims:
            raise web.HTTPBadRequest(text=json.dumps({"code": -1121, "msg": "Invalid symbol."}))
        return s

    def _json(self, data: Any, weight: int) -> web.Response:
        self.weight += weight
        return web.json_response(data, headers={"X-MBX-USED-WEIGHT-1M": str(min(self.weight, 5000))})

    # ---- REST ---------------------------------------------------------------------
    async def exchange_info(self, request: web.Request) -> web.Response:
        syms = [self.request_sym(request)] if request.query.get("symbol") else list(self.sims)
        out = []
        for s in syms:
            sim = self.sims[s]
            out.append({"symbol": s, "baseAsset": s.replace("USDT", ""), "quoteAsset": "USDT",
                        "contractType": "PERPETUAL", "marginAsset": "USDT",
                        "filters": [{"filterType": "PRICE_FILTER", "tickSize": sim.tick_s},
                                    {"filterType": "LOT_SIZE", "stepSize": sim.step_s}]})
        return self._json({"symbols": out}, 20)

    def request_sym(self, request: web.Request) -> str:
        return self._sym(request)

    async def price(self, request: web.Request) -> web.Response:
        s = self._sym(request)
        return self._json({"symbol": s, "price": f"{self.sims[s].mid:.2f}"}, 2)

    async def ticker24(self, request: web.Request) -> web.Response:
        s = self._sym(request)
        m = self.sims[s].mid
        return self._json({"symbol": s, "lastPrice": f"{m:.2f}", "priceChangePercent": "0.42", "volume": "12345.6",
                           "quoteVolume": f"{12345.6 * m:.2f}"}, 2)

    async def depth(self, request: web.Request) -> web.Response:
        s = self._sym(request)
        limit = int(request.query.get("limit", 100))
        await asyncio.sleep(0.05)
        return self._json(self.sims[s].snapshot(limit), 250 if limit > 1000 else 50)

    async def klines(self, request: web.Request) -> web.Response:
        s = self._sym(request)
        limit = int(request.query.get("limit", 500))
        end = int(request.query.get("endTime", time.time() * 1000))
        start = int(request.query.get("startTime", end - limit * 60000))
        start -= start % 60000
        end = min(end, int(time.time() * 1000))
        end -= end % 60000
        rows = self.sims[s].klines(start, min(end, start + limit * 60000), seed=7)
        return self._json(rows, 2)

    async def agg_trades(self, request: web.Request) -> web.Response:
        s = self._sym(request)
        frm = int(request.query.get("fromId", 0))
        limit = min(1000, int(request.query.get("limit", 500)))
        out = [d for d in self.trades[s] if d["a"] >= frm][:limit]
        return self._json([{k: d[k] for k in ("a", "p", "q", "f", "l", "T", "m", "M")} for d in out], 4)

    async def open_interest(self, request: web.Request) -> web.Response:
        s = self._sym(request)
        return self._json({"symbol": s, "openInterest": "81234.5", "time": int(time.time() * 1000)}, 1)

    async def empty_list(self, request: web.Request) -> web.Response:
        return self._json([], 1)

    async def fault(self, request: web.Request) -> web.Response:
        body = await request.json()
        kind, n = body["kind"], int(body.get("n", 1))
        if kind == "disconnect":
            for ws, _, _ in list(self.clients):
                await ws.close()
            return web.json_response({"ok": True})
        for s in ([body["symbol"]] if body.get("symbol") else list(self.sims)):
            self.faults[s][kind] = self.faults[s].get(kind, 0) + n
        return web.json_response({"ok": True})

    async def stats(self, request: web.Request) -> web.Response:
        return web.json_response({"clients": len(self.clients), "sent": self.sent, "weight": self.weight,
                                  "symbols": list(self.sims)})

    # ---- WS -----------------------------------------------------------------------
    async def ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        streams = set((request.query.get("streams") or "").split("/"))
        item = (ws, streams, request.path)
        self.clients.append(item)
        try:
            async for _ in ws:
                pass
        finally:
            if item in self.clients:
                self.clients.remove(item)
        return ws

    def app(self) -> web.Application:
        app = web.Application()
        r = app.router
        for p in ("/api/v3", "/fapi/v1"):
            r.add_get(f"{p}/exchangeInfo", self.exchange_info)
            r.add_get(f"{p}/ticker/price", self.price)
            r.add_get(f"{p}/ticker/24hr", self.ticker24)
            r.add_get(f"{p}/depth", self.depth)
            r.add_get(f"{p}/klines", self.klines)
            r.add_get(f"{p}/aggTrades", self.agg_trades)
        r.add_get("/fapi/v1/openInterest", self.open_interest)
        r.add_get("/fapi/v1/fundingRate", self.empty_list)
        r.add_get("/futures/data/openInterestHist", self.empty_list)
        r.add_get("/stream", self.ws)
        r.add_get("/public/stream", self.ws)
        r.add_get("/market/stream", self.ws)
        r.add_post("/admin/fault", self.fault)
        r.add_get("/admin/stats", self.stats)

        async def start(app):
            self.task = asyncio.ensure_future(self.loop())

        async def stop(app):
            if self.task:
                self.task.cancel()

        app.on_startup.append(start)
        app.on_cleanup.append(stop)
        return app


async def run_mock(port: int, symbols: List[str]) -> None:
    mx = MockExchange(symbols)
    runner = web.AppRunner(mx.app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    print(f"mock exchange on http://127.0.0.1:{port} symbols={symbols} (synthetic data)", flush=True)
    await asyncio.Event().wait()
