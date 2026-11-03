"""Local order book with integer prices/quantities and explicit depth coverage.

Coverage model
--------------
A REST snapshot returns at most N levels per side. Levels deeper than the
snapshot are unknown until they change, so the book is only *complete* between
the best price and the deepest snapshot level (``bid_floor`` / ``ask_ceil``).
For top-N streaming books (Bybit ``orderbook.N``) the complete range is the
range of the N levels currently held.

Every metric derived from the book reports which part of the requested range
is covered. A band that is not fully covered is returned as ``None`` with its
coverage fraction, never as a smaller number.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .numeric import parse

# cumulative relative depths r (fractions of mid)
CORE_BANDS: Tuple[float, ...] = (0.0025, 0.005, 0.01)
EXT_BANDS: Tuple[float, ...] = (0.03, 0.05, 0.10)
ALL_BANDS: Tuple[float, ...] = CORE_BANDS + EXT_BANDS


class LocalBook:
    __slots__ = (
        "bids", "asks", "update_id", "bid_floor", "ask_ceil", "valid", "epoch",
        "top_n", "last_exch_ts", "crossed_count",
    )

    def __init__(self, top_n: Optional[int] = None) -> None:
        self.bids: Dict[int, int] = {}
        self.asks: Dict[int, int] = {}
        self.update_id: int = 0
        self.bid_floor: int = 0      # bids below this price are not known (0 = whole side known)
        self.ask_ceil: Optional[int] = None  # asks above are not known (None = whole side known)
        self.valid = False
        self.epoch = 0               # increments on every (re)synchronisation
        self.top_n = top_n           # coverage from held levels (top-N books)
        self.last_exch_ts: Optional[int] = None
        self.crossed_count = 0

    # ---- mutation -------------------------------------------------------
    def invalidate(self) -> None:
        self.valid = False

    def load_snapshot(self, bids: Iterable[Sequence[str]], asks: Iterable[Sequence[str]],
                      update_id: int, limit: Optional[int]) -> None:
        self.bids = {}
        self.asks = {}
        for p, q, *_ in bids:
            qi = parse(q)
            if qi:
                self.bids[parse(p)] = qi
        for p, q, *_ in asks:
            qi = parse(q)
            if qi:
                self.asks[parse(p)] = qi
        self.update_id = update_id
        if self.top_n is None:
            self.bid_floor = min(self.bids) if (limit and len(self.bids) >= limit and self.bids) else 0
            self.ask_ceil = max(self.asks) if (limit and len(self.asks) >= limit and self.asks) else None
        self.valid = True
        self.epoch += 1

    def apply(self, bids: Iterable[Sequence[str]], asks: Iterable[Sequence[str]]) -> None:
        """Apply absolute quantities; zero removes the level (removing an absent
        level is normal and ignored)."""
        b = self.bids
        for p, q, *_ in bids:
            qi = parse(q)
            if qi:
                b[parse(p)] = qi
            else:
                b.pop(parse(p), None)
        a = self.asks
        for p, q, *_ in asks:
            qi = parse(q)
            if qi:
                a[parse(p)] = qi
            else:
                a.pop(parse(p), None)

    def copy_from(self, other: "LocalBook") -> None:
        self.bids = other.bids
        self.asks = other.asks
        self.update_id = other.update_id
        self.bid_floor = other.bid_floor
        self.ask_ceil = other.ask_ceil

    def prune(self, max_levels: int) -> int:
        """Drop levels outside the known range when the book grows too large."""
        removed = 0
        if len(self.bids) > max_levels and self.bid_floor:
            for p in [p for p in self.bids if p < self.bid_floor]:
                del self.bids[p]
                removed += 1
        if len(self.asks) > max_levels and self.ask_ceil is not None:
            for p in [p for p in self.asks if p > self.ask_ceil]:
                del self.asks[p]
                removed += 1
        return removed

    # ---- queries --------------------------------------------------------
    def best_bid(self) -> Optional[int]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[int]:
        return min(self.asks) if self.asks else None

    def known_range(self) -> Tuple[int, Optional[int]]:
        """(lowest known bid price, highest known ask price); 0/None = unbounded."""
        if self.top_n is not None:
            lo = min(self.bids) if len(self.bids) >= self.top_n else 0
            hi = max(self.asks) if len(self.asks) >= self.top_n else None
            return lo, hi
        return self.bid_floor, self.ask_ceil

    def sample(self, bands: Sequence[float], heat_pct: float, heat_step: int) -> Optional["BookSample"]:
        """Compute one sample of band sums and heatmap buckets.

        Returns None when the book is not valid, empty or crossed.
        """
        if not self.valid or not self.bids or not self.asks:
            return None
        bb = max(self.bids)
        ba = min(self.asks)
        if bb >= ba:
            self.crossed_count += 1
            return None
        mid = (bb + ba) / 2.0
        lo_known, hi_known = self.known_range()
        nb = len(bands)
        # thresholds (inclusive): bid level p belongs to band k if p >= mid*(1-r_k)
        bid_lim = [mid * (1.0 - r) for r in bands]
        ask_lim = [mid * (1.0 + r) for r in bands]
        b_qty = [0] * nb
        b_not = [0] * nb
        a_qty = [0] * nb
        a_not = [0] * nb
        heat_lo = mid * (1.0 - heat_pct / 100.0)
        heat_hi = mid * (1.0 + heat_pct / 100.0)
        hb: Dict[int, int] = {}
        ha: Dict[int, int] = {}
        outer_b = bid_lim[-1]
        outer_a = ask_lim[-1]
        heat_floor = max(heat_lo, lo_known)
        heat_ceil = heat_hi if hi_known is None else min(heat_hi, hi_known)
        for p, q in self.bids.items():
            if p >= outer_b:
                k = 0
                while p < bid_lim[k]:
                    k += 1
                b_qty[k] += q
                b_not[k] += p * q
            if p >= heat_floor:
                idx = p // heat_step
                hb[idx] = hb.get(idx, 0) + q
        for p, q in self.asks.items():
            if p <= outer_a:
                k = 0
                while p > ask_lim[k]:
                    k += 1
                a_qty[k] += q
                a_not[k] += p * q
            if p <= heat_ceil:
                idx = p // heat_step
                ha[idx] = ha.get(idx, 0) + q
        # cumulative sums and coverage per band
        cb_q = cb_n = ca_q = ca_n = 0
        out_b: List[Tuple[int, int, float]] = []
        out_a: List[Tuple[int, int, float]] = []
        for k, r in enumerate(bands):
            cb_q += b_qty[k]
            cb_n += b_not[k]
            ca_q += a_qty[k]
            ca_n += a_not[k]
            cov_b = 1.0 if lo_known <= bid_lim[k] else max(0.0, (mid - lo_known) / (mid * r))
            cov_a = 1.0 if (hi_known is None or hi_known >= ask_lim[k]) else max(0.0, (hi_known - mid) / (mid * r))
            out_b.append((cb_q, cb_n, min(1.0, cov_b)))
            out_a.append((ca_q, ca_n, min(1.0, cov_a)))
        return BookSample(bb, ba, mid, out_b, out_a, hb, ha,
                          int(heat_floor // heat_step), int(heat_ceil // heat_step))


@dataclass(slots=True)
class BookSample:
    best_bid: int
    best_ask: int
    mid: float
    bids: List[Tuple[int, int, float]]   # per band: (cum qty 1e8, cum notional 1e16, coverage)
    asks: List[Tuple[int, int, float]]
    heat_bid: Dict[int, int]             # bucket index -> qty (1e8)
    heat_ask: Dict[int, int]
    heat_lo_idx: int                     # first covered bucket
    heat_hi_idx: int                     # last covered bucket
