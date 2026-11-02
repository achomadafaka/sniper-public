"""Phase-0 capture report — what was discovered/captured and what blocks the taker paper simulator.

Reads only the local capture DB. No edge claim, no PnL — this reports data health against the
Phase-0 gates in the spec.
"""
from __future__ import annotations

from decimal import Decimal

from . import calibration as CAL
from . import persistence as P
from . import price_to_beat_resolver as P2B
from .features import CHECKPOINTS

# gates (mirror the spec)
GATE_MIN_WINDOWS = 100                  # discovery floor (early signal)
GATE_MIN_RESOLVED_WINDOWS = 200         # cal(100)+holdout(100) need 200 usable resolved windows
GATE_MIN_BOUNDARY_MATCH_RATE = 0.99
GATE_MIN_DUAL_BOOK_COVERAGE = 0.95


def _calibration_rows(conn) -> list:
    """Rebuild per-window boundary comparison rows for ``calibration.two_stage`` from
    crypto5m_price_to_beat (one ``{rule: {abs_error, missing}}`` dict per window, with window_start_ts for
    the out-of-time split). abs_error is carried as ``Decimal(str(..))`` so float repr can't perturb the
    precision compare."""
    rows = conn.execute(
        "SELECT p.condition_id, p.candidate_rule, p.abs_error, m.window_start_ts "
        "FROM crypto5m_price_to_beat p JOIN crypto5m_markets m ON m.condition_id = p.condition_id "
        "WHERE p.gamma_oracle IS NOT NULL").fetchall()
    by_window = {}
    for r in rows:
        w = by_window.setdefault(r["condition_id"], {"window_start_ts": r["window_start_ts"]})
        ae = r["abs_error"]
        w[r["candidate_rule"]] = {"abs_error": (Decimal(str(ae)) if ae is not None else None),
                                  "missing": ae is None}
    return list(by_window.values())


def _dual_book_coverage(conn):
    """``(coverage, covered, total)`` — fraction of (resolved window × planned checkpoint) cells that have
    BOTH an up-token and a down-token book snapshot available AT the checkpoint (recv_ts <= decision_ts).
    Per-checkpoint coverage, not mere existence of one snapshot."""
    resolved = conn.execute(
        "SELECT m.window_start_ts, m.up_token_id, m.down_token_id FROM crypto5m_markets m "
        "JOIN crypto5m_settlements s ON s.condition_id = m.condition_id "
        "WHERE s.winning_outcome IS NOT NULL AND s.reconciliation_status != 'conflict'").fetchall()
    if not resolved:
        return 0.0, 0, 0
    total = covered = 0
    for m in resolved:
        for cp in CHECKPOINTS:
            decision = m["window_start_ts"] + cp
            total += 1
            up = conn.execute("SELECT 1 FROM crypto5m_book_snapshots WHERE token_id=? AND recv_ts<=? "
                              "LIMIT 1", (m["up_token_id"], decision)).fetchone()
            dn = conn.execute("SELECT 1 FROM crypto5m_book_snapshots WHERE token_id=? AND recv_ts<=? "
                              "LIMIT 1", (m["down_token_id"], decision)).fetchone()
            covered += 1 if (up and dn) else 0
    return covered / total, covered, total


def _two_stage_summary(ts: dict) -> dict:
    """JSON-safe compaction of ``calibration.two_stage`` (drops Decimal-keyed match-rate maps + the
    Decimal precision so the report can be jsonified by the API)."""
    return {
        "status": ts.get("status"),
        "selected_rule": ts.get("selected_rule"),
        "selected_precision": (str(ts["selected_precision"])
                               if ts.get("selected_precision") is not None else None),
        "holdout": ts.get("holdout"),
        "have_cal": ts.get("have_cal"),
        "have_holdout": ts.get("have_holdout"),
        "need_each": ts.get("need_each"),
    }


