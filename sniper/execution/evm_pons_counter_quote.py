"""Generic ERC-20-counter Pons routes. Entry reads immutable prepared state only.

The bridge buys an EXACT counter quantity with a bounded WETH spend. Relay refunds
unused WETH; no intermediate counter inventory is left in our wallet. On exit the
curve pays SwapRouter02, which consumes its entire counter balance atomically.
No Relay/Kyber HTTP quote, new contract, or counter-token allowlist is needed.
"""
from __future__ import annotations

import logging
import math
import re
import time
from types import MappingProxyType

from eth_abi import encode, decode
from sniper.execution.evm_dex_quote import _encode_exact_input_single
from sniper.execution.evm_launchpad_curve import encode_buy, encode_sell, encode_approve
from sniper.execution.evm_relay_quote import RELAY_APPROVAL_PROXY, RELAY_ROUTER

WETH = '0x0bd7d308f8e1639fab988df18a8011f41eacad73'
ZERO = '0x' + '00' * 20
MAX_AGE_S = 2.0
MULTICALL3 = '0xca11bde05977b3631167028862be2a173976ca11'
MULTICALL3_CODE_HASH = '0xd5c15df687b16f2ff992fc8d767b4216323184a2bbc6ee2f9c398c318e770891'
log = logging.getLogger(__name__)
_ADDRESS = re.compile(r'0x[0-9a-f]{40}\Z')
_CALL_TYPES = ['address[]', 'uint256[]', '(address,bool,uint256,bytes)[]',
               'address', 'address', 'bytes']


def _net(amount, fee, tax):
    return amount - amount * fee // 10000 - amount * tax // 10000


def _valid(s, *, entry=True):
    try:
        addresses = [s[k] for k in ('mint', 'curve', 'counter', 'router', 'bridge_pool')]
        return (all(isinstance(a, str) and _ADDRESS.fullmatch(a) and a != ZERO for a in addresses)
                and len(set(addresses)) == len(addresses)
                and s['counter'] != WETH and s['mint'] != WETH
                and all(type(s[k]) is int and 0 < s[k] < 2**256 for k in
                        (('quote_reserve', 'token_reserve', 'max_input', 'max_counter',
                          'reverse_cash', 'sellable', 'block_number') if entry
                         else ('quote_reserve', 'token_reserve')))
                and type(s['graduated']) is int and s['graduated'] == 0
                and all(type(s[k]) is int and 0 <= s[k] < 10000 for k in ('fee_bps', 'tax_bps'))
                and s['fee_bps'] + s['tax_bps'] < 10000
                and type(s['bridge_fee']) is int and 0 <= s['bridge_fee'] < 1000000)
    except (KeyError, TypeError, ValueError):
        return False


def _bundle(s, token, amount, taker, output, minimum, calls, *, round_trip=None):
    cleanup = bytes.fromhex('9bb43718') + encode(
        ['address[]', 'address[]', 'uint256[]', 'bytes'],
        [[WETH, s['counter'], s['mint']], [taker] * 3, [0] * 3, b''])
    calls = [(*c[:3], bytes.fromhex(c[3][2:])) for c in calls]
    calls.append((RELAY_ROUTER, False, 0, cleanup))
    data = '0xf9e4bab4' + encode(_CALL_TYPES,
        [[token], [amount], calls, taker, taker, b'']).hex()
    route = dict(protocol='pons_v2_counter', curve=s['curve'], counter=s['counter'],
                 bridge_pool=s['bridge_pool'], bridge_fee=s['bridge_fee'],
                 bridge_router=s['router'], state_block=s.get('block_number'))
    if round_trip is not None:
        route.update(round_trip_pct=round_trip, round_trip_basis='prepared_conservative_bound')
    return dict(sellAmount=str(amount), buyAmount=str(output), minBuyAmount=str(minimum),
                liquidityAvailable=True,
                transaction=dict(to=RELAY_APPROVAL_PROXY, data=data, value='0x0', gas='1200000'),
                issues=dict(allowance=dict(spender=RELAY_APPROVAL_PROXY)), route=route)


