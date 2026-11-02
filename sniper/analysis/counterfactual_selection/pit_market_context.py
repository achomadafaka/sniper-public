"""CSE-F2 — point-in-time market context (the counterfactual foundation).

Pure logic over loaded artifacts (market metadata + a per-market price index).
Answers: which markets were tradeable at T, and each one's state. The heavy job
of BUILDING the artifacts from the Becker parquet (block→time index,
token→condition map, created/end dates, price index) is a separate data-ops
preprocessing step (scripts/build_cse_pit_index.py — spec sre-f2). This module
is what the engine calls; it is fully testable on a small in-memory universe.

Every answer carries a fidelity tier (active/sparse/pre_trade) per F0/G1.
Spec: sre-f2-pit-market-context.md.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

ACTIVE_MIN_TRADES = 5  # within the lookup window to call price "active"


@dataclass
class MarketContext:
    """market_meta: cid -> {created_at, end_date, category, market_type,
    outcome_role, favorite_rank, event_family}. price_index: cid -> sorted
    [(ts, price), ...]."""
    market_meta: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    price_index: Mapping[str, Sequence[tuple]] = field(default_factory=dict)

    @classmethod
    def from_json_file(cls, path: str) -> "MarketContext":
        """Load a context produced by scripts/build_cse_pit_index.py. JSON stores
        price_index points as [ts, price] lists; restore them as tuples."""
        import json
        with open(path) as f:
            raw = json.load(f)
        price_index = {k: [tuple(p) for p in v] for k, v in (raw.get("price_index") or {}).items()}
        return cls(market_meta=raw.get("market_meta") or {}, price_index=price_index)

    # Axes that are point-in-time-derived from price when meta omits them.
    _DERIVED_AXES = ("outcome_role", "favorite_rank")

    def universe_at(self, T: int, *, filters: Mapping[str, Any] | None = None) -> list[str]:
        out = []
        for cid, m in self.market_meta.items():
            ca, ed = m.get("created_at"), m.get("end_date")
            if ca is None or ca > T:
                continue
            if ed is not None and ed < T:
                continue
            if filters and not self._match_at(cid, m, T, filters):
                continue
            out.append(cid)
        return out

    def _scope_value(self, cid: str, m: Mapping[str, Any], T: int, key: str) -> Any:
        """Meta value if present; else point-in-time derivation for derived axes
        (outcome_role/favorite_rank from the YES price at T)."""
        mv = m.get(key)
        if mv is not None or key not in self._DERIVED_AXES:
            return mv
        price, _, _ = self.price_at(cid, T)
        if price is None:
            return None
        if key == "outcome_role":
            return "favorite" if price > 0.5 else "underdog"
        return 1 if price > 0.5 else 2  # favorite_rank

    def _match_at(self, cid: str, m: Mapping[str, Any], T: int,
                  filters: Mapping[str, Any]) -> bool:
        for k, v in filters.items():
            if v is None:
                continue
            mv = self._scope_value(cid, m, T, k)
            if isinstance(v, (list, tuple, set)):
                if mv not in v:
                    return False
            elif mv != v:
                return False
        return True

    def price_at(self, cid: str, T: int) -> tuple[float | None, int | None, str]:
        """(price, staleness_s, fidelity). Nearest trade at-or-before T.

        If no trade exists yet, report pre-trade without looking ahead.
        """
        idx = list(self.price_index.get(cid) or [])
        if not idx:
            return None, None, "pre_trade"
        ts_list = [t for t, _ in idx]
        i = bisect.bisect_right(ts_list, T) - 1
        if i < 0:
            return None, None, "pre_trade"
        price = float(idx[i][1])
        staleness = T - ts_list[i]
        # density in a ±1h window
        lo = bisect.bisect_left(ts_list, T - 3600)
        hi = bisect.bisect_right(ts_list, T + 3600)
        n_window = hi - lo
        fidelity = "active" if n_window >= ACTIVE_MIN_TRADES else "sparse"
        return price, staleness, fidelity

    def market_state_at(self, cid: str, T: int) -> dict:
        m = self.market_meta.get(cid, {})
        price, staleness, fidelity = self.price_at(cid, T)
        ca, ed = m.get("created_at"), m.get("end_date")
        # outcome_role / favorite_rank: explicit meta wins (synthetic tests); else
        # derive point-in-time from the YES price. A market's favorite/underdog
        # role is genuinely time-varying (the price crosses 0.5), so deriving it at
        # T is more correct than a static label when real data omits it.
        outcome_role = m.get("outcome_role")
        favorite_rank = m.get("favorite_rank")
        if outcome_role is None and price is not None:
            outcome_role = "favorite" if price > 0.5 else "underdog"
        if favorite_rank is None and price is not None:
            favorite_rank = 1 if price > 0.5 else 2
        return {
            "conditionId": cid, "price": price, "price_staleness_s": staleness,
            "price_fidelity": fidelity,
            "market_age_s": (T - ca) if ca is not None else None,
            "time_to_resolution_s": (ed - T) if ed is not None else None,
            "category": m.get("category"), "market_type": m.get("market_type"),
            "outcome_role": outcome_role, "favorite_rank": favorite_rank,
            "event_family": m.get("event_family"),
        }

    def unselected_candidates(self, *, picked_cid: str, T: int,
                              filters: Mapping[str, Any] | None = None,
                              exclude: Sequence[str] = ()) -> list[str]:
        """The counterfactual set: markets tradeable at T matching the scope
        filters, minus the picked market (and any other already-held)."""
        ex = set(exclude) | {picked_cid}
        return [c for c in self.universe_at(T, filters=filters) if c not in ex]
