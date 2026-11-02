"""W6.1 — operator_decisions audit-overlay schema.

Records human review decisions on analyzer runs WITHOUT mutating
``wallet_layer5_decision``. Per the I3 SOP, the analyzer is the source
of truth for the verdict; operator decisions are an audit overlay
explaining what a human reviewer chose to do with that verdict (accept,
reject, or defer).

Schema invariants:

* Composite primary key (wallet, analyzer_run_id) — at most one decision
  per (wallet, run).
* CHECK constraint enforces decision ∈ {accept, reject, defer}.
* Mandatory: wallet, analyzer_run_id, decided_at_ts, decision.
* Defaults: reasoning='', operator='unknown' so the table can be
  populated without operator metadata when source attribution is
  unknown.

Spec ref: docs/superpowers/specs/2026-04-30-analyzer-completion-and-archive-design.md §I3.
Plan ref: docs/superpowers/plans/2026-04-30-analyzer-completion-phase-1.md W6.1.
"""

from __future__ import annotations

import sqlite3


def ensure_operator_decisions(conn: sqlite3.Connection) -> None:
    """Create the ``operator_decisions`` table if absent. Idempotent."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operator_decisions (
            wallet              TEXT    NOT NULL,
            analyzer_run_id     TEXT    NOT NULL,
            decided_at_ts       INTEGER NOT NULL,
            decision            TEXT    NOT NULL
                                CHECK (decision IN ('accept', 'reject', 'defer')),
            reasoning           TEXT    NOT NULL DEFAULT '',
            operator            TEXT    NOT NULL DEFAULT 'unknown',
            PRIMARY KEY (wallet, analyzer_run_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_op_dec_run
            ON operator_decisions(analyzer_run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_op_dec_wallet_ts
            ON operator_decisions(wallet, decided_at_ts DESC)
        """
    )


__all__ = ["ensure_operator_decisions"]
