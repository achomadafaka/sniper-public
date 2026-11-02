"""Typed subscription transports. The supervisor supplies a trusted corrected clock."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import time
from typing import Literal
from uuid import uuid4
from . import model_diagnostics as diagnostics

Outcome = Literal['success','empty','stale','partial','timeout','rate_limited',
                  'auth_revoked','refused','invalid','schema_error']


@dataclass(frozen=True)
class Envelope:
    requested_model: str
    requested_provider: str
    outcome: Outcome
    served_model: str | None = None
    served_model_basis: str = 'unknown'
    result_id: str | None = None
    session_id: str | None = None
    text: str | None = None
    usage: dict | None = None
    billing_basis: Literal['subscription','api','unknown'] = 'unknown'
    started_at: float | None = None
    ended_at: float | None = None
    attempt_id: str = field(default_factory=lambda: str(uuid4()))
    parent_attempt_id: str | None = None
    account_id: str | None = None
    price_list_snapshot_id: str | None = None
    waiting_s: float = 0.
    throttled_s: float = 0.
    guard_error: dict | None = None
    error_reason: str | None = None
    latency_s: float | None = None
    cost_usd: float | None = None
    cost_basis: str = 'unknown'
    invalid_metadata_n: int = 0
    provider_error_kind: str | None = None
    provider_error_detail: str | None = None


def _claude_adapter(system, user, **kwargs):
    from scripts.memecoin_desk_judge_worker import sdk_call_model
    kwargs.pop('schema_obj', None)
    return sdk_call_model(system, user, persist_session=False, **kwargs)


def _codex_adapter(system, user, **kwargs):
    from sniper.services.memecoin_desk_gpt import call_model
    # This adapter already acquires the shared codex_slot, including isolation/auth rotation.
    return call_model(system, user, **kwargs)


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _time(clock):
    try:
        value = clock() if clock is not None else None
        return float(value) if _finite(value) else None
    except Exception:
        return None


def _object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('duplicate_key')
    return result


def _reject_constant(value):
    raise ValueError('nonfinite_json')


def call(provider, model, system, user, *, deadline_s, schema_obj=None, clock=None,
         account_id=None, price_list_snapshot_id=None, parent_attempt_id=None, transport=None) -> Envelope:
    """One attempt, never a fallback/retry. Unknown corrected time refuses before I/O.

    Caller owns semantic action validation and sandboxed process lifetime. Schema validation
    here rejects malformed structured replies; it grants no tool or trade authority.
    """
    started = _time(clock)
    meta = dict(requested_provider=provider, requested_model=model, started_at=started,
                account_id=account_id, price_list_snapshot_id=price_list_snapshot_id,
                parent_attempt_id=parent_attempt_id)
    if started is None:
        return Envelope(**meta, outcome='refused', error_reason='clock_unknown')
    if (provider not in ('claude','codex') or not isinstance(model,str) or not model
            or not _finite(deadline_s) or deadline_s <= 0 or (transport is not None and not callable(transport))):
        return Envelope(**meta, outcome='refused', ended_at=started, error_reason='configuration')
    # Validate the schema before any provider call; reuse the installed JSON Schema library.
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry
        schema = {'type':'object'} if schema_obj is None else schema_obj
        if not isinstance(schema,dict) or schema.get('type') != 'object':
            raise ValueError('schema')
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, registry=Registry())
    except Exception:
        return Envelope(**meta, outcome='schema_error', ended_at=started, error_reason='schema_configuration')
    begun = time.monotonic()
    try:
        adapter = transport if transport is not None else (_claude_adapter if provider == 'claude' else _codex_adapter)
        raw = adapter(system, user, model=model, deadline_s=deadline_s, schema_obj=schema)
    except TimeoutError:
        raw = {'call_error':'timeout'}
    except Exception:
        # Never store exception text: SDK messages can contain credentials or prompt content.
        raw = {'call_error':'transport_error'}
    elapsed = time.monotonic() - begun
    ended = _time(clock)
    meta['ended_at'] = ended
    if not isinstance(raw,dict):
        raw = {'call_error':'invalid_result'}
    error = raw.get('call_error')
    mapped = {**{key:'refused' for key in ('claude_sdk_vendor_missing','claude_sdk_vendor_mismatch','claude_sdk_wheel_invalid','claude_sdk_manifest_mismatch','claude_sdk_import_conflict','claude_sdk_missing','claude_sdk_version_mismatch','claude_cli_hash_mismatch','claude_cli_version_unknown','claude_cli_too_old')}, 'claude_auth_near_expiry':'refused', 'claude_auth_unknown':'auth_revoked',
              'launcher_identity':'refused', 'launcher_refused':'refused', 'auth_snapshot_path':'refused',
              'existing_unknown':'partial', 'rate_limit':'rate_limited', 'timeout':'timeout', 'provider_timeout':'timeout', 'rate_limited':'rate_limited',
              'provider_paused':'refused', 'provider_busy':'rate_limited', 'codex_busy':'rate_limited', 'auth_expired':'auth_revoked', 'auth_revoked':'auth_revoked',
              'codex_cleanup_unproven':'partial', 'codex_auth_writeback_refused':'auth_revoked',
              'codex_auth_snapshot_missing':'auth_revoked', 'trusted_auth_bindings_required':'refused',
              'auth_private_state':'refused', 'no_codex_login':'auth_revoked', 'refusal':'refused', 'refused':'refused',
              'no_isolation':'refused', 'cli_missing':'refused', 'output_budget_unsupported':'refused',
              'stale':'stale', 'partial':'partial', 'schema_error':'schema_error', 'structured_output_missing':'schema_error'}
    outcome = mapped.get(error,'invalid') if error else 'invalid' if raw.get('is_error') else 'success'
    text = raw.get('text')
    if outcome == 'success':
        if text is None or isinstance(text,str) and not text.strip():
            outcome = 'empty'
        elif not isinstance(text,str):
            outcome = 'invalid'
        else:
            try:
                parsed = json.loads(text,object_pairs_hook=_object,parse_constant=_reject_constant)
                if not isinstance(parsed,dict):
                    raise ValueError('not_object')
            except (ValueError,TypeError,RecursionError):
                outcome = 'invalid'
            else:
                try:
                    valid = validator.is_valid(parsed)
                except Exception:
                    valid = False
                if not valid:
                    outcome = 'schema_error'
    from sniper.services.memecoin_desk_model_echo import echo_ok
    if outcome == 'success' and not echo_ok(raw.get('model_echo'), model):
        outcome, error = 'invalid', 'model_mismatch'
    if elapsed >= deadline_s:
        outcome, error = 'timeout','deadline'
    elif ended is None or ended < started:
        outcome, error = 'stale','clock_unknown' if ended is None else 'clock_regressed'
    usage = raw.get('usage')
    counters = {'input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens'}
    invalid_metadata_n = (sum(k in counters and not (type(v) is int and 0 <= v <= 9223372036854775807)
                              for k,v in usage.items()) if isinstance(usage,dict) else int(usage is not None))
    usage = ({k:v for k,v in usage.items() if k in counters and type(v) is int and 0 <= v <= 9223372036854775807}
             if isinstance(usage,dict) else {})
    if usage and account_id is not None:
        usage['account_id'] = account_id
    served = raw.get('model_echo')
    basis = {'subscription_flat':'subscription','subscription':'subscription',
             'billed_receipt':'api','api':'api'}.get(raw.get('billing_basis'),'unknown')
    cost=raw.get('cost_usd')
    cost_basis=raw.get('cost_basis')
    valid_cost = _finite(cost) and cost >= 0 and (type(cost) is not int or cost <= 9223372036854775807)
    invalid_metadata_n += int(cost is not None and not valid_cost)
    measured=valid_cost and cost_basis in ('billed_receipt','provider_reported')
    return Envelope(**meta, outcome=outcome, text=text if outcome=='success' else None,
                    usage=usage or None, billing_basis=basis,
                    latency_s=elapsed,cost_usd=float(cost) if measured else None,cost_basis=cost_basis if measured else 'unknown',
                    invalid_metadata_n=invalid_metadata_n,
                    served_model=served if isinstance(served,str) and served else None,
                    served_model_basis='echoed' if isinstance(served,str) and served else
                        'requested_pinned' if provider=='codex' and raw.get('served_model_basis')=='requested_pinned' else 'unknown',
                    session_id=raw.get('session_id') if isinstance(raw.get('session_id'),str) else None,
                    result_id=raw.get('result_id') if isinstance(raw.get('result_id'),str) else None,
                    error_reason=error if outcome!='success' and isinstance(error,str) else None,
                    provider_error_kind=diagnostics.kind(raw.get('provider_error_kind')) if outcome!='success' else None,
                    provider_error_detail=diagnostics.detail(raw.get('provider_error_detail'),prompts=(system,user)) if outcome!='success' else None)


def independent_families(a: Envelope, b: Envelope) -> bool | None:
    if a.outcome != 'success' or b.outcome != 'success':
        return None
    def family(model):
        if isinstance(model,str):
            if model.startswith('claude-'): return 'claude'
            if model.startswith(('gpt-','o1','o3','o4')): return 'gpt'
        return None
    left,right = family(a.served_model),family(b.served_model)
    return None if left is None or right is None else left != right
