"""Phase 2B — pure parsers for extra BTC price/vol sources, plus DVOL->per-second sigma.

Normalizes Coinbase / Kraken / Deribit (perp ticker + index) REST responses to a float price, and Deribit
DVOL (annualized implied-vol index) to the latest close and to a per-second sigma usable by the structural
models. Pure; the live HTTP fetch lives in the runner. All return None on missing/malformed input
(fail-closed — a bad source must drop out of fusion, never inject a fabricated number).
"""
from __future__ import annotations

import math

SECONDS_PER_YEAR = 365 * 86400


def _f(x):
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def parse_coinbase_ticker(resp) -> float | None:
    return _f((resp or {}).get("price"))


def parse_kraken_ticker(resp) -> float | None:
    result = (resp or {}).get("result")
    if not isinstance(result, dict) or not result:
        return None
    pair = next(iter(result.values()), None)
    if not isinstance(pair, dict):
        return None
    c = pair.get("c")
    return _f(c[0]) if isinstance(c, list) and c else None


def parse_deribit_ticker(resp) -> float | None:
    r = (resp or {}).get("result") or {}
    return _f(r.get("last_price")) or _f(r.get("mark_price"))


def parse_deribit_index(resp) -> float | None:
    return _f(((resp or {}).get("result") or {}).get("index_price"))


def parse_dvol(resp) -> float | None:
    data = (((resp or {}).get("result") or {}).get("data")) or []
    if not data:
        return None
    last = data[-1]
    # Deribit volatility series rows are [timestamp, open, high, low, close]
    return _f(last[4]) if isinstance(last, (list, tuple)) and len(last) >= 5 else None


def dvol_to_vol_per_s(dvol_annual_pct) -> float | None:
    v = _f(dvol_annual_pct)
    if v is None:
        return None
    return (v / 100.0) / math.sqrt(SECONDS_PER_YEAR)
