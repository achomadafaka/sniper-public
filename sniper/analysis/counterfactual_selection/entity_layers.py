"""CSE-F1 — entity layering.

Reconstruct the unit ladder from raw activity, so detection runs at the right
grain. The load-bearing distinction:

  SELECTION unit = engagement (entering a NEW market)   ← the decision / the edge
  EXECUTION unit = fills / clips (laddering into it)     ← mechanics, NOT the edge

The behavioral classifier mislabeled SwissMiss because it read fill cadence
(execution, ~6s) as selection cadence; her engagement cadence is ~hours.

Pure compute on WalletData.activity_rows (TRADE rows: type, side, price, size,
asset, conditionId, outcome, timestamp). No network. JSON-serializable output.
"""
from __future__ import annotations

import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Any, Mapping, Sequence

CLIP_GAP_S = int(os.getenv("CSE_CLIP_GAP_S", "120"))
BASKET_GAP_S = int(os.getenv("CSE_BASKET_GAP_S", "600"))
SESSION_GAP_S = int(os.getenv("CSE_SESSION_GAP_S", "3600"))


def _buys(activity_rows: Sequence[Mapping[str, Any]]) -> list[dict]:
    out = []
    for r in activity_rows or []:
        if str(r.get("type")) != "TRADE":
            continue
        if str(r.get("side") or "").upper() != "BUY":
            continue
        if r.get("timestamp") is None:
            continue
        out.append(r)
    return sorted(out, key=lambda r: int(r.get("timestamp") or 0))


def _engagement_key(r: Mapping[str, Any]) -> tuple[str, str]:
    return (str(r.get("conditionId") or ""), str(r.get("asset") or ""))


def _count_clips(sorted_rows: list[Mapping[str, Any]], gap_s: int = CLIP_GAP_S) -> int:
    """A clip = a burst of fills within gap_s = one 'add decision'."""
    if not sorted_rows:
        return 0
    clips = 1
    prev = int(sorted_rows[0].get("timestamp") or 0)
    for r in sorted_rows[1:]:
        ts = int(r.get("timestamp") or 0)
        if ts - prev > gap_s:
            clips += 1
        prev = ts
    return clips


def build_clips(activity_rows: Sequence[Mapping[str, Any]], *, gap_s: int = CLIP_GAP_S) -> list[dict]:
    """Group consecutive same-(condition,asset) BUY fills into clips."""
    buys = _buys(activity_rows)
    by_key: dict[tuple[str, str], list] = defaultdict(list)
    for r in buys:
        by_key[_engagement_key(r)].append(r)
    clips: list[dict] = []
    for (cid, asset), rows in by_key.items():
        rows.sort(key=lambda r: int(r.get("timestamp") or 0))
        cur: list = []
        prev = None
        for r in rows:
            ts = int(r.get("timestamp") or 0)
            if prev is not None and ts - prev > gap_s and cur:
                clips.append(_clip_row(cid, asset, cur))
                cur = []
            cur.append(r)
            prev = ts
        if cur:
            clips.append(_clip_row(cid, asset, cur))
    return sorted(clips, key=lambda c: c["start_ts"])


def _clip_row(cid: str, asset: str, rows: list[Mapping[str, Any]]) -> dict:
    ts = [int(r.get("timestamp") or 0) for r in rows]
    sz = sum(float(r.get("size") or 0) for r in rows)
    return {"conditionId": cid, "asset": asset, "start_ts": ts[0], "end_ts": ts[-1],
            "n_fills": len(rows), "size": sz}


def build_engagements(activity_rows: Sequence[Mapping[str, Any]]) -> list[dict]:
    """One row per (conditionId, asset) the wallet BOUGHT into — the SELECTION unit.

    entry_vwap = size-weighted buy price; first_entry_ts = the selection moment.
    n_clips/n_fills/accumulation_span describe EXECUTION (kept separate).
    """
    buys = _buys(activity_rows)
    by_key: dict[tuple[str, str], list] = defaultdict(list)
    for r in buys:
        by_key[_engagement_key(r)].append(r)
    engagements: list[dict] = []
    for (cid, asset), rows in by_key.items():
        rows.sort(key=lambda r: int(r.get("timestamp") or 0))
        ts = [int(r.get("timestamp") or 0) for r in rows]
        num = sum(float(r.get("price") or 0) * float(r.get("size") or 0) for r in rows)
        den = sum(float(r.get("size") or 0) for r in rows)
        engagements.append({
            "conditionId": cid, "asset": asset,
            "outcome": str(rows[0].get("outcome") or ""),
            "market_category": str(rows[0].get("market_category") or rows[0].get("category") or ""),
            "first_entry_ts": ts[0], "last_entry_ts": ts[-1],
            "n_fills": len(rows), "n_clips": _count_clips(rows),
            "accumulation_span_s": ts[-1] - ts[0],
            "entry_vwap": (num / den if den > 0 else None),
            "first_entry_price": (float(rows[0].get("price")) if rows[0].get("price") is not None else None),
            "total_size": den,
        })
    engagements.sort(key=lambda e: e["first_entry_ts"])
    for i, e in enumerate(engagements):
        e["nth_market"] = i + 1
    return engagements


