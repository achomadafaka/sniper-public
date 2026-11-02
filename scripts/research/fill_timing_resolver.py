#!/usr/bin/env python3
"""Fill timing resolver: one `memecoin_fill_timing` row per SUBMITTED funded entry (filled or failed,
with a transaction), with its source slot or block, our landing slot or block, the gap (S+n) and the
why-not-S+1 cause. Spec docs/superpowers/specs/2026-10-02-memecoin-fill-timing-design.md (rev 2),
section 2.4.

Run from cron every minute. Its own process, never the runner: the ledger is opened READ-ONLY (URI
mode=ro) and the only writes go to the timing file `data/memecoin_fill_timing.db`. What is stored
comes first (the attempt's trace, the detection's source_slot, the slot lane's attempt and skip row,
the EVM fast lanes' decisions); reads fill the rest only behind their knobs, both default OFF:
- MEMECOIN_FILL_TIMING_SOLANA_READS: ONE getSignatureStatuses per pass holding every pending Solana
  signature (ours and the sources'), on MEMECOIN_RESEARCH_HELIUS_RPC_URL only;
- MEMECOIN_FILL_TIMING_EVM_READS: missing blocks on the 4663 research QuickNode key, Base tenderly,
  or BSC's configured watcher receipt host; each host passes an eth_chainId check. BSC uses the
  existing short-timeout/cooldown reader and never custody. Held complete gaps spend no RPC.
States: pending (a read failed; retried), unread (a read is needed and its knob is off or the chain
has no host; reopened when it turns on), complete (the gap was measured), no_source (a manual entry:
no source to time), unsupported (the origin names no source transaction we can read), not_landed
(our signature still unknown to the chain 10 min after the attempt, with reads on), unknown (open
for 24 h). Never guessed, never 0.

    python3 scripts/research/fill_timing_resolver.py [--summary]
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sniper.services import memecoin_fill_timing as ft  # noqa: E402

WINDOW = timedelta(hours=24)
NOT_LANDED_AFTER = timedelta(minutes=10)
PASS_LIMIT = 500
SOLANA_BATCH = 256
TENDERLY_BASE = "https://base.gateway.tenderly.co"
_CURSOR = "attempts_completed_at"


def _iso(dt):
    return dt.isoformat()


def _parse(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _evm_block(evm_read, chain, tx, *, require_success=True):
    rec = evm_read(chain, "eth_getTransactionReceipt", [tx])
    if rec is None:
        return None                  # only RPC null is evidence that this transaction is unknown
    from sniper.services.memecoin_evm_post_trade import receipt_observation
    observation = receipt_observation(chain, tx, rec, require_success=require_success)
    if observation is None:
        raise ValueError("unbound or malformed timing receipt")
    return observation["source_block_number"]


def _evm_block_time(evm_read, chain, number):
    header = evm_read(chain, "eth_getBlockByNumber", [hex(int(number)), False])
    if not isinstance(header, dict) or header.get("timestamp") is None:
        return None
    return int(str(header["timestamp"]), 0)


def resolve_pass(ledger, timing, *, now, solana_read=None, evm_read=None, evm_chains=(), alert=None,
                 lane_conns=None, limit=PASS_LIMIT, max_seconds=50.0, clock=time.monotonic):
    """One pass. `solana_read(sigs) -> {sig: slot or None}` (None = the chain does not know it);
    `evm_read(chain, method, params) -> result` for chains in `evm_chains`. Either may raise: the
    rows it served stay pending. Returns a summary dict."""
    if not math.isfinite(max_seconds) or max_seconds < 0:
        raise ValueError("max_seconds must be finite and nonnegative")
    from sniper.services.memecoin_bsc_execution_observation import validate_pending_witnesses
    validate_pending_witnesses(ledger, timing, limit=limit)
    deadline = clock() + max_seconds
    limit = min(PASS_LIMIT, max(0, int(limit)))
    lane_conns = lane_conns or {}
    timing.row_factory = sqlite3.Row
    now_iso = _iso(now)
    cursor = (timing.execute("SELECT value FROM memecoin_fill_timing_cursor WHERE name=?", (_CURSOR,)).fetchone()
              or [None])[0] or _iso(now - WINDOW)
    ledger.row_factory = sqlite3.Row
    new = ledger.execute(
        "SELECT attempt_id, completed_at FROM memecoin_entry_attempts INDEXED BY idx_mc_entry_view_completed "
        "WHERE completed_at >= ? AND terminal_status IN ('filled', 'failed') AND tx_sig IS NOT NULL "
        "ORDER BY completed_at LIMIT ?", (cursor, limit)).fetchall()
    for row in new:
        timing.execute("INSERT OR IGNORE INTO memecoin_fill_timing (attempt_id, state, first_seen_at, updated_at) "
                       "VALUES (?, 'pending', ?, ?)", (row["attempt_id"], now_iso, now_iso))
    if new:
        timing.execute("INSERT OR REPLACE INTO memecoin_fill_timing_cursor VALUES (?, ?)",
                       (_CURSOR, new[-1]["completed_at"]))
    timing.commit()

    # R8 (Codex review 2026-10-06): 'complete' once meant "nothing more to read", even with no gap.
    # Such rows are revisited: they become complete only on a measured gap, else a typed state.
    timing.execute("UPDATE memecoin_fill_timing SET state='pending' WHERE attempt_id IN "
                   "(SELECT attempt_id FROM memecoin_fill_timing INDEXED BY idx_fill_timing_incomplete_completed "
                   "WHERE state='complete' AND gap IS NULL ORDER BY updated_at LIMIT ?)", (limit,))
    timing.commit()
    work = []
    stored_rows = []
    for state in ("pending", "unread"):
        stored_rows.extend(dict(x) for x in timing.execute(
            "SELECT * FROM memecoin_fill_timing INDEXED BY idx_fill_timing_state "
            "WHERE state=? ORDER BY updated_at LIMIT ?", (state, limit)))
    for stored in sorted(stored_rows, key=lambda row: (row.get("updated_at") or "", row["attempt_id"]))[:limit]:
        attempt = ft.one(ledger, "SELECT * FROM memecoin_entry_attempts WHERE attempt_id=?", (stored["attempt_id"],))
        if attempt is None:
            continue
        chain = str(attempt.get("network_id") or "")
        ref = ft.source_ref(attempt)
        det = ft.detection_of(ledger, chain, ref)
        ctx = {"stored": stored, "attempt": attempt, "chain": chain, "ref": ref, "det": det,
               "slot": ft.slot_attempt_of(ledger, chain, ref), "skip": ft.skip_of(timing, chain, ref),
               "fast": ft.fast_of(lane_conns, chain, ref, timing=timing, mint=attempt.get('mint'), ledger=ledger),
               "live": ft.lane_live_of(lane_conns, chain, ref, attempt.get("tx_sig")),
               "reads": 0, "asked": set(), "answered": {}}
        ctx["f"] = ft.facts(attempt, det, ctx["slot"], ctx["skip"], ctx["fast"], stored, live=ctx["live"])
        work.append(ctx)

    # Solana: ONE batched read for every missing signature (spec 2.4, decision B).
    wanted = {}
    for ctx in work:
        if ctx["chain"] != "solana":
            continue
        if ctx["f"]["source_slot"] is None and ctx["ref"]:
            wanted.setdefault(ctx["ref"], []).append(ctx)
        if ctx["f"]["landing_slot"] is None and ctx["attempt"].get("tx_sig"):
            wanted.setdefault(ctx["attempt"]["tx_sig"], []).append(ctx)
    answers, failed = {}, False
    if wanted and solana_read is not None and clock() < deadline:
        sigs = sorted(wanted)[:SOLANA_BATCH]
        try:
            answers = solana_read(sigs) or {}
        except Exception:
            failed = True
        for sig in sigs:
            for ctx in wanted[sig]:
                ctx["asked"].add(sig)
                if not failed and sig in answers:
                    ctx["answered"][sig] = answers[sig]
        if sigs:
            for ctx in {id(c): c for s in sigs for c in wanted[s]}.values():
                ctx["reads"] += 1

    summary = {"new": len(new), "open": len(work), "complete": 0, "unread": 0, "pending": 0,
               "not_landed": 0, "unknown": 0, "no_source": 0, "unsupported": 0, "reads": 0,
               "followups": 0}
    for ctx in work:
        stored, attempt, chain, f = ctx["stored"], ctx["attempt"], ctx["chain"], ctx["f"]
        upd = {}
        ours = attempt.get("tx_sig")
        if chain == "solana":
            if f["source_slot"] is None and ctx["answered"].get(ctx["ref"]) is not None:
                upd.update(source_slot=int(ctx["answered"][ctx["ref"]]), source_slot_basis="rpc")
            if f["landing_slot"] is None and ctx["answered"].get(ours) is not None:
                upd.update(landing_slot=int(ctx["answered"][ours]), landing_slot_basis="rpc")
        elif evm_read is not None and chain in evm_chains and ft.gap_of(f) is None:
            try:
                if f["source_slot"] is None and ctx["ref"] and clock() < deadline:
                    ctx["reads"] += 1
                    block = _evm_block(evm_read, chain, ctx["ref"])
                    if block is not None:
                        upd.update(source_slot=block, source_slot_basis="rpc")
                landing = f["landing_slot"]
                if landing is None and ours and clock() < deadline:
                    ctx["reads"] += 1
                    ctx["asked"].add(ours)
                    landing = _evm_block(evm_read, chain, ours, require_success=False)
                    ctx["answered"][ours] = landing
                    if landing is not None:
                        upd.update(landing_slot=landing, landing_slot_basis="rpc")
                if landing is not None and f["landing_time"] is None and clock() < deadline:
                    ctx["reads"] += 1
                    stamp = _evm_block_time(evm_read, chain, landing)
                    if stamp is not None:
                        upd["landing_time"] = stamp
            except Exception:
                pass
        merged = dict(stored, **upd)
        if f["source_time"] is not None and merged.get("source_time") is None:
            merged["source_time"] = f["source_time"]
        f = ft.facts(attempt, ctx["det"], ctx["slot"], ctx["skip"], ctx["fast"], merged, live=ctx["live"])
        gap = ft.gap_of(f)
        reader = solana_read if chain == "solana" else (evm_read if chain in evm_chains else None)
        first = _parse(stored.get("first_seen_at")) or now
        age = now - (_parse(attempt.get("completed_at")) or first)
        if gap is not None:
            state = "complete"                  # complete = the gap was measured, nothing less
        elif ctx["ref"] is None and f["landing_slot"] is not None:
            # No source transaction to time against: a manual entry has none (no_source); any
            # other origin names none we can read, a lowercased J7 id or an unknown format.
            state = "no_source" if attempt.get("signal_source") in ft.MANUAL_SOURCES else "unsupported"
        elif (f["landing_slot"] is None and ours in ctx["asked"] and ours in ctx["answered"]
              and ctx["answered"][ours] is None and age >= NOT_LANDED_AFTER):
            state = "not_landed"
        elif reader is None:
            state = "unread"
        else:
            state = "pending"
        if state in ("pending", "unread") and now - first >= WINDOW:
            state = "unknown"
        merged["state"] = state
        cause, detail = ft.classify(dict(f, state=state))
        reads = int(stored.get("reads") or 0) + ctx["reads"]
        summary[state] += 1
        summary["reads"] += ctx["reads"]
        followup = int(stored.get("followup_sent") or 0)
        if (state == "complete" and f["source_basis"] == "rpc" and not followup and alert is not None
                and attempt.get("terminal_status") == "filled"):
            label = attempt.get("ticker") or str(attempt.get("mint") or "")[:8]
            try:
                alert("TIMING %s %s %s" % (label, chain, ft.landed_text(f)))
                followup = 1
                summary["followups"] += 1
            except Exception:
                pass
        # Undispatched pending reads retain their old priority. Rotating the whole exhausted
        # page would put the same first attempt ahead of its untouched neighbours every pass.
        updated_at = (stored.get("updated_at") if state == "pending" and reader is not None
                      and ctx["reads"] == 0 else now_iso)
        timing.execute(
            "UPDATE memecoin_fill_timing SET network_id=?, lane=?, source_sig=?, source_slot=?, "
            "source_slot_basis=?, source_time=?, landing_slot=?, landing_slot_basis=?, landing_time=?, "
            "recv_slot=?, gap=?, gap_s=?, cause=?, cause_detail=?, stages_json=?, state=?, reads=?, "
            "updated_at=?, completed_at=?, followup_sent=? WHERE attempt_id=?",
            (chain, attempt.get("delivery_lane") or attempt.get("signal_source"), ctx["ref"],
             f["source_slot"], f["source_basis"], f["source_time"], f["landing_slot"], f["landing_basis"],
             f["landing_time"], f["recv_slot"], gap, f["gap_s"], cause, detail,
             json.dumps(ft.stages(f), sort_keys=True), state, reads, updated_at, attempt.get("completed_at"),
             followup, stored["attempt_id"]))
    timing.commit()
    return summary


# ---------------------------------------------------------------------- processed execution joins

def _execution_join(ledger, source, attempt):
    """Adapt actual persisted fields; raw signed identity is checked locally, never recovered."""
    import base64
    from solders.transaction import VersionedTransaction
    from sniper.execution.submission_transport import sha256_hex
    trace = ft._trace(attempt)
    original = trace.get('source_evidence')
    original = original if isinstance(original, dict) else {}
    proof = original.get('source_initiation')
    proof = proof if isinstance(proof, dict) else {}
    wallet = original.get('source_wallet') or proof.get('payer')
    if wallet != source.get('source_wallet') or ft.source_ref(attempt) != source.get('source_sig'):
        return None, None, None, 'source_identity_unknown'
    mint = attempt.get('mint')
    if source.get('mint') is not None and mint != source['mint']:
        return None, None, None, 'source_mint_mismatch'
    source = dict(source, mint=mint)
    confirmations = trace.get('submission_events')
    if not isinstance(confirmations, list) or any(not isinstance(ev, dict) for ev in confirmations):
        return source, None, None, 'confirmation_trace_unknown'
    landing = [ev.get('landing_slot') for ev in confirmations if ev.get('stage') == 'chain_confirmation']
    normalized = dict(attempt, source_sig=source['source_sig'], source_wallet=wallet,
                      landing_slot=landing[0] if len(landing) == 1 else None)
    pending = ft.one(ledger, 'SELECT * FROM memecoin_pending_entries WHERE id=?',
                     (attempt.get('pending_id'),))
    fill = ft.one(ledger, 'SELECT * FROM memecoin_fills WHERE id=?', (attempt.get('fill_id'),))
    position = ft.one(ledger, 'SELECT * FROM memecoin_positions WHERE id=?',
                      ((fill or {}).get('position_id'),))
    if not pending or not fill or not position:
        return source, normalized, (None, None, None), 'missing_join'
    sig = attempt.get('tx_sig')
    if (pending.get('tx_sig') != sig or fill.get('tx_sig') != sig
            or pending.get('attempt_id') != attempt['attempt_id']
            or fill.get('attempt_id') != attempt['attempt_id']
            or attempt.get('position_id') != position['id']
            or pending.get('mint') != mint or position.get('mint') != mint
            or any(row.get('network_id') != 'solana' for row in (attempt, pending, fill, position))
            or fill.get('kind') != 'entry' or fill.get('fill_source') != 'chain'
            or position.get('execution_mode') != 'real'):
        return source, normalized, (None, None, None), 'ledger_identity_mismatch'
    try:
        payload = pending['signed_payload_b64']
        if not isinstance(payload, str) or sha256_hex(payload) != pending['signed_payload_sha256']:
            raise ValueError('hash mismatch')
        tx = VersionedTransaction.from_bytes(base64.b64decode(payload, validate=True))
        if (not tx.signatures or str(tx.signatures[0]) != sig or not tx.message.account_keys
                or tx.message.header.num_required_signatures < 1
                or not all(tx.verify_with_results())):
            raise ValueError('signature mismatch')
        payer = str(tx.message.account_keys[0])
    except (ValueError, TypeError, KeyError, IndexError):
        return source, normalized, (None, None, None), 'signed_identity_unknown'
    evidence = [ev.get('evidence') for ev in (trace.get('substage_events') if isinstance(trace.get('substage_events'), list) else ())
                if isinstance(ev, dict) and ev.get('name') == 'receipt_execution_evidence']
    if (len(evidence) != 1 or not isinstance(evidence[0], dict)
            or evidence[0].get('state') != 'verified'):
        return source, normalized, (None, None, payer), 'receipt_atoms_unknown'
    raw = evidence[0]
    if (raw.get('tx_sig') != sig or raw.get('custody_pubkey') != payer
            or raw.get('mint') != mint or raw.get('network_id') != 'solana'):
        return source, normalized, (None, None, payer), 'receipt_identity_mismatch'
    pending_norm = dict(pending, pending_id=pending['id'], state=pending['status'], custody_pubkey=payer)
    fill_norm = dict(raw, pending_id=pending['id'], attempt_id=fill['attempt_id'], side='buy',
                     chain_confirmed=pending['status'] == 'confirmed', custody_pubkey=payer)
    return source, normalized, (pending_norm, fill_norm, payer), None


# Most rowids one execution pass may walk (bounded I/O per pass; see resolve_execution_pass).
RECEIPT_SCAN_WINDOW_ROWS = 20000


def resolve_execution_pass(ledger, timing, *, max_rows=512, max_seconds=5.0, now=None):
    """Bounded DB-only raw receipt page plus independently retried orphan joins.

    The pass distribution/fraction describes its exact page, never an implicit full-cohort verdict.
    Raw records remain append-only; cursor state only selects the next page/retry work.
    """
    from collections import Counter
    limit = min(512, max(0, int(max_rows)))
    budget = float(max_seconds)
    if not math.isfinite(budget) or budget < 0:
        raise ValueError('invalid execution resolver budget')
    deadline = time.monotonic() + budget
    summary = dict(scope='bounded_received_event_page', scanned=0, received_events=0, eligible=0,
        unknown=0, unknown_eligibility=0, independent_policy_refused=0, verified_sub200=0,
        complete_witnesses=0, p50_ms=None, p90_ms=None, verified_fraction=None,
        canonical_execution_time='unverified', results=[], source_runs=[], coverage=[], outcomes={})
    if not limit or time.monotonic() >= deadline:
        return summary
    cursor_name = 'execution_source_receipts_v1'
    value = timing.execute('SELECT value FROM memecoin_fill_timing_cursor WHERE name=?',
                           (cursor_name,)).fetchone()
    try:
        state = json.loads(value[0]) if value else {}
        cursor, retry = int(state.get('cursor', 0)), state.get('retry', [])
        if not isinstance(retry, list):
            retry = []
    except (ValueError, TypeError, AttributeError):
        state, cursor, retry = {}, 0, []
    def rows(sql, args):
        cur = timing.cursor()
        cur.row_factory = sqlite3.Row
        return [dict(row) for row in cur.execute(sql, args)]
    work, retained = [], []
    retry_count = min(len(retry), max(1, limit // 2))
    retried = {rowid for rowid in retry[:retry_count] if type(rowid) is int}
    for rowid in retry[:retry_count]:
        if type(rowid) is int:
            work.extend(rows('SELECT rowid AS receipt_rowid,* FROM memecoin_signal_receipts WHERE rowid=?',
                             (rowid,)))
    retained.extend(retry[retry_count:])
    # RECEIPT BLOAT (Gamma a61a5bb27, 2026-10-21). Empty decodes and pre-execution stamps are never
    # source receipts, so the page never selects them; one pass reads at most a bounded rowid
    # window, so a run of 877k empty rows cannot turn a page into a full-table scan; and the cursor
    # never returns to 0. Late joins stay covered by the retry list, which keeps every unknown row.
    frontier = timing.execute('SELECT MAX(rowid) FROM memecoin_signal_receipts').fetchone()[0] or 0
    start_cursor, room = cursor, limit - len(work)
    window_end = min(frontier, cursor + RECEIPT_SCAN_WINDOW_ROWS)
    new = rows('SELECT rowid AS receipt_rowid,* FROM memecoin_signal_receipts '
               'WHERE rowid>? AND rowid<=? AND decode_state NOT IN (?, ?) ORDER BY rowid LIMIT ?',
               (cursor, window_end, 'empty_decode_unknown', 'rabbit_pre_execution', room)) if room > 0 else []
    work.extend(new)
    if room > 0:
        cursor = new[-1]['receipt_rowid'] if len(new) == room else max(cursor, window_end)
    seen, runs, outcomes, times = set(), {}, Counter(), []
    for source in work:
        if time.monotonic() >= deadline:
            retained.append(source['receipt_rowid'])
            continue
        if source.get('decode_state') == 'rabbit_pre_execution':
            continue   # a pre-execution arrival stamp, never a source receipt (MEMECOIN_SLOT_LANE_RABBIT_SIGNAL)
        summary['scanned'] += 1
        run = (source['producer_run_id'], source.get('config_generation'), source.get('roster_generation'))
        runs[run] = dict(producer_run_id=run[0], config_generation=run[1], roster_generation=run[2])
        sig, wallet, mint = (source.get(key) for key in ('source_sig', 'source_wallet', 'mint'))
        cur = ledger.cursor()
        cur.row_factory = sqlite3.Row
        candidates = [dict(row) for row in cur.execute(
            'SELECT * FROM memecoin_entry_attempts INDEXED BY idx_mc_entry_attempt_origin '
            'WHERE origin_event_id IN (?,?) ORDER BY decision_started_at LIMIT 65',
            ('slotlane:' + str(sig or ''), 'onchain:' + str(sig or '')))] if sig else []
        matched = []
        for attempt in candidates[:64]:
            trace_source = ft._trace(attempt).get('source_evidence')
            trace_source = trace_source if isinstance(trace_source, dict) else {}
            proof = trace_source.get('source_initiation')
            proof = proof if isinstance(proof, dict) else {}
            owner = trace_source.get('source_wallet') or proof.get('payer')
            resolved_original = bool(source.get('pool') and trace_source.get('pool') == source['pool']
                and proof.get('mint') == attempt.get('mint')) and all(trace_source.get(field) == source.get(field)
                and source.get(field) is not None for field in
                ('producer_run_id', 'recv_monotonic', 'boot_id', 'clock_domain'))
            if (owner == wallet and wallet and
                    (attempt.get('mint') == mint if mint is not None else resolved_original)):
                matched.append(attempt)
        attempt = matched[0] if len(matched) == 1 and len(candidates) <= 64 else None
        if mint is None and attempt:
            mint = attempt.get('mint')
            source = dict(source, mint=mint)
        key = ((source.get('network_id'), sig, wallet, mint) if sig and wallet and mint else
               ('unresolved', source['producer_run_id'], source['sequence']))
        if key in seen:
            continue
        seen.add(key)
        summary['received_events'] += 1
        result = dict(network_id=source.get('network_id'), source_sig=sig, source_wallet=wallet,
                      mint=mint, producer_run_id=source['producer_run_id'], sequence=source['sequence'],
                      config_generation=source.get('config_generation'), roster_generation=source.get('roster_generation'),
                      feed=source.get('feed'), venue=source.get('venue'), decode_state=source.get('decode_state'),
                      state='unknown', reason='unattempted', observed_bound_ms=None, sub200=None,
                      canonical_execution_time='unverified', eligibility='unknown')
        if attempt:
            result['attempt_id'] = attempt['attempt_id']
            terminal = attempt.get('terminal_status') or 'unfinished'
            outcomes[terminal] += 1
            result['outcome'] = terminal
            if attempt.get('pending_id') is not None or terminal in ('filled', 'submitted_unknown'):
                result['eligibility'] = 'eligible'
                summary['eligible'] += 1
            original, normalized, joined, reason = _execution_join(ledger, source, attempt)
            if reason is None:
                pending, fill, payer = joined
                observed = rows('SELECT * FROM memecoin_processed_receipts INDEXED BY idx_processed_receipt_identity '
                    'WHERE network_id=? AND our_sig=? ORDER BY recv_monotonic LIMIT 4097',
                    ('solana', attempt['tx_sig']))
                # Source duplicates include every feed: retain the earliest original receipt.
                originals = rows('SELECT * FROM memecoin_signal_receipts INDEXED BY idx_signal_receipt_identity '
                    'WHERE network_id=? AND source_sig IS ? AND source_wallet IS ? AND mint IS ? '
                    "AND COALESCE(decode_state,'') != 'rabbit_pre_execution' "
                    'ORDER BY recv_monotonic LIMIT 4097', ('solana', sig, wallet, source.get('mint')))
                first_source = originals[0] if originals else original
                result.update({field: first_source.get(field) for field in (
                    'producer_run_id', 'sequence', 'config_generation', 'roster_generation',
                    'feed', 'venue', 'decode_state', 'boot_id', 'clock_domain')})
                if len(observed) > 4096 or len(originals) > 4096:
                    reason = 'identity_receipt_overflow'
                elif any(row.get('recv_monotonic') is None for row in originals):
                    reason = 'source_clock_unknown'
                else:
                    result.update(ft.execution_bound(first_source,
                        observed, normalized, pending, fill, custody_pubkey=payer))
            if reason:
                result['reason'] = reason
            result['terminal_reason'] = attempt.get('terminal_reason')
        else:
            outcomes['unattempted_or_identity_unknown'] += 1
        if result['eligibility'] == 'unknown':
            skip = ft.one(timing, 'SELECT * FROM memecoin_slot_lane_skips WHERE signature=?', (sig,))
            try:
                disposition = json.loads((skip or {}).get('evidence_json') or '{}')
            except (ValueError, TypeError):
                disposition = {}
            if not isinstance(disposition, dict):
                disposition = {}
            if (disposition.get('source_wallet') == wallet and disposition.get('mint') == mint
                    and disposition.get('eligibility') == 'independent_policy_refused'):
                result['eligibility'] = 'independent_policy_refused'
                summary['independent_policy_refused'] += 1
            elif (disposition.get('source_wallet') == wallet and disposition.get('mint') == mint
                    and disposition.get('eligibility') == 'eligible'):
                result['eligibility'] = 'eligible'
                summary['eligible'] += 1
            else:
                summary['unknown_eligibility'] += 1
        if result['observed_bound_ms'] is not None:
            times.append(result['observed_bound_ms'])
            summary['complete_witnesses'] += 1
            summary['verified_sub200'] += int(result['sub200'] is True)
        else:
            summary['unknown'] += 1
            # A receipt with no attempt at all ('unattempted', hundreds a day) gets ONE retry, which
            # covers an attempt written just after this pass, then leaves the list; otherwise those
            # orphans fill the 512 slots and push a real late join off the tail (Gamma, 18c83d208).
            if candidates or source['receipt_rowid'] not in retried:
                retained.append(source['receipt_rowid'])
        summary['results'].append(result)
    summary['source_runs'] = list(runs.values())
    summary['outcomes'] = dict(outcomes)
    if times:
        times.sort()
        for key, quantile in (('p50_ms', .5), ('p90_ms', .9)):
            summary[key] = times[max(0, math.ceil(quantile * len(times)) - 1)]
    if summary['eligible']:
        summary['verified_fraction'] = summary['verified_sub200'] / summary['eligible']
    for run in runs:
        summary['coverage'].extend(rows('SELECT * FROM memecoin_execution_coverage '
            'WHERE producer_run_id=? ORDER BY sequence DESC LIMIT ?', (run[0], limit)))
    summary['observer_coverage'] = rows('SELECT * FROM memecoin_execution_coverage '
        'ORDER BY rowid DESC LIMIT ?', (limit,))
    # Whole universe = this pass began at rowid 0 with no carried retries and reached the frontier.
    # Rowids are sparse (empty decodes are skipped, retention deletes), so never compare a count.
    summary['pagination'] = dict(next_cursor=cursor, source_frontier=frontier,
        full_universe_covered=not retry and start_cursor == 0 and cursor >= frontier)
    retained = list(dict.fromkeys(retained))
    summary['retry_overflow'] = max(0, len(retained) - 512)
    timing.execute('INSERT OR REPLACE INTO memecoin_fill_timing_cursor VALUES (?,?)',
        (cursor_name, json.dumps(dict(cursor=cursor, retry=retained[:512]))))
    timing.commit()
    return summary


# ------------------------------------------------------------------------------------- readers

def _tail_lines(path, max_bytes=256 * 1024):
    """Bounded complete lines only. Rotation/deletion and a partial last write remain unknown."""
    try:
        with open(path, 'rb') as handle:
            start = max(0, handle.seek(0, 2) - max_bytes)
            handle.seek(start)
            data = handle.read(max_bytes)
        lines = data.splitlines(keepends=True)
        if start and lines:
            lines.pop(0)
        return [line.decode('utf-8') for line in lines if line.endswith(b'\n')]
    except (OSError, UnicodeError):
        return []


def ingest_bsc_notes(timing, log_path, *, capture_paths=()):
    """Persist receipt-held timing evidence to the timing file only. Never called by a lane.

    Runner verdicts still take precedence. Captured unsupported Relay receipts supply a source
    block and the exact old decode refusal; wallet proof and funded state are never written.
    """
    count = 0

    def store(record, basis):
        nonlocal count
        tx, mint = record.get('tx_hash'), record.get('mint')
        block, reason = record.get('source_block'), record.get('reason')
        if (record.get('chain') != '56' or not isinstance(tx, str)
                or not re.fullmatch(r'0x[0-9a-f]{64}', tx) or not isinstance(mint, str)
                or not re.fullmatch(r'0x[0-9a-f]{40}', mint)
                or type(block) is not int or block <= 0 or not isinstance(reason, str) or not reason):
            return
        timing.execute('INSERT OR IGNORE INTO memecoin_fast_lane_notes '
                       '(network_id, source_sig, mint, reason, source_slot, source_slot_basis) '
                       'VALUES (?,?,?,?,?,?)', ('56', tx, mint, reason[:512], block, basis))
        count += 1

    for line in _tail_lines(log_path):
        if '[bsc-lane-note] ' not in line:
            continue
        try:
            record = json.loads(line.split('[bsc-lane-note] ', 1)[1])
            if isinstance(record, dict):
                store(record, 'watcher_note')
        except (TypeError, ValueError):
            continue
    for path in capture_paths:
        for line in _tail_lines(path):
            try:
                record = json.loads(line)
                tx, receipt = record['transaction'], record['receipt']
                tx_hash = record['tx_hash']
                block = int(receipt['blockNumber'], 16)
                block_hash = tx.get('blockHash')
                if (record.get('chain') != '56' or receipt.get('status') != '0x1'
                        or tx_hash != tx.get('hash') or tx_hash != receipt.get('transactionHash')
                        or not isinstance(block_hash, str) or not re.fullmatch(r'0x[0-9a-f]{64}', block_hash)
                        or block_hash != receipt.get('blockHash')
                        or tx.get('blockNumber') != receipt.get('blockNumber')):
                    continue
                # Only mint transfers delivered to a watched beneficiary identify the coin.
                watched = set(record.get('watched') or ())
                for log in receipt.get('logs') or ():
                    topics = log.get('topics') or ()
                    if (log.get('transactionHash') == tx_hash and log.get('blockHash') == block_hash
                            and log.get('blockNumber') == receipt['blockNumber']
                            and len(topics) == 3 and topics[0].lower() ==
                            '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
                            and ('0x' + topics[2][-40:].lower()) in watched
                            and int(log['data'], 16) > 0):
                        store(dict(chain='56', tx_hash=tx_hash, mint=log['address'].lower(),
                                   source_block=block, reason='source_receipt_unsupported'), 'relay_capture')
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
    timing.commit()
    return count

def solana_reader(url, rpc=None):
    """getSignatureStatuses on the research Helius key through its SharedPacer (Gamma LOW2): the gap
    and a 429's pause are shared with every other process reading that key."""
    if rpc is None:
        from sniper.services.memecoin_research_pacer import SharedPacer
        from sniper.services.memecoin_slot_end_shadow import ResearchRpc
        rpc = ResearchRpc(url, pacer=SharedPacer.for_url(url))

    def read(sigs):
        result = rpc("getSignatureStatuses", [list(sigs), {"searchTransactionHistory": True}])
        if not isinstance(result, dict):
            raise RuntimeError("getSignatureStatuses answered no result")
        values = result.get("value") or []
        return {sig: (v or {}).get("slot") if isinstance(v, dict) else None for sig, v in zip(sigs, values)}
    return read


