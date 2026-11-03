"""Command line entry point.

  python -m flowlens serve [--demo] [--config FILE] [--port 8000]
  python -m flowlens mock-exchange --port 9100 --symbols BTCUSDT,ETHUSDT
  python -m flowlens replay FILE.jsonl.gz [...] --exchange binance --market spot --fp-step 5 --heat-step 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

from aiohttp import web

from . import config as config_mod


def default_static() -> str | None:
    here = Path(__file__).resolve().parent
    for cand in (here / "static", here.parent.parent / "frontend" / "dist"):
        if (cand / "index.html").exists():
            return str(cand)
    return None


async def serve(cfg) -> None:
    from .api import Api
    from .collector import Collector
    from .storage import Storage

    storage = Storage(cfg.db_path)
    collector = Collector(cfg, storage)
    await collector.start()
    api = Api(collector, cfg.static_dir or default_static())
    app = api.app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, cfg.host, cfg.port)
    await site.start()
    mode = "DEMO (синтетические данные)" if cfg.demo else "LIVE (публичные данные бирж)"
    print(f"FlowLens {mode}: http://{cfg.host}:{cfg.port}  (Ctrl+C — выход)", flush=True)

    async def screener_tick() -> None:
        while True:
            await asyncio.sleep(1.0)
            api.screener.on_tick()

    bg = [asyncio.ensure_future(screener_tick()), asyncio.ensure_future(api.alerts.run())]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows
            pass
    try:
        await stop.wait()
    finally:
        for t in bg:
            t.cancel()
        await collector.stop()
        await runner.cleanup()
        storage.close()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="flowlens")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run collector + API + UI")
    s.add_argument("--demo", action="store_true", help="synthetic DEMO data instead of exchanges")
    s.add_argument("--config")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--db")
    m = sub.add_parser("mock-exchange", help="Binance-like mock exchange for tests and load tests")
    m.add_argument("--port", type=int, default=9100)
    m.add_argument("--symbols", default="BTCUSDT")
    r = sub.add_parser("replay", help="replay recorded raw events and print aggregates")
    r.add_argument("files", nargs="+")
    r.add_argument("--exchange", default="binance")
    r.add_argument("--market", default="spot")
    r.add_argument("--fp-step", type=float, required=True, help="footprint step in price units")
    r.add_argument("--heat-step", type=float, required=True, help="heatmap step in price units")
    args = ap.parse_args(argv)
    logging.basicConfig(level=os.environ.get("FLOWLENS_LOG", "WARNING"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd in (None, "serve"):
        cfg = config_mod.load(getattr(args, "config", None), demo=True if getattr(args, "demo", False) else None,
                              host=getattr(args, "host", None), port=getattr(args, "port", None),
                              db_path=getattr(args, "db", None))
        try:
            asyncio.run(serve(cfg))
        except KeyboardInterrupt:
            pass
    elif args.cmd == "mock-exchange":
        from .mockexchange import run_mock
        asyncio.run(run_mock(args.port, args.symbols.split(",")))
    elif args.cmd == "replay":
        from . import numeric
        from .recorder import read_events, replay
        res = replay(read_events(args.files), args.exchange, args.market,
                     fp_step=numeric.parse(f"{args.fp_step:.8f}"), heat_step=numeric.parse(f"{args.heat_step:.8f}"))
        print(json.dumps({"depth_minutes": len(res["depth"]), "candles": len(res["candles"]), "sync": res["stats"],
                          "depth": res["depth"][-3:]}, indent=1, default=str))


if __name__ == "__main__":
    main()