def buy_quote(s, amount, taker, slippage_bps):
    """A bounded atomic buy. Cached maximum-clip bridge rates are conservative for smaller clips.

    Standard V3 output per input decreases as input increases, so the maximum-clip
    forward/reverse rates lower-bound smaller clips. The reverse uses the curve's
    POST-buy reserves and the bridge's pre-buy rate (also conservative). WETH refunds
    are omitted from the cost bound, never counted as extra profit.
    """
    if not _valid(s) or type(amount) is not int or not 0 < amount <= s['max_input']:
        return None
    if not _ADDRESS.fullmatch(str(taker)) or taker == ZERO or not 0 <= slippage_bps < 10000:
        return None
    counter = amount * s['max_counter'] // s['max_input']
    net = _net(counter, s['fee_bps'], s['tax_bps'])
    output = net * s['token_reserve'] // (s['quote_reserve'] + net)
    if not 0 < output <= s['sellable'] or output >= s['token_reserve']:
        return None
    minimum = output * (10000 - slippage_bps) // 10000
    returned = output * (s['quote_reserve'] + net) // s['token_reserve']
    returned = _net(returned, s['fee_bps'], s['tax_bps'])
    if not 0 < returned <= s['max_counter'] or minimum <= 0:
        return None
    cash = returned * s['reverse_cash'] // s['max_counter']
    bridge = '0x5023b4df' + encode(
        ['(address,address,uint24,address,uint256,uint256,uint160)'],
        [(WETH, s['counter'], s['bridge_fee'], RELAY_ROUTER, counter, amount, 0)]).hex()
    calls = [(WETH, False, 0, encode_approve(s['router'], amount)),
             (s['router'], False, 0, bridge),
             (s['counter'], False, 0, encode_approve(s['curve'], counter)),
             (s['curve'], False, 0, encode_buy(counter, minimum, taker))]
    return _bundle(s, WETH, amount, taker, output, minimum, calls,
                   round_trip=100 * (cash / amount - 1))


def sell_quote(s, amount, taker, counter_out, cash_out, slippage_bps):
    """Fresh exact curve + V3 quoter reads must supply these outputs; entry cache cannot supply them."""
    if (not _valid(s, entry=False) or type(amount) is not int or not 0 < amount < 2**256
            or type(counter_out) is not int or counter_out <= 0
            or type(cash_out) is not int or cash_out <= 0
            or not _ADDRESS.fullmatch(str(taker)) or taker == ZERO
            or not 0 <= slippage_bps < 10000):
        return None
    floor = cash_out * (10000 - slippage_bps) // 10000
    counter_floor = counter_out * (10000 - slippage_bps) // 10000
    if floor <= 0 or counter_floor <= 0:
        return None
    calls = [(s['mint'], False, 0, encode_approve(s['curve'], amount)),
             (s['curve'], False, 0, encode_sell(amount, counter_floor, s['router'])),
             (s['router'], False, 0, _encode_exact_input_single(
                 s['counter'], WETH, s['bridge_fee'], taker, 0, floor))]
    return _bundle(s, s['mint'], amount, taker, cash_out, floor, calls)


class CounterCache:
    """One background writer publishes whole immutable snapshots; readers never acquire a lock."""
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.snapshot = (None, MappingProxyType({}))
        self.thread = None

    def publish(self, rows, *, started):
        self.snapshot = (started, MappingProxyType({k: MappingProxyType(dict(v)) for k, v in rows.items()}))

    def state(self, mint):
        at, rows = self.snapshot
        age = None if at is None else self.clock() - at
        return rows.get(str(mint).lower()) if age is not None and 0 <= age <= MAX_AGE_S else None

    def quote(self, sell, buy, amount, taker, slippage_bps):
        if str(sell).lower() != WETH:
            return None
        s = self.state(buy)
        return buy_quote(s, int(amount), str(taker).lower(), slippage_bps) if s is not None else None


