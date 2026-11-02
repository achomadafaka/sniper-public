"""A paper order on a partial sheet (Alpha ruling 2026-10-31, operator: the sheet is an extra, not a prerequisite).

When the full sheet cannot be built for an AGEING / PROOF reason (stale mint receipt, tape loss or commit lag since the cut, evidence past its expiry, unproven pool state), enter() may still submit
a paper order on a PARTIAL receipt built from the stored ticket. What changes: only the "sheet is known" check and the sells_60s input. What does not: clip, concurrency, daily halt, kill, exits,
the strict fill proof (a partial sheet never turns a failed strict quote into a fill), the role / epoch / incarnation checks, authority-closed, sell-probe-ok, and a PROVEN zero of sells. No new call,
lock or wait is added to the fill path: the one tape read below runs in the seat's order call.

Sells (risk wall input) are recomputed from the PumpSwap tape for the 60 s BEFORE THE TICKET'S CUT, read-only:
  seen sells               -> True,  basis 'cut_tape'  (count stored)
  proven zero              -> False, risk vetoes (no_sells_60s)   [rows seen, pool selected for the whole window, capture frontier past the cut, every loss in the window complete-scope with a readable list not naming the pool, no capture point]
  unknown                  -> None;  when the stored sell probe passed the order is still allowed, basis 'sell_probe' (probe age and round-trip loss stored); otherwise risk vetoes sells_60s_unknown
Unknown is never recorded as zero or as passed-by-tape. This proves the pool was sold into at the cut, NOT that it is sellable now: no current-liquidity evidence exists for these orders beyond the strict fill proof.
"""
import json
import math
import os
import sqlite3
from pathlib import Path

from sniper.office import partial_sheet as PS
from sniper.office import safety_evidence as SE
from sniper.office import store as S
from sniper.office.tools import sheet as SH
from sniper.services import memecoin_desk_contract as K

ALLOWED_CLASSES = ('mint_receipt_too_old_or_invalid', 'mint_receipt_unusable', 'tape_loss_since_cut', 'tape_commit_unproven', 'evidence_not_pass_at_read', 'pool_state_unproven')
CHANGE_ID = 'partial-sheet-orders-v1'
WINDOW_S = 60.0


def enabled():
    return os.environ.get('OFFICE_PARTIAL_SHEET_ORDERS', '1').strip() != '0'


def _num(v):
    return type(v) in (int, float) and math.isfinite(v)


def sells_before_cut(desk_dir, pool, cut):
    """-> (sells_exist True|False|None, sells_n int|None, why). Read-only on the PumpSwap tape; never raises."""
    if not isinstance(pool, str) or not _num(cut):
        return None, None, 'no_pool_or_cut'
    try:
        c = sqlite3.connect('file:%s?mode=ro' % (Path(desk_dir) / 'pumpswap' / 'tape.db'), uri=True, timeout=1.0)
    except (sqlite3.Error, OSError):
        return None, None, 'tape_unreadable'
    try:
        rows, sells = c.execute('SELECT count(*), coalesce(sum(is_buy=0),0) FROM swaps_ps WHERE pool=? AND received_at>? AND received_at<=?', (pool, cut - WINDOW_S, cut)).fetchone()
        if sells > 0:
            return True, int(sells), 'seen_on_tape'
        if rows == 0:
            return None, None, 'no_rows_in_window'                       # outside retention or never stored: unknown, not zero
        lo = cut - WINDOW_S
        # One selection row and one capture interval must span the window: holes between rows are never bridged. An open selection runs to the live end; an open capture has no frontier, so it proves nothing.
        if not c.execute('SELECT 1 FROM ps_pool_selection WHERE pool=? AND start_at<=? AND (end_at IS NULL OR end_at>=?) LIMIT 1', (pool, lo, cut)).fetchone():
            return None, None, 'pool_not_selected_whole_window'
        for scope, pj in c.execute('SELECT scope,pools_json FROM ps_loss_attribution WHERE received_at>? AND received_at<=?', (lo, cut)):
            if scope != 'complete':
                return None, None, 'loss_scope_unknown'                  # NULL or 'unknown': the loss may be on this pool
            try:
                pools = json.loads(pj)
            except (TypeError, ValueError):
                return None, None, 'loss_scope_unreadable'
            if type(pools) is not list or not pools or not all(type(x) is str for x in pools):
                return None, None, 'loss_scope_unreadable'               # NULL, {} and [] name no pool, so they cannot clear this one
            if pool in pools:
                return None, None, 'pool_loss_in_window'
        if c.execute("SELECT 1 FROM gaps_ps WHERE (cause IS NULL OR cause NOT IN ('decode_loss','virtual_quote_unproven')) AND at>? AND at<=? LIMIT 1", (lo, cut)).fetchone():
            return None, None, 'capture_point_in_window'
        if not c.execute('SELECT 1 FROM ps_capture_intervals WHERE start_at<=? AND through_at IS NOT NULL AND through_at>=? LIMIT 1', (lo, cut)).fetchone():
            return None, None, 'capture_not_continuous'
        return False, 0, 'proven_zero'
    except sqlite3.Error:
        return None, None, 'tape_query_failed'
    finally:
        c.close()


