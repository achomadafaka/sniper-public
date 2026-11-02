"""Off-path Relay origin verification. Token delivery alone never grants copy authority."""
from __future__ import annotations

import copy
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import quote, quote_plus, unquote, unquote_plus, urlencode, urlsplit

import base58
from eth_account._utils.encode_typed_data.encoding_and_hashing import hash_struct

from sniper.services.relay_order_types import ORDER_TYPES

PROOF_VERSION = 'relay-origin-paid-v1'
# GET /requests/v3 carries no order preimage (no orderData, salt or deadline; recorded pair
# 2026-10-07 in tests/fixtures/memecoin/relay_requests_2dcf925a), so the v1 commitment check cannot
# run. B binds the origin deposit and the destination fill to the SAME orderId on chain instead.
BOUND_PROOF_VERSION = 'relay-origin-bound-v1'
_HEX32 = re.compile(r'0x[0-9a-f]{64}\Z')
SOLANA_CHAIN = 792703809
# Independently checked against Relay /chains, not values supplied by an order response.
SOLANA_DEPOSITORY = '99vQwtBwYtrqqD9YSXbdum3KBdxPAVxYTaQ3cfnJSrN2'
SOLANA_VAULT = '7uTT8Xi5RWXzy7h9XL244GRgEycDYDhLjr3ZyNdXi8pZ'
SOLANA_TOKEN_DEPOSIT = bytes.fromhex('0b9c60da27a3b413')
LOG = logging.getLogger(__name__)


class ProofError(ValueError):
    pass


CAPTURE_MAX_BYTES = 256 * 1024          # a captured Relay answer over this keeps only its size


def _malformed(exc):
    """malformed_origin_evidence naming what failed (dfeaef22 (b), 2026-10-07): the exception class,
    its key or message, and the verifier line that raised it. The reason string is unchanged, so the
    retry rules and every reader of it are too."""
    err = ProofError('malformed_origin_evidence')
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    where = ('%s:%d' % (os.path.basename(tb.tb_frame.f_code.co_filename), tb.tb_lineno)
             if tb is not None else 'unknown line')
    err.detail = '%s(%s) at %s' % (type(exc).__name__, str(exc)[:80], where)
    err.from_exception = True               # a field the proof read was absent or unreadable
    return err


# The Relay labels the proof reads, in the order it reads them (dfeaef22 follow-up, 2026-10-07).
# A fixed list, so the key set on a malformed attempt is bounded whatever Relay sends.
_RELAY_FIELDS = (
    ('rcpt', ('recipient',)), ('user', ('user',)),
    ('out.status', ('status',), 'out'), ('out.block', ('block',), 'out'), ('out.ts', ('timestamp',), 'out'),
    ('out.data', ('data', 'data'), 'out'), ('out.to', ('data', 'to'), 'out'),
    ('fills', ('protocol', 'settlement', 'destination', 'fills')),
    ('oid', ('protocol', 'orderId')), ('solver', ('protocol', 'solver', 'address')),
    ('route.mint', ('data', 'route', 'quoted', 'destination', 'outputCurrency', 'currency', 'address')),
    ('route.min', ('data', 'route', 'quoted', 'destination', 'outputCurrency', 'minimumAmount')),
    ('dep.from', ('protocol', 'deposit', 'origin', 'depositor')),
    ('dep.tx', ('protocol', 'deposit', 'origin', 'transactionId')),
    ('dep.amt', ('protocol', 'deposit', 'origin', 'amount')),
    ('dep.cur', ('protocol', 'deposit', 'origin', 'currency')),
    ('dep.depo', ('protocol', 'deposit', 'origin', 'depository')),
    ('in.tx', ('txHash',), 'in'), ('in.block', ('block',), 'in'), ('in.ts', ('timestamp',), 'in'),
)
_ABSENT = object()


def _relay_missing_keys(payload, proof, v3):
    """`@miss=<name>:<name>...` for the Relay fields absent (`name`) or null (`name=null`) in the one
    request of `payload`; 'out' is our destination tx's outTx, 'in' the single inTx. '' when there
    is no single request to read. A diagnostic from the answer already fetched: never raises."""
    try:
        requests = payload.get('requests') if isinstance(payload, dict) else None
        if not (isinstance(requests, list) and len(requests) == 1 and isinstance(requests[0], dict)):
            return ''
        request, tx_key = requests[0], 'txHash' if v3 else 'hash'
        data = request.get('data') if isinstance(request.get('data'), dict) else {}
        outs = [tx for tx in (data.get('outTxs') or []) if isinstance(tx, dict)
                and tx.get(tx_key) == proof.get('tx_hash')]
        ins = [tx for tx in (data.get('inTxs') or []) if isinstance(tx, dict)]
        roots = {None: request, 'out': outs[0] if len(outs) == 1 else None,
                 'in': ins[0] if len(ins) == 1 else None}
        if v3:
            fields = [f for f in _RELAY_FIELDS if f[0] != 'in.tx'] + [('in.tx', ('txHash',), 'in')]
        else:
            fields = [f for f in _RELAY_FIELDS if not f[0].startswith('route.') and f[0] != 'in.tx'] + [
                ('in.tx', ('hash',), 'in')]
        missing = []
        for side in ('out', 'in'):
            if roots[side] is None:
                missing.append(side)
        for name, path, *side in fields:
            node = roots[side[0] if side else None]
            if node is None:
                continue
            for key in path:
                node = node.get(key, _ABSENT) if isinstance(node, dict) else _ABSENT
                if node is _ABSENT or node is None:
                    break
            if node is _ABSENT:
                missing.append(name)
            elif node is None:
                missing.append(name + '=null')
        return '@miss=' + (':'.join(missing) or 'none')
    except Exception:
        return ''


