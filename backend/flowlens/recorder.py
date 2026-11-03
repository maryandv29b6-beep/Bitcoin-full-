"""Raw event recording and deterministic replay.

Recording (``record_raw: true``) appends every raw exchange message, REST
snapshot, book reset and sampling tick with its local receive time to hourly
gzip JSONL files: ``{raw_dir}/{exchange}_{market}_{symbol}/{YYYYmmddHH}.jsonl.gz``.

Replay feeds such a file through a fresh sync state machine and pipeline. As
the pipeline is driven only by its inputs, replaying a recording reproduces the
aggregates of the live run exactly (tests/test_replay.py).
"""
from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path
from typing import IO, Any, Dict, Iterable, Iterator, List, Optional, Tuple


class Recorder:
    def __init__(self, root: str, key: str, retention_hours: float = 6.0) -> None:
        self.dir = Path(root) / key.replace(":", "_")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.retention_hours = retention_hours
        self._fh: Optional[IO[bytes]] = None
        self._hour: Optional[str] = None
        self.lines = 0

    def write(self, kind: str, recv: int, data: Any) -> None:
        hour = time.strftime("%Y%m%d%H", time.gmtime(recv / 1000))
        if hour != self._hour:
            self.close()
            self._hour = hour
            self._fh = gzip.open(self.dir / f"{hour}.jsonl.gz", "ab", compresslevel=5)
            self._cleanup()
        assert self._fh is not None
        self._fh.write(json.dumps({"k": kind, "r": recv, "d": data}, separators=(",", ":")).encode() + b"\n")
        self.lines += 1

    def flush(self) -> None:
        if self._fh:
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def _cleanup(self) -> None:
        cut = time.time() - self.retention_hours * 3600
        for f in self.dir.glob("*.jsonl.gz"):
            if f.stat().st_mtime < cut:
                f.unlink(missing_ok=True)


def read_events(paths: Iterable[str]) -> Iterator[Tuple[str, int, Any]]:
    for p in paths:
        with gzip.open(p, "rb") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue  # truncated last line of an unclosed file
                yield o["k"], o["r"], o["d"]


def replay(events: Iterable[Tuple[str, int, Any]], exchange: str, market: str, *, fp_step: int, heat_step: int,
           heat_pct: float = 3.0, snapshot_limit: Optional[int] = None, depth: int = 200) -> Dict[str, Any]:
    """Run recorded events through sync + pipeline; returns closed aggregates."""
    from .adapters.binance import parse_agg_trade
    from .adapters.bybit import parse_public_trade
    from .book import LocalBook
    from .models import Instrument
    from .pipeline import Pipeline
    from .sync import BinanceFuturesSync, BinanceSpotSync, BybitSync

    book = LocalBook()
    if exchange == "bybit":
        sync: Any = BybitSync(book, depth)
        parse_trade = parse_public_trade
    elif market == "perp" and exchange == "binance":
        sync = BinanceFuturesSync(book, snapshot_limit or 1000)
        parse_trade = parse_agg_trade
    else:
        sync = BinanceSpotSync(book, snapshot_limit or 5000)
        parse_trade = parse_agg_trade
    pipe = Pipeline(Instrument(exchange, market, "REPLAY"), book, fp_step=fp_step, heat_step=heat_step,
                    heat_pct=heat_pct)
    started = False
    for kind, recv, data in events:
        if kind == "depth":
            sync.on_message(data)
        elif kind == "snap":
            sync.on_snapshot(data)
        elif kind == "snap_refresh":
            sync.on_refresh_snapshot(data)
        elif kind == "reset":
            sync.reset()
        elif kind == "trade":
            tr = parse_trade(data, recv)
            if not started:
                started = True
                pipe.trade_stream_started(tr.t_exch)
            pipe.on_trade(tr)
        elif kind == "trade_start":
            pipe.trade_stream_started(data)
            started = True
        elif kind == "tick":
            pipe.on_tick(recv)
    pipe.flush_depth()
    return {"depth": [r for r, _ in pipe.closed_depth], "heat": [h for _, h in pipe.closed_depth],
            "candles": [pipe.candles[m].row() for m in sorted(pipe.candles)], "stats": sync.stats.as_dict()}