def candidate(store_conn, ticket, cohort, got, *, offset, sampled_at, host, desk_dir):
    """-> {'state': 'ok', 'candidate': {...}, 'order': {...stratum fields...}} or {'state': 'refused'|'unknown', 'reason': str}. Persists the partial receipt (same format and verify path as a full one)."""
    cls = PS.failure_class(got.get('reason') if isinstance(got, dict) else None)[0]
    if cls not in ALLOWED_CLASSES:
        return dict(state='refused', reason='partial_class_not_allowed:' + cls)
    if not _num(offset) or not _num(sampled_at) or not _num(host) or not isinstance(cohort, dict) or not isinstance(cohort.get('policy'), dict):
        return dict(state='unknown', reason='partial_inputs_unknown')
    cand = ticket.get('candidate') if isinstance(ticket.get('candidate'), dict) else {}
    stage = next((s for s in ticket.get('stages', []) if isinstance(s, dict) and s.get('stage') == 'hard_filter'), None)
    evidence = ((stage or {}).get('inputs') or {}).get('evidence') if stage else None
    if not isinstance(evidence, dict) or not isinstance(evidence.get('checks'), dict) or not isinstance(evidence.get('market_cut'), dict):
        return dict(state='refused', reason='partial_evidence_missing')
    row = {'verdict': stage.get('verdict'), 'reason': stage.get('reason'), 'ts': stage.get('ts'), 'evidence': evidence}
    feed = 'pass' if stage.get('verdict') == 'PASS' else 'near_pass'
    if feed == 'near_pass' and SH.near_pass_blockers(row) is None:
        return dict(state='refused', reason='not_near_pass_eligible')
    checks = evidence['checks']
    token = checks.get('token_safety') if isinstance(checks.get('token_safety'), dict) else {}
    probe = checks.get('sell_probe') if isinstance(checks.get('sell_probe'), dict) else {}
    cut = evidence['market_cut'].get('receipt_cut')
    sells, sells_n, why = sells_before_cut(desk_dir, ticket.get('pool', cand.get('pool')), cut)
    probe_ok = probe.get('status') == 'pass'
    basis = 'cut_tape' if sells is not None else ('sell_probe' if probe_ok else 'unknown')
    soft = K.soft_facts(checks)
    holders = soft.get('holders') if isinstance(soft.get('holders'), dict) else {}
    safety = {'authority_mint': token.get('authority_mint'), 'authority_freeze': token.get('authority_freeze'), 'cluster_hit': None, 'lp_locked_or_burned': None,
              'top10_pct': holders.get('top10_pct'), 'creator_pct': holders.get('creator_pct'), 'sell_probe': 'ok' if probe_ok else None,
              'sells_exist_60s': sells, 'sells_unknown_allowed_by_probe': bool(sells is None and probe_ok)}
    cluster_hit, _, evidence_block = SE.build(desk_dir=desk_dir, mint=ticket.get('mint', cand.get('mint')), pool=ticket.get('pool', cand.get('pool')), as_of=host, soft=soft, reader=None, checks=checks)    # no RPC on the order path
    safety['cluster_hit'] = cluster_hit
    view = PS.partial_sheet(ticket, got, now_host=host)['sheet']
    receipt_mint = (view.get('mint_receipt') or {})
    decimals = receipt_mint.get('decimals') if isinstance(receipt_mint.get('decimals'), int) else None
    corrected = host - offset
    body = {'sheet_kind': 'partial', 'safety': safety, 'safety_evidence': evidence_block, 'pool_state': {'base_decimals': decimals, 'virtual_quote_source': None}, 'ticker': None, 'partial_view': view,
            'sells': {'basis': basis, 'count': sells_n, 'why': why, 'window': '%d s before the ticket cut' % WINDOW_S, 'cut': cut},
            'liquidity_note': 'No current-liquidity evidence exists for this order beyond the strict fill proof.'}
    policy, epoch = cohort['policy'], ticket.get('cohort_id')
    bundle = SH.definition_bundle_hash(policy, epoch)
    request = {'network_id': ticket.get('network_id', cand.get('network_id')), 'mint': ticket.get('mint', cand.get('mint')), 'pool': ticket.get('pool', cand.get('pool')), 'sheet_kind': 'partial', 'feed': feed,
               'snapshot_hash': ticket.get('snapshot_hash'), 'evidence_hash': SH._sha(evidence), 'definition_bundle_hash': bundle, 'strategy_epoch': epoch, 'built_at_corrected': corrected,
               'change_id': CHANGE_ID, 'failure_class': cls}
    key = SH.cache_key(request)
    clock = {'corrected_utc': corrected, 'offset_s': float(offset), 'sampled_at': float(sampled_at)}
    receipt = {'kind': 'sheet', 'state': 'known', 'sheet_kind': 'partial', 'contract': SH.OFFICE_SHEET_CONTRACT['version'], 'cache_key': key, 'request': request, 'definition_bundle_hash': bundle,
               'strategy_epoch': epoch, 'cut': {'basis': 'corrected_utc', 'receipt_cut': (cut - offset) if _num(cut) else None, 'knowledge_cut': corrected}, 'clock_sample': clock,
               'sheet': body, 'sheet_hash': SH._sha(body), 'lenses': {}, 'lenses_hash': SH._sha({}), 'read_budget_s': {}, 'elapsed_s': {}}
    row_hash = S.append(store_conn, 'receipts', receipt)
    emitted = cand.get('snapshot', {}).get('emission_at') if isinstance(cand.get('snapshot'), dict) else None
    age = (host - emitted) if _num(emitted) else None
    evidence_at = _epoch(evidence.get('known_at'))
    order = {'sheet_state': 'partial', 'sheet_class': cls, 'sheet_ticket_age_s': age, 'sheet_unknown_n': len(view.get('unknown') or {}), 'sheet_view_hash': SH._sha(view),
             'sells_basis': basis, 'sells_n': sells_n, 'probe_age_s': (host - evidence_at) if basis == 'sell_probe' and _num(evidence_at) else None,
             'probe_loss_pct': probe.get('round_trip_loss_pct') if basis == 'sell_probe' and _num(probe.get('round_trip_loss_pct')) else None}
    cand_out = {'network_id': request['network_id'], 'receipt_row_hash': row_hash, 'cache_key': key, 'sheet_hash': receipt['sheet_hash'], 'mint': request['mint'], 'pool': request['pool'], 'feed': feed,
                'sheet': body, 'lenses': {}}
    return dict(state='ok', candidate=cand_out, order=order, known_at=[_epoch(stage.get('ts'))])


def _epoch(value):
    from datetime import datetime
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None
