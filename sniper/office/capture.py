"""Office Task 8: outcome capture. For every eligible candidate, a BOUNDED forward price path with a typed path state, so SKIPs, vetoes and uncovered coins can be graded under the same fill model
and the censored share is measurable by reason (Gamma prereg: censored share above 20% = no verdict). Read-only on the PumpSwap tape; indexed bounded reads only (operator order 2026-10-28).
State is typed, never inferred from an empty answer:
  pending     the horizon has not matured (not censored, not in any denominator yet)
  no_pool     the candidate has no PumpSwap pool: no exact path exists (older / non-graduated coins)
  source_off  the tape has no rows at all in the window: a dead source is never a quiet pool
  path_gap    capture coverage does not span the window (frontier short or unreadable), so a quiet stretch cannot be told from a hole
  observed    the source covered the window; rows may be empty (reason no_swaps: the pool did not trade; NOT a flat price, the grader must treat it as unpriced)
Window reads use the exact received_at index range with a hard row cap across all pools. No slot monotonicity is assumed. t_from and now are host receipt clocks."""
import sqlite3

ROWS_CAP = 200_000
COLS = "received_at, slot, ordinal, signature, is_buy, post_base_reserve_atomic, post_quote_reserve_atomic, lp_fee_bps, protocol_fee_bps, creator_fee_bps"
KEYS = ("received_at", "slot", "ordinal", "signature", "is_buy", "post_base", "post_quote", "lp_fee_bps", "protocol_fee_bps", "creator_fee_bps")


def _covered(tape, lo, hi):
    """None = coverage unreadable; True/False = the capture frontiers (merged) span [lo, hi]. through_at is the committed frontier: never extrapolated."""
    try:
        ivs = sorted(tape.execute("SELECT start_at, through_at FROM ps_capture_intervals WHERE through_at >= ? AND start_at <= ?", (lo, hi)).fetchall())
    except sqlite3.Error:
        return None
    if not ivs:
        return None if tape.execute("SELECT 1 FROM ps_capture_intervals LIMIT 1").fetchone() is None else False
    reach = lo
    for a, b in ivs:
        if a > reach:
            return False
        reach = max(reach, b)
    return reach >= hi


def read_forward_path(tape, pool, t_from, horizon_s, *, now):
    """-> {"state", "reason"?, "rows": [dict]} for the pool over [t_from, t_from + horizon_s)."""
    hi = t_from + horizon_s
    if now < hi:
        return {"state": "pending", "rows": []}
    if not pool:
        return {"state": "no_pool", "rows": []}
    rows = tape.execute("SELECT pool,%s FROM swaps_ps WHERE received_at >= ? AND received_at < ? ORDER BY received_at LIMIT ?" % COLS,
                        (t_from, hi, ROWS_CAP + 1)).fetchall()
    if len(rows) > ROWS_CAP:
        return {"state": "path_gap", "reason": "rows_cap", "rows": []}
    if not rows:
        return {"state": "source_off", "rows": []}
    cov = _covered(tape, t_from, hi)
    if cov is None:
        return {"state": "path_gap", "reason": "coverage_unknown", "rows": []}
    if cov is False:
        return {"state": "path_gap", "reason": "capture_frontier_short", "rows": []}
    rows = sorted((r[1:] for r in rows if r[0] == pool), key=lambda r: (r[0], r[1], r[2]))
    out = {"state": "observed", "rows": [dict(zip(KEYS, r)) for r in rows]}
    if not rows:
        out["reason"] = "no_swaps"
    return out


def censored_share(records):
    """records: [{"entered": bool, "state": str}] -> censored share overall and for entered vs skipped, with reasons. `pending` and an observed path are not censored; pending is excluded from the denominator and counted."""
    def block(rs):
        rs = [r for r in rs if r["state"] != "pending"]
        by = {}
        for r in rs:
            if r["state"] != "observed":
                by[r["state"]] = by.get(r["state"], 0) + 1
        n, c = len(rs), sum(by.values())
        return {"n": n, "censored": c, "share": (c / n) if n else None, "by_reason": by}
    out = {"entered": block([r for r in records if r["entered"]]), "skipped": block([r for r in records if not r["entered"]]), "all": block(records)}
    out["pending"] = sum(1 for r in records if r["state"] == "pending")
    return out


def _number(value, name):
    import math
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError('invalid_' + name)
    return value


def require_opportunity(conn, epoch_id, network_id, mint):
    """Agent adapters must call this before releasing a candidate snapshot."""
    import json
    row = conn.execute('SELECT payload_json FROM opportunity_log WHERE epoch_id=? AND network_id=? AND mint=?',
                       (epoch_id, network_id, mint)).fetchone()
    if row is None:
        raise ValueError('not_in_opportunity_log')
    return json.loads(row[0])


