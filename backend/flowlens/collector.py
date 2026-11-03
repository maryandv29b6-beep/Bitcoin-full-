"""Collector: one runtime per instrument (adapter + sync + pipeline), the
sampling clock, persistence and live-state snapshots for the API."""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import aiohttp

from . import numeric
from .adapters.base import RestClient, now_ms
from .adapters.binance import BinanceAdapter
from .adapters.bybit import BybitAdapter
from .adapters.demo import DemoAdapter
from .book import LocalBook
from .config import Config, InstrumentCfg
from .models import DerivState, Instrument, Trade
from .pipeline import MIN_MS, Pipeline, minute_of
from .recorder import Recorder
from .storage import Storage
from .sync import BinanceFuturesSync, BinanceSpotSync, BybitSync

try:  # not available on Windows
    import resource
except ImportError:  # pragma: no cover
    resource = None  # type: ignore

log = logging.getLogger("flowlens.collector")

ADAPTERS = {"binance": BinanceAdapter, "bybit": BybitAdapter, "demo": DemoAdapter}


def default_steps(price: float, tick: int) -> Tuple[int, int]:
    """Storage steps (scaled ints): footprint ~0.5 bp of price, heatmap ~1 bp,
    rounded to nice multiples of the tick size."""
    t = tick / numeric.SCALE
    fp = numeric.nice_step(price * 0.00005, t)
    hm = numeric.nice_step(price * 0.0001, t)
    return numeric.parse(f"{fp:.8f}"), numeric.parse(f"{hm:.8f}")