def require(ok, reason):
    if not ok:
        raise ProofError(reason)


def order_id(order, chains):
    """Same hashStruct(Order) as @relay-protocol/settlement-sdk 0.0.144."""
    vm = {c['protocol']['v2']['chainId']: c['vmType'] for c in chains
          if c.get('protocol', {}).get('v2', {}).get('chainId')}
    def address(value, chain):
        if vm.get(chain) == 'svm':
            raw = base58.b58decode(value)
            require(len(raw) == 32, 'invalid_origin_address')
        else:
            require(vm.get(chain) == 'evm', 'unsupported_order_vm')
            raw = bytes.fromhex(value.removeprefix('0x'))
            require(len(raw) == 20, 'invalid_origin_address')
        return raw
    normalized = copy.deepcopy(order)
    require(order['version'] == 'v1', 'unsupported_order_version')
    for item in normalized['inputs']:
        payment = item['payment']
        payment['currency'] = address(payment['currency'], payment['chainId'])
        for refund in item['refunds']:
            for field in ('recipient', 'currency'):
                refund[field] = address(refund[field], refund['chainId'])
    for payment in normalized['output']['payments']:
        for field in ('recipient', 'currency'):
            payment[field] = address(payment[field], normalized['output']['chainId'])
    for fee in normalized['fees']:
        fee['recipient'] = address(fee['recipient'], fee['recipientChainId'])
        fee['currency'] = address(fee['currency'], fee['currencyChainId'])
    return '0x' + hash_struct('Order', ORDER_TYPES, normalized).hex()


def verify_origin(request, origin, proof, destination, *, identities, chains, block_absent_ok=False,
                  destination_time=None):
    """Pure verifier of RPC facts, prior identity and the committed order, not API labels."""
    return _verify_relay(request, origin, proof, destination, identities=identities, chains=chains,
                         bound=False, block_absent_ok=block_absent_ok, destination_time=destination_time)


def verify_bound_origin(request, origin, proof, destination, *, identities, chains, block_absent_ok=False,
                        destination_time=None):
    """B (v3, 2026-10-07): the same RPC facts and prior identity, with the orderId bound on chain
    -- the origin deposit commits it and the destination calldata ends with it -- in place of the
    lost order preimage. LOST against v1, and resting on Relay's labels or on nothing: the
    committed output (mint, recipient, minimum, calls, chain), the deadline, the solver and the
    order's input payment. The fill's mint and beneficiary are still the receipt decode's."""
    return _verify_relay(request, origin, proof, destination, identities=identities, chains=chains,
                         bound=True, block_absent_ok=block_absent_ok, destination_time=destination_time)


def _bound_output(request):
    """(mint, minimumAmount) Relay reports for the destination leg (labels, not a commitment)."""
    route = request['data']['route']
    out = (route.get('quoted') or {})['destination']['outputCurrency']
    actual = (route.get('actual') or {}).get('destination', {}).get('outputCurrency', {})
    mint = str(out['currency']['address']).lower()
    require(not actual or str(actual['currency']['address']).lower() == mint, 'output_identity_mismatch')
    return mint, int(out['minimumAmount'])


