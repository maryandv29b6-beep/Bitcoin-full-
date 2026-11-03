"""Screener metrics per collected instrument (also the input of alerts).

Fast metrics come from memory (current book sample, last candles); slow ones
(CVD since 00:00 UTC, z-score of the 1 % bands against the last day of 1m
frames) are recomputed from storage at most once a minute.
"""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from . import indicators as ind
from .book import CORE_BANDS
from .numeric import NOTIONAL_SCALE, SCALE
from .pipeline import MIN_MS, minute_of
from .storage import q_candles, q_depth


class ImbHistory:
    """Per-tick imbalance history (for Dynamics in the screener / alerts)."""

    def __init__(self, seconds: int = 1800) -> None:
        self.buf: Deque[Tuple[int, List[Optional[float]]]] = deque(maxlen=seconds)

    def add(self, t: int, vals: List[Optional[float]]) -> None:
        self.buf.append((t, vals))

    def at(self, t: int) -> Optional[List[Optional[float]]]:
        best = None
        for ts, v in self.buf:
            if ts <= t:
                best = v
            else:
                break
        return best


def sample_metrics(sample) -> Optional[Dict[str, Any]]:
    if sample is None:
        return None
    out: Dict[str, Any] = {"mid": sample.mid / SCALE, "best_bid": sample.best_bid / SCALE,
                           "best_ask": sample.best_ask / SCALE,
                           "spread_bps": (sample.best_ask - sample.best_bid) / sample.mid * 1e4, "bands": {}}
    for k, r in enumerate(CORE_BANDS):
        bq, bn, cb = sample.bids[k]
        aq, an, ca = sample.asks[k]
        b = bn / NOTIONAL_SCALE if cb >= 1.0 else None
        a = an / NOTIONAL_SCALE if ca >= 1.0 else None
        bqf = bq / SCALE if cb >= 1.0 else None
        aqf = aq / SCALE if ca >= 1.0 else None
        out["bands"][repr(r)] = {"bid": b, "ask": a, "bid_qty": bqf, "ask_qty": aqf, "cov_bid": round(cb, 4),
                                 "cov_ask": round(ca, 4), "imbalance": ind.imbalance(b, a),
                                 "delta": ind.ob_delta(b, a)}
    return out


class Screener:
    def __init__(self, collector) -> None:
        self.c = collector
        self.hist: Dict[str, ImbHistory] = {}
        self.slow: Dict[str, Dict[str, Any]] = {}
        self.slow_t: Dict[str, float] = {}

    def on_tick(self) -> None:
        now = int(time.time() * 1000)
        for key, rt in self.c.runtimes.items():
            if rt.pipeline is None:
                continue
            m = sample_metrics(rt.pipeline.last_sample)
            vals = [m["bands"][repr(r)]["imbalance"] if m else None for r in CORE_BANDS]
            self.hist.setdefault(key, ImbHistory()).add(now, vals)

    async def _slow(self, key: str, rt) -> Dict[str, Any]:
        if time.monotonic() - self.slow_t.get(key, 0) < 60 and key in self.slow:
            return self.slow[key]
        self.slow_t[key] = time.monotonic()
        now = int(time.time() * 1000)
        day0 = now - now % 86400_000
        out: Dict[str, Any] = {}
        try:
            rows = await self.c.storage.run(q_candles, rt.iid, day0, now + MIN_MS)
            deltas = [(r["q_buy"] - r["q_sell"]) if r["q_buy"] is not None and r["q_sell"] is not None else None
                      for r in rows]
            out["cvd_day"] = sum(d for d in deltas if d is not None) if rows else None
            out["cvd_day_partial"] = any(d is None for d in deltas) or any(not r["complete"] for r in rows)
            drows = await self.c.storage.run(q_depth, rt.iid, now - 86400_000 - 2 * MIN_MS, now)
            for side, idx in (("bid", 1), ("ask", 3)):
                x = [r["bands"].get("0.01", [None] * 6)[idx] if r["samples"] >= 30 else None for r in drows]
                z, st = ind.zscore(x + [None], min(1440, max(1, len(x))), 30, 0.9) if x else ([None], ["warmup"])
                out[f"z_{side}_1"] = None
                out[f"z_{side}_1_status"] = "warmup"
                if x:
                    # z of the latest closed minute against the previous day
                    zz, ss = ind.zscore(x, 1440, 30, 0.9)
                    out[f"z_{side}_1"], out[f"z_{side}_1_status"] = zz[-1], ss[-1]
        except Exception as e:  # pragma: no cover
            out["error"] = str(e)
        self.slow[key] = out
        return out

    async def rows(self) -> List[Dict[str, Any]]:
        now = int(time.time() * 1000)
        out = []
        for key, rt in self.c.runtimes.items():
            p = rt.pipeline
            st = rt.live_status()
            row: Dict[str, Any] = {"key": key, "exchange": rt.inst.exchange, "market": rt.inst.market,
                                   "symbol": rt.inst.symbol, "base": rt.inst.base, "quote": rt.inst.quote,
                                   "status": st["status"], "demo": rt.inst.is_demo}
            tk = rt.ticker or {}
            last = None
            if p is not None and p.last_trade is not None:
                last = p.last_trade.price / SCALE
            row["price"] = last if last is not None else tk.get("last")
            row["change_24h_pct"] = tk.get("change_pct")
            row["volume_24h_quote"] = tk.get("volume_quote")
            row["volume_unit"] = rt.inst.quote
            m = sample_metrics(p.last_sample) if p else None
            row["spread_bps"] = m["spread_bps"] if m else None
            row["bands"] = m["bands"] if m else None
            h = self.hist.get(key)
            dyn = {}
            if h and m:
                prev = h.at(now - 5 * 60_000)
                for i, r in enumerate(CORE_BANDS):
                    cur = m["bands"][repr(r)]["imbalance"]
                    pv = prev[i] if prev else None
                    dyn[repr(r)] = None if cur is None or pv is None else cur - pv
            row["dynamics_5m"] = dyn or None
            # trade delta from memory (quote units)
            if p is not None:
                cur_m = minute_of(now)
                d5 = d60 = 0.0
                ok5 = ok60 = True
                for mm, c in p.candles.items():
                    if mm >= cur_m - 59 * MIN_MS:
                        d = (c.q_buy - c.q_sell) / NOTIONAL_SCALE
                        d60 += d
                        ok60 &= c.complete and c.gap_count == 0
                        if mm >= cur_m - 4 * MIN_MS:
                            d5 += d
                            ok5 &= c.complete and c.gap_count == 0
                row["delta_5m"] = d5
                row["delta_1h"] = d60
                row["delta_partial"] = not (ok5 and ok60)
            slow = await self._slow(key, rt) if rt.iid >= 0 else {}
            row.update(slow)
            age = (now - st["last_event"]) / 1000 if st["last_event"] else None
            row["freshness_s"] = age
            row["coverage_1"] = [m["bands"]["0.01"]["cov_bid"], m["bands"]["0.01"]["cov_ask"]] if m else None
            if rt.inst.market == "perp":
                ds = rt.deriv_state
                row["oi"] = ds.open_interest
                row["oi_value"] = ds.open_interest_value
                row["funding"] = ds.funding_rate
                row["next_funding"] = ds.next_funding_time
                row["funding_interval_h"] = ds.funding_interval_h
            out.append(row)
        return out
