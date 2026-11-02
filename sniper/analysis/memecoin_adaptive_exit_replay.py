"""Compare four quote rules. This module cannot submit or authorize an exit.

The contract is docs/audits/2026-09-16-exit-rule-comparison-contract.md.
Missing safety history and alternative fills prevent an executable net-PnL verdict.
"""
from collections import Counter, defaultdict
from datetime import datetime
import json
import math
import random
import statistics

VARIANTS = ('current', 'moving_stop', 'reduced_target', 'both')
SPLIT = '2026-09-13T00:00:00+00:00'


def timestamp(value):
    """Reject timestamps without a timezone."""
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('timestamp has no timezone')
    return stamp.timestamp()


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError('value must be finite and positive')
    return number


def replay(bundle, variant, *, max_gap_s=10):
    """Return a conditional quote outcome or an explicit unknown reason."""
    if variant not in VARIANTS:
        raise ValueError(variant)
    result = dict(status='path_ended', quote_pnl=None, net_pnl=None,
                  initial_gap_pct=None, provenance_complete=True)

    def unknown(reason):
        return dict(result, status=reason)

    p = bundle['position']
    if p['exit_reason'] in ('manual', 'mirror', 'mirror_sell', 'source_sell'):
        return unknown('manual_or_mirror_exit')
    if bundle['memecoin_position_exit_overrides'] or bundle['memecoin_exit_targets']:
        return unknown('operator_overlay')
    entries = [f for f in bundle['memecoin_fills'] if f['kind'] == 'entry']
    exits = [f for f in bundle['memecoin_fills'] if f['kind'] != 'entry']
    if len(entries) != 1 or len(exits) > 1 or any(f['kind'] == 'derisk' for f in exits):
        return unknown('partial_or_multiple_exit')
    if any(a['status'] not in ('closed', 'already_closing')
           for a in bundle['memecoin_exit_execution_attempts']):
        return unknown('execution_complexity')
    try:
        policy = json.loads(p['funded_contract_json'])['policy']
        # Do not erase another active rail to obtain a larger comparison sample.
        if (policy.get('trail_pct') or policy.get('derisk_pct') or policy.get('mirror_on_sell')
                or policy.get('engine_authoritative')):
            return unknown('unsupported_policy')
        stop = float(policy['stop_pct'])
        target = float(policy['tp_pct'])
        catastrophe = float(policy['catastrophe_pct'])
        hold = positive(policy['max_hold_s'])
        confirmations = int(policy['stop_debounce_sweeps'])
        if not all(math.isfinite(x) and x >= 0 for x in (stop, target, catastrophe)) or confirmations < 1:
            return unknown('invalid_policy')
        cost, qty = positive(entries[0]['notional']), positive(entries[0]['qty'])
        if not math.isclose(cost, positive(p['entry_notional']), rel_tol=1e-6):
            return unknown('entry_cost_mismatch')
        opened = timestamp(p['opened_at'])
        filled = timestamp(p['opened_recorded_at'])
        closed = timestamp(p['closed_at'])
        if not opened <= filled <= closed:
            return unknown('invalid_position_clock')
        boundary = min([timestamp(a['checked_at']) for a in bundle['memecoin_exit_trigger_audit']
                        if a['reason'] == 'vol_death' and a['allowed']] or [math.inf])
        quotes = sorted((q for q in bundle['memecoin_route_series'] if q['seam'] == 'exit_decision'),
                        key=lambda q: timestamp(q['observed_at']))
    except (ValueError, TypeError, KeyError, OverflowError):
        return unknown('invalid_input')
    first = last = last_quote = None
    high_move = low_move = 0.0
    peak = 0.0
    hits = 0
    for q in quotes:
        observed = timestamp(q['observed_at'])
        if observed < filled:
            continue
        if observed > closed:
            break
        if (str(q.get('position_id')) != str(p['id']) or q.get('mint') != p['mint']
                or q.get('network_id') != p['network_id']):
            return unknown('quote_identity_mismatch')
        if observed >= boundary:
            return unknown('observed_safety_exit')
        try:
            quoted = timestamp(q['quoted_at'])
        except (TypeError, ValueError):
            return unknown('invalid_timestamp')
        age = observed - quoted
        if age < 0:
            return unknown('future_quote')
        if age > 5:
            return unknown('stale_quote')
        if quoted < filled:
            continue
        if last_quote is not None and quoted <= last_quote:
            continue
        if first is None and observed - filled > 10:
            return unknown('late_initial_quote')
        if last is not None and observed - last > max_gap_s:
            return unknown('quote_gap')
        try:
            value, quote_qty = positive(q['expected_usdc']), positive(q['open_qty'])
        except (TypeError, ValueError, OverflowError):
            return unknown('invalid_quote')
        if not math.isclose(quote_qty, qty, rel_tol=1e-6):
            return unknown('quantity_changed')
        evidence = q.get('quote_evidence_json')
        if evidence:
            try:
                evidence = json.loads(evidence)
                atomic_in = positive(evidence['input_atomic'])
                decimals = round(math.log10(atomic_in / qty))
                if not 0 <= decimals <= 36 or not math.isclose(atomic_in / 10**decimals, qty, rel_tol=1e-6):
                    return unknown('atomic_quantity_mismatch')
                amount = positive(evidence['expected_output_atomic'])
                usd = positive(evidence['quote_asset_usd'])
                output_decimals = int(evidence['quote_asset_decimals'])
                if not 0 <= output_decimals <= 36:
                    return unknown('invalid_route_evidence')
                if not math.isclose(amount * usd / 10**output_decimals, value, rel_tol=1e-5, abs_tol=1e-6):
                    return unknown('quote_value_mismatch')
            except (ValueError, KeyError, TypeError, OverflowError):
                return unknown('invalid_route_evidence')
        else:
            result['provenance_complete'] = False
        last, last_quote = observed, quoted
        if first is None:
            first = value
            result['initial_gap_pct'] = 100 * (first - cost) / cost
            result['initial_delay_s'] = observed - filled
        pnl_pct = 100 * (value - cost) / cost
        move = 100 * (value - first) / cost
        high_move, low_move = max(high_move, move), min(low_move, move)
        peak = max(peak, value)
        stop_at = -stop + (high_move if variant in ('moving_stop', 'both') else 0)
        target_at = target + (low_move if variant in ('reduced_target', 'both') else 0)
        reason = None
        if stop > 0 and pnl_pct <= stop_at + 1e-9:
            reason = 'b_stop'
        elif catastrophe > 0 and pnl_pct <= -catastrophe + 1e-9:
            reason = 'catastrophe'
        elif target > 0 and pnl_pct >= target_at - 1e-9:
            reason = 'real_tp'
        elif observed - opened > hold:
            reason = 'b_timecap'
        if reason in ('b_stop', 'catastrophe'):
            hits += 1
            past_floor = catastrophe > 0 and pnl_pct <= -catastrophe
            grace = reason == 'b_stop' and observed - filled < 20 and not past_floor
            instant = pnl_pct <= -50 and not grace
            if not instant and hits < confirmations:
                continue
        else:
            hits = 0
        if reason:
            return dict(result, status='quoted', reason=reason, quote_pnl=value-cost,
                        return_pct=pnl_pct, exit_at=q['observed_at'], proceeds=value,
                        peak_surrendered=peak-value,
                        profit_surrendered=max(0, peak-cost)-max(0, value-cost),
                        stop_at=stop_at, target_at=target_at)
    return unknown('observed_safety_exit' if boundary <= closed else 'path_ended')


