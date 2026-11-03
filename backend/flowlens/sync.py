"""Exchange-specific order book synchronisation state machines.

Each class implements the documented procedure of one API and is pure:
it receives already-decoded messages / snapshots and returns a list of
actions for the I/O adapter. No generic "id + 1" rule is shared between them.

Actions returned:
  "need_snapshot"   adapter must fetch a REST depth snapshot and call on_snapshot
  "need_refresh"    adapter should fetch a snapshot for a coverage refresh
  "gap"             a sequence gap was detected, the book was invalidated
  "synced"          a new valid sequence started (book.epoch incremented)
  "refreshed"       coverage refresh swapped in without breaking continuity
"""
from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, List, Optional

from .book import LocalBook


class SyncStats:
    __slots__ = ("events", "applied", "ignored", "gaps", "resyncs", "refreshes",
                 "snapshot_retries", "seq_jumps", "buffer_overflow")

    def __init__(self) -> None:
        for k in self.__slots__:
            setattr(self, k, 0)

    def as_dict(self) -> Dict[str, int]:
        return {k: getattr(self, k) for k in self.__slots__}


class BinanceSpotSync:
    """Binance Spot diff-depth procedure ("How to manage a local order book
    correctly", spot web-socket-streams.md):

    buffer events, note U of the first one; snapshot with lastUpdateId L;
    if L < U(first) fetch again; drop buffered events with u <= L; set book to
    snapshot; apply events: ignore if u < local id, gap if U > local id + 1.
    """

    name = "binance-spot"

    def __init__(self, book: LocalBook, snapshot_limit: int = 5000, buffer_max: int = 20000,
                 recent_max: int = 1500) -> None:
        self.book = book
        self.limit = snapshot_limit
        self.state = "init"           # init -> buffering -> live
        self.buffer: Deque[dict] = deque(maxlen=buffer_max)
        self.recent: Deque[dict] = deque(maxlen=recent_max)  # applied events, for coverage refresh
        self.pending: Optional[LocalBook] = None
        self.stats = SyncStats()

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _apply_to(book: LocalBook, ev: dict) -> str:
        """Update procedure on an arbitrary book. Returns 'ok', 'ignore' or 'gap'."""
        if ev["u"] < book.update_id:
            return "ignore"
        if ev["U"] > book.update_id + 1:
            return "gap"
        book.apply(ev["b"], ev["a"])
        book.update_id = ev["u"]
        return "ok"

    def reset(self) -> None:
        """Called by the adapter when the stream reconnects."""
        self.book.invalidate()
        self.state = "init"
        self.buffer.clear()
        self.recent.clear()
        self.pending = None

    # -- stream ---------------------------------------------------------
    def on_message(self, ev: dict) -> List[str]:
        self.stats.events += 1
        if ev.get("E"):
            self.book.last_exch_ts = ev["E"]
        if self.state == "init":
            self.buffer.clear()
            self.buffer.append(ev)
            self.state = "buffering"
            return ["need_snapshot"]
        if self.state == "buffering":
            if len(self.buffer) == self.buffer.maxlen:
                self.stats.buffer_overflow += 1
            self.buffer.append(ev)
            return []
        res = self._apply_to(self.book, ev)
        if res == "gap":
            self.stats.gaps += 1
            self.book.invalidate()
            self.state = "buffering"
            self.pending = None
            self.recent.clear()
            self.buffer.clear()
            self.buffer.append(ev)
            return ["gap", "need_snapshot"]
        if res == "ignore":
            self.stats.ignored += 1
            return []
        self.stats.applied += 1
        self.recent.append(ev)
        if self.pending is not None:
            return self._advance_pending(ev)
        return []

    def on_snapshot(self, snap: dict) -> List[str]:
        if self.state != "buffering":
            return []
        L = int(snap["lastUpdateId"])
        if not self.buffer:
            return ["need_snapshot"]
        if L < self.buffer[0]["U"]:
            self.stats.snapshot_retries += 1
            return ["need_snapshot"]
        while self.buffer and self.buffer[0]["u"] <= L:
            self.buffer.popleft()
        self.book.load_snapshot(snap["bids"], snap["asks"], L, self.limit)
        for ev in list(self.buffer):
            res = self._apply_to(self.book, ev)
            if res == "gap":
                self.stats.gaps += 1
                self.book.invalidate()
                last = self.buffer[-1]
                self.buffer.clear()
                self.buffer.append(last)
                return ["gap", "need_snapshot"]
            if res == "ok":
                self.recent.append(ev)
        self.buffer.clear()
        self.state = "live"
        self.stats.resyncs += 1
        return ["synced"]

    # -- coverage refresh -------------------------------------------------
    def request_refresh(self) -> List[str]:
        if self.state != "live" or self.pending is not None:
            return []
        return ["need_refresh"]

    def on_refresh_snapshot(self, snap: dict) -> List[str]:
        """Build a fresh book from a newer snapshot and swap it in once it has
        caught up with the live book, so continuity is not broken."""
        if self.state != "live":
            return []
        L = int(snap["lastUpdateId"])
        nb = LocalBook()
        nb.load_snapshot(snap["bids"], snap["asks"], L, self.limit)
        if L < self.book.update_id:
            # catch up from the ring buffer of already applied events
            evs = [e for e in self.recent if e["u"] > L]
            if not evs or evs[0]["U"] > L + 1:
                return []  # ring buffer does not reach back: give up this refresh
            for e in evs:
                if self._apply_to(nb, e) == "gap":
                    return []
        self.pending = nb
        if nb.update_id == self.book.update_id:
            return self._swap()
        return []

    def _advance_pending(self, ev: dict) -> List[str]:
        nb = self.pending
        assert nb is not None
        if ev["u"] > nb.update_id:
            if self._apply_to(nb, ev) == "gap":
                self.pending = None
                return []
        if nb.update_id == self.book.update_id:
            return self._swap()
        return []

    def _swap(self) -> List[str]:
        nb = self.pending
        self.pending = None
        if nb is None:
            return []
        self.book.copy_from(nb)
        self.stats.refreshes += 1
        return ["refreshed"]


