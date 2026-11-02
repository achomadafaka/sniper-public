"""Typed, versioned MarketState for the memecoin real exit brain.

Pure value types — no DB, no network. See docs/superpowers/specs/2026-07-23-memecoin-real-exit-brain-design.md
(Phase 1). Rules that these types enforce structurally:

- `None` means UNKNOWN and is never `0.0` (known zero). Optionals default to None.
- `executable_price` is None until the fast lane populates a real whole-bag route quote under an
  approved provider — it is NEVER fabricated from a mid plus a haircut.
- Profile-level WARMUP/DATA_INSUFFICIENT/DATA_STALE states are non-actionable, but an explicit
  rule may use its own trusted family subset. SOURCE_UNTRUSTED is always non-actionable.
- Every feature family may carry a `Provenance` (source, event-time window, freshness, coverage,
  quality, feature_version) so any field is explainable from provenance alone.
"""
from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field, fields
from types import MappingProxyType
from typing import Optional


class StateLabel(enum.Enum):
    # actionable lifecycle
    WARMUP = "warmup"                       # not yet enough closed windows -> non-actionable
    TRENDING = "trending"
    WEAKENING = "weakening"
    BREAK_NOMINATED = "break_nominated"
    BREAK_CONFIRMED = "break_confirmed"
    EXIT_PROPOSED = "exit_proposed"
    # non-actionable / fallback
    DATA_INSUFFICIENT = "data_insufficient"
    DATA_STALE = "data_stale"
    SOURCE_UNTRUSTED = "source_untrusted"   # profile not candidate-capable (Solana, 4663-V3-depth)


# Only these authorize the candidate engine to act; everything else -> DEFER_TO_LEGACY.
_ACTIONABLE = frozenset({
    StateLabel.TRENDING, StateLabel.WEAKENING, StateLabel.BREAK_NOMINATED,
    StateLabel.BREAK_CONFIRMED, StateLabel.EXIT_PROPOSED,
})


@dataclass(frozen=True)
class Provenance:
    """Why a feature family may be trusted. Carried alongside the features, never inferred later."""
    source: str                                   # e.g. "pool_ticks", "v2_sync", "marks"
    event_time_window: Optional[tuple] = None     # (start_block_ts, end_block_ts) ISO strings
    freshness_s: Optional[float] = None           # age of the newest input at compute time
    coverage: Optional[float] = None              # fraction of the window with data [0,1]
    quality: str = "untrusted"                    # trusted | degraded | untrusted
    feature_version: str = "v0"
    price_kind: Optional[str] = None              # e.g. "block_vwap_exec_atomic" — NOT a human price


@dataclass(frozen=True)
class Pivot:
    kind: str                                     # "high" | "low"
    block_ts: str
    price: float


