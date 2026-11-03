"""Build chart series on a fixed time grid from stored 1-minute rows and
compute indicators for them. Used by the HTTP API, the screener and alerts."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import indicators as ind
from .book import ALL_BANDS, CORE_BANDS
from .numeric import SCALE

TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
BASE_MS = {"1h": 3_600_000, "4h": 14_400_000, "12h": 43_200_000, "1d": 86_400_000, "3d": 259_200_000,
           "7d": 604_800_000, "14d": 1_209_600_000}
BAND_KEYS = [repr(r) for r in ALL_BANDS]

DEFAULT_PARAMS: Dict[str, Any] = {
    "units": "quote",            # quote (notional) | base (quantity)
    "book_min_time_cov": 0.5,     # min share of a candle covered by valid book samples
    "z_base": "1d",
    "z_threshold": 3.0,
    "z_min_n": 30,
    "z_min_cov": 0.9,
    "z_metric": "band_volume",
    "dyn_lag": 1,                 # candles
    "dyn_ema": 0,                 # 0 = off
    "dom_mode": "imbalance",      # imbalance | bid_share
    "cvd_reset": "utc_day",       # utc_day | session | history
    "cvd_session_start": 0,       # minutes after 00:00 UTC
    "anom_window": 100,
    "anom_threshold": 3.0,
    "rsi_period": 14,
    "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
    "ob_depth": 0.01,
}


def params_hash(p: Dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(p, sort_keys=True).encode()).hexdigest()[:12]


def grid(t0: int, t1: int, tf: int) -> List[int]:
    start = t0 - t0 % tf
    return list(range(start, t1, tf))


def aggregate_candles(rows: Sequence[dict], times: Sequence[int], tf: int) -> Dict[str, list]:
    """Aggregate 1m candle rows into tf candles. A tf candle is complete only if
    all its minutes are present and complete. Trade counts by side are None if
    any minute comes from exchange klines (no side split there)."""
    n = len(times)
    t_index = {t: i for i, t in enumerate(times)}
    o = [None] * n; h = [None] * n; l = [None] * n; c = [None] * n
    vb = [0.0] * n; vs = [0.0] * n; vu = [0.0] * n
    qb = [0.0] * n; qs = [0.0] * n; qu = [0.0] * n
    nb: List[Optional[int]] = [0] * n; ns: List[Optional[int]] = [0] * n; nt = [0] * n
    present = [0] * n; complete = [0] * n
    split_ok = [True] * n   # False if any minute has no buy/sell split (e.g. Bybit klines)
    src = [set() for _ in range(n)]
    for r in rows:
        i = t_index.get(r["t"] - r["t"] % tf)
        if i is None or r["o"] is None:
            continue
        if o[i] is None:
            o[i] = r["o"] / SCALE
            h[i] = r["h"] / SCALE
            l[i] = r["l"] / SCALE
        else:
            h[i] = max(h[i], r["h"] / SCALE)
            l[i] = min(l[i], r["l"] / SCALE)
        c[i] = r["c"] / SCALE
        if r["v_buy"] is None or r["v_sell"] is None:
            split_ok[i] = False
        vb[i] += (r["v_buy"] or 0) / SCALE
        vs[i] += (r["v_sell"] or 0) / SCALE
        vu[i] += (r["v_unknown"] or 0) / SCALE
        qb[i] += r["q_buy"] or 0.0
        qs[i] += r["q_sell"] or 0.0
        qu[i] += r["q_unknown"] or 0.0
        if r["source"] == "trades" and nb[i] is not None:
            nb[i] += r["n_buy"] or 0
            ns[i] += r["n_sell"] or 0
        else:
            nb[i] = ns[i] = None
        nt[i] += r["n_total"] or 0
        present[i] += 1
        complete[i] += 1 if r["complete"] else 0
        src[i].add(r["source"])
    per = tf // 60_000
    out_complete = [present[i] == per and complete[i] == per for i in range(n)]
    for i in range(n):
        if o[i] is None:
            vb[i] = vs[i] = vu[i] = qb[i] = qs[i] = qu[i] = None  # type: ignore
            nb[i] = ns[i] = None
            nt[i] = None  # type: ignore
        elif not split_ok[i]:
            vb[i] = vs[i] = qb[i] = qs[i] = None  # type: ignore
            nb[i] = ns[i] = None
    return {"t": list(times), "o": o, "h": h, "l": l, "c": c, "v_buy": vb, "v_sell": vs, "v_unknown": vu,
            "q_buy": qb, "q_sell": qs, "q_unknown": qu, "n_buy": nb, "n_sell": ns, "n_total": nt,
            "minutes": present, "complete": out_complete, "source": [sorted(s) for s in src]}


def aggregate_depth(rows: Sequence[dict], times: Sequence[int], tf: int, min_time_cov: float,
                    bands: Sequence[str] = BAND_KEYS) -> Dict[str, Any]:
    """Time-weighted mean (weights = valid samples) of the 1m depth frames."""
    n = len(times)
    t_index = {t: i for i, t in enumerate(times)}
    samples = [0] * n      # time weight: sampled book time in ms
    mid = [0.0] * n
    spread = [0.0] * n
    lag = [0.0] * n
    lag_n = [0] * n
    epoch = [False] * n
    acc = {k: [[0.0, 0.0, 0.0, 0.0, 0, 0, 0.0, 0.0] for _ in range(n)] for k in bands}
    for r in rows:
        i = t_index.get(r["t"] - r["t"] % tf)
        if i is None:
            continue
        s = r.get("time_ms")
        if s is None:            # rows written before migration 0002: 1 sample = 1 s
            s = r["samples"] * 1000
        if r["epoch_start"]:
            epoch[i] = True
        if not s:
            continue
        samples[i] += s
        mid[i] += (r["mid"] or 0) * s
        spread[i] += (r["spread_bps"] or 0) * s
        if r["lag_ms"] is not None:
            lag[i] += r["lag_ms"] * s
            lag_n[i] += s
        for k in bands:
            v = r["bands"].get(k)
            if v is None:
                continue
            a = acc[k][i]
            if v[0] is not None:
                a[0] += v[0] * s; a[1] += v[1] * s; a[4] += s
            if v[2] is not None:
                a[2] += v[2] * s; a[3] += v[3] * s; a[5] += s
            a[6] += v[4] * s
            a[7] += v[5] * s
    out: Dict[str, Any] = {"t": list(times), "samples": samples, "epoch_start": epoch}
    tcov = [min(1.0, samples[i] / tf) for i in range(n)]
    out["time_cov"] = tcov
    out["mid"] = [mid[i] / samples[i] if samples[i] else None for i in range(n)]
    out["spread_bps"] = [spread[i] / samples[i] if samples[i] else None for i in range(n)]
    out["lag_ms"] = [lag[i] / lag_n[i] if lag_n[i] else None for i in range(n)]
    bands_out = {}
    for k in bands:
        bq, bn, aq, an, cb, ca = [], [], [], [], [], []
        for i in range(n):
            a = acc[k][i]
            s = samples[i]
            okb = s and a[4] >= 0.9 * s and tcov[i] >= min_time_cov
            oka = s and a[5] >= 0.9 * s and tcov[i] >= min_time_cov
            bq.append(a[0] / a[4] if okb else None)
            bn.append(a[1] / a[4] if okb else None)
            aq.append(a[2] / a[5] if oka else None)
            an.append(a[3] / a[5] if oka else None)
            cb.append(round(a[6] / s, 4) if s else None)
            ca.append(round(a[7] / s, 4) if s else None)
        bands_out[k] = {"bid_qty": bq, "bid_notional": bn, "ask_qty": aq, "ask_notional": an,
                        "cov_bid": cb, "cov_ask": ca}
    out["bands"] = bands_out
    return out


def aggregate_deriv(rows: Sequence[dict], times: Sequence[int], tf: int) -> Dict[str, list]:
    n = len(times)
    t_index = {t: i for i, t in enumerate(times)}
    keys_last = ("oi", "oi_value", "funding", "next_funding", "mark")
    keys_sum = ("liq_long_q", "liq_short_q", "liq_long_v", "liq_short_v", "liq_long_n", "liq_short_n")
    out: Dict[str, list] = {k: [None] * n for k in keys_last + keys_sum}
    for r in rows:
        i = t_index.get(r["t"] - r["t"] % tf)
        if i is None:
            continue
        for k in keys_last:
            if r.get(k) is not None:
                out[k][i] = r[k]
        for k in keys_sum:
            if r.get(k) is not None:
                out[k][i] = (out[k][i] or 0) + r[k]
    out["t"] = list(times)
    return out


def _sub(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or b is None else a - b


def compute_indicators(c: Dict[str, list], d: Dict[str, Any], dv: Optional[Dict[str, list]], tf: int,
                       params: Dict[str, Any], live_index: Optional[int]) -> Dict[str, Any]:
    """Compute every indicator for the aligned candle/depth/deriv series.

    ``live_index`` is the index of the still-forming candle (or None); its
    values are marked provisional and it is excluded from statistical bases.
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    n = len(c["t"])
    unit_key = "notional" if p["units"] == "quote" else "qty"
    out: Dict[str, Any] = {"params": p, "params_hash": params_hash(p), "algorithm": ind.ALGORITHM_VERSION,
                           "units": {"book": "quote" if unit_key == "notional" else "base",
                                     "trades": "quote" if p["units"] == "quote" else "base"}}

    # ---- trade flow
    if p["units"] == "quote":
        buy, sell = c["q_buy"], c["q_sell"]
    else:
        buy, sell = c["v_buy"], c["v_sell"]
    delta = [_sub(b, s) for b, s in zip(buy, sell)]
    total = [None if b is None else b + s for b, s in zip(buy, sell)]
    tdelta = [None if (b is None or s is None) else b - s for b, s in zip(c["n_buy"], c["n_sell"])]
    incomplete = [not x for x in c["complete"]]
    cv, cv_part = ind.cvd(delta, c["t"], incomplete, p["cvd_reset"], int(p["cvd_session_start"]))
    out["flow"] = {"buy": buy, "sell": sell, "delta": delta, "total": total, "trades_delta": tdelta,
                   "trades_buy": c["n_buy"], "trades_sell": c["n_sell"], "trades_total": c["n_total"],
                   "cvd": cv, "cvd_partial": cv_part}

    # ---- anomalous flow (bases exclude incomplete and live candles)
    win = int(p["anom_window"])
    thr = float(p["anom_threshold"])
    an_out = {}
    peaks: List[dict] = []
    for side, x in (("buy", buy), ("sell", sell)):
        base_x = [None if (x[i] is None or incomplete[i] and i != live_index) else x[i] for i in range(n)]
        ratio, z, st = ind.anomaly(base_x, win, thr)
        if live_index is not None and x[live_index] is not None:
            st[live_index] = st[live_index] if st[live_index] != "nodata" else "warmup"
        an_out[side] = {"ratio": ratio, "z": z, "status": st}
        peaks += ind.anomaly_peaks(c["t"], x, ratio, thr, side)
    an_out["peaks"] = sorted(peaks, key=lambda q: q["t"])
    out["anomaly"] = an_out

    # ---- order book
    bands = d["bands"]
    dom: Dict[str, list] = {}
    obd: Dict[str, dict] = {}
    for r in CORE_BANDS:
        k = repr(r)
        b = bands[k]["bid_" + unit_key]
        a = bands[k]["ask_" + unit_key]
        f = ind.imbalance if p["dom_mode"] == "imbalance" else ind.bid_share
        dom[k] = [f(x, y) for x, y in zip(b, a)]
        obd[k] = {"bid": b, "ask": a, "delta": [ind.ob_delta(x, y) for x, y in zip(b, a)],
                  "cov_bid": bands[k]["cov_bid"], "cov_ask": bands[k]["cov_ask"]}
    ext = {}
    for r in ALL_BANDS[3:]:
        k = repr(r)
        b = bands[k]["bid_" + unit_key]
        a = bands[k]["ask_" + unit_key]
        ext[k] = {"bid": b, "ask": a, "delta": [ind.ob_delta(x, y) for x, y in zip(b, a)],
                  "cov_bid": bands[k]["cov_bid"], "cov_ask": bands[k]["cov_ask"]}
    out["ob_delta"] = obd
    out["ob_delta_ext"] = ext
    out["dominance"] = {"mode": p["dom_mode"], "lines": dom,
                        "range": [-100, 100] if p["dom_mode"] == "imbalance" else [0, 100],
                        "neutral": 0 if p["dom_mode"] == "imbalance" else 50}
    lag = int(p["dyn_lag"])
    dyn = {}
    imb_lines = {repr(r): [ind.imbalance(x, y) for x, y in zip(bands[repr(r)]["bid_" + unit_key],
                                                                 bands[repr(r)]["ask_" + unit_key])]
                 for r in CORE_BANDS}
    for k, line in imb_lines.items():
        dd = ind.dynamics(line, lag)
        if int(p["dyn_ema"]) > 1:
            dd = ind.ema(dd, int(p["dyn_ema"]))
        dyn[k] = dd
    out["dynamics"] = {"lag": lag, "ema": int(p["dyn_ema"]), "lines": dyn, "unit": "pp"}

    # ---- band clouds: disjoint belts 0-0.25, 0.25-0.5, 0.5-1 %, z-score per side
    base_slots = max(1, BASE_MS[p["z_base"]] // tf)
    belts = []
    edges = [(None, "0.0025"), ("0.0025", "0.005"), ("0.005", "0.01")]
    for lo, hi in edges:
        belt = {"lo": float(lo) if lo else 0.0, "hi": float(hi)}
        for side in ("bid", "ask"):
            outer = bands[hi][f"{side}_{unit_key}"]
            inner = bands[lo][f"{side}_{unit_key}"] if lo else [0.0] * n
            x = [_sub(a, b) for a, b in zip(outer, inner)]
            # the base of z(t) only holds slots before t, so the live (last) candle never
            # enters a base; its own z is provisional and flagged by `live_index`
            z, st = ind.zscore(x, base_slots, int(p["z_min_n"]), float(p["z_min_cov"]))
            belt[side] = {"value": x, "z": z, "status": st}
        belts.append(belt)
    out["belts"] = {"base": p["z_base"], "slots": base_slots, "threshold": p["z_threshold"], "items": belts}

    # ---- classic
    close = [c["c"][i] for i in range(n)]
    closed = [None if (live_index is not None and i == live_index) else v for i, v in enumerate(close)]
    out["rsi"] = {"period": int(p["rsi_period"]), "values": ind.rsi(closed, int(p["rsi_period"]))}
    m, s, h = ind.macd(closed, int(p["macd_fast"]), int(p["macd_slow"]), int(p["macd_signal"]))
    out["macd"] = {"macd": m, "signal": s, "hist": h}
    if live_index is not None and close[live_index] is not None:
        out["rsi"]["live"] = ind.rsi(close[:live_index + 1], int(p["rsi_period"]))[-1]
        ml, sl, hl = ind.macd(close[:live_index + 1], int(p["macd_fast"]), int(p["macd_slow"]), int(p["macd_signal"]))
        out["macd"]["live"] = {"macd": ml[-1], "signal": sl[-1], "hist": hl[-1]}

    # ---- derivatives
    if dv is not None:
        oi = dv["oi"]
        out["deriv"] = {"oi": oi, "oi_value": dv["oi_value"], "oi_change": [None] + [_sub(oi[i], oi[i - 1]) for i in range(1, n)],
                        "funding": dv["funding"], "next_funding": dv["next_funding"], "mark": dv["mark"],
                        "liq_long": dv["liq_long_v" if p["units"] == "quote" else "liq_long_q"],
                        "liq_short": dv["liq_short_v" if p["units"] == "quote" else "liq_short_q"],
                        "liq_long_n": dv["liq_long_n"], "liq_short_n": dv["liq_short_n"]}
    return out


def clean(obj: Any) -> Any:
    """Replace NaN/inf with None for JSON."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    return obj