class BinanceFuturesSync:
    """Binance USD-M Futures procedure: buffer; snapshot L; drop events with
    u < L; the first processed event must have U <= L <= u; afterwards every
    event's pu must equal the previous event's u, otherwise resync from the
    snapshot step."""

    name = "binance-futures"

    def __init__(self, book: LocalBook, snapshot_limit: int = 1000, buffer_max: int = 20000) -> None:
        self.book = book
        self.limit = snapshot_limit
        self.state = "init"
        self.buffer: Deque[dict] = deque(maxlen=buffer_max)
        self.last_u: Optional[int] = None
        self.pending_snap: Optional[dict] = None
        self.stats = SyncStats()

    def reset(self) -> None:
        self.book.invalidate()
        self.state = "init"
        self.buffer.clear()
        self.last_u = None
        self.pending_snap = None

    def request_refresh(self) -> List[str]:
        return []  # futures snapshot limit is 1000; refresh = full resync, not done implicitly

    def on_message(self, ev: dict) -> List[str]:
        self.stats.events += 1
        if ev.get("E"):
            self.book.last_exch_ts = ev["E"]
        if self.state == "init":
            self.buffer.clear()
            self.buffer.append(ev)
            self.state = "buffering"
            return ["need_snapshot"]
        if self.state == "buffering":
            if len(self.buffer) == self.buffer.maxlen:
                self.stats.buffer_overflow += 1
            self.buffer.append(ev)
            if self.pending_snap is not None and ev["u"] >= int(self.pending_snap["lastUpdateId"]):
                snap, self.pending_snap = self.pending_snap, None
                return self.on_snapshot(snap)
            return []
        if ev.get("pu") != self.last_u:
            self.stats.gaps += 1
            self.book.invalidate()
            self.state = "buffering"
            self.buffer.clear()
            self.buffer.append(ev)
            return ["gap", "need_snapshot"]
        self.book.apply(ev["b"], ev["a"])
        self.book.update_id = ev["u"]
        self.last_u = ev["u"]
        self.stats.applied += 1
        return []

    def on_snapshot(self, snap: dict) -> List[str]:
        if self.state != "buffering":
            return []
        L = int(snap["lastUpdateId"])
        while self.buffer and self.buffer[0]["u"] < L:
            self.buffer.popleft()
        if not self.buffer:
            # snapshot is ahead of everything received: keep it until the stream catches up
            self.pending_snap = snap
            return []
        first = self.buffer[0]
        if not (first["U"] <= L <= first["u"]):
            self.stats.snapshot_retries += 1
            return ["need_snapshot"]
        self.book.load_snapshot(snap["bids"], snap["asks"], L, self.limit)
        prev_u: Optional[int] = None
        for ev in self.buffer:
            if prev_u is not None and ev.get("pu") != prev_u:
                self.stats.gaps += 1
                self.book.invalidate()
                last = self.buffer[-1]
                self.buffer.clear()
                self.buffer.append(last)
                return ["gap", "need_snapshot"]
            self.book.apply(ev["b"], ev["a"])
            self.book.update_id = ev["u"]
            prev_u = ev["u"]
        self.last_u = prev_u
        self.buffer.clear()
        self.state = "live"
        self.stats.resyncs += 1
        return ["synced"]


class BybitSync:
    """Bybit v5 ``orderbook.{depth}.{symbol}``: a ``snapshot`` message resets the
    book (also re-sent by Bybit after problems, and ``u == 1`` means a service
    restart snapshot); ``delta`` messages carry absolute sizes, 0 deletes.

    The documentation does not promise that consecutive deltas have u+1, so a
    jump is only counted (``seq_jumps``), not treated as a gap. Freshness is
    enforced by the adapter's watchdog (no message -> reconnect -> snapshot).
    """

    name = "bybit"

    def __init__(self, book: LocalBook, depth: int) -> None:
        self.book = book
        self.book.top_n = depth
        self.depth = depth
        self.state = "init"
        self.stats = SyncStats()

    def reset(self) -> None:
        self.book.invalidate()
        self.state = "init"

    def request_refresh(self) -> List[str]:
        return []

    def on_message(self, msg: dict) -> List[str]:
        self.stats.events += 1
        data = msg.get("data") or {}
        if msg.get("cts") or msg.get("ts"):
            self.book.last_exch_ts = msg.get("cts") or msg.get("ts")
        u = int(data.get("u", 0))
        if msg.get("type") == "snapshot" or u == 1:
            self.book.load_snapshot(data.get("b", []), data.get("a", []), u, None)
            first = self.state != "live"
            self.state = "live"
            self.stats.resyncs += 1
            return ["synced"] if first else ["synced"]
        if self.state != "live":
            self.stats.ignored += 1
            return []
        if u <= self.book.update_id:
            self.stats.ignored += 1
            return []
        if u > self.book.update_id + 1:
            self.stats.seq_jumps += 1
        self.book.apply(data.get("b", []), data.get("a", []))
        self.book.update_id = u
        self.stats.applied += 1
        return []

    def on_snapshot(self, snap: Any) -> List[str]:
        return []
