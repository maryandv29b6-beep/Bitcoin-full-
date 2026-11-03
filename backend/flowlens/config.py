"""Configuration: defaults < JSON file (FLOWLENS_CONFIG or ./flowlens.config.json) < env vars.

No secrets live here. Telegram credentials are entered in the UI and stored
in the local database only.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class InstrumentCfg:
    exchange: str
    market: str
    symbol: str
    # optional overrides of storage steps (price units, e.g. "10" USDT)
    heatmap_step: Optional[str] = None
    footprint_step: Optional[str] = None
    depth: Optional[int] = None          # Bybit orderbook depth / Binance snapshot limit


DEFAULT_INSTRUMENTS = [
    {"exchange": "binance", "market": "spot", "symbol": "BTCUSDT"},
    {"exchange": "binance", "market": "spot", "symbol": "ETHUSDT"},
    {"exchange": "binance", "market": "perp", "symbol": "BTCUSDT"},
    {"exchange": "bybit", "market": "spot", "symbol": "BTCUSDT"},
    {"exchange": "bybit", "market": "perp", "symbol": "BTCUSDT"},
]

DEMO_INSTRUMENTS = [
    {"exchange": "demo", "market": "spot", "symbol": "BTCUSDT"},
    {"exchange": "demo", "market": "perp", "symbol": "ETHUSDT"},
]


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8000
    db_path: str = "data/flowlens.db"
    demo: bool = False
    instruments: List[Dict[str, Any]] = field(default_factory=lambda: [dict(x) for x in DEFAULT_INSTRUMENTS])
    sample_ms: int = 1000                 # book sampling period
    heatmap_range_pct: float = 3.0        # heatmap rows recorded within ±% of mid
    kline_backfill_days: float = 3.0      # candle history from exchange klines at startup
    retention_days: float = 14.0
    flush_ms: int = 2000
    live_push_ms: int = 250               # UI update period (4 Hz)
    stale_after_s: float = 10.0           # no messages -> STALE
    reconnect_after_s: float = 30.0       # no messages -> reconnect
    coverage_refresh_min_s: float = 300.0
    demo_history_hours: float = 6.0       # DEMO only: synthetic history (always labelled DEMO)
    record_raw: bool = False              # record raw exchange messages for replay
    raw_dir: str = "data/raw"
    raw_retention_hours: float = 6.0
    # REST/WS endpoints (overridable to point at the mock exchange in tests)
    binance_spot_rest: str = "https://api.binance.com"
    binance_spot_ws: str = "wss://stream.binance.com:9443"
    binance_perp_rest: str = "https://fapi.binance.com"
    # USD-M futures WebSocket is split into routes since the 2026 upgrade notice:
    # depth on /public, aggTrade / markPrice / forceOrder on /market.
    binance_perp_ws_public: str = "wss://fstream.binance.com/public"
    binance_perp_ws_market: str = "wss://fstream.binance.com/market"
    bybit_rest: str = "https://api.bybit.com"
    bybit_ws: str = "wss://stream.bybit.com"
    binance_weight_limit: int = 6000      # REQUEST_WEIGHT per minute (spot); we use at most 70%
    static_dir: Optional[str] = None

    def instrument_cfgs(self) -> List[InstrumentCfg]:
        items = DEMO_INSTRUMENTS if self.demo else self.instruments
        return [InstrumentCfg(**x) for x in items]

    def public(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("static_dir", None)
        return d


def _coerce(cur: Any, raw: str) -> Any:
    if isinstance(cur, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    if isinstance(cur, int):
        return int(raw)
    if isinstance(cur, float):
        return float(raw)
    if isinstance(cur, list):
        return json.loads(raw)
    return raw


def load(path: Optional[str] = None, **overrides: Any) -> Config:
    cfg = Config()
    p = path or os.environ.get("FLOWLENS_CONFIG") or "flowlens.config.json"
    if Path(p).exists():
        data = json.loads(Path(p).read_text("utf-8"))
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
    for k in list(vars(cfg)):
        env = os.environ.get("FLOWLENS_" + k.upper())
        if env is not None:
            setattr(cfg, k, _coerce(getattr(cfg, k), env))
    for k, v in overrides.items():
        if v is not None and hasattr(cfg, k):
            setattr(cfg, k, v)
    return cfg