def build_sessions(activity_rows: Sequence[Mapping[str, Any]], *, gap_s: int = SESSION_GAP_S) -> list[dict]:
    """Cluster ALL activity into sessions (gap > gap_s breaks a session)."""
    buys = _buys(activity_rows)
    if not buys:
        return []
    sessions: list[dict] = []
    cur: list = []
    prev = None
    for r in buys:
        ts = int(r.get("timestamp") or 0)
        if prev is not None and ts - prev > gap_s and cur:
            sessions.append(_session_row(cur))
            cur = []
        cur.append(r)
        prev = ts
    if cur:
        sessions.append(_session_row(cur))
    return sessions


def _session_row(rows: list[Mapping[str, Any]]) -> dict:
    ts = [int(r.get("timestamp") or 0) for r in rows]
    new_markets = {_engagement_key(r) for r in rows}
    return {"start_ts": ts[0], "end_ts": ts[-1], "duration_s": ts[-1] - ts[0],
            "n_fills": len(rows), "n_new_markets": len(new_markets)}


def build_baskets(engagements: Sequence[Mapping[str, Any]], *, gap_s: int = BASKET_GAP_S) -> list[dict]:
    """Cluster NEW-market first-entries in time — co-selected markets."""
    if not engagements:
        return []
    es = sorted(engagements, key=lambda e: e["first_entry_ts"])
    baskets: list[dict] = []
    cur: list = [es[0]]
    for e in es[1:]:
        if e["first_entry_ts"] - cur[-1]["first_entry_ts"] > gap_s:
            baskets.append(_basket_row(cur))
            cur = []
        cur.append(e)
    baskets.append(_basket_row(cur))
    for i, b in enumerate(baskets):
        b["basket_id"] = i
    return baskets


def _basket_row(members: list[Mapping[str, Any]]) -> dict:
    return {"start_ts": members[0]["first_entry_ts"], "n_markets": len(members),
            "conditionIds": [m["conditionId"] for m in members]}


# --------------------------------------------------------------------------
# Decision-grain signals (the selection vs execution separation)
# --------------------------------------------------------------------------

def selection_cadence(engagements: Sequence[Mapping[str, Any]]) -> dict:
    """Gaps between entering DISTINCT new markets — the SELECTION decision rate."""
    fe = sorted(e["first_entry_ts"] for e in engagements)
    gaps = [fe[i + 1] - fe[i] for i in range(len(fe) - 1)]
    span_h = (fe[-1] - fe[0]) / 3600 if len(fe) > 1 else 0.0
    return {
        "n_markets": len(fe),
        "median_gap_s": int(median(gaps)) if gaps else None,
        "new_markets_per_hour": (len(fe) / span_h) if span_h > 0 else None,
        "span_hours": round(span_h, 1),
    }


def selection_schedule(engagements: Sequence[Mapping[str, Any]]) -> dict:
    """Hour-of-day (UTC) histogram of NEW-market entries — automated selection runs
    24/7 incl. dead hours; human selection clusters in waking hours."""
    hours = Counter(
        datetime.fromtimestamp(int(e["first_entry_ts"]), tz=timezone.utc).hour
        for e in engagements
    )
    n = sum(hours.values())
    covered = len(hours)
    dead = sum(1 for h in range(24) if hours.get(h, 0) == 0)
    return {
        "n": n,
        "hours_covered": covered,
        "hour_coverage_fraction": covered / 24.0 if n else None,
        "dead_hours": dead,
        "histogram": {h: hours.get(h, 0) for h in range(24)},
    }


def execution_intensity(engagements: Sequence[Mapping[str, Any]]) -> dict:
    """Fills/clips per engagement — EXECUTION mechanics, NOT selection. Reported
    separately so it is never mistaken for the selection signal."""
    if not engagements:
        return {"median_fills_per_market": None, "median_clips_per_market": None}
    return {
        "median_fills_per_market": median(e["n_fills"] for e in engagements),
        "median_clips_per_market": median(e["n_clips"] for e in engagements),
        "max_fills_in_one_market": max(e["n_fills"] for e in engagements),
    }
