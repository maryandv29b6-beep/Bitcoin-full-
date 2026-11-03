"""Indicator formulas of FlowLens (our specification, see docs/FORMULAS.md).

All functions are pure and work on aligned lists where ``None`` means "no
valid value". None propagates: an indicator never turns missing input into 0.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

Num = Optional[float]

ALGORITHM_VERSION = "flowlens-1.0"


# ---- order book ---------------------------------------------------------------

def ob_delta(b: Num, a: Num) -> Num:
    """OrderbookDelta_r = B_r - A_r."""
    if b is None or a is None:
        return None
    return b - a


def imbalance(b: Num, a: Num) -> Num:
    """Imbalance_r = 100 * (B_r - A_r) / (B_r + A_r), range -100..100."""
    if b is None or a is None:
        return None
    den = b + a
    if den == 0:
        return None
    return 100.0 * (b - a) / den


def bid_share(b: Num, a: Num) -> Num:
    """BidShare_r = 100 * B_r / (B_r + A_r), range 0..100, balance = 50."""
    if b is None or a is None:
        return None
    den = b + a
    if den == 0:
        return None
    return 100.0 * b / den


def dynamics(x: Sequence[Num], lag: int) -> List[Num]:
    """Dynamics(t) = x(t) - x(t - lag) in percentage points; None if either is missing."""
    out: List[Num] = []
    for i, v in enumerate(x):
        j = i - lag
        if lag <= 0 or j < 0 or v is None or x[j] is None:
            out.append(None)
        else:
            out.append(v - x[j])
    return out


def ema(x: Sequence[Num], period: int) -> List[Num]:
    """EMA with alpha = 2/(period+1). A missing input yields None and restarts the EMA."""
    if period <= 1:
        return list(x)
    a = 2.0 / (period + 1)
    out: List[Num] = []
    prev: Num = None
    for v in x:
        if v is None:
            prev = None
            out.append(None)
            continue
        prev = v if prev is None else prev + a * (v - prev)
        out.append(prev)
    return out


# ---- z-score ------------------------------------------------------------------

def zscore(x: Sequence[Num], window: int, min_n: int = 30, min_cov: float = 0.9) -> Tuple[List[Num], List[str]]:
    """z(t) = (x(t) - mean(prev)) / std(prev) on a fixed time grid.

    ``window`` is the number of grid slots of the base period (e.g. 1 day of
    5-minute candles = 288). The base contains only the ``window`` slots
    strictly before t (no current or future values). std is the population
    standard deviation. The value is None with status:
      'warmup'   fewer than max(min_n, min_cov*window) valid values in the base
      'zero_var' variance is zero
      'nodata'   x(t) itself is missing
    """
    n = len(x)
    need = max(min_n, int(math.ceil(min_cov * window)))
    z: List[Num] = [None] * n
    st: List[str] = ["warmup"] * n
    # shift values by a reference to keep sum of squares well conditioned
    ref = next((v for v in x if v is not None), 0.0) or 0.0
    s = s2 = 0.0
    cnt = 0
    for i in range(n):
        # base = slots [i-window, i-1]
        if i - 1 >= 0 and x[i - 1] is not None:
            d = x[i - 1] - ref
            s += d
            s2 += d * d
            cnt += 1
        j = i - window - 1
        if j >= 0 and x[j] is not None:
            d = x[j] - ref
            s -= d
            s2 -= d * d
            cnt -= 1
        v = x[i]
        if v is None:
            st[i] = "nodata"
            continue
        if cnt < need or i < window:
            st[i] = "warmup"
            continue
        mean = s / cnt
        var = s2 / cnt - mean * mean
        scale = max(1.0, abs(mean), s2 / cnt)
        if var <= 1e-12 * scale:
            st[i] = "zero_var"
            continue
        z[i] = (v - ref - mean) / math.sqrt(var)
        st[i] = "ok"
    return z, st


# ---- trades -----------------------------------------------------------------------

def cvd(delta: Sequence[Num], times: Sequence[int], incomplete: Sequence[bool], mode: str = "utc_day",
        session_start_min: int = 0) -> Tuple[List[Num], List[bool]]:
    """Cumulative Volume Delta with reset modes:
      'utc_day'  reset at 00:00 UTC
      'session'  reset daily at session_start_min (minutes after 00:00 UTC)
      'history'  never reset inside the loaded range
    Returns (cvd, partial) where partial marks points accumulated over a gap
    (missing delta or incomplete candle) since the last reset.
    """
    out: List[Num] = []
    partial: List[bool] = []
    acc = 0.0
    part = False
    prev_key = None
    day = 86400_000
    for i, (d, t) in enumerate(zip(delta, times)):
        if mode == "utc_day":
            key = t // day
        elif mode == "session":
            key = (t - session_start_min * 60_000) // day
        else:
            key = 0
        if prev_key is None or key != prev_key:
            acc = 0.0
            part = False
            prev_key = key
        if d is None:
            part = True
            out.append(None)
            partial.append(True)
            continue
        if incomplete[i]:
            part = True
        acc += d
        out.append(acc)
        partial.append(part)
    return out, partial


def rsi(close: Sequence[Num], period: int = 14) -> List[Num]:
    """Wilder RSI. Missing closes restart the warm-up."""
    out: List[Num] = [None] * len(close)
    ag = al = 0.0
    n = 0
    prev: Num = None
    for i, c in enumerate(close):
        if c is None:
            prev, n, ag, al = None, 0, 0.0, 0.0
            continue
        if prev is None:
            prev = c
            continue
        ch = c - prev
        prev = c
        g, l = (ch, 0.0) if ch > 0 else (0.0, -ch)
        n += 1
        if n <= period:
            ag += g / period
            al += l / period
            if n < period:
                continue
        else:
            ag = (ag * (period - 1) + g) / period
            al = (al * (period - 1) + l) / period
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def macd(close: Sequence[Num], fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[List[Num], List[Num], List[Num]]:
    ef = ema(close, fast)
    es = ema(close, slow)
    line: List[Num] = []
    for i, (a, b) in enumerate(zip(ef, es)):
        # require the slow EMA to be warmed up for `slow` consecutive values
        ok = a is not None and b is not None and i >= slow - 1 and all(c is not None for c in close[i - slow + 1:i + 1])
        line.append(a - b if ok else None)
    sig = ema(line, signal)
    hist = [None if (m is None or s is None) else m - s for m, s in zip(line, sig)]
    return line, sig, hist


def anomaly(x: Sequence[Num], window: int = 100, threshold: float = 3.0, min_cov: float = 0.9
            ) -> Tuple[List[Num], List[Num], List[str]]:
    """Anomalous aggressive flow of one side: ratio = x / mean(prev `window`
    completed candles), z = (x - mean)/std (population). Status 'warmup' until
    the window has at least min_cov*window valid candles."""
    n = len(x)
    ratio: List[Num] = [None] * n
    z: List[Num] = [None] * n
    st: List[str] = ["warmup"] * n
    need = int(math.ceil(min_cov * window))
    s = s2 = 0.0
    cnt = 0
    for i in range(n):
        if i - 1 >= 0 and x[i - 1] is not None:
            s += x[i - 1]
            s2 += x[i - 1] ** 2
            cnt += 1
        j = i - window - 1
        if j >= 0 and x[j] is not None:
            s -= x[j]
            s2 -= x[j] ** 2
            cnt -= 1
        v = x[i]
        if v is None:
            st[i] = "nodata"
            continue
        if cnt < need or i < window:
            continue
        mean = s / cnt
        var = max(0.0, s2 / cnt - mean * mean)
        ratio[i] = v / mean if mean > 0 else None
        z[i] = (v - mean) / math.sqrt(var) if var > 1e-12 * max(1.0, mean * mean) else None
        st[i] = "ok"
    return ratio, z, st


def anomaly_peaks(times: Sequence[int], x: Sequence[Num], ratio: Sequence[Num], threshold: float,
                  side: str) -> List[dict]:
    """Merge consecutive candles with ratio >= threshold into episodes and
    return one label per episode at its maximum ratio."""
    peaks: List[dict] = []
    cur: Optional[dict] = None
    for i, r in enumerate(ratio):
        hit = r is not None and r >= threshold
        if hit:
            if cur is None:
                cur = {"i0": i, "i1": i, "imax": i, "sum": 0.0}
            cur["i1"] = i
            cur["sum"] += x[i] or 0.0
            if r > (ratio[cur["imax"]] or 0):
                cur["imax"] = i
        elif cur is not None:
            peaks.append(cur)
            cur = None
    if cur is not None:
        peaks.append(cur)
    return [{"t": times[p["imax"]], "t0": times[p["i0"]], "t1": times[p["i1"]], "side": side,
             "sum": p["sum"], "ratio": ratio[p["imax"]], "candles": p["i1"] - p["i0"] + 1} for p in peaks]


# ---- footprint / profile ---------------------------------------------------------

def value_area(buckets: Sequence[Tuple[int, float]], share: float = 0.7) -> Optional[dict]:
    """POC and Value Area from (bucket_index, volume) sorted by index.

    POC: bucket with the largest volume; ties -> lower price.
    Value Area: start at POC, repeatedly compare the next bucket above and the
    next bucket below and add the larger one; on a tie add both. Stop once the
    included volume >= share * total.
    """
    if not buckets:
        return None
    idx = [b[0] for b in buckets]
    vol = [b[1] for b in buckets]
    total = sum(vol)
    if total <= 0:
        return None
    poc = max(range(len(vol)), key=lambda k: (vol[k], -idx[k]))
    lo = hi = poc
    acc = vol[poc]
    target = share * total
    while acc < target and (lo > 0 or hi < len(vol) - 1):
        up = vol[hi + 1] if hi < len(vol) - 1 else -1.0
        dn = vol[lo - 1] if lo > 0 else -1.0
        if up > dn:
            hi += 1
            acc += up
        elif dn > up:
            lo -= 1
            acc += dn
        else:
            if hi < len(vol) - 1:
                hi += 1
                acc += up
            if lo > 0:
                lo -= 1
                acc += dn
    return {"poc": idx[poc], "val": idx[lo], "vah": idx[hi], "share": acc / total, "total": total}
