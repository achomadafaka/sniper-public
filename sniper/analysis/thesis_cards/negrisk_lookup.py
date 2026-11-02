"""negativeRisk enrichment for the forward-window provider (D4 data-ops).

Raw Data-API /positions rows carry no negRisk flag, so the provider's exclusion is a
no-op on live data unless a precomputed negRisk condition_id set is supplied. This module
builds that set from the Gamma `/markets` API (the authoritative negRisk source) and is
fed in via `WalletForwardWindowProvider(negrisk_conditions=...)` — provider behavior is
unchanged (None on fetch failure, [] on no qualifying rows).

The markets fetcher is injectable so the lookup is unit-tested offline; the default
`gamma_negrisk_markets` uses the project's geo/UA-correct session and `closed=true`
(resolved markets are excluded by Gamma's default).
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Iterable, Mapping, Sequence

from sniper.analysis.thesis_cards.forward_window_provider import _row_is_negrisk

# (condition_ids batch) -> market dicts (each with conditionId + a negRisk flag), or None.
MarketsFetcher = Callable[[Sequence[str]], "Sequence[Mapping[str, Any]] | None"]


def load_negrisk_conditions(
    condition_ids: Iterable[str],
    *,
    fetch_markets: MarketsFetcher,
    batch_size: int = 20,
) -> set[str]:
    """Return the subset of `condition_ids` whose Gamma market is negativeRisk.

    Markets not returned by the fetcher are treated as NOT negRisk (conservative: we only
    exclude confirmed negRisk markets). Only wanted condition_ids are considered."""
    wanted = {str(c).strip() for c in condition_ids if c}
    if not wanted:
        return set()
    out: set[str] = set()
    ordered = sorted(wanted)
    for i in range(0, len(ordered), batch_size):
        batch = ordered[i:i + batch_size]
        for market in (fetch_markets(batch) or []):
            cid = str(market.get("conditionId") or "").strip()
            if cid in wanted and _row_is_negrisk(market):
                out.add(cid)
    return out


class NegriskCache:
    """Durable, deduplicating negRisk lookup — a `negrisk_lookup` callable (condition_ids ->
    negRisk subset) that queries Gamma ONLY for condition_ids it has never seen, persisting
    every resolved condition_id -> bool to disk. A market's negRisk flag is immutable, so the
    first run pays once per unique market and every re-run (and every shared market across
    wallets) is free. Per-batch Gamma errors are tolerated (those ids stay unresolved and are
    retried next time) so a long run never dies on a transient API error."""

    def __init__(self, path: str, *, fetch_markets: MarketsFetcher, batch_size: int = 50) -> None:
        self.path = path
        self._fetch = fetch_markets
        self._batch_size = batch_size
        self.cache: dict[str, bool] = {}
        if os.path.exists(path):
            try:
                self.cache = {str(k): bool(v) for k, v in json.load(open(path)).items()}
            except (OSError, ValueError):
                self.cache = {}

    def __call__(self, condition_ids: Iterable[str]) -> set[str]:
        ids = {str(c).strip() for c in condition_ids if c}
        unknown = sorted(ids - self.cache.keys())
        if unknown:
            resolved = False
            for i in range(0, len(unknown), self._batch_size):
                batch = unknown[i:i + self._batch_size]
                try:
                    neg = load_negrisk_conditions(batch, fetch_markets=self._fetch,
                                                  batch_size=self._batch_size)
                except Exception:                          # noqa: BLE001 — transient API error
                    continue                                # leave unresolved; retry next run
                for c in batch:
                    self.cache[c] = c in neg               # markets not returned -> not negRisk
                resolved = True
            if resolved:
                self._save()
        return {c for c in ids if self.cache.get(c)}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.cache, f)
        os.replace(tmp, self.path)


def gamma_negrisk_markets(condition_ids: Sequence[str], *, session: Any = None
                          ) -> list[dict]:
    """Live Gamma /markets fetch for a batch of resolved condition_ids (closed=true)."""
    from sniper.markets import polymarket_api as P
    s = session or P.build_session()
    data = P._request_json(
        s, f"{P.GAMMA_BASE_URL}/markets",
        params={"condition_ids": list(condition_ids), "closed": "true", "limit": 500},
    )
    return data if isinstance(data, list) else []
