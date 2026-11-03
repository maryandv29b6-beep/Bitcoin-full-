"""Exact fixed-point arithmetic for prices and quantities.

Exchanges send prices and quantities as decimal strings. The local order book
and trade aggregates keep them as integers with a fixed scale of 1e8
(``SCALE``), so applying updates and summing quantities never accumulates
binary floating point error. Floats are produced only at the API/rendering
boundary.
"""
from __future__ import annotations

from decimal import Decimal

DECIMALS = 8
SCALE = 10 ** DECIMALS
# notional of one price unit times one quantity unit (both scaled by 1e8)
NOTIONAL_SCALE = SCALE * SCALE


def parse(s: str) -> int:
    """Parse a non-negative decimal string into an int scaled by 1e8.

    Raises ValueError if the string carries non-zero digits beyond 8 decimals
    (that would silently lose precision).
    """
    if s.__class__ is not str:
        s = str(s)
    i = s.find(".")
    if i < 0:
        if "e" in s or "E" in s:
            return parse(format(Decimal(s), "f"))
        return int(s) * SCALE
    frac = s[i + 1:]
    n = len(frac)
    if n <= DECIMALS:
        return int(s[:i] + frac + "0" * (DECIMALS - n)) if (i or n) else 0
    if frac[DECIMALS:].strip("0"):
        raise ValueError(f"more than {DECIMALS} significant decimals: {s}")
    return int(s[:i] + frac[:DECIMALS])


def to_float(v: int) -> float:
    return v / SCALE


def notional_to_float(v: int) -> float:
    return v / NOTIONAL_SCALE


def fmt(v: int) -> str:
    """Format a 1e8-scaled int back to a canonical decimal string."""
    sign = "-" if v < 0 else ""
    v = abs(v)
    whole, frac = divmod(v, SCALE)
    if not frac:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{frac:08d}".rstrip("0")


def decimals_of(step: str) -> int:
    """Number of significant decimals of a step string, e.g. '0.01000000' -> 2."""
    d = Decimal(step).normalize()
    exp = d.as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


def nice_step(raw: float, tick: float) -> float:
    """Round ``raw`` up to a 'nice' multiple (1, 2, 5 x 10^k) that is also a
    multiple of ``tick``."""
    if raw <= tick:
        return tick
    import math

    p = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        cand = m * p
        if cand >= raw:
            k = max(1, round(cand / tick))
            return k * tick
    return raw