def _verify_relay(request, origin, proof, destination, *, identities, chains, bound, block_absent_ok=False,
                  destination_time=None):
    tx_key = 'txHash' if bound else 'hash'

    # RELAY'S BLOCK LABEL MAY LAG (operator YES via Alpha, 2026-10-07): every 4663 malformed attempt
    # was KeyError('block') on this match, Relay listing our tx as a success before writing its
    # block. With block_absent_ok an ABSENT label matches: the block number and hash are our
    # receipt's, required equal to our own read of the destination tx just above. A present label
    # (null and 0 included) must still equal it.
    def same_block(tx):
        return (block_absent_ok and 'block' not in tx) or tx['block'] == proof['block_number']

    def destination_time_of(tx):
        # An absent outTx timestamp is our own header's time for the receipt's block, else the
        # KeyError keeps today's malformed poll: never a guessed or arrival time.
        if block_absent_ok and 'timestamp' not in tx and destination_time is not None:
            return destination_time
        return tx['timestamp']
    try:
        require(request['status'] == 'success', 'relay_not_settled')
        require(isinstance(proof['network_id'], str) and proof['network_id'].isdigit()
                and int(proof['network_id']) > 0, 'destination_mismatch')
        destination_chain = int(proof['network_id'])
        require(proof['tx_hash'] == destination['hash']
                and proof['block_hash'] == destination['blockHash']
                and proof['block_number'] == int(destination['blockNumber'], 16), 'destination_mismatch')
        require(request['recipient'].lower() == proof['beneficiary'], 'recipient_mismatch')
        outputs = request['data']['outTxs']
        matches = [tx for tx in outputs if tx[tx_key] == proof['tx_hash'] and tx['chainId'] == destination_chain
                   and tx['status'] == 'success' and same_block(tx)]
        require(len(matches) == 1 and matches[0]['data']['data'].lower() == destination['input'].lower()
                and matches[0]['data']['to'].lower() == destination['to'].lower(), 'destination_mismatch')
        fills = request['protocol']['settlement']['destination']['fills']
        require(any(tx['chainId'] == destination_chain and tx['transactionId'] == proof['tx_hash'] for tx in fills),
                'settlement_mismatch')
        if bound:
            order = None
            oid = str(request['protocol']['orderId']).lower()
            require(_HEX32.fullmatch(oid) is not None, 'malformed_origin_evidence')
            require(destination['input'][-64:].lower() == oid[2:], 'destination_order_mismatch')
            require(proof['payer'] == str(request['protocol']['solver']['address']).lower(), 'solver_mismatch')
            mint, minimum = _bound_output(request)
            require(mint == proof['mint'] and minimum > 0, 'output_identity_mismatch')
            require(int(proof['acquired_atomic']) >= minimum, 'destination_underfilled')
            destination_ts = destination_time_of(matches[0])
        else:
            order = request['orderData']
            oid = order_id(order, chains)
            require(oid == request['protocol']['orderId'], 'order_commitment_mismatch')
            require(destination['input'][-64:].lower() == oid[2:], 'destination_order_mismatch')
            require(proof['payer'] == order['solver'].lower(), 'solver_mismatch')
            protocol_chain = next(c for c in chains if c['id'] == destination_chain)['protocol']['v2']['chainId']
            require(order['output']['chainId'] == protocol_chain and not order['output']['calls'], 'output_chain_mismatch')
            payments = order['output']['payments']
            require(len(payments) == 1 and payments[0]['currency'].lower() == proof['mint']
                    and payments[0]['recipient'].lower() == proof['beneficiary']
                    and int(payments[0]['minimumAmount']) > 0, 'output_identity_mismatch')
            require(int(proof['acquired_atomic']) >= int(payments[0]['minimumAmount']), 'destination_underfilled')
            destination_ts = destination_time_of(matches[0])
            require(order['output']['deadline'] >= destination_ts, 'origin_time_mismatch')
        deposit = request['protocol']['deposit']['origin']
        origin_chain = deposit['chainId']
        payer = deposit['depositor']
        require(request['user'] == payer, 'payer_mismatch')
        if origin_chain != SOLANA_CHAIN:
            return _verify_evm_origin(request, origin, proof, oid, chains, destination_ts, bound=bound)
        require(any(row['source_user_id'] == proof['source_user_id'] and row['network_id'] == 'solana'
                    and row['address'] == payer and row['status'] == 'verified'
                    and row.get('conflict_at') is None and row.get('status_at')
                    and datetime.fromisoformat(row['status_at']).timestamp() <= origin['blockTime']
                    for row in identities), 'origin_wallet_unverified')
        require(origin['meta']['err'] is None and origin['slot'] > 0, 'origin_transaction_failed')
        inputs = request['data']['inTxs']
        require(len(inputs) == 1 and inputs[0][tx_key] == deposit['transactionId']
                and inputs[0]['chainId'] == SOLANA_CHAIN and inputs[0]['status'] == 'success'
                and inputs[0][tx_key] in origin['transaction']['signatures']
                and inputs[0]['block'] == origin['slot'], 'origin_transaction_mismatch')
        require(any(key['pubkey'] == payer and key['signer']
                    for key in origin['transaction']['message']['accountKeys']), 'origin_did_not_sign')
        require(deposit['depository'] == SOLANA_DEPOSITORY, 'depository_mismatch')
        if not bound:
            require(len(order['inputs']) == 1, 'ambiguous_input')
            input_payment = order['inputs'][0]['payment']
            require(input_payment['chainId'] == 'solana' and input_payment['currency'] == deposit['currency']
                    and int(input_payment['amount']) == int(deposit['amount']) > 0, 'payment_mismatch')
        amount = int(deposit['amount'])
        require(amount > 0, 'payment_mismatch')
        instructions = origin['transaction']['message']['instructions']
        deposits = [ins for ins in instructions if ins.get('programId') == SOLANA_DEPOSITORY]
        require(len(deposits) == 1, 'depository_instruction_missing')
        ins = deposits[0]
        raw = base58.b58decode(ins['data'])
        # Anchor depositErc20: discriminator, amount u64 LE, committed order bytes32.
        require(len(raw) == 48 and raw[:8] == SOLANA_TOKEN_DEPOSIT
                and int.from_bytes(raw[8:16], 'little') == amount
                and '0x' + raw[16:].hex() == oid, 'signed_deposit_mismatch')
        accounts = ins['accounts']
        require(accounts[1:3] == [payer, payer] and accounts[3] == SOLANA_VAULT
                and accounts[4] == deposit['currency'], 'deposit_accounts_mismatch')
        def balance(rows, owner):
            return sum(int(row['uiTokenAmount']['amount']) for row in rows
                       if row['mint'] == deposit['currency'] and row.get('owner') == owner)
        pre, post = origin['meta']['preTokenBalances'], origin['meta']['postTokenBalances']
        require(balance(pre, payer) - balance(post, payer) == amount
                and balance(post, SOLANA_VAULT) - balance(pre, SOLANA_VAULT) == amount, 'origin_payment_unproved')
        require(0 <= destination_ts - origin['blockTime'] <= 120, 'origin_time_mismatch')
        return dict(proof, proof_version=BOUND_PROOF_VERSION if bound else PROOF_VERSION, origin_wallet=payer,
                    origin_network_id='solana', origin_tx=deposit['transactionId'],
                    origin_slot=origin['slot'], origin_amount_atomic=str(amount), order_id=oid,
                    origin_identity_user_id=proof['source_user_id'])
    except ProofError:
        raise
    except (KeyError, TypeError, ValueError, StopIteration, IndexError, OverflowError) as exc:
        raise _malformed(exc) from exc


