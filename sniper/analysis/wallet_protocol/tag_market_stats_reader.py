"""Phase-3 minimal read helpers over ``tag_market_stats``.

The table is populated by Workstream D (Codex) — see Phase 3 plan § 0.2
and § A11. This module is the Claude-side reader. It carries **no**
analyzer-behavior obligations in Phase 3:

- ``tag_market_stats_unavailable`` emission belongs to the Layer 3
  ``market_selection_edge`` heuristic in
  ``wallet_archetype_extractor``, which is itself stubbed today
  (migration plan § A.2). Wiring the emission lands in Phase 4 along
  with the stub-fill.
- ``tag_market_stats`` is per-tag *market metadata* (n_markets,
  yes_share, volume, co-tags). It is **not** the per-(wallet, tag)
  peer baseline — H5/FH4 baselines flow through ``peer_baseline_fn``
  injected into ``run_held_branch`` and read ``wallet_tag_pnl`` via
  ``db.peer_baseline_for_grain``. The two readers are separate.

The helpers return ``None`` / ``False`` against an empty table so
they're independent of D's merge order.
"""
from __future__ import annotations

import sqlite3
from typing import Any


def is_tag_market_stats_available(conn: sqlite3.Connection, tag: str) -> bool:
    """True iff a row for ``tag`` exists in ``tag_market_stats``."""
    row = conn.execute(
        "SELECT 1 FROM tag_market_stats WHERE tag = ? LIMIT 1", (tag,),
    ).fetchone()
    return row is not None


def fetch_tag_market_stats(
    conn: sqlite3.Connection, tag: str,
) -> dict[str, Any] | None:
    """Return the row dict for ``tag`` or ``None`` if absent."""
    row = conn.execute(
        "SELECT * FROM tag_market_stats WHERE tag = ?", (tag,),
    ).fetchone()
    return dict(row) if row else None


__all__ = [
    "is_tag_market_stats_available",
    "fetch_tag_market_stats",
]
