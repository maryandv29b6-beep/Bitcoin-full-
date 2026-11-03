"""SQLite storage with versioned migrations.

One write connection is used from the asyncio thread; reads run in a thread
pool with per-thread connections (WAL mode allows concurrent readers).
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
import struct
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import numeric
from .models import Instrument

MIGRATIONS = Path(__file__).parent / "migrations"

CANDLE_COLS = ("t", "o", "h", "l", "c", "v_buy", "v_sell", "v_unknown", "q_buy", "q_sell", "q_unknown",
               "n_buy", "n_sell", "n_unknown", "n_total", "first_id", "last_id", "source", "complete",
               "gap_count")
DERIV_COLS = ("t", "oi", "oi_value", "funding", "next_funding", "mark", "liq_long_q", "liq_short_q",
              "liq_long_v", "liq_short_v", "liq_long_n", "liq_short_n")


def pack_heat(row: dict) -> bytes:
    n = len(row["bid_mean"])
    arr = row["bid_mean"] + row["ask_mean"] + row["bid_max"] + row["ask_max"]
    return zlib.compress(struct.pack(f"<{4 * n}f", *arr), 6)


def unpack_heat(blob: bytes, n: int) -> Tuple[List[float], List[float], List[float], List[float]]:
    vals = struct.unpack(f"<{4 * n}f", zlib.decompress(blob))
    return list(vals[:n]), list(vals[n:2 * n]), list(vals[2 * n:3 * n]), list(vals[3 * n:])


class Storage:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = self._connect()
        self._local = threading.local()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="db-read")
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    # ---- migrations -----------------------------------------------------
    def migrate(self) -> int:
        self.db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = self.db.execute("SELECT MAX(version) FROM schema_version").fetchone()
        cur = row[0] or 0
        for f in sorted(MIGRATIONS.glob("*.sql")):
            ver = int(f.name.split("_", 1)[0])
            if ver <= cur:
                continue
            self.db.executescript("BEGIN;" + f.read_text("utf-8") + f";INSERT INTO schema_version VALUES ({ver});COMMIT;")
            cur = ver
        return cur

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.db.close()

    # ---- reads in threads --------------------------------------------------
    def _rconn(self) -> sqlite3.Connection:
        if self.path == ":memory:":
            return self.db
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = self._connect()
        return c

    async def run(self, fn, *args):
        if self.path == ":memory:":
            return fn(self.db, *args)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, lambda: fn(self._rconn(), *args))

    # ---- instruments -------------------------------------------------------
    def upsert_instrument(self, inst: Instrument, meta: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """Returns (id, meta). Existing meta (storage steps) wins so history stays consistent."""
        row = self.db.execute("SELECT id, meta FROM instruments WHERE exchange=? AND market=? AND symbol=?",
                              (inst.exchange, inst.market, inst.symbol)).fetchone()
        if row:
            old = json.loads(row["meta"])
            merged = {**meta, **old}
            self.db.execute("UPDATE instruments SET base=?, quote=?, tick_size=?, qty_step=?, contract_spec=?, meta=? WHERE id=?",
                            (inst.base, inst.quote, numeric.fmt(inst.tick), numeric.fmt(inst.step),
                             json.dumps(inst.contract), json.dumps(merged), row["id"]))
            return row["id"], merged
        cur = self.db.execute(
            "INSERT INTO instruments (exchange, market, symbol, base, quote, tick_size, qty_step, contract_spec, meta)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (inst.exchange, inst.market, inst.symbol, inst.base, inst.quote, numeric.fmt(inst.tick),
             numeric.fmt(inst.step), json.dumps(inst.contract), json.dumps(meta)))
        return cur.lastrowid, meta

    # ---- writes --------------------------------------------------------------
    def upsert_candles(self, iid: int, rows: Iterable[dict]) -> None:
        cols = ",".join(CANDLE_COLS)
        ph = ",".join("?" * (len(CANDLE_COLS) + 1))
        upd = ",".join(f"{c}=excluded.{c}" for c in CANDLE_COLS if c != "t")
        self.db.executemany(
            f"INSERT INTO candles_1m (instrument_id,{cols}) VALUES ({ph}) "
            f"ON CONFLICT(instrument_id,t) DO UPDATE SET {upd}",
            [(iid, *[r.get(c) for c in CANDLE_COLS]) for r in rows])

    def merge_kline_candles(self, iid: int, rows: Iterable[dict]) -> int:
        """Insert exchange klines where no row exists; replace incomplete
        trade-built rows (keeping their footprint, which stays marked partial)."""
        n = 0
        for r in rows:
            cur = self.db.execute("SELECT source, complete FROM candles_1m WHERE instrument_id=? AND t=?",
                                  (iid, r["t"])).fetchone()
            if cur is None:
                self.upsert_candles(iid, [r])
                n += 1
            elif cur["source"] == "trades" and not cur["complete"]:
                self.upsert_candles(iid, [{**r, "source": "kline_reconciled"}])
                n += 1
        return n

    def upsert_depth(self, iid: int, row: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO depth_1m (instrument_id,t,samples,ticks,mid,best_bid,best_ask,spread_bps,lag_ms,epoch_start,bands,time_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, row["t"], row["samples"], row["ticks"], row["mid"], row["best_bid"], row["best_ask"],
             row["spread_bps"], row["lag_ms"], row["epoch_start"], json.dumps(row["bands"], separators=(",", ":")),
             row.get("time_ms")))

    def upsert_heat(self, iid: int, row: dict, step: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO heatmap_1m (instrument_id,t,step,origin,n,samples,data) VALUES (?,?,?,?,?,?,?)",
            (iid, row["t"], step, row["origin"], len(row["bid_mean"]), row["samples"], pack_heat(row)))

    def upsert_footprints(self, iid: int, fps: Iterable[dict]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO footprint_1m (instrument_id,t,step,partial,data) VALUES (?,?,?,?,?)",
            [(iid, f["t"], f["step"], 1 if f["partial"] else 0,
              zlib.compress(json.dumps(f["rows"], separators=(",", ":")).encode(), 6)) for f in fps])

    def upsert_deriv(self, iid: int, rows: Iterable[dict]) -> None:
        cols = ",".join(DERIV_COLS)
        ph = ",".join("?" * (len(DERIV_COLS) + 1))
        upd = ",".join(f"{c}=COALESCE(excluded.{c},{c})" for c in DERIV_COLS if c != "t")
        self.db.executemany(
            f"INSERT INTO deriv_1m (instrument_id,{cols}) VALUES ({ph}) ON CONFLICT(instrument_id,t) DO UPDATE SET {upd}",
            [(iid, *[r.get(c) for c in DERIV_COLS]) for r in rows])

    def quality_event(self, iid: int, kind: str, t_start: int, t_end: Optional[int] = None, detail: str = "") -> None:
        self.db.execute("INSERT INTO quality_events (instrument_id,t_start,t_end,kind,detail) VALUES (?,?,?,?,?)",
                        (iid, t_start, t_end, kind, detail))

    def clear_instrument(self, iid: int) -> None:
        for tbl in ("candles_1m", "depth_1m", "heatmap_1m", "footprint_1m", "deriv_1m", "quality_events"):
            self.db.execute(f"DELETE FROM {tbl} WHERE instrument_id=?", (iid,))

    def purge(self, retention_days: float) -> None:
        cut = int(time.time() * 1000 - retention_days * 86400_000)
        for tbl in ("candles_1m", "depth_1m", "heatmap_1m", "footprint_1m", "deriv_1m"):
            self.db.execute(f"DELETE FROM {tbl} WHERE t < ?", (cut,))
        self.db.execute("DELETE FROM quality_events WHERE t_start < ?", (cut,))

    # ---- settings / alerts -----------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, json.dumps(value)))

    def alert_rules(self) -> List[dict]:
        out = []
        for r in self.db.execute("SELECT * FROM alert_rules ORDER BY id"):
            out.append({"id": r["id"], "created": r["created"], "enabled": bool(r["enabled"]),
                        "instrument": r["instrument"], "rule": json.loads(r["rule"]), "last_fired": r["last_fired"]})
        return out

    def add_alert_rule(self, instrument: str, rule: dict) -> int:
        cur = self.db.execute("INSERT INTO alert_rules (created,enabled,instrument,rule) VALUES (?,?,?,?)",
                              (int(time.time() * 1000), 1, instrument, json.dumps(rule)))
        return cur.lastrowid

    def update_alert_rule(self, rid: int, enabled: Optional[bool] = None, last_fired: Optional[int] = None) -> None:
        if enabled is not None:
            self.db.execute("UPDATE alert_rules SET enabled=? WHERE id=?", (1 if enabled else 0, rid))
        if last_fired is not None:
            self.db.execute("UPDATE alert_rules SET last_fired=? WHERE id=?", (last_fired, rid))

    def delete_alert_rule(self, rid: int) -> None:
        self.db.execute("DELETE FROM alert_rules WHERE id=?", (rid,))

    def log_alert(self, rid: int, t: int, message: str, data: dict, delivery: str) -> int:
        cur = self.db.execute("INSERT INTO alert_log (rule_id,t,message,data,delivery) VALUES (?,?,?,?,?)",
                              (rid, t, message, json.dumps(data), delivery))
        return cur.lastrowid

    def alert_log(self, limit: int = 200) -> List[dict]:
        return [{"id": r["id"], "rule_id": r["rule_id"], "t": r["t"], "message": r["message"],
                 "data": json.loads(r["data"]), "delivery": r["delivery"]}
                for r in self.db.execute("SELECT * FROM alert_log ORDER BY id DESC LIMIT ?", (limit,))]


# ---- read functions (executed in worker threads) ------------------------------

def q_candles(db: sqlite3.Connection, iid: int, t0: int, t1: int) -> List[dict]:
    cur = db.execute(f"SELECT {','.join(CANDLE_COLS)} FROM candles_1m WHERE instrument_id=? AND t>=? AND t<? ORDER BY t",
                     (iid, t0, t1))
    return [dict(r) for r in cur]


def q_depth(db: sqlite3.Connection, iid: int, t0: int, t1: int) -> List[dict]:
    out = []
    for r in db.execute("SELECT * FROM depth_1m WHERE instrument_id=? AND t>=? AND t<? ORDER BY t", (iid, t0, t1)):
        d = dict(r)
        d["bands"] = json.loads(d["bands"])
        out.append(d)
    return out


def q_heat(db: sqlite3.Connection, iid: int, t0: int, t1: int) -> List[dict]:
    out = []
    for r in db.execute("SELECT t,step,origin,n,samples,data FROM heatmap_1m WHERE instrument_id=? AND t>=? AND t<? ORDER BY t",
                        (iid, t0, t1)):
        bm, am, bx, ax = unpack_heat(r["data"], r["n"])
        out.append({"t": r["t"], "step": r["step"], "origin": r["origin"], "samples": r["samples"],
                    "bid_mean": bm, "ask_mean": am, "bid_max": bx, "ask_max": ax})
    return out


def q_footprint(db: sqlite3.Connection, iid: int, t0: int, t1: int) -> List[dict]:
    return [{"t": r["t"], "step": r["step"], "partial": bool(r["partial"]),
             "rows": json.loads(zlib.decompress(r["data"]))}
            for r in db.execute("SELECT * FROM footprint_1m WHERE instrument_id=? AND t>=? AND t<? ORDER BY t",
                                (iid, t0, t1))]


def q_deriv(db: sqlite3.Connection, iid: int, t0: int, t1: int) -> List[dict]:
    return [dict(r) for r in db.execute(
        f"SELECT {','.join(DERIV_COLS)} FROM deriv_1m WHERE instrument_id=? AND t>=? AND t<? ORDER BY t", (iid, t0, t1))]


def q_quality(db: sqlite3.Connection, iid: int, t0: int, t1: int) -> List[dict]:
    return [dict(r) for r in db.execute(
        "SELECT t_start,t_end,kind,detail FROM quality_events WHERE instrument_id=? AND t_start>=? AND t_start<? ORDER BY t_start",
        (iid, t0, t1))]


def q_first(db: sqlite3.Connection, iid: int) -> Dict[str, Optional[int]]:
    out = {}
    for tbl in ("candles_1m", "depth_1m", "heatmap_1m", "footprint_1m", "deriv_1m"):
        row = db.execute(f"SELECT MIN(t) FROM {tbl} WHERE instrument_id=?", (iid,)).fetchone()
        out[tbl] = row[0]
    row = db.execute("SELECT MIN(t) FROM candles_1m WHERE instrument_id=? AND source='trades'", (iid,)).fetchone()
    out["trades"] = row[0]
    return out


def q_sizes(db: sqlite3.Connection) -> Dict[str, int]:
    out = {}
    for tbl in ("candles_1m", "depth_1m", "heatmap_1m", "footprint_1m", "deriv_1m", "quality_events"):
        out[tbl] = db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    return out
