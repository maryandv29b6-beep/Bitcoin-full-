"""End-to-end: real collector + Binance adapter against the mock exchange with
fault injection (book gap, lost trades, duplicates, disconnect), then the API."""
import asyncio
import json
import socket
import time

import aiohttp
from aiohttp import web

from flowlens.api import Api
from flowlens.collector import Collector
from flowlens.config import Config
from flowlens.mockexchange import MockExchange
from flowlens.storage import Storage


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def wait_for(cond, timeout=20.0, step=0.1):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        await asyncio.sleep(step)
    return False


async def scenario(tmp_path):
    mport = free_port()
    mx = MockExchange(["BTCUSDT"], levels=800)
    mrunner = web.AppRunner(mx.app())
    await mrunner.setup()
    await web.TCPSite(mrunner, "127.0.0.1", mport).start()
    cfg = Config(db_path=str(tmp_path / "t.db"), instruments=[{"exchange": "binance", "market": "spot", "symbol": "BTCUSDT"},
                                                               {"exchange": "binance", "market": "perp", "symbol": "BTCUSDT"}],
                 binance_spot_rest=f"http://127.0.0.1:{mport}", binance_spot_ws=f"ws://127.0.0.1:{mport}",
                 binance_perp_rest=f"http://127.0.0.1:{mport}", binance_perp_ws_public=f"ws://127.0.0.1:{mport}/public",
                 binance_perp_ws_market=f"ws://127.0.0.1:{mport}/market",
                 kline_backfill_days=0.05, stale_after_s=5, reconnect_after_s=10, flush_ms=500)
    storage = Storage(cfg.db_path)
    col = Collector(cfg, storage)
    await col.start()
    api = Api(col, None)
    arunner = web.AppRunner(api.app())
    await arunner.setup()
    aport = free_port()
    await web.TCPSite(arunner, "127.0.0.1", aport).start()
    res = {}
    try:
        rt = col.runtimes["binance:spot:BTCUSDT"]
        perp = col.runtimes["binance:perp:BTCUSDT"]
        assert await wait_for(lambda: rt.status() == "LIVE" and perp.status() == "LIVE"), (rt.live_status(), perp.live_status())
        res["epoch0"] = rt.book.epoch
        async with aiohttp.ClientSession() as s:
            async def fault(**kw):
                async with s.post(f"http://127.0.0.1:{mport}/admin/fault", json=kw) as r:
                    assert r.status == 200

            # 1) book gap -> resync with a new epoch
            await fault(kind="drop_depth", n=2, symbol="BTCUSDT")
            assert await wait_for(lambda: rt.sync.stats.gaps >= 1 and rt.status() == "LIVE")
            res["epoch_after_gap"] = rt.book.epoch
            # 2) lost trades on the stream -> id gap -> REST backfill closes the hole
            await asyncio.sleep(1.0)
            await fault(kind="drop_trades", n=5, symbol="BTCUSDT")
            assert await wait_for(lambda: any(k == "trade_gap_filled" for _, k, _ in rt.events))
            res["holes"] = rt.pipeline.seq_open_holes()
            # 3) duplicates are dropped
            await fault(kind="dup_trades", n=3, symbol="BTCUSDT")
            assert await wait_for(lambda: rt.pipeline.seq.duplicates >= 3)
            # 4) disconnect -> reconnect -> resync
            await fault(kind="disconnect")
            assert await wait_for(lambda: rt.book.epoch >= res["epoch_after_gap"] + 1 and rt.status() == "LIVE", timeout=30)
            res["epoch_after_disconnect"] = rt.book.epoch
            await asyncio.sleep(2.5)
            base = f"http://127.0.0.1:{aport}"
            now = int(time.time() * 1000)
            q = {"inst": "binance:spot:BTCUSDT", "tf": "1m", "from": now - 3 * 3600_000,
                 "params": json.dumps({"book_min_time_cov": 0.0, "z_base": "1h"})}
            async with s.get(f"{base}/api/chart", params=q) as r:
                res["chart_status"] = r.status
                res["chart"] = await r.json()
            async with s.get(f"{base}/api/heatmap", params={"inst": "binance:spot:BTCUSDT", "tf": "1m", "from": now - 600_000}) as r:
                res["heat"] = await r.json()
            async with s.get(f"{base}/api/footprint", params={"inst": "binance:spot:BTCUSDT", "tf": "1m", "from": now - 600_000, "step": "20"}) as r:
                res["fp"] = await r.json()
            async with s.get(f"{base}/api/screener") as r:
                res["screener"] = await r.json()
            async with s.get(f"{base}/api/health") as r:
                res["health"] = await r.json()
            async with s.ws_connect(f"{base}/api/stream") as ws:
                await ws.send_json({"op": "sub", "inst": "binance:spot:BTCUSDT", "tf": "5m"})
                got = []
                t0 = time.monotonic()
                while time.monotonic() - t0 < 5 and not any(m.get("type") == "live" for m in got):
                    m = await ws.receive(timeout=5)
                    got.append(json.loads(m.data))
                res["ws"] = got
                # switching the instrument replaces the subscription
                await ws.send_json({"op": "sub", "inst": "binance:perp:BTCUSDT", "tf": "1m"})
                seen = set()
                t0 = time.monotonic()
                while time.monotonic() - t0 < 5:
                    m = json.loads((await ws.receive(timeout=5)).data)
                    if m.get("type") == "live":
                        seen.add(m["inst"])
                        if "binance:perp:BTCUSDT" in seen and len(seen) == 1:
                            break
                        seen = {m["inst"]} if m["inst"] == "binance:perp:BTCUSDT" else seen
                res["switched"] = seen
        res["quality"] = [k for _, k, _ in rt.events]
        res["perp_deriv"] = perp.deriv_state
    finally:
        await col.stop()
        await arunner.cleanup()
        await mrunner.cleanup()
        storage.close()
    return res


