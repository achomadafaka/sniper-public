# Wallet Protocol — Timing Surface v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explanatory render surface to the wallet protocol report (`## Timing surface`) that classifies a wallet's edge as `holding_driven` / `trading_driven` / `mixed` / `unclear` / `underpowered` from already-attributed closed positions and activity rows, with audit-locked semantics (raw owned-token movement, size-weighted SELL, no asymmetric NO flip, no external lookups, verdict-invariant).

**Architecture:** Pure-compute module (`timing.py`) with no I/O, mirroring the shape of `exit_attribution.py`. Re-uses Exit Attribution v1's per-position categories (PnL-sign-based) so timing categorization cannot drift from attribution categorization. Held-bucket captured-travel uses the universal payout convention (1.0 win / 0.0 loss) — coordinate-independent — to route around `closed_positions[].curPrice`'s unresolved coordinate (audit R2). Pre-resolution captured-move uses size-weighted average SELL with explicit `sell_price_method` tracking; latest-SELL fallback only when SELL `size` is invalid. Plumbed through `WalletData → ProtocolReport.timing_surface → render`. Display-only; verdict-invariant.

**Tech Stack:** Python 3.x, pytest, dataclasses. No new dependencies. Wire only into existing `sniper/analysis/wallet_protocol/` modules.

**Authoritative spec:** `docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md` at commit `d598cf2` (r2). The earlier r1 (commits `5590f9b`, `c12d3cd`) is **stale** — do not regress to it. Concretely, r1 had:
- `n_unmapped_fills > 0` forcing `underpowered` (forbidden in r2)
- `avg_last_sell_move_*` fields (renamed to `avg_sw_sell_move_*` in r2)
- A latest-SELL default (replaced with size-weighted SELL in r2)

**Audit cross-reference:** `docs/superpowers/specs/2026-05-06-wallet-protocol-semantic-audit.md`. Findings R1, R2, R3 are **separate followup tickets** (`2026-05-06-fh6-no-side-flip-fix.md`, `2026-05-06-curprice-coordinate-empirical-resolution.md`, `2026-05-06-stage1-f1-open-dead-split.md`). **Do not implement R1/R2/R3 inside this plan.**

**Plan-review patches (pre-dispatch, applied 2026-05-06):**
1. **Forbidden-phrase contradiction in Task 6 render note removed.** The original phrasing contained the literal string "entered before repricing", which Task 7's regression scan flags. Replaced with "strict market-repricing timing analysis."
2. **`_classify_one_position` now respects `walk_full_history`.** The original derivation `has_full_history = (not activity_cap_hit) or subgraph_fallback_used` ignored `walk_full_history`, so the coverage gate and attribution classifier could disagree. Wrapper now ORs in `walk_full_history` explicitly.
3. **`sw_sell_move` prefers stable token-identity matching when available.** When a position carries an `asset` token id, match SELLs by token id; fall back to `(conditionId, outcome)` only when token identity is absent.

**Noted upstream miss (out-of-scope here, track separately):** `exit_attribution.compute_attribution_summary` and `exit_attribution.attribute_subset` derive `has_full_history` without including `walk_full_history` — same bug pattern as fix #2. Timing Surface routes around this by computing `has_full_history` correctly in its own wrapper, so the surface gets the correct answer; the upstream miss only affects `report.exit_attribution` itself. Flag for a followup ticket (proposed: `2026-05-06-exit-attribution-walk-full-history-coverage-fix.md`) — not blocking Timing Surface v1.

**Hard scope guards (non-negotiable):**
- No `price_history_fn` calls.
- No CLOB `/prices-history` calls.
- No FH6 / H6 path touched.
- No simulator logic.
- No per-grain attribution.
- No verdict logic changes (`overall_verdict`, `gate_result`, `primary_edge_type`, `copyability_note` must be byte-for-byte invariant).
- No analyzer batch runs.
- No replay schema bump.
- No `WalletData` schema change.
- No DB mutation outside test fixtures.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `sniper/analysis/wallet_protocol/timing.py` | **NEW** | Pure-compute module: `held_resolution_travel()`, `sw_sell_move()`, `compute_timing_surface()`. No I/O. |
| `sniper/analysis/wallet_protocol/types.py` | modify | Add `timing_surface: dict \| None = None` to `ProtocolReport`. |
| `sniper/analysis/wallet_protocol/analyze_wallet.py` | modify | Call `compute_timing_surface(...)` after `compute_attribution_summary(...)`. |
| `sniper/analysis/wallet_protocol/report.py` | modify | Add `_timing_surface_block(report)` helper; render `## Timing surface` after `## Edge surface`, before `### Stage 1 result`. |
| `tests/test_wallet_protocol_timing_surface.py` | **NEW** | All Timing Surface v1 tests (T1–T5 audit oracles + structural cases). Mirrors layout of `tests/test_wallet_protocol_completeness_surface.py`. |

---

## Task 1: Bootstrap `timing.py` and lock the raw-owned-token movement rule (`held_resolution_travel`)

**Files:**
- Create: `sniper/analysis/wallet_protocol/timing.py`
- Create: `tests/test_wallet_protocol_timing_surface.py`
- Test: `tests/test_wallet_protocol_timing_surface.py::test_held_resolution_travel_yes_win` (and 5 sibling tests)

**What this task locks:** The audit's raw owned-token rule for the held-bucket captured-travel formula. The formula is label-agnostic (works the same for YES, NO, OTHER, Trump, England, Over, Under, anything categorical) because it uses universal payouts (1.0 win / 0.0 loss), not `curPrice`.

- [ ] **Step 1: Create `tests/test_wallet_protocol_timing_surface.py` with the failing test for held YES win**

```python
"""Timing Surface v1 — captured-travel-only timing classification.

Spec: docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md
      (commit d598cf2, r2)
Audit: docs/superpowers/specs/2026-05-06-wallet-protocol-semantic-audit.md

Test layout mirrors tests/test_wallet_protocol_completeness_surface.py.
"""
from __future__ import annotations

import pytest

from sniper.analysis.wallet_protocol.timing import held_resolution_travel


# ---------------------------------------------------------------------------
# Section A — held_resolution_travel: raw-token rule, label-agnostic
# ---------------------------------------------------------------------------


def test_held_resolution_travel_yes_win():
    """YES bought at 0.30 resolved at 1.0 → captured travel +0.70."""
    pos = {"outcome": "Yes", "avgPrice": 0.30, "curPrice": 1.0,
           "realizedPnl": 70.0, "size": 100, "conditionId": "cid_a"}
    travel = held_resolution_travel(pos, "held_to_resolution_win")
    assert travel == pytest.approx(0.70)
```

- [ ] **Step 2: Run the test and verify it fails with ImportError**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py::test_held_resolution_travel_yes_win -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sniper.analysis.wallet_protocol.timing'`.

- [ ] **Step 3: Create `sniper/analysis/wallet_protocol/timing.py` with the minimal helper**

```python
"""Timing Surface v1 — captured-travel timing classification.

Pure-compute module mirroring `exit_attribution.py`. No I/O, no external
HTTP calls, no `price_history_fn` calls. Display-only; consumers MUST
NOT feed any of these results into verdict, gate, or primary_edge_type
logic.

Spec:  docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md
       (commit d598cf2, r2)
Audit: docs/superpowers/specs/2026-05-06-wallet-protocol-semantic-audit.md

Hard rules:
- Raw owned-token movement: favorable = later_price - entry_price for
  any outcome (YES, NO, OTHER, categorical). NO asymmetric flip for
  NO/Short. The audit's R1 finding documents the inverse mistake in
  flipper.py FH6.
- Held-bucket captured-travel uses universal payout (resolved token
  redeems at $1 win / $0 loss); coordinate-independent. Avoids the
  audit R2 unresolved coordinate question on closed_positions.curPrice.
- Pre-resolution captured-move uses size-weighted average SELL by
  default. Latest-SELL fallback ONLY when SELL `size` is invalid;
  per-position `sell_price_method` records which path ran.
- `n_unmapped_fills > 0` does NOT alone force `underpowered`. It fires
  the SUBGRAPH_UNMAPPED_FILLS caveat and drops `confidence_band` to
  "low" but the timing classification stands.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal


AttributionCategory = Literal[
    "held_to_resolution_win",
    "held_to_resolution_loss",
    "pre_resolution_profitable_exit",
    "pre_resolution_loss_cut",
    "unresolved_or_unknown",
    "insufficient_data",
]


def held_resolution_travel(
    position: Mapping,
    attribution_category: AttributionCategory,
) -> float:
    """Side-adjustment-free held-bucket captured-travel.

    Universal payout convention: a held token that wins resolves to $1;
    a held token that loses resolves to $0. This is true for YES, NO,
    OTHER, and any categorical outcome label, because resolution
    payouts are universal — not coordinate-dependent.

    + value = wallet captured price travel toward win.
    - value = wallet lost the entry premium.

    Args:
        position: a /closed-positions row (`avgPrice` is the held token's
            entry price per ADR-003).
        attribution_category: must be either "held_to_resolution_win"
            or "held_to_resolution_loss" (the only two held-bucket
            categories from exit_attribution).

    Raises:
        ValueError: when called with a non-held-bucket category.
    """
    entry = float(position.get("avgPrice") or 0.0)
    if attribution_category == "held_to_resolution_win":
        return 1.0 - entry        # token redeemed at $1
    if attribution_category == "held_to_resolution_loss":
        return -entry             # token redeemed at $0
    raise ValueError(
        f"held_resolution_travel called with non-held category: "
        f"{attribution_category!r}"
    )


__all__ = [
    "AttributionCategory",
    "held_resolution_travel",
]
```

- [ ] **Step 4: Run the test and verify it passes**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py::test_held_resolution_travel_yes_win -v`
Expected: PASS.

- [ ] **Step 5: Add the five remaining held_resolution_travel tests (NO win, OTHER win, YES loss, NO loss, invalid category)**

Append to `tests/test_wallet_protocol_timing_surface.py`:

```python
def test_held_resolution_travel_no_win_locks_audit_r1_pattern():
    """NO bought at 0.40, NO won → universal payout: 1.0 - 0.40 = +0.60.

    Locks audit-R1's NO-token sign correctness. A buggy implementation
    that flipped the NO side (entry - later or similar) would yield
    -0.60 here.
    """
    pos = {"outcome": "No", "avgPrice": 0.40, "curPrice": 0.0,
           "realizedPnl": 60.0, "size": 100, "conditionId": "cid_a"}
    travel = held_resolution_travel(pos, "held_to_resolution_win")
    assert travel == pytest.approx(0.60)


