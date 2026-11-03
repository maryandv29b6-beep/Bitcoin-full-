"""Bybit v5 adapter (spot and linear perpetual), public data only.

  WS   wss://stream.bybit.com/v5/public/spot | /v5/public/linear
       topics orderbook.{depth}.{symbol} (snapshot + delta), publicTrade.{symbol},
       tickers.{symbol}, liquidation.{symbol} (linear)
  REST https://api.bybit.com/v5/market/...

Differences from Binance that matter here:
  * the book stream is a top-N book; depth beyond N levels is not available,
    so wide bands are reported as not covered (null) rather than understated;
  * publicTrade carries the taker side directly (S = Buy/Sell); trade ids are
    strings, so trade-stream continuity cannot be verified by sequence;
  * klines have volume and turnover but no taker-buy split, so candle history
    from klines has no buy/sell volumes (null) - delta exists only from launch;
  * liquidation side: 'Buy' means a long position was liquidated (per docs).
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from .. import numeric
from ..models import BUY, SELL, UNKNOWN, Liquidation, Trade
from .base import AdapterBase, RestClient, WsConn, now_ms


def parse_public_trade(d: dict, recv: int) -> Trade:
    s = d.get("S")
    return Trade(trade_id=d.get("i"), seq=None, t_exch=int(d["T"]), t_recv=recv, price=numeric.parse(d["p"]),
                 qty=numeric.parse(d["v"]), side=BUY if s == "Buy" else SELL if s == "Sell" else UNKNOWN,
                 count=1, granularity="raw")


def kline_row(k: list) -> dict:
    return {"t": int(k[0]), "o": numeric.parse(k[1]), "h": numeric.parse(k[2]), "l": numeric.parse(k[3]),
            "c": numeric.parse(k[4]), "v_buy": None, "v_sell": None, "v_unknown": numeric.parse(k[5]),
            "q_buy": None, "q_sell": None, "q_unknown": float(k[6]), "n_buy": None, "n_sell": None,
            "n_unknown": None, "n_total": None, "first_id": None, "last_id": None, "source": "kline",
            "complete": 1, "gap_count": 0}


class BybitAdapter(AdapterBase):
    exchange = "bybit"

    def __init__(self, rt) -> None:
        super().__init__(rt)
        cfg = rt.cfg
        self.spot = rt.inst.market == "spot"
        self.category = "spot" if self.spot else "linear"
        self.sym = rt.inst.symbol
        self.depth = rt.icfg.depth or (200 if self.spot else 500)
        self.rest = rt.shared_rest("bybit", cfg.bybit_rest, 400, None)
        self.conn = WsConn(rt, f"bybit-{self.category}", lambda: f"{cfg.bybit_ws}/v5/public/{self.category}",
                           self._on_open, self._on_message, self._on_close,
                           app_ping=lambda: {"op": "ping"}, app_ping_s=20.0)
        self.conns = [self.conn]

    async def describe(self) -> Dict[str, Any]:
        d = await self.rest.get("/v5/market/instruments-info", {"category": self.category, "symbol": self.sym})
        it = d["result"]["list"][0]
        lot = it.get("lotSizeFilter", {})
        step = lot.get("qtyStep") or lot.get("basePrecision")
        out = {"base": it["baseCoin"], "quote": it["quoteCoin"], "tick": numeric.parse(it["priceFilter"]["tickSize"]),
               "step": numeric.parse(step), "contract": {}}
        if not self.spot:
            ct = it.get("contractType")
            if ct != "LinearPerpetual":
                raise ValueError(f"{self.sym}: unsupported contract type {ct}")
            out["contract"] = {"type": "linear", "contractType": ct, "settleCoin": it.get("settleCoin"),
                               "quantityUnit": "base", "multiplier": "1",
                               "fundingIntervalMin": it.get("fundingInterval")}
            if it.get("fundingInterval"):
                self.rt.deriv_state.funding_interval_h = int(it["fundingInterval"]) / 60
        return out

    async def run(self) -> None:
        self.spawn(self._backfill_klines())
        if not self.spot:
            self.spawn(self._deriv_backfill())
        self.spawn(self._maintenance_loop())
        await self.conn.run()

    async def _on_open(self, ws) -> None:
        self.rt.reset_sync()
        self.rt.set_book_status("CONNECTING")
        topics = [f"orderbook.{self.depth}.{self.sym}", f"publicTrade.{self.sym}", f"tickers.{self.sym}"]
        if not self.spot:
            topics.append(f"liquidation.{self.sym}")
        await ws.send_json({"op": "subscribe", "args": topics})
        self.rt.quality("connect", self.conn.name)
        self.rt.trade_stream_started(now_ms())

    def _on_close(self) -> None:
        self.rt.reset_sync()
        self.rt.set_book_status("CONNECTING")
        self.rt.quality("disconnect", self.conn.name)

    def _on_message(self, msg: Any, recv: int) -> None:
        topic = msg.get("topic", "")
        if topic.startswith("orderbook."):
            self.rt.record("depth", recv, msg)
            self.rt.handle_sync(self.rt.sync.on_message(msg), recv)
            self.rt.note_exchange_time(msg.get("cts") or msg.get("ts"), recv, "book")
        elif topic.startswith("publicTrade."):
            for d in msg.get("data") or []:
                self.rt.record("trade", recv, d)
                self.rt.on_trade(parse_public_trade(d, recv))
            self.rt.note_exchange_time(msg.get("ts"), recv, "trades")
        elif topic.startswith("tickers."):
            d = msg.get("data") or {}
            tk = dict(self.rt.ticker or {})
            if d.get("lastPrice"):
                tk["last"] = float(d["lastPrice"])
            if d.get("price24hPcnt"):
                tk["change_pct"] = float(d["price24hPcnt"]) * 100
            if d.get("turnover24h"):
                tk["volume_quote"] = float(d["turnover24h"])
            if d.get("volume24h"):
                tk["volume_base"] = float(d["volume24h"])
            tk["t"] = recv
            tk["source"] = "exchange 24h ticker"
            self.rt.ticker = tk
            if not self.spot:
                self.rt.on_deriv(recv,
                                 oi=float(d["openInterest"]) if d.get("openInterest") else None,
                                 oi_value=float(d["openInterestValue"]) if d.get("openInterestValue") else None,
                                 funding=float(d["fundingRate"]) if d.get("fundingRate") else None,
                                 next_funding=int(d["nextFundingTime"]) if d.get("nextFundingTime") else None,
                                 mark=float(d["markPrice"]) if d.get("markPrice") else None)
        elif topic.startswith("liquidation."):
            d = msg.get("data") or {}
            try:
                liq = Liquidation(t_exch=int(d.get("updateTime") or recv), t_recv=recv,
                                  side="long" if d.get("side") == "Buy" else "short",
                                  price=numeric.parse(d["price"]), qty=numeric.parse(d["size"]))
                self.rt.pipeline.on_liquidation(liq)
            except (KeyError, ValueError):
                pass

    def request_snapshot(self, refresh: bool = False) -> None:
        """Bybit pushes snapshots itself; a forced resync is a reconnect."""
        if self.conn.ws is not None:
            asyncio.ensure_future(self.conn.ws.close())

    async def _fetch_klines(self, start: int, end: int) -> List[dict]:
        out: List[dict] = []
        cur_end = end - 1
        while cur_end > start:
            d = await self.rest.get("/v5/market/kline", {"category": self.category, "symbol": self.sym, "interval": "1",
                                                          "start": start, "end": cur_end, "limit": 1000})
            rows = d.get("result", {}).get("list") or []
            if not rows:
                break
            out += [kline_row(k) for k in rows]
            oldest = min(int(k[0]) for k in rows)
            if oldest <= start or len(rows) < 1000:
                break
            cur_end = oldest - 1
        out.sort(key=lambda r: r["t"])
        return out

    async def _backfill_klines(self) -> None:
        end = now_ms()
        end -= end % 60000
        start = end - int(self.rt.cfg.kline_backfill_days * 86400_000)
        try:
            rows = await self._fetch_klines(start, end)
            n = self.rt.storage.merge_kline_candles(self.rt.iid, rows)
            self.rt.quality("kline_backfill", f"{len(rows)} klines, {n} inserted (без разбивки buy/sell)")
        except Exception as e:
            self.rt.error(f"klines backfill: {e}")

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            mins = self.rt.incomplete_closed_minutes()
            if mins:
                try:
                    rows = await self._fetch_klines(min(mins), max(mins) + 60000)
                    want = set(mins)
                    # Bybit klines carry no buy/sell split: only OHLC/volume of incomplete minutes is reconciled
                    self.rt.storage.merge_kline_candles(self.rt.iid, [r for r in rows if r["t"] in want])
                except Exception as e:
                    self.rt.error(f"reconcile: {e}")

    async def _deriv_backfill(self) -> None:
        try:
            d = await self.rest.get("/v5/market/open-interest", {"category": "linear", "symbol": self.sym,
                                                                  "intervalTime": "5min", "limit": 200})
            for r in d.get("result", {}).get("list") or []:
                self.rt.pipeline.on_deriv(int(r["timestamp"]), oi=float(r["openInterest"]))
        except Exception as e:
            self.rt.error(f"open-interest: {e}")
        try:
            d = await self.rest.get("/v5/market/funding/history", {"category": "linear", "symbol": self.sym, "limit": 200})
            for r in d.get("result", {}).get("list") or []:
                self.rt.pipeline.on_deriv(int(r["fundingRateTimestamp"]), funding=float(r["fundingRate"]))
        except Exception as e:
            self.rt.error(f"funding history: {e}")
