"""HTTP + WebSocket API of the FlowLens server (our own routes, not exchange URLs).

Every data response carries units, source, time range, validity and the
algorithm version.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import struct
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import WSMsgType, web

from . import __version__
from . import indicators as ind
from .alerts import METRICS, OPS, AlertEngine, Telegram, validate_rule
from .collector import Collector
from .numeric import NOTIONAL_SCALE, SCALE, fmt
from .pipeline import MIN_MS, minute_of
from .screener import Screener, sample_metrics
from .series import (BASE_MS, DEFAULT_PARAMS, TF_MS, aggregate_candles, aggregate_deriv, aggregate_depth,
                     clean, compute_indicators, grid)
from .storage import (q_candles, q_deriv, q_depth, q_first, q_footprint, q_heat, q_quality, q_sizes)

log = logging.getLogger("flowlens.api")


def jresp(data: Any, status: int = 200) -> web.Response:
    return web.json_response(clean(data), status=status, dumps=lambda o: json.dumps(o, separators=(",", ":")))


def err(msg: str, status: int = 400) -> web.Response:
    return jresp({"error": msg}, status)


class Api:
    def __init__(self, collector: Collector, static_dir: Optional[str]) -> None:
        self.c = collector
        self.storage = collector.storage
        self.static_dir = Path(static_dir) if static_dir else None
        self.screener = Screener(collector)
        self.telegram = Telegram(self.storage, lambda: collector.session)
        self.alerts = AlertEngine(self.storage, self.screener, self.telegram)
        self.clients: set = set()
        self.bucket_cache: Dict[Tuple[str, str], Tuple[int, Dict[str, Any]]] = {}
        collector.listeners.append(self._on_event)
        self.alerts.listeners.append(self._on_event)

    # ---- helpers -------------------------------------------------------------
    def _rt(self, request: web.Request):
        key = request.query.get("inst")
        rt = self.c.runtimes.get(key or "")
        if rt is None or rt.iid < 0:
            raise web.HTTPNotFound(text=json.dumps({"error": f"unknown or not ready instrument {key}"}),
                                   content_type="application/json")
        return rt

    @staticmethod
    def _range(request: web.Request, tf: int) -> Tuple[int, int]:
        now = int(time.time() * 1000)
        t1 = int(request.query.get("to") or now + 1)   # exclusive; includes the forming candle
        t0 = int(request.query.get("from") or t1 - 86400_000)
        if t1 <= t0:
            raise web.HTTPBadRequest(text='{"error":"empty range"}', content_type="application/json")
        if (t1 - t0) / tf > 20000:
            raise web.HTTPBadRequest(text='{"error":"range too large for timeframe"}', content_type="application/json")
        return t0, t1

    @staticmethod
    def _tf(request: web.Request) -> Tuple[str, int]:
        name = request.query.get("tf", "5m")
        if name not in TF_MS:
            raise web.HTTPBadRequest(text='{"error":"bad tf"}', content_type="application/json")
        return name, TF_MS[name]

    @staticmethod
    def _params(request: web.Request) -> Dict[str, Any]:
        raw = request.query.get("params")
        if not raw:
            return {}
        try:
            p = json.loads(raw)
        except ValueError:
            raise web.HTTPBadRequest(text='{"error":"bad params"}', content_type="application/json")
        return {k: v for k, v in p.items() if k in DEFAULT_PARAMS}

    def _availability(self, rt, first: Dict[str, Optional[int]]) -> Dict[str, Any]:
        return {"candles_from": first.get("candles_1m"), "trades_from": first.get("trades"),
                "book_from": first.get("depth_1m"), "heatmap_from": first.get("heatmap_1m"),
                "footprint_from": first.get("footprint_1m"), "deriv_from": first.get("deriv_1m"),
                "note": "История стакана собирается с момента запуска коллектора; биржа не отдаёт её задним числом."}

    async def _series(self, rt, tf: int, t0: int, t1: int, params: Dict[str, Any]) -> Dict[str, Any]:
        p = {**DEFAULT_PARAMS, **params}
        warm = max(BASE_MS[p["z_base"]], (int(p["anom_window"]) + 2) * tf,
                   (int(p["macd_slow"]) + int(p["macd_signal"]) + 2) * tf, (int(p["rsi_period"]) * 4) * tf)
        warm = min(warm, 15 * 86400_000)
        w0 = t0 - t0 % tf - warm
        times = grid(w0, t1, tf)
        crow = await self.storage.run(q_candles, rt.iid, w0, t1)
        drow = await self.storage.run(q_depth, rt.iid, w0, t1)
        cur = rt.pipeline.current_depth_row() if rt.pipeline else None
        if cur and w0 <= cur["t"] < t1:
            drow = [r for r in drow if r["t"] != cur["t"]] + [cur]
        dv = None
        if rt.inst.market == "perp":
            dv = aggregate_deriv(await self.storage.run(q_deriv, rt.iid, w0, t1), times, tf)
        c = aggregate_candles(crow, times, tf)
        d = aggregate_depth(drow, times, tf, float(p["book_min_time_cov"]))
        now = int(time.time() * 1000)
        live_index = len(times) - 1 if times and times[-1] <= now < times[-1] + tf else None
        indic = compute_indicators(c, d, dv, tf, p, live_index)
        # trim to the requested view
        k0 = next((i for i, t in enumerate(times) if t >= t0 - t0 % tf), len(times))

        def trim(o: Any) -> Any:
            if isinstance(o, list):
                if len(o) == len(times):
                    return o[k0:]
                return [trim(v) for v in o]   # e.g. belts.items: list of per-belt dicts
            if isinstance(o, dict):
                return {k: trim(v) for k, v in o.items()}
            return o

        peaks = [q for q in indic["anomaly"].pop("peaks") if q["t"] >= times[k0]] if k0 < len(times) else []
        out_c = trim(c)
        out_d = trim({k: v for k, v in d.items() if k != "bands"})
        out_i = trim(indic)
        out_i["anomaly"]["peaks"] = peaks
        return {"t": times[k0:], "candles": out_c, "depth": out_d, "indicators": out_i,
                "live_index": (live_index - k0) if live_index is not None and live_index >= k0 else None}

    # ---- routes ----------------------------------------------------------------
    async def meta(self, request: web.Request) -> web.Response:
        return jresp({"name": "FlowLens", "version": __version__, "algorithm": ind.ALGORITHM_VERSION,
                      "demo": self.c.cfg.demo, "timeframes": list(TF_MS), "bases": list(BASE_MS),
                      "defaults": DEFAULT_PARAMS, "alert_metrics": METRICS, "alert_ops": OPS,
                      "time": int(time.time() * 1000)})

    async def instruments(self, request: web.Request) -> web.Response:
        out = []
        for key, rt in self.c.runtimes.items():
            d = rt.inst.public()
            d["status"] = rt.status()
            d["ready"] = rt.iid >= 0
            d["ticker"] = rt.ticker
            d["last_price"] = rt.pipeline.last_trade.price / SCALE if rt.pipeline and rt.pipeline.last_trade else None
            d["steps"] = {"footprint": fmt(int(rt.meta.get("footprint_step", 0))) if rt.meta else None,
                          "heatmap": fmt(int(rt.meta.get("heatmap_step", 0))) if rt.meta else None}
            d["sync_rules"] = rt.sync.name
            out.append(d)
        return jresp({"instruments": out})

    async def chart(self, request: web.Request) -> web.Response:
        rt = self._rt(request)
        tf_name, tf = self._tf(request)
        t0, t1 = self._range(request, tf)
        params = self._params(request)
        s = await self._series(rt, tf, t0, t1, params)
        first = await self.storage.run(q_first, rt.iid)
        quality = await self.storage.run(q_quality, rt.iid, t0, t1)
        return jresp({"inst": rt.inst.key, "tf": tf_name, "from": t0, "to": t1, "status": rt.status(),
                      "source": {"exchange": rt.inst.exchange, "market": rt.inst.market, "sync_rules": rt.sync.name,
                                 "demo": rt.inst.is_demo},
                      "units": {"price": rt.inst.quote, "base": rt.inst.base, "quote": rt.inst.quote},
                      "availability": self._availability(rt, first), "quality": quality, **s})

    async def candles(self, request: web.Request) -> web.Response:
        rt = self._rt(request)
        tf_name, tf = self._tf(request)
        t0, t1 = self._range(request, tf)
        times = grid(t0, t1, tf)
        c = aggregate_candles(await self.storage.run(q_candles, rt.iid, t0, t1), times, tf)
        return jresp({"inst": rt.inst.key, "tf": tf_name, "from": t0, "to": t1,
                      "units": {"price": rt.inst.quote, "volume": rt.inst.base, "notional": rt.inst.quote},
                      "candles": c})

    async def depth_history(self, request: web.Request) -> web.Response:
        rt = self._rt(request)
        tf_name, tf = self._tf(request)
        t0, t1 = self._range(request, tf)
        times = grid(t0, t1, tf)
        d = aggregate_depth(await self.storage.run(q_depth, rt.iid, t0, t1), times, tf,
                            float(self._params(request).get("book_min_time_cov", DEFAULT_PARAMS["book_min_time_cov"])))
        return jresp({"inst": rt.inst.key, "tf": tf_name, "from": t0, "to": t1,
                      "units": {"qty": rt.inst.base, "notional": rt.inst.quote},
                      "bands_definition": "cumulative ranges B_r = bids in [mid*(1-r), mid], A_r = asks in [mid, mid*(1+r)]",
                      "depth": d})

    async def indicators(self, request: web.Request) -> web.Response:
        rt = self._rt(request)
        tf_name, tf = self._tf(request)
        t0, t1 = self._range(request, tf)
        s = await self._series(rt, tf, t0, t1, self._params(request))
        names = [x for x in (request.query.get("names") or "").split(",") if x]
        ind_out = s["indicators"]
        if names:
            ind_out = {k: v for k, v in ind_out.items() if k in names or k in ("params", "params_hash", "algorithm", "units")}
        return jresp({"inst": rt.inst.key, "tf": tf_name, "t": s["t"], "live_index": s["live_index"], "indicators": ind_out})

    async def heatmap(self, request: web.Request) -> web.Response:
        rt = self._rt(request)
        tf_name, tf = self._tf(request)
        t0, t1 = self._range(request, tf)
        agg = request.query.get("agg", "mean")
        rows = await self.storage.run(q_heat, rt.iid, t0 - t0 % tf, t1)
        cur = rt.pipeline.current_heat_row() if rt.pipeline else None
        if cur is not None and t0 - t0 % tf <= cur["t"] < t1 and not any(r["t"] == cur["t"] for r in rows):
            rows.append({**cur, "step": rt.pipeline.heat_step})
        base_step = int(rt.meta["heatmap_step"])
        k = max(1, int(round(float(request.query.get("step") or 0) * SCALE / base_step))) if request.query.get("step") else 1
        times = grid(t0, t1, tf)
        if not rows:
            return jresp({"inst": rt.inst.key, "tf": tf_name, "t": times, "rows": 0, "step": fmt(base_step * k),
                          "agg": agg, "units": rt.inst.base, "empty": True})
        lo = min(r["origin"] for r in rows) // k
        hi = max((r["origin"] + len(r["bid_mean"]) - 1) for r in rows) // k
        nrows = hi - lo + 1
        # cap the payload: coarsen price buckets if needed
        while nrows * len(times) > 2_500_000:
            k *= 2
            lo = min(r["origin"] for r in rows) // k
            hi = max((r["origin"] + len(r["bid_mean"]) - 1) for r in rows) // k
            nrows = hi - lo + 1
        ncol = len(times)
        col_of = {t: i for i, t in enumerate(times)}
        nan = float("nan")
        bid = [nan] * (ncol * nrows)
        ask = [nan] * (ncol * nrows)
        wsum = [0.0] * (ncol * nrows)
        use_max = agg == "max"
        src_b = "bid_max" if use_max else "bid_mean"
        src_a = "ask_max" if use_max else "ask_mean"
        # first merge price buckets within each minute, then aggregate minutes into columns
        for r in rows:
            ci = col_of.get(r["t"] - r["t"] % tf)
            if ci is None:
                continue
            merged: Dict[int, List[float]] = {}
            o = r["origin"]
            bm, am = r[src_b], r[src_a]
            for j in range(len(bm)):
                vb, va = bm[j], am[j]
                if vb != vb:  # NaN: not covered in this minute
                    continue
                ri = (o + j) // k - lo
                m = merged.get(ri)
                if m is None:
                    merged[ri] = [vb, va]
                else:
                    m[0] += vb
                    m[1] += va
            w = r["samples"]
            base = ci * nrows
            for ri, (vb, va) in merged.items():
                idx = base + ri
                if use_max:
                    cb = bid[idx]
                    bid[idx] = vb if cb != cb or vb > cb else cb
                    ca = ask[idx]
                    ask[idx] = va if ca != ca or va > ca else ca
                else:
                    if wsum[idx] == 0.0:
                        bid[idx] = 0.0
                        ask[idx] = 0.0
                    bid[idx] += vb * w
                    ask[idx] += va * w
                    wsum[idx] += w
        if not use_max:
            for idx in range(len(bid)):
                w = wsum[idx]
                if w:
                    bid[idx] /= w
                    ask[idx] /= w
        b64 = lambda arr: base64.b64encode(struct.pack(f"<{len(arr)}f", *arr)).decode()
        step = base_step * k
        return jresp({"inst": rt.inst.key, "tf": tf_name, "t": times, "agg": agg, "step": fmt(step),
                      "step_value": step / SCALE, "lo_price": lo * step / SCALE, "rows": nrows, "cols": ncol,
                      "units": rt.inst.base, "layout": "column-major: index = col*rows + row, row 0 = lo_price",
                      "bid": b64(bid), "ask": b64(ask),
                      "note": "NaN = цена вне покрытого диапазона стакана или нет данных за интервал",
                      "max_mode": "сумма поминутных максимумов по объединённым бакетам" if use_max and k > 1 else None})

    async def footprint(self, request: web.Request) -> web.Response:
        rt = self._rt(request)
        tf_name, tf = self._tf(request)
        t0, t1 = self._range(request, tf)
        base_step = int(rt.meta["footprint_step"])
        req = request.query.get("step")
        k = max(1, int(round(float(req) * SCALE / base_step))) if req else 1
        rows = await self.storage.run(q_footprint, rt.iid, t0 - t0 % tf, t1)
        cols: Dict[int, Dict[int, List[int]]] = {}
        partial: Dict[int, bool] = {}
        for r in rows:
            t = r["t"] - r["t"] % tf
            col = cols.setdefault(t, {})
            partial[t] = partial.get(t, False) or r["partial"]
            for idx, bq, sq, bn, sn in r["rows"]:
                b = col.setdefault(idx // k, [0, 0, 0, 0])
                b[0] += bq; b[1] += sq; b[2] += bn; b[3] += sn
        step = base_step * k
        out = []
        for t in sorted(cols):
            out.append({"t": t, "partial": partial[t],
                        "rows": [[i * step / SCALE, v[0] / SCALE, v[1] / SCALE, v[2], v[3]] for i, v in sorted(cols[t].items())]})
        return jresp({"inst": rt.inst.key, "tf": tf_name, "step": fmt(step), "step_value": step / SCALE,
                      "base_step": fmt(base_step), "units": {"qty": rt.inst.base, "count": "trades"},
                      "row_format": ["price_lo", "buy_qty", "sell_qty", "buy_trades", "sell_trades"],
                      "note": "Кол-во сделок — исполнения по данным биржи (у Binance: l-f+1 агрегированной записи).",
                      "candles": out})

    async def profile(self, request: web.Request) -> web.Response:
        rt = self._rt(request)
        now = int(time.time() * 1000)
        t1 = int(request.query.get("to") or now + MIN_MS)
        t0 = int(request.query.get("from") or t1 - 86400_000)
        base_step = int(rt.meta["footprint_step"])
        req = request.query.get("step")
        k = max(1, int(round(float(req) * SCALE / base_step))) if req else 1
        share = float(request.query.get("va", 0.7))
        rows = await self.storage.run(q_footprint, rt.iid, t0, t1)
        acc: Dict[int, List[int]] = {}
        partial = False
        for r in rows:
            partial |= r["partial"]
            for idx, bq, sq, bn, sn in r["rows"]:
                b = acc.setdefault(idx // k, [0, 0])
                b[0] += bq
                b[1] += sq
        step = base_step * k
        buckets = sorted(acc.items())
        va = ind.value_area([(i, (v[0] + v[1]) / SCALE) for i, v in buckets], share)
        return jresp({"inst": rt.inst.key, "from": t0, "to": t1, "step": fmt(step), "step_value": step / SCALE,
                      "units": rt.inst.base, "partial": partial, "minutes": len(rows),
                      "buckets": [[i * step / SCALE, v[0] / SCALE, v[1] / SCALE] for i, v in buckets],
                      "poc": va["poc"] * step / SCALE if va else None,
                      "val": va["val"] * step / SCALE if va else None,
                      "vah": (va["vah"] + 1) * step / SCALE if va else None,
                      "va_share": va["share"] if va else None,
                      "algorithm": "POC = max bucket (tie: lower price); VA: expand from POC adding the larger neighbour, tie adds both"})

    async def screener_route(self, request: web.Request) -> web.Response:
        return jresp({"rows": await self.screener.rows(), "time": int(time.time() * 1000),
                      "units": "quote currency of each instrument; spread in bps; dynamics in percentage points",
                      "disclaimer": "Исследовательские фильтры, не торговые рекомендации."})

    async def health(self, request: web.Request) -> web.Response:
        h = self.c.health()
        h["db_rows"] = await self.storage.run(q_sizes)
        h["ws_clients"] = len(self.clients)
        try:
            h["db_size_mb"] = round(Path(self.storage.path).stat().st_size / 1e6, 2)
        except OSError:
            h["db_size_mb"] = None
        return jresp(h)

    async def get_setting(self, request: web.Request) -> web.Response:
        key = request.match_info["key"]
        if key == "telegram":
            return err("use /api/telegram", 403)
        return jresp({"key": key, "value": self.storage.get_setting("ui:" + key)})

    async def put_setting(self, request: web.Request) -> web.Response:
        key = request.match_info["key"]
        if key == "telegram":
            return err("use /api/telegram", 403)
        body = await request.json()
        self.storage.set_setting("ui:" + key, body.get("value"))
        return jresp({"ok": True})

    async def alerts_list(self, request: web.Request) -> web.Response:
        return jresp({"rules": self.storage.alert_rules(), "metrics": METRICS, "ops": OPS})

    async def alerts_create(self, request: web.Request) -> web.Response:
        body = await request.json()
        inst = body.get("instrument")
        if inst not in self.c.runtimes:
            return err("unknown instrument")
        try:
            rule = validate_rule(body.get("rule") or {})
        except (ValueError, TypeError, KeyError) as e:
            return err(str(e))
        rid = self.storage.add_alert_rule(inst, rule)
        return jresp({"id": rid, "rule": rule})

    async def alerts_patch(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.storage.update_alert_rule(int(request.match_info["id"]), enabled=bool(body.get("enabled")))
        return jresp({"ok": True})

    async def alerts_delete(self, request: web.Request) -> web.Response:
        self.storage.delete_alert_rule(int(request.match_info["id"]))
        return jresp({"ok": True})

    async def alerts_log(self, request: web.Request) -> web.Response:
        return jresp({"log": self.storage.alert_log(int(request.query.get("limit", 200)))})

    async def telegram_get(self, request: web.Request) -> web.Response:
        return jresp(self.telegram.public_config())

    async def telegram_put(self, request: web.Request) -> web.Response:
        body = await request.json()
        cur = self.telegram.config()
        if body.get("token"):
            cur["token"] = str(body["token"]).strip()
        if "chat_id" in body:
            cur["chat_id"] = str(body["chat_id"]).strip()
        if "enabled" in body:
            cur["enabled"] = bool(body["enabled"])
        self.storage.set_setting("telegram", cur)
        return jresp(self.telegram.public_config())

    async def telegram_test(self, request: web.Request) -> web.Response:
        res = await self.telegram.send("FlowLens: тестовое сообщение")
        return jresp({"result": res})

    # ---- live stream ---------------------------------------------------------------
    def _on_event(self, kind: str, data: Dict[str, Any]) -> None:
        msg = json.dumps({"type": kind, **data})
        for ws in list(self.clients):
            sub = getattr(ws, "_flowlens_sub", None)
            if kind == "bar_closed" and (not sub or sub.get("inst") != data.get("inst")):
                continue
            asyncio.ensure_future(self._send(ws, msg))

    async def _send(self, ws: web.WebSocketResponse, msg: str) -> None:
        try:
            await ws.send_str(msg)
        except Exception:
            self.clients.discard(ws)

    def _live_candle(self, rt, tf_name: str) -> Optional[Dict[str, Any]]:
        p = rt.pipeline
        if p is None:
            return None
        tf = TF_MS[tf_name]
        now = int(time.time() * 1000)
        b0 = now - now % tf
        mins = [p.candles[m] for m in sorted(p.candles) if m >= b0]
        cache_key = (rt.inst.key, tf_name)
        older = None
        if tf > MIN_MS and (not mins or mins[0].t > b0):
            cached = self.bucket_cache.get(cache_key)
            if cached and cached[0] == b0:
                older = cached[1]
        if not mins and older is None:
            return None
        o = h = l = c = None
        qb = qs = vb = vs = 0.0
        nb = ns = 0
        complete = True
        if older:
            o, h, l, c = older["o"], older["h"], older["l"], older["c"]
            qb, qs, vb, vs = older["q_buy"], older["q_sell"], older["v_buy"], older["v_sell"]
            nb, ns = older["n_buy"], older["n_sell"]
            complete = older["complete"]
        for m in mins:
            if m.n_rec == 0:
                continue
            if o is None:
                o, h, l = m.o / SCALE, m.h / SCALE, m.l / SCALE
            h = max(h, m.h / SCALE)
            l = min(l, m.l / SCALE)
            c = m.c / SCALE
            qb += m.q_buy / NOTIONAL_SCALE
            qs += m.q_sell / NOTIONAL_SCALE
            vb += m.v_buy / SCALE
            vs += m.v_sell / SCALE
            if nb is not None:
                nb += m.n_buy
                ns += m.n_sell
            complete &= m.complete and m.gap_count == 0
        if o is None:
            return None
        return {"t": b0, "o": o, "h": h, "l": l, "c": c, "q_buy": qb, "q_sell": qs, "v_buy": vb, "v_sell": vs,
                "n_buy": nb, "n_sell": ns, "complete_so_far": complete, "provisional": True}

    async def _refresh_bucket_cache(self, rt, tf_name: str) -> None:
        tf = TF_MS[tf_name]
        if tf <= MIN_MS or rt.pipeline is None:
            return
        now = int(time.time() * 1000)
        b0 = now - now % tf
        mem0 = min(rt.pipeline.candles) if rt.pipeline.candles else now
        if mem0 <= b0:
            return
        rows = await self.storage.run(q_candles, rt.iid, b0, mem0)
        if not rows:
            return
        c = aggregate_candles(rows, [b0], tf)
        self.bucket_cache[(rt.inst.key, tf_name)] = (b0, {k: c[k][0] for k in ("o", "h", "l", "c", "q_buy", "q_sell", "v_buy", "v_sell", "n_buy", "n_sell")} | {"complete": False})

    async def stream(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.clients.add(ws)
        ws._flowlens_sub = None  # type: ignore[attr-defined]
        pusher = asyncio.ensure_future(self._push_loop(ws))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if data.get("op") == "sub":
                    inst = data.get("inst")
                    tf = data.get("tf", "5m")
                    if inst in self.c.runtimes and tf in TF_MS:
                        # one subscription per connection: a new one replaces the old one
                        ws._flowlens_sub = {"inst": inst, "tf": tf}  # type: ignore[attr-defined]
                        await self._refresh_bucket_cache(self.c.runtimes[inst], tf)
                        await ws.send_str(json.dumps({"type": "subscribed", "inst": inst, "tf": tf}))
                    else:
                        await ws.send_str(json.dumps({"type": "error", "error": "bad subscription"}))
                elif data.get("op") == "unsub":
                    ws._flowlens_sub = None  # type: ignore[attr-defined]
        finally:
            pusher.cancel()
            self.clients.discard(ws)
        return ws

    async def _push_loop(self, ws: web.WebSocketResponse) -> None:
        period = self.c.cfg.live_push_ms / 1000.0
        n = 0
        while not ws.closed:
            await asyncio.sleep(period)
            n += 1
            sub = getattr(ws, "_flowlens_sub", None)
            now = int(time.time() * 1000)
            try:
                if n % int(max(1, 2 / period)) == 0:
                    await ws.send_str(json.dumps(clean({"type": "statuses", "now": now, "items": {
                        k: {"status": rt.status(), "last": (rt.pipeline.last_trade.price / SCALE) if rt.pipeline and rt.pipeline.last_trade else (rt.ticker or {}).get("last"),
                            "change_pct": (rt.ticker or {}).get("change_pct"), "volume_quote": (rt.ticker or {}).get("volume_quote")}
                        for k, rt in self.c.runtimes.items()}})))
                if not sub:
                    continue
                rt = self.c.runtimes.get(sub["inst"])
                if rt is None or rt.pipeline is None:
                    continue
                if n % int(max(1, 60 / period)) == 0:
                    await self._refresh_bucket_cache(rt, sub["tf"])
                st = rt.live_status()
                msg: Dict[str, Any] = {"type": "live", "inst": sub["inst"], "tf": sub["tf"], "now": now,
                                       "status": {k: st[k] for k in ("status", "book_status", "last_event", "latency_ms",
                                                                      "book_epoch", "generating", "trade_holes", "sync")},
                                       "candle": self._live_candle(rt, sub["tf"]),
                                       "book": sample_metrics(rt.pipeline.last_sample)}
                if n % int(max(1, 1 / period)) == 0 and rt.pipeline.last_sample is not None:
                    s = rt.pipeline.last_sample
                    if s.heat_bid or s.heat_ask:
                        lo = s.heat_lo_idx
                        hi = s.heat_hi_idx
                        if hi - lo < 4000:
                            msg["heat"] = {"t": minute_of(now), "origin": lo, "step": rt.pipeline.heat_step / SCALE,
                                           "bid": [s.heat_bid.get(i, 0) / SCALE for i in range(lo, hi + 1)],
                                           "ask": [s.heat_ask.get(i, 0) / SCALE for i in range(lo, hi + 1)],
                                           "instant": True}
                await ws.send_str(json.dumps(clean(msg), separators=(",", ":")))
            except (ConnectionResetError, RuntimeError):
                return
            except Exception as e:  # pragma: no cover
                log.warning("push failed: %s", e)

    # ---- app -----------------------------------------------------------------------
    def app(self) -> web.Application:
        app = web.Application(client_max_size=2 * 1024 * 1024)
        r = app.router
        r.add_get("/api/meta", self.meta)
        r.add_get("/api/instruments", self.instruments)
        r.add_get("/api/chart", self.chart)
        r.add_get("/api/candles", self.candles)
        r.add_get("/api/depth-history", self.depth_history)
        r.add_get("/api/indicators", self.indicators)
        r.add_get("/api/heatmap", self.heatmap)
        r.add_get("/api/footprint", self.footprint)
        r.add_get("/api/profile", self.profile)
        r.add_get("/api/screener", self.screener_route)
        r.add_get("/api/health", self.health)
        r.add_get("/api/settings/{key}", self.get_setting)
        r.add_put("/api/settings/{key}", self.put_setting)
        r.add_get("/api/alerts", self.alerts_list)
        r.add_post("/api/alerts", self.alerts_create)
        r.add_patch("/api/alerts/{id}", self.alerts_patch)
        r.add_delete("/api/alerts/{id}", self.alerts_delete)
        r.add_get("/api/alerts/log", self.alerts_log)
        r.add_get("/api/telegram", self.telegram_get)
        r.add_put("/api/telegram", self.telegram_put)
        r.add_post("/api/telegram/test", self.telegram_test)
        r.add_get("/api/stream", self.stream)
        if self.static_dir and (self.static_dir / "index.html").exists():
            index = self.static_dir / "index.html"

            async def spa(request: web.Request) -> web.StreamResponse:
                return web.FileResponse(index)

            r.add_get("/", spa)
            r.add_static("/assets", self.static_dir / "assets", show_index=False)
            for f in self.static_dir.iterdir():
                if f.is_file() and f.name != "index.html":
                    r.add_get("/" + f.name, lambda req, f=f: web.FileResponse(f))
        else:
            async def no_ui(request: web.Request) -> web.Response:
                return web.Response(text="FlowLens API is running. Frontend build not found: run `npm run build` in frontend/.",
                                    content_type="text/plain")
            r.add_get("/", no_ui)
        return app
