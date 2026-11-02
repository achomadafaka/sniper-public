"""Pure structural eligibility. External provenance must also pass an independent review.

An eligible episode supplies a model input, not a proven edge or funded authority.
No provider, database, clock or environment is read here.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import math
from collections import Counter

PROTOCOL = 'wallet-execution-research-v1'
COSTS = frozenset({'gas', 'tax', 'impact', 'slippage', 'approval'})

@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    codes: tuple[str, ...]
    details: tuple[str, ...] = ()

    def as_dict(self):
        return {'eligible': self.eligible, 'codes': list(self.codes), 'details': list(self.details)}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def number(value, *, positive=False):
    if isinstance(value, bool):
        return False
    return isinstance(value, (float, int)) and math.isfinite(value) and (value > 0 if positive else value >= 0)


def atomic(value):
    return isinstance(value, str) and value.isascii() and value.isdecimal() and int(value) > 0


def hash_ref(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def dossier_admission_gate(receipt, baseline, origin, *, now):
    """Check recorded admission evidence. External provenance still needs review.

    All-account prestate and gate transcripts are producer observations, not
    authenticated chain proofs. Hashes bind their bytes; they do not prove truth.
    """
    codes = []
    if not isinstance(receipt, dict) or receipt.get('protocol') != 'wallet-fast-move-admission-v2':
        return Eligibility(False, ('admission_evidence_missing_or_legacy',))
    body = receipt.get('evidence')
    if not isinstance(body, dict): return Eligibility(False, ('admission_evidence_missing_or_legacy',))
    try:
        if receipt.get('admission_digest') != digest(body): codes.append('admission_digest_mismatch')
        if any(not isinstance(body.get(k), str) or not body[k].strip() or body[k] != origin.get(k)
               for k in ('origin_id', 'wallet', 'chain', 'mint')):
            codes.append('source_identity_missing_or_mismatched')
        source = body.get('source') or {}
        if not isinstance(source, dict): source = {}
        raw, identity, proof = (source.get(k) or {} for k in ('raw', 'identity_raw', 'proof'))
        source_fields = ('transaction', 'address', 'paid_atomic', 'acquired_atomic')
        if (source.get('kind') != 'paid_swap' or source.get('identity_status') != 'verified'
            or any(not isinstance(source.get(k), str) or not source[k].strip() for k in ('transaction', 'address'))
            or not atomic(source.get('paid_atomic')) or not atomic(source.get('acquired_atomic'))
            or not number(source.get('spent_usd'), positive=True)
            or not isinstance(source.get('decimals'), int) or isinstance(source['decimals'], bool)
            or not 0 <= source['decimals'] <= 36
            or not isinstance(raw, dict) or not isinstance(identity, dict) or not isinstance(proof, dict)
            or proof.get('transaction_digest') != digest(raw) or proof.get('identity_digest') != digest(identity)
            or any(raw.get(k) != source.get(k) for k in source_fields)
            or any(raw.get(k) != body.get(k) for k in ('chain', 'mint'))
            or identity != {'wallet': body.get('wallet'), 'chain': body.get('chain'), 'address': source.get('address')}):
            codes.append('source_paid_swap_unproven')
        from datetime import datetime
        observed = datetime.fromisoformat(origin['observed_at'])
        source_at, available, received = (source.get(k) for k in ('source_at', 'available_at', 'received_at'))
        decision = body.get('decision_at')
        if (observed.tzinfo is None or not all(number(t) for t in (source_at, available, received, decision))
            or not source_at <= available <= received <= decision <= now
            or abs(observed.timestamp() - received) > .001):
            codes.append('decision_time_receipt_missing')
        if (not baseline or body.get('baseline_id') != baseline['id']
            or digest(body.get('defaults')) != baseline['defaults_digest']
            or not number(received) or received < datetime.fromisoformat(baseline['sealed_at']).timestamp()):
            codes.append('legacy_or_different_defaults')
        cap = body.get('market_cap') or {}
        if (not isinstance(cap, dict) or not number(cap.get('value_usd'), positive=True)
            or not isinstance(cap.get('raw'), dict) or not cap['raw']):
            codes.append('market_cap_unknown')
        elif cap['value_usd'] >= 1000000: codes.append('market_cap_out_of_scope')
        if not isinstance(cap, dict) or not number(cap.get('available_at')) or not number(decision) or cap['available_at'] > decision:
            codes.append('market_cap_after_decision_or_unknown')
        opening = body.get('opening') or {}
        if not isinstance(opening, dict): opening = {}
        accounts = opening.get('accounts')
        order = source.get('order')
        if (opening.get('scope') != 'wallet_mint_prestate' or opening.get('complete') is not True
            or not isinstance(order, list) or len(order) != 3
            or any(not isinstance(n, int) or isinstance(n, bool) or n < 0 for n in order)
            or opening.get('before_order') != order
            or any(opening.get(k) != source.get(k) for k in ('address', 'transaction'))
            or any(opening.get(k) != body.get(k) for k in ('chain', 'mint'))
            or not number(opening.get('available_at')) or not number(decision) or opening['available_at'] > decision
            or not isinstance(accounts, list)):
            codes.append('new_position_unproven_or_addon')
        else:
            seen = set()
            for account in accounts:
                if (not isinstance(account, dict) or not isinstance(account.get('address'), str) or not account['address']
                    or account['address'] in seen or account.get('owner') != source.get('address')
                    or account.get('mint') != body.get('mint') or account.get('amount_atomic') != '0'):
                    codes.append('new_position_unproven_or_addon'); break
                seen.add(account['address'])
        gates = body.get('gates')
        if (body.get('exemptions') != [] or body.get('wallet_overrides') != {} or not isinstance(gates, dict)
            or not baseline or set(gates) != set(baseline['required_gates'])):
            codes.append('default_admission_unproven_or_rejected')
        else:
            for name, gate in gates.items():
                if (not isinstance(gate, dict) or gate.get('passed') is not True
                    or digest(gate.get('default')) != digest(baseline['defaults'][name])
                    or not isinstance(gate.get('inputs'), dict) or not gate['inputs']
                    or not number(gate.get('available_at')) or not number(decision) or gate['available_at'] > decision):
                    codes.append('default_admission_unproven_or_rejected'); break
    except (KeyError, TypeError, ValueError, OverflowError):
        codes.append('admission_evidence_malformed')
    return Eligibility(not codes, tuple(dict.fromkeys(codes)))


def episode_gate(episode, *, decision_at=None):
    codes, details = [], []
    def fail(code, detail):
        if code not in codes: codes.append(code)
        if len(details) < 16: details.append(detail)
    if not isinstance(episode, dict) or episode.get('protocol') != PROTOCOL:
        return Eligibility(False, ('protocol_missing_or_legacy',))
    e = episode
    source, window, execution = (e.get(k) or {} for k in ('source', 'window', 'execution'))
    if not all(isinstance(x, dict) for x in (source, window, execution)):
        return Eligibility(False, ('execution_evidence_missing',))
    proof = source.get('proof') or {}
    if (not all(e.get(k) for k in ('origin_id', 'wallet', 'chain', 'mint'))
        or source.get('kind') != 'paid_swap' or source.get('identity_status') != 'verified'
        or not all(source.get(k) for k in ('address', 'transaction'))
        or not atomic(source.get('paid_atomic')) or not atomic(source.get('acquired_atomic'))
        or not isinstance(proof, dict)
        or not all(hash_ref(proof.get(k)) for k in ('transaction_digest', 'identity_digest'))):
        fail('identity_unproven', 'paid source swap and decision-time address proof required')
    clocks = [source.get(k) for k in ('source_at', 'available_at', 'received_at')]
    if not all(isinstance(t, (float, int)) and not isinstance(t, bool) and math.isfinite(t) for t in clocks):
        fail('ordering_or_lookahead', 'source clocks missing')
        received = None
    else:
        received = clocks[-1]
        if clocks != sorted(clocks): fail('ordering_or_lookahead', 'source clocks regress')
    order = source.get('order')
    if not isinstance(order, list) or len(order) != 3 or not all(number(t) and int(t) == t for t in order):
        fail('ordering_or_lookahead', 'source chain order unknown')
    if not number(source.get('spent_usd'), positive=True) or not isinstance(source.get('decimals'), int) or not 0 <= source['decimals'] <= 36:
        fail('execution_evidence_missing', 'source value/decimals unknown')
    try:
        start, end = float(window['start']), float(window['end'])
        wait, hold, margin = (float(window[k]) for k in ('entry_wait_s', 'max_hold_s', 'settlement_margin_s'))
        if not all(math.isfinite(t) for t in (start, end, wait, hold, margin)) or min(wait, hold, margin) < 0:
            raise ValueError
        required = received + max(1800., wait + hold + margin) if received is not None else end + 1
        if received is None or start > received - 120 or end < required: fail('path_resolution_or_gap', 'registered context/horizon absent')
    except (KeyError, TypeError, ValueError):
        fail('path_resolution_or_gap', 'invalid window')
        start = end = None
    if (not all(number(execution.get(k)) for k in ('entry_delay_s', 'exit_delay_s', 'settlement_s', 'max_quote_age_s'))
        or not COSTS.issubset(execution.get('cost_components') or [])
        or not hash_ref(execution.get('cost_model_digest'))
        or not hash_ref(execution.get('calibration_digest'))):
        fail('cost_or_latency_unknown', 'explicit costs and prior measured delay required')
    before = execution.get('calibration_before')
    if not isinstance(before, (float, int)) or not math.isfinite(before) or received is None or before >= received:
        fail('ordering_or_lookahead', 'delay calibration is not prior')
    if execution.get('state_verified') is not True:
        fail('execution_evidence_missing', 'route-state pricing not calibrated')
    frames = e.get('frames')
    if not isinstance(frames, list) or not frames:
        fail('path_resolution_or_gap', 'no path')
        return Eligibility(False, tuple(codes), tuple(details))
    previous = None
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            fail('execution_evidence_missing', 'malformed frame'); continue
        at, available, seq = (frame.get(k) for k in ('at', 'available_at', 'sequence'))
        if not all(isinstance(t, (float, int)) and not isinstance(t, bool) and math.isfinite(t) for t in (at, available, seq)):
            fail('ordering_or_lookahead', 'missing frame clocks'); continue
        if available < at or (previous and (at <= previous[0] or available < previous[1] or seq <= previous[2])):
            fail('ordering_or_lookahead', 'frame order regresses or publication precedes state')
        if previous and at - previous[0] > 1.000001:
            fail('path_resolution_or_gap', 'uncovered second')
        if any(k in frame for k in ('high', 'low', 'ohlc')):
            fail('ordering_or_lookahead', 'OHLC is not ordered executable state')
        previous = (at, available, seq)
        try: raw_ok = hash_ref(frame.get('raw_digest')) and digest(frame.get('raw')) == frame['raw_digest']
        except (ValueError, TypeError): raw_ok = False
        if str(frame.get('chain')) != str(e.get('chain')) or not raw_ok:
            fail('execution_evidence_missing', 'raw state digest/chain mismatch')
        if frame.get('quality') != 'ok': fail('outcome_censored', 'state observation failure')
        extra = frame.get('entry_quotes', [])
        if not isinstance(extra, list):
            fail('execution_evidence_missing', 'entry alternatives must be a list'); extra = []
        spends = set()
        for buy in [frame.get('buy') or {}, *extra]:
            if (not isinstance(buy, dict) or not atomic(buy.get('quantity_atomic')) or not atomic(buy.get('floor_atomic'))
                or not isinstance(buy.get('decimals'), int) or isinstance(buy.get('decimals'), bool)
                or buy.get('decimals') != source.get('decimals')
                or not number(buy.get('spend_usd'), positive=True) or not buy.get('route')
                or (atomic(buy.get('floor_atomic')) and atomic(buy.get('quantity_atomic'))
                    and int(buy['floor_atomic']) > int(buy['quantity_atomic']))):
                fail('execution_evidence_missing', 'invalid entry quantity/route')
            if not isinstance(buy, dict) or not number(buy.get('fee_usd')):
                fail('cost_or_latency_unknown', 'entry cost unknown')
            if isinstance(buy, dict) and number(buy.get('spend_usd'), positive=True):
                if buy['spend_usd'] in spends: fail('execution_evidence_missing', 'ambiguous entry amount')
                spends.add(buy['spend_usd'])
        sells = frame.get('sells')
        if not isinstance(sells, dict) or not sells: fail('execution_evidence_missing', 'no quantity-bound exits')
        else:
            for quantity, quote in sells.items():
                if not atomic(quantity) or not isinstance(quote, dict):
                    fail('execution_evidence_missing', 'bad exit quantity'); continue
                status = quote.get('status', 'ok')
                if status == 'token_unsellable':
                    if not hash_ref(quote.get('proof')) or not number(quote.get('fee_usd')):
                        fail('execution_evidence_missing', 'unsellable needs token evidence and failed-attempt fee')
                elif status != 'ok': fail('outcome_censored', f'exit provider/state failure: {status}')
                elif (not number(quote.get('floor_usd')) or not number(quote.get('expected_usd'))
                      or quote['floor_usd'] > quote['expected_usd'] or not quote.get('route')):
                    fail('execution_evidence_missing', 'bad whole-quantity exit output')
                if status == 'ok' and not number(quote.get('fee_usd')):
                    fail('cost_or_latency_unknown', 'exit cost unknown')
    if start is not None and (not isinstance(frames[0], dict) or not isinstance(frames[-1], dict)
        or not isinstance(frames[0].get('at'), (int, float)) or not isinstance(frames[-1].get('at'), (int, float))
        or frames[0]['at'] > start or frames[-1]['at'] < end):
        fail('path_resolution_or_gap', 'path does not reach window boundaries')
    if decision_at is not None:
        features = e.get('features') or {}
        if not isinstance(features, dict): fail('ordering_or_lookahead', 'feature availability missing')
        else:
            for feature in features.values():
                if (not isinstance(feature, dict) or not isinstance(feature.get('available_at'), (int, float))
                    or isinstance(feature['available_at'], bool) or not math.isfinite(feature['available_at'])
                    or feature['available_at'] > decision_at):
                    fail('ordering_or_lookahead', 'feature unavailable at decision')
    return Eligibility(not codes, tuple(codes), tuple(details))


def study_gate(manifest, episodes):
    """Validate a sealed future comparison, never infer readiness from close counts.

    Independent provenance/method review is mandatory in addition to structural
    checks; no legacy publisher can manufacture this from old PnL summaries.
    """
    # No independently authenticated study receipt is implemented. Caller hashes
    # and booleans cannot unlock publication, even if structural checks pass.
    codes = ['verified_study_receipt_unavailable']
    def fail(c):
        if c not in codes: codes.append(c)
    if not isinstance(manifest, dict) or manifest.get('protocol') != PROTOCOL:
        return Eligibility(False, ('protocol_missing_or_legacy',))
    m = manifest
    origins = [e.get('origin_id') for e in episodes]
    if (not episodes or len(origins) != len(set(origins)) or not all(origins)
        or set(m.get('origins') or []) != set(origins)
        or any(e.get('wallet') != m.get('wallet') or str(e.get('chain')) != str(m.get('chain')) for e in episodes)):
        fail('cohort_invalid')
    if m.get('episodes_digest') != digest(episodes): fail('execution_evidence_missing')
    for e in episodes:
        for code in episode_gate(e).codes: fail(code)
    trials = m.get('trials')
    if not isinstance(trials, list) or not 1 <= len(trials) <= 9 or len({digest(t) for t in trials}) != len(trials):
        fail('validation_missing')
    try:
        sealed, discovery_end, validation_start, validation_end, test_start, test_end = (
            float(m[k]) for k in ('sealed_at', 'discovery_end', 'validation_start', 'validation_end', 'test_start', 'test_end'))
        embargo = max(1800., max(float(e['window']['entry_wait_s']) + float(e['window']['max_hold_s'])
                                + float(e['window']['settlement_margin_s']) for e in episodes))
        if not discovery_end <= sealed < validation_start <= validation_end < test_start < test_end:
            fail('validation_missing')
        if validation_start - discovery_end < embargo or test_start - validation_end < embargo:
            fail('validation_missing')
        if any(not test_start <= e['source']['received_at'] <= test_end for e in episodes): fail('cohort_invalid')
    except (KeyError, TypeError, ValueError): fail('validation_missing')
    power = m.get('power') or {}
    if (not isinstance(power, dict) or not number(power.get('minimum_detectable_cash'), positive=True)
        or not hash_ref(power.get('training_evidence_digest'))
        or not number(power.get('min_coin_clusters'), positive=True)
        or not number(power.get('min_days'), positive=True)
        or not hash_ref(m.get('trial_ledger_digest')) or m.get('holdout_read_count') != 1
        or m.get('outcome_complete') is not True or m.get('census_complete') is not True
        or m.get('multiplicity') != 'holm_coin_day_clustered'
        or not hash_ref(m.get('independent_review_digest'))
        or not hash_ref(m.get('sealed_store_receipt'))):
        fail('validation_missing')
    else:
        coins = {(e['chain'], e['mint']) for e in episodes}
        days = {int(e['source']['received_at'] // 86400) for e in episodes}
        if len(coins) < power['min_coin_clusters'] or len(days) < power['min_days']: fail('validation_missing')
    return Eligibility(not codes, tuple(codes))