def test_held_resolution_travel_other_categorical_win():
    """Categorical winning outcome (Trump bought at 0.30 → wins) gets
    the same +0.70 favorable travel as a YES bought at 0.30. The rule
    is label-agnostic.
    """
    pos = {"outcome": "Trump", "avgPrice": 0.30, "curPrice": 1.0,
           "realizedPnl": 70.0, "size": 100, "conditionId": "cid_x"}
    travel = held_resolution_travel(pos, "held_to_resolution_win")
    assert travel == pytest.approx(0.70)


def test_held_resolution_travel_yes_loss():
    """YES bought at 0.30, lost → -0.30 (lost entry premium)."""
    pos = {"outcome": "Yes", "avgPrice": 0.30, "curPrice": 0.0,
           "realizedPnl": -30.0, "size": 100, "conditionId": "cid_a"}
    travel = held_resolution_travel(pos, "held_to_resolution_loss")
    assert travel == pytest.approx(-0.30)


def test_held_resolution_travel_no_loss():
    """NO bought at 0.40, lost (YES won, NO token redeems at $0) → -0.40."""
    pos = {"outcome": "No", "avgPrice": 0.40, "curPrice": 1.0,
           "realizedPnl": -40.0, "size": 100, "conditionId": "cid_a"}
    travel = held_resolution_travel(pos, "held_to_resolution_loss")
    assert travel == pytest.approx(-0.40)


def test_held_resolution_travel_rejects_non_held_category():
    pos = {"outcome": "Yes", "avgPrice": 0.30, "curPrice": 1.0}
    with pytest.raises(ValueError, match="non-held category"):
        held_resolution_travel(pos, "pre_resolution_profitable_exit")
    with pytest.raises(ValueError, match="non-held category"):
        held_resolution_travel(pos, "insufficient_data")
```

- [ ] **Step 6: Run all six held_resolution_travel tests and verify they pass**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py -k held_resolution_travel -v`
Expected: 6 passed.

- [ ] **Step 7: Commit**

```bash
git add sniper/analysis/wallet_protocol/timing.py tests/test_wallet_protocol_timing_surface.py
git commit -m "$(cat <<'EOF'
feat(wallet-protocol): Timing Surface v1 — held_resolution_travel helper

Pure-compute helper that locks the audit's raw-owned-token rule for
the held bucket. Universal payout convention (held token redeems at
$1 win / $0 loss) avoids any dependency on closed_positions.curPrice
coordinate (audit R2 unresolved). Label-agnostic — works the same for
YES, NO, Trump, England, Over, Under, any categorical outcome.

Six unit tests cover YES/NO/OTHER wins + YES/NO losses + invalid-
category guard. The NO win test (avgPrice=0.40, curPrice=0.0,
realizedPnl > 0 → +0.60) explicitly locks the audit-R1 sign-correctness
contract.

Spec: docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md
      (commit d598cf2, r2)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Size-weighted SELL helper (`sw_sell_move`) + audit oracles T1–T3

**Files:**
- Modify: `sniper/analysis/wallet_protocol/timing.py`
- Modify: `tests/test_wallet_protocol_timing_surface.py`

**What this task locks:** Audit oracles T1 (NO-token raw movement sign), T2 (categorical / OTHER raw movement sign), T3 (size-weighted SELL beats dust-close). Plus the latest-SELL fallback path and the unavailable path.

- [ ] **Step 1: Add the failing T1 test (NO-token raw movement sign)**

Append to `tests/test_wallet_protocol_timing_surface.py`:

```python
# ---------------------------------------------------------------------------
# Section B — sw_sell_move: size-weighted SELL with explicit method tracking
# ---------------------------------------------------------------------------


from sniper.analysis.wallet_protocol.timing import sw_sell_move


def test_t1_sw_sell_move_no_token_raw_movement_sign():
    """T1 (audit oracle): NO bought at 0.40, sold at 0.65 → +0.25 favorable.

    Locks the raw-owned-token rule: long-token favorable = later − entry,
    no asymmetric NO flip.
    """
    pos = {"conditionId": "cid_a", "outcome": "No",
           "avgPrice": 0.40, "realizedPnl": 25.0}
    activity = [
        {"side": "BUY",  "conditionId": "cid_a", "outcome": "No",
         "price": 0.40, "timestamp": 1000, "size": 100, "asset": "tok_no"},
        {"side": "SELL", "conditionId": "cid_a", "outcome": "No",
         "price": 0.65, "timestamp": 2000, "size": 100, "asset": "tok_no"},
    ]
    result = sw_sell_move(pos, activity)
    assert result.sw_move == pytest.approx(0.25)
    assert result.sell_price_method == "size_weighted"
    assert result.weighted_sell_price == pytest.approx(0.65)
    assert result.last_sell_price == pytest.approx(0.65)
    assert result.n_sell_fills == 1
```

- [ ] **Step 2: Run the test and verify it fails with ImportError**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py::test_t1_sw_sell_move_no_token_raw_movement_sign -v`
Expected: FAIL with `ImportError: cannot import name 'sw_sell_move'`.

- [ ] **Step 3: Add `sw_sell_move()` and its result NamedTuple to `timing.py`**

Append to `sniper/analysis/wallet_protocol/timing.py` (above `__all__`):

```python
from collections.abc import Sequence
from typing import NamedTuple


class SwSellMoveResult(NamedTuple):
    """Per-position size-weighted SELL move outcome.

    `sw_move` is None when no SELL events exist for the (cid, outcome).
    `sell_price_method` is one of:
      - "size_weighted":         all SELLs had size > 0; sw_move uses
                                  the size-weighted average.
      - "latest_sell_fallback":  at least one SELL had missing/invalid
                                  size; sw_move uses the latest SELL.
      - "unavailable":           no SELL events for (cid, outcome).
    """
    sw_move: float | None
    sell_price_method: Literal["size_weighted", "latest_sell_fallback", "unavailable"]
    weighted_sell_price: float | None
    last_sell_price: float | None
    n_sell_fills: int


def _matching_sells(
    position: Mapping,
    activity_rows: Sequence[Mapping],
) -> list[Mapping]:
    """Return SELL rows for this position. Prefer stable token identity
    when both sides expose `asset`; fall back to (conditionId, outcome)
    only when the position has no asset id.

    This guards against the same class of label-based mismatch the
    audit flagged (an outcome string is not a stable identity in the
    way a CLOB token id is). Polymarket maps (cid, outcome) → asset
    1:1 today, but matching by asset is strictly safer when both sides
    carry the field.
    """
    pos_asset = str(position.get("asset") or "").strip()
    if pos_asset:
        return [
            r for r in activity_rows
            if (r.get("side") or "").upper() == "SELL"
            and str(r.get("asset") or "").strip() == pos_asset
        ]
    cid = position.get("conditionId") or ""
    outcome = position.get("outcome") or ""
    return [
        r for r in activity_rows
        if (r.get("side") or "").upper() == "SELL"
        and r.get("conditionId") == cid
        and (r.get("outcome") or "") == outcome
    ]


def sw_sell_move(
    position: Mapping,
    activity_rows: Sequence[Mapping],
) -> SwSellMoveResult:
    """Size-weighted captured move from entry to SELL price.

    Raw owned-token rule: favorable = later_price − entry_price.
    Both prices live in held-token coordinate (`avgPrice` per ADR-003;
    `activity_rows[].price` per fetch.py:120 = usdc_amt / outcome_amt).
    Label-agnostic — applies to YES, NO, OTHER, and categorical outcomes.

    Match path: prefer stable token identity (`asset` token id) when
    both position and activity rows carry it; fall back to
    (conditionId, outcome) only when the position has no asset id.

    Default value path: size-weighted average over ALL SELLs.
    Fallback value path (`latest_sell_fallback`): triggered when ANY
    SELL row has missing/invalid `size`; falls back to the latest SELL
    by timestamp. Ties on timestamp are broken by input order via
    stable max().
    """
    entry = float(position.get("avgPrice") or 0.0)
    sells = _matching_sells(position, activity_rows)
    n_sell_fills = len(sells)
    if n_sell_fills == 0:
        return SwSellMoveResult(
            sw_move=None,
            sell_price_method="unavailable",
            weighted_sell_price=None,
            last_sell_price=None,
            n_sell_fills=0,
        )

    last = max(sells, key=lambda r: int(r.get("timestamp") or 0))
    last_sell_price = float(last.get("price") or 0.0)

    # Size-weighted path requires every SELL to carry positive size.
    sizes = [float(r.get("size") or 0.0) for r in sells]
    if all(s > 0 for s in sizes):
        prices = [float(r.get("price") or 0.0) for r in sells]
        weight_sum = sum(sizes)
        weighted_sell_price = (
            sum(p * s for p, s in zip(prices, sizes)) / weight_sum
        )
        return SwSellMoveResult(
            sw_move=weighted_sell_price - entry,
            sell_price_method="size_weighted",
            weighted_sell_price=weighted_sell_price,
            last_sell_price=last_sell_price,
            n_sell_fills=n_sell_fills,
        )

    # Fallback: at least one SELL had invalid size.
    return SwSellMoveResult(
        sw_move=last_sell_price - entry,
        sell_price_method="latest_sell_fallback",
        weighted_sell_price=None,
        last_sell_price=last_sell_price,
        n_sell_fills=n_sell_fills,
    )
```

Update the `__all__` list:

```python
__all__ = [
    "AttributionCategory",
    "SwSellMoveResult",
    "held_resolution_travel",
    "sw_sell_move",
]
```

- [ ] **Step 4: Run T1 and verify it passes**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py::test_t1_sw_sell_move_no_token_raw_movement_sign -v`
Expected: PASS.

- [ ] **Step 5: Add T2, T3, latest-SELL-fallback, and unavailable tests**

Append to `tests/test_wallet_protocol_timing_surface.py`:

```python
def test_t2_sw_sell_move_categorical_outcome_label_agnostic():
    """T2 (audit oracle): outcome="Trump" bought at 0.30, sold at 0.55
    → +0.25 favorable. Same rule as YES/NO; label-agnostic."""
    pos = {"conditionId": "cid_x", "outcome": "Trump",
           "avgPrice": 0.30, "realizedPnl": 25.0}
    activity = [
        {"side": "BUY",  "conditionId": "cid_x", "outcome": "Trump",
         "price": 0.30, "timestamp": 1000, "size": 100, "asset": "tok_trump"},
        {"side": "SELL", "conditionId": "cid_x", "outcome": "Trump",
         "price": 0.55, "timestamp": 2000, "size": 100, "asset": "tok_trump"},
    ]
    result = sw_sell_move(pos, activity)
    assert result.sw_move == pytest.approx(0.25)
    assert result.sell_price_method == "size_weighted"


