"""A seat's read of a ticket whose full sheet could not be built: what the ticket already holds, with everything else typed unknown.

Display only. The full sheet (tools/sheet.py) stays the one admission input: v2_paper.enter() still needs state 'known', and a partial view is marked order_ready false and carries no cache
key, receipt, sheet hash or lens. Nothing here reads a provider, the tape or a database: it projects the stored ticket (emission snapshot, hard-filter evidence, mint receipt) and names why the
full sheet failed. Values are as recorded at emission/evidence time, never refreshed; their age against the read is stated so a reader cannot take them for current.

Bounded and total: any malformed part of a ticket is typed unknown, never an exception; strings, containers and the whole view have byte limits (omissions are named); a non-finite number
becomes null and is named, never 0 and never NaN on the wire.
"""
import json
import math

MAX_VIEW_BYTES = 16000       # the seat tool's own limit is 20000 (MAX_SHEET); a view over this drops detail, never identity or order_ready
MAX_STR, MAX_KEYS, MAX_LIST, MAX_FIELDS, MAX_UNKNOWN = 160, 12, 8, 40, 60
MINT_BOUND_S = 3600.0        # desk_near_pass.MINT_MAX_AGE_S, the bound the full sheet applies to the receipt
FAILURE_CLASSES = (       # (reason fragment, class, plain meaning): the first match wins
    ('intake_mint_unusable', 'mint_receipt_too_old_or_invalid', 'the stored mint receipt is older than the sheet bound or fails its checks; supply and decimals below are the stored values, not re-proven'),
    ('intake_mint_', 'mint_receipt_unusable', 'the stored mint receipt cannot be used for a refreshed sheet'),
    ('tape_gap_since_cut', 'tape_loss_since_cut', 'a loss or capture gap on this pool lies between the market cut and the read, so the current pool state cannot be proven'),
    ('ps_commit_unproven', 'tape_commit_unproven', 'the tape heartbeat did not prove the commit point at the read'),
    ('safety_evidence', 'evidence_not_pass_at_read', 'the stored admission evidence no longer passes the hard filter at the read time (it expires)'),
    ('latest_swap_not_buy', 'pool_state_unproven', 'the last stored swap is not a buy, so the pool state after it is not proven'),
    ('virtual_quote_unproven', 'pool_state_unproven', 'the virtual quote is not proven'),
    ('hard_filter_missing', 'ticket_incomplete', 'the ticket has no hard-filter stage'),
    ('market_cut_unknown', 'ticket_incomplete', 'the ticket has no market cut'),
    ('not_near_pass_eligible', 'ticket_not_eligible', 'the ticket is not in the near-pass stratum'),
)


def _num(v):
    return type(v) in (int, float) and math.isfinite(v)


def _dict(v):
    return v if isinstance(v, dict) else {}


def _list(v):
    return v if isinstance(v, list) else []


def _epoch(value):
    """Epoch seconds from a number or an ISO text stamp; anything else is unknown."""
    if _num(value):
        return value
    if isinstance(value, str):
        from datetime import datetime
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def failure_class(reason):
    text = reason if isinstance(reason, str) else ''
    for fragment, name, meaning in FAILURE_CLASSES:
        if fragment in text:
            return name, meaning
    return 'other', 'the full sheet could not be built for a reason not classified here'


class _Clean:
    """Bounded copy of stored values. Strings are cut, containers capped with the omission counted, non-finite numbers become None and are named in `nonfinite`."""

    def __init__(self):
        self.nonfinite = []
        self.cut_strings = 0

    def cut(self, text, limit=MAX_STR):
        """A string cut to `limit`; every cut is counted so it is never silent."""
        if len(text) > limit:
            self.cut_strings += 1
            return text[:limit]
        return text

    def scalar(self, v, where):
        if isinstance(v, bool) or v is None or isinstance(v, int):
            return v
        if isinstance(v, float):
            if math.isfinite(v):
                return v
            self.nonfinite.append(where)
            return None
        return self.cut(v) if isinstance(v, str) else str(type(v).__name__)

    def small(self, v, where, depth=0):
        if isinstance(v, dict) and depth == 0:
            keys = [k for k in v if isinstance(k, str)][:MAX_KEYS]
            out = {self.cut(k, 60): self.small(v[k], where + '.' + self.cut(k, 60), 1) for k in keys}
            if len(v) > len(keys):
                out['_omitted_keys'] = len(v) - len(keys)
            return out
        if isinstance(v, (dict, list)):
            return {'_omitted': 'nested detail not shown (%d entries)' % len(v)}
        return self.scalar(v, where)


