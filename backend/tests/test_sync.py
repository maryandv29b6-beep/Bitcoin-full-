"""Order book synchronisation state machines, per documented exchange rules."""
from flowlens import numeric
from flowlens.book import LocalBook
from flowlens.pipeline import TradeSeq
from flowlens.sync import BinanceFuturesSync, BinanceSpotSync, BybitSync


def ev(U, u, b=(), a=(), pu=None):
    d = {"e": "depthUpdate", "E": 1, "U": U, "u": u, "b": [list(x) for x in b], "a": [list(x) for x in a]}
    if pu is not None:
        d["pu"] = pu
    return d


SNAP = {"lastUpdateId": 105, "bids": [["100", "1"], ["99", "2"]], "asks": [["101", "1"], ["102", "2"]]}


def test_spot_normal_sync_and_updates():
    book = LocalBook()
    s = BinanceSpotSync(book, 5000)
    assert s.on_message(ev(100, 103)) == ["need_snapshot"]
    s.on_message(ev(104, 107, b=[("100", "5")]))
    assert s.on_snapshot(SNAP) == ["synced"]
    assert book.valid and book.update_id == 107
    assert book.bids[numeric.parse("100")] == numeric.parse("5")   # event 104..107 applied over snapshot 105
    s.on_message(ev(108, 108, a=[("101", "0")]))                     # zero removes
    assert numeric.parse("101") not in book.asks
    s.on_message(ev(105, 106))                                       # u < local id -> ignored
    assert s.stats.ignored == 1


def test_spot_snapshot_older_than_buffer_is_refetched():
    s = BinanceSpotSync(LocalBook(), 5000)
    s.on_message(ev(200, 210))
    assert s.on_snapshot(SNAP) == ["need_snapshot"]   # lastUpdateId 105 < U 200
    assert s.stats.snapshot_retries == 1


def test_spot_gap_invalidates_and_resyncs():
    book = LocalBook()
    s = BinanceSpotSync(book, 5000)
    s.on_message(ev(100, 106))
    s.on_snapshot(SNAP)
    acts = s.on_message(ev(110, 112))        # expected U <= 107
    assert acts == ["gap", "need_snapshot"]
    assert not book.valid and s.stats.gaps == 1
    snap2 = {"lastUpdateId": 111, "bids": [["100", "1"]], "asks": [["101", "1"]]}
    assert s.on_snapshot(snap2) == ["synced"]
    assert book.valid and book.epoch == 2 and book.update_id == 112


def test_spot_duplicate_event_after_sync_is_harmless():
    book = LocalBook()
    s = BinanceSpotSync(book, 5000)
    s.on_message(ev(100, 106, b=[("98", "1")]))
    s.on_snapshot(SNAP)
    before = dict(book.bids)
    s.on_message(ev(100, 106, b=[("98", "1")]))   # duplicate (u < id+1 rule handles it)
    assert book.bids == before


def test_spot_coverage_refresh_keeps_continuity():
    book = LocalBook()
    s = BinanceSpotSync(book, 2)
    s.on_message(ev(100, 104))
    s.on_snapshot(SNAP)
    s.on_message(ev(106, 106, b=[("99.5", "1")]))
    s.on_message(ev(107, 108, a=[("101.5", "1")]))
    epoch = book.epoch
    snap = {"lastUpdateId": 106, "bids": [["100", "1"], ["99.5", "1"], ["98", "3"]], "asks": [["101", "1"], ["102", "2"], ["103", "1"]]}
    assert s.on_refresh_snapshot(snap) == ["refreshed"]
    assert book.epoch == epoch and book.update_id == 108
    assert book.asks[numeric.parse("101.5")] == numeric.parse("1")   # replayed from ring buffer
    assert book.bids[numeric.parse("98")] == numeric.parse("3")


def test_futures_pu_rule():
    book = LocalBook()
    s = BinanceFuturesSync(book, 1000)
    s.on_message(ev(100, 104, pu=99))
    s.on_message(ev(105, 108, pu=104, b=[("100", "7")]))
    assert s.on_snapshot(SNAP) == ["synced"]          # first event with U <= 105 <= u is 105..108
    assert book.update_id == 108 and book.bids[numeric.parse("100")] == numeric.parse("7")
    assert s.on_message(ev(109, 110, pu=108)) == []
    acts = s.on_message(ev(112, 113, pu=111))         # pu != previous u -> resync
    assert acts == ["gap", "need_snapshot"] and not book.valid


def test_futures_snapshot_ahead_of_stream_waits():
    book = LocalBook()
    s = BinanceFuturesSync(book, 1000)
    s.on_message(ev(90, 95, pu=89))
    assert s.on_snapshot(SNAP) == []                  # all buffered events older: keep snapshot
    assert s.on_message(ev(96, 104, pu=95)) == []     # still older than 105
    assert s.on_message(ev(105, 106, pu=104)) == ["synced"]
    assert book.valid and book.update_id == 106


def test_bybit_snapshot_delta_and_restart():
    book = LocalBook()
    s = BybitSync(book, 2)
    snap = {"topic": "orderbook.2.BTCUSDT", "type": "snapshot", "ts": 1,
            "data": {"s": "BTCUSDT", "b": [["100", "1"], ["99", "1"]], "a": [["101", "1"], ["102", "1"]], "u": 10, "seq": 5}}
    assert s.on_message(snap) == ["synced"]
    s.on_message({"type": "delta", "ts": 2, "data": {"b": [["99", "0"], ["98.5", "2"]], "a": [], "u": 11}})
    assert numeric.parse("99") not in book.bids and book.update_id == 11
    s.on_message({"type": "delta", "ts": 3, "data": {"b": [["98.5", "9"]], "a": [], "u": 11}})  # stale u -> ignored
    assert book.bids[numeric.parse("98.5")] == numeric.parse("2")
    s.on_message({"type": "delta", "ts": 4, "data": {"b": [], "a": [["101", "3"]], "u": 15}})   # jump counted only
    assert s.stats.seq_jumps == 1 and book.valid
    # top-N coverage: 2 levels held -> deepest bid is the floor
    lo, hi = book.known_range()
    assert lo == numeric.parse("98.5") and hi == numeric.parse("102")
    s.on_message({"type": "delta", "ts": 5, "data": {"b": [["100", "1"]], "a": [], "u": 1}})   # u == 1 -> restart snapshot
    assert book.update_id == 1 and book.epoch == 2


def test_trade_sequence_gaps_and_duplicates():
    t = TradeSeq()
    assert t.check(10) == (True, None)
    assert t.check(11) == (True, None)
    assert t.check(15) == (True, (12, 14))
    assert t.check(13) == (True, None)        # backfill inside the hole
    assert t.check(13) == (False, None)       # duplicate
    assert t.holes == [[12, 12], [14, 14]]
    assert t.check(11) == (False, None) and t.duplicates == 2
