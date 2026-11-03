"""Checks from section 10 of the specification plus edge cases of each formula."""
import math

import pytest

from flowlens import indicators as ind
from flowlens import numeric
from flowlens.book import LocalBook


def test_spec_orderbook_example():
    # B=150, A=100 -> Delta=50, Imbalance=20 %, BidShare=60 %
    assert ind.ob_delta(150, 100) == 50
    assert ind.imbalance(150, 100) == pytest.approx(20.0)
    assert ind.bid_share(150, 100) == pytest.approx(60.0)


def test_spec_trade_delta_example():
    vbuy, vsell = 12, 8
    assert vbuy - vsell == 4 and vbuy + vsell == 20


def test_spec_dynamics_example():
    # Imbalance from -10 % to 20 % -> Dynamics = 30 percentage points
    assert ind.dynamics([-10.0, 20.0], 1) == [None, 30.0]


def test_zero_denominator_and_missing_values_are_null():
    assert ind.imbalance(0, 0) is None
    assert ind.bid_share(0, 0) is None
    assert ind.imbalance(None, 10) is None
    assert ind.ob_delta(5, None) is None
    assert ind.dynamics([1.0, None, 3.0], 1) == [None, None, None]


def test_ema_restarts_after_gap():
    out = ind.ema([1.0, 2.0, None, 4.0], 3)
    assert out[2] is None and out[3] == 4.0


def test_zscore_uses_only_past_values():
    x = [float(i % 5) for i in range(100)]
    z1, _ = ind.zscore(x, 40, min_n=30, min_cov=0.9)
    y = list(x)
    y[60] = 1e9  # a huge future value must not change z at t <= 60 except at 60 itself
    z2, _ = ind.zscore(y, 40, min_n=30, min_cov=0.9)
    assert z1[:60] == z2[:60]
    assert z2[60] > 1e6


def test_zscore_population_std_and_status():
    x = [1.0, 3.0] * 20 + [5.0]
    z, st = ind.zscore(x, 40, min_n=30, min_cov=0.9)
    # base mean 2, population std 1 -> z = 3
    assert st[-1] == "ok" and z[-1] == pytest.approx(3.0)
    assert st[10] == "warmup" and z[10] is None
    z0, st0 = ind.zscore([2.0] * 50, 40)
    assert st0[-1] == "zero_var" and z0[-1] is None


def test_zscore_requires_coverage():
    x = [1.0, 3.0] * 20 + [5.0]
    for i in range(0, 6):
        x[i] = None  # 6 of 40 missing -> 34 valid < 36 (90 %)
    z, st = ind.zscore(x, 40, min_n=30, min_cov=0.9)
    assert st[-1] == "warmup" and z[-1] is None


def test_cvd_resets_daily_and_marks_partial():
    day = 86400_000
    t = [0, 60_000, day, day + 60_000]
    out, part = ind.cvd([1.0, None, 2.0, 3.0], t, [False] * 4, "utc_day")
    assert out == [1.0, None, 2.0, 5.0]
    assert part == [False, True, False, False]
    out, _ = ind.cvd([1.0, 1.0, 2.0, 3.0], t, [False] * 4, "history")
    assert out[-1] == 7.0


def test_rsi_wilder_known_values():
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    r = ind.rsi(closes, 14)
    assert r[13] is None and r[14] == pytest.approx(70.46, abs=0.05)


def test_macd_needs_warmup():
    m, s, h = ind.macd([float(i) for i in range(40)], 12, 26, 9)
    assert m[24] is None and m[25] is not None
    assert h[-1] is not None


def test_anomaly_ratio_and_peaks():
    x = [10.0] * 100 + [40.0, 35.0, 10.0]
    ratio, z, st = ind.anomaly(x, 100, 3.0)
    assert ratio[100] == pytest.approx(4.0) and st[100] == "ok"
    peaks = ind.anomaly_peaks(list(range(len(x))), x, ratio, 3.0, "buy")
    assert len(peaks) == 1 and peaks[0]["t"] == 100 and peaks[0]["candles"] == 2
    _, _, st2 = ind.anomaly([10.0] * 50, 100)
    assert st2[-1] == "warmup"


def test_value_area_deterministic():
    va = ind.value_area([(0, 1), (1, 2), (2, 10), (3, 2), (4, 1)], 0.7)
    assert va["poc"] == 2 and (va["val"], va["vah"]) == (1, 3)
    # tie at POC -> lower price wins
    assert ind.value_area([(0, 5), (1, 5)], 0.5)["poc"] == 0


def test_numeric_parse_is_exact():
    assert numeric.parse("77616.01000000") == 7761601000000
    assert numeric.parse("0.00000001") == 1
    assert numeric.parse("5") == 500000000
    assert numeric.fmt(numeric.parse("0.10")) == "0.1"
    with pytest.raises(ValueError):
        numeric.parse("0.000000001")
    # summing many quantities stays exact (floats would not)
    assert sum(numeric.parse("0.1") for _ in range(10)) == numeric.parse("1")


def _book(bids, asks, limit=None):
    b = LocalBook()
    b.load_snapshot([[str(p), str(q)] for p, q in bids], [[str(p), str(q)] for p, q in asks], 1, limit)
    return b


def test_book_band_sums_and_coverage():
    # mid = 100; levels at 99.9 (0.1 %), 99.6 (0.4 %), 99.2 (0.8 %)
    b = _book([(99.9, 1), (99.6, 2), (99.2, 3)], [(100.1, 1), (100.4, 1), (100.8, 1)])
    s = b.sample((0.0025, 0.005, 0.01), 3.0, numeric.parse("1"))
    q = [x[0] / numeric.SCALE for x in s.bids]
    assert q == [1, 3, 6]
    n = [x[1] / numeric.NOTIONAL_SCALE for x in s.bids]
    assert n[0] == pytest.approx(99.9) and n[2] == pytest.approx(99.9 + 2 * 99.6 + 3 * 99.2)
    assert all(x[2] == 1.0 for x in s.bids)  # snapshot smaller than limit: whole side known


def test_book_coverage_limited_by_snapshot_depth():
    # snapshot returned exactly `limit` levels -> deeper levels unknown
    b = _book([(99.9, 1), (99.7, 1)], [(100.1, 1), (100.3, 1)], limit=2)
    s = b.sample((0.0025, 0.005, 0.01), 3.0, numeric.parse("1"))
    assert s.bids[0][2] == 1.0          # 0.25 % reaches 99.75 >= floor 99.7
    assert s.bids[2][2] == pytest.approx(0.3)  # 1 % band only 0.3 % covered


def test_crossed_book_is_not_sampled():
    b = _book([(100.2, 1)], [(100.1, 1)])
    assert b.sample((0.01,), 3.0, numeric.parse("1")) is None
