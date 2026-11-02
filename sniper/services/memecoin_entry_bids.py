"""Own the bounded background bid reads; custody/entry threads only read snapshots."""
import json
import os
from types import SimpleNamespace

from sniper.execution.fast_http import FastHttpError, PooledHttpClient
from sniper.execution.solana_entry_bids import EntryBidCache
from sniper.execution.submission_backends import register_client

READ_TIMEOUT_S = 0.8
TIP_FLOOR_URL = "https://api.nozomi.temporal.xyz/tip_floor"


class EntryBidReadError(RuntimeError):
    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.reason = reason
        self.response = SimpleNamespace(status_code=status)


def validate_priority_response(payload):
    if not isinstance(payload, list) or len(payload) != 2:
        raise EntryBidReadError('malformed')
    by_id = {}
    for row in payload:
        if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] in by_id:
            raise EntryBidReadError('malformed')
        if 'error' in row:
            code = row['error'].get('code') if isinstance(row['error'], dict) else None
            raise EntryBidReadError('rate_limit' if code in (-32429, 429) else 'rejected')
        by_id[row['id']] = row.get('result')
    fees, head = by_id.get(1), by_id.get(2)
    if (set(by_id) != {1, 2} or not isinstance(fees, list) or not fees
            or type(head) is not int or not 0 <= head < 2**64):
        raise EntryBidReadError('malformed')
    slots = [row.get('slot') for row in fees if isinstance(row, dict)]
    if len(slots) != len(fees) or any(type(x) is not int or not 0 <= x < 2**64 for x in slots):
        raise EntryBidReadError('malformed')
    if max(slots) > head + 1:
        raise EntryBidReadError('future_source')
    if head - max(slots) > 8:
        raise EntryBidReadError('stale_source')
    return fees


def start_entry_bids(read_rpc_url, *, env=None, http=None):
    values = os.environ if env is None else env
    if str(values.get("MEMECOIN_SOLANA_ENTRY_BIDS_ENABLED", "")).strip().lower() not in (
            "1", "true", "yes", "on"):
        return None
    if http is None:
        client = register_client(PooledHttpClient(
            max_connections=2, max_keepalive=2, connect_timeout_s=READ_TIMEOUT_S,
            read_timeout_s=READ_TIMEOUT_S, write_timeout_s=READ_TIMEOUT_S,
            pool_timeout_s=READ_TIMEOUT_S))
        http = client.request

    def response(method, url, **kwargs):
        try:
            status, payload, _ = http(method, url, timeout=READ_TIMEOUT_S, **kwargs)
        except Exception as exc:
            # FastHttpError contains only method + exception type, already redacted at its source.
            timed_out = (isinstance(exc, TimeoutError) or 'timeout' in type(exc).__name__.lower()
                         or isinstance(exc, FastHttpError) and 'timeout' in str(exc).lower())
            reason = 'timeout' if timed_out else 'transport'
            raise EntryBidReadError(reason) from None
        if status != 200:
            raise EntryBidReadError('rate_limit' if status == 429 else 'rejected', status)
        return payload

    def priority_fees(pool):
        payload = response('POST', read_rpc_url, body=json.dumps([
            {'jsonrpc': '2.0', 'id': 1, 'method': 'getRecentPrioritizationFees', 'params': [[pool]]},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'getSlot', 'params': [{'commitment': 'processed'}]}]))
        return validate_priority_response(payload)

    def tip_floor():
        payload = response('GET', TIP_FLOOR_URL)
        if not isinstance(payload, list):
            raise EntryBidReadError('tip_response_malformed')
        return payload

    # Twelve hot pools fit a 30s freshness window even with 800ms reads and tip refreshes.
    return register_client(EntryBidCache(priority_fees, tip_floor, max_pools=12)).start()
