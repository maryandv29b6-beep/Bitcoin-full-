"""Binance Spot and USD-M Futures adapter.

Sources (public market data only, no API key):
  Spot     REST https://api.binance.com/api/v3, WS wss://stream.binance.com:9443/stream
  USD-M    REST https://fapi.binance.com/fapi/v1, WS routed /public (depth) and /market
           (aggTrade, markPrice, forceOrder) since the 2026 WebSocket upgrade.

Aggressor side: aggTrade field ``m`` = "is the buyer the market maker".
m = true -> the seller was the taker (aggressive SELL); m = false -> BUY.
One aggTrade record aggregates the fills of one taker order at one price;
``l - f + 1`` is the number of underlying trades.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from .. import numeric
from ..models import BUY, SELL, Liquidation, Trade
from .base import AdapterBase, RateLimited, RestClient, WsConn, now_ms


def parse_agg_trade(d: dict, recv: int) -> Trade:
    return Trade(trade_id=d["a"], seq=d["a"], t_exch=d["T"], t_recv=recv, price=numeric.parse(d["p"]),
                 qty=numeric.parse(d["q"]), side=SELL if d["m"] else BUY,
                 count=max(1, d["l"] - d["f"] + 1), granularity="aggregate")


def kline_row(k: list) -> dict:
    vol = numeric.parse(k[5])
    tb = numeric.parse(k[9])
    qv = float(k[7])
    tq = float(k[10])
    return {"t": int(k[0]), "o": numeric.parse(k[1]), "h": numeric.parse(k[2]), "l": numeric.parse(k[3]),
            "c": numeric.parse(k[4]), "v_buy": tb, "v_sell": vol - tb, "v_unknown": 0, "q_buy": tq,
            "q_sell": qv - tq, "q_unknown": 0.0, "n_buy": None, "n_sell": None, "n_unknown": None,
            "n_total": int(k[8]), "first_id": None, "last_id": None, "source": "kline", "complete": 1,
            "gap_count": 0}


def parse_force_order(d: dict, recv: int) -> Optional[Liquidation]:
    o = d.get("o") or {}
    try:
        qty = numeric.parse(o.get("z") or o.get("q") or "0")
        price = numeric.parse(o.get("ap") or o.get("p") or "0")
    except ValueError:
        return None
    if not qty:
        return None
    # the liquidation order SELLs a long position / BUYs back a short position
    side = "long" if o.get("S") == "SELL" else "short"
    return Liquidation(t_exch=int(o.get("T") or d.get("E") or recv), t_recv=recv, side=side, price=price, qty=qty)


class BinanceAdapter(AdapterBase):
    exchange = "binance"
    supports_seq_trades = True

    def __init__(self, rt) -> None:
        super().__init__(rt)
        cfg = rt.cfg
        self.spot = rt.inst.market == "spot"
        self.sym = rt.inst.symbol
        lo = self.sym.lower()
        if self.spot:
            self.rest = rt.shared_rest("binance_spot", cfg.binance_spot_rest, int(cfg.binance_weight_limit * 0.7),
                                       "X-MBX-USED-WEIGHT-1M")
            self.p = "/api/v3"
            self.snapshot_limit = rt.icfg.depth or 5000
            self.snapshot_weight = 250 if self.snapshot_limit > 1000 else 50 if self.snapshot_limit > 500 else 25
            self.depth_conn = WsConn(rt, "spot-ws", lambda: f"{cfg.binance_spot_ws}/stream?streams={lo}@depth@100ms/{lo}@aggTrade",
                                     self._on_open, self._on_message, self._on_close)
            self.conns = [self.depth_conn]
        else:
            self.rest = rt.shared_rest("binance_perp", cfg.binance_perp_rest, 1600, "X-MBX-USED-WEIGHT-1M")
            self.p = "/fapi/v1"
            self.snapshot_limit = rt.icfg.depth or 1000
            self.snapshot_weight = 20
            self.depth_conn = WsConn(rt, "perp-public", lambda: f"{cfg.binance_perp_ws_public}/stream?streams={lo}@depth@100ms",
                                     self._on_open, self._on_message, self._on_close)
            self.market_conn = WsConn(rt, "perp-market",
                                      lambda: f"{cfg.binance_perp_ws_market}/stream?streams={lo}@aggTrade/{lo}@markPrice@1s/{lo}@forceOrder",
                                      self._on_open_market, self._on_message, self._on_close_market)
            self.conns = [self.depth_conn, self.market_conn]
        self.trade_started = False

    # ---- metadata --------------------------------------------------------
    async def describe(self) -> Dict[str, Any]:
        if self.spot:
            info = await self.rest.get(f"{self.p}/exchangeInfo", {"symbol": self.sym}, weight=20)
        else:
            info = await self.rest.get(f"{self.p}/exchangeInfo", None, weight=1)
        s = next(x for x in info["symbols"] if x["symbol"] == self.sym)
        f = {x["filterType"]: x for x in s.get("filters", [])}
        out = {"base": s["baseAsset"], "quote": s["quoteAsset"], "tick": numeric.parse(f["PRICE_FILTER"]["tickSize"]),
               "step": numeric.parse(f["LOT_SIZE"]["stepSize"]), "contract": {}}
        if not self.spot:
            if s.get("contractType") != "PERPETUAL":
                raise ValueError(f"{self.sym} is not a USD-M perpetual")
            out["contract"] = {"type": "linear", "contractType": "PERPETUAL", "marginAsset": s.get("marginAsset"),
                               "quantityUnit": "base", "multiplier": "1"}
        return out

    # ---- lifecycle -------------------------------------------------------------
    async def run(self) -> None:
        self.spawn(self._backfill_klines())
        if not self.spot:
            self.spawn(self._deriv_backfill())
            self.spawn(self._oi_loop())
        self.spawn(self._ticker_loop())
        self.spawn(self._maintenance_loop())
        await asyncio.gather(*(c.run() for c in self.conns))

    async def _on_open(self, ws) -> None:
        self.rt.reset_sync()
        self.rt.set_book_status("CONNECTING")
        self.rt.quality("connect", self.depth_conn.name)

    def _on_close(self) -> None:
        self.rt.reset_sync()
        self.rt.set_book_status("CONNECTING")
        self.rt.quality("disconnect", self.depth_conn.name)

    async def _on_open_market(self, ws) -> None:
        self.rt.quality("connect", self.market_conn.name)

    def _on_close_market(self) -> None:
        self.rt.quality("disconnect", self.market_conn.name)

    def _on_message(self, msg: Any, recv: int) -> None:
        stream = msg.get("stream", "")
        d = msg.get("data") or {}
        e = d.get("e")
        if e == "depthUpdate":
            self.rt.record("depth", recv, d)
            self.rt.handle_sync(self.rt.sync.on_message(d), recv)
            self.rt.note_exchange_time(d.get("E"), recv, "book")
        elif e == "aggTrade":
            tr = parse_agg_trade(d, recv)
            if not self.trade_started:
                self.trade_started = True
                self.rt.trade_stream_started(tr.t_exch)
            self.rt.record("trade", recv, d)
            self.rt.note_exchange_time(d.get("E"), recv, "trades")
            gap = self.rt.on_trade(tr)
            if gap:
                self.spawn(self._backfill_trades(*gap))
        elif e == "markPriceUpdate":
            self.rt.on_deriv(recv, funding=float(d["r"]) if d.get("r") not in (None, "") else None,
                             next_funding=int(d["T"]) if d.get("T") else None, mark=float(d["p"]))
        elif e == "forceOrder":
            liq = parse_force_order(d, recv)
            if liq:
                self.rt.pipeline.on_liquidation(liq)
        elif e == "serverShutdown" or stream == "!serverShutdown":
            self.rt.quality("disconnect", "serverShutdown")

    # ---- snapshots -------------------------------------------------------------
    def request_snapshot(self, refresh: bool = False) -> None:
        if self._snapshot_task and not self._snapshot_task.done():
            return
        self._snapshot_task = self.spawn(self._snapshot(refresh))

    async def _snapshot(self, refresh: bool) -> None:
        delay = 0.5
        for attempt in range(8):
            await asyncio.sleep(delay)
            try:
                snap = await self.rest.get(f"{self.p}/depth", {"symbol": self.sym, "limit": self.snapshot_limit},
                                           weight=self.snapshot_weight)
            except Exception as e:
                self.rt.error(f"depth snapshot: {e}")
                delay = min(delay * 2, 30)
                continue
            recv = now_ms()
            self.rt.record("snap_refresh" if refresh else "snap", recv, snap)
            if refresh:
                self.rt.handle_sync(self.rt.sync.on_refresh_snapshot(snap), recv)
                return
            actions = self.rt.sync.on_snapshot(snap)
            if "need_snapshot" in actions and "gap" not in actions:
                delay = min(delay * 2, 10)
                continue  # snapshot older than the buffer: fetch again
            self.rt.handle_sync([a for a in actions if a != "need_snapshot"], recv)
            if "need_snapshot" in actions:
                delay = 0.5
                continue
            return

    # ---- trades backfill ---------------------------------------------------------
    async def _backfill_trades(self, lo: int, hi: int) -> None:
        self.rt.quality("trade_gap", f"aggTrade id {lo}..{hi}")
        cur = lo
        touched = set()
        try:
            while cur <= hi:
                rows = await self.rest.get(f"{self.p}/aggTrades", {"symbol": self.sym, "fromId": cur, "limit": 1000},
                                           weight=4 if self.spot else 20)
                if not rows:
                    break
                recv = now_ms()
                for d in rows:
                    if d["a"] > hi:
                        break
                    tr = parse_agg_trade(d, recv)
                    touched.add(tr.t_exch - tr.t_exch % 60000)
                    self.rt.on_trade(tr, backfill=True)
                cur = rows[-1]["a"] + 1
                if len(rows) < 1000:
                    break
            self.rt.pipeline.gap_filled(lo, hi, sorted(touched) or [])
            self.rt.quality("trade_gap_filled", f"aggTrade id {lo}..{hi}")
        except Exception as e:
            self.rt.error(f"aggTrades backfill {lo}..{hi}: {e}")

    # ---- klines ------------------------------------------------------------------
    async def _fetch_klines(self, start: int, end: int) -> List[dict]:
        out = []
        cur = start
        limit = 1000 if self.spot else 1500
        while cur < end:
            rows = await self.rest.get(f"{self.p}/klines", {"symbol": self.sym, "interval": "1m", "startTime": cur,
                                                             "endTime": end - 1, "limit": limit},
                                       weight=2 if self.spot else 10)
            if not rows:
                break
            out += [kline_row(k) for k in rows]
            cur = int(rows[-1][0]) + 60000
            if len(rows) < limit:
                break
        return out

    async def _backfill_klines(self) -> None:
        end = now_ms()
        end -= end % 60000  # closed minutes only
        start = end - int(self.rt.cfg.kline_backfill_days * 86400_000)
        try:
            rows = await self._fetch_klines(start, end)
            n = self.rt.storage.merge_kline_candles(self.rt.iid, rows)
            self.rt.quality("kline_backfill", f"{len(rows)} klines, {n} inserted")
        except Exception as e:
            self.rt.error(f"klines backfill: {e}")

    async def reconcile(self, minutes: List[int]) -> None:
        """Replace incomplete trade-built minutes with exchange klines."""
        if not minutes:
            return
        try:
            rows = await self._fetch_klines(min(minutes), max(minutes) + 60000)
            want = set(minutes)
            n = self.rt.storage.merge_kline_candles(self.rt.iid, [r for r in rows if r["t"] in want])
            if n:
                self.rt.quality("reconcile", f"{n} неполных минут заменены свечами биржи")
        except Exception as e:
            self.rt.error(f"reconcile: {e}")

    # ---- periodic ----------------------------------------------------------------
    async def _maintenance_loop(self) -> None:
        last_refresh = 0.0
        while True:
            await asyncio.sleep(60)
            await self.reconcile(self.rt.incomplete_closed_minutes())
            # coverage refresh (spot only, snapshot limit 5000 may not reach 1%)
            if self.spot and time.monotonic() - last_refresh > self.rt.cfg.coverage_refresh_min_s:
                if self.rt.coverage_low():
                    acts = self.rt.sync.request_refresh()
                    if "need_refresh" in acts:
                        last_refresh = time.monotonic()
                        self.request_snapshot(refresh=True)

    async def _ticker_loop(self) -> None:
        while True:
            try:
                d = await self.rest.get(f"{self.p}/ticker/24hr", {"symbol": self.sym}, weight=2 if self.spot else 1)
                self.rt.ticker = {"last": float(d["lastPrice"]), "change_pct": float(d["priceChangePercent"]),
                                  "volume_base": float(d["volume"]), "volume_quote": float(d["quoteVolume"]),
                                  "t": now_ms(), "source": "exchange 24h ticker"}
            except Exception as e:
                self.rt.error(f"ticker: {e}")
            await asyncio.sleep(60)

    async def _oi_loop(self) -> None:
        while True:
            try:
                d = await self.rest.get(f"{self.p}/openInterest", {"symbol": self.sym}, weight=1)
                oi = float(d["openInterest"])
                t = int(d.get("time") or now_ms())
                mark = self.rt.deriv_state.mark_price
                self.rt.on_deriv(t, oi=oi, oi_value=oi * mark if mark else None)
            except Exception as e:
                self.rt.error(f"openInterest: {e}")
            await asyncio.sleep(30)

    async def _deriv_backfill(self) -> None:
        try:
            rows = await self.rest.get("/futures/data/openInterestHist",
                                       {"symbol": self.sym, "period": "5m", "limit": 500}, weight=1)
            for r in rows:
                self.rt.pipeline.on_deriv(int(r["timestamp"]), oi=float(r["sumOpenInterest"]),
                                          oi_value=float(r["sumOpenInterestValue"]))
        except Exception as e:
            self.rt.error(f"openInterestHist: {e}")
        try:
            rows = await self.rest.get(f"{self.p}/fundingRate", {"symbol": self.sym, "limit": 100}, weight=1)
            times = [int(r["fundingTime"]) for r in rows]
            for r in rows:
                self.rt.pipeline.on_deriv(int(r["fundingTime"]), funding=float(r["fundingRate"]))
            if len(times) >= 2:
                self.rt.deriv_state.funding_interval_h = round((times[-1] - times[-2]) / 3600_000, 2)
        except Exception as e:
            self.rt.error(f"fundingRate: {e}")