def _verify_evm_origin(request, origin, proof, oid, chains, destination_ts, *, bound=False):
    """An EVM deposit must be signed by the already-verified destination wallet itself."""
    from eth_abi import decode, encode
    from eth_utils import keccak
    deposit = request['protocol']['deposit']['origin']
    chain = next(c for c in chains if c['id'] == deposit['chainId'])
    require(chain['vmType'] == 'evm', 'unsupported_origin_chain')
    protocol = chain['protocol']['v2']
    depository = protocol['depository'].lower()
    payer = deposit['depositor'].lower()
    require(payer == proof['beneficiary'], 'origin_wallet_unverified')
    tx, receipt = origin['transaction'], origin['receipt']
    sig = deposit['transactionId']
    require(int(origin['chain_id'], 16) == deposit['chainId']
            and int(tx['chainId'], 16) == deposit['chainId'], 'origin_chain_mismatch')
    require(tx['hash'] == receipt['transactionHash'] == sig
            and tx['blockHash'] == receipt['blockHash']
            and tx['blockNumber'] == receipt['blockNumber']
            and int(receipt['status'], 16) == 1, 'origin_transaction_failed')
    require(tx['from'].lower() == receipt['from'].lower() == payer
            and tx['to'].lower() == receipt['to'].lower() == depository
            and deposit['depository'].lower() == depository, 'origin_did_not_sign')
    inputs = request['data']['inTxs']
    tx_key = 'txHash' if bound else 'hash'
    require(len(inputs) == 1 and inputs[0][tx_key] == sig and inputs[0]['status'] == 'success'
            and inputs[0]['chainId'] == deposit['chainId']
            and inputs[0]['block'] == int(tx['blockNumber'], 16), 'origin_transaction_mismatch')
    amount = int(deposit['amount'])
    currency = deposit['currency'].lower()
    require(amount > 0, 'payment_mismatch')
    if not bound:
        payment = request['orderData']['inputs']
        require(len(payment) == 1, 'ambiguous_input')
        payment = payment[0]['payment']
        require(payment['chainId'] == protocol['chainId'] and payment['currency'].lower() == currency
                and int(payment['amount']) == amount > 0, 'payment_mismatch')
    raw = bytes.fromhex(tx['input'][2:])
    native = currency == '0x' + '00' * 20
    types = ['address', 'bytes32'] if native else ['address', 'address', 'uint256', 'bytes32']
    selector = keccak(text='depositNative(address,bytes32)' if native else
                      'depositErc20(address,address,uint256,bytes32)')[:4]
    args = decode(types, raw[4:])
    require(raw[:4] == selector and encode(types, args) == raw[4:]
            and args[0] == payer and '0x' + args[-1].hex() == oid, 'signed_deposit_mismatch')
    if native:
        require(int(tx['value'], 16) == amount, 'origin_payment_unproved')
    else:
        require(args[1] == currency and args[2] == amount and int(tx['value'], 16) == 0,
                'origin_payment_unproved')
        topic = '0x' + keccak(text='Transfer(address,address,uint256)').hex()
        paid = sum(int(log['data'], 16) for log in receipt['logs']
                   if log['address'].lower() == currency and len(log.get('topics', [])) == 3
                   and log['topics'][0] == topic and '0x' + log['topics'][1][-40:] == payer
                   and '0x' + log['topics'][2][-40:] == depository)
        require(paid == amount, 'origin_payment_unproved')
    require(0 <= destination_ts - inputs[0]['timestamp'] <= 120, 'origin_time_mismatch')
    return dict(proof, proof_version=BOUND_PROOF_VERSION if bound else PROOF_VERSION, origin_wallet=payer,
                origin_network_id=str(deposit['chainId']), origin_tx=sig,
                origin_slot=int(tx['blockNumber'], 16), origin_amount_atomic=str(amount), order_id=oid,
                origin_identity_user_id=proof['source_user_id'])


_RELAY_ENDPOINTS = {'/requests/v3': 'requests/v3', '/requests/v2': 'requests/v2', '/requests': 'requests',
                    '/chains': 'chains'}
_LONG_TOKEN = re.compile(r'[A-Za-z0-9_\-]{24,}')


def _diagnostic_parts(url, body_text, secrets=()):
    """(host, endpoint label, body) for a log line, never a credential (Codex N6, 2026-10-07): the
    helper also reads source-chain RPC, whose keys ride in the path (/v2/<key>), the query or the
    userinfo. Only the hostname and a fixed Relay label survive; every URL part and every header
    value (`secrets`, e.g. x-api-key) of 6+ characters, each also in its percent-decoded and
    percent-encoded forms (N6 residual: a body can echo a key decoded), and any 24+ character
    token are redacted from the body, which is then bounded."""
    try:
        parts = urlsplit(url)
        host, path = parts.hostname or 'unknown', parts.path.rstrip('/')
        pieces = re.split(r'[/?&=:@]', '%s&%s' % (parts.path, parts.query))
        pieces += [parts.username or '', parts.password or '']
    except ValueError:
        host, path, pieces = 'unknown', '', re.split(r'[/?&=:@\[\]]', str(url))
    label = _RELAY_ENDPOINTS.get(path, 'path_redacted') if host == 'api.relay.link' else 'path_redacted'
    forms = {form for piece in list(pieces) + [str(s) for s in secrets if s]
             for form in (piece, unquote(piece), unquote_plus(piece), quote(piece, safe=''),
                          quote_plus(piece, safe=''))}
    for piece in sorted({p for p in forms if len(p) >= 6}, key=len, reverse=True):
        body_text = body_text.replace(piece, '[REDACTED]')
    return host, label, _LONG_TOKEN.sub('[REDACTED]', body_text)[:120]


