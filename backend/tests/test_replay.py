"""Direct processing and replay of the same recorded events must give identical
aggregates; recomputing a past point must not use future events."""
import glob

from flowlens import numeric
from flowlens.adapters.binance import parse_agg_trade
from flowlens.book import LocalBook
from flowlens.models import Instrument
from flowlens.pipeline import Pipeline
from flowlens.recorder import Recorder, read_events, replay
from flowlens.simulator import MarketSim
from flowlens.sync import BinanceSpotSync

FP = numeric.parse("5")
HM = numeric.parse("10")


def run_live(tmp_path, seconds=300, drop_at=None):
    sim = MarketSim(levels=600, seed=3, spacing=77000 * 0.05 / 600)
    rec = Recorder(str(tmp_path), "binance:spot:TEST", 24)
    book = LocalBook()
    sync = BinanceSpotSync(book, 5000)
    pipe = Pipeline(Instrument("binance", "spot", "TEST"), book, fp_step=FP, heat_step=HM)
    t0 = 1_700_000_000_000
    started = False
    step = 0
    for ms in range(0, seconds * 1000, 100):
        now = t0 + ms
        ev, trades = sim.step_ms(now, 100)
        step += 1
        if ev is not None:
            if drop_at is not None and step == drop_at:
                ev = None  # simulate a lost message -> gap on the next one
            else:
                rec.write("depth", now, ev)
                acts = sync.on_message(ev)
                if "need_snapshot" in acts:
                    snap = sim.snapshot(5000)
                    rec.write("snap", now, snap)
                    sync.on_snapshot(snap)
        for d in trades:
            tr = parse_agg_trade(d, now)
            if not started:
                started = True
                rec.write("trade_start", now, tr.t_exch)
                pipe.trade_stream_started(tr.t_exch)
            rec.write("trade", now, d)
            pipe.on_trade(tr)
        if ms % 1000 == 0:
            rec.write("tick", now, None)
            pipe.on_tick(now)
    rec.close()
    pipe.flush_depth()
    return pipe, sync


def test_replay_reproduces_live_aggregates(tmp_path):
    pipe, sync = run_live(tmp_path)
    live_depth = [r for r, _ in pipe.closed_depth]
    live_heat = [h for _, h in pipe.closed_depth]
    live_candles = [pipe.candles[m].row() for m in sorted(pipe.candles)]
    files = sorted(glob.glob(str(tmp_path / "*" / "*.jsonl.gz")))
    res = replay(read_events(files), "binance", "spot", fp_step=FP, heat_step=HM)
    assert res["depth"] == live_depth
    assert res["candles"] == live_candles
    assert [h["bid_mean"] for h in res["heat"]] != []
    assert len(live_depth) >= 5
    # bands are populated and coverage complete for a 600-level synthetic book
    full = [r for r in live_depth if r["samples"] == 60]
    assert full and all(r["bands"]["0.01"][1] is not None for r in full)


def test_gap_is_detected_and_marked(tmp_path):
    pipe, sync = run_live(tmp_path, seconds=120, drop_at=400)
    assert sync.stats.gaps == 1 and sync.stats.resyncs == 2
    marked = [r for r, _ in pipe.closed_depth if r["epoch_start"]]
    assert len(marked) == 2  # initial sync and the resync after the gap


def test_past_points_do_not_depend_on_future_events(tmp_path):
    a, _ = run_live(tmp_path / "a", seconds=180)
    b, _ = run_live(tmp_path / "b", seconds=300)
    da = [r for r, _ in a.closed_depth]
    db = [r for r, _ in b.closed_depth][: len(da) - 1]
    assert da[: len(db)] == db   # the longer run extends, never rewrites, closed minutes
