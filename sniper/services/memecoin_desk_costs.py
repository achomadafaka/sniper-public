"""Bounded off-path desk pricing. No custody key, signing, or funded callback imports."""
from __future__ import annotations

import hashlib
import json
import math
import socket
from http.client import HTTPException
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from sniper.persistence import db_memecoin as ledger


class CostDependencyError(ValueError):
    def __init__(self, reason, *, stop_batch=False):
        super().__init__(reason)
        self.stop_batch = stop_batch


def _time(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError
        return stamp
    except (TypeError, ValueError, AttributeError, OverflowError):
        raise ValueError('aware_utc_required') from None


def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _http_json(url, *, payload=None, timeout_s=3.0):
    if urlsplit(url).scheme != 'https' or not urlsplit(url).netloc:
        raise CostDependencyError('provider_url_invalid')
    data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
    request = Request(url, data=data, headers={'Accept': 'application/json',
        'Content-Type': 'application/json', 'User-Agent': 'Sniper-desk-costs/1'})
    try:
        with urlopen(request, timeout=timeout_s) as response:
            body = response.read(2_097_153)
        if len(body) > 2_097_152:
            raise CostDependencyError('provider_response_too_large')
        return json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except HTTPError as exc:
        if exc.code == 429:
            raise CostDependencyError('provider_rate_limited', stop_batch=True) from None
        if exc.code in (401, 403):
            raise CostDependencyError('provider_rejected', stop_batch=True) from None
        raise CostDependencyError('provider_http_failure') from None
    except (socket.timeout, TimeoutError):
        raise CostDependencyError('provider_timeout') from None
    except HTTPException:
        raise CostDependencyError('provider_partial_response') from None
    except URLError as exc:
        reason = 'provider_timeout' if isinstance(exc.reason, (socket.timeout, TimeoutError)) else 'provider_transport'
        raise CostDependencyError(reason) from None
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, CostDependencyError):
            raise
        raise CostDependencyError('provider_malformed_json') from None


def read_solana_receipt(signature, *, rpc_url, http_json=_http_json):
    reply = http_json(rpc_url, payload={'jsonrpc': '2.0', 'id': 1,
        'method': 'getTransaction', 'params': [signature, {'encoding': 'jsonParsed',
        'commitment': 'confirmed', 'maxSupportedTransactionVersion': 1}]})
    if not isinstance(reply, dict) or reply.get('jsonrpc') != '2.0' or type(reply.get('id')) is not int or reply['id'] != 1:
        raise CostDependencyError('rpc_response_identity')
    if 'error' in reply:
        code = reply['error'].get('code') if isinstance(reply['error'], dict) else None
        raise CostDependencyError('rpc_rate_limited' if code in (-32429, 429) else 'rpc_rejected',
                                  stop_batch=True)
    if not isinstance(reply.get('result'), dict):
        raise CostDependencyError('receipt_unavailable')
    return reply['result']


def read_sol_usd_quote(block_time, *, http_json=_http_json, now_iso=None, now_iso_fn=None):
    if type(block_time) is not int or block_time < 60:
        raise ValueError('fx_time_invalid')
    close_at = block_time // 60 * 60
    start_at = close_at - 60
    url = 'https://api.exchange.coinbase.com/products/SOL-USD/candles?' + urlencode({
        'granularity': '60', 'start': _iso(start_at), 'end': _iso(close_at)})
    rows = http_json(url)
    now_iso = now_iso_fn() if now_iso_fn is not None else now_iso
    if _time(now_iso).timestamp() < block_time:
        raise ValueError('fx_time_invalid')
    if not isinstance(rows, list) or len(rows) > 300:
        raise ValueError('fx_response_invalid')
    matches = [row for row in rows if isinstance(row, list) and row and type(row[0]) is int and row[0] == start_at]
    if len(matches) != 1:
        raise ValueError('fx_candle_missing_or_ambiguous')
    candle = matches[0]
    if len(candle) != 6 or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in candle[1:]):
        raise ValueError('fx_candle_invalid')
    _start, low, high, opening, closing, _volume = candle
    if not low <= min(opening, closing) <= max(opening, closing) <= high:
        raise ValueError('fx_candle_invalid')
    return {'price': float(closing), 'observed_at': _iso(close_at), 'known_at': now_iso,
            'source': 'coinbase:SOL-USD:60s'}


def price_execution_batch(reader, writer, *, owner, receipt_fn, usd_quote_fn, now_iso_fn,
                          after=None, limit=10):
    """Materialize one page before I/O; each resolver rechecks ownership under its own writer."""
    from sniper.services.memecoin_desk_costs_solana import price_solana_expense

    if reader.in_transaction or writer.in_transaction:
        raise ValueError('cost_worker_open_transaction')
    requests = ledger.list_unpriced_desk_execution_costs(reader, after=after, limit=limit)
    results, cursor = [], after
    for request in requests:
        cursor = (request['cohort_id'], request['event_id'])
        row = {'cohort_id': request['cohort_id'], 'event_id': request['event_id'], 'state': 'deferred'}
        try:
            if request.get('ownership_error'):
                raise ValueError(request['ownership_error'])
            if request['network_id'] != 'solana':
                raise ValueError('unsupported_cost_chain')
            receipt = receipt_fn(request['tx_sig'])
            if receipt is None:
                raise CostDependencyError('receipt_unavailable')
            try:
                priced = price_solana_expense(request, receipt, owner=owner,
                    usd_quote=None, known_at=now_iso_fn())
            except ValueError as exc:
                if str(exc) != 'usd_quote_missing':
                    raise
                quote = usd_quote_fn(receipt['blockTime'])
                priced = price_solana_expense(request, receipt, owner=owner,
                    usd_quote=quote, known_at=now_iso_fn())
            evidence = priced['evidence']
            digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(',', ':'),
                                               allow_nan=False).encode()).hexdigest()
            ledger.resolve_desk_execution_cost(writer, request=request, evidence=evidence,
                execution_usd=priced['execution_usd'], refund_usd=priced['refund_usd'],
                known_at=evidence['known_at'], provenance='desk-cost-v1:' + digest)
            row.update(state='priced', net_usd=priced['execution_usd'] - priced['refund_usd'])
        except CostDependencyError as exc:
            row['reason'] = str(exc)
            results.append(row)
            if exc.stop_batch:
                break
            continue
        except (ValueError, TimeoutError, OSError) as exc:
            # Provider URLs/credentials and arbitrary response text never enter diagnostics.
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            row['reason'] = reason if len(reason) <= 160 and all(c.isalnum() or c in '_:- ' for c in reason) else 'cost_evidence_invalid'
        results.append(row)
    return {'results': results, 'next_cursor': cursor, 'drained': len(requests) < limit and len(results) == len(requests)}
