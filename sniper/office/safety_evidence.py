"""Holder, cluster and LP evidence for Office sheets (operator order 2026-10-31, request 24245). Reported and labelled, never a gate: risk.py reads none of it, and null means not known, never safe.

cluster_hit  The host's own admission check, evidence.checks.cluster: kill is True, pass is False, anything else is null with its reason. The newest cluster decision by the sheet's as-of is shown beside it for its shares (denominator attempted_supply), never replaces it, and a disagreement is labelled.
lp_locked_or_burned  True only when the LP mint's on-chain supply is exactly 0, read at a dated slot. A later deposit can mint again, so this is true at that slot only. Non-zero supply is null: a lock cannot be sourced here, and the largest holder is reported instead.
Off the funded path: the store read runs in the sheet build and the RPC read uses the capped research reader on the Office thread."""
import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path


def _epoch(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _share(value):
    return value * 100.0 if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1 else None


def cluster_evidence(desk_dir, mint, as_of):
    """-> the cluster block. state known or unknown; a literal 0.0 share is a measured zero, an absent one is null."""
    path = Path(desk_dir) / 'cluster.db'
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1.0)
        try:
            rows = conn.execute('SELECT d.allowed,d.reason,d.policy_id,d.snapshot_at,d.completed_at,d.metrics_json,d.source_path FROM cluster_decisions d JOIN cluster_snapshots s USING(snapshot_id) '
                                'WHERE s.mint=?', (mint,)).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, TypeError):
        return dict(state='unknown', reason='cluster_store_unreadable', cluster_hit=None)
    # ISO stamps can differ in offset text, so both the cut and "newest" use parsed times, never text order.
    row = max((r for r in rows if _epoch(r[4]) is not None and _epoch(r[4]) <= as_of), key=lambda r: _epoch(r[4]), default=None)
    if row is None:
        return dict(state='unknown', reason='no_decision_by_as_of' if not rows else 'only_later_decisions', cluster_hit=None)
    allowed, reason, policy, snapshot_at, completed_at, raw, source = row
    if allowed not in (0, 1):
        return dict(state='unknown', reason='decision_malformed', cluster_hit=None)
    try:
        metrics = json.loads(raw)
    except (TypeError, ValueError):
        metrics = None
    metrics = metrics if isinstance(metrics, dict) else {}
    return dict(state='known', cluster_hit=allowed == 0, reason=reason, policy_id=policy, source_path=source, snapshot_at=snapshot_at, completed_at=completed_at,
                age_s=as_of - _epoch(completed_at), denominator='attempted_supply', attempted_supply_atomic=metrics.get('attempted_supply'), coverage_pct=_share(metrics.get('coverage')),
                holders_eligible=metrics.get('eligible'), holders_resolved=metrics.get('resolved'), top_funder_pct_of_attempted=_share(metrics.get('top_funder_of_attempted')),
                clustered_pct_of_attempted=_share(metrics.get('clustered_of_attempted')), excluded_known_funders_pct_of_attempted=_share(metrics.get('excluded_known_funders_of_attempted')))


def holder_evidence(soft):
    holders = soft.get('holders') if isinstance(soft, dict) and isinstance(soft.get('holders'), dict) else {}
    return dict(state=holders.get('state', 'unknown'), check=holders.get('check'), reason=holders.get('reason'), top10_pct=holders.get('top10_pct'), creator_pct=holders.get('creator_pct'), creator_basis=holders.get('creator_basis'))


def lp_evidence(reader, pool, mint):
    if reader is None or not hasattr(reader, 'read_lp'):
        return dict(state='unknown', reason='lp_reader_off', lp_locked_or_burned=None)
    try:
        got = reader.read_lp(pool, mint)
    except Exception as exc:                # evidence only: a failed read is a labelled unknown, never a failed sheet
        return dict(state='unknown', reason='lp_read_error:' + type(exc).__name__, lp_locked_or_burned=None)
    if not isinstance(got, dict) or 'unknown' in got:
        return dict(state='unknown', reason=str((got or {}).get('unknown', 'lp_read_unusable'))[:80], lp_locked_or_burned=None)
    supply = got.get('lp_mint_supply_atomic')
    if type(supply) is not int or supply < 0:
        return dict(state='unknown', reason='lp_supply_unusable', lp_locked_or_burned=None)
    if type(got.get('slot')) is not int or got['slot'] <= 0:
        return dict(state='unknown', reason='lp_slot_unusable', lp_locked_or_burned=None)      # burned is claimed only at a dated slot
    return dict(got, state='known', lp_locked_or_burned=True if supply == 0 else None,
                custody='burned_supply_zero_at_slot' if supply == 0 else 'supply_outstanding_lock_not_sourced')


def _dump_group(checks):
    row = checks.get('dump_group') if isinstance(checks, dict) else None
    row = row if isinstance(row, dict) else {}
    status, reason = row.get('status'), row.get('reason')
    return dict(source='evidence.checks.dump_group', status=status if status in ('pass', 'kill', 'unknown') else 'unknown', reason=reason if isinstance(reason, str) else None)