@dataclass(frozen=True)
class MarketState:
    """One immutable market-state snapshot for one open position at one materialization tick."""
    # identity & versioning (required)
    chain_id: str
    mint: str
    pool: Optional[str]
    position_id: int
    engine_version: str
    feature_version: str
    state: StateLabel

    # identity (defaulted so early non-actionable returns need not supply it)
    entry_contract_version: Optional[str] = None
    # feature families the profile REQUIRES to be trusted before this state may act (spec §3.1).
    # candidate_capable() checks each one's provenance quality — the state label alone never authorizes.
    required_families: tuple = ()

    # time
    source_event_time: Optional[str] = None
    # Canonical identity of the newest decoded swap that can confirm a structure break. None for
    # provisional/vault-delta-only evidence, which may wake the hot lane but cannot advance CUT
    # hysteresis.
    latest_event_id: Optional[str] = None
    latest_event_time: Optional[str] = None
    latest_event_block_no: Optional[int] = None
    latest_event_log_index: Optional[int] = None
    observed_time: Optional[str] = None
    computed_time: Optional[str] = None
    age_s: Optional[float] = None
    freshness_state: Optional[str] = None

    # price (None = unknown; executable stays None until the fast lane runs)
    nominal_price: Optional[float] = None
    executable_price: Optional[float] = None
    entry_price: Optional[float] = None
    nominal_peak: Optional[float] = None
    executable_peak: Optional[float] = None
    drawdown_from_exec_peak: Optional[float] = None
    drawdown_velocity: Optional[float] = None

    # structure (causal, no lookahead)
    confirmed_pivots: tuple = ()
    prior_high: Optional[float] = None
    prior_low: Optional[float] = None
    higher_low_state: Optional[bool] = None
    lower_high_state: Optional[bool] = None
    confirmation_delay_blocks: Optional[int] = None

    # volatility (per pre-registered window)
    realized_vol: Optional[float] = None

    # flow (EVM, trusted-only; pool-level signed net, dust-only wash)
    buy_n: Optional[int] = None
    sell_n: Optional[int] = None
    buy_vol_quote: Optional[str] = None
    sell_vol_quote: Optional[str] = None
    net_flow_quote: Optional[str] = None
    arrival_rate: Optional[float] = None
    arrival_accel: Optional[float] = None
    wash_filter: Optional[str] = None
    rejected_dust_share: Optional[float] = None

    # depth (V2 only)
    reserve_base: Optional[str] = None
    reserve_quote: Optional[str] = None
    reserve_delta: Optional[str] = None
    depth_pull_flag: Optional[bool] = None
    liquidity_removed_fraction: Optional[float] = None   # WO-3: |quote removed| / pre-burn reserve

    # actor (untrusted by default — routers, not buyers)
    top_actor_concentration: Optional[float] = None

    # provenance per family
    provenance: dict = field(default_factory=dict)

    def __post_init__(self):
        # frozen=True only blocks rebinding the attribute; the dict itself stays mutable. Wrap it in
        # a read-only view and normalize the sequence fields to tuples so the value object is
        # deeply immutable (spec §4). object.__setattr__ is the sanctioned frozen-dataclass escape.
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))
        object.__setattr__(self, "confirmed_pivots", tuple(self.confirmed_pivots))
        object.__setattr__(self, "required_families", tuple(self.required_families))

    def candidate_capable(self, required=None) -> bool:
        """True only when the engine may act on this state FOR THE GIVEN RULE.

        Profile-level and explicit rule-level checks deliberately differ:
        1. `required=None` preserves the profile gate: the lifecycle label must be actionable and
           every profile family must be trusted.
        2. An explicit rule contract is governed by its own trusted families. WARMUP/DATA_STALE/
           DATA_INSUFFICIENT describe the profile, not an independent depth/quote/position rule.
           SOURCE_UNTRUSTED remains a hard global veto.

        WO-1 per-rule evidence contracts: each rule passes its own required subset via `required`
        instead of the profile's full set, so a fresh family (e.g. a decoded LP burn on `depth`) can
        act while an unrelated stale family (quiet-tape swaps on `price`/`flow`/`vol`) cannot veto it.
          - `required=None` -> the profile-level gate (every `self.required_families` trusted). An
            empty profile set is malformed (only forced-legacy profiles have it, and those are never
            actionable) -> fail closed, exactly as before.
          - `required=()` -> a quote/position-only rule that needs no market-data family.
          - `required=(...)` -> just those families must be trusted.

        The label alone never authorizes action — a mislabeled TRENDING with an untrusted required
        family still fails closed here.
        """
        if required is None:
            if self.state not in _ACTIONABLE:
                return False
            required = self.required_families
            if not required:
                return False   # profile-level: actionable with nothing required is malformed
        elif self.state == StateLabel.SOURCE_UNTRUSTED:
            return False
        prov = self.provenance or {}
        # a missing family yields getattr(None, "quality", None) -> None, which is never "trusted";
        # an empty `required` makes all(...) vacuously True -> label-only rules pass.
        return all(getattr(prov.get(f), "quality", None) == "trusted" for f in required)

    def to_dict(self) -> dict:
        """Plain JSON-ready dict. Defined because `dataclasses.asdict` chokes on the immutable
        MappingProxyType provenance — this is the serialization contract Phase 2 persistence uses."""
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        out["state"] = self.state.value
        out["required_families"] = list(self.required_families)
        out["confirmed_pivots"] = [asdict(p) for p in self.confirmed_pivots]
        out["provenance"] = {k: asdict(v) for k, v in self.provenance.items()}
        return out


def market_state_from_dict(d: dict) -> "MarketState":
    """Inverse of `to_dict()` — rebuild a MarketState from its persisted JSON (the exit-state
    tables' `state_json` column). Any key `d` carries that this dataclass doesn't declare is
    ignored (forward-compatible with a newer writer); a missing/invalid `state` label raises, same
    as any other malformed persisted row should never be silently trusted."""
    known = {f.name for f in fields(MarketState)}
    kw = {k: v for k, v in d.items() if k in known}
    kw["state"] = StateLabel(d["state"])
    kw["provenance"] = {
        k: (Provenance(**v) if isinstance(v, dict) else v)
        for k, v in (d.get("provenance") or {}).items()
    }
    kw["confirmed_pivots"] = tuple(
        (Pivot(**p) if isinstance(p, dict) else p) for p in (d.get("confirmed_pivots") or ())
    )
    return MarketState(**kw)
