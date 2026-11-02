"""Slice 5 — per-hypothesis market-structure guard.

Every emitted result surface from `run_held_branch` / `run_flipper_branch`
maps to one of three classes (spec §14, plan §D5):

* ``structure_tolerant`` — works on any market structure with a clean
  closed-position record (e.g. H1 net PnL, FH5 hold-duration descriptor).
* ``token_price_only`` — needs token-price history but is indifferent to
  outcome resolution (e.g. H4 entry timing, H6/FH6 post-sell drift,
  FH1/FH2/FH3/FH4 realized BUY→SELL).
* ``binary_outcome_required`` — needs win/loss labels from a binary
  outcome resolution (e.g. H2/H2_sub080/H3 held-to-resolution stats, H5
  tag-grain win-rate).

Slice 5 wires this module into the materialized branch path only —
direct legacy ``run_h*`` / ``run_fh*`` callers keep their current
contract. Task 4 will add the guard call sites.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from sniper.analysis.wallet_protocol.types import UnderpoweredReason


HYPOTHESIS_STRUCTURE_CLASS: dict[str, str] = {
    "H1": "structure_tolerant",
    "H2": "binary_outcome_required",
    "H2_sub080": "binary_outcome_required",
    "H3": "binary_outcome_required",
    "H4": "token_price_only",
    "H5": "binary_outcome_required",
    "H6": "token_price_only",
    "FH1": "token_price_only",
    "FH2": "token_price_only",
    "FH3": "token_price_only",
    "FH4": "token_price_only",
    "FH5": "structure_tolerant",
    "FH6": "token_price_only",
}


def market_structure_supported(
    hypothesis_id: str, rows: Sequence[Mapping]
) -> tuple[bool, UnderpoweredReason | None]:
    """Return whether `hypothesis_id` can run on `rows` of any structure.

    Returns ``(True, None)`` when supported. Returns
    ``(False, <UnderpoweredReason>)`` when not — the reason is one of the
    three Slice 5 structure-guard values defined on
    :data:`sniper.analysis.wallet_protocol.types.UnderpoweredReason`.

    Raises:
        ValueError: ``hypothesis_id`` is not in
            :data:`HYPOTHESIS_STRUCTURE_CLASS`. Unknown ids are caller bugs;
            the guard surface stays exhaustive over the 13 known result
            surfaces.
    """
    try:
        cls = HYPOTHESIS_STRUCTURE_CLASS[hypothesis_id]
    except KeyError as exc:
        raise ValueError(
            f"unknown hypothesis_id {hypothesis_id!r} — not in "
            "HYPOTHESIS_STRUCTURE_CLASS"
        ) from exc

    if cls == "structure_tolerant":
        return True, None

    if cls == "token_price_only":
        for row in rows:
            token_id = row.get("token_id")
            if token_id:
                return True, None
        return False, "no_token_history"

    if cls == "binary_outcome_required":
        binary_rows = [
            row for row in rows if row.get("market_structure") == "binary"
        ]
        if not binary_rows:
            return False, "market_structure_unsupported"
        for row in binary_rows:
            if row.get("outcome_index") is None:
                return False, "outcome_index_missing"
        return True, None

    # Defensive: unreachable if HYPOTHESIS_STRUCTURE_CLASS values stay in
    # the three documented classes. Surface as ValueError so a future
    # typo in the table fails loudly rather than silently passing through.
    raise ValueError(f"unknown structure class {cls!r} for hypothesis_id {hypothesis_id!r}")


__all__ = [
    "HYPOTHESIS_STRUCTURE_CLASS",
    "market_structure_supported",
]