CACHE = CounterCache()


def find_bridge(counter, amount, read, *, sell=False):
    """Configured standard V3 deployments only; factory binds the derived pool identity."""
    from eth_utils import keccak
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_pons_v2_quote import _address
    from sniper.execution.evm_dex_quote import _single_quote_data, _decode_amount_out
    selector = lambda sig: '0x' + keccak(text=sig)[:4].hex()
    best = None
    for deployment in cfg.memecoin_evm_v3_deployments('4663'):
        if deployment.get('protocol', 'uniswap_v3') != 'uniswap_v3':
            continue
        router, quoter = str(deployment['router']).lower(), str(deployment['quoter']).lower()
        rf, qf, rw, fv2, pm = read([(router, selector('factory()')), (quoter, selector('factory()')),
                          (router, selector('WETH9()')), (router, selector('factoryV2()')),
                          (router, selector('positionManager()'))])
        factory = _address(rf)
        # Factory/WETH getters alone also match routers with incompatible swap selectors.
        # Router02's immutable getters bind the ABI needed for exact-output + amountIn=0.
        if (factory is None or factory != _address(qf) or _address(rw) != WETH
                or _address(fv2) is None or _address(pm) is None):
            continue
        fees = cfg.memecoin_evm_v3_fee_tiers('4663')
        pools = read([(factory, selector('getPool(address,address,uint24)') + encode(
            ['address', 'address', 'uint24'], [WETH, counter, fee]).hex()) for fee in fees])
        for fee, raw in zip(fees, pools):
            pool = _address(raw)
            if pool is None:
                continue
            token_in, token_out = (counter, WETH) if sell else (WETH, counter)
            output = _decode_amount_out(read([(quoter, _single_quote_data(
                token_in, token_out, amount, fee))])[0])
            if output and (best is None or output > best[0]):
                best = (output, dict(router=router, quoter=quoter, bridge_pool=pool, bridge_fee=int(fee)))
    return best


def exit_quote(s, amount, taker, slippage_bps, call_fn):
    """Whole sold quantity, fresh curve state + bridge quote. Cold/manual exits need no cache."""
    gross = amount * s['quote_reserve'] // (s['token_reserve'] + amount)
    counter = _net(gross, s['fee_bps'], s['tax_bps'])
    if counter <= 0 or s['counter'] == WETH:
        return None
    batch = getattr(call_fn, 'batch', None)
    read = batch if callable(batch) else lambda calls: [call_fn(*c) for c in calls]
    from sniper.execution.evm_dex_quote import _single_quote_data, _decode_amount_out
    known = CACHE.snapshot[1].get(s['mint'])
    if known and known['curve'] == s['curve'] and known['counter'] == s['counter']:
        # Immutable routing identity can be reused; executable exit price never can.
        cash = _decode_amount_out(call_fn(known['quoter'], _single_quote_data(
            s['counter'], WETH, counter, known['bridge_fee'])))
        bridge = (cash, {k: known[k] for k in ('router', 'quoter', 'bridge_pool', 'bridge_fee')}) if cash else None
    else:
        bridge = find_bridge(s['counter'], counter, read, sell=True)
    if bridge is None:
        return None
    # The same pinned contracts as existing Relay quotes, verified before custody uses them.
    verify = getattr(call_fn, 'verify_relay', None)
    if not callable(verify):
        from sniper.execution.evm_pons_v2_quote import PonsReadUnavailable
        raise PonsReadUnavailable('code_identity_unavailable')
    verify()
    cash, identity = bridge
    return sell_quote(s | identity, amount, taker, counter, cash, slippage_bps)


