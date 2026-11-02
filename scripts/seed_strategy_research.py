#!/usr/bin/env python3
"""Seed the research layer with the current observed edges and anti-edges."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import get_connection, init_db, upsert_strategy_research_note


def _paper_trade_sample_size(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM paper_trades
        WHERE signal_type != 'manual_copy'
          AND status != 'open'
        """
    ).fetchone()
    return int(row[0] or 0)


def main() -> None:
    conn = get_connection()
    init_db(conn)

    automated_closed = _paper_trade_sample_size(conn)

    notes = [
        {
            "slug": "paper-insider-conviction-gate",
            "title": "Paper Insider Conviction Gate",
            "category": "paper_entry",
            "hypothesis": (
                "The current paper edge comes from only entering positions that clear the "
                "numeric conviction gates: price room to run, position insider score, "
                "wallet qualifying ROI, speculator cap, and reasonable time to resolution."
            ),
            "edge_summary": (
                "This is the core copy-trading edge the repo has evidence for. It is not a "
                "classification edge. The label helps route, but the entry gate is numeric."
            ),
            "entry_logic": (
                "Enter only when price <= 0.70, insider score >= 55, speculator score < 70, "
                "qualifying_roi >= 10 or insider >= 90 if qROI is null, and resolution is "
                "within the paper horizon."
            ),
            "exit_logic": (
                "Primary exit is source-wallet exit. Secondary exits are resolution thresholds "
                "and stale-close protection."
            ),
            "risk_notes": (
                "Do not keep changing this gate while using paper performance as the benchmark. "
                "If the gate changes, the benchmark resets."
            ),
            "evidence": (
                "Backed by the current automated paper-trade sample in paper_trades. Manual-copy "
                "rows should be excluded from evaluating this edge."
            ),
            "sample_size": automated_closed,
            "tags": ["paper", "entry-gates", "numeric-edge"],
        },
        {
            "slug": "arb-fund-hedged-scanner",
            "title": "Arb Fund / Hedged Scanner",
            "category": "wallet_archetype",
            "hypothesis": (
                "Some wallets show real profitability but derive it from cross-market hedging, "
                "paired books, or price-inefficiency harvesting rather than directional information."
            ),
            "edge_summary": (
                "These wallets are worth studying for structure and market microstructure, but "
                "they are poor copy candidates for the paper/real trader because the copied leg "
                "does not preserve the original hedge."
            ),
            "entry_logic": (
                "Typical signs: both-sided exposure, repeated high-probability entries, broad "
                "market spray, and large notionals across closely related markets."
            ),
            "exit_logic": (
                "Often closes or rotates one leg while maintaining offsetting exposure elsewhere. "
                "A simple mirror bot cannot reconstruct the full book."
            ),
            "risk_notes": (
                "Do not interpret high wallet ROI alone as evidence this archetype should be copied."
            ),
            "evidence": (
                "Recent audits of 0xc8ab... and 0xb100... show exactly this pattern in public "
                "Polymarket activity and in low_buy_trades."
            ),
            "source_wallet": "0xc8ab97a9089a9ff7e6ef0688e6e591a066946418",
            "sample_size": 2,
            "tags": ["arb", "hedging", "anti-edge", "classification"],
        },
        {
            "slug": "wallet-audit-0xb100",
            "title": "Wallet Audit: 0xb100 (arb_fund)",
            "category": "wallet_audit",
            "hypothesis": (
                "0xb100 appears profitable (WR=80.53%, qROI=11.92%, 425 closed trades) but "
                "derives edge from cross-market hedging rather than directional information."
            ),
            "edge_summary": (
                "arb_fund classification is directionally correct. Despite decent win rate, the "
                "wallet generates low-quality copy signals (3 paper trades, all with zero signal "
                "scores via manual_copy). 37 position signals mostly position_low_signal."
            ),
            "entry_logic": "N/A — arb_fund wallets are excluded from copy-trading.",
            "exit_logic": "N/A",
            "risk_notes": (
                "High WR inflated by arb mechanics. Do not follow for directional copy-trades."
            ),
            "evidence": (
                "insider=37, spec=60, 1000 followed_wallet_activity records. "
                "3 paper trades (1 win +$3.13, 2 open), all manual_copy with zero signal scores."
            ),
            "source_wallet": "0xb10047d6a254b2ebb306d7a7d13bf59171ab6461",
            "sample_size": 3,
            "tags": ["wallet-audit", "arb_fund", "anti-edge"],
        },
        {
            "slug": "execution-bot-directional-conviction",
            "title": "Execution Bot With Directional Conviction",
            "category": "wallet_archetype",
            "hypothesis": (
                "Some high-frequency or bot-like wallets still carry genuine directional edge. "
                "The classification is useful, but the current single label hides the difference "
                "between harmless execution automation and uncopyable market making."
            ),
            "edge_summary": (
                "These wallets can still be valuable as references if they repeatedly enter early "
                "and resolve profitably, but they should be judged by signal quality and copyability, "
                "not by label alone."
            ),
            "entry_logic": (
                "Look for consistent early entries in liquid categories, repeat wins, and limited "
                "need for multi-leg hedging to preserve expectancy."
            ),
            "exit_logic": (
                "Needs independent study. Execution speed alone is not enough to make it uncopyable."
            ),
            "risk_notes": (
                "The current speculator_bot label is too blunt. Rename or split it before making "
                "hard policy decisions from it."
            ),
            "evidence": (
                "Wallet 0x84db... has extreme speculator metrics and a very strong historical win "
                "rate, which suggests the repo needs a better descriptive research layer."
            ),
            "source_wallet": "0x84dbb7103982e3617704a2ed7d5b39691952aeeb",
            "sample_size": 1,
            "tags": ["execution-bot", "classification", "copyability"],
        },
    ]

    for note in notes:
        upsert_strategy_research_note(conn, **note)

    count = conn.execute("SELECT COUNT(*) FROM strategy_research").fetchone()[0]
    print(f"Seeded strategy_research with {len(notes)} notes ({count} total rows present).")

    # Add 0x84db to watchlist for activity monitoring (research-monitored)
    import time
    wallet_84db = "0x84dbb7103982e3617704a2ed7d5b39691952aeeb"
    conn.execute(
        """INSERT INTO watchlist_wallets (wallet, label, added_ts)
           VALUES (?, ?, ?)
           ON CONFLICT(wallet) DO UPDATE SET label = excluded.label""",
        (wallet_84db, "research-monitored", int(time.time())),
    )
    conn.commit()
    print(f"Added {wallet_84db} to watchlist_wallets with label 'research-monitored'.")


if __name__ == "__main__":
    main()