class InstrumentRuntime:
    def __init__(self, collector: "Collector", icfg: InstrumentCfg) -> None:
        self.collector = collector
        self.cfg: Config = collector.cfg
        self.icfg = icfg
        self.session: aiohttp.ClientSession = collector.session
        self.storage: Storage = collector.storage
        self.inst = Instrument(icfg.exchange, icfg.market, icfg.symbol)
        self.book = LocalBook()
        if icfg.exchange == "bybit":
            self.sync: Any = BybitSync(self.book, icfg.depth or (200 if icfg.market == "spot" else 500))
        elif icfg.exchange == "binance" and icfg.market == "perp":
            self.sync = BinanceFuturesSync(self.book, icfg.depth or 1000)
        else:
            self.sync = BinanceSpotSync(self.book, icfg.depth or 5000)
        self.adapter = ADAPTERS[icfg.exchange](self)
        self.pipeline: Optional[Pipeline] = None
        self.iid: int = -1
        self.meta: Dict[str, Any] = {}
        self.book_status = "CONNECTING"
        self.stale: Dict[str, bool] = {}
        self.errors: Deque[Tuple[int, str]] = deque(maxlen=50)
        self.events: Deque[Tuple[int, str, str]] = deque(maxlen=200)
        self.ticker: Optional[Dict[str, Any]] = None
        self.deriv_state = DerivState()
        self.lat: Dict[str, Deque[int]] = {"book": deque(maxlen=200), "trades": deque(maxlen=200)}
        self.last_event: Dict[str, Optional[int]] = {"book": None, "trades": None}
        self.recorder = Recorder(self.cfg.raw_dir, self.inst.key, self.cfg.raw_retention_hours) if self.cfg.record_raw else None
        self.reconciled: set = set()
        self.started = False
        self.generating = False
        self.last_closed_minute: Optional[int] = None
        self.tasks: List[asyncio.Task] = []

    # ---- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        delay = 2.0
        while True:
            try:
                d = await self.adapter.describe()
                break
            except Exception as e:
                self.error(f"instrument metadata: {e}")
                self.book_status = "ERROR"
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)
        inst = self.inst
        inst.base, inst.quote, inst.tick, inst.step, inst.contract = d["base"], d["quote"], d["tick"], d["step"], d["contract"]
        price = await self._reference_price()
        fp, hm = default_steps(price, inst.tick)
        if self.icfg.footprint_step:
            fp = numeric.parse(self.icfg.footprint_step)
        if self.icfg.heatmap_step:
            hm = numeric.parse(self.icfg.heatmap_step)
        self.iid, self.meta = self.storage.upsert_instrument(inst, {"footprint_step": fp, "heatmap_step": hm,
                                                                   "first_seen": now_ms()})
        inst.id = self.iid
        if inst.is_demo:
            self.storage.clear_instrument(self.iid)
        self.pipeline = Pipeline(inst, self.book, fp_step=int(self.meta["footprint_step"]),
                                 heat_step=int(self.meta["heatmap_step"]), heat_pct=self.cfg.heatmap_range_pct,
                                 sample_ms=self.cfg.sample_ms)
        self.started = True
        self.spawn(self.adapter.run())

    async def _reference_price(self) -> float:
        if self.inst.is_demo:
            return self.adapter.params["price"]
        try:
            if self.inst.exchange == "binance":
                path = "/api/v3/ticker/price" if self.inst.market == "spot" else "/fapi/v1/ticker/price"
                d = await self.adapter.rest.get(path, {"symbol": self.inst.symbol}, weight=2)
                return float(d["price"])
            d = await self.adapter.rest.get("/v5/market/tickers", {"category": self.adapter.category,
                                                                    "symbol": self.inst.symbol})
            return float(d["result"]["list"][0]["lastPrice"])
        except Exception as e:
            self.error(f"reference price: {e}")
            return 100.0

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)

        def done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                self.error(f"task failed: {type(exc).__name__}: {exc}")
                log.exception("task failed", exc_info=exc)

        task.add_done_callback(done)
        self.tasks.append(task)
        self.tasks = [t for t in self.tasks if not t.done()]
        return task

    def shared_rest(self, key: str, base: str, budget: int, header: Optional[str]) -> RestClient:
        return self.collector.rest(key, base, budget, header)

    def stop(self) -> None:
        self.adapter.stop()
        for t in self.tasks:
            t.cancel()
        if self.recorder:
            self.recorder.close()

    # ---- events from adapters --------------------------------------------------
    def record(self, kind: str, recv: int, data: Any) -> None:
        if self.recorder is not None:
            self.recorder.write(kind, recv, data)

    def reset_sync(self) -> None:
        self.record("reset", now_ms(), None)
        self.sync.reset()

    def trade_stream_started(self, t: int) -> None:
        self.record("trade_start", now_ms(), t)
        if self.pipeline is not None:
            self.pipeline.trade_stream_started(t)

    def handle_sync(self, actions: List[str], recv: int) -> None:
        for a in actions:
            if a == "need_snapshot":
                self.adapter.request_snapshot()
            elif a == "gap":
                self.book_status = "RESYNC"
                self.quality("book_gap", "разрыв последовательности стакана, ресинхронизация")
            elif a == "synced":
                self.book_status = "LIVE"
                self.quality("resync", f"новая достоверная последовательность стакана #{self.book.epoch}")
            elif a == "refreshed":
                self.quality("refresh", "снимок стакана обновлён для покрытия глубины")

    def on_trade(self, tr: Trade, backfill: bool = False) -> Optional[Tuple[int, int]]:
        if self.pipeline is None:
            return None
        if not backfill:
            self.last_event["trades"] = tr.t_recv
        return self.pipeline.on_trade(tr, backfill)

    def on_deriv(self, ts: int, **kw: Any) -> None:
        st = self.deriv_state
        if kw.get("oi") is not None:
            st.open_interest, st.t_oi = kw["oi"], ts
        if kw.get("oi_value") is not None:
            st.open_interest_value = kw["oi_value"]
        if kw.get("funding") is not None:
            st.funding_rate, st.t_funding = kw["funding"], ts
        if kw.get("next_funding") is not None:
            st.next_funding_time = kw["next_funding"]
        if kw.get("mark") is not None:
            st.mark_price = kw["mark"]
        if self.pipeline is not None:
            self.pipeline.on_deriv(ts, **kw)

    def note_exchange_time(self, exch: Optional[int], recv: int, kind: str) -> None:
        self.last_event[kind] = recv
        if exch:
            self.lat[kind].append(recv - int(exch))

    def quality(self, kind: str, detail: str = "") -> None:
        t = now_ms()
        self.events.append((t, kind, detail))
        if self.iid >= 0 and kind not in ("connect",):
            try:
                self.storage.quality_event(self.iid, kind, t, None, detail)
            except Exception as e:  # pragma: no cover
                log.warning("quality_event failed: %s", e)

    def error(self, msg: str) -> None:
        self.errors.append((now_ms(), msg))
        log.warning("%s: %s", self.inst.key, msg)

    def set_stale(self, name: str, flag: bool) -> None:
        self.stale[name] = flag

    def set_book_status(self, s: str) -> None:
        self.book_status = s

    # ---- derived state ---------------------------------------------------------
    def status(self) -> str:
        if self.inst.is_demo:
            return "DEMO"
        if self.book_status == "ERROR":
            return "ERROR"
        if any(self.stale.values()):
            return "STALE"
        if self.book_status in ("RESYNC", "CONNECTING"):
            return self.book_status
        return "LIVE"

    def coverage_low(self) -> bool:
        s = self.pipeline.last_sample if self.pipeline else None
        if s is None:
            return False
        k = 2  # 1 % band
        return s.bids[k][2] < 1.0 or s.asks[k][2] < 1.0

    def incomplete_closed_minutes(self) -> List[int]:
        if self.pipeline is None:
            return []
        cur = minute_of(now_ms())
        out = []
        for m, c in self.pipeline.candles.items():
            if m in self.reconciled or m >= cur - MIN_MS or m < cur - 120 * MIN_MS:
                continue
            if not (c.complete and c.gap_count == 0) and not self.pipeline.seq_open_holes():
                out.append(m)
                self.reconciled.add(m)
        return sorted(out)

    def latency(self, kind: str) -> Optional[float]:
        v = self.lat[kind]
        if not v:
            return None
        s = sorted(v)
        return float(s[len(s) // 2])

    def live_status(self) -> Dict[str, Any]:
        st = self.sync.stats.as_dict()
        return {
            "status": self.status(),
            "book_status": self.book_status,
            "demo": self.inst.is_demo,
            "generating": self.generating,
            "last_event": max([v for v in self.last_event.values() if v] or [0]) or None,
            "last_book_event": self.last_event["book"],
            "last_trade_event": self.last_event["trades"],
            "latency_ms": {"book": self.latency("book"), "trades": self.latency("trades")},
            "book_epoch": self.book.epoch,
            "book_levels": [len(self.book.bids), len(self.book.asks)],
            "sync": st,
            "sync_rules": self.sync.name,
            "trade_dupes": self.pipeline.seq.duplicates if self.pipeline else 0,
            "trade_holes": self.pipeline.seq_open_holes() if self.pipeline else [],
            "errors": list(self.errors)[-5:],
            "events": list(self.events)[-10:],
            "stale": self.stale,
        }


class Collector:
    def __init__(self, cfg: Config, storage: Storage) -> None:
        self.cfg = cfg
        self.storage = storage
        self.session: Optional[aiohttp.ClientSession] = None
        self.runtimes: Dict[str, InstrumentRuntime] = {}
        self._rest: Dict[str, RestClient] = {}
        self.tasks: List[asyncio.Task] = []
        self.listeners: List[Callable[[str, Dict[str, Any]], None]] = []
        self.started_at = now_ms()
        self.tick_count = 0
        self.tick_lag_ms: Deque[float] = deque(maxlen=120)
        self.flush_ms_hist: Deque[float] = deque(maxlen=120)

    def rest(self, key: str, base: str, budget: int, header: Optional[str]) -> RestClient:
        if key not in self._rest:
            assert self.session is not None
            self._rest[key] = RestClient(self.session, base, budget, header, key)
        return self._rest[key]

    async def start(self) -> None:
        # trust_env: honour HTTP(S)_PROXY / NO_PROXY from the environment
        self.session = aiohttp.ClientSession(headers={"User-Agent": "FlowLens/1.0 (market data terminal)"}, trust_env=True)
        for icfg in self.cfg.instrument_cfgs():
            rt = InstrumentRuntime(self, icfg)
            self.runtimes[rt.inst.key] = rt
            self.tasks.append(asyncio.ensure_future(rt.start()))
        self.tasks.append(asyncio.ensure_future(self._tick_loop()))
        self.tasks.append(asyncio.ensure_future(self._flush_loop()))
        self.tasks.append(asyncio.ensure_future(self._purge_loop()))

    async def stop(self) -> None:
        for rt in self.runtimes.values():
            rt.stop()
            if rt.pipeline:
                rt.pipeline.flush_depth()
                self.flush_runtime(rt)
        for t in self.tasks:
            t.cancel()
        if self.session:
            await self.session.close()

    # ---- clock ---------------------------------------------------------------------
    async def _tick_loop(self) -> None:
        period = self.cfg.sample_ms / 1000.0
        while True:
            now = time.time()
            nxt = (math.floor(now / period) + 1) * period
            await asyncio.sleep(nxt - now)
            t = int(round(nxt * 1000))
            self.tick_lag_ms.append((time.time() - nxt) * 1000)
            self.tick_count += 1
            for rt in self.runtimes.values():
                if rt.pipeline is None or rt.generating:
                    continue
                rt.record("tick", t, None)
                rt.pipeline.on_tick(t)
                if rt.book.epoch and len(rt.book.bids) + len(rt.book.asks) > 60000:
                    rt.book.prune(20000)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.flush_ms / 1000.0)
            t0 = time.perf_counter()
            for rt in self.runtimes.values():
                if rt.pipeline is not None and not rt.generating:
                    try:
                        self.flush_runtime(rt)
                    except Exception as e:
                        rt.error(f"flush: {e}")
                        log.exception("flush failed")
            self.flush_ms_hist.append((time.perf_counter() - t0) * 1000)

    def flush_runtime(self, rt: InstrumentRuntime) -> None:
        p = rt.pipeline
        if p is None:
            return
        rows = p.take_dirty_candles()
        if rows:
            rt.storage.upsert_candles(rt.iid, rows)
            rt.storage.upsert_footprints(rt.iid, p.take_footprints([r["t"] for r in rows]))
        closed = p.take_closed_depth()
        for drow, hrow in closed:
            rt.storage.upsert_depth(rt.iid, drow)
            if hrow is not None:
                rt.storage.upsert_heat(rt.iid, hrow, p.heat_step)
        drv = p.take_dirty_deriv()
        if drv:
            rt.storage.upsert_deriv(rt.iid, drv)
        if rt.recorder:
            rt.recorder.flush()
        p.evict(now_ms())
        if closed and not rt.generating:
            last = closed[-1][0]["t"]
            if rt.last_closed_minute != last:
                rt.last_closed_minute = last
                for fn in self.listeners:
                    fn("bar_closed", {"inst": rt.inst.key, "t": last})

    async def _purge_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                self.storage.purge(self.cfg.retention_days)
            except Exception as e:  # pragma: no cover
                log.warning("purge failed: %s", e)

    # ---- metrics ---------------------------------------------------------------------
    def health(self) -> Dict[str, Any]:
        rss = cpu = None
        if resource is not None:
            ru = resource.getrusage(resource.RUSAGE_SELF)
            rss, cpu = round(ru.ru_maxrss / 1024, 1), round(ru.ru_utime + ru.ru_stime, 1)
        lag = sorted(self.tick_lag_ms)
        fl = sorted(self.flush_ms_hist)
        return {
            "uptime_s": (now_ms() - self.started_at) / 1000,
            "rss_max_mb": rss,
            "cpu_s": cpu,
            "tick_lag_ms_p50": round(lag[len(lag) // 2], 2) if lag else None,
            "tick_lag_ms_max": round(lag[-1], 2) if lag else None,
            "flush_ms_p50": round(fl[len(fl) // 2], 2) if fl else None,
            "flush_ms_max": round(fl[-1], 2) if fl else None,
            "rest": {k: {"requests": c.requests, "errors": c.errors, "server_weight": c.server_used}
                     for k, c in self._rest.items()},
            "instruments": {k: rt.live_status() for k, rt in self.runtimes.items()},
        }
