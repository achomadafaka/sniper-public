"""Desk `buyers` fact (cohort C prereg b90ddeb59 section 1a, Amendment 1 226c0144e).

Population: distinct signers of BUY swaps on the candidate pool with received_at in [pool create, receipt cut),
from our PumpSwap tape. The pool itself, the AMM program, the Mayhem agent and our own wallets are excluded and
counted (excluded_n). Every join is as of the cut:
  - address -> roster wallet: the latest address-registry event at or before the cut must be 'verified';
  - roster state: memecoin_desk_roster_history.state_at (30 s snapshot history, unknown reasons kept);
  - grade (real_pnl_grade: OUR copy PnL following that wallet, not the wallet's own skill): robot real solana closes with closed_at <= cut, this mint excluded; fewer than
    MIN_CLOSES = insufficient, else pos (sum > 0) or neg.
Counts are lower bounds (lower_bound true) unless the whole population window is proven captured. A failed
read is {"state": "unknown", "reason": ...}, never zeros. No network call; local stores only.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from sniper.services import memecoin_desk_ps_losses as L
from sniper.services import memecoin_desk_ps_tape as PT
from sniper.services import memecoin_desk_roster_history as R
from sniper.services import memecoin_desk_structure as S

VERSION = "desk-buyers-v1"
MIN_CLOSES = 5
MAX_SPAN_S = 3600.0           # population window cap (24 h measured p50 4.3 s on busy pools); older pool = lower bound
MAX_ROWS = 20000
PROOF_S = 3600.0             # = ps_facts LOOKBACK_S
AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
# Frozen definition (labels and tie rules). Its sha256 is committed before the first cohort C ticket.
DEFINITION = {"version": VERSION, "roster_labels": ["armed", "followed", "excluded", "state_unknown", "unknown"],
              "grade_labels": ["pos", "neg", "insufficient"], "min_closes": MIN_CLOSES,
              "grade_rule": "sum(realized_pnl) > 0 -> pos, else neg; closed_at <= cut; this mint excluded",
              "armed_rule": "real_approved=1 and 'solana' in real_chains and not paused and not excluded",
              "followed_rule": "on roster, not excluded, not armed (paused counts as followed)",
              "unknown_rule": "address not verified in the registry at the cut",
              "state_unknown_rule": "verified address whose roster state is unknowable at the cut",
              "roster_state_lag_s": R.LAG_S}
DEFINITION_SHA256 = hashlib.sha256(json.dumps(DEFINITION, sort_keys=True, separators=(",", ":"),
                                              ensure_ascii=False).encode("utf-8")).hexdigest()   # the one D rule


def _unknown(reason):
    return {"state": "unknown", "reason": reason, "version": VERSION}


def _epoch(iso):
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return None


def _verified_at(registry, cut):
    """{address: source_user_id} whose latest registry event at or before cut is 'verified'."""
    latest = {}
    for address, uid, status, at in registry:
        t = _epoch(at)
        if t is not None and t <= cut:
            latest[address] = (uid, status)
    return {a: uid for a, (uid, status) in latest.items() if status == "verified"}


def _grade(closes, mint, cut):
    rows = [pnl for closed_at, m, pnl in closes if m != mint and (_epoch(closed_at) or float("inf")) <= cut]
    if len(rows) < MIN_CLOSES:
        return "insufficient"
    return "pos" if sum(rows) > 0 else "neg"


def _roster_label(state):
    if state["excluded"]:
        return "excluded"
    chains = {c.strip() for c in (state["real_chains"] or "").split(",")}
    if state["real_approved"] and "solana" in chains and not state["paused"]:
        return "armed"
    return "followed"


def buyers_for(*, mint, pool, receipt_cut, cut_slot, sol_usd, pool_created_at, desk_dir, ledger, roster_store,
               ours=frozenset()):
    """The buyers fact at receipt_cut. pool_created_at: the pool's proven create block time or None.
    ledger: db_memecoin.desk_buyer_ledger(...) or an Exception it raised; roster_store: an open roster-history store."""
    if isinstance(ledger, Exception) or not isinstance(ledger, dict):
        return _unknown("ledger_unreadable")
    try:
        reason = R.history_reason(roster_store, receipt_cut)
    except sqlite3.Error:
        return _unknown("roster_unreadable")
    if reason:
        return _unknown(reason)          # the whole fact: no current-state join (prereg 1a)
    span_known = pool_created_at is not None and 0 <= receipt_cut - pool_created_at
    lo = max(pool_created_at, receipt_cut - MAX_SPAN_S) if span_known else receipt_cut - MAX_SPAN_S
    in_window = span_known and lo == pool_created_at
    try:
        # Exact population only from the pool's own create slot (no swap can precede it). Any other floor is a
        # heuristic, so the counts are a lower bound (Delta P2 on 2233b7723).
        create_slot = PT.pool_create_slot(desk_dir, pool, mint, receipt_cut, cut_slot) if in_window else None
        buys = PT.first_buys(desk_dir, pool, lo, receipt_cut, cut_slot, MAX_ROWS, floor_slot=create_slot)
    except (PT.TapeUnreadable, OSError, sqlite3.Error):
        return _unknown("tape_unreadable")
    if buys is None:
        return _unknown("tape_missing")
    # Completeness is proven only within PROOF_S (the loss/coverage readers' bound); an older window is a lower bound.
    covered, complete = 0.0, False
    if in_window and create_slot is not None and receipt_cut - lo <= PROOF_S:
        try:
            lost = L.losses(desk_dir, pool, lo, receipt_cut, cut_slot=cut_slot, max_rows=MAX_ROWS)
            intervals = L.pool_coverage(desk_dir, pool, lo, receipt_cut, max_rows=MAX_ROWS, cut_slot=cut_slot)
            covered = sum(max(0.0, min(b, receipt_cut) - max(a, lo)) for a, b in intervals)
            complete = covered >= receipt_cut - lo and not lost
        except (L.LossUnreadable, OSError, sqlite3.Error):
            complete = False       # counts stay valid lower bounds
    excluded = frozenset(ours) | S.AGENTS | {pool, AMM_PROGRAM}
    dropped = {a for a in buys if a in excluded}
    first_buy = {a: t for a, t in buys.items() if a not in excluded}
    verified = _verified_at(ledger["registry"], receipt_cut)
    roster = dict.fromkeys(DEFINITION["roster_labels"], 0)
    grades = dict.fromkeys(DEFINITION["grade_labels"], 0)
    known_first = []
    try:
        for actor, at in first_buy.items():
            uid = verified.get(actor)
            if uid is None:
                roster["unknown"] += 1
                continue
            state, why = R.state_at(roster_store, uid, receipt_cut)
            if state is None:
                roster["state_unknown" if why != "not_on_roster" else "unknown"] += 1
                continue
            label = _roster_label(state)
            roster[label] += 1
            grades[_grade(ledger["closes"].get(uid, ()), mint, receipt_cut)] += 1
            if label != "excluded":
                known_first.append(at)
    except sqlite3.Error:
        return _unknown("roster_unreadable")
    known = roster["armed"] + roster["followed"]
    return {"state": "known", "version": VERSION, "definition_sha256": DEFINITION_SHA256,
            "buyers_n": len(first_buy), "excluded_n": len(dropped), "followed_n": known, "known_n": known,
            "graded_n": grades["pos"] + grades["neg"], "roster": roster, "real_pnl_grade": grades, "grade_hist": dict(grades),
            # Amendment 4 fcd5cb3af: from the create BLOCK time (host-clock receipts minus block time), or from the window start when the pool is older than the cap
            "first_seen_followed_s": (min(known_first) - lo if known_first and span_known else None),
            "first_seen_basis": ("create_block_time" if lo == pool_created_at else "window_start") if span_known else None,
            "lower_bound": not complete, "covered_s": covered, "window_s": receipt_cut - lo,
            "roster_verified_n": len(set(verified.values())), "roster_state_lag_s": R.LAG_S}
