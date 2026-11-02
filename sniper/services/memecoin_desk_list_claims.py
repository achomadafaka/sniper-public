"""Frozen cohort-D provider list claims; these are not our measurements."""
import hashlib
import json
import math
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

_PATH = Path(__file__).with_name('desk_context_definitions') / 'list_claims.json'
_BYTES = _PATH.read_bytes()
DEFINITION = json.loads(_BYTES)
KNOWN_KEYS = frozenset(DEFINITION['known_keys'])
UNKNOWN_KEYS = frozenset(DEFINITION['unknown_keys'])
REASONS = frozenset(DEFINITION['reasons'])
FIELD_REASONS = frozenset(DEFINITION['field_reasons'])


def definition():
    return {'version': DEFINITION['version'], 'definition_sha256': hashlib.sha256(json.dumps(DEFINITION, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()}


def _unknown(reason, as_of):
    return {'state': 'unknown', 'reason': reason, 'definition_sha256': definition()['definition_sha256'], 'as_of': as_of}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def numeric(value, *, percent=False, count=False):
    """Provider display units only; a malformed/negative count or USD value is unknown."""
    if type(value) is bool or not isinstance(value, (str, int, float)):
        raise ValueError('parse_cell')
    text = str(value).strip().replace('−', '-').replace('▲', '+').replace('▼', '-')
    text = re.sub(r'\s+', '', text)
    if percent:
        text = text.removesuffix('%')
    else:
        text = text.removeprefix('$')
    if not re.fullmatch(r'[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?[KMBkmb]?', text):
        raise ValueError('parse_cell')
    multiplier = {'K': 1000, 'M': 1000000, 'B': 1000000000}.get(text[-1:].upper(), 1)
    if multiplier != 1:
        text = text[:-1]
    try:
        number = Decimal(text.replace(',', '')) * multiplier
        if not number.is_finite() or abs(number) > Decimal('1e15') or (not percent and number < 0):
            raise ValueError('parse_cell')
        if count:
            if number != number.to_integral_value():
                raise ValueError('parse_cell')
            return int(number)
        return float(number)
    except InvalidOperation:
        raise ValueError('parse_cell') from None


def _lines(value):
    if not isinstance(value, str):
        return []
    return value.replace('\\n', '\n').replace('\\u25b2', '▲').replace('\\u25bc', '▼').splitlines()


def symbol_of(raw, feed):
    """Provider symbol, used locally for matching; never exported as model text."""
    symbol = raw.get('symbol') if isinstance(raw, dict) else None
    if feed == 'fomo' and isinstance(raw, dict):
        cells = raw.get('item', {}).get('cells', [])
        lines = _lines(cells[0]) if cells else []
        symbol = lines[0] if lines else None
    if not isinstance(symbol, str) or not re.fullmatch(r'[\w-]{1,32}', symbol):
        return None
    return symbol.upper()


def source_row(candidate, desk_dir):
    """Reuse the intake reader's unique-ref/schema/path proof, binding the selected row."""
    from sniper.services import memecoin_desk_intake_proof as P
    claim = candidate['intake']
    path, ref, _ = P._source_path(Path(desk_dir)/'intake', claim)
    row = P._read(path, ref)
    if row is None:
        raise FileNotFoundError
    if (row['mint'] != candidate['mint'] or row['network_id'] != candidate['network_id']
            or row['poll_status'] != 'ok' or row['poll_error'] is not None
            or row['poll_feed'] != row['feed'] or row['poll_filter'] != row['filter']
            or row['poll_received_at'] != row['host_received_at']
            or any(row[k] != claim[k] for k in ('feed','filter','provider_rank','rank_kind',
                                               'observed_at','host_received_at','clock_offset_s'))):
        raise ValueError('source_mismatch')
    return row


def _claims(raw, feed):
    fields = ('vol_24h_usd','change_24h_pct','holders','holders_total','mcap_claim_usd')
    values, missing = {}, {}
    if feed == 'fomo':
        cells = raw.get('item', {}).get('cells', [])
        def cell(i):
            return cells[i] if isinstance(cells, list) and len(cells) > i else None
        holders = _lines(cell(4))
        vals = [cell(3), ''.join(_lines(cell(2))), holders[0] if holders else None,
                re.sub(r'\s+(?:en\s+)?total\s*$', '', holders[1], flags=re.I) if len(holders)>1 else None, cell(1)]
        present = [True]*5
    else:
        aliases = [('vol_24h_usd','volume_24h'),('change_24h_pct','price_change_24h'),('holders',),
                   ('holders_total',),('market_cap_usd','usd_market_cap')]
        vals, present = [], []
        for names in aliases:
            found = [raw[k] for k in names if k in raw]
            present.append(bool(found))
            vals.append(found[0] if found and all(v == found[0] for v in found) else None)
    for field, value, exists in zip(fields, vals, present):
        try:
            if not exists:
                raise KeyError
            values[field] = numeric(value, percent=field=='change_24h_pct', count=field in ('holders','holders_total'))
        except (ValueError, KeyError):
            values[field] = None
            missing[field] = 'parse_cell' if exists else 'not_reported'
    return values, missing


def list_claims_for(candidate, *, desk_dir, as_of):
    """Exact selected intake row, read-only. Source failures never become empty claims."""
    started = time.monotonic()
    try:
        if not _number(as_of):
            return _unknown('clock_unknown', None)
        if not isinstance(candidate, dict) or not isinstance(candidate.get('intake'), dict):
            return _unknown('no_source', as_of)
        feed = candidate['intake'].get('feed')
        if feed not in ('fomo', 'pumpfun'):
            return _unknown('unsupported_source', as_of)
        row = source_row(candidate, desk_dir)
        host, offset = row['host_received_at'], row['clock_offset_s']
        if not _number(host) or not _number(offset):
            return _unknown('clock_unknown', as_of)
        captured = host-offset
        if not _number(row['observed_at']) or not math.isclose(captured, row['observed_at'], abs_tol=1e-6, rel_tol=0):
            return _unknown('source_mismatch', as_of)
        age = as_of-captured
        if age < 0:
            return _unknown('future_source', as_of)
        if age > DEFINITION['freshness_s']:
            return _unknown('stale', as_of)
        raw = json.loads(row['raw_json'])
        if not isinstance(raw, dict) or not raw:
            return _unknown('parse_failed', as_of)
        if feed == 'fomo' and (not isinstance(raw.get('item'), dict) or not isinstance(raw['item'].get('cells'), list) or not raw['item']['cells']):
            return _unknown('parse_failed', as_of)
        values, missing = _claims(raw, feed)
        if all(v is None for v in values.values()):
            return _unknown('parse_failed', as_of)
        if time.monotonic()-started > DEFINITION['read_budget_s']:
            return _unknown('read_budget', as_of)
        return dict(state='known', reason=None, definition_sha256=definition()['definition_sha256'],
                    as_of=captured, source=feed, row_age_s=age, parse_ok='parse_cell' not in missing.values(),
                    label='provider_claim', field_unknown=missing, **values)
    except FileNotFoundError:
        return _unknown('no_source', as_of)
    except ValueError as exc:
        return _unknown('source_mismatch' if str(exc)=='source_mismatch' else 'source_unreadable', as_of)
    except Exception:
        return _unknown('source_unreadable', as_of)
