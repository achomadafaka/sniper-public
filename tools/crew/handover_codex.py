"""Offline Codex0.166.0 protocol slice, not a lifecycle host or installed hook.

Binding/caller, current turn/window, clock and semantics proof are host inputs.
Hook notifications only report events: they do NOT prove a hook denied a tool.
Delta's transport owns credentials, completion leases, writer and effect receipts.
No I/O, persistent storage, session launch or capability promotion occurs here.
"""
import hashlib
import json
import re

from .handover import identity, number
from .handover_context import IDENTITY
from .handover_gate import allow_tool, note_turn_finished

PINNED_CLIENT_VERSION = '0.166.0'
BINDING_FIELDS = (*IDENTITY, 'permissions_hash')
MAX_FRAME_BYTES = 65536


def _bound(binding, caller):
    return (isinstance(binding, dict) and isinstance(caller, dict)
            and all(identity(binding.get(k)) and caller.get(k) == binding[k] for k in BINDING_FIELDS)
            and binding['client_version'] == PINNED_CLIENT_VERSION
            and re.fullmatch('[0-9a-f]{64}', binding['permissions_hash']) is not None)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('nonfinite_json')


def normalize_notification(raw_bytes, *, binding, caller, turn_id, window_id, observed_at,
                           semantics_verified=False):
    """Return sample/irrelevant/unknown; only a host-proved sample may trigger.

    Latest inputTokens already includes cached input under the pinned contract.
    Cumulative totals never enter fill math. The host must prove that contract
    at runtime; the default leaves common observe() unknown even on valid JSON.
    """
    unknown = dict(state='unknown', reason='codex_protocol_unverified', needs_page=True)
    try:
        if (not _bound(binding, caller) or not identity(turn_id) or not identity(window_id)
                or not number(observed_at) or type(semantics_verified) is not bool
                or not isinstance(raw_bytes, bytes) or not 0 < len(raw_bytes) <= MAX_FRAME_BYTES):
            return unknown
        packet = json.loads(raw_bytes, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
        if not isinstance(packet, dict) or not identity(packet.get('method')):
            return unknown
        params = packet.get('params')
        if not isinstance(params, dict) or params.get('threadId') != binding['session_id']:
            return unknown
        if params.get('turnId') != turn_id:
            if identity(params.get('turnId')):
                # A bound late turn is not a new failure of the current turn's usage.
                return dict(state='unknown', reason='codex_turn_mismatch', needs_page=False)
            return unknown
        method = packet['method']
        sample = {k: binding[k] for k in IDENTITY}
        sample.update(backend='codex', observed_at=observed_at, window_id=window_id,
            event_id=hashlib.sha256(raw_bytes).hexdigest(), provenance='bound_codex_app_server',
            semantics_verified=semantics_verified, compaction=False)
        if method == 'thread/tokenUsage/updated':
            usage = params.get('tokenUsage')
            if not isinstance(usage, dict) or not isinstance(usage.get('last'), dict):
                return unknown
            last = usage['last'];window = usage.get('modelContextWindow')
            fields = ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningOutputTokens', 'totalTokens')
            if (any(type(last.get(k)) is not int or not number(last[k]) for k in fields)
                    or type(last.get('cacheWriteInputTokens', 0)) is not int
                    or not number(last.get('cacheWriteInputTokens', 0))
                    or type(window) is not int or not number(window) or window == 0
                    or last['inputTokens'] > window or last['cachedInputTokens'] > last['inputTokens']
                    or last['totalTokens'] < last['inputTokens']):
                return unknown
            sample.update(input_tokens=last['inputTokens'], window_tokens=window)
        elif method in ('hook/started', 'hook/completed'):
            run = params.get('run')
            if not isinstance(run, dict) or not identity(run.get('id')):
                return unknown
            expected = 'preCompact' if method == 'hook/started' else 'postCompact'
            if run.get('eventName') != expected:
                return dict(state='irrelevant')
            status = run.get('status', 'unknown')
            if status not in ('unknown', 'running', 'completed', 'failed', 'blocked', 'stopped'):
                return unknown
            sample.update(compaction=True, input_tokens=None, window_tokens=None, hook_status=status)
        elif method == 'item/completed':
            item = params.get('item')
            if not isinstance(item, dict) or not identity(item.get('id')):
                return unknown
            if item.get('type') != 'contextCompaction':
                return dict(state='irrelevant')
            sample.update(compaction=True, input_tokens=None, window_tokens=None)
        else:
            return dict(state='irrelevant')
        return dict(state='sample', sample=sample)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return unknown


def pending_tool_allowed(state, *, binding, caller, tool, arguments, allowlist):
    """Pinned host caller + common exact packet fence; not a Codex hook response."""
    if not _bound(binding, caller):
        return False
    return allow_tool(state, session_id=binding['session_id'], incarnation=binding['incarnation'],
                      tool=tool, arguments=arguments, allowlist=allowlist)


def finish_note_turn(state, *, binding, caller, valid):
    """Host supplies valid only after its own checkpoint/lease validation."""
    if (not _bound(binding, caller) or not isinstance(state, dict)
            or state.get('session_id') != binding['session_id']
            or state.get('incarnation') != binding['incarnation']):
        return dict(phase='parked', needs_page=True, reason='codex_note_binding_unverified')
    return note_turn_finished(state, valid=valid)
