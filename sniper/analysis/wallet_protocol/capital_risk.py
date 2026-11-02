"""Capital & Risk surface — `capital_risk_surface`.

Spec: docs/superpowers/specs/2026-05-22-capital-risk-surface.md

Converts the TheOnlyHuman capital-base discovery into a reusable surface:
how much capital a wallet needed to PLAY (peak concurrent deployment), how
much it needed to SURVIVE (max drawdown), how hard it churned that pool
(recycle factor), what it returned on the real base, and whether the PnL
was broad or a few lucky hits.

HARD WALL — display-only. `compute_capital_risk_surface` is read ONLY by
the report renderer. It MUST NEVER feed `overall_verdict`, `gate_result`,
`primary_edge_type`, `copyability_note`, branch/hypothesis logic, or append
to the top-level `data_quality_flags`. Caveats live in the surface's own
`caveats` list. Pure function — no I/O.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

SURFACE_VERSION = 1

# A surface below this many closed positions is too thin to characterize.
MIN_CLOSED_FLOOR = 20
# top-5 share of gross profit thresholds → concentration bucket.
_CONCENTRATED_AT = 0.60
_MODERATE_AT = 0.30

# data_state severity ranking — worst wins.
_SEVERITY = {"ok": 0, "proxy_only": 1, "underpowered": 2, "data_unavailable": 3}

_DD_APPROX_CAVEAT = (
    "DRAWDOWN_ORDERING_APPROXIMATE: some positions lacked a redemption "
    "timestamp; equity ordered by end-date / position timestamp."
)


def _cid(x: Any) -> str:
    """Normalize a condition id for keying — lowercased + stripped — so the
    REST `conditionId` and subgraph `condition` fields join reliably
    regardless of 0x-case differences between sources."""
    return str(x or "").strip().lower()


def _num(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _trade_usdc(row: Mapping[str, Any]) -> float:
    """USDC moved by a trade row. REST rows carry `usdcSize`; subgraph rows
    carry `price` + `size` instead."""
    amt = _num(row.get("usdcSize"))
    if amt > 0:
        return amt
    return _num(row.get("price")) * _num(row.get("size"))


def _trade_shares(row: Mapping[str, Any], usdc: float) -> float:
    """Share count of a trade row. REST/subgraph rows can omit `size`;
    derive it from `usdc / price` so committed cost and share count stay in
    sync. A desync silently mis-releases later SELLs — applies symmetrically
    to BUYs (cost = the buy outlay) and SELLs (usdc = the sale proceeds);
    `usdc / price` = shares either way."""
    sh = _num(row.get("size"))
    if sh > 0:
        return sh
    price = _num(row.get("price"))
    return (usdc / price) if (price > 0 and usdc > 0) else 0.0


def _ledger_events(capital_ledger: Any) -> list[dict]:
    """Tolerate a CapitalLedger object (`.events`), a raw fixture dict
    (`["events"]`), or None."""
    if capital_ledger is None:
        return []
    ev = getattr(capital_ledger, "events", None)
    if ev is None and isinstance(capital_ledger, Mapping):
        ev = capital_ledger.get("events")
    return list(ev or [])


def _parse_end_date(raw: Any) -> int | None:
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, TypeError):
        return None


def _gini(values: list[float]) -> float | None:
    """Gini coefficient of non-negative values. 0 = perfectly even,
    →1 = one value holds it all. None when undefined (< 2 values / no
    mass) — `None` means unknown, never a false 0.0."""
    xs = sorted(v for v in values if v > 0)
    n = len(xs)
    total = sum(xs)
    if n < 2 or total <= 0:
        return None
    cum = sum((i + 1) * x for i, x in enumerate(xs))
    return (2.0 * cum) / (n * total) - (n + 1) / n


def _empty(data_state: str, reason: str) -> dict:
    return {
        "surface_version": SURFACE_VERSION,
        "data_state": data_state,
        "peak_concurrent_deployed_usdc": None,
        "peak_deployed_at": None,
        "cumulative_deployed_usdc": None,
        "recycle_factor": None,
        "max_drawdown_usdc": None,
        "max_drawdown_pct_of_peak": None,
        "return_on_peak_deployed": None,
        "pnl_concentration": None,
        "lifespan_days": None,
        "caveats": [reason],
    }


def compute_capital_risk_surface(
    closed_positions: list[Mapping[str, Any]],
    activity_rows: list[Mapping[str, Any]],
    capital_ledger: Any,
    net_pnl: float,
    n_unmapped_fills: int = 0,
) -> dict:
    """Build the Capital & Risk surface. Pure, deterministic, display-only.

    `n_unmapped_fills` > 0 means some subgraph fills could not be mapped to a
    condition — BUY/SELL cashflow is then incomplete and the surface degrades
    to `proxy_only` (spec §5)."""
    closed = list(closed_positions or [])
    if not closed:
        return _empty("data_unavailable", "no_closed_positions")

    caveats: list[str] = []
    redemptions = [
        e for e in _ledger_events(capital_ledger)
        if (e.get("kind") or "") == "redemption"
    ]
    # condition_id -> earliest redemption timestamp (resolution time).
    # Keys are normalized via _cid so the activity↔ledger join is immune to
    # 0x-case / prefix differences between the REST and subgraph sources.
    redeem_ts: dict[str, int] = {}
    for e in redemptions:
        cond = _cid(e.get("condition"))
        ts = int(e.get("timestamp") or 0)
        if cond and ts and (cond not in redeem_ts or ts < redeem_ts[cond]):
            redeem_ts[cond] = ts

    # ---- §4.1 concurrent-deployment curve --------------------------------
    # Capital is tracked per outcome-token (asset), not per condition —
    # buying YES and NO of the same market are distinct commitments.
    # event kinds ordered BUY < SELL < REDEEM at a shared timestamp.
    # tuple: (ts, kind, condition, asset, cost, shares)
    events: list[tuple[int, int, str, str, float, float]] = []
    n_buys = 0
    untracked_buys = 0   # BUYs with cost > 0 but no derivable share count
    untracked_sells = 0  # SELLs against a held position but no derivable size
    cond_assets: dict[str, set[str]] = {}
    for r in activity_rows or []:
        if r.get("type") != "TRADE":
            continue
        ts = int(r.get("timestamp") or 0)
        cond = _cid(r.get("conditionId"))
        asset = str(r.get("asset") or "")
        side = r.get("side")
        if side == "BUY":
            cost = _trade_usdc(r)
            sh = _trade_shares(r, cost)
            if cost > 0 and sh <= 0:
                untracked_buys += 1
            events.append((ts, 0, cond, asset, cost, sh))
            cond_assets.setdefault(cond, set()).add(asset)
            n_buys += 1
        elif side == "SELL":
            # derive share count from proceeds/price when `size` is absent —
            # same source irregularity as BUYs. A SELL that still cannot
            # derive its size cannot release committed capital; the walk
            # counts those (untracked_sells) only when a position is held.
            events.append((ts, 1, cond, asset, 0.0,
                           _trade_shares(r, _trade_usdc(r))))
    for e in redemptions:
        events.append((int(e.get("timestamp") or 0), 2,
                       _cid(e.get("condition")), "", 0.0, 0.0))
    # Total sort over the whole tuple — fully deterministic, fetch-order
    # independent. BUYs/SELLs/REDEEMs commute within a shared timestamp for
    # peak/cumulative purposes; the total order just removes any ambiguity.
    events.sort()

    committed: dict[str, float] = {}  # asset -> cost basis still deployed
    shares: dict[str, float] = {}     # asset -> shares still held
    glob = 0.0
    peak = 0.0
    peak_ts: int | None = None
    cumulative = 0.0
    for ts, kind, cond, asset, cost, size in events:
        if kind == 0:  # BUY — commit cost, accrue shares
            committed[asset] = committed.get(asset, 0.0) + cost
            shares[asset] = shares.get(asset, 0.0) + size
            glob += cost
            cumulative += cost
        elif kind == 1:  # SELL — release the COST BASIS of shares sold,
            # NOT the sale proceeds. A losing sale's proceeds are below cost
            # (releasing proceeds leaves phantom committed capital); a
            # winning sale's exceed it. Cost-basis fraction is correct both.
            held = shares.get(asset, 0.0)
            if held > 0:
                if size <= 0:
                    # A SELL against a held position whose share count could
                    # not be derived — it cannot release committed capital,
                    # leaving it phantom-deployed. Flag rather than mis-state.
                    untracked_sells += 1
                else:
                    sold = min(size, held)
                    released = committed.get(asset, 0.0) * (sold / held)
                    committed[asset] = committed.get(asset, 0.0) - released
                    shares[asset] = held - sold
                    glob -= released
        else:  # REDEEM — release every asset of the resolved condition
            for a in cond_assets.get(cond, ()):
                rel = committed.get(a, 0.0)
                if rel:
                    committed[a] = 0.0
                    shares[a] = 0.0
                    glob -= rel
        if glob > peak:
            peak, peak_ts = glob, ts

    peak_computable = n_buys > 0 and peak > 0
    if not peak_computable:
        # The capital-base half of the surface is dead (no trade cashflow),
        # but the realized-PnL equity curve is still computable from closed
        # positions. Surface it honestly under `underpowered` — NOT
        # `data_unavailable`, which would imply nothing was computed.
        dd_usdc, dd_approx = _max_drawdown(closed, redeem_ts)
        surface = _empty("underpowered", "no_trade_cashflow")
        surface["max_drawdown_usdc"] = (
            round(dd_usdc, 2) if dd_usdc is not None else None
        )
        surface["pnl_concentration"] = _concentration(closed)
        if dd_approx:
            surface["caveats"].append(_DD_APPROX_CAVEAT)
        return surface

    # ---- §4.2 realized-PnL equity curve → max drawdown -------------------
    max_dd, approx_ordering = _max_drawdown(closed, redeem_ts)
    if approx_ordering:
        caveats.append(_DD_APPROX_CAVEAT)
    if n_unmapped_fills > 0:
        caveats.append(
            f"UNMAPPED_FILLS: {n_unmapped_fills} subgraph fills could not be "
            "mapped to a condition; deployment cashflow is under-counted."
        )
    if untracked_buys > 0:
        caveats.append(
            f"UNTRACKED_BUYS: {untracked_buys} BUY rows had cost but no "
            "derivable share count; their capital is not released by SELLs "
            "(peak may be overstated)."
        )
    if untracked_sells > 0:
        caveats.append(
            f"UNTRACKED_SELLS: {untracked_sells} SELL rows against a held "
            "position had no derivable share count; their capital release is "
            "missed (peak may be overstated)."
        )

    # ---- §4.3 PnL concentration -----------------------------------------
    concentration = _concentration(closed)

    # ---- ratios ----------------------------------------------------------
    recycle = round(cumulative / peak, 4) if peak > 0 else None
    rob = round(net_pnl / peak, 6) if peak > 0 else None
    dd_pct = round(max_dd / peak, 6) if (peak > 0 and max_dd is not None) else None

    lifespan_days = None
    if events:
        span = events[-1][0] - events[0][0]
        lifespan_days = round(span / 86400.0, 1) if span > 0 else 0.0

    # ---- §5 data_state ---------------------------------------------------
    state = "ok"
    if len(closed) < MIN_CLOSED_FLOOR:
        state = _worse(state, "underpowered")
    if (concentration or {}).get("n_winning_positions", 0) < 2:
        state = _worse(state, "underpowered")
    if (approx_ordering or n_unmapped_fills > 0
            or untracked_buys > 0 or untracked_sells > 0):
        state = _worse(state, "proxy_only")

    return {
        "surface_version": SURFACE_VERSION,
        "data_state": state,
        "peak_concurrent_deployed_usdc": round(peak, 2),
        "peak_deployed_at": peak_ts,
        "cumulative_deployed_usdc": round(cumulative, 2),
        "recycle_factor": recycle,
        "max_drawdown_usdc": (round(max_dd, 2) if max_dd is not None else None),
        "max_drawdown_pct_of_peak": dd_pct,
        "return_on_peak_deployed": rob,
        "pnl_concentration": concentration,
        "lifespan_days": lifespan_days,
        "caveats": caveats,
    }


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def _max_drawdown(closed: list[Mapping[str, Any]],
                  redeem_ts: dict[str, int]) -> tuple[float | None, bool]:
    """Largest peak-to-trough decline of the realized-PnL equity curve.
    Returns (max_drawdown_usdc, ordering_was_approximate)."""
    # (close_ts, condition_id, realized_pnl). condition_id is the sort
    # tiebreak so the curve is independent of closed-position fetch order
    # when two positions share a close timestamp — replay-deterministic.
    contributions: list[tuple[int, str, float]] = []
    approx = False
    for p in closed:
        cond = _cid(p.get("conditionId"))
        ts = redeem_ts.get(cond)
        if ts is None:
            ts = _parse_end_date(p.get("endDate"))
            approx = True
        if ts is None:
            ts = int(p.get("timestamp") or 0)
            approx = True
        contributions.append((ts, cond, _num(p.get("realizedPnl"))))
    if not contributions:
        return None, approx
    contributions.sort(key=lambda x: (x[0], x[1]))
    equity = 0.0
    running_peak = 0.0
    max_dd = 0.0
    for _ts, _cond, pnl in contributions:
        equity += pnl
        if equity > running_peak:
            running_peak = equity
        dd = running_peak - equity
        if dd > max_dd:
            max_dd = dd
    return max_dd, approx


def _concentration(closed: list[Mapping[str, Any]]) -> dict:
    """Whether the PnL came from many edges or a few outsized hits."""
    winners = sorted((_num(p.get("realizedPnl")) for p in closed
                      if _num(p.get("realizedPnl")) > 0), reverse=True)
    n_win = len(winners)
    gross = sum(winners)
    top5_share = round(sum(winners[:5]) / gross, 4) if gross > 0 else None
    g = _gini(winners)
    gini = round(g, 4) if g is not None else None
    if top5_share is None:
        bucket = None
    elif top5_share >= _CONCENTRATED_AT:
        bucket = "concentrated"
    elif top5_share >= _MODERATE_AT:
        bucket = "moderate"
    else:
        bucket = "broad"
    return {
        "gini": gini,
        "top5_share_of_gross_profit": top5_share,
        "n_winning_positions": n_win,
        "bucket": bucket,
    }
