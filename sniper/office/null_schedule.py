"""Pure null submission intents. The caller persists the first bytes and applies them chronologically."""
from dataclasses import dataclass
import hashlib
import math

from sniper.office import ledger
from sniper.office.null_arm import submission_time
from sniper.office.store import canonical_json


@dataclass(frozen=True)
class Intent:
    state: str
    reason: str | None = None
    intent_id: str | None = None
    payload_json: bytes | None = None


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def due_intent(opportunity, *, latency_s, contract, clock_sample, now):
    if not isinstance(opportunity, dict) or not isinstance(contract, dict) or not _finite(now):
        return Intent('refused', 'invalid_null_inputs')
    for name in ('epoch_id', 'book_id', 'fill_model_version'):
        if not isinstance(contract.get(name), str) or not contract[name]:
            return Intent('refused', 'invalid_null_contract')
    if any(not _hash(contract.get(k)) for k in ('policy_hash', 'exit_policy_hash')) or any(
            not _finite(contract.get(k)) or contract[k] <= 0 for k in ('clip_usd', 'initial_capital_usd', 'daily_loss_usd')) or type(contract.get('max_concurrent')) is not int or contract['max_concurrent'] < 1:
        return Intent('refused', 'invalid_null_contract')
    grace = contract.get('tick_grace_s')
    if not _finite(grace) or grace != ledger.TICK_GRACE_S:
        return Intent('refused', 'unsupported_tick_grace')
    if opportunity.get('epoch_id') != contract['epoch_id'] or not _hash(opportunity.get('opportunity_hash')) or type(opportunity.get('candidate_seq')) is not int or opportunity['candidate_seq'] < 1 or any(not isinstance(opportunity.get(k), str) or not opportunity[k] for k in ('network_id', 'mint', 'pool')):
        return Intent('refused', 'invalid_opportunity_identity')
    try:
        submit_at = submission_time(opportunity.get('first_eligible_at'), latency_s)
    except ValueError:
        return Intent('refused', 'infeasible_null_latency')
    identity = dict(opportunity=opportunity, contract=contract, latency_s=latency_s)
    try:
        key = hashlib.sha256(canonical_json(identity)).hexdigest()
    except (ValueError, TypeError):
        return Intent('refused', 'invalid_null_identity')
    if now < submit_at:
        return Intent('pending', intent_id=key)
    if now - submit_at > grace:
        return Intent('censored', 'missed_submit_window', key)
    if not isinstance(clock_sample, dict) or not isinstance(clock_sample.get('clock_sample_id'), str) or not clock_sample['clock_sample_id'] or ledger._offset(clock_sample, now) is None:
        return Intent('censored', 'clock_unknown', key)
    sample = {k: clock_sample[k] for k in ("clock_sample_id", "offset_s", "sampled_at")}
    fill_at = submit_at + ledger.ENTRY_DELAY_S
    if not all(math.isfinite(v) for v in (fill_at, fill_at + sample["offset_s"], now + sample["offset_s"])):
        return Intent("refused", "invalid_clock_targets", key)
    body = dict(identity, intent_id=key, submit_at=submit_at, fill_at=fill_at,
                quote_target_host=fill_at + sample['offset_s'], clock_sample=sample,
                corrected_cut=now, host_cut=now + sample['offset_s'])
    return Intent('due', intent_id=key, payload_json=canonical_json(body))