def test_t3_sw_sell_move_dust_close_does_not_dominate():
    """T3 (audit oracle): BUY 100 NO at 0.40. SELL 99 at 0.65, then
    SELL 1 at 0.05.

    Latest-SELL would have given last_sell_price=0.05 → sw_move=−0.35.
    Size-weighted gives weighted ≈ 0.6440 → sw_move ≈ +0.2440.

    The size-weighted path must run (all SELLs have valid size) and the
    dust SELL must NOT dominate.
    """
    pos = {"conditionId": "cid_a", "outcome": "No",
           "avgPrice": 0.40, "realizedPnl": 24.40}
    activity = [
        {"side": "BUY",  "conditionId": "cid_a", "outcome": "No",
         "price": 0.40, "timestamp": 1000, "size": 100, "asset": "tok_no"},
        {"side": "SELL", "conditionId": "cid_a", "outcome": "No",
         "price": 0.65, "timestamp": 2000, "size": 99,  "asset": "tok_no"},
        {"side": "SELL", "conditionId": "cid_a", "outcome": "No",
         "price": 0.05, "timestamp": 3000, "size": 1,   "asset": "tok_no"},
    ]
    result = sw_sell_move(pos, activity)
    assert result.sell_price_method == "size_weighted"
    # (0.65*99 + 0.05*1) / 100 = 64.40/100 = 0.6440
    assert result.weighted_sell_price == pytest.approx(0.6440, abs=1e-4)
    assert result.sw_move == pytest.approx(0.2440, abs=1e-4)
    # And explicitly not the dust value.
    assert result.last_sell_price == pytest.approx(0.05)
    assert result.n_sell_fills == 2


def test_sw_sell_move_falls_back_to_latest_when_size_invalid():
    """When ANY SELL has missing/zero size, fall back to latest SELL
    by timestamp and stamp sell_price_method="latest_sell_fallback"."""
    pos = {"conditionId": "cid_a", "outcome": "Yes",
           "avgPrice": 0.30, "realizedPnl": 10.0}
    activity = [
        {"side": "BUY",  "conditionId": "cid_a", "outcome": "Yes",
         "price": 0.30, "timestamp": 1000, "size": 100, "asset": "tok_yes"},
        {"side": "SELL", "conditionId": "cid_a", "outcome": "Yes",
         "price": 0.40, "timestamp": 2000, "size": 0,   "asset": "tok_yes"},
        {"side": "SELL", "conditionId": "cid_a", "outcome": "Yes",
         "price": 0.45, "timestamp": 3000, "size": 50,  "asset": "tok_yes"},
    ]
    result = sw_sell_move(pos, activity)
    assert result.sell_price_method == "latest_sell_fallback"
    assert result.weighted_sell_price is None
    assert result.last_sell_price == pytest.approx(0.45)
    assert result.sw_move == pytest.approx(0.45 - 0.30)
    assert result.n_sell_fills == 2


def test_sw_sell_move_unavailable_when_no_sells():
    """No SELL events for (cid, outcome) → unavailable, sw_move=None.

    Won't normally happen for pre_resolution_* categories (those by
    definition have a SELL match), but the helper is robust.
    """
    pos = {"conditionId": "cid_a", "outcome": "Yes",
           "avgPrice": 0.30, "realizedPnl": 0.0}
    activity = [
        {"side": "BUY",  "conditionId": "cid_a", "outcome": "Yes",
         "price": 0.30, "timestamp": 1000, "size": 100, "asset": "tok_yes"},
    ]
    result = sw_sell_move(pos, activity)
    assert result.sw_move is None
    assert result.sell_price_method == "unavailable"
    assert result.weighted_sell_price is None
    assert result.last_sell_price is None
    assert result.n_sell_fills == 0


def test_sw_sell_move_filters_by_cid_and_outcome():
    """Activity for a different (cid, outcome) must not be matched
    when the position has no asset id (fallback path)."""
    pos = {"conditionId": "cid_a", "outcome": "No",
           "avgPrice": 0.40, "realizedPnl": 0.0}    # no `asset` on pos
    activity = [
        # Wrong outcome
        {"side": "SELL", "conditionId": "cid_a", "outcome": "Yes",
         "price": 0.65, "timestamp": 2000, "size": 100, "asset": "tok_yes"},
        # Wrong cid
        {"side": "SELL", "conditionId": "cid_b", "outcome": "No",
         "price": 0.65, "timestamp": 2000, "size": 100, "asset": "tok_no"},
    ]
    result = sw_sell_move(pos, activity)
    assert result.sell_price_method == "unavailable"
    assert result.sw_move is None


def test_sw_sell_move_prefers_token_identity_when_position_has_asset():
    """When position carries `asset`, match SELLs by asset token id —
    NOT by (cid, outcome) label. Locks the audit's preference for
    stable identity over label-based matching."""
    pos = {
        "conditionId": "cid_a", "outcome": "No",
        "asset": "tok_no_a",                     # stable token identity
        "avgPrice": 0.40, "realizedPnl": 25.0,
    }
    activity = [
        # Same (cid, outcome) but DIFFERENT token id — must NOT match.
        {"side": "SELL", "conditionId": "cid_a", "outcome": "No",
         "price": 0.05, "timestamp": 2000, "size": 100, "asset": "tok_no_b"},
        # Matching token id — this is the SELL we want.
        {"side": "SELL", "conditionId": "cid_a", "outcome": "No",
         "price": 0.65, "timestamp": 3000, "size": 100, "asset": "tok_no_a"},
    ]
    result = sw_sell_move(pos, activity)
    assert result.sell_price_method == "size_weighted"
    assert result.weighted_sell_price == pytest.approx(0.65)
    assert result.sw_move == pytest.approx(0.25)
    assert result.n_sell_fills == 1


def test_sw_sell_move_falls_back_to_cid_outcome_when_position_has_no_asset():
    """When position has no `asset`, fall back to (cid, outcome) match."""
    pos = {"conditionId": "cid_a", "outcome": "No",
           "avgPrice": 0.40, "realizedPnl": 25.0}
    activity = [
        {"side": "SELL", "conditionId": "cid_a", "outcome": "No",
         "price": 0.65, "timestamp": 2000, "size": 100, "asset": "tok_no_a"},
    ]
    result = sw_sell_move(pos, activity)
    assert result.sell_price_method == "size_weighted"
    assert result.sw_move == pytest.approx(0.25)
```

- [ ] **Step 6: Run all sw_sell_move tests and verify they pass**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py -k sw_sell_move -v`
Expected: 8 passed (T1, T2, T3, fallback, unavailable, cid/outcome filter, token-identity-preferred, no-asset-fallback).

- [ ] **Step 7: Commit**

```bash
git add sniper/analysis/wallet_protocol/timing.py tests/test_wallet_protocol_timing_surface.py
git commit -m "$(cat <<'EOF'
feat(wallet-protocol): Timing Surface v1 — sw_sell_move helper + audit T1-T3

Pure-compute helper for size-weighted SELL move per (cid, outcome).
Default path: size-weighted average across all SELLs (every SELL must
carry positive size). Fallback path: latest SELL by timestamp,
triggered when any SELL has missing/invalid size; explicit
sell_price_method tag distinguishes the two. Unavailable path when no
SELL events exist.

Audit oracles locked:
  T1 — NO bought at 0.40, sold at 0.65 → sw_move=+0.25 (raw-token rule,
       no asymmetric NO flip).
  T2 — outcome="Trump" bought at 0.30, sold at 0.55 → sw_move=+0.25
       (label-agnostic; same rule as YES/NO).
  T3 — BUY 100 at 0.40, SELL 99@0.65, SELL 1@0.05 → weighted ≈ 0.6440,
       sw_move ≈ +0.2440. Dust close does NOT dominate; method
       remains "size_weighted".

Plus tests for: latest-SELL fallback (size invalid), unavailable
(no SELL match), (cid, outcome) filtering correctness when position
has no asset, token-identity-preferred match when position has
`asset` (different asset id with same cid+outcome must NOT match),
and (cid, outcome) fallback when position has no asset id.

Spec: docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md
      (commit d598cf2, r2 §6.1)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Aggregator `compute_timing_surface` — classification + gating + future-reserved fields

**Files:**
- Modify: `sniper/analysis/wallet_protocol/timing.py`
- Modify: `tests/test_wallet_protocol_timing_surface.py`

**What this task locks:** The classification rule (§5 of the r2 spec), the `force_underpowered` / `coverage_caveats` / `confidence_band` / `confidence_downgraded` split (audit-required), the future-reserved fields (always literal `false` / `null` in v1), and the dominant-rule-before-mixed ordering.

- [ ] **Step 1: Add the failing classification-skeleton test (None on empty input)**

Append to `tests/test_wallet_protocol_timing_surface.py`:

```python
# ---------------------------------------------------------------------------
# Section C — compute_timing_surface: classification + gating + reserved fields
# ---------------------------------------------------------------------------


from sniper.analysis.wallet_protocol.timing import compute_timing_surface


def _build_held_win_pos(*, cid, avg_price=0.40, realized_pnl=60.0,
                       outcome="Yes", ts=1000):
    """Held-to-resolution win fixture (curPrice at resolution, no SELL match)."""
    return {
        "conditionId": cid, "outcome": outcome,
        "avgPrice": avg_price, "curPrice": 1.0,
        "realizedPnl": realized_pnl, "size": 100,
        "timestamp": ts, "eventSlug": f"slug-{cid}",
    }


def _build_held_loss_pos(*, cid, avg_price=0.40, realized_pnl=-40.0,
                        outcome="Yes", ts=1000):
    """Held-to-resolution loss fixture."""
    return {
        "conditionId": cid, "outcome": outcome,
        "avgPrice": avg_price, "curPrice": 0.0,
        "realizedPnl": realized_pnl, "size": 100,
        "timestamp": ts, "eventSlug": f"slug-{cid}",
    }


def _build_preres_profitable_pos(*, cid, avg_price=0.40,
                                 sell_price=0.60, realized_pnl=20.0,
                                 outcome="Yes", buy_ts=1000, sell_ts=1500):
    """Position fixture + matching activity rows for a profitable pre-res exit."""
    pos = {
        "conditionId": cid, "outcome": outcome,
        "avgPrice": avg_price, "curPrice": sell_price,    # not at resolution
        "realizedPnl": realized_pnl, "size": 100,
        "timestamp": buy_ts, "eventSlug": f"slug-{cid}",
    }
    activity = [
        {"side": "BUY",  "conditionId": cid, "outcome": outcome,
         "price": avg_price, "timestamp": buy_ts, "size": 100,
         "asset": f"tok_{cid}"},
        {"side": "SELL", "conditionId": cid, "outcome": outcome,
         "price": sell_price, "timestamp": sell_ts, "size": 100,
         "asset": f"tok_{cid}"},
    ]
    return pos, activity