def prepare(conn, rpc, *, now, identities, bridges):
    """Background-only bounded tape discovery and pinned-block reads. All counters are derived.

    `rpc` accepts JSON-RPC method/params pairs and returns matching results. Failures
    raise; a partial result cannot publish an executable snapshot. Immutable identity
    discoveries are reused, but reserves, fees and both bridge quotes refresh every pass.
    """
    from eth_utils import keccak
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_volume_writer import iso
    from sniper.execution.evm_pons_v2_quote import _GETTERS, _address, _uint
    from sniper.execution.evm_dex_quote import _single_quote_data, _decode_amount_out
    from sniper.services.memecoin_pons_metadata import _info_address

    def read(calls, block):
        answers = rpc([('eth_call', [{'to': to, 'data': data}, block]) for to, data in calls])
        if not isinstance(answers, (list, tuple)) or len(answers) != len(calls):
            raise ValueError('partial counter state batch')
        return answers
    def selector(sig):
        return '0x' + keccak(text=sig)[:4].hex()
    # Rowid bounds discovery work even when tape history is large; overflow stays fallback.
    rows = conn.execute("SELECT m.pool,m.token1,m.token0 FROM pool_ticks t "
        "JOIN pool_meta m ON m.chain_id=t.chain_id AND m.pool=t.pool "
        "WHERE t.id>(SELECT COALESCE(MAX(id),0)-20000 FROM pool_ticks) "
        "AND t.chain_id='4663' AND t.kind='pons_v2_swap' AND t.observed_at>=? "
        "AND m.resolve_state='pons_verified' AND m.token0 NOT IN (?,?) "
        "GROUP BY m.pool ORDER BY MAX(t.id) DESC LIMIT 32", (iso(now - 60), ZERO, WETH)).fetchall()
    # Prepare newly verified launches before their first watched wallet buy, not just swaps.
    launches = conn.execute("SELECT pool,token1,token0 FROM pool_meta WHERE chain_id='4663' "
        "AND resolve_state='pons_verified' AND resolved_at>=? AND token0 NOT IN (?,?) "
        "ORDER BY resolved_at DESC LIMIT 32", (iso(now - 60), ZERO, WETH)).fetchall()
    rows = list(dict.fromkeys(tuple(r) for r in [*rows, *launches]))[:32]
    price = conn.execute("SELECT usd,observed FROM volume_quote_prices "
                         "WHERE chain_id='4663' AND token=?", (WETH,)).fetchone()
    if not rows or price is None:
        return {}
    px, at = price
    if not (isinstance(px, (int, float)) and math.isfinite(px) and px > 0
            and isinstance(at, (int, float)) and 0 <= now - at <= 120):
        return {}
    block = rpc([('eth_blockNumber', [])])[0]
    if not isinstance(block, str) or not re.fullmatch(r'0x[0-9a-fA-F]+', block):
        raise ValueError('counter snapshot block unavailable')
    maximum = math.ceil(cfg.MEMECOIN_REAL_MAX_ENTRY_USD / px * 10**18 * 1.01)
    factories = {str(e['address']).lower(): e for e in cfg.MEMECOIN_EVM_V4_LAUNCHPADS.get('4663', ())}
    # Identity is verified independently of the tape labels, in two bounded multicalls.
    pending = [(c,m,x) for c,m,x in rows if m not in identities]
    identity_reads = read([(c,selector(sig)) for c,m,x in pending
                           for sig in ('token()', 'pairToken()', 'factory()')], block) if pending else []
    verified = []
    for index, (curve,mint,counter) in enumerate(pending):
        token,pair,factory = identity_reads[index*3:index*3+3]
        factory = _address(factory)
        entry = factories.get(factory)
        if _address(token) == mint and _address(pair) == counter and entry is not None:
            verified.append((curve,mint,counter,factory,entry))
    infos = read([(f,'0x'+e['info_selector']+m[2:].rjust(64,'0'))
                  for c,m,x,f,e in verified], block) if verified else []
    for (curve,mint,counter,factory,entry),info in zip(verified,infos):
        if (_info_address(info,entry['coin_word']) == mint
                and _info_address(info,1) == curve
                and _info_address(info,entry['counter_word']) == counter):
            identities[mint] = dict(mint=mint,curve=curve,counter=counter)
    # No counter-token names: bridge identities are derived from configured factories.
    for counter in dict.fromkeys(r[2] for r in rows if r[1] in identities):
        if counter not in bridges or bridges[counter].get('retry_after', math.inf) <= now:
            found = find_bridge(counter, maximum, lambda calls: read(calls, block))
            bridges[counter] = found[1] if found is not None else {'retry_after': now + 30}
    candidates = [identities[m] | bridges[x] for c, m, x in rows
                  if m in identities and x in bridges and 'router' in bridges[x] and identities[m]['curve'] == c]
    if not candidates:
        return {}
    # Discovery may be cold. Price state gets its OWN fresh block and clock afterward.
    read_started = time.monotonic()
    block = rpc([('eth_blockNumber', [])])[0]
    if not isinstance(block, str) or not re.fullmatch(r'0x[0-9a-fA-F]+', block):
        raise ValueError('counter pricing block unavailable')
    counters = dict((s['counter'], s) for s in candidates)
    keys = list(counters)
    calls = [(s['curve'], '0x' + sel) for s in candidates for sel in _GETTERS]
    calls += [(s['quoter'], _single_quote_data(WETH, x, maximum, s['bridge_fee']))
              for x, s in counters.items()]
    answers = read(calls, block)
    outputs = {x: _decode_amount_out(raw) for x, raw in zip(keys, answers[7 * len(candidates):])}
    quantities = {x: output * 99 // 100 for x, output in outputs.items() if output}
    reverse = read([(counters[x]['quoter'], _single_quote_data(x, WETH, qty,
                         counters[x]['bridge_fee'])) for x, qty in quantities.items()], block) if quantities else []
    backs = {x: _decode_amount_out(raw) for x, raw in zip(quantities, reverse)}
    result = {}
    for index, s in enumerate(candidates):
        v = answers[index * 7:index * 7 + 7]
        if (_address(v[0]) != s['mint'] or _address(v[1]) != s['counter']
                or s['counter'] not in quantities):
            continue
        counter, back = quantities[s['counter']], backs[s['counter']]
        snapshot = s | dict(max_input=maximum, max_counter=counter, reverse_cash=back,
            quote_reserve=_uint(v[2], 0), token_reserve=_uint(v[2], 1),
            fee_bps=_uint(v[3]), tax_bps=_uint(v[4]), graduated=_uint(v[5]), sellable=_uint(v[6]),
            block_number=int(block, 16), read_started=read_started)
        if _valid(snapshot):
            result[s['mint']] = snapshot
    return result


def public_rpc(session):
    """Private background session, strict matched batches. Only explicit call reverts are misses."""
    from sniper.services.memecoin_evm_holdings import PUBLIC_RPC
    from sniper.execution.evm_dex_quote import _classify_rpc_error
    def rpc(calls):
        if (len(calls) > 1 and all(m == 'eth_call' and p[1] == calls[0][1][1] for m, p in calls)):
            data = '0x82ad56cb' + encode(['(address,bool,bytes)[]'], [[
                (p[0]['to'], True, bytes.fromhex(p[0]['data'][2:])) for _, p in calls]]).hex()
            raw = rpc([('eth_call', [dict(to=MULTICALL3, data=data), calls[0][1][1]])])[0]
            if not isinstance(raw, str):
                raise ValueError('counter multicall unavailable')
            results = decode(['(bool,bytes)[]'], bytes.fromhex(raw[2:]))[0]
            if len(results) != len(calls):
                raise ValueError('partial counter multicall')
            return ['0x' + ret.hex() if ok else None for ok, ret in results]
        out = []
        for offset in range(0, len(calls), 50):
            chunk = calls[offset:offset + 50]
            response = session.post(PUBLIC_RPC, json=[dict(jsonrpc='2.0', id=i, method=m, params=p)
                for i, (m, p) in enumerate(chunk)], timeout=2)
            response.raise_for_status()
            raw = response.json()
            if not isinstance(raw, list) or len(raw) != len(chunk):
                raise ValueError('partial counter RPC batch')
            by_id = {r.get('id'): r for r in raw if isinstance(r, dict)}
            if set(by_id) != set(range(len(chunk))):
                raise ValueError('unmatched counter RPC batch')
            for i, (method, _) in enumerate(chunk):
                r = by_id[i]
                if 'error' in r or 'result' not in r:
                    if method == 'eth_call' and _classify_rpc_error(r) == 'revert':
                        out.append(None)
                        continue
                    raise ValueError('counter RPC unavailable')
                out.append(r['result'])
        return out
    return rpc


def start_cache():
    """Runner startup only. Private public-RPC socket; no funded provider, lock, or entry wait."""
    import json
    import sqlite3
    import threading
    from contextlib import closing
    from pathlib import Path
    from eth_utils import keccak
    from sniper.util.clock import real_utc_now, real_utc_now_iso
    from sniper.util.state_paths import state_root
    from sniper.execution.evm_relay_quote import RELAY_APPROVAL_PROXY_CODE_HASH, RELAY_ROUTER_CODE_HASH
    if CACHE.thread is not None:
        return

    def run():
        import requests
        identities, bridges, episode = {}, {}, None
        root = Path(state_root())
        with requests.Session() as session:
            rpc = public_rpc(session)
            authorized = False
            while True:
                started, error, rows = CACHE.clock(), None, {}
                try:
                    if not authorized:
                        chain, proxy, router, multicall = rpc([('eth_chainId', []),
                            ('eth_getCode', [RELAY_APPROVAL_PROXY, 'latest']),
                            ('eth_getCode', [RELAY_ROUTER, 'latest']),
                            ('eth_getCode', [MULTICALL3, 'latest'])])
                        if (int(chain, 16) != 4663
                                or '0x' + keccak(bytes.fromhex(proxy[2:])).hex() != RELAY_APPROVAL_PROXY_CODE_HASH
                                or '0x' + keccak(bytes.fromhex(router[2:])).hex() != RELAY_ROUTER_CODE_HASH
                                or '0x' + keccak(bytes.fromhex(multicall[2:])).hex() != MULTICALL3_CODE_HASH):
                            raise ValueError('counter router identity mismatch')
                        authorized = True
                    file = (root / 'data/memecoin_onchain_volume.db').resolve()
                    with closing(sqlite3.connect(file.as_uri() + '?mode=ro', uri=True, timeout=0,
                                                 isolation_level=None)) as conn:
                        rows = prepare(conn, rpc, now=real_utc_now(), identities=identities, bridges=bridges)
                except Exception as exc:
                    error = type(exc).__name__
                    if error != episode:
                        log.warning('Pons counter preparation unavailable: %s', error)
                price_started = min((s['read_started'] for s in rows.values()), default=started)
                CACHE.publish(rows, started=price_started)
                if episode is not None and error is None:
                    log.info('Pons counter preparation recovered: %d ready', len(rows))
                episode = error
                # Runtime proof, off path; no trade/ledger state is written.
                try:
                    beat = dict(at=real_utc_now_iso(), ready=len(rows) if CACHE.clock()-price_started <= MAX_AGE_S else 0, error=error,
                                refresh_ms=round((CACHE.clock()-started)*1000, 3),
                                max_age_s=MAX_AGE_S, counters=len(bridges),
                                state_blocks=sorted({s['block_number'] for s in rows.values()}))
                    path = root / 'data/memecoin_pons_counter_heartbeat.json'
                    tmp = path.with_suffix('.tmp')
                    tmp.write_text(json.dumps(beat))
                    tmp.replace(path)
                except OSError:
                    log.warning('Pons counter heartbeat write failed')
                # ponytail: 32 active curves; overflow or preparation older than 2s stays fallback.
                time.sleep(0.5 if error is None else 5)
    CACHE.thread = threading.Thread(target=run, name='pons-counter-quotes', daemon=True)
    CACHE.thread.start()