def test_end_to_end_with_faults(tmp_path):
    res = asyncio.run(scenario(tmp_path))
    assert res["epoch_after_gap"] == res["epoch0"] + 1
    assert res["epoch_after_disconnect"] >= res["epoch_after_gap"] + 1
    assert res["holes"] == []
    assert "book_gap" in res["quality"] and "trade_gap_filled" in res["quality"] and "disconnect" in res["quality"]
    c = res["chart"]
    assert res["chart_status"] == 200, c
    n = len(c["t"])

    def aligned(o, path):
        # every per-candle series must be aligned with the time grid (incl. nested belts)
        if isinstance(o, dict):
            for k, v in o.items():
                if k not in ("peaks", "range", "source"):
                    aligned(v, path + "." + k)
        elif isinstance(o, list) and o and not isinstance(o[0], dict):
            assert len(o) == n, f"{path}: {len(o)} != {n}"
        elif isinstance(o, list):
            for j, v in enumerate(o):
                aligned(v, f"{path}[{j}]")
    aligned(c["indicators"], "indicators")
    aligned(c["candles"], "candles")
    closes = [x for x in c["candles"]["c"] if x is not None]
    assert len(closes) > 30            # klines backfill + live candles
    assert any(s == ["kline"] for s in c["candles"]["source"])
    dom = c["indicators"]["dominance"]["lines"]["0.01"]
    assert any(v is not None for v in dom[-3:])
    assert all(v is None or -100 <= v <= 100 for v in dom)
    assert c["status"] == "LIVE" and c["source"]["sync_rules"] == "binance-spot"
    assert res["heat"]["rows"] > 10 and res["heat"]["units"] == "BTC"
    assert res["fp"]["step"] == "20" and res["fp"]["candles"]
    rows = {r["key"]: r for r in res["screener"]["rows"]}
    assert rows["binance:spot:BTCUSDT"]["spread_bps"] is not None
    assert rows["binance:perp:BTCUSDT"]["funding"] == 0.0001
    assert any(m.get("type") == "live" and m.get("candle") for m in res["ws"])
    assert res["switched"] == {"binance:perp:BTCUSDT"}
    assert res["perp_deriv"].open_interest == 81234.5