def _build_preres_loss_cut_pos(*, cid, avg_price=0.40,
                               sell_price=0.20, realized_pnl=-20.0,
                               outcome="Yes", buy_ts=1000, sell_ts=1500):
    pos = {
        "conditionId": cid, "outcome": outcome,
        "avgPrice": avg_price, "curPrice": sell_price,
        "realizedPnl": realized_pnl, "size": 100,
        "timestamp": buy_ts, "eventSlug": f"slug-{cid}",
    }
    activity = [
        {"side": "BUY",  "conditionId": cid, "outcome": outcome,
         "price": avg_price, "timestamp": buy_ts, "size": 100,
         "asset": f"tok_{cid}"},
        {"side": "SELL", "conditionId": cid, "outcome": outcome,
         "price": sell_price, "timestamp": sell_ts, "size": 100,
         "asset": f"tok_{cid}"},
    ]
    return pos, activity


def test_compute_timing_surface_returns_none_on_empty_input():
    """Mirrors exit_attribution.compute_attribution_summary's contract."""
    result = compute_timing_surface(
        closed_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        subgraph_fallback_used=False,
        walk_full_history=False,
        n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert result is None
```

- [ ] **Step 2: Run the test and verify it fails with ImportError**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py::test_compute_timing_surface_returns_none_on_empty_input -v`
Expected: FAIL with `ImportError: cannot import name 'compute_timing_surface'`.

- [ ] **Step 3: Add `compute_timing_surface()` to `timing.py`**

Append to `sniper/analysis/wallet_protocol/timing.py` (above `__all__`):

```python
# ---------------------------------------------------------------------------
# Aggregator: compute_timing_surface
# ---------------------------------------------------------------------------

# r2 §5 thresholds — locked.
_N_ATTRIBUTED_FLOOR = 20
_DOMINANT_SHARE_THRESHOLD = 0.70
_MIXED_MIN_PER_BUCKET = 5

ClassificationLabel = Literal[
    "holding_driven",
    "trading_driven",
    "mixed",
    "unclear",
    "underpowered",
]

ConfidenceBand = Literal["high", "low", "underpowered"]


def _classify_one_position(
    position: Mapping,
    activity_rows: Sequence[Mapping],
    *,
    activity_cap_hit: bool,
    subgraph_fallback_used: bool,
    walk_full_history: bool,
) -> AttributionCategory:
    """Re-categorize one closed position via exit_attribution.classify_position.

    `has_full_history` MUST OR in `walk_full_history` so the timing
    categorizer agrees with the coverage gate (which suppresses the
    ACTIVITY_TRUNCATED caveat when walk_full_history is True).

    Note: upstream `exit_attribution.compute_attribution_summary` does
    NOT currently OR in walk_full_history (audit followup tracked
    separately as `2026-05-06-exit-attribution-walk-full-history-
    coverage-fix.md`). Timing Surface fixes the derivation locally so
    this surface is correct independent of that upstream patch.
    """
    # Local import to keep timing.py free of cycles at module load.
    from sniper.analysis.wallet_protocol.exit_attribution import (
        _activity_min_ts, classify_position,
    )
    activity_min_ts = _activity_min_ts(activity_rows)
    has_full_history = (
        (not activity_cap_hit)
        or subgraph_fallback_used
        or walk_full_history
    )
    return classify_position(
        position, activity_rows,
        activity_min_ts=activity_min_ts,
        has_full_history=has_full_history,
    )


def compute_timing_surface(
    closed_positions: Sequence[Mapping],
    activity_rows: Sequence[Mapping],
    *,
    activity_cap_hit: bool,
    subgraph_fallback_used: bool,
    walk_full_history: bool,
    n_unmapped_fills: int,
    attribution_coverage_low: bool,
) -> dict | None:
    """Aggregate per-position attribution into a captured-travel timing
    surface. See spec §5–§6 (commit d598cf2, r2).

    Returns None when `closed_positions` is empty (mirrors
    exit_attribution.compute_attribution_summary).

    Display-only — NEVER feeds verdict, gate, or primary_edge_type.
    """
    if not closed_positions:
        return None

    # Per-position categorization + sw_sell_move calculation.
    held_wins: list[Mapping] = []
    held_losses: list[Mapping] = []
    pre_res_winners: list[tuple[Mapping, SwSellMoveResult]] = []
    pre_res_losers: list[tuple[Mapping, SwSellMoveResult]] = []
    n_unresolved = 0
    n_insufficient = 0

    sell_method_counts = {
        "size_weighted": 0,
        "latest_sell_fallback": 0,
        "unavailable": 0,
    }

    for pos in closed_positions:
        cat = _classify_one_position(
            pos, activity_rows,
            activity_cap_hit=activity_cap_hit,
            subgraph_fallback_used=subgraph_fallback_used,
            walk_full_history=walk_full_history,
        )
        if cat == "held_to_resolution_win":
            held_wins.append(pos)
        elif cat == "held_to_resolution_loss":
            held_losses.append(pos)
        elif cat == "pre_resolution_profitable_exit":
            sm = sw_sell_move(pos, activity_rows)
            sell_method_counts[sm.sell_price_method] += 1
            pre_res_winners.append((pos, sm))
        elif cat == "pre_resolution_loss_cut":
            sm = sw_sell_move(pos, activity_rows)
            sell_method_counts[sm.sell_price_method] += 1
            pre_res_losers.append((pos, sm))
        elif cat == "unresolved_or_unknown":
            n_unresolved += 1
        elif cat == "insufficient_data":
            n_insufficient += 1

    # Counts.
    held_n = len(held_wins) + len(held_losses)
    preres_n = len(pre_res_winners) + len(pre_res_losers)
    n_total_closed = len(closed_positions)
    n_attributed = held_n + preres_n + n_unresolved   # excludes insufficient_data

    held_pnl = sum(float(p.get("realizedPnl") or 0.0) for p in held_wins + held_losses)
    preres_pnl = sum(
        float(p.get("realizedPnl") or 0.0)
        for p, _ in pre_res_winners + pre_res_losers
    )
    held_share = (held_n / n_attributed) if n_attributed > 0 else 0.0
    preres_share = (preres_n / n_attributed) if n_attributed > 0 else 0.0

    # r2 §5 — gating + caveats + confidence band split.
    activity_truncated = (
        activity_cap_hit and not (subgraph_fallback_used or walk_full_history)
    )
    force_underpowered = (
        n_attributed < _N_ATTRIBUTED_FLOOR
        or attribution_coverage_low
        or activity_truncated
    )
    coverage_caveats: list[str] = []
    if activity_truncated:
        coverage_caveats.append("ACTIVITY_TRUNCATED")
    if n_unmapped_fills > 0:
        coverage_caveats.append("SUBGRAPH_UNMAPPED_FILLS")
    if attribution_coverage_low:
        coverage_caveats.append("ATTRIBUTION_COVERAGE_LOW")

    if force_underpowered:
        confidence_band: ConfidenceBand = "underpowered"
    elif coverage_caveats:
        confidence_band = "low"
    else:
        confidence_band = "high"
    confidence_downgraded = (confidence_band == "low")

    # Back-compat: True only when force_underpowered AND the cause was a
    # coverage caveat (not just n_attributed < floor).
    coverage_downgraded = force_underpowered and (
        attribution_coverage_low or activity_truncated
    )

    # r2 §5 — classification rule. Dominant rules MUST evaluate before mixed.
    if force_underpowered:
        classification: ClassificationLabel = "underpowered"
        if n_attributed < _N_ATTRIBUTED_FLOOR:
            reason = f"n_attributed={n_attributed} below floor {_N_ATTRIBUTED_FLOOR}"
        else:
            reason = f"coverage caveats: {','.join(coverage_caveats)}"
    elif held_share >= _DOMINANT_SHARE_THRESHOLD and held_pnl > 0:
        classification = "holding_driven"
        reason = f"held_share={held_share:.2f}, held_pnl=${held_pnl:.2f}"
    elif preres_share >= _DOMINANT_SHARE_THRESHOLD and preres_pnl > 0:
        classification = "trading_driven"
        reason = f"preres_share={preres_share:.2f}, preres_pnl=${preres_pnl:.2f}"
    elif (held_pnl > 0 and preres_pnl > 0
          and held_n >= _MIXED_MIN_PER_BUCKET
          and preres_n >= _MIXED_MIN_PER_BUCKET):
        classification = "mixed"
        reason = (
            f"held_n={held_n}, preres_n={preres_n}, both buckets profitable"
        )
    else:
        classification = "unclear"
        reason = "neither bucket dominates and no clear positive concentration"

    # Bucket aggregates.
    held_to_resolution = _build_held_bucket(held_wins, held_losses)
    pre_resolution = _build_preres_bucket(pre_res_winners, pre_res_losers)
    pre_resolution["sell_price_method_counts"] = sell_method_counts

    return {
        "classification": classification,
        "classification_reason": reason,
        "confidence_band": confidence_band,
        "confidence_downgraded": confidence_downgraded,
        "coverage_downgraded": coverage_downgraded,
        "coverage_caveats": coverage_caveats,
        "n_attributed": n_attributed,
        "n_total_closed": n_total_closed,
        "held_to_resolution": held_to_resolution,
        "pre_resolution": pre_resolution,
        # Future strict-repricing reservation (v1.5 / H6). NEVER populated
        # in v1 — see spec §10 + audit followup ticket
        # 2026-05-06-fh6-no-side-flip-fix.md.
        "future_repricing_available": False,
        "future_repricing_signal": None,
        "future_repricing_window": None,
        "price_history_source": None,
    }


def _build_held_bucket(
    held_wins: Sequence[Mapping],
    held_losses: Sequence[Mapping],
) -> dict:
    n = len(held_wins) + len(held_losses)
    if n == 0:
        return {
            "n": 0, "wins": 0, "losses": 0,
            "win_rate": None, "total_pnl": 0.0,
            "avg_captured_travel": None,
        }
    travels = (
        [held_resolution_travel(p, "held_to_resolution_win") for p in held_wins]
        + [held_resolution_travel(p, "held_to_resolution_loss") for p in held_losses]
    )
    pnls = [
        float(p.get("realizedPnl") or 0.0)
        for p in list(held_wins) + list(held_losses)
    ]
    return {
        "n": n,
        "wins": len(held_wins),
        "losses": len(held_losses),
        "win_rate": len(held_wins) / n,
        "total_pnl": float(sum(pnls)),
        "avg_captured_travel": sum(travels) / len(travels),
    }


def _build_preres_bucket(
    winners: Sequence[tuple[Mapping, SwSellMoveResult]],
    losers: Sequence[tuple[Mapping, SwSellMoveResult]],
) -> dict:
    n = len(winners) + len(losers)
    if n == 0:
        return {
            "n": 0, "wins": 0, "losses": 0,
            "win_rate": None, "total_pnl": 0.0,
            "avg_sw_sell_move_winners": None,
            "avg_sw_sell_move_losers": None,
        }
    pnls = [
        float(p.get("realizedPnl") or 0.0)
        for p, _ in list(winners) + list(losers)
    ]
    winner_moves = [sm.sw_move for _, sm in winners if sm.sw_move is not None]
    loser_moves = [sm.sw_move for _, sm in losers if sm.sw_move is not None]
    return {
        "n": n,
        "wins": len(winners),
        "losses": len(losers),
        "win_rate": len(winners) / n,
        "total_pnl": float(sum(pnls)),
        "avg_sw_sell_move_winners": (
            sum(winner_moves) / len(winner_moves) if winner_moves else None
        ),
        "avg_sw_sell_move_losers": (
            sum(loser_moves) / len(loser_moves) if loser_moves else None
        ),
    }
```

Update `__all__`:

```python
__all__ = [
    "AttributionCategory",
    "ClassificationLabel",
    "ConfidenceBand",
    "SwSellMoveResult",
    "held_resolution_travel",
    "sw_sell_move",
    "compute_timing_surface",
]
```

- [ ] **Step 4: Run the empty-input test and verify it passes**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py::test_compute_timing_surface_returns_none_on_empty_input -v`
Expected: PASS.

- [ ] **Step 5: Add the classification-matrix tests**

Append to `tests/test_wallet_protocol_timing_surface.py`:

```python
def test_classification_holding_driven_with_dominant_held_bucket():
    """25 held wins + 5 pre-res profitable exits → holding_driven.
    Locks the dominant-rule-before-mixed ordering: even though both
    buckets are profitable and each has n >= 5 (mixed-eligible), the
    held bucket's 0.83 share clears the 0.70 dominance threshold first.
    """
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    activity = []
    for i in range(5):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    result = compute_timing_surface(
        closed, activity,
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert result["classification"] == "holding_driven"
    assert result["confidence_band"] == "high"
    assert result["confidence_downgraded"] is False
    assert result["coverage_downgraded"] is False
    assert result["coverage_caveats"] == []
    assert result["n_attributed"] == 30
    assert result["held_to_resolution"]["n"] == 25
    assert result["pre_resolution"]["n"] == 5


def test_classification_trading_driven_with_dominant_preres_bucket():
    """5 held losses + 25 pre-res profitable → trading_driven."""
    closed = [_build_held_loss_pos(cid=f"held_l_{i}") for i in range(5)]
    activity = []
    for i in range(25):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    result = compute_timing_surface(
        closed, activity,
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert result["classification"] == "trading_driven"


def test_classification_mixed_when_buckets_split_and_both_profitable():
    """12 held profitable + 12 pre-res profitable, neither dominant → mixed."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(12)]
    activity = []
    for i in range(12):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    result = compute_timing_surface(
        closed, activity,
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert result["classification"] == "mixed"


def test_classification_unclear_when_neither_bucket_dominates_and_pnl_negative():
    """6 held losses + 6 pre-res loss cuts + 8 held wins + 10 pre-res winners.
    Held = 14 (50% of n_attributed=28), pre-res = 14 (50%) — neither
    dominates. Both buckets net positive on PnL? Need to check.

    Use a fixture with both buckets net negative to force `unclear`.
    """
    closed = [_build_held_loss_pos(cid=f"held_l_{i}") for i in range(14)]
    activity = []
    for i in range(14):
        pos, act = _build_preres_loss_cut_pos(cid=f"pre_l_{i}")
        closed.append(pos); activity.extend(act)
    result = compute_timing_surface(
        closed, activity,
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert result["classification"] == "unclear"


def test_classification_underpowered_when_n_attributed_below_floor():
    """n_attributed=15 < 20 floor → underpowered, regardless of strong
    composition."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(15)]
    result = compute_timing_surface(
        closed, [],
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert result["classification"] == "underpowered"
    assert "n_attributed=15 below floor 20" in result["classification_reason"]
    assert result["confidence_band"] == "underpowered"
    assert result["coverage_downgraded"] is False     # n < floor, not coverage


def test_future_repricing_fields_always_literal_in_v1():
    """The four reserved fields MUST be literal false/null in v1 and never
    populated. Locks the v1.5 reservation contract."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    result = compute_timing_surface(
        closed, [],
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert result["future_repricing_available"] is False
    assert result["future_repricing_signal"] is None
    assert result["future_repricing_window"] is None
    assert result["price_history_source"] is None
```

- [ ] **Step 6: Run all classification tests and verify they pass**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py -k "classification or future_repricing" -v`
Expected: 6 passed (holding, trading, mixed, unclear, underpowered, future-reserved).

- [ ] **Step 7: Commit**

```bash
git add sniper/analysis/wallet_protocol/timing.py tests/test_wallet_protocol_timing_surface.py
git commit -m "$(cat <<'EOF'
feat(wallet-protocol): Timing Surface v1 — compute_timing_surface aggregator

Aggregator that runs per-position attribution (re-using
exit_attribution.classify_position so categorization cannot drift),
computes held/pre-res aggregates including avg_captured_travel and
avg_sw_sell_move_*, and applies the r2 classification rule.

r2 §5 split landed:
  force_underpowered = n_attributed < 20 OR attribution_coverage_low
                       OR activity_truncated_without_fallback
  coverage_caveats   = stable tag list (informational; never alone
                       forces underpowered)
  confidence_band    = "high" / "low" / "underpowered"
  confidence_downgraded = (band == "low")
  coverage_downgraded   = back-compat: forced underpowered specifically
                          by a coverage caveat

n_unmapped_fills > 0 fires SUBGRAPH_UNMAPPED_FILLS caveat + drops
confidence to "low" but does NOT alone force underpowered.

Dominant-rule-before-mixed ordering locked: 25 held wins + 5 pre-res
profitable exits classifies as `holding_driven` (held_share=0.83 > 0.70),
not `mixed`.

Future-reserved fields (future_repricing_*, price_history_source)
always literal false/null in v1.

Six classification tests: holding_driven, trading_driven, mixed,
unclear, underpowered (n<floor), future-reserved literal contract.

Spec: docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md
      (commit d598cf2, r2 §5–§6)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Aggregator gating — coverage caveats + unmapped-fills caveat-without-force-underpowered (T5)

**Files:**
- Modify: `tests/test_wallet_protocol_timing_surface.py`

**What this task locks:** Audit oracle T5 (unmapped fills caveat alone does NOT force underpowered) plus the remaining coverage-gate cases (attribution_coverage_low forces underpowered + coverage_downgraded; activity_truncated forces underpowered + coverage_downgraded). No code changes — `compute_timing_surface` already implements this; these tests lock the contract.

- [ ] **Step 1: Add T5 — unmapped fills alone do NOT force underpowered**

Append to `tests/test_wallet_protocol_timing_surface.py`:

```python
# ---------------------------------------------------------------------------
# Section D — coverage gating + caveats (audit T5 anchor)
# ---------------------------------------------------------------------------


def test_t5_unmapped_fills_alone_does_not_force_underpowered():
    """T5 (audit oracle): one unmapped fill in an otherwise strong
    sample fires SUBGRAPH_UNMAPPED_FILLS caveat + drops confidence to
    "low" but classification stands. Critical regression-lock against
    the r1-era behavior that forced underpowered on any unmapped count.
    """
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    activity = []
    for i in range(5):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    result = compute_timing_surface(
        closed, activity,
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=1,    # the lone unmapped fill
        attribution_coverage_low=False,
    )
    # Classification stands.
    assert result["classification"] == "holding_driven"
    # Confidence drops; coverage caveat fires.
    assert result["confidence_band"] == "low"
    assert result["confidence_downgraded"] is True
    # Not forced-underpowered.
    assert result["coverage_downgraded"] is False
    assert result["coverage_caveats"] == ["SUBGRAPH_UNMAPPED_FILLS"]


def test_attribution_coverage_low_forces_underpowered_with_coverage_downgraded():
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    result = compute_timing_surface(
        closed, [],
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=True,
    )
    assert result["classification"] == "underpowered"
    assert result["confidence_band"] == "underpowered"
    assert result["coverage_downgraded"] is True
    assert "ATTRIBUTION_COVERAGE_LOW" in result["coverage_caveats"]


def test_activity_truncated_without_fallback_forces_underpowered():
    """activity_cap_hit AND not (subgraph_fallback OR walk_full_history)
    → ACTIVITY_TRUNCATED caveat + force_underpowered + coverage_downgraded.
    """
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    result = compute_timing_surface(
        closed, [],
        activity_cap_hit=True, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert result["classification"] == "underpowered"
    assert result["coverage_downgraded"] is True
    assert "ACTIVITY_TRUNCATED" in result["coverage_caveats"]


def test_activity_truncated_with_subgraph_fallback_does_not_fire_caveat():
    """Cap hit but subgraph fallback ran → no ACTIVITY_TRUNCATED caveat."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    result = compute_timing_surface(
        closed, [],
        activity_cap_hit=True, subgraph_fallback_used=True,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert "ACTIVITY_TRUNCATED" not in result["coverage_caveats"]
    assert result["classification"] == "holding_driven"
    assert result["confidence_band"] == "high"


def test_activity_truncated_with_walk_full_history_does_not_fire_caveat():
    """Cap hit but walk_full_history ran → no ACTIVITY_TRUNCATED caveat."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    result = compute_timing_surface(
        closed, [],
        activity_cap_hit=True, subgraph_fallback_used=False,
        walk_full_history=True, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert "ACTIVITY_TRUNCATED" not in result["coverage_caveats"]
    assert result["classification"] == "holding_driven"


def test_multiple_caveats_aggregate_into_caveat_list():
    """Multiple coverage issues → all caveats fire; classification forced
    underpowered; coverage_downgraded=True."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    result = compute_timing_surface(
        closed, [],
        activity_cap_hit=True, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=3,
        attribution_coverage_low=True,
    )
    assert result["classification"] == "underpowered"
    assert result["coverage_downgraded"] is True
    # Order must match the spec: ACTIVITY_TRUNCATED, SUBGRAPH_UNMAPPED_FILLS,
    # ATTRIBUTION_COVERAGE_LOW.
    assert result["coverage_caveats"] == [
        "ACTIVITY_TRUNCATED", "SUBGRAPH_UNMAPPED_FILLS", "ATTRIBUTION_COVERAGE_LOW",
    ]
```

- [ ] **Step 2: Run the gating tests**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py -k "t5 or coverage or activity_truncated or multiple_caveats" -v`
Expected: 6 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_wallet_protocol_timing_surface.py
git commit -m "$(cat <<'EOF'
test(wallet-protocol): Timing Surface v1 — coverage gating + audit T5

Locks the r2 §5 gating split via tests on the existing aggregator:
  - T5: SUBGRAPH_UNMAPPED_FILLS alone fires caveat + drops confidence
        but classification stands (regression-lock against r1 behavior).
  - ATTRIBUTION_COVERAGE_LOW forces underpowered + coverage_downgraded.
  - ACTIVITY_TRUNCATED (cap hit AND not fallback/walk) forces
    underpowered + coverage_downgraded.
  - subgraph_fallback_used and walk_full_history each individually
    suppress the ACTIVITY_TRUNCATED caveat.
  - Multiple caveats aggregate in deterministic order
    (ACTIVITY_TRUNCATED, SUBGRAPH_UNMAPPED_FILLS, ATTRIBUTION_COVERAGE_LOW).

No code changes — these tests verify behavior already implemented in
Task 3.

Spec: docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md
      (commit d598cf2, r2 §5)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Plumbing — `ProtocolReport.timing_surface` + `analyze_wallet` wiring + curPrice-coordinate-invariance test (T4)

**Files:**
- Modify: `sniper/analysis/wallet_protocol/types.py`
- Modify: `sniper/analysis/wallet_protocol/analyze_wallet.py`
- Modify: `tests/test_wallet_protocol_timing_surface.py`

**What this task locks:** ProtocolReport gains the new field; analyze_wallet calls compute_timing_surface; audit oracle T4 (curPrice coordinate flip leaves timing_surface unchanged) is verified.

- [ ] **Step 1: Add the failing plumbing test**

Append to `tests/test_wallet_protocol_timing_surface.py`:

```python
# ---------------------------------------------------------------------------
# Section E — plumbing: ProtocolReport.timing_surface + analyze_wallet wiring
# ---------------------------------------------------------------------------


import time

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


def _now() -> int:
    return int(time.time())


def _wallet_data(
    *,
    closed_positions,
    activity_rows,
    activity_cap_hit=False,
    subgraph_fallback_used=False,
    walk_full_history=False,
    n_unmapped_fills=0,
):
    return WalletData(
        wallet="0xabc",
        closed_positions=list(closed_positions),
        open_positions=[],
        activity_rows=list(activity_rows),
        activity_cap_hit=activity_cap_hit,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=None,
        fetched_at=_now(),
        subgraph_fallback_used=subgraph_fallback_used,
        walk_full_history=walk_full_history,
        n_unmapped_fills=n_unmapped_fills,
    )


def test_protocol_report_has_timing_surface_field_default_none():
    """A bare ProtocolReport has timing_surface=None until analyze_wallet
    populates it. Display-only field; never feeds verdict."""
    r = ProtocolReport(wallet="0xabc")
    assert r.timing_surface is None
```

- [ ] **Step 2: Run the test and verify it fails with AttributeError**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py::test_protocol_report_has_timing_surface_field_default_none -v`
Expected: FAIL with `AttributeError: 'ProtocolReport' object has no attribute 'timing_surface'`.

- [ ] **Step 3: Add `timing_surface` to `ProtocolReport`**

In `sniper/analysis/wallet_protocol/types.py`, immediately after the
`exit_attribution` field (around line 344, end of the existing
ProtocolReport block before the closing of the dataclass), add:

```python
    # Timing Surface v1 — captured-travel-only timing classification.
    # Display-only; NEVER feeds verdict, gate, or primary_edge_type.
    # See docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md
    # (commit d598cf2, r2). Audit cross-ref:
    # docs/superpowers/specs/2026-05-06-wallet-protocol-semantic-audit.md.
    # Shape (when populated): see spec §6 — top-level fields:
    #     classification, classification_reason,
    #     confidence_band, confidence_downgraded,
    #     coverage_downgraded, coverage_caveats,
    #     n_attributed, n_total_closed,
    #     held_to_resolution, pre_resolution,
    #     future_repricing_available (always False in v1),
    #     future_repricing_signal / window / price_history_source
    #         (always None in v1).
    timing_surface: dict | None = None
```

- [ ] **Step 4: Run the field-default test and verify it passes**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py::test_protocol_report_has_timing_surface_field_default_none -v`
Expected: PASS.

- [ ] **Step 5: Add the failing analyze_wallet wiring test**

Append to `tests/test_wallet_protocol_timing_surface.py`:

```python
def _build_strong_holding_data():
    """30 attributed closed positions, all held wins, no caveats."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(30)]
    return _wallet_data(
        closed_positions=closed, activity_rows=[],
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
    )


def test_analyze_wallet_populates_timing_surface_on_strong_data():
    data = _build_strong_holding_data()
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    assert isinstance(report, ProtocolReport)
    assert report.timing_surface is not None
    assert report.timing_surface["classification"] == "holding_driven"
    assert report.timing_surface["confidence_band"] == "high"
    assert report.timing_surface["coverage_caveats"] == []
    # Future-reserved fields literal in v1.
    assert report.timing_surface["future_repricing_available"] is False
    assert report.timing_surface["future_repricing_signal"] is None
```

- [ ] **Step 6: Run the wiring test and verify it fails (timing_surface is still None)**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py::test_analyze_wallet_populates_timing_surface_on_strong_data -v`
Expected: FAIL with `AssertionError: assert None is not None` (or similar).

- [ ] **Step 7: Wire compute_timing_surface into analyze_wallet**

In `sniper/analysis/wallet_protocol/analyze_wallet.py`, locate the
existing block that computes `report.exit_attribution` (around line 668-678,
ending with the closing `)` of `compute_attribution_summary(...)`).
Immediately after that block, add:

```python
    # Timing Surface v1 — captured-travel-only timing classification.
    # Display-only. Re-uses exit_attribution categorization so timing
    # cannot drift. NEVER feeds verdict / gate / primary_edge_type.
    # See docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md
    # (commit d598cf2, r2).
    from sniper.analysis.wallet_protocol.timing import compute_timing_surface

    ea = report.exit_attribution or {}
    ea_summary = ea.get("summary") or {}
    ea_coverage = ea.get("coverage") or {}
    _n_total = int(ea_coverage.get("n_total", 0) or 0)
    _n_insufficient = int((ea_summary.get("insufficient_data") or {}).get("n", 0) or 0)
    _n_attributed_for_coverage = max(_n_total - _n_insufficient, 0)
    _attribution_coverage_low = (
        _n_total > 0 and (_n_attributed_for_coverage / _n_total) < 0.50
    )
    report.timing_surface = compute_timing_surface(
        data.closed_positions,
        data.activity_rows,
        activity_cap_hit=data.activity_cap_hit,
        subgraph_fallback_used=data.subgraph_fallback_used,
        walk_full_history=data.walk_full_history,
        n_unmapped_fills=data.n_unmapped_fills,
        attribution_coverage_low=_attribution_coverage_low,
    )
```

- [ ] **Step 8: Run the wiring test and verify it passes**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py::test_analyze_wallet_populates_timing_surface_on_strong_data -v`
Expected: PASS.

- [ ] **Step 9: Add T4 — curPrice-coordinate invariance test**

Append to `tests/test_wallet_protocol_timing_surface.py`:

```python
def test_t4_timing_surface_invariant_under_curprice_coordinate_flip():
    """T4 (audit oracle): proves Timing Surface v1 does NOT depend on
    closed_positions[].curPrice's coordinate.

    Build a held-to-resolution win set. Build a "flipped" twin where
    every position's curPrice is replaced (1.0 ↔ 0.0). All other
    fields identical (especially realizedPnl — the source of truth for
    the held_to_resolution_win/_loss split via exit_attribution's
    PnL-sign-based classifier).

    Expected: timing_surface dict is identical between the two runs.
    Locks audit-R2 routing.
    """
    base_closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    base = _wallet_data(closed_positions=base_closed, activity_rows=[])
    r_base = analyze_wallet("0xabc", pre_fetched_data=base, n_iter=200, seed=1)

    # Flipped twin: curPrice 1.0 → 0.0 on every position. realizedPnl
    # preserved (still > 0 → exit_attribution classifies as held_win
    # because curPrice ≈ 0 still trips _at_resolution AND PnL > 0).
    flipped_closed = []
    for p in base_closed:
        q = dict(p)
        q["curPrice"] = 0.0 if q["curPrice"] == 1.0 else 1.0
        flipped_closed.append(q)
    flipped = _wallet_data(closed_positions=flipped_closed, activity_rows=[])
    r_flip = analyze_wallet("0xabc", pre_fetched_data=flipped, n_iter=200, seed=1)

    # The timing_surface dict must be identical — Timing v1 doesn't
    # touch curPrice for magnitude.
    assert r_base.timing_surface == r_flip.timing_surface
```

- [ ] **Step 10: Run T4 and verify it passes**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py::test_t4_timing_surface_invariant_under_curprice_coordinate_flip -v`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add sniper/analysis/wallet_protocol/types.py sniper/analysis/wallet_protocol/analyze_wallet.py tests/test_wallet_protocol_timing_surface.py
git commit -m "$(cat <<'EOF'
feat(wallet-protocol): Timing Surface v1 — plumbing + T4 coord invariance

ProtocolReport gains `timing_surface: dict | None = None`. analyze_wallet
calls compute_timing_surface(...) immediately after compute_attribution_
summary(...), deriving attribution_coverage_low from the same
n_attributed/n_total threshold (< 0.50) used by Completeness Surface v1.

Audit oracle T4 locked: identical closed_positions universe with
curPrice flipped (1.0 ↔ 0.0) yields a byte-for-byte identical
timing_surface dict because Timing v1 uses universal payout (1.0/0.0)
for held captured-travel — never reads curPrice for magnitude. This
proves the surface is decoupled from audit-R2's unresolved coordinate
question.

Spec: docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md
      (commit d598cf2, r2 §7)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Render — `_timing_surface_block` with per-classification interpretation + confidence band

**Files:**
- Modify: `sniper/analysis/wallet_protocol/report.py`
- Modify: `tests/test_wallet_protocol_timing_surface.py`

**What this task locks:** The `## Timing surface` section renders after Edge surface, before Stage 1 result. Per-classification interpretation paragraphs use only allowed phrasing. Confidence band line surfaces alongside classification. Coverage downgrade paragraph references `## Data completeness`.

- [ ] **Step 1: Add the failing render-section-presence test**

Append to `tests/test_wallet_protocol_timing_surface.py`:

```python
# ---------------------------------------------------------------------------
# Section F — render: ## Timing surface block
# ---------------------------------------------------------------------------


from sniper.analysis.wallet_protocol.report import render_report


def test_timing_surface_section_renders_after_edge_surface_before_stage1():
    """Section ordering: ## Timing surface comes after ## Edge surface
    and before ### Stage 1 result."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    activity = []
    for i in range(5):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    data = _wallet_data(closed_positions=closed, activity_rows=activity)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)
    timing_pos = md.find("## Timing surface")
    edge_pos = md.find("## Edge surface")
    stage1_pos = md.find("### Stage 1 result")
    assert timing_pos != -1, "## Timing surface must appear in render"
    if edge_pos != -1:
        assert edge_pos < timing_pos
    assert stage1_pos != -1
    assert timing_pos < stage1_pos
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py::test_timing_surface_section_renders_after_edge_surface_before_stage1 -v`
Expected: FAIL with `assert -1 != -1` (the section doesn't render yet).

- [ ] **Step 3: Add `_timing_surface_block` to `report.py`**

In `sniper/analysis/wallet_protocol/report.py`, immediately after the
existing `_edge_surface_block` function (around line 517, after its
`return out`), add:

```python
def _classification_interpretation(ts: dict) -> str:
    """Return the plain-language interpretation paragraph for a
    classification. Allowed-phrasing-only — see spec §8.7 + audit
    forbidden-phrase list."""
    cls = ts["classification"]
    held = ts["held_to_resolution"]
    pre  = ts["pre_resolution"]
    n_attr = ts["n_attributed"]

    if cls == "holding_driven":
        return (
            f"This wallet's gains came predominantly from holding positions "
            f"through resolution. Of {n_attr} attributed positions, "
            f"{held['n']} were held-to-resolution and {held['wins']}/{held['n']} "
            f"resolved in the wallet's favor "
            f"(WR={_fmt_pct(held['win_rate'])}, "
            f"avg captured travel={_fmt_pct(held['avg_captured_travel'])}). "
            f"Pre-resolution exits contributed "
            f"{_fmt_money(pre['total_pnl'])} over {pre['n']} trades."
        )
    if cls == "trading_driven":
        return (
            f"This wallet's gains came predominantly from pre-resolution exits. "
            f"Of {n_attr} attributed positions, {pre['n']} closed via SELL "
            f"before resolution; {pre['wins']}/{pre['n']} of those exited "
            f"profitably (WR={_fmt_pct(pre['win_rate'])}, "
            f"avg observed exit move (winners)="
            f"{_fmt_pct(pre.get('avg_sw_sell_move_winners'))}). "
            f"Held-to-resolution positions contributed "
            f"{_fmt_money(held['total_pnl'])} over {held['n']} trades."
        )
    if cls == "mixed":
        return (
            f"Both holding and pre-resolution trading contributed to this "
            f"wallet's results. Held-to-resolution: {held['n']} trades, "
            f"WR={_fmt_pct(held['win_rate'])}, "
            f"PnL={_fmt_money(held['total_pnl'])}. "
            f"Pre-resolution exits: {pre['n']} trades, "
            f"WR={_fmt_pct(pre['win_rate'])}, "
            f"PnL={_fmt_money(pre['total_pnl'])}. "
            f"Neither pattern dominated."
        )
    if cls == "unclear":
        return (
            f"Neither holding nor pre-resolution trading clearly explains "
            f"this wallet's results. Held-to-resolution and pre-resolution "
            f"buckets each have material n but no single bucket exceeds "
            f"the share/PnL thresholds for a confident label."
        )
    if cls == "underpowered":
        if ts.get("coverage_downgraded"):
            return (
                "Timing evidence is limited — Completeness caveats fired "
                "(capture truncated, unmapped fills, or attribution coverage "
                "<50%). Captured-travel numbers below are still reported but "
                "should not drive copy decisions. See `## Data completeness`."
            )
        return (
            f"Timing evidence is limited — {ts['classification_reason']}. "
            f"Captured-travel numbers below are reported but should not "
            f"drive copy decisions."
        )
    return ""


def _timing_surface_block(report: ProtocolReport) -> list[str]:
    """Timing Surface v1 — captured-travel-only classification block.

    Renders nothing when report.timing_surface is None (no closed
    positions). NEVER feeds verdict, gate, or primary_edge_type.

    See spec §8 (commit d598cf2, r2).
    """
    ts = report.timing_surface
    if not isinstance(ts, dict):
        return []

    out = ["## Timing surface", "", f"Classification: {ts['classification']}"]
    interpretation = _classification_interpretation(ts)
    if interpretation:
        out.append(interpretation)

    out.append("")

    held = ts["held_to_resolution"]
    if held["n"] > 0:
        out.append(
            f"- Held to resolution: n={held['n']} ({held['wins']} win / "
            f"{held['losses']} loss, WR={_fmt_pct(held['win_rate'])}, "
            f"avg captured travel="
            f"{_fmt_pct(held['avg_captured_travel'])}), "
            f"PnL={_fmt_money(held['total_pnl'])}"
        )

    pre = ts["pre_resolution"]
    if pre["n"] > 0:
        out.append(
            f"- Pre-resolution exit: n={pre['n']} ({pre['wins']} win / "
            f"{pre['losses']} loss, WR={_fmt_pct(pre['win_rate'])}, "
            f"avg observed exit move (winners)="
            f"{_fmt_pct(pre.get('avg_sw_sell_move_winners'))}), "
            f"PnL={_fmt_money(pre['total_pnl'])}"
        )

    # Confidence + coverage caveats.
    if ts["confidence_band"] != "high" or ts["coverage_caveats"]:
        out.append(f"- Confidence: {ts['confidence_band']}")
    if ts["coverage_caveats"]:
        out.append(
            f"- Coverage caveats: {', '.join(ts['coverage_caveats'])} "
            f"(see `## Data completeness`)"
        )

    # NOTE: render-language guard — do NOT include the literal string
    # "entered before repricing" or any other phrase from Task 7's
    # forbidden-phrase scan. The note below describes the v1 limitation
    # in allowed phrasing only.
    out.append(
        "- Note: timing surface describes captured travel only. Strict "
        "market-repricing timing analysis requires external price "
        "history and is not implemented in v1 "
        "(`future_repricing_available=false`)."
    )
    return out
```

Then, in `render_report` (around line 671-678 where `_edge_surface_block`
is rendered, immediately after that block and before the Stage 1
section that begins at line 680 with `lines.append("### Stage 1 result")`),
add:

```python
    # Timing Surface v1 — captured-travel-only classification block.
    # NEVER feeds verdict; rendered between Edge surface and Stage 1.
    timing_lines = _timing_surface_block(report)
    if timing_lines:
        lines.append("")
        lines.extend(timing_lines)

```

- [ ] **Step 4: Run the section-presence test and verify it passes**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py::test_timing_surface_section_renders_after_edge_surface_before_stage1 -v`
Expected: PASS.

- [ ] **Step 5: Add the per-classification render tests**

Append to `tests/test_wallet_protocol_timing_surface.py`:

```python
def _render_for_classification(closed, activity, **kwargs):
    data = _wallet_data(
        closed_positions=closed, activity_rows=activity, **kwargs,
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    return render_report(report)


def test_render_holding_driven_uses_allowed_phrasing():
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    activity = []
    for i in range(5):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    md = _render_for_classification(closed, activity)
    assert "Classification: holding_driven" in md
    assert "predominantly from holding positions" in md
    # Coverage caveats line absent on a clean run.
    assert "Coverage caveats:" not in md


def test_render_trading_driven_uses_allowed_phrasing():
    closed = [_build_held_loss_pos(cid=f"held_l_{i}") for i in range(5)]
    activity = []
    for i in range(25):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    md = _render_for_classification(closed, activity)
    assert "Classification: trading_driven" in md
    assert "predominantly from pre-resolution exits" in md


def test_render_underpowered_with_coverage_downgrade_references_completeness():
    """coverage_downgraded path must reference `## Data completeness`."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    md = _render_for_classification(
        closed, [],
        activity_cap_hit=True, subgraph_fallback_used=False,
        walk_full_history=False,
    )
    assert "Classification: underpowered" in md
    assert "Completeness caveats fired" in md
    assert "`## Data completeness`" in md


def test_render_unmapped_fills_alone_renders_caveat_with_holding_classification():
    """Locks T5 in the render path: classification=holding_driven,
    confidence: low, coverage caveat present."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    activity = []
    for i in range(5):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    md = _render_for_classification(closed, activity, n_unmapped_fills=1)
    assert "Classification: holding_driven" in md
    assert "Confidence: low" in md
    assert "SUBGRAPH_UNMAPPED_FILLS" in md
    assert "(see `## Data completeness`)" in md
```

- [ ] **Step 6: Run all render tests and verify they pass**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py -k render -v`
Expected: 5 passed (section ordering + 4 per-classification render tests).

- [ ] **Step 7: Commit**

```bash
git add sniper/analysis/wallet_protocol/report.py tests/test_wallet_protocol_timing_surface.py
git commit -m "$(cat <<'EOF'
feat(wallet-protocol): Timing Surface v1 — render block

`## Timing surface` section renders after `## Edge surface` and before
`### Stage 1 result`. Per-classification interpretation paragraphs use
only allowed phrasing (no "entered before repricing" / information-edge
language — those are reserved for v1.5/H6). Structured held/pre-res
bullet lines + confidence + coverage caveat block (when applicable) +
note pointing at the future_repricing_available=false reservation.

Coverage downgrade path explicitly references `## Data completeness`
so the operator follows the trail back to the upstream caveat surface.

Five render tests cover section ordering + holding_driven /
trading_driven phrasing + underpowered-with-coverage-downgrade
phrasing + T5 (unmapped fills caveat without forced underpowered)
in the rendered output.

Spec: docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md
      (commit d598cf2, r2 §8)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Lock-in regression tests — forbidden phrasing, no-external-lookup, verdict-invariance

**Files:**
- Modify: `tests/test_wallet_protocol_timing_surface.py`

**What this task locks:** Three regression tests that cannot regress without breaking — forbidden-phrase scan across every classification, no-HTTP-call assertion in `compute_timing_surface`, and verdict-invariance across the matched/unmatched timing surface.

- [ ] **Step 1: Add the forbidden-phrasing regression test**

Append to `tests/test_wallet_protocol_timing_surface.py`:

```python
# ---------------------------------------------------------------------------
# Section G — regression locks: forbidden phrasing, no external lookup,
# verdict invariance
# ---------------------------------------------------------------------------


# Spec §8.7 + audit forbidden-phrase list. Anything here in a rendered
# Timing Surface v1 report is a regression — these claims require strict
# repricing enrichment (v1.5 / H6) which v1 does not implement.
_FORBIDDEN_PHRASES = (
    "entered before repricing",
    "acted before the market",
    "had information advantage",
    "predicted the move early",
    "front-ran",
    "knew before the market",
    "pre-news edge",
    "early information edge",
    "latency edge",
)


def _all_classifications_render_corpus() -> list[str]:
    """Build a rendered-report corpus covering every classification path."""
    corpus: list[str] = []

    # holding_driven
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    activity = []
    for i in range(5):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    corpus.append(_render_for_classification(closed, activity))

    # trading_driven
    closed = [_build_held_loss_pos(cid=f"held_l_{i}") for i in range(5)]
    activity = []
    for i in range(25):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    corpus.append(_render_for_classification(closed, activity))

    # mixed
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(12)]
    activity = []
    for i in range(12):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    corpus.append(_render_for_classification(closed, activity))

    # unclear (all losses both buckets)
    closed = [_build_held_loss_pos(cid=f"held_l_{i}") for i in range(14)]
    activity = []
    for i in range(14):
        pos, act = _build_preres_loss_cut_pos(cid=f"pre_l_{i}")
        closed.append(pos); activity.extend(act)
    corpus.append(_render_for_classification(closed, activity))

    # underpowered (n < floor)
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(15)]
    corpus.append(_render_for_classification(closed, []))

    # underpowered + coverage_downgraded
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    corpus.append(_render_for_classification(
        closed, [],
        activity_cap_hit=True, subgraph_fallback_used=False,
        walk_full_history=False,
    ))
    return corpus


def test_no_forbidden_phrases_appear_in_any_rendered_report():
    """Lock §8.7 against accidental over-claiming. If any rendered
    report contains a forbidden phrase, the test fails with a precise
    pointer to which phrase + which classification."""
    corpus = _all_classifications_render_corpus()
    for i, md in enumerate(corpus):
        lower = md.lower()
        for phrase in _FORBIDDEN_PHRASES:
            assert phrase.lower() not in lower, (
                f"Forbidden phrase {phrase!r} appeared in rendered "
                f"report #{i} (classification corpus index)."
            )


def test_compute_timing_surface_makes_no_http_calls(monkeypatch):
    """compute_timing_surface MUST NOT call requests.Session.get or any
    HTTP method. Locks the no-external-lookup contract."""
    import requests
    calls: list[tuple] = []

    def _forbidden_get(self, *args, **kwargs):
        calls.append(("get", args, kwargs))
        raise AssertionError("requests.Session.get called inside compute_timing_surface")

    def _forbidden_post(self, *args, **kwargs):
        calls.append(("post", args, kwargs))
        raise AssertionError("requests.Session.post called inside compute_timing_surface")

    monkeypatch.setattr(requests.Session, "get", _forbidden_get)
    monkeypatch.setattr(requests.Session, "post", _forbidden_post)

    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    activity = []
    for i in range(5):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    result = compute_timing_surface(
        closed, activity,
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert result is not None
    assert calls == [], f"Unexpected HTTP calls: {calls}"


def _verdict_signature(r: ProtocolReport) -> tuple:
    return (
        r.overall_verdict,
        r.gate_result,
        r.primary_edge_type,
        r.copyability_note,
    )


def test_timing_surface_does_not_alter_verdict_signature():
    """Verdict invariance: same closed-positions universe → identical
    (overall_verdict, gate_result, primary_edge_type, copyability_note)
    regardless of timing_surface presence/content. Mirrors Completeness
    Surface v1's invariance contract."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}", realized_pnl=20.0)
              for i in range(40)]
    base = _wallet_data(closed_positions=closed, activity_rows=[])
    r_base = analyze_wallet("0xabc", pre_fetched_data=base, n_iter=200, seed=1)

    # Decorated twin: introduce coverage caveats + unmapped fills + cap_hit
    # → forces timing_surface into underpowered + coverage_downgraded.
    decorated = _wallet_data(
        closed_positions=closed, activity_rows=[],
        activity_cap_hit=True, subgraph_fallback_used=True,
        walk_full_history=True, n_unmapped_fills=11,
    )
    r_dec = analyze_wallet("0xabc", pre_fetched_data=decorated, n_iter=200, seed=1)

    # Verdicts must be identical even though timing_surface differs.
    assert _verdict_signature(r_base) == _verdict_signature(r_dec)
    # Sanity: timing_surface really did differ between runs.
    assert r_base.timing_surface != r_dec.timing_surface
