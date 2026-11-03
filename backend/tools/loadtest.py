"""Load test: collector + N Binance-spot-like instruments against the mock exchange.

The mock runs in a separate process (its CPU is not counted). Reports CPU,
memory, sampling-clock lag, DB flush time, message rate and DB growth.

  python tools/loadtest.py --symbols 10 --seconds 120
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import resource
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowlens.collector import Collector  # noqa: E402
from flowlens.config import Config  # noqa: E402
from flowlens.mockexchange import PRICES  # noqa: E402
from flowlens.storage import Storage  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def rss_mb() -> float:
    with open("/proc/self/statm") as f:
        pages = int(f.read().split()[1])
    return pages * os.sysconf("SC_PAGE_SIZE") / 1e6


async def run(n: int, seconds: int, levels: int) -> dict:
    syms = list(PRICES)[:n]
    port = free_port()
    mock = subprocess.Popen([sys.executable, "-m", "flowlens", "mock-exchange", "--port", str(port), "--symbols", ",".join(syms)],
                            cwd=str(Path(__file__).resolve().parents[1]), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    await asyncio.sleep(2.0)
    tmp = tempfile.mkdtemp()
    cfg = Config(db_path=f"{tmp}/load.db", instruments=[{"exchange": "binance", "market": "spot", "symbol": s, "depth": 5000} for s in syms],
                 binance_spot_rest=f"http://127.0.0.1:{port}", binance_spot_ws=f"ws://127.0.0.1:{port}", kline_backfill_days=0.01)
    storage = Storage(cfg.db_path)
    col = Collector(cfg, storage)
    await col.start()
    t_start = time.time()
    while time.time() - t_start < 30 and not all(rt.status() == "LIVE" for rt in col.runtimes.values()):
        await asyncio.sleep(0.5)
    warm = time.time() - t_start
    samples = []
    ru0 = resource.getrusage(resource.RUSAGE_SELF)
    msgs0 = sum(c.messages for rt in col.runtimes.values() for c in rt.adapter.conns)
    t0 = time.time()
    while time.time() - t0 < seconds:
        await asyncio.sleep(5)
        ru = resource.getrusage(resource.RUSAGE_SELF)
        el = time.time() - t0
        cpu = (ru.ru_utime + ru.ru_stime - ru0.ru_utime - ru0.ru_stime) / el * 100
        msgs = sum(c.messages for rt in col.runtimes.values() for c in rt.adapter.conns) - msgs0
        h = col.health()
        samples.append({"t": round(el), "cpu_pct": round(cpu, 1), "rss_mb": round(rss_mb(), 1), "msg_per_s": round(msgs / el, 1),
                        "tick_lag_p50": h["tick_lag_ms_p50"], "tick_lag_max": h["tick_lag_ms_max"],
                        "flush_p50": h["flush_ms_p50"], "flush_max": h["flush_ms_max"]})
    statuses = {k: rt.status() for k, rt in col.runtimes.items()}
    gaps = sum(rt.sync.stats.gaps for rt in col.runtimes.values())
    levels_held = sum(len(rt.book.bids) + len(rt.book.asks) for rt in col.runtimes.values())
    await col.stop()
    storage.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db_mb = sum(f.stat().st_size for f in Path(tmp).glob("load.db*")) / 1e6
    tables = {}
    for tbl in ("candles_1m", "depth_1m", "heatmap_1m", "footprint_1m"):
        try:
            tables[tbl] = storage.db.execute("SELECT SUM(pgsize) FROM dbstat WHERE name=?", (tbl,)).fetchone()[0]
        except Exception:
            tables[tbl] = None
    minutes = storage.db.execute("SELECT COUNT(*) FROM depth_1m").fetchone()[0]
    storage.close()
    mock.terminate()
    last = samples[-1]
    return {"symbols": n, "seconds": seconds, "warmup_s": round(warm, 1), "statuses": statuses, "book_gaps": gaps,
            "levels_held": levels_held, "db_mb": round(db_mb, 2), "depth_minutes_stored": minutes,
            "bytes_per_instrument_minute": {k: (round(v / max(1, minutes)) if v else None) for k, v in tables.items()},
            "cpu_pct": last["cpu_pct"], "rss_mb_end": last["rss_mb"], "msg_per_s": last["msg_per_s"],
            "tick_lag_p50_ms": last["tick_lag_p50"], "tick_lag_max_ms": max(s["tick_lag_max"] or 0 for s in samples),
            "flush_p50_ms": last["flush_p50"], "flush_max_ms": max(s["flush_max"] or 0 for s in samples), "samples": samples}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=1)
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--levels", type=int, default=1500)
    a = ap.parse_args()
    res = asyncio.run(run(a.symbols, a.seconds, a.levels))
    res["host"] = {"cpus": os.cpu_count(), "python": platform.python_version(), "platform": platform.platform()}
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
