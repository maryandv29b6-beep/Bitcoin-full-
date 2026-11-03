"""Bybit adapter end-to-end against the mock (top-N book, publicTrade, tickers)
and alert rules (confirmation, cooldown, invalid data)."""
import asyncio
import time

from aiohttp import web

from flowlens.adapters.bybit import kline_row, parse_public_trade
from flowlens.alerts import AlertEngine, validate_rule
from flowlens.collector import Collector
from flowlens.config import Config
from flowlens.mockexchange import MockExchange
from flowlens.models import BUY, SELL
from flowlens.series import aggregate_candles
from flowlens.storage import Storage, q_candles

from test_integration import free_port, wait_for


def test_bybit_parsers_follow_docs():
    tr = parse_public_trade({"T": 1672304486865, "s": "BTCUSDT", "S": "Buy", "v": "0.001", "p": "16578.50",
                             "L": "PlusTick", "i": "20f43950-d8dd-5b31-9112-a178eb6023af", "BT": False}, 1)
    assert tr.side == BUY and tr.granularity == "raw" and tr.seq is None
    assert parse_public_trade({"T": 1, "S": "Sell", "v": "1", "p": "1"}, 1).side == SELL
    row = kline_row(["1670608800000", "17071", "17073", "17027", "17055.5", "268611", "15.74"])
    assert row["v_buy"] is None and row["q_buy"] is None and row["v_unknown"] > 0   # no taker split in Bybit klines


async def bybit_scenario(tmp_path):
    mport = free_port()
    mx = MockExchange(["BTCUSDT"], levels=800)
    mr = web.AppRunner(mx.app())
    await mr.setup()
    await web.TCPSite(mr, "127.0.0.1", mport).start()
    cfg = Config(db_path=str(tmp_path / "b.db"),
                 instruments=[{"exchange": "bybit", "market": "spot", "symbol": "BTCUSDT", "depth": 200},
                              {"exchange": "bybit", "market": "perp", "symbol": "BTCUSDT", "depth": 500}],
                 bybit_rest=f"http://127.0.0.1:{mport}", bybit_ws=f"ws://127.0.0.1:{mport}",
                 kline_backfill_days=0.02, flush_ms=500)
    storage = Storage(cfg.db_path)
    col = Collector(cfg, storage)
    await col.start()
    out = {}
    try:
        spot = col.runtimes["bybit:spot:BTCUSDT"]
        perp = col.runtimes["bybit:perp:BTCUSDT"]
        assert await wait_for(lambda: spot.status() == "LIVE" and perp.status() == "LIVE"
                              and spot.pipeline.trades_total > 20 and perp.deriv_state.open_interest is not None, 25)
        await asyncio.sleep(2.5)
        out["levels"] = (len(spot.book.bids), len(spot.book.asks))
        out["range"] = spot.book.known_range()
        out["sample"] = spot.pipeline.last_sample
        out["oi"] = perp.deriv_state.open_interest
        out["funding"] = perp.deriv_state.funding_rate
        out["sync"] = spot.sync.stats.as_dict()
        now = int(time.time() * 1000)
        rows = await storage.run(q_candles, spot.iid, now - 3600_000, now + 60_000)
        out["rows"] = rows
    finally:
        await col.stop()
        await mr.cleanup()
        storage.close()
    return out


def test_bybit_end_to_end(tmp_path):
    out = asyncio.run(bybit_scenario(tmp_path))
    assert out["levels"] == (200, 200)                # top-N book held exactly
    lo, hi = out["range"]
    assert lo > 0 and hi is not None                  # coverage limited to the held levels
    s = out["sample"]
    assert s.bids[0][2] == 1.0                        # 0.25 % covered by 200 levels of the synthetic book
    assert s.bids[3][2] < 1.0 and s.asks[3][2] < 1.0  # 3 % is beyond 200 levels -> reported as not covered
    assert out["oi"] == 51234.5 and out["funding"] == 0.0001
    assert out["sync"]["applied"] > 5 and out["sync"]["gaps"] == 0
    kl = [r for r in out["rows"] if r["source"] == "kline"]
    tr = [r for r in out["rows"] if r["source"] == "trades"]
    assert kl and tr
    agg = aggregate_candles(kl, [r["t"] for r in kl], 60_000)
    assert all(v is None for v in agg["q_buy"])       # never 0 when the split is unknown
    assert any(r["n_buy"] for r in tr)


class FakeStorage:
    def __init__(self):
        self.rules = []
        self.log = []

    def alert_rules(self):
        return self.rules

    def log_alert(self, rid, t, msg, data, delivery):
        self.log.append((rid, t, msg, data))

    def update_alert_rule(self, rid, enabled=None, last_fired=None):
        for r in self.rules:
            if r["id"] == rid and last_fired is not None:
                r["last_fired"] = last_fired


class NoTelegram:
    def enabled(self):
        return False


def test_alert_confirmation_cooldown_and_invalid_data():
    st = FakeStorage()
    rule = validate_rule({"metric": "imbalance_0.01", "op": ">", "value": 40, "confirm_s": 5, "cooldown_s": 60})
    st.rules.append({"id": 1, "enabled": True, "instrument": "x", "rule": rule, "last_fired": None})
    eng = AlertEngine(st, None, NoTelegram())

    def row(v, status="LIVE"):
        return [{"key": "x", "status": status, "price": 1.0, "bands": {"0.01": {"imbalance": v}}}]

    t0 = 1_000_000
    run = lambda v, t, status="LIVE": asyncio.run(eng.evaluate(row(v, status), t))
    assert run(50, t0) == []                       # condition starts holding
    assert run(50, t0 + 3000) == []                # not confirmed yet (3 s < 5 s)
    assert len(run(50, t0 + 5000)) == 1            # confirmed -> fires
    assert run(50, t0 + 20000) == []               # cooldown
    assert run(None, t0 + 70000) == []             # missing data never fires and resets confirmation
    assert run(50, t0 + 71000, "STALE") == []      # stale data never fires
    assert run(50, t0 + 72000) == []
    assert len(run(50, t0 + 77000)) == 1
    assert len(st.log) == 2 and st.log[0][3]["value"] == 50