```

- [ ] **Step 2: Run the regression-lock tests and verify they pass**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py -k "forbidden or http or verdict" -v`
Expected: 3 passed.

- [ ] **Step 3: Run the entire timing-surface test file and verify everything passes**

Run: `python3 -m pytest tests/test_wallet_protocol_timing_surface.py -v`
Expected: all tests in the file pass (target ~25 tests across all sections).

- [ ] **Step 4: Commit**

```bash
git add tests/test_wallet_protocol_timing_surface.py
git commit -m "$(cat <<'EOF'
test(wallet-protocol): Timing Surface v1 — regression locks

Three locks that prevent silent regressions:

1. Forbidden-phrasing scan across every classification path. If any
   rendered Timing Surface v1 report contains "entered before
   repricing", "front-ran", "had information advantage", "knew before
   the market", "pre-news edge", "early information edge",
   "latency edge", "acted before the market", or "predicted the
   move early", the test fails with a precise pointer.

2. No-HTTP-call assertion: monkey-patches requests.Session.get and
   .post to raise AssertionError; runs compute_timing_surface across
   a strong fixture; asserts zero calls were made. Locks the no-
   external-lookup contract.

3. Verdict invariance: same closed_positions universe with vs without
   coverage caveats produces an identical
   (overall_verdict, gate_result, primary_edge_type, copyability_note)
   tuple even though timing_surface itself differs. Locks the display-
   only contract; mirrors Completeness Surface v1's invariance test.

Spec: docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md
      (commit d598cf2, r2 §9)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Final verification — full repo suite + calibration + schema validator

**Files:** none modified (verification-only).

**What this task locks:** No regressions elsewhere in the codebase, calibration suite still green, schema validator clean.

- [ ] **Step 1: Run the full repo test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All tests pass. The pre-Timing-Surface baseline was `4106 passed, 16 skipped` (per `9a03140` commit). The new file adds ~25 cases, so target is `~4131 passed, 16 skipped`.

If anything other than the new tests fails, stop and investigate — the failure is likely a regression introduced by the wiring in `analyze_wallet.py` or the render insert in `report.py`. Common failure modes to look for first:
- A test that asserts on the rendered report's exact line count or structure may need updating to account for the new section. The fix is to update the offending test's assertion to either ignore the new section or accept its presence — NOT to skip rendering Timing Surface in those fixtures.
- A schema validator failure: see Step 3.

- [ ] **Step 2: Run the calibration suite**

Run: `python3 -m pytest tests/calibration/ -q`
Expected: `22 passed, 20 skipped` (the pre-Timing-Surface baseline). Verdict-invariance test from Task 7 is the contract that prevents this from drifting.

- [ ] **Step 3: Run the schema reference validator**

Run: `python3 scripts/validate_schema_refs.py`
Expected: clean, no schema violations.

- [ ] **Step 4: Run the brain-index freshness check**

Run: `python3 scripts/brain_index.py generate`
Expected: no diff against the existing `index/schema_registry.json` and `index/dependency_index.json` (we didn't touch DB schema). If a diff appears, commit it; CI checks freshness.

- [ ] **Step 5: Smoke-render a representative fixture if available**

If a calibration or replay fixture exists for a known wallet (YatSen at `0x5bff…d6ffbe` was the recurring example in the spec), render the report and visually verify the `## Timing surface` section appears with sensible content.

