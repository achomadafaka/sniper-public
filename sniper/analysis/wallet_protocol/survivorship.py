"""Survivorship surface — `survivorship_surface`.

Spec: docs/superpowers/specs/2026-05-22-survivorship-surface-design.md

Flags when a wallet was discovered via a winner-ranked list (leaderboard,
top-PnL), so no downstream reader treats a positive analyzer result as
unconditional. It DISCLOSES the discovery basis — it does not correct for
survivorship (that needs a loser cohort, out of scope).

HARD WALL — display-only. `compute_survivorship_surface` is read ONLY by
the report renderer. It MUST NEVER feed `overall_verdict`, `gate_result`,
`primary_edge_type`, `copyability_note`, branch/hypothesis logic, the
Strategy Pattern Audit, or append to `data_quality_flags`. Pure function —
no I/O.
"""
from __future__ import annotations

from typing import Any, Mapping

SURFACE_VERSION = 1

# Discovery-source substrings that indicate a winner-ranked list.
_WINNER_LIST_MARKERS = (
    "leaderboard", "top_pnl", "top-pnl", "winner", "top_wallet",
)

_NOTE = (
    "Survivorship surface is display-only — it does not change verdict, "
    "gate, edge type, copyability note, or quality flags. It flags only "
    "HOW the wallet was discovered; it cannot correct for survivorship, "
    "which would require a loser cohort (out of scope)."
)


def compute_survivorship_surface(triage_context: Any) -> dict:
    """Build the Survivorship surface from the discovery triage context.
    Pure, deterministic, display-only."""
    source: str | None = None
    if isinstance(triage_context, Mapping):
        raw = triage_context.get("source")
        if raw:
            source = str(raw)

    survivor_sampled: bool | None = None
    caveat: str | None = None
    if source is not None:
        low = source.lower()
        if any(marker in low for marker in _WINNER_LIST_MARKERS):
            # A winner-ranked discovery source — survivorship bias is
            # present. Never assert `False`: a non-winner-list source does
            # not prove discovery was unbiased, so the absence of a marker
            # leaves the status `None` (unknown), not `False`.
            survivor_sampled = True
            caveat = (
                f"SURVIVORSHIP_BIAS: discovered via {source} — a "
                "winner-ranked list. Wallets that ran a similar strategy "
                "and lost are not observed; the strategy class's base rate "
                "is unknown. Treat any positive read as conditioned on "
                "survival."
            )

    return {
        "surface_version": SURFACE_VERSION,
        "discovery_source": source,
        "survivor_sampled": survivor_sampled,
        "caveat": caveat,
        "note": _NOTE,
    }