def clustered_interval(rows, variant):
    """Resample whole coins, including every repeated position on each coin."""
    clusters = defaultdict(list)
    for row in rows:
        o = row['outcomes']
        clusters[(row['network_id'], row['mint'])].append(o[variant]['quote_pnl']-o['current']['quote_pnl'])
    if len(clusters) < 2:
        return None
    groups = list(clusters.values())
    rng = random.Random(20260916)
    means = []
    for _ in range(2000):
        sample = [v for group in rng.choices(groups, k=len(groups)) for v in group]
        means.append(statistics.mean(sample))
    means.sort()
    return [means[49], means[1949]]


def summarize(rows):
    """Keep the complete intersection fixed across the four comparisons."""
    complete = [r for r in rows if all(r['outcomes'][v]['status'] == 'quoted' for v in VARIANTS)]
    report = dict(n=len(rows), complete_n=len(complete),
                  coins=len({(r['network_id'], r['mint']) for r in complete}),
                  unknown={}, reasons={}, variants={}, pairwise={})
    for v in VARIANTS:
        report['unknown'][v] = sum(r['outcomes'][v]['status'] != 'quoted' for r in rows)
        report['reasons'][v] = dict(Counter(r['outcomes'][v]['status'] for r in rows))
        pair = [r for r in rows if r['outcomes'][v]['status'] == r['outcomes']['current']['status'] == 'quoted']
        report['pairwise'][v] = dict(n=len(pair), quote_delta=sum(
            r['outcomes'][v]['quote_pnl']-r['outcomes']['current']['quote_pnl'] for r in pair))
        if not complete:
            continue
        out = [r['outcomes'][v] for r in complete]
        deltas = [r['outcomes'][v]['quote_pnl']-r['outcomes']['current']['quote_pnl'] for r in complete]
        gains = sorted([d for d in deltas if d > 1e-9], reverse=True)
        cut = [r for r in complete if r['outcomes']['current']['quote_pnl'] > 0
               and r['outcomes'][v]['quote_pnl'] < r['outcomes']['current']['quote_pnl']-1e-9
               and timestamp(r['outcomes'][v]['exit_at']) < timestamp(r['outcomes']['current']['exit_at'])]
        large = [o for o in out if o['return_pct'] <= -35]
        report['variants'][v] = dict(
            quote_pnl=sum(o['quote_pnl'] for o in out), net_pnl=None,
            delta=sum(deltas), mean_delta=statistics.mean(deltas), median_delta=statistics.median(deltas),
            improved=sum(d > 1e-9 for d in deltas), worse=sum(d < -1e-9 for d in deltas),
            without_two_largest_gains=sum(deltas)-sum(gains[:2]), min_delta=min(deltas), max_delta=max(deltas),
            mean_delta_interval=clustered_interval(complete, v),
            large_losses=len(large), large_loss_cash=sum(o['quote_pnl'] for o in large),
            peak_surrendered=sum(o['peak_surrendered'] for o in out),
            profit_surrendered=sum(o['profit_surrendered'] for o in out), winners_cut_short=len(cut),
            winner_profit_surrendered=sum(r['outcomes']['current']['quote_pnl']-r['outcomes'][v]['quote_pnl'] for r in cut),
        )
    return report

from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('summarize',))
