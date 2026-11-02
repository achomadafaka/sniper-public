"""Hyperliquid instrument resolution across the main universe and HIP-3 dexes.

Equities, indices and commodities are NOT in the 232-asset main universe — they live
on builder-deployed perp dexes addressed as `dex:SYMBOL`: flx:OIL, km:USOIL, xyz:TSLA,
vntl:DEFENSE, cash:USA500. The dex list is permissionless, so it is fetched per run
and never hardcoded.

Refusing an unknown symbol is the whole point. The prior system emitted EIS, OIL-USD
and OIL-PERP, none of which exist on any Hyperliquid venue (audit F-01) — one of them
with reasoning text that said so outright.

Spec: docs/superpowers/specs/2026-07-29-intel-paper-testing-design.md
"""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

INFO_URL = "https://api.hyperliquid.xyz/info"


def _post(payload: dict, timeout: float = 10.0):
    req = urllib.request.Request(
        INFO_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "sniper/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def load_universe(timeout: float = 10.0) -> set[str]:
    """Every tradeable symbol. Returns an empty set on failure — callers then refuse
    to open, which is correct: an unverifiable universe must not admit an instrument."""
    universe: set[str] = set()
    try:
        for a in _post({"type": "meta"}, timeout).get("universe", []):
            universe.add(a["name"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("hl load_universe: main meta failed: %s", exc)
        return set()

    try:
        dexes = [d.get("name") for d in _post({"type": "perpDexs"}, timeout) if d]
    except Exception as exc:  # noqa: BLE001
        logger.warning("hl load_universe: perpDexs failed: %s", exc)
        dexes = []

    for dex in dexes:
        if not dex:
            continue
        try:
            for a in _post({"type": "meta", "dex": dex}, timeout).get("universe", []):
                universe.add(a["name"])
        except Exception as exc:  # noqa: BLE001
            logger.info("hl load_universe: dex %s failed: %s", dex, exc)
    return universe


def resolve_symbol(proposed: str, universe: set[str]) -> Optional[str]:
    """Exact match, else a UNIQUE suffix match across dexes.

    None when absent or ambiguous. Ambiguity must refuse rather than guess: TSLA
    exists on several dexes with different liquidity, and picking one silently
    would fabricate a venue choice the analyst never made.
    """
    s = str(proposed or "").strip()
    if not s or not universe:
        return None
    if s in universe:
        return s
    hits = [u for u in universe if ":" in u and u.split(":", 1)[1] == s]
    return hits[0] if len(hits) == 1 else None


def all_mids(symbols: set[str], timeout: float = 10.0) -> dict[str, float]:
    """Mids for the given symbols. One call for the main universe plus one per dex —
    cost is O(distinct dexes), never O(positions)."""
    dexes = {s.split(":", 1)[0] for s in symbols if ":" in s}
    if any(":" not in s for s in symbols):
        dexes.add("")

    out: dict[str, float] = {}
    for dex in sorted(dexes):
        payload = {"type": "allMids"}
        if dex:
            payload["dex"] = dex
        try:
            raw = _post(payload, timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hl all_mids failed for dex=%r: %s", dex, exc)
            continue
        for sym, px in (raw or {}).items():
            if sym in symbols:
                try:
                    out[sym] = float(px)
                except (TypeError, ValueError):
                    pass
    return out
