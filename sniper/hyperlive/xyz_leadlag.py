"""Cross-asset lead-lag helpers for the Hyperliquid `xyz` HIP-3 dex (equities / indices /
commodities / FX). The thesis: the HL perp lags its real-world underlying, and because these
markets make BIG scheduled moves (earnings, CPI, oil inventories), the lag can clear the ~9bps
taker fee where crypto micro-momentum could not.

Pure functions only (network lives in the capture script). The basis here is perp-vs-REAL
(Yahoo reference), reused through the same scan_log/basis_direction_edge machinery as the
crypto Binance basis. Research capture; paper only; selection/entry labels, never PnL."""
from __future__ import annotations

# xyz dex asset -> Yahoo reference symbol. Only assets with a clean, public, ~live reference are
# mapped; private/synthetic names (SPCX=SpaceX, XYZ100, SKHX) have no leader to lag and are
# deliberately omitted. Commodities/indices/FX are futures-referenced (~24h) -> cleanest;
# single equities only price-discover during the cash session (guarded at capture time).
REF_MAP = {
    # commodities (CME ~23h)
    "CL": "CL=F", "BRENTOIL": "BZ=F", "GOLD": "GC=F", "SILVER": "SI=F",
    "COPPER": "HG=F", "GAS": "NG=F", "OIL": "CL=F",
    # indices / index futures
    "SP500": "ES=F", "USA500": "ES=F", "USA100": "NQ=F", "US500": "ES=F", "USTECH": "NQ=F",
    # FX
    "EUR": "EURUSD=X", "JPY": "JPY=X",
    # liquid US equities (cash session only)
    "NVDA": "NVDA", "TSLA": "TSLA", "AAPL": "AAPL", "MSFT": "MSFT", "GOOGL": "GOOGL",
    "AMZN": "AMZN", "META": "META", "AMD": "AMD", "COIN": "COIN", "HOOD": "HOOD",
    "PLTR": "PLTR", "MSTR": "MSTR", "NFLX": "NFLX", "ORCL": "ORCL", "MU": "MU",
    "INTC": "INTC", "BABA": "BABA", "CRCL": "CRCL", "COST": "COST", "LLY": "LLY",
}

MAX_SANE_BASIS_BPS = 1000.0   # >10% perp-vs-real = stale oracle / contract mismatch, not a lag


def strip_dex(name: str) -> str:
    """'xyz:NVDA' -> 'NVDA'."""
    return name.split(":", 1)[1] if ":" in name else name


def ref_symbol(asset: str) -> str | None:
    return REF_MAP.get(asset.upper())


def select_referenceable(contexts: list[dict], min_volume: float) -> list[dict]:
    """xyz markets that are BOTH liquid (>= min_volume) AND have a public reference, as
    [{asset, ctx, ref_symbol}]. Everything else can't be lead-lagged and is dropped."""
    out = []
    for c in contexts:
        asset = strip_dex(str(c.get("name") or "")).upper()
        sym = REF_MAP.get(asset)
        if not sym:
            continue
        try:
            vol = float((c.get("ctx") or {}).get("dayNtlVlm") or 0.0)
        except (TypeError, ValueError):
            vol = 0.0
        if vol >= min_volume:
            out.append({"asset": asset, "ctx": c.get("ctx") or {}, "ref_symbol": sym})
    return out


def basis_bps(ref_px: float | None, hl_mid: float | None) -> float | None:
    """(reference - HL perp mid) / HL perp mid, in bps. NULL if either side is missing or the
    gap is too large to be a real lag (stale oracle / wrong contract)."""
    try:
        ref, mid = float(ref_px), float(hl_mid)
    except (TypeError, ValueError):
        return None
    if ref <= 0 or mid <= 0:
        return None
    b = (ref - mid) / mid * 1e4
    if abs(b) > MAX_SANE_BASIS_BPS:
        return None
    return round(b, 2)


def is_live(prev_reg_time: int | None, cur_reg_time: int | None) -> bool:
    """A reference is LIVE (actively price-discovering, i.e. can lead) iff its own
    regularMarketTime ADVANCED since the previous cycle. Immune to the env clock offset:
    it compares the feed's timestamp to itself, never to a local 'now'. Closed markets
    (weekends/after-hours) freeze regularMarketTime -> not live -> no basis logged."""
    if prev_reg_time is None or cur_reg_time is None:
        return False
    return cur_reg_time > prev_reg_time
