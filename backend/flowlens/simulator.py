"""Synthetic market used by the DEMO source and by the mock exchange.

Produces Binance-Spot-shaped messages (diff depth with U/u ids, aggTrade with
ids, REST snapshots) so both the DEMO adapter and the integration tests run
through the real synchronisation code. Everything it produces is synthetic
and is always labelled DEMO / mock.
"""
from __future__ import annotations

import math
import random
from typing import Dict, List, Optional, Tuple

from . import numeric


class MarketSim:
    def __init__(self, symbol: str = "BTCUSDT", price: float = 77000.0, tick: str = "0.01",
                 step: str = "0.00001", seed: int = 7, levels: int = 2500, spacing: Optional[float] = None,
                 base_qty: float = 0.8, trade_rate: float = 12.0) -> None:
        self.symbol = symbol
        self.rng = random.Random(seed)
        self.tick = float(tick)
        self.tick_s = tick
        self.step = float(step)
        self.step_s = step
        self._tick_i = numeric.parse(tick)
        self._step_i = numeric.parse(step)
        self.mid = price
        self.levels = levels
        # spacing between resting levels, in price units (keeps level counts realistic)
        self.spacing = max(float(tick), spacing or round(price * 0.000018 / float(tick)) * float(tick))
        self.base_qty = base_qty
        self.trade_rate = trade_rate
        self.bids: Dict[int, int] = {}
        self.asks: Dict[int, int] = {}
        self.update_id = 1000
        self.agg_id = 5000
        self.trade_id = 90000
        self.t = 0
        # regimes (all rates per second)
        self.drift = 0.0        # slow trend, fraction per second
        self.vol = 0.00011      # diffusion, fraction per sqrt(second)
        self.pressure = 0.0     # >0 more bids, <0 more asks
        self.burst = 0          # remaining burst time, ms
        self.burst_side = 1
        self.burst_amp = 1.0
        self.burst_drift = 0.0  # fraction per second during a burst
        self.walls: List[Tuple[int, int, int]] = []   # (side, price, qty)
        self._build()

    # ---- helpers -------------------------------------------------------------
    def _p(self, x: float) -> int:
        return int(round(x / self.tick)) * self._tick_i

    def _q(self, x: float) -> int:
        return max(1, int(round(x / self.step))) * self._step_i

    def _depth_qty(self, dist_frac: float, side: int) -> float:
        # thin near mid, thicker further out; pressure shifts liquidity between sides
        shape = 0.35 + 2.2 * min(1.0, dist_frac / 0.01) ** 0.7
        tilt = math.exp(side * self.pressure * 0.9 * min(1.0, dist_frac / 0.006 + 0.3))
        return self.base_qty * shape * tilt * self.rng.lognormvariate(0, 0.8)

    def _grid(self, price: float) -> int:
        """Snap a price to the resting-level grid (multiples of `spacing`)."""
        return self._p(round(price / self.spacing) * self.spacing)

    def _build(self) -> None:
        self.bids.clear()
        self.asks.clear()
        for i in range(1, self.levels + 1):
            if self.rng.random() < 0.85:
                pb = self._grid(self.mid - i * self.spacing)
                self.bids[pb] = self._q(self._depth_qty(i * self.spacing / self.mid, 1))
            if self.rng.random() < 0.85:
                pa = self._grid(self.mid + i * self.spacing)
                self.asks[pa] = self._q(self._depth_qty(i * self.spacing / self.mid, -1))

    # ---- REST-like views ---------------------------------------------------------
    def snapshot(self, limit: int = 5000) -> dict:
        bids = sorted(self.bids.items(), reverse=True)[:limit]
        asks = sorted(self.asks.items())[:limit]
        return {"lastUpdateId": self.update_id,
                "bids": [[numeric.fmt(p), numeric.fmt(q)] for p, q in bids],
                "asks": [[numeric.fmt(p), numeric.fmt(q)] for p, q in asks]}

    # ---- evolution -----------------------------------------------------------------
    def step_ms(self, now: int, dt_ms: int = 100) -> Tuple[Optional[dict], List[dict]]:
        """Advance the market by dt; returns (depthUpdate or None, aggTrades)."""
        rng = self.rng
        self.t = now
        dt = dt_ms / 1000.0
        if rng.random() < dt / 900:          # new slow trend every ~15 min
            self.drift = rng.gauss(0, 1.5e-6)
        if rng.random() < dt / 600:          # book pressure regime every ~10 min
            self.pressure = max(-1.5, min(1.5, rng.gauss(0, 0.8)))
        self.pressure *= 0.9999 ** dt_ms
        if self.burst <= 0 and rng.random() < dt / 1500:   # flow burst every ~25 min
            dur = rng.uniform(60, 420)                       # seconds
            self.burst = int(dur * 1000)
            self.burst_side = 1 if rng.random() < 0.5 else -1
            self.burst_amp = rng.uniform(2.0, 6.0)
            self.burst_drift = self.burst_side * rng.uniform(0.0015, 0.006) / dur
        amp, side = 1.0, 0
        burst_ret = 0.0
        if self.burst > 0:
            k = 1.0 - self.burst / 420000.0
            self.burst -= dt_ms
            amp, side = 1.0 + (self.burst_amp - 1.0) * max(0.3, math.sin(math.pi * min(1.0, max(0.0, k)))), self.burst_side
            burst_ret = self.burst_drift * dt
        ret = self.drift * dt + burst_ret + rng.gauss(0, self.vol * math.sqrt(dt))
        new_mid = self.mid * (1 + ret)
        changes_b: Dict[int, int] = {}
        changes_a: Dict[int, int] = {}
        trades: List[dict] = []
        # trades: aggressive flow hits the book in the direction of the move
        n_tr = self._poisson(self.trade_rate * dt * amp)
        for _ in range(n_tr):
            buy = rng.random() < (0.5 + 0.18 * side + (0.1 if ret > 0 else -0.1))
            qty = min(25.0, self.base_qty * 0.08 * rng.paretovariate(1.6) * amp)
            book = self.asks if buy else self.bids
            if not book:
                continue
            best = min(book) if buy else max(book)
            q_int = self._q(qty)
            avail = book[best]
            fill = min(q_int, avail)
            if fill <= 0:
                continue
            left = avail - fill
            if left > 0:
                book[best] = left
            else:
                del book[best]
            (changes_a if buy else changes_b)[best] = left
            self.agg_id += 1
            n_fills = 1 + int(rng.random() * 3)
            trades.append({"e": "aggTrade", "E": now, "s": self.symbol, "a": self.agg_id,
                           "p": numeric.fmt(best), "q": numeric.fmt(fill), "f": self.trade_id + 1,
                           "l": self.trade_id + n_fills, "T": now, "m": not buy, "M": True})
            self.trade_id += n_fills
        # move the book with the mid: remove crossed levels, keep the touch filled
        self.mid = new_mid
        bid_lim = self._p(self.mid - self.spacing * 0.5)
        ask_lim = self._p(self.mid + self.spacing * 0.5)
        if self.bids and max(self.bids) >= bid_lim:
            for p in [p for p in self.bids if p >= bid_lim]:
                del self.bids[p]
                changes_b[p] = 0
        if self.asks and min(self.asks) <= ask_lim:
            for p in [p for p in self.asks if p <= ask_lim]:
                del self.asks[p]
                changes_a[p] = 0
        for k in range(1, 6):
            for sd, book, ch in ((1, self.bids, changes_b), (-1, self.asks, changes_a)):
                p = self._grid(self.mid - sd * k * self.spacing)
                if p not in book and rng.random() < 0.6:
                    q = self._q(self._depth_qty(k * self.spacing / self.mid, sd))
                    book[p] = q
                    ch[p] = q
        # random churn on the grid: modify, cancel or add, more often near the touch
        n_upd = 3 + self._poisson(60 * dt if dt <= 1 else 60 + 20 * (dt - 1))
        for _ in range(n_upd):
            sd = 1 if rng.random() < 0.5 else -1
            dist = min(0.05, rng.expovariate(1 / 0.004)) + self.spacing / self.mid
            p = self._grid(self.mid * (1 - dist) if sd > 0 else self.mid * (1 + dist))
            book = self.bids if sd > 0 else self.asks
            ch = changes_b if sd > 0 else changes_a
            if p in book:
                if rng.random() < 0.3:
                    del book[p]
                    ch[p] = 0
                else:
                    q = self._q(self._depth_qty(dist, sd))
                    book[p] = q
                    ch[p] = q
            elif rng.random() < 0.85:
                q = self._q(self._depth_qty(dist, sd))
                book[p] = q
                ch[p] = q
        # occasional large resting walls
        if rng.random() < 0.002 * dt_ms / 100:
            sd = 1 if rng.random() < 0.5 else -1
            dist = rng.uniform(0.001, 0.009)
            p = self._grid(self.mid * (1 - dist) if sd > 0 else self.mid * (1 + dist))
            q = self._q(self.base_qty * rng.uniform(30, 120))
            (self.bids if sd > 0 else self.asks)[p] = q
            (changes_b if sd > 0 else changes_a)[p] = q
        self._trim(changes_b, changes_a)
        if not changes_b and not changes_a:
            return None, trades
        U = self.update_id + 1
        self.update_id += max(1, len(changes_b) + len(changes_a))
        ev = {"e": "depthUpdate", "E": now, "s": self.symbol, "U": U, "u": self.update_id,
              "b": [[numeric.fmt(p), numeric.fmt(q)] for p, q in changes_b.items()],
              "a": [[numeric.fmt(p), numeric.fmt(q)] for p, q in changes_a.items()]}
        return ev, trades

    def _trim(self, cb: Dict[int, int], ca: Dict[int, int]) -> None:
        """Keep resting levels out to ~5 % of mid on both sides."""
        lo = self.mid * (1 - 0.055)
        hi = self.mid * (1 + 0.055)
        lo_i, hi_i = self._p(lo), self._p(hi)
        if self.bids and min(self.bids) < lo_i:
            for p in [p for p in self.bids if p < lo_i]:
                del self.bids[p]
                cb[p] = 0
        if self.asks and max(self.asks) > hi_i:
            for p in [p for p in self.asks if p > hi_i]:
                del self.asks[p]
                ca[p] = 0
        far_b = min(self.bids) if self.bids else self._grid(self.mid)
        while far_b / 1e8 > self.mid * (1 - 0.05):
            nxt = self._grid(far_b / 1e8 - self.spacing)
            far_b = nxt if nxt < far_b else far_b - self._tick_i
            if far_b <= 0:
                break
            if self.rng.random() < 0.85:
                q = self._q(self._depth_qty((self.mid - far_b / 1e8) / self.mid, 1))
                self.bids[far_b] = q
                cb[far_b] = q
        far_a = max(self.asks) if self.asks else self._grid(self.mid)
        while far_a / 1e8 < self.mid * (1 + 0.05):
            nxt = self._grid(far_a / 1e8 + self.spacing)
            far_a = nxt if nxt > far_a else far_a + self._tick_i
            if self.rng.random() < 0.85:
                q = self._q(self._depth_qty((far_a / 1e8 - self.mid) / self.mid, -1))
                self.asks[far_a] = q
                ca[far_a] = q

    def _poisson(self, lam: float) -> int:
        if lam <= 0:
            return 0
        if lam > 30:
            return max(0, int(self.rng.gauss(lam, math.sqrt(lam))))
        L = math.exp(-lam)
        k, p = 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= L:
                return k
            k += 1

    # ---- klines history (mock exchange only) -------------------------------------
    def klines(self, start: int, end: int, seed: int = 1) -> List[list]:
        """Deterministic synthetic 1m klines ending at the current mid (mock only)."""
        rng = random.Random(seed + start // 60000)
        n = max(0, (end - start) // 60000)
        out = []
        p = self.mid
        closes = []
        for _ in range(n):
            closes.append(p)
            p /= (1 + rng.gauss(0, 0.0007))
        closes.reverse()
        for i in range(n):
            t = start + i * 60000
            c = closes[i]
            o = closes[i - 1] if i else c * (1 + rng.gauss(0, 0.0005))
            h = max(o, c) * (1 + abs(rng.gauss(0, 0.0003)))
            l = min(o, c) * (1 - abs(rng.gauss(0, 0.0003)))
            vol = abs(rng.gauss(40, 15)) + 1
            tb = vol * min(0.95, max(0.05, rng.gauss(0.5, 0.08)))
            out.append([t, f"{o:.2f}", f"{h:.2f}", f"{l:.2f}", f"{c:.2f}", f"{vol:.5f}", t + 59999,
                        f"{vol * c:.8f}", int(vol * 30), f"{tb:.5f}", f"{tb * c:.8f}", "0"])
        return out
