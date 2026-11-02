"""Forward-window provider for D4 — the data bridge that hands the forward-tracking job
a wallet's POST-enrollment positions, shaped per the card's metric.

`ForwardWindowProvider` contract (see forward_tracking.py): `(wallet, since_ts, metric)
-> list[dict] | None`. Return `None` ONLY when data cannot be fetched (D4 stays
active/awaiting_data, never faking a failed test); return `[]` when fetched but nothing
qualifies.

calibration_edge (sizing_card) — BUILT:
  - re-fetch the wallet's closed positions; keep only positions entered AFTER `since_ts`
    (strict PIT) that have RESOLVED; map to sizing rows {entry_price, size, won}.
  - exclusions: negativeRisk markets (hedge contamination), pre-enrollment rows,
    unresolved rows.

dead_zone_drift_gap (timing_card) — DEFERRED: no reliable per-position post-entry price
feed exists yet, so this provider returns `None` for that metric (D4 stays
active/awaiting_data — it does NOT fabricate a drift). Build it only once a reliable
post-entry price feed is wired (required shape: entry_ts, entry_price, next_session_price).

negativeRisk note: raw Data-API /positions rows do NOT carry a negRisk flag, so the
key-based detector only bites when the fetcher enriches rows. A precomputed
`negrisk_conditions` set (condition_ids known to be negRisk) can be supplied to make the
exclusion effective on un-enriched rows; until enrichment exists, the exclusion is a
documented no-op on live closed-position data.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

# Resolution thresholds (mirror web_settings / domain convention: a position has resolved
# when its outcome price has collapsed to ~1 (won) or ~0 (lost)).
RESOLUTION_WIN = 0.97
RESOLUTION_LOSS = 0.03
_EPS = 1e-9

# Keys a row may carry to flag a negativeRisk market (matches
# wallet_protocol.liquidity._is_negative_risk; replicated to avoid a private import).
_NEGRISK_KEYS = ("negRisk", "negativeRisk", "neg_risk", "enableNegRisk")

# (wallet) -> raw closed-position dicts, or None if the fetch itself failed.
PositionFetcher = Callable[[str], "Sequence[Mapping[str, Any]] | None"]


def _boolish(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return bool(value)


def _row_is_negrisk(raw: Mapping[str, Any]) -> bool:
    return any(_boolish(raw.get(k)) for k in _NEGRISK_KEYS)


def sizing_row_core(raw: Mapping[str, Any], is_negrisk: Callable[[Mapping], bool]
                    ) -> "tuple[int, dict] | None":
    """Map a raw closed position to `(entry_ts, {entry_price, size, won})`, or None if it
    fails the resolved/negRisk/validity filters. NO time filter — callers apply their own
    bound (provider: entry_ts > since; batch enrollment: entry_ts ≤ as_of)."""
    try:
        entry = float(raw["avgPrice"]); cur = float(raw["curPrice"])
        ts = int(raw["timestamp"]); rpnl = float(raw["realizedPnl"])
    except (KeyError, TypeError, ValueError):
        return None
    if is_negrisk(raw):                                  # hedge contamination guard
        return None
    if not (0.0 < entry < 1.0):
        return None
    if not (cur >= RESOLUTION_WIN or cur <= RESOLUTION_LOSS):   # unresolved
        return None
    denom = cur - entry
    if abs(denom) < _EPS:
        return None
    won = 1 if cur >= RESOLUTION_WIN else 0
    size = abs(rpnl / denom) * entry                     # ADR-004 cost basis = avgPrice * shares
    if size <= 0:
        return None
    return ts, {"entry_price": entry, "size": size, "won": won}


def _to_sizing_row(raw: Mapping[str, Any], since_ts: int, is_negrisk: Callable[[Mapping], bool]
                   ) -> dict | None:
    """Forward-window mapping: a sizing row for a position entered AFTER `since_ts` (strict PIT)."""
    core = sizing_row_core(raw, is_negrisk)
    if core is None:
        return None
    ts, row = core
    return row if ts > since_ts else None


class WalletForwardWindowProvider:
    """ForwardWindowProvider backed by a wallet position fetcher."""

    def __init__(self, fetch_closed_positions: PositionFetcher, *,
                 negrisk_conditions: "set[str] | None" = None,
                 negrisk_lookup: "Callable[[list[str]], set[str]] | None" = None) -> None:
        self._fetch = fetch_closed_positions
        self._negrisk_conditions = set(negrisk_conditions or ())
        # optional lazy enrichment: given the fetched positions' condition_ids, return the
        # negRisk subset (so one provider can enrich per-wallet across a multi-wallet D4 pass).
        self._negrisk_lookup = negrisk_lookup

    def __call__(self, wallet: str, since_ts: int, metric: str, *, until_ts: int | None = None
                 ) -> "list[dict] | None":
        if metric == "calibration_edge":
            return self._calibration_edge(wallet, since_ts, until_ts)
        # dead_zone_drift_gap deferred + unknown metrics -> no window (D4 stays awaiting/untrackable)
        return None

    def _calibration_edge(self, wallet: str, since_ts: int, until_ts: int | None
                          ) -> "list[dict] | None":
        raw = self._fetch(wallet)
        if raw is None:                                  # fetch failed -> None
            return None
        negset = set(self._negrisk_conditions)
        if self._negrisk_lookup is not None:
            cids = sorted({str(p.get("conditionId")) for p in raw if p.get("conditionId")})
            negset |= set(self._negrisk_lookup(cids))
        is_negrisk = lambda r: _row_is_negrisk(r) or (r.get("conditionId") in negset)
        rows = []
        for p in raw:
            # upper bound (until_ts) slices the forward window so consecutive D4 runs at
            # increasing `now` form non-overlapping batches (two-pass promotion in backtest).
            if until_ts is not None:
                try:
                    if int(p.get("timestamp", 0)) > until_ts:
                        continue
                except (TypeError, ValueError):
                    continue
            row = _to_sizing_row(p, since_ts, is_negrisk)
            if row is not None:
                rows.append(row)
        return rows                                      # [] when nothing qualifies
