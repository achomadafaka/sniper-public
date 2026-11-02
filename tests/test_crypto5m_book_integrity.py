"""Book integrity: compare reconstructed book vs a fresh REST full book; quarantine divergence."""
from __future__ import annotations

import os

from sniper.algos.polymarket_crypto5m import book_integrity as BI
from sniper.algos.polymarket_crypto5m import persistence as P
from sniper.algos.polymarket_crypto5m.book_state import BookState


def _book(bids, asks):
    b = BookState(token_id="t", condition_id="c", side="Up")
    b.apply_book(bids=bids, asks=asks)
    return b


def test_identical_books_match():
    recon = _book([["0.30", "100"], ["0.29", "50"]], [["0.40", "10"]])
    rest = {"bids": [["0.29", "50"], ["0.30", "100"]], "asks": [["0.40", "10"]]}  # different order
    out = BI.compare_books(recon, rest)
    assert out["match"] is True
    assert out["divergence_levels"] == 0
    assert out["best_bid_match"] and out["best_ask_match"]


def test_both_empty_is_indeterminate_not_a_match():
    recon = _book([], [])
    out = BI.compare_books(recon, {"bids": [], "asks": []})
    assert out["match"] is None and out["indeterminate"] is True   # no evidence != agreement


def test_size_mismatch_is_one_divergent_level():
    recon = _book([["0.30", "100"]], [["0.40", "10"]])
    rest = {"bids": [["0.30", "80"]], "asks": [["0.40", "10"]]}   # size differs at 0.30
    out = BI.compare_books(recon, rest)
    assert out["match"] is False
    assert out["divergence_levels"] == 1


def test_missing_level_counts_and_best_divergence():
    recon = _book([["0.30", "100"]], [["0.40", "10"]])
    rest = {"bids": [["0.31", "100"], ["0.30", "100"]], "asks": [["0.40", "10"]]}  # extra better bid
    out = BI.compare_books(recon, rest)
    assert out["divergence_levels"] == 1            # the 0.31 level present only in REST
    assert out["best_bid_match"] is False           # best bid diverges (0.30 vs 0.31)


def test_should_quarantine_only_on_best_price_mismatch_not_interior_movement():
    # 2026-06-21: quarantine must reflect a top-of-book (decision-relevant) reconstruction failure, NOT
    # benign interior-level movement between the WS state and a later REST refetch. Interior divergence
    # alone (best bid/ask still match) must NOT quarantine; a best-price mismatch MUST.
    interior_only = _book([["0.30", "100"], ["0.29", "50"]], [["0.40", "10"]])
    rest_interior = {"bids": [["0.30", "100"], ["0.28", "999"]], "asks": [["0.40", "10"]]}  # only L2 moved
    cmp_interior = BI.compare_books(interior_only, rest_interior)
    assert cmp_interior["best_bid_match"] and cmp_interior["best_ask_match"]
    assert cmp_interior["divergence_levels"] > 0
    assert BI.should_quarantine(cmp_interior) is False         # interior move alone -> not quarantined

    best_moved = _book([["0.30", "100"]], [["0.40", "10"]])
    rest_best = {"bids": [["0.31", "100"], ["0.30", "100"]], "asks": [["0.40", "10"]]}  # best bid wrong
    cmp_best = BI.compare_books(best_moved, rest_best)
    assert BI.should_quarantine(cmp_best) is True              # best-price mismatch -> quarantined

    indeterminate = BI.compare_books(_book([], []), {"bids": [], "asks": []})
    assert BI.should_quarantine(indeterminate) is False        # no evidence -> never quarantine


def test_persist_reconciliation_and_quarantine(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "bi.sqlite"))
    P.ensure_schema(conn)
    P.record_book_reconciliation(conn, condition_id="c", token_id="t", book_state_match=False,
                                 book_divergence_levels=2, book_divergence_duration_ms=1500,
                                 rest_reconciliation_ts=1781947200, quarantined_interval=True,
                                 code_version="v1", now_ts=1781947201)
    row = conn.execute("SELECT * FROM crypto5m_book_reconciliation").fetchone()
    assert row["book_state_match"] == 0 and row["book_divergence_levels"] == 2
    assert row["quarantined_interval"] == 1