Run (example):
```bash
python3 -c "
import json
from pathlib import Path
from sniper.analysis.wallet_protocol.replay import deserialize_wallet_data
from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.report import render_report

# Replace with actual fixture path on disk if available.
fixtures = list(Path('tests').rglob('*yatsen*.json')) + list(Path('tests').rglob('*0x5bff*.json'))
if not fixtures:
    print('no YatSen-style fixture available; skip smoke')
else:
    payload = json.loads(fixtures[0].read_text())
    data = deserialize_wallet_data(payload)
    report = analyze_wallet(data.wallet, pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)
    print(md.split('## Timing surface', 1)[-1].split('## ', 1)[0])
"
```

If no fixture exists, skip — the unit tests above are sufficient for the v1 contract.

- [ ] **Step 6: If Steps 1–4 are clean and there were no leftover modifications, no further commit is needed**

The seven feature commits from Tasks 1–7 form the complete Timing Surface v1 r2 ship. If brain index regenerated with diff, commit it as a `chore(brain-index): regenerate after Timing Surface v1` follow-up.

---

## Self-review (pre-handoff)

**Spec coverage check:** Each r2 spec section maps to a task:
- §1, §2, §3 (purpose, scope, deferred): no code; covered by Task 7 forbidden-phrasing test (claims-language guard) + future-reserved fields (Task 3).
- §4 (inputs): used by Task 3 + Task 5 (analyze_wallet wiring).
- §5 (classification rule): Task 3 + Task 4.
- §6 (returned dict shape): Task 3 (built) + Task 4 (caveats list shape).
- §6.1 (formula): Task 1 (held_resolution_travel) + Task 2 (sw_sell_move + audit T1–T3).
- §7 (plumbing): Task 5.
- §8 (render): Task 6.
- §8.7 (allowed/forbidden phrasing): Task 6 (rendering) + Task 7 (regression lock).
- §9 (tests T1–T5 + structural): T1, T2, T3 in Task 2; T4 in Task 5; T5 in Task 4 + Task 6 (render); structural cases in Tasks 3, 4, 6, 7.
- §10 (v1.5 reservation + followup tickets): Task 3 (literal false/null) + plan body (cross-references).
- §11 (files touched): matches the File Structure table.
- §12 (acceptance criteria): all checked by Task 7 + Task 8.
- §13 (verification plan): exactly Task 8.