def build(*, desk_dir, mint, pool, as_of, soft, reader, checks=None):
    """-> (cluster_hit, lp_locked_or_burned, block). Total: any failure stays inside its own part."""
    decision, lp = cluster_evidence(desk_dir, mint, as_of), lp_evidence(reader, pool, mint)
    host = soft.get('cluster') if isinstance(soft, dict) and isinstance(soft.get('cluster'), dict) else {}
    hit = {'flagged': True, 'pass': False}.get(host.get('check'))
    cluster = dict(source='evidence.checks.cluster', check=host.get('check', 'unknown'), reason=host.get('reason'), cluster_hit=hit, top_funder_high=host.get('top_funder_high'), store_decision=decision,
                   store_agrees=None if hit is None or decision['cluster_hit'] is None else hit == decision['cluster_hit'])
    block = dict(version='sheet-safety-evidence-v1', note='Reported evidence, not a gate. Null is not known, never safe.', holders=holder_evidence(soft), cluster=cluster, lp=lp, dump_group=_dump_group(checks))
    return hit, lp['lp_locked_or_burned'], block


def price_basis(state):
    """What the sheet's price means. PumpSwap fills price on quote + virtual quote (23063 raw buys, 2026-10-31: fill over that model p50 1.0115, fee included), so a fill above the real-reserve price is not a premium.
    Real-reserve price is shown for reading only: it is not executable. Any unusable input leaves it null."""
    state = state if isinstance(state, dict) else {}
    try:
        base, quote, virtual = (int(state[k]) for k in ('base_reserve_atomic', 'quote_reserve_atomic', 'virtual_quote_atomic'))
        decimals, sol = state['base_decimals'], float(state['sol_usd'])
        if base <= 0 or quote <= 0 or virtual < 0 or type(decimals) is not int or not 0 <= decimals <= 18 or not math.isfinite(sol) or sol <= 0:
            raise ValueError
        per_token = 10 ** decimals / base / 1e9 * sol
        real, inclusive, ratio = quote * per_token, (quote + virtual) * per_token, (quote + virtual) / quote
        if not all(math.isfinite(v) for v in (real, inclusive, ratio)):
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        return dict(basis='virtual_inclusive', state='unknown', real_reserve_price_usd=None, virtual_inclusive_price_usd=None, virtual_over_real=None)
    return dict(basis='virtual_inclusive', state='known', real_reserve_price_usd=real, virtual_inclusive_price_usd=inclusive, virtual_over_real=ratio,
                note='Fills and exits price on real plus virtual quote. The real-reserve price is not executable; a fill above it is not a premium.')


ROUND_S = 0.0005 + 1e-6   # structure rounds its age to 3 places


def unpriced_after_last(desk_dir, pool, structure, as_of):
    """Swaps on the tape after structure's last priced one. A sell is priced only from the next swap, so a sell at the tip leaves structure.price_usd at the price before it (7x high on cbzdwMnW, 2026-10-31).
    The last priced time comes from structure's own as_of and its age, rounded to 3 places, so it is known to +-0.5 ms. Another swap inside that span may or may not be later: state lower_bound, never a proven count.
    Counts stored rows only, not lost ones. Any unreadable part is state unknown: never a count of zero."""
    unknown = lambda reason: dict(state='unknown', reason=reason, count=None, sells=None, newest_age_s=None)
    structure = structure if isinstance(structure, dict) else {}
    age, cut = structure.get('last_swap_age_s'), structure.get('as_of')
    numeric = lambda x: type(x) in (int, float) and math.isfinite(x)
    if not numeric(age) or age < 0 or not numeric(cut) or not numeric(as_of) or not isinstance(pool, str):
        return unknown('no_priced_swap')
    last = cut - age
    try:
        conn = sqlite3.connect((Path(desk_dir) / 'pumpswap' / 'tape.db').resolve().as_uri() + '?mode=ro', uri=True, timeout=1.0)
        try:
            count, sells, newest = conn.execute('SELECT count(*),coalesce(sum(is_buy=0),0),max(received_at) FROM swaps_ps WHERE pool=? AND received_at>? AND received_at<=?', (pool, last + ROUND_S, as_of)).fetchone()
            span = conn.execute('SELECT count(*) FROM swaps_ps WHERE pool=? AND received_at>=? AND received_at<=?', (pool, last - ROUND_S, last + ROUND_S)).fetchone()[0]
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return unknown('tape_unreadable')
    out = dict(state='known' if span <= 1 else 'lower_bound', count=count, sells=sells, newest_age_s=as_of - newest if newest is not None else None)
    if span > 1:
        out['reason'] = 'swaps_within_age_rounding'
    if count or span > 1:
        out['note'] = 'structure last may predate swaps; use the reference price.'
    return out