def _field(c, name, f):
    f = _dict(f)
    return {k: c.scalar(f.get(k), 'snapshot.%s.%s' % (name, k)) for k in ('value', 'completeness', 'known_at', 'slot', 'source')}


def partial_sheet(ticket, got, *, now_host):
    """-> {state: 'partial', reason, sheet: {...}}. `got` is the failed full-sheet result ({state, reason}). `ticket` is desk.get_ticket's dict (may be thin or malformed). Never raises: a missing or
    malformed part is listed under `unknown` with why."""
    got = _dict(got)
    ticket, c = _dict(ticket), _Clean()
    reason = c.cut(got['reason'], 200) if isinstance(got.get('reason'), str) else 'unknown'
    cls, meaning = failure_class(reason)
    cand = _dict(ticket.get('candidate'))
    snap = _dict(cand.get('snapshot'))
    stages = [s for s in _list(ticket.get('stages')) if isinstance(s, dict)]
    hf = next((s for s in stages if s.get('stage') == 'hard_filter'), None)
    evidence = _dict(_dict((hf or {}).get('inputs')).get('evidence')) if hf else {}
    now = now_host if _num(now_host) else None
    unknown = {}
    body = {'partial': True, 'order_ready': False,
            'order_note': 'Orders need a known full sheet; this view cannot admit one.',
            'why_partial': {'full_sheet_state': c.cut(got['state'], 40) if isinstance(got.get('state'), str) else None, 'reason': reason, 'class': cls, 'meaning': meaning},
            'read_at_host': now,
            'identity': {k: c.scalar(ticket.get(k, cand.get(k)), 'identity.' + k) for k in ('ticket_id', 'network_id', 'mint', 'pool', 'producer', 'emitted_at')},
            'values_note': 'Values below are as recorded at emission or evidence time and are NOT refreshed; age_s says how old they are against this read.'}
    body['identity']['band_entry_slot'] = c.scalar(cand.get('band_entry_slot'), 'identity.band_entry_slot')
    emission = snap.get('emission_at')
    fields = _dict(snap.get('fields'))
    if fields:
        names = [n for n in fields if isinstance(n, str)][:MAX_FIELDS]
        missing = [n for n in _list(snap.get('missing')) if isinstance(n, str)][:MAX_FIELDS]
        body['emission_snapshot'] = {'emission_at': c.scalar(emission, 'snapshot.emission_at'),
                                     'age_s': (now - emission) if now is not None and _num(emission) else None,
                                     'fields': {c.cut(n, 60): _field(c, c.cut(n, 60), fields[n]) for n in names}, 'missing_at_emission': [c.cut(m, 60) for m in missing]}
        if len(fields) > len(names):
            body['emission_snapshot']['_omitted_fields'] = len(fields) - len(names)
        for n in missing:
            unknown['snapshot.' + c.cut(n, 60)] = 'absent at emission (%s)' % c.cut(str(_dict(fields.get(n)).get('source')), 60)
    else:
        unknown['emission_snapshot'] = 'ticket carries no usable snapshot fields'
    if hf is not None:
        checks = _dict(evidence.get('checks'))
        known_at = _epoch(evidence.get('known_at'))        # stored as ISO text on tickets
        body['hard_filter'] = {'verdict': c.scalar(hf.get('verdict'), 'hard_filter.verdict'), 'reason': c.scalar(hf.get('reason'), 'hard_filter.reason'), 'at': c.scalar(hf.get('ts'), 'hard_filter.at'),
                               'evidence_known_at': c.scalar(evidence.get('known_at'), 'hard_filter.known_at'),
                               'age_s': (now - known_at) if now is not None and _num(known_at) else None,
                               'checks': {c.cut(n, 60): c.small(checks[n], 'check.' + c.cut(n, 60)) for n in [k for k in checks if isinstance(k, str)][:MAX_KEYS]}}
        if len(checks) > MAX_KEYS:
            body['hard_filter']['_omitted_checks'] = len(checks) - MAX_KEYS
        body['market_cut'] = c.small(evidence.get('market_cut'), 'market_cut')
        for n, chk in list(checks.items())[:MAX_KEYS]:
            if isinstance(n, str) and (not isinstance(chk, dict) or chk.get('status') in ('unknown', None)):
                unknown['check.' + c.cut(n, 40)] = c.cut(str(chk.get('reason') if isinstance(chk, dict) else 'check not a record'), 80) or 'unknown'
        cl = checks.get('cluster') if isinstance(checks.get('cluster'), dict) else None
        cl_status = cl.get('status') if cl and isinstance(cl.get('status'), str) else None          # a list or dict status is unknown, never looked up
        body['derived'] = {'cluster_hit': {'kill': True, 'pass': False}.get(cl_status),
                           'cluster_hit_source': 'hard_filter.checks.cluster.status (kill means hit); None when the check is unknown or absent, never False by default',
                           'lp_locked_or_burned': None, 'lp_locked_or_burned_source': 'no stored source on a ticket; unknown'}
        if cl_status not in ('kill', 'pass'):
            unknown['derived.cluster_hit'] = 'cluster check is unknown or absent'
        unknown['derived.lp_locked_or_burned'] = 'no stored source on a ticket'
    else:
        unknown['hard_filter'] = 'ticket has no usable hard-filter stage'
    intake = _dict(cand.get('intake'))
    if intake:
        body['intake'] = {k: c.scalar(intake.get(k), 'intake.' + k) for k in ('feed', 'provider_rank', 'rank_kind', 'observed_at', 'host_received_at')}
    try:                                                  # the stored mint receipt is an immutable-looking fact the full sheet only rejects for age; show it with that age
        from sniper.services.memecoin_desk_judge import intake_mint_of
        receipt = intake_mint_of(ticket, ('PASS',) if _dict(hf).get('verdict') == 'PASS' else ('SKIP',)) if intake else None
    except Exception:                                     # a thin or odd ticket never breaks the view
        receipt = None
    if isinstance(receipt, dict):
        known = receipt.get('known_at')
        age = (now - known) if now is not None and _num(known) else None
        body['mint_receipt'] = {**{k: c.scalar(receipt.get(k), 'mint_receipt.' + k) for k in ('supply_atomic', 'decimals', 'authority_mint', 'known_at', 'context_slot')},
                                'age_s': age, 'older_than_sheet_bound': (age > MINT_BOUND_S) if age is not None else None,
                                'note': 'stored at intake; supply and decimals do not change but a later burn can lower supply, so mcap from it is an upper bound'}
    else:
        unknown['mint_receipt'] = 'no stored mint receipt on the ticket'
    unknown['refreshed_pool_state_flow_chart_lenses'] = 'not read: the full sheet failed (%s)' % cls
    if c.nonfinite:
        unknown['nonfinite_numbers'] = 'a non-finite number was stored at: ' + ', '.join(c.cut(w, 60) for w in sorted(set(c.nonfinite))[:8])
    omitted = []
    if len(unknown) > MAX_UNKNOWN:
        keep = sorted(unknown)[:MAX_UNKNOWN]
        omitted.append('unknown entries beyond %d: %d' % (MAX_UNKNOWN, len(unknown) - MAX_UNKNOWN))
        unknown = {k: unknown[k] for k in keep}
    body['unknown'] = unknown
    if c.cut_strings:
        body['truncated_strings'] = c.cut_strings
    out = {'state': 'partial', 'reason': reason, 'sheet': body}
    for drop in ('hard_filter.checks', 'emission_snapshot.fields', 'market_cut', 'intake', 'mint_receipt', 'derived', 'hard_filter', 'emission_snapshot'):          # shed detail, name it
        if len(json.dumps(out, allow_nan=False)) <= MAX_VIEW_BYTES:
            break
        part, _, key = drop.partition('.')
        target = body.get(part)
        if key and isinstance(target, dict) and key in target:
            target[key] = {'_omitted': 'dropped to fit the view size'}
        elif not key and part in body:
            body[part] = {'_omitted': 'dropped to fit the view size'}
        omitted.append(drop)
        body['omitted'] = omitted
    if len(json.dumps(out, allow_nan=False)) > MAX_VIEW_BYTES:          # the envelope itself (identity, why_partial, unknown) is still too big: keep only what makes the view safe to read
        body = {'partial': True, 'order_ready': False, 'order_note': body['order_note'],
                'why_partial': {'full_sheet_state': body['why_partial']['full_sheet_state'], 'reason': reason[:100], 'class': cls},
                'identity': {k: (v[:80] if isinstance(v, str) else v) for k, v in body['identity'].items()},
                'unknown': {'view': 'dropped to fit the view size; %d unknown entries were not shown' % len(unknown)}, 'omitted': omitted + ['everything but identity and why_partial']}
        out = {'state': 'partial', 'reason': reason[:100], 'sheet': body}
    elif omitted:
        body['omitted'] = omitted
    return out