def evm_hosts(*, deadline=None, clock=time.monotonic):
    """{chain: url} for the EVM reads: the research hosts the evm-key guard accepts and whose
    eth_chainId matches. 56 reuses its dedicated watcher receipt reader, never custody."""
    from scripts.research.verify_axiom_addresses import code_read_url
    from sniper.services import axiom_address_verify as av
    from sniper.services import memecoin_evm_post_trade as pt
    from urllib.parse import urlsplit
    out = {}
    for chain, value in (("4663", "MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663"), ("8453", TENDERLY_BASE)):
        url, why = code_read_url(value)
        if why:
            print("fill timing: no %s host: %s" % (chain, why), flush=True)
            continue
        if deadline is not None and clock() >= deadline:
            break
        result, error = av._http_call(url, "eth_chainId", [])
        try:
            if error is None and int(str(result), 16) == int(chain):
                out[chain] = url
        except (TypeError, ValueError):
            pass
    url = pt.read_host("56")
    custody = {part.strip().rstrip("/") for name in ("MEMECOIN_EVM_RPC_URL_56", "MEMECOIN_EVM_RPC_POOL_URLS_56")
               for part in os.getenv(name, "").split(",") if part.strip()}
    try:
        parsed = urlsplit(url or "")
        hostname = parsed.hostname
    except ValueError:
        return out
    if (url and parsed.scheme == "https" and hostname
            and url.rstrip("/") not in custody and (deadline is None or clock() < deadline)):
        try:
            result = pt.default_read_fn("56")("eth_chainId", [])
            if not isinstance(result, bool) and int(str(result), 16) == 56:
                out["56"] = url
        except Exception:
            pass                     # unavailable/wrong chain is unknown, never a custody fallback
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", default=None)
    parser.add_argument("--timing", default=None)
    parser.add_argument("--max-seconds", type=float, default=50.0)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    if not math.isfinite(args.max_seconds) or args.max_seconds < 0:
        parser.error("--max-seconds must be finite and nonnegative")
    deadline = time.monotonic() + args.max_seconds
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_path, state_root
    load_env_files(str(state_root()), names=(".env",))
    from sniper.settings import memecoin as cfg
    from sniper.services import axiom_address_verify as av
    # A relative DB_PATH (.env has DB_PATH=sniper_tracker.db) is the STATE ROOT's file, never the
    # cron's cwd: in $HOME a stray empty sniper_tracker.db answered "no such table" (2026-10-02).
    ledger_path = Path(args.ledger or os.getenv("DB_PATH") or state_path("sniper_tracker.db"))
    if not ledger_path.is_absolute():
        ledger_path = Path(state_root()) / ledger_path
    ledger = sqlite3.connect("file:%s?mode=ro" % ledger_path, uri=True, timeout=5)
    timing = ft.connect(args.timing)
    capture = Path(state_path('data', 'onchain_evm_relay_unsupported.56.jsonl'))
    ingest_bsc_notes(timing, state_path('logs', 'onchain_evm_armed.56.log'),
                     capture_paths=(str(capture) + '.1', capture))
    lane_conns = ft.open_lanes_readonly()
    solana_read = evm_read = None
    evm_chains = ()
    if cfg.MEMECOIN_FILL_TIMING_SOLANA_READS and os.environ.get("MEMECOIN_RESEARCH_HELIUS_RPC_URL", "").strip():
        solana_read = solana_reader(os.environ["MEMECOIN_RESEARCH_HELIUS_RPC_URL"].strip())
    if cfg.MEMECOIN_FILL_TIMING_EVM_READS:
        hosts = evm_hosts(deadline=deadline)
        evm_chains = tuple(hosts)
        from sniper.services import memecoin_evm_post_trade as pt
        bsc_read = pt.default_read_fn("56") if "56" in hosts else None

        def evm_read(chain, method, params):
            if time.monotonic() >= deadline:
                raise TimeoutError("fill timing pass budget exhausted")
            if chain == "56":
                return bsc_read(method, params)
            result, error = av._http_call(hosts[chain], method, params)
            if error is not None:
                raise RuntimeError("%s %s failed" % (chain, method))
            return result
    alert = None
    try:
        from sniper.services.memecoin_telegram import send_telegram
        alert = lambda text: send_telegram(text)  # noqa: E731
    except Exception:
        pass
    try:
        summary = resolve_pass(ledger, timing, now=datetime.now(timezone.utc), solana_read=solana_read,
                               evm_read=evm_read, evm_chains=evm_chains, alert=alert, lane_conns=lane_conns,
                               max_seconds=max(0.0, deadline - time.monotonic()))
        summary['execution'] = resolve_execution_pass(ledger, timing,
            max_seconds=min(5.0, max(0.0, deadline - time.monotonic())))
    finally:
        ledger.close()
        timing.close()
        for conn in lane_conns.values():
            conn.close()
    if args.summary:
        print(json.dumps(summary, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
