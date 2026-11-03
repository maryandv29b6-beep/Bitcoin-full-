"""Per-instrument processing pipeline.

The pipeline is a synchronous state machine driven only by its inputs
(messages, snapshots, trades and sampling ticks, each with timestamps), so the
same event sequence always produces the same aggregates. This is what makes
recording + replay deterministic (see recorder.py and tests/test_replay.py).

Time conventions (all ms UTC):
  * candles / footprint are keyed by the *exchange* trade time;
  * depth frames / heatmap are keyed by the *local* sampling time; the mean
    exchange->local lag of the sampled book is stored next to them.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .book import ALL_BANDS, CORE_BANDS, BookSample, LocalBook
from .models import BUY, SELL, Instrument, Liquidation, Trade
from .numeric import NOTIONAL_SCALE, SCALE

MIN_MS = 60_000


def minute_of(ts: int) -> int:
    return ts - ts % MIN_MS


class CandleAcc:
    """1-minute candle built from executed trades (exchange time)."""

    __slots__ = ("t", "o", "h", "l", "c", "o_key", "c_key", "v_buy", "v_sell", "v_unk",
                 "q_buy", "q_sell", "q_unk", "n_buy", "n_sell", "n_unk", "n_rec",
                 "first_id", "last_id", "gap_count", "complete", "source", "fp", "fp_partial",
                 "n_total")

    def __init__(self, t: int, complete: bool) -> None:
        self.t = t
        self.o = self.h = self.l = self.c = 0
        self.o_key: Tuple[int, int] = (1 << 62, 0)
        self.c_key: Tuple[int, int] = (-1, 0)
        self.v_buy = self.v_sell = self.v_unk = 0          # qty 1e8
        self.q_buy = self.q_sell = self.q_unk = 0          # notional 1e16
        self.n_buy = self.n_sell = self.n_unk = 0          # underlying executions
        self.n_rec = 0                                     # records received
        self.n_total: Optional[int] = None
        self.first_id: Optional[int] = None
        self.last_id: Optional[int] = None
        self.gap_count = 0
        self.complete = complete
        self.source = "trades"
        self.fp: Dict[int, List[int]] = {}                 # bucket -> [buy_qty, sell_qty, buy_n, sell_n]
        self.fp_partial = not complete

    def add(self, tr: Trade, fp_step: int) -> None:
        p, q = tr.price, tr.qty
        key = (tr.t_exch, tr.seq if tr.seq is not None else self.n_rec)
        if self.n_rec == 0:
            self.o = self.h = self.l = self.c = p
            self.o_key = self.c_key = key
        else:
            if p > self.h:
                self.h = p
            if p < self.l:
                self.l = p
            if key < self.o_key:
                self.o_key, self.o = key, p
            if key >= self.c_key:
                self.c_key, self.c = key, p
        self.n_rec += 1
        nq = p * q
        b = self.fp.get(p // fp_step)
        if b is None:
            b = self.fp[p // fp_step] = [0, 0, 0, 0]
        if tr.side == BUY:
            self.v_buy += q
            self.q_buy += nq
            self.n_buy += tr.count
            b[0] += q
            b[2] += tr.count
        elif tr.side == SELL:
            self.v_sell += q
            self.q_sell += nq
            self.n_sell += tr.count
            b[1] += q
            b[3] += tr.count
        else:
            self.v_unk += q
            self.q_unk += nq
            self.n_unk += tr.count
        if tr.seq is not None:
            if self.first_id is None or tr.seq < self.first_id:
                self.first_id = tr.seq
            if self.last_id is None or tr.seq > self.last_id:
                self.last_id = tr.seq

    def row(self) -> dict:
        n_total = self.n_total if self.n_total is not None else self.n_buy + self.n_sell + self.n_unk
        return {
            "t": self.t, "o": self.o, "h": self.h, "l": self.l, "c": self.c,
            "v_buy": self.v_buy, "v_sell": self.v_sell, "v_unknown": self.v_unk,
            "q_buy": self.q_buy / NOTIONAL_SCALE, "q_sell": self.q_sell / NOTIONAL_SCALE,
            "q_unknown": self.q_unk / NOTIONAL_SCALE,
            "n_buy": self.n_buy, "n_sell": self.n_sell, "n_unknown": self.n_unk, "n_total": n_total,
            "first_id": self.first_id, "last_id": self.last_id, "source": self.source,
            "complete": 1 if (self.complete and self.gap_count == 0) else 0,
            "gap_count": self.gap_count,
        }


class DepthAcc:
    """1-minute accumulator of book samples (local time)."""

    __slots__ = ("t", "samples", "ticks", "mid_sum", "spread_sum", "lag_sum", "lag_n", "bb", "ba",
                 "band", "cov", "full", "epoch_start", "heat", "heat_cov", "nb", "time_ms")

    def __init__(self, t: int, nbands: int) -> None:
        self.t = t
        self.samples = 0          # valid samples
        self.time_ms = 0          # time represented by the valid samples (weights)
        self.ticks = 0            # sampling ticks while running
        self.mid_sum = 0.0
        self.spread_sum = 0.0
        self.lag_sum = 0.0
        self.lag_n = 0
        self.bb = self.ba = 0
        self.nb = nbands
        # per band: [sum bid qty, sum bid notional, sum ask qty, sum ask notional] over fully covered samples
        self.band = [[0.0, 0.0, 0.0, 0.0] for _ in range(nbands)]
        self.cov = [[0.0, 0.0] for _ in range(nbands)]   # sum of coverage (bid, ask)
        self.full = [[0, 0] for _ in range(nbands)]      # count of fully covered samples (bid, ask)
        self.epoch_start = False
        self.heat: Dict[int, List[float]] = {}           # bucket -> [sum_b, sum_a, max_b, max_a]
        self.heat_cov: Dict[int, int] = {}               # bucket -> covered sample count

    def add(self, s: BookSample, lag: Optional[int], w: int = 1000) -> None:
        """Add one sample representing ``w`` ms of book time (time weight)."""
        self.samples += 1
        self.time_ms += w
        self.mid_sum += s.mid / SCALE * w
        self.spread_sum += (s.best_ask - s.best_bid) / s.mid * 1e4 * w
        if lag is not None:
            self.lag_sum += lag
            self.lag_n += 1
        self.bb, self.ba = s.best_bid, s.best_ask
        for k in range(self.nb):
            bq, bn, cb = s.bids[k]
            aq, an, ca = s.asks[k]
            acc = self.band[k]
            cv = self.cov[k]
            fl = self.full[k]
            cv[0] += cb * w
            cv[1] += ca * w
            if cb >= 1.0:
                acc[0] += bq / SCALE * w
                acc[1] += bn / NOTIONAL_SCALE * w
                fl[0] += w
            if ca >= 1.0:
                acc[2] += aq / SCALE * w
                acc[3] += an / NOTIONAL_SCALE * w
                fl[1] += w
        heat = self.heat
        for idx, q in s.heat_bid.items():
            h = heat.get(idx)
            v = q / SCALE
            if h is None:
                heat[idx] = [v * w, 0.0, v, 0.0]
            else:
                h[0] += v * w
                if v > h[2]:
                    h[2] = v
        for idx, q in s.heat_ask.items():
            h = heat.get(idx)
            v = q / SCALE
            if h is None:
                heat[idx] = [0.0, v * w, 0.0, v]
            else:
                h[1] += v * w
                if v > h[3]:
                    h[3] = v
        hc = self.heat_cov
        for idx in range(s.heat_lo_idx, s.heat_hi_idx + 1):
            hc[idx] = hc.get(idx, 0) + w

    def row(self, bands: Sequence[float], min_full: float) -> dict:
        n = self.samples
        W = self.time_ms
        out_bands = {}
        for k, r in enumerate(bands):
            acc, cv, fl = self.band[k], self.cov[k], self.full[k]
            ok_b = W > 0 and fl[0] >= min_full * W
            ok_a = W > 0 and fl[1] >= min_full * W
            out_bands[repr(r)] = [
                acc[0] / fl[0] if ok_b else None,
                acc[1] / fl[0] if ok_b else None,
                acc[2] / fl[1] if ok_a else None,
                acc[3] / fl[1] if ok_a else None,
                round(cv[0] / W, 4) if W else 0.0,
                round(cv[1] / W, 4) if W else 0.0,
            ]
        return {
            "t": self.t,
            "samples": n,
            "time_ms": W,
            "ticks": self.ticks,
            "mid": self.mid_sum / W if W else None,
            "best_bid": self.bb / SCALE if n else None,
            "best_ask": self.ba / SCALE if n else None,
            "spread_bps": self.spread_sum / W if W else None,
            "lag_ms": self.lag_sum / self.lag_n if self.lag_n else None,
            "epoch_start": 1 if self.epoch_start else 0,
            "bands": out_bands,
        }

    def heat_row(self, min_cov: float) -> Optional[dict]:
        if not self.heat_cov or not self.samples:
            return None
        lo = min(self.heat_cov)
        hi = max(self.heat_cov)
        nan = float("nan")
        size = hi - lo + 1
        bm = [nan] * size
        am = [nan] * size
        bx = [nan] * size
        ax = [nan] * size
        need = max(1, int(math.ceil(min_cov * self.time_ms)))
        for idx, c in self.heat_cov.items():
            if c < need:
                continue
            j = idx - lo
            h = self.heat.get(idx)
            if h is None:
                bm[j] = am[j] = bx[j] = ax[j] = 0.0
            else:
                bm[j] = h[0] / c
                am[j] = h[1] / c
                bx[j] = h[2]
                ax[j] = h[3]
        return {"t": self.t, "origin": lo, "bid_mean": bm, "ask_mean": am, "bid_max": bx,
                "ask_max": ax, "samples": self.samples}


class DerivAcc:
    __slots__ = ("t", "oi", "oi_value", "funding", "next_funding", "mark", "liq_long_q", "liq_short_q",
                 "liq_long_n", "liq_short_n", "liq_long_v", "liq_short_v")

    def __init__(self, t: int) -> None:
        self.t = t
        self.oi = self.oi_value = self.funding = self.mark = None
        self.next_funding = None
        self.liq_long_q = self.liq_short_q = 0.0
        self.liq_long_v = self.liq_short_v = 0.0
        self.liq_long_n = self.liq_short_n = 0

    def row(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


class TradeSeq:
    """Continuity of an integer trade sequence (Binance aggregate trade id)."""

    def __init__(self) -> None:
        self.last: Optional[int] = None
        self.holes: List[List[int]] = []     # [lo, hi] inclusive ranges still missing
        self.duplicates = 0

    def check(self, seq: int) -> Tuple[bool, Optional[Tuple[int, int]]]:
        """Returns (accept, new_gap)."""
        if self.last is None:
            self.last = seq
            return True, None
        if seq == self.last + 1:
            self.last = seq
            return True, None
        if seq > self.last + 1:
            gap = (self.last + 1, seq - 1)
            self.holes.append([gap[0], gap[1]])
            self.last = seq
            return True, gap
        for h in self.holes:
            if h[0] <= seq <= h[1]:
                if seq == h[0]:
                    h[0] += 1
                elif seq == h[1]:
                    h[1] -= 1
                else:
                    self.holes.append([seq + 1, h[1]])
                    h[1] = seq - 1
                self.holes = [x for x in self.holes if x[0] <= x[1]]
                return True, None
        self.duplicates += 1
        return False, None

    def reset_tracking(self) -> None:
        self.last = None


class Pipeline:
    def __init__(self, inst: Instrument, book: LocalBook, *, fp_step: int, heat_step: int,
                 heat_pct: float = 3.0, bands: Sequence[float] = ALL_BANDS,
                 min_full: float = 0.9, heat_min_cov: float = 0.5,
                 sample_ms: int = 1000, keep_minutes: int = 240) -> None:
        self.inst = inst
        self.book = book
        self.fp_step = fp_step
        self.heat_step = heat_step
        self.heat_pct = heat_pct
        self.bands = tuple(bands)
        self.min_full = min_full
        self.heat_min_cov = heat_min_cov
        self.sample_ms = sample_ms
        self.keep_minutes = keep_minutes
        self.candles: Dict[int, CandleAcc] = {}
        self.depth: Dict[int, DepthAcc] = {}
        self.deriv: Dict[int, DerivAcc] = {}
        self.dirty_candles: Set[int] = set()
        self.closed_depth: List[Tuple[dict, Optional[dict]]] = []
        self.dirty_deriv: Set[int] = set()
        self.seq = TradeSeq()
        self.trade_stream_since: Optional[int] = None   # exchange time since which the trade stream is continuous
        self.last_epoch = book.epoch
        self.last_sample: Optional[BookSample] = None
        self.last_trade: Optional[Trade] = None
        self.trades_total = 0

    # ---- trades ------------------------------------------------------------
    def trade_stream_started(self, t_exch: int) -> None:
        """Called on (re)connect of the trade stream: candles before this point are
        not covered from the start."""
        self.trade_stream_since = t_exch

    def on_trade(self, tr: Trade, backfill: bool = False) -> Optional[Tuple[int, int]]:
        """Add a trade. Returns a new sequence gap (lo, hi) that should be backfilled."""
        gap = None
        if tr.seq is not None:
            ok, gap = self.seq.check(tr.seq)
            if not ok:
                return None
        m = minute_of(tr.t_exch)
        c = self.candles.get(m)
        if c is None:
            since = self.trade_stream_since
            # complete only if the trade stream was continuous since the minute start;
            # sequence gaps detected later are tracked through gap_count
            complete = since is not None and since <= m
            c = self.candles[m] = CandleAcc(m, complete)
        c.add(tr, self.fp_step)
        if gap is not None:
            c.gap_count += 1
        self.dirty_candles.add(m)
        self.trades_total += 1
        if not backfill:
            self.last_trade = tr
        return gap

    def gap_filled(self, lo: int, hi: int, minutes: Sequence[int]) -> None:
        """Backfill finished for a gap: clear gap markers of affected candles."""
        for m in minutes:
            c = self.candles.get(m)
            if c is not None and c.gap_count:
                c.gap_count -= 1
                self.dirty_candles.add(m)

    def seq_open_holes(self) -> List[List[int]]:
        return [h[:] for h in self.seq.holes]

    # ---- book sampling -----------------------------------------------------
    def on_tick(self, now: int, period_ms: Optional[int] = None) -> Optional[BookSample]:
        """Take one book sample at ``now`` that stands for ``period_ms`` of time."""
        m = minute_of(now)
        acc = self.depth.get(m)
        if acc is None:
            self._close_depth_before(m)
            acc = self.depth[m] = DepthAcc(m, len(self.bands))
        acc.ticks += 1
        if self.book.epoch != self.last_epoch:
            acc.epoch_start = True
            self.last_epoch = self.book.epoch
        s = self.book.sample(self.bands, self.heat_pct, self.heat_step)
        self.last_sample = s
        if s is not None:
            lag = (now - self.book.last_exch_ts) if self.book.last_exch_ts else None
            acc.add(s, lag, period_ms or self.sample_ms)
        return s

    def _close_depth_before(self, m: int) -> None:
        for t in sorted(k for k in self.depth if k < m):
            acc = self.depth.pop(t)
            self.closed_depth.append((acc.row(self.bands, self.min_full), acc.heat_row(self.heat_min_cov)))

    def flush_depth(self) -> None:
        """Close every open depth minute (used on shutdown / end of replay)."""
        self._close_depth_before(1 << 62)

    def current_heat_row(self) -> Optional[dict]:
        if not self.depth:
            return None
        return self.depth[max(self.depth)].heat_row(self.heat_min_cov)

    def current_depth_row(self) -> Optional[dict]:
        if not self.depth:
            return None
        acc = self.depth[max(self.depth)]
        return acc.row(self.bands, self.min_full)

    # ---- derivatives -------------------------------------------------------
    def _deriv(self, ts: int) -> DerivAcc:
        m = minute_of(ts)
        d = self.deriv.get(m)
        if d is None:
            d = self.deriv[m] = DerivAcc(m)
        self.dirty_deriv.add(m)
        return d

    def on_deriv(self, ts: int, **kw) -> None:
        d = self._deriv(ts)
        for k, v in kw.items():
            if v is not None:
                setattr(d, k, v)

    def on_liquidation(self, liq: Liquidation) -> None:
        d = self._deriv(liq.t_exch)
        q = liq.qty / SCALE
        v = liq.price * liq.qty / NOTIONAL_SCALE
        if liq.side == "long":
            d.liq_long_q += q
            d.liq_long_v += v
            d.liq_long_n += 1
        else:
            d.liq_short_q += q
            d.liq_short_v += v
            d.liq_short_n += 1

    # ---- persistence helpers -----------------------------------------------
    def take_dirty_candles(self) -> List[dict]:
        rows = [self.candles[m].row() for m in sorted(self.dirty_candles) if m in self.candles]
        self.dirty_candles.clear()
        return rows

    def take_footprints(self, minutes: Sequence[int]) -> List[dict]:
        out = []
        for m in minutes:
            c = self.candles.get(m)
            if c is None or not c.fp:
                continue
            out.append({"t": m, "step": self.fp_step, "partial": c.fp_partial or c.gap_count > 0,
                        "rows": sorted([k, *v] for k, v in c.fp.items())})
        return out

    def take_closed_depth(self) -> List[Tuple[dict, Optional[dict]]]:
        out, self.closed_depth = self.closed_depth, []
        return out

    def take_dirty_deriv(self) -> List[dict]:
        rows = [self.deriv[m].row() for m in sorted(self.dirty_deriv) if m in self.deriv]
        self.dirty_deriv.clear()
        return rows

    def evict(self, now: int) -> None:
        cut = minute_of(now) - self.keep_minutes * MIN_MS
        for d in (self.candles, self.deriv):
            for k in [k for k in d if k < cut]:
                del d[k]
