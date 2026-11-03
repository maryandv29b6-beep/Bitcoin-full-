"""Core domain entities (see docs/ARCHITECTURE.md, section "Сущности")."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import numeric

BUY = 1
SELL = -1
UNKNOWN = 0


@dataclass
class Instrument:
    exchange: str            # binance | bybit | demo
    market: str              # spot | perp
    symbol: str              # BTCUSDT
    base: str = ""
    quote: str = ""
    tick: int = 0            # price tick, scaled 1e8
    step: int = 0            # quantity step, scaled 1e8
    contract: dict = field(default_factory=dict)  # contract spec for derivatives
    id: Optional[int] = None

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.market}:{self.symbol}"

    @property
    def is_demo(self) -> bool:
        return self.exchange == "demo"

    def public(self) -> dict:
        return {
            "key": self.key,
            "exchange": self.exchange,
            "market": self.market,
            "symbol": self.symbol,
            "base": self.base,
            "quote": self.quote,
            "tickSize": numeric.fmt(self.tick),
            "quantityStep": numeric.fmt(self.step),
            "contractSpec": self.contract,
            "demo": self.is_demo,
        }


@dataclass(slots=True)
class Trade:
    trade_id: Any            # exchange id of the record (aggregate id for Binance)
    seq: Optional[int]       # integer sequence for continuity checks, if the source has one
    t_exch: int              # exchange trade time, ms UTC
    t_recv: int              # local receive time, ms UTC
    price: int               # scaled 1e8
    qty: int                 # base quantity, scaled 1e8
    side: int                # BUY / SELL aggressor, UNKNOWN if the source does not say
    count: int = 1           # underlying executions represented by this record
    granularity: str = "raw"  # raw | aggregate


@dataclass(slots=True)
class Liquidation:
    t_exch: int
    t_recv: int
    side: str                # 'long' (long position liquidated) | 'short'
    price: int
    qty: int


@dataclass
class DerivState:
    """Latest derivative metrics for a perpetual (None when unknown)."""
    open_interest: Optional[float] = None       # base units
    open_interest_value: Optional[float] = None  # quote units
    funding_rate: Optional[float] = None         # fraction per funding period
    next_funding_time: Optional[int] = None
    funding_interval_h: Optional[float] = None
    mark_price: Optional[float] = None
    t_oi: Optional[int] = None
    t_funding: Optional[int] = None
