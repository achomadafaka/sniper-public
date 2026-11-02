"""Causal whole-bag quote-floor model, isolated from funded execution."""
from __future__ import annotations
from .evidence import episode_gate, digest, number


def replay(episode, policy, *, decision_at=None):
    base = {'origin_id': episode.get('origin_id'), 'policy_id': policy.get('policy_id'),
            'episode_digest': digest(episode), 'policy_digest': digest(policy),
            'basis': 'modeled_quote_floor', 'same_block': 'unknown', 'net_cash_usd': None,
            'locked_capital_usd': 0.}
    def censored(reason, **extra):
        return {**base, 'state': 'censored', 'reason': reason, **extra}
    gate = episode_gate(episode)
    if not gate.eligible: return censored(','.join(gate.codes))
    entry_mode = policy.get('entry')
    if (entry_mode not in ('immediate', 'dip') or not policy.get('policy_id')
        or not isinstance(policy.get('mirror', False), bool)
        or not number(policy.get('clip_usd'), positive=True)
        or not number(policy.get('max_hold_s'), positive=True)
        or policy['max_hold_s'] > episode['window']['max_hold_s']
        or any(not number(policy.get(k, 0)) for k in ('tp_pct', 'sl_pct', 'trail_pct', 'trail_arm_pct'))):
        return censored('invalid_policy')
    frames = episode['frames']; source = episode['source']; ex = episode['execution']
    # Alternatives share the frame's state/availability clock, but retain their
    # own acquired quantity and fee. Never interpolate a different clip.
    def entry_quote(frame):
        return next((q for q in [frame['buy'], *frame.get('entry_quotes', [])]
                     if q['spend_usd'] == policy['clip_usd']), None)
    received = source['received_at']; decision = received if decision_at is None else decision_at
    if not number(decision) or decision < received or (decision_at is not None and entry_mode != 'immediate'):
        return censored('invalid_admission_clock')
    source_price = source['spent_usd'] / (int(source['acquired_atomic']) / 10 ** source['decimals'])
    if entry_mode == 'dip':
        wait, premium = policy.get('max_wait_s'), policy.get('premium_cap_pct')
        if not number(wait) or wait > episode['window']['entry_wait_s'] or not isinstance(premium, (float, int)):
            return censored('invalid_dip_policy')
        trigger = None
        for f in frames:
            if not received <= f['available_at'] <= received + wait: continue
            b = entry_quote(f)
            if b is None: return censored('entry_quote_missing')
            price = b['spend_usd'] / (int(b['floor_atomic']) / 10 ** b['decimals'])
            if price <= source_price * (1 + premium / 100.): trigger = f['available_at']; break
        if trigger is None:
            return {**base, 'state': 'not_entered', 'reason': 'dip_expired', 'net_cash_usd': 0.}
        decision = trigger
    # A frame's executable state is the market at `at`; it becomes usable at `available_at`. A fill at an
    # instant much later than its state is not an execution floor (Codex W7), so the episode's registered
    # quote-age contract bounds the gap for every frame the model fills on.
    max_age = ex['max_quote_age_s']
    def stale(frame): return frame['available_at'] - frame['at'] > max_age
    feature_gate = episode_gate(episode, decision_at=decision)
    if not feature_gate.eligible: return censored(','.join(feature_gate.codes))
    entry = next((f for f in frames if f['at'] >= decision + ex['entry_delay_s']
                  and f['available_at'] >= decision + ex['entry_delay_s']), None)
    buy = entry_quote(entry) if entry is not None else None
    if buy is None: return censored('entry_quote_missing')
    if stale(entry): return censored('entry_state_stale')
    qty = buy['floor_atomic']; cost = buy['spend_usd']; entry_at = entry['available_at']
    deadline = entry_at + policy['max_hold_s']; peak = 0.; reason = None; trigger_at = None
    mirror_at = None
    if policy.get('mirror'):
        for sell in episode.get('source_sells') or []:
            if (not isinstance(sell, dict) or not isinstance(sell.get('available_at'), (float, int))
                or not isinstance(sell.get('source_at'), (float, int))
                or sell['available_at'] < sell['source_at'] or sell.get('fraction') != 1.):
                return censored('source_sell_partial_or_order_unknown', locked_capital_usd=cost)
        times = [s['available_at'] for s in episode.get('source_sells') or [] if s['available_at'] >= entry_at]
        mirror_at = min(times) if times else None
    for f in frames:
        if f['sequence'] < entry['sequence'] or f['at'] < entry['at'] or f['available_at'] < entry_at: continue
        q = f['sells'].get(qty)
        if q is None: return censored('whole_quantity_exit_missing', locked_capital_usd=cost)
        if q.get('status', 'ok') == 'token_unsellable':
            reason, trigger_at = 'token_unsellable', f['available_at']; break
        gain = (q['floor_usd'] / cost - 1) * 100.; peak = max(peak, gain)
        at = f['available_at']
        if mirror_at is not None and at >= mirror_at: reason = 'mirror'
        elif policy.get('sl_pct', 0) > 0 and gain <= -policy['sl_pct']: reason = 'stop'
        elif policy.get('tp_pct', 0) > 0 and gain >= policy['tp_pct']: reason = 'tp'
        elif (policy.get('trail_pct', 0) > 0 and peak >= policy.get('trail_arm_pct', 0)
              and q['floor_usd'] <= cost * (1 + peak / 100) * (1 - policy['trail_pct'] / 100)):
            reason = 'trail'
        elif at >= deadline: reason = 'timecap'
        if reason: trigger_at = at; break
    if reason is None: return censored('no_exit_before_path_end', locked_capital_usd=cost)
    exit_frame = next((f for f in frames if f['at'] >= trigger_at + ex['exit_delay_s']
                       and f['available_at'] >= trigger_at + ex['exit_delay_s']), None)
    if exit_frame is None: return censored('exit_quote_missing', locked_capital_usd=cost)
    if stale(exit_frame): return censored('exit_state_stale', locked_capital_usd=cost)
    q = exit_frame['sells'].get(qty)
    if q is None: return censored('whole_quantity_exit_missing', locked_capital_usd=cost)
    if exit_frame['available_at'] + ex['settlement_s'] > episode['window']['end']:
        return censored('settlement_outside_path', locked_capital_usd=cost)
    details = {'decision_at': decision, 'entry_at': entry_at, 'entry_sequence': entry['sequence'], 'entry_quote_at': entry['at'], 'exit_at': exit_frame['available_at'], 'quantity_atomic': qty,
               'entry_spend_usd': cost, 'entry_fee_usd': buy['fee_usd'],
               'settled_at': exit_frame['available_at'] + ex['settlement_s'], 'reason': reason}
    if q.get('status', 'ok') == 'token_unsellable':
        return {**base, **details, 'state': 'unsellable', 'net_cash_usd': -cost - buy['fee_usd'] - q['fee_usd'],
                'locked_capital_usd': cost}
    return {**base, **details, 'state': 'closed',
            'net_cash_usd': q['floor_usd'] - cost - buy['fee_usd'] - q['fee_usd']}