def log_opportunity(conn, row):
    """Persist a deterministic producer's validated eligibility, retaining its first cut."""
    from sniper.office.store import append, write_transaction
    fields = ('epoch_id', 'network_id', 'mint', 'shift_id', 'sheet_hash', 'snapshot_hash',
              'clock_sample_id', 'hard_filter_version', 'reason')
    for name in fields:
        if not isinstance(row.get(name), str) or not row[name].strip():
            raise ValueError('invalid_' + name)
    for name in ('first_eligible_at', 'receipt_cut', 'knowledge_cut'):
        _number(row.get(name), name)
    if type(row.get('slot_cut')) is not int or row['slot_cut'] < 0:
        raise ValueError('invalid_slot_cut')
    if row.get('eligible') is not True:
        raise ValueError('opportunity_must_be_eligible')
    if row['knowledge_cut'] > row['first_eligible_at'] or row['receipt_cut'] > row['first_eligible_at']:
        raise ValueError('future_opportunity_cut')
    with write_transaction(conn):
        try:
            return require_opportunity(conn, row['epoch_id'], row['network_id'], row['mint'])
        except ValueError as exc:
            if str(exc) != 'not_in_opportunity_log':
                raise
        append(conn, 'opportunity_log', row)
        return dict(row)


def record_gap(conn, *, epoch_id, source, from_at, to_at, state):
    from sniper.office.store import append
    if state not in ('observed', 'degraded', 'unknown', 'off'):
        raise ValueError('invalid_source_state')
    if not isinstance(epoch_id, str) or not epoch_id or not isinstance(source, str) or not source:
        raise ValueError('invalid_gap_identity')
    if _number(to_at, 'to_at') < _number(from_at, 'from_at'):
        raise ValueError('invalid_gap_interval')
    return append(conn, 'opportunity_gaps', dict(epoch_id=epoch_id, source=source,
                  from_at=from_at, to_at=to_at, state=state))


def record_capture(evaluation, *, candidate_seq, epoch_id, look_id, observation):
    """Keep the exact bounded path in evaluator custody; a frozen look never changes."""
    import hashlib
    import json
    from sniper.office.store import append, canonical_json, write_transaction
    purpose = evaluation.execute('SELECT purpose FROM _store_info WHERE id=1').fetchone()
    if purpose is None or purpose[0] != 'evaluation':
        raise ValueError('private_evaluation_store_required')
    if type(candidate_seq) is not int or candidate_seq < 1 or not isinstance(epoch_id, str) or not epoch_id or not isinstance(look_id, str) or not look_id:
        raise ValueError('invalid_capture_identity')
    if not isinstance(observation, dict) or observation.get('state') not in ('pending', 'observed', 'no_pool', 'source_off', 'path_gap') or not isinstance(observation.get('rows'), list) or len(observation['rows']) > ROWS_CAP:
        raise ValueError('invalid_capture')
    digest = hashlib.sha256(canonical_json(observation)).hexdigest()
    with write_transaction(evaluation):
        prior = evaluation.execute("SELECT payload_json,row_hash FROM outcome_captures WHERE candidate_seq=? AND json_extract(payload_json,'$.look_id')=? ORDER BY seq DESC LIMIT 1", (candidate_seq, look_id)).fetchone()
        if prior:
            row = json.loads(prior[0])
            if row['capture_hash'] != digest or row['epoch_id'] != epoch_id:
                raise ValueError('frozen_capture_changed')
            return prior[1]
        return append(evaluation, 'outcome_captures', dict(candidate_seq=candidate_seq,
                      epoch_id=epoch_id, look_id=look_id, capture_hash=digest, observation=observation))


def record_coverage(conn, *, epoch_id, source, from_at, to_at, proof_hash):
    """Observed coverage requires an existing successful host poll receipt, never an empty gaps table."""
    import json
    from sniper.office.store import append, write_transaction
    if not isinstance(epoch_id, str) or not epoch_id or not isinstance(source, str) or not source:
        raise ValueError('invalid_coverage_identity')
    if _number(to_at, 'to_at') <= _number(from_at, 'from_at'):
        raise ValueError('invalid_coverage_interval')
    with write_transaction(conn):
        coverage_proof(conn, source=source, from_at=from_at, to_at=to_at, proof_hash=proof_hash)
        return append(conn, 'opportunity_coverage', dict(epoch_id=epoch_id, source=source, from_at=from_at,
                      to_at=to_at, state='observed', proof_hash=proof_hash))


def coverage_proof(conn, *, source, from_at, to_at, proof_hash, max_seq=None):
    """Resolve the poll receipt inside the caller's audited prefix when supplied."""
    import json
    sql = 'SELECT payload_json FROM receipts WHERE row_hash=?'
    args = [proof_hash]
    if max_seq is not None:
        sql += ' AND seq<=?'
        args.append(max_seq)
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise ValueError('coverage_proof_missing')
    proof = json.loads(row[0])
    if proof.get('kind') != 'eligible_source_poll' or proof.get('source') != source or proof.get('state') != 'observed' or not _number(proof.get('from_at'), 'proof_from_at') <= from_at < to_at <= _number(proof.get('to_at'), 'proof_to_at') <= _number(proof.get('known_at'), 'proof_known_at'):
        raise ValueError('coverage_proof_mismatch')
    return proof