def read_json(url, *, body=None, headers=None, timeout=1.0):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 headers=headers or {'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            # WHICH LIMIT FIRED (Alpha/Gamma 2026-10-06): Relay documents 10 rps per key on
            # /requests, far above our rate, so the answer's own Retry-After and body are what can
            # tell a key limit from an IP or edge limit. WARNING: the watchers configure no logging.
            try:
                detail = exc.read()[:400].decode('utf-8', 'replace')
            except Exception:
                detail = ''
            host, endpoint, detail = _diagnostic_parts(url, detail, secrets=(headers or {}).values())
            answer_headers = exc.headers or {}
            LOG.warning('relay 429 host=%s endpoint=%s key_sent=%d retry_after=%.32s deprecation=%.32s '
                        'sunset=%.40s body=%r', host, endpoint, int(bool((headers or {}).get('x-api-key'))),
                        str(answer_headers.get('Retry-After')), str(answer_headers.get('Deprecation')),
                        str(answer_headers.get('Sunset')), detail)
        raise ProofError('origin_rate_limit' if exc.code == 429 else 'origin_http_%s' % exc.code) from exc
    except (OSError, ValueError) as exc:
        raise ProofError('origin_provider_unavailable') from exc


ORIGIN_VERIFY_BUDGET_S = 8.0     # today's cap: 7 attempts with 0.1 x 2^n sleeps came to ~8 s
_RETRY_INDEXING = frozenset({'relay_request_not_indexed', 'relay_not_settled', 'malformed_origin_evidence'})


class RelayRequestPacer:
    """At most `per_s` Relay /requests calls a second across the threads that share it (one per
    watcher process: 4 watchers x 2/s stay under Relay's 10/s per key). The lock only reserves the
    next slot; the wait happens outside it. 0 = no pacing."""

    def __init__(self, per_s, *, clock=time.monotonic, sleep=time.sleep):
        self.gap = 1.0 / float(per_s) if per_s and float(per_s) > 0 else 0.0
        self.clock, self.sleep = clock, sleep
        self._lock, self._next = threading.Lock(), 0.0

    def wait(self):
        if not self.gap:
            return
        with self._lock:
            now = self.clock()
            at = max(now, self._next)
            self._next = at + self.gap
        if at > now:
            self.sleep(at - now)


def _header_time(row, proof):
    """Our own node's time for the receipt's block: the watcher's log for this buy, only when that
    log names the same block number and hash as the proof. None otherwise; never raises."""
    def quantity(value):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.startswith('0x'):
            return int(value, 16)
        return None
    try:
        log = row.get('log') or {}
        if (quantity(log.get('blockNumber')) == proof['block_number']
                and str(log.get('blockHash') or '').lower() == str(proof['block_hash']).lower()):
            return quantity(log.get('blockTimestamp'))
    except Exception:
        pass
    return None


_DEPOSIT_VERSION = 'relay-deposit-processed-v1'     # memecoin_source_initiation.RELAY_DEPOSIT_PROOF_VERSION


def _settled_solana_depositor(exc):
    """The Solana depositor a refused verification read from Relay's settled answer, or None: the early
    give-up names it in its detail; a full check keeps the answer on the exception."""
    detail = str(getattr(exc, 'detail', None) or '')
    m = re.search(r'early:depositor=([1-9A-HJ-NP-Za-km-z]{32,44})', detail)
    if m:
        return m.group(1)
    try:
        requests = (getattr(exc, 'relay_payload', None) or {}).get('requests') or []
        origin = requests[0]['protocol']['deposit']['origin'] if len(requests) == 1 else {}
        if origin.get('chainId') == SOLANA_CHAIN and isinstance(origin.get('depositor'), str):
            return origin['depositor']
    except (KeyError, TypeError, IndexError, AttributeError):
        pass
    return None


class WatcherVerifier:
    """Worker-only provider reads, with each request bound to its original destination row."""
    def __init__(self, identities_fn, *, solana_rpc='https://api.mainnet-beta.solana.com',
                 evm_rpcs=None, api_key='', fetch=read_json, chains=None, requests_version='v2',
                 capture_fn=None, clock=None, sleep=None, poll_s=None, requests_per_s=None,
                 early_giveup=None, accept_block_absent=None, deposit_fn=None, depositor_fn=None):
        from sniper.settings import memecoin as cfg
        # Relay option A (spec 2026-10-09): `deposit_fn(item)` returns the item proved from a stored
        # signed deposit, or None; it is asked before every Relay call, so the first proof to land
        # wins. `depositor_fn` (the deposit store) records who funded whom and answers shared(): a
        # depositor funding SHARED_AT_UIDS source users is refused on this path too. None: today's.
        self.deposit_fn, self.depositor_fn = deposit_fn, depositor_fn
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep
        self.poll_s = float(cfg.MEMECOIN_RELAY_ORIGIN_POLL_S if poll_s is None else poll_s)
        self.early_giveup = bool(cfg.MEMECOIN_RELAY_ORIGIN_EARLY_GIVEUP if early_giveup is None else early_giveup)
        self.accept_block_absent = bool(cfg.MEMECOIN_RELAY_ACCEPT_OUTTX_BLOCK_ABSENT
                                        if accept_block_absent is None else accept_block_absent)
        # One verifier per watcher process, shared by its OriginWorker threads: a per-process pacer.
        self.pacer = RelayRequestPacer(cfg.MEMECOIN_RELAY_REQUESTS_MAX_PER_S if requests_per_s is None
                                       else requests_per_s, clock=self.clock, sleep=self.sleep)
        self.identities_fn, self.solana_rpc = identities_fn, solana_rpc
        self.capture_fn = capture_fn            # the final malformed answer, for a real recording
        self.api_key, self.fetch, self.chains = api_key, fetch, chains
        # 'v3' asks GET /requests/v3 and returns relay-origin-bound-v1 (B); anything else is today.
        self.v3 = str(requests_version).strip().lower() == 'v3'
        self.evm_rpcs = evm_rpcs or {}
        self.chains_lock = threading.Lock()

    def __call__(self, item):
        if self.depositor_fn is None:
            return self._call(item)
        try:
            result = self._call(item)
        except ProofError as exc:
            if str(exc) != 'origin_wallet_unverified':
                raise
            depositor = _settled_solana_depositor(exc)
            if depositor is None:
                raise
            uid = item['row']['source_user_id']
            shared = self._shared(depositor, uid)
            if shared is None:                   # the store failed: today's refusal, unrenamed
                raise
            err = ProofError('shared_depositor' if shared else 'foreign_depositor')
            err.detail = 'depositor=%s' % depositor
            err.origin_attempts = getattr(exc, 'origin_attempts', None)
            raise err from exc
        proof = result['row']['buy']['source_initiation']
        if proof.get('origin_network_id') == 'solana' and proof.get('proof_version') != _DEPOSIT_VERSION:
            depositor, uid = proof.get('origin_wallet'), item['row']['source_user_id']
            if self._shared(depositor, uid):     # None (the store failed) keeps today's verified buy
                err = ProofError('shared_depositor')
                err.detail = 'depositor=%s' % depositor
                err.origin_attempts = result.get('origin_attempts')
                raise err
        return result

    def _shared(self, depositor, uid):
        """Record the pair and answer shared(): True/False, or None when the store failed. The
        shared rule is new strictness, so a store error must fall back to today's outcome (Gamma M1:
        a locked store turned a Relay-verified buy into origin_verification_failed)."""
        try:
            self.depositor_fn.note_funder(depositor, uid)
            return bool(self.depositor_fn.shared(depositor, uid))
        except Exception:
            self.store_errors = getattr(self, 'store_errors', 0) + 1
            LOG.warning('relay origin depositor store failed (today\'s outcome kept) depositor=%s', depositor,
                        exc_info=True)
            return None

    def _deposit(self, item):
        try:
            return self.deposit_fn(item)
        except Exception:
            LOG.warning('relay origin deposit check failed', exc_info=True)
            return None

    def _call(self, item):
        # A fill can precede Relay's terminal index by seconds. Keep only indexing
        # unknowns pending longer, on these private workers; every proof check runs again.
        # (i) Each attempt's note (_attempt_note) rides the result or the raised error to the
        # watcher's relay origin line; it reads the answer already fetched, never a new call.
        # FLAT POLL (Part 1, operator YES 2026-10-07): an indexing miss is re-asked every poll_s
        # until ORIGIN_VERIFY_BUDGET_S, the same ~8 s wall the exponential schedule reached; the
        # backoff overshot Relay's readiness by half its last gap. poll_s 0 restores 0.1 x 2^n.
        trace, started_at = [], self.clock()
        attempt, candidate = 0, None             # the unregistered depositor of the previous poll
        while True:
            if self.deposit_fn is not None:
                proved = self._deposit(item)
                if proved is not None:
                    trace.append('deposit/0/-/-')
                    return dict(proved, origin_attempts=trace)
            seen, started = {}, time.perf_counter()
            try:
                result = self._verify(item, seen)
            except Exception as exc:
                trace.append(_attempt_note(exc, started, seen))
                exc.origin_attempts = trace
                if not isinstance(exc, ProofError):
                    raise
                reason = str(exc)
                if reason in _RETRY_INDEXING:
                    other = self._provably_other_depositor(item, seen) if self.early_giveup else None
                    # TWO CONSECUTIVE settled answers must name the same depositor (Gamma LOW on
                    # 544f1b639): one answer could carry a provisional depositor Relay corrects a
                    # poll later. One poll more (~0.3 s), still ~7 s ahead of the 8 s path.
                    confirmed, candidate = other is not None and other == candidate, other
                    if confirmed:
                        # EARLY GIVE-UP: Relay already answered, settled, with a depositor that can
                        # never pass; waiting only holds the worker (~7.8 s on 4663). Refuses what
                        # the 8 s path would refuse; the depositor rides the note (Beta).
                        err = ProofError('origin_wallet_unverified')
                        err.detail = 'early:depositor=%s' % other
                        trace.append(_attempt_note(err, time.perf_counter(), seen))
                        err.origin_attempts = trace
                        raise err from exc
                    if self.poll_s > 0:
                        delay = self.poll_s
                        retry = self.clock() - started_at + delay < ORIGIN_VERIFY_BUDGET_S
                    else:
                        delay, retry = .1 * 2 ** attempt, attempt < 6
                elif reason.split(':')[0] in {'origin_transaction_not_indexed', 'origin_rate_limit'}:
                    delay, retry = .1, attempt < 2
                else:
                    retry = False
                if not retry:
                    if reason == 'malformed_origin_evidence':
                        self._capture(item, exc)
                    raise
                self.sleep(delay)
                attempt += 1
            else:
                trace.append(_attempt_note(None, started, seen))
                return dict(result, origin_attempts=trace)

    def _provably_other_depositor(self, item, seen):
        """The depositor of Relay's SETTLED answer when it can never pass, else None. Settled
        (status success), one request, deposit.origin.depositor present and request.user equal to
        it; a Solana origin: no verified, unconflicted registry row of the source holds that
        address (a registered one with an unknown timing keeps polling); an EVM origin: not the
        beneficiary. Anything short of that keeps polling. Never raises."""
        try:
            answer = seen.get('payload')
            requests = answer.get('requests') if isinstance(answer, dict) else None
            if not (isinstance(requests, list) and len(requests) == 1 and isinstance(requests[0], dict)):
                return None
            request = requests[0]
            origin = ((request.get('protocol') or {}).get('deposit') or {}).get('origin') or {}
            depositor = origin.get('depositor')
            if (request.get('status') != 'success' or not isinstance(depositor, str) or not depositor
                    or request.get('user') != depositor):
                return None
            row = item['row']
            if origin.get('chainId') == SOLANA_CHAIN:
                rows = self.identities_fn(row['source_user_id']) or []
                registered = any(r.get('network_id') == 'solana' and r.get('address') == depositor
                                 and r.get('status') == 'verified' and r.get('conflict_at') is None
                                 for r in rows)
                return None if registered else depositor
            beneficiary = str(row['buy']['source_initiation'].get('beneficiary') or '').lower()
            return depositor if beneficiary and depositor.lower() != beneficiary else None
        except Exception:
            LOG.warning('relay origin early give-up check failed', exc_info=True)
            return None

    def _capture(self, item, exc):
        """Hand the final malformed Relay answer, key-stripped and bounded, to `capture_fn`. Never
        raises and never changes the verdict: the refusal stands whatever happens here."""
        if self.capture_fn is None:
            return
        try:
            text = json.dumps(getattr(exc, 'relay_payload', None), separators=(',', ':'), default=str)
            if self.api_key:
                text = text.replace(self.api_key, '<key>')
            self.capture_fn({
                'tx_hash': (item.get('row') or {}).get('tx_hash') or (item.get('transaction') or {}).get('hash'),
                'error': str(exc), 'detail': getattr(exc, 'detail', None),
                'request_url': getattr(exc, 'relay_url', None),
                'payload': text if len(text) <= CAPTURE_MAX_BYTES else None, 'payload_bytes': len(text)})
        except Exception:
            LOG.warning('relay origin capture failed', exc_info=True)

    def _fetch(self, host, url, **kwargs):
        """self.fetch, with a transport refusal naming the host that refused (Alpha 2026-10-06:
        origin_rate_limit at 21:15:45Z could have been Relay /chains, Relay /requests, the Solana
        RPC or the EVM origin RPC). The base reason before ':' is unchanged for the retry rules."""
        try:
            return self.fetch(url, **kwargs)
        except ProofError as exc:
            base = str(exc)
            if base in {'origin_rate_limit', 'origin_provider_unavailable'} or base.startswith('origin_http_'):
                raise ProofError('%s:%s' % (base, host)) from exc
            raise

    def _verify(self, item, seen=None):
        row, transaction = item['row'], item['transaction']
        proof = row['buy']['source_initiation']
        headers = {'Accept': 'application/json'}
        if self.api_key:
            headers['x-api-key'] = self.api_key
        order_hint = '0x' + transaction['input'][-64:].lower()
        if self.v3:
            # v3 authenticates every request: no key, no call (it would be a typed 401 anyway).
            require(bool(self.api_key), 'relay_api_key_missing')
            url = 'https://api.relay.link/requests/v3?' + urlencode({'orderId': order_hint})
        else:
            # Lookup hint only: verify_origin recomputes this commitment and binds the fill.
            url = 'https://api.relay.link/requests/v2?' + urlencode(
                {'orderId': order_hint, 'includeOrderData': 'true'})
        self.pacer.wait()                        # per-process Relay /requests rate (Part 1)
        payload = self._fetch('relay_requests', url, headers=headers, timeout=.8)
        if seen is not None:
            seen['payload'] = payload
        try:
            return self._settle(item, row, proof, transaction, payload, headers)
        except ProofError as exc:
            exc.relay_url, exc.relay_payload = url, payload      # on the exception: two worker threads
            if getattr(exc, 'from_exception', False):
                # WHAT ELSE IS ABSENT (Alpha 2026-10-07): the block-absent decision needs it.
                exc.detail = (getattr(exc, 'detail', None) or '') + _relay_missing_keys(payload, proof, self.v3)
            raise

    def _settle(self, item, row, proof, transaction, payload, headers):
        requests = payload.get('requests')
        require(isinstance(requests, list), 'malformed_relay_response')
        require(len(requests) == 1, 'relay_request_not_indexed' if not requests else 'ambiguous_relay_request')
        request = requests[0]
        if self.v3:
            require(request.get('supersededByRequestId') is None, 'relay_request_superseded')
        status = request.get('status')
        require(status not in {'failure', 'refund'}, 'relay_request_failed')
        # v3's enum adds submitted and waiting (both before settlement); v2's `delayed` is gone
        # and any value outside the map is malformed, never a pass.
        unsettled = {'pending', 'depositing', 'submitted', 'waiting'} if self.v3 else {'pending', 'depositing'}
        if status != 'success':
            err = ProofError('relay_not_settled' if status in unsettled else 'malformed_origin_evidence')
            if status not in unsettled:
                err.detail = 'status=%r' % (status,)
            raise err
        deposit = request.get('protocol', {}).get('deposit', {}).get('origin', {})
        origin_chain = deposit.get('chainId')
        signature = deposit.get('transactionId')
        require(isinstance(signature, str) and 64 <= len(signature) <= 88, 'malformed_origin_signature')
        # Bound Solana proofs use pinned contracts and on-chain order IDs, not the catalogue.
        if not self.v3 or origin_chain != SOLANA_CHAIN:
            with self.chains_lock:
                if self.chains is None:
                    self.chains = self._fetch('relay_chains', 'https://api.relay.link/chains',
                                              headers=headers, timeout=1)['chains']
        if origin_chain == SOLANA_CHAIN:
            response = self._fetch('solana_rpc', self.solana_rpc, body={'jsonrpc': '2.0', 'id': 1, 'method': 'getTransaction',
                              'params': [signature, {'encoding': 'jsonParsed', 'commitment': 'confirmed',
                                                     'maxSupportedTransactionVersion': 1}]}, timeout=.8)
            require(not response.get('error'), 'origin_rpc_rejected')
            origin = response.get('result')
            require(isinstance(origin, dict), 'origin_transaction_not_indexed')
        else:
            rpc = self.evm_rpcs.get(str(origin_chain))
            require(bool(rpc), 'unsupported_origin_chain')
            response = self._fetch('evm_rpc', rpc, body=[{'jsonrpc': '2.0', 'id': i, 'method': method,
                                             'params': params} for i, (method, params) in enumerate([
                ('eth_getTransactionByHash', [signature]), ('eth_getTransactionReceipt', [signature]),
                ('eth_chainId', [])])], timeout=.8)
            require(isinstance(response, list) and len(response) == 3, 'origin_rpc_rejected')
            indexed = {reply['id']: reply for reply in response}
            require(set(indexed) == {0, 1, 2} and not any(reply.get('error') for reply in response),
                    'origin_rpc_rejected')
            require(all(reply.get('result') is not None for reply in response), 'origin_transaction_not_indexed')
            origin = dict(transaction=indexed[0]['result'], receipt=indexed[1]['result'],
                          chain_id=indexed[2]['result'])
        result = (verify_bound_origin if self.v3 else verify_origin)(
            request, origin, proof, transaction,
            identities=self.identities_fn(row['source_user_id']) if origin_chain == SOLANA_CHAIN else [],
            chains=self.chains, block_absent_ok=self.accept_block_absent,
            destination_time=_header_time(row, proof) if self.accept_block_absent else None)
        # New private dictionaries only. The publisher/socket never observes a half-updated proof.
        verified_row = dict(row, buy=dict(row['buy'], source_initiation=result))
        return dict(item, row=verified_row)


def _attempt_note(exc, started, seen):
    """`<error|ok>/<ms>/<Relay status>/<d1|d0|->[/<detail>]` for one verifier attempt, from the
    answer it already fetched: status `none` when Relay listed no request, `-` without one usable
    answer; d1 when deposit.origin.transactionId is present, d0 when absent. Never raises."""
    ms = int((time.perf_counter() - started) * 1000)
    reason = 'ok' if exc is None else str(exc) if isinstance(exc, ProofError) else type(exc).__name__
    status = deposit = '-'
    try:
        answer = seen.get('payload')
        requests = answer.get('requests') if isinstance(answer, dict) else None
        if requests == []:
            status = 'none'
        elif isinstance(requests, list) and len(requests) == 1 and isinstance(requests[0], dict):
            status = re.sub(r'[^A-Za-z0-9_.-]', '?', str(requests[0].get('status')))[:24]
            origin = (((requests[0].get('protocol') or {}).get('deposit') or {}).get('origin') or {})
            deposit = 'd1' if isinstance(origin, dict) and origin.get('transactionId') else 'd0'
    except Exception:
        pass
    note = '%s/%d/%s/%s' % (re.sub(r'[^A-Za-z0-9_:.-]', '?', reason)[:64], ms, status, deposit)
    # THE ATTEMPT'S OWN DETAIL (Alpha 2026-10-07): a malformed attempt that later settles named
    # nothing, so which field Relay indexes late was unknown. A fifth field, only when set, with no
    # '/', ',' or space so the line's separators hold.
    detail = getattr(exc, 'detail', None)
    if detail:
        note += '/' + re.sub(r"[^A-Za-z0-9_:.()'=@-]", '?', str(detail).replace(' at ', '@'))[:256]
    return note


class OriginWorker:
    """Bounded asynchronous verification. submit never waits for RPC or a free worker."""
    def __init__(self, verify_fn, publish_fn, *, workers=2, capacity=64, on_stop=None):
        self.verify_fn, self.publish_fn = verify_fn, publish_fn
        self.on_stop = on_stop
        self.queue = queue.Queue(maxsize=capacity)
        self.stop = threading.Event()
        self.pending = {}
        self.pending_capacity = capacity + workers
        self.pending_lock = threading.Lock()       # source watcher only, never funded entry
        self.stats = {'queued': 0, 'queue_full': 0, 'verified': 0, 'refused': 0, 'callback_failed': 0}
        self.threads = [threading.Thread(target=self._run, daemon=True, name='relay-origin-%s' % i)
                        for i in range(workers)]
        for thread in self.threads:
            thread.start()

    def submit(self, item):
        key = item.get('tx_hash') or item.get('row', {}).get('tx_hash')
        with self.pending_lock:
            if key in self.pending:
                return True
            if len(self.pending) >= self.pending_capacity:
                self.stats['queue_full'] += 1
                return False
            self.pending[key] = item.get('row', {}).get('buy', {}).get('source_initiation', {}).get('block_number')
            try:
                self.queue.put_nowait(item)
                self.stats['queued'] += 1
                return True
            except queue.Full:
                self.pending.pop(key, None)
                self.stats['queue_full'] += 1
                return False

    def frontier(self, block):
        """Hold restart backfill before any queued/in-flight uncommitted source block."""
        with self.pending_lock:
            blocks = [b - 1 for b in self.pending.values() if type(b) is int and b > 0]
        return min([block] + blocks) if block is not None else min(blocks) if blocks else None

    def _run(self):
        try:
            self._run_work()
        finally:
            if self.on_stop is not None:
                self.on_stop()

    def _run_work(self):
        while not self.stop.is_set():
            try:
                item = self.queue.get(timeout=.1)
            except queue.Empty:
                continue
            started = time.perf_counter()
            try:
                result = self.verify_fn(item)
                self.stats['verified'] += 1
            except Exception as exc:
                self.stats['refused'] += 1
                result = dict(item, origin_verification_error=str(exc) if isinstance(exc, ProofError)
                              else 'origin_verification_failed',
                              origin_verification_detail=getattr(exc, 'detail', None),
                              origin_attempts=getattr(exc, 'origin_attempts', None))
            result['origin_verification_ms'] = round((time.perf_counter() - started) * 1000, 3)
            published = False
            try:
                self.publish_fn(result)
                published = True
            except Exception:
                self.stats['callback_failed'] += 1
                LOG.exception('Relay origin publish failed')
            finally:
                # Keep failed publication at the restart frontier; never fabricate coverage.
                if published:
                    key = item.get('tx_hash') or item.get('row', {}).get('tx_hash')
                    with self.pending_lock:
                        self.pending.pop(key, None)
                self.queue.task_done()

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=.2)
