"""DEMO source: synthetic market, always labelled DEMO.

Runs the simulator in-process and feeds its Binance-Spot-shaped messages
through the real BinanceSpotSync + Pipeline. A configurable amount of
synthetic history is generated at start (fast-forward through the same code);
it is stored under the 'demo' exchange only and never mixed with real data.
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from typing import Any, Dict

from .. import numeric
from ..models import Liquidation
from ..simulator import MarketSim
from .base import AdapterBase, now_ms
from .binance import parse_agg_trade

DEMO_PARAMS = {
    "BTCUSDT": dict(price=77000.0, tick="0.01", step="0.00001", base_qty=0.9, seed=11, levels=1200),
    "ETHUSDT": dict(price=2300.0, tick="0.01", step="0.0001", base_qty=14.0, seed=23, levels=1200),
}


class DemoAdapter(AdapterBase):
    exchange = "demo"
    supports_seq_trades = True

    def __init__(self, rt) -> None:
        super().__init__(rt)
        p = DEMO_PARAMS.get(rt.inst.symbol, DEMO_PARAMS["BTCUSDT"])
        self.params = p
        self.sim = MarketSim(symbol=rt.inst.symbol, price=p["price"], tick=p["tick"], step=p["step"],
                             base_qty=p["base_qty"], seed=p["seed"], levels=p["levels"],
                             spacing=p["price"] * 0.05 / p["levels"])
        self.perp = rt.inst.market == "perp"
        self.rng = random.Random(p["seed"] + 1)
        self.oi = p["base_qty"] * 40000
        self.ready = False

    async def describe(self) -> Dict[str, Any]:
        base = self.rt.inst.symbol.replace("USDT", "")
        return {"base": base, "quote": "USDT", "tick": numeric.parse(self.params["tick"]),
                "step": numeric.parse(self.params["step"]),
                "contract": {"type": "linear", "quantityUnit": "base", "multiplier": "1", "demo": True} if self.perp else {}}

    def _feed(self, ev, trades, now: int) -> None:
        if ev is not None:
            acts = self.rt.sync.on_message(ev)
            if "need_snapshot" in acts:
                acts += self.rt.sync.on_snapshot(self.sim.snapshot(5000))
            self.rt.handle_sync([a for a in acts if a != "need_snapshot"], now)
            self.rt.book.last_exch_ts = now
            self.rt.note_exchange_time(now, now, "book")
        if trades:
            self.rt.note_exchange_time(now, now, "trades")
        for d in trades:
            tr = parse_agg_trade(d, now)
            self.rt.on_trade(tr)
        if self.perp:
            self._deriv(now, trades)

    def _deriv(self, now: int, trades) -> None:
        rng = self.rng
        if now % 5000 < 100:
            self.oi = max(1.0, self.oi * (1 + rng.gauss(0, 0.0004) + 0.00005 * self.sim.burst_side * (self.sim.burst > 0)))
            funding = 0.0001 + 0.00008 * self.sim.pressure
            period = 8 * 3600_000
            nxt = now - now % period + period
            self.rt.on_deriv(now, oi=self.oi, oi_value=self.oi * self.sim.mid, funding=funding, next_funding=nxt,
                             mark=self.sim.mid)
        if self.sim.burst > 0 and rng.random() < 0.04:
            side = "short" if self.sim.burst_side > 0 else "long"
            q = abs(rng.gauss(0, self.params["base_qty"] * 3)) + self.params["base_qty"] * 0.1
            self.rt.pipeline.on_liquidation(Liquidation(now, now, side, numeric.parse(f"{self.sim.mid:.2f}"),
                                                        numeric.parse(f"{q:.5f}")))

    async def run(self) -> None:
        self.rt.set_book_status("CONNECTING")
        hours = float(self.rt.cfg.demo_history_hours)
        end = now_ms()
        end -= end % 5000
        t = end - int(hours * 3600_000)
        self.rt.pipeline.trade_stream_started(t)
        # fast-forward with 1 s steps; same sync + pipeline as live
        steps = 0
        self.rt.generating = True
        step = 5000  # coarse fast-forward: one market step and one book sample per 5 s
        while t < end:
            ev, trades = self.sim.step_ms(t, step)
            self._feed(ev, trades, t)
            self.rt.pipeline.on_tick(t, step)
            t += step
            steps += 1
            if steps % 4 == 0:
                if steps % 120 == 0:
                    self.rt.collector.flush_runtime(self.rt)
                await asyncio.sleep(0)
        self.rt.generating = False
        self.rt.collector.flush_runtime(self.rt)
        self.ready = True
        self.rt.quality("demo", f"синтетическая история {hours} ч сгенерирована")
        # live: 100 ms steps in real time; sampling ticks come from the collector
        nxt = time.monotonic()
        while True:
            nxt += 0.1
            ev, trades = self.sim.step_ms(now_ms(), 100)
            self._feed(ev, trades, now_ms())
            await asyncio.sleep(max(0.0, nxt - time.monotonic()))

    def request_snapshot(self, refresh: bool = False) -> None:
        acts = self.rt.sync.on_snapshot(self.sim.snapshot(5000))
        self.rt.handle_sync(acts, now_ms())
