"""Regression locks for leakage/integrity bugs in the live crypto5m runner scripts (Codex review
2026-06-21). The runners are thin orchestration; these tests pin the pure helpers that carry the
known-at-decision and fail-closed contracts.

  #1 forward paper: the Binance 'now' price MUST be as-of decision_ts, never a post-checkpoint sample.
  #2 quant shadow: Chainlink-from-capture MUST exclude ticks after the checkpoint (no replay look-ahead).
  #3/#4 quant shadow: the actionable side is fail-closed — blocked on degraded ensemble or non-coherent
       (False OR None) books, and ranked on the fee-EXCLUSIVE gross edge (fee is in conflict, never asserted).
"""
import sqlite3

import scripts.run_polymarket_crypto5m_forward_paper as FWD
import scripts.run_polymarket_crypto5m_quant_shadow as QS


def test_forward_decision_binance_prices_have_no_lookahead():
    decision_ts = 1000
    # buf holds a sample at 1004s — AFTER the 1000s checkpoint+latency. It must never be used.
    buf = [(940, 100.0), (970, 101.0), (1000, 102.0), (1004, 999.0)]
    now_px, ago_px = FWD.decision_binance_prices(buf, decision_ts, lookback_s=30)
    assert now_px == 102.0           # as-of decision_ts, NOT 999.0 (buf[-1], post-checkpoint leak)
    assert ago_px == 101.0           # as-of decision_ts - 30s (the 970 sample)


def test_chainlink_from_capture_excludes_post_checkpoint_ticks(tmp_path):
    db = str(tmp_path / "cap.sqlite")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE crypto5m_chainlink_ticks (value REAL, source_ts_ms INTEGER, "
              "recv_ts INTEGER, window_start_ts INTEGER)")
    ws = 1000
    c.execute("INSERT INTO crypto5m_chainlink_ticks VALUES (?,?,?,?)",
              (100.0, (ws + 100) * 1000, ws + 100, ws))            # known before the 120s checkpoint
    c.execute("INSERT INTO crypto5m_chainlink_ticks VALUES (?,?,?,?)",
              (999.0, (ws + 240) * 1000, ws + 240, ws))            # FUTURE (240s) — must be excluded
    c.commit()
    c.close()
    out = QS.chainlink_from_capture(ws, ws + 120, db_path=db)      # decide at the 120s checkpoint
    assert out is not None and out["price"] == 100.0              # NOT the future 999.0 tick


def test_chainlink_from_capture_excludes_late_arriving_tick(tmp_path):
    db = str(tmp_path / "cap.sqlite")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE crypto5m_chainlink_ticks (value REAL, source_ts_ms INTEGER, "
              "recv_ts INTEGER, window_start_ts INTEGER)")
    ws = 1000
    # sourced before the checkpoint but ARRIVED after it -> not known-at-decision, must be excluded
    c.execute("INSERT INTO crypto5m_chainlink_ticks VALUES (?,?,?,?)",
              (100.0, (ws + 100) * 1000, ws + 100, ws))
    c.execute("INSERT INTO crypto5m_chainlink_ticks VALUES (?,?,?,?)",
              (777.0, (ws + 119) * 1000, ws + 200, ws))            # recv_ts after the 120s checkpoint
    c.commit()
    c.close()
    out = QS.chainlink_from_capture(ws, ws + 120, db_path=db)
    assert out is not None and out["price"] == 100.0


def test_resolve_actionable_fail_closed_gate_and_gross_ranking():
    matrix = {"up_net_edge_gross": 0.05, "down_net_edge_gross": -0.02}
    # #4: a non-coherent book is None (invalid), not just False — BOTH must block
    assert QS.resolve_actionable(matrix, {"ensemble_degraded": False, "market_coherent": None}) \
        == (None, "incoherent_books")
    assert QS.resolve_actionable(matrix, {"ensemble_degraded": False, "market_coherent": False}) \
        == (None, "incoherent_books")
    # degraded ensemble blocks
    assert QS.resolve_actionable(matrix, {"ensemble_degraded": True, "market_coherent": True}) \
        == (None, "degraded_ensemble")
    # #3: coherent + a positive GROSS (fee-exclusive) edge -> actionable on that side
    assert QS.resolve_actionable(matrix, {"ensemble_degraded": False, "market_coherent": True}) \
        == ("Up", "edge")
    # coherent but no positive gross edge -> no actionable
    assert QS.resolve_actionable({"up_net_edge_gross": -0.05, "down_net_edge_gross": -0.02},
                                 {"ensemble_degraded": False, "market_coherent": True}) == (None, "no_edge")