No gaps.

**Placeholder scan:** No "TBD" / "TODO" / "fill in" / "similar to Task N" entries. Every step contains either exact code or an exact command with expected output.

**Type consistency check:**
- `held_resolution_travel(position, attribution_category) -> float` — same in Task 1 (definition) and Task 3 (consumer in `_build_held_bucket`). ✓
- `sw_sell_move(position, activity_rows) -> SwSellMoveResult` — same in Task 2 (definition) and Task 3 (consumer). ✓
- `compute_timing_surface(...)` keyword args match between Task 3 (definition), Task 5 (analyze_wallet caller), and the test fixtures (Tasks 3, 4). ✓
- `ProtocolReport.timing_surface: dict | None` — added in Task 5 step 3, consumed in Task 6 (`_timing_surface_block`). ✓
- `coverage_caveats` is always a list of strings; ordering locked in Task 4 (`ACTIVITY_TRUNCATED, SUBGRAPH_UNMAPPED_FILLS, ATTRIBUTION_COVERAGE_LOW`). ✓
- `confidence_band` ∈ {"high", "low", "underpowered"}. Used consistently in Task 3 (build), Task 4 (assert), Task 6 (render). ✓
- `sell_price_method` ∈ {"size_weighted", "latest_sell_fallback", "unavailable"}. Used consistently in Task 2 (NamedTuple) and Task 3 (sell_method_counts dict keys). ✓

**No forbidden cross-task references:** Each task defines what it adds and what it consumes; no task assumes a function defined in a later task.

**Hard scope guards verified:**
- No `price_history_fn` import anywhere in the new module. ✓
- No `requests` import in `timing.py`. ✓ (Task 7 step 1 imports `requests` only inside the test function, to monkey-patch.)
- No FH6 / H6 references. ✓
- No verdict-logic mutations in `analyze_wallet.py` (Task 5 step 7 only adds the `report.timing_surface = ...` assignment after exit_attribution). ✓
- Verdict invariance test in Task 7 explicitly asserts no drift. ✓
