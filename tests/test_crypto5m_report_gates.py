"""Phase 1B review fix (Codex #1): build_report must not over-clear Phase-0 readiness. It must enforce
the spec gates — >=200 resolved windows, a two-stage (cal/holdout) boundary lock, >=95% dual-book
coverage, and that book reconciliation was actually performed — not just '>=100 discovered + 1 resolved
+ best-rate>=0.99'. A tiny populated DB must still show blockers.
"""
import os

from sniper.algos.polymarket_crypto5m import persistence as P
from sniper.algos.polymarket_crypto5m import report as R


def _conn(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "rep.sqlite"))
    P.ensure_schema(conn)
    return conn


def _seed(conn, *, n_resolved, books_for=None, with_reconciliation=True, rule="last_at_or_before",
          match=True):
    """Seed n resolved windows directly (minimal columns the report reads). books_for = how many of
    those windows get BOTH up+down book snapshots (defaults to all)."""
    books_for = n_resolved if books_for is None else books_for
    for i in range(n_resolved):
        cid = f"0x{i:06x}"
        up, dn = f"{cid}-U", f"{cid}-D"
        conn.execute("INSERT INTO crypto5m_markets (condition_id, up_token_id, down_token_id, "
                     "window_start_ts, price_to_beat) VALUES (?,?,?,?,?)",
                     (cid, up, dn, 1000 + i, 100.0))
        conn.execute("INSERT INTO crypto5m_settlements (condition_id, winning_outcome, "
                     "reconciliation_status) VALUES (?,?,?)", (cid, "Up", "gamma_only"))
        conn.execute("INSERT INTO crypto5m_price_to_beat (condition_id, candidate_rule, abs_error, "
                     "gamma_oracle) VALUES (?,?,?,?)", (cid, rule, 0.0 if match else 9.9, 100.0))
        if i < books_for:
            recv = 1000 + i                      # at window start -> available at every checkpoint
            conn.execute("INSERT INTO crypto5m_book_snapshots (condition_id, token_id, recv_ts) "
                         "VALUES (?,?,?)", (cid, up, recv))
            conn.execute("INSERT INTO crypto5m_book_snapshots (condition_id, token_id, recv_ts) "
                         "VALUES (?,?,?)", (cid, dn, recv))
    if with_reconciliation:
        conn.execute("INSERT INTO crypto5m_book_reconciliation (condition_id, token_id, "
                     "book_state_match) VALUES (?,?,?)", ("0x000000-U", "0x000000-U", 1))
    conn.commit()


def test_one_resolved_window_does_not_clear_gates(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, n_resolved=1)
    rep = R.build_report(conn)
    assert rep["blockers"]                                   # NOT clear
    assert any("200" in b for b in rep["blockers"])          # 200-window floor cited


def test_two_stage_calibration_is_used_not_adhoc_best_rate(tmp_path):
    # 1 window with a 100% match would clear the OLD ad-hoc best-rate gate; two_stage needs 200 usable.
    conn = _conn(tmp_path)
    _seed(conn, n_resolved=1)
    rep = R.build_report(conn)
    assert rep["boundary_two_stage"]["status"] == "insufficient_windows"
    assert any("boundary" in b.lower() for b in rep["blockers"])


def test_full_gates_clear_with_200_resolved_and_calibration(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, n_resolved=200)
    rep = R.build_report(conn)
    assert rep["boundary_two_stage"]["status"] == "locked"
    assert rep["blockers"] == []                             # all gates satisfied
    assert any("LOCK" in r for r in rep["ready"])            # recommends locking the boundary rule


def test_missing_book_reconciliation_blocks(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, n_resolved=200, with_reconciliation=False)
    rep = R.build_report(conn)
    assert any("reconcil" in b.lower() for b in rep["blockers"])


def test_low_dual_book_coverage_blocks(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, n_resolved=200, books_for=100)               # only 50% have both books
    rep = R.build_report(conn)
    assert any("dual-book" in b.lower() or "book coverage" in b.lower() for b in rep["blockers"])