def build_report(conn) -> dict:
    c = P.counts(conn)
    total_markets = c["crypto5m_markets"]
    settlement_rows = c["crypto5m_settlements"]
    # the CLI records from_gamma() unconditionally, so active windows land rows with winning_outcome
    # NULL; only a decided, non-conflicted row is genuinely "resolved".
    resolved = conn.execute(
        "SELECT COUNT(*) FROM crypto5m_settlements "
        "WHERE winning_outcome IS NOT NULL AND reconciliation_status != 'conflict'").fetchone()[0]
    conflicts = conn.execute(
        "SELECT COUNT(*) FROM crypto5m_settlements "
        "WHERE reconciliation_status = 'conflict'").fetchone()[0]

    # priceToBeat oracle availability among stored markets
    p2b_present = conn.execute(
        "SELECT COUNT(*) FROM crypto5m_markets WHERE price_to_beat IS NOT NULL").fetchone()[0]

    # boundary-rule calibration status (best rule by match rate, if any rows scored)
    cal_rows = conn.execute(
        "SELECT candidate_rule, COUNT(*) n, "
        "SUM(CASE WHEN match=1 THEN 1 ELSE 0 END) matches "
        "FROM crypto5m_price_to_beat WHERE gamma_oracle IS NOT NULL GROUP BY candidate_rule"
    ).fetchall()
    calibration = {}
    best_rule, best_rate = None, -1.0
    for r in cal_rows:
        rate = (r["matches"] / r["n"]) if r["n"] else None
        calibration[r["candidate_rule"]] = {"n": r["n"], "match_rate": rate}
        if rate is not None and rate > best_rate:
            best_rule, best_rate = r["candidate_rule"], rate

    # Binance basis disagreement (Lane B), from stored reference observations is computed elsewhere;
    # here we just report coverage of the reference lane.
    binance_refs = c["crypto5m_reference_observations"]

    # the REAL boundary gate is the two-stage cal/holdout lock (not an in-sample best rate)
    two_stage = CAL.two_stage(_calibration_rows(conn))
    dual_cov, dual_both, dual_total = _dual_book_coverage(conn)
    reconciliations = c.get("crypto5m_book_reconciliation", 0)

    blockers, ready = [], []
    if total_markets < GATE_MIN_WINDOWS:
        blockers.append(f"need >={GATE_MIN_WINDOWS} windows discovered (have {total_markets})")
    if resolved < GATE_MIN_RESOLVED_WINDOWS:
        blockers.append(f"need >={GATE_MIN_RESOLVED_WINDOWS} resolved windows for cal/holdout "
                        f"(have {resolved})")
    if P2B.LOCKED_BOUNDARY_RULE is None:
        if two_stage["status"] == "locked":
            ready.append(f"boundary rule '{two_stage['selected_rule']}' clears two-stage "
                         f"(cal+holdout) — ready to LOCK")
        else:
            blockers.append(f"boundary rule not locked (two-stage status: {two_stage['status']})")
    if dual_total == 0 or dual_cov < GATE_MIN_DUAL_BOOK_COVERAGE:
        blockers.append(f"dual-book coverage {dual_cov:.2f} < {GATE_MIN_DUAL_BOOK_COVERAGE} "
                        f"({dual_both}/{dual_total} resolved windows have both books)")
    if reconciliations == 0:
        blockers.append("book reconciliation not performed (book divergence unmeasured)")

    return {
        "phase": 0,
        "tables": c,
        "markets": {"total": total_markets, "price_to_beat_present": p2b_present},
        "settlements": {"total": settlement_rows, "resolved": resolved, "conflicts": conflicts,
                        "unresolved": settlement_rows - resolved - conflicts},
        "chainlink_ticks": c["crypto5m_chainlink_ticks"],
        "book_snapshots": c["crypto5m_book_snapshots"],
        "book_events": c["crypto5m_book_events"],
        "detected_gaps": c["crypto5m_capture_gaps"],
        "identity_changes": c.get("crypto5m_identity_changes", 0),
        "book_reconciliations": c.get("crypto5m_book_reconciliation", 0),
        "binance_reference_observations": binance_refs,
        "boundary_calibration": calibration,
        "boundary_two_stage": _two_stage_summary(two_stage),
        "dual_book_coverage": {"coverage": dual_cov, "both": dual_both, "total": dual_total},
        "boundary_rule_locked": P2B.LOCKED_BOUNDARY_RULE,
        "storage": {"db_bytes": P.db_size_bytes(conn),
                    "total_rows": sum(c.values())},
        "ready": ready,
        "blockers": blockers,
    }


def format_report(rep: dict) -> str:
    lines = [
        "=== Polymarket crypto-5m — Phase 0 capture report ===",
        f"markets discovered : {rep['markets']['total']} "
        f"(priceToBeat oracle present: {rep['markets']['price_to_beat_present']})",
        f"settlements        : {rep['settlements']['total']} rows "
        f"({rep['settlements']['resolved']} resolved, {rep['settlements']['unresolved']} unresolved, "
        f"{rep['settlements'].get('conflicts', 0)} conflict)",
        f"chainlink ticks    : {rep['chainlink_ticks']}",
        f"book snapshots     : {rep['book_snapshots']}   book events: {rep['book_events']}",
        f"detected gaps      : {rep['detected_gaps']}   (detected != provably none — no seq number)",
        f"identity changes   : {rep['identity_changes']}   book reconciliations: {rep['book_reconciliations']}",
        f"binance refs (LaneB): {rep['binance_reference_observations']}",
        f"boundary rule locked: {rep['boundary_rule_locked']}",
        f"storage            : {rep['storage']['db_bytes'] / 1e6:.2f} MB, "
        f"{rep['storage']['total_rows']} rows",
    ]
    if rep["boundary_calibration"]:
        lines.append("boundary calibration (RTDS rule vs Gamma oracle):")
        for rule, st in rep["boundary_calibration"].items():
            lines.append(f"  - {rule}: n={st['n']} match_rate={st['match_rate']}")
    for r in rep.get("ready") or []:
        lines.append(f"ready              : {r}")
    lines.append("blockers before taker paper sim:")
    for b in rep["blockers"] or ["none"]:
        lines.append(f"  - {b}")
    return "\n".join(lines)
