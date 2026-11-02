"""Forward paper-FOLLOW simulation — the price-band counterfactual made concrete.

We can only act on a tracked trader's entry AFTER we detect it by polling, at the mark price
available THEN (not their fill px) plus our taker fee. So the honest question this answers is:
'by the time we see a copyable pro open a position and pay 9bps to follow, does it still net
positive?' — exactly the gate that downgraded every Polymarket 'copyable' wallet. Pure functions
over clearinghouseState; the runner persists. Paper only; not copy advice, not a live trigger."""
from __future__ import annotations

FEE_BPS = 9.0                 # HL round-trip taker
MIN_REL_INCREASE = 0.10       # a size bump must be >=10% to count as an 'add' (ignore dust drift)


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def positions_map(state: dict) -> dict:
    """clearinghouseState -> {coin: {szi, side, entry_px}} for non-zero positions."""
    out = {}
    for ap in (state or {}).get("assetPositions", []) or []:
        pos = ap.get("position") or {}
        coin = pos.get("coin")
        szi = _f(pos.get("szi"))
        if not coin or szi == 0:
            continue
        out[coin] = {"szi": szi, "side": "long" if szi > 0 else "short",
                     "entry_px": _f(pos.get("entryPx"))}
    return out


def detect_entries(prev: dict, cur: dict) -> list[dict]:
    """New directional entries between two snapshots: brand-new positions, side flips, and
    meaningful size increases on the same side. Reductions / closes are NOT entries."""
    out = []
    for coin, c in cur.items():
        p = prev.get(coin)
        if p is None:
            out.append({"coin": coin, "side": c["side"], "kind": "new"})
        elif p["side"] != c["side"]:
            out.append({"coin": coin, "side": c["side"], "kind": "flip"})
        elif abs(c["szi"]) > abs(p["szi"]) * (1.0 + MIN_REL_INCREASE):
            out.append({"coin": coin, "side": c["side"], "kind": "increase"})
    return out


def follow_net_bps(side: str, entry_mark: float | None, exit_mark: float | None,
                   fee_bps: float = FEE_BPS) -> float | None:
    """Net bps of following at entry_mark and exiting at exit_mark, in the trader's direction,
    after the round-trip taker fee. None if prices are unusable."""
    e, x = _f(entry_mark), _f(exit_mark)
    if not entry_mark or not exit_mark or e <= 0 or x <= 0:
        return None
    gross = (x - e) / e * 1e4
    if side == "short":
        gross = -gross
    return round(gross - fee_bps, 2)
