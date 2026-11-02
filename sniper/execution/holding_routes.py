"""Validate manual Robinhood sales through three known LI.FI routes.

The automated route manifest stays separate. Contract sources and runtime hashes came from
Robinhood Blockscout and the public RPC on 2026-09-17. Tests contain quote examples.
"""
from copy import deepcopy

from sniper.execution import lifi_route as r

TOOLS = ['nordstern', 'fly', 'kyberswap']
WETH = '0x0bd7d308f8e1639fab988df18a8011f41eacad73'
KYBER_TYPES = ['(address,address,bytes,(address,address,address[],uint256[],address[],uint256[],address,uint256,uint256,uint256,bytes),bytes)']
PINS = {
    'fly': {
        'target': '0x20f6ee51340adeed01a59b0e65cb3703f3dc860c',
        'code_hash': '0xaffe8098fa7a152f718c07de0ea7657382e535a301aaf12f89bbba8f91e4b352',
        'executor': '0x09ad820aac5779683b481c4674208a4e1b024afa',
        'executor_code_hash': '0xc3508fb257eee0d20085dfdf41a56e95e8912ca82e3d0a9a9f6e696a261ed1cd',
        'selector': '0x46ec278a',
    },
    'kyberswap': {
        'target': '0x6131b5fae19ea4f9d964eac0408e4408b66337b5',
        'code_hash': '0xdc6eb20a6d4701d8f0f04f9a3342d254eb2698bbad281d8578d6efba21865867',
        'executor': '0x8f10b468b06c6fd214b65f87778827f7d113f996',
        'executor_code_hash': '0xfc8bfd5c118d0c06e9ff71b223fc3ba9612ac167edbf7e7e6e22bec156a5e70d',
        'selector': '0xe21fd0e9',
    },
    # KyberSwap on BSC (2026-09-23). Same router and executor ADDRESSES as 4663 (Kyber deploys
    # MetaAggregationRouterV2 at one address on every chain) but DIFFERENT bytecode, so the code
    # hashes are their own pin: measured on bsc-dataseed1.defibit.io at 15:0xZ from one real
    # /bsc/api/v1/route/build (GPERP), router 18,652 bytes, executor 13,529 bytes, selector
    # identical. A hash from 4663 must never be reused here; the verifier would refuse every route.
    'kyberswap_56': {
        'target': '0x6131b5fae19ea4f9d964eac0408e4408b66337b5',
        'code_hash': '0xe3ebca8e2e93d25ffe58633779fbd60452793976b812e36612a11ffddd13a4b3',
        'executor': '0x8f10b468b06c6fd214b65f87778827f7d113f996',
        'executor_code_hash': '0xc3fe18bd9e1e31adecfcaba58177f3d796c3028e7d53a628e321e18295e10682',
        'selector': '0xe21fd0e9',
    },
    # KyberSwap on Base (2026-09-25). Same router and executor addresses again. The ROUTER bytecode
    # is Base's own (18,312 bytes; 4663 and BSC each differ); the EXECUTOR bytecode matches BSC's
    # byte for byte (13,529 bytes). Measured from one real /base/api/v1/route/build at 21:15:36Z,
    # then eth_getCode on base-rpc.publicnode.com and mainnet.base.org, which agreed.
    'kyberswap_8453': {
        'target': '0x6131b5fae19ea4f9d964eac0408e4408b66337b5',
        'code_hash': '0x99590941aab8e1eaf2ceb825abf64b6956e82ce41e8966d7e320846905db85e3',
        'executor': '0x8f10b468b06c6fd214b65f87778827f7d113f996',
        'executor_code_hash': '0xc3fe18bd9e1e31adecfcaba58177f3d796c3028e7d53a628e321e18295e10682',
        'selector': '0xe21fd0e9',
    },
}


def manifest_for(base, tool):
    r._require(tool in TOOLS, 'unsupported manual sell route')
    manifest = deepcopy(base)
    manifest['allow_exchanges'] = list(TOOLS)
    manifest['top_level']['tool'] = tool
    if tool in PINS:
        step = manifest['steps'][1]
        step.update(PINS[tool], tool=tool)
        step['approve_to'] = step['target']
    return manifest


def _packed_number(data, at):
    shift = data[at]
    offset = int.from_bytes(data[at + 1:at + 3], 'big')
    size = (256 - shift + 7) // 8
    r._require(offset + size <= len(data), 'truncated Fly number')
    return int.from_bytes(data[offset:offset + 32].ljust(32, b'\0'), 'big') >> shift


def _swap(step, swap, pin, input_token, output_token, amount, minimum, recipient):
    if pin['tool'] == 'nordstern':
        return r._validate_swap_step(step, swap, pin, input_token, output_token,
                                     amount, minimum, recipient)
    call_to, approve_to, src, dst, qty, data, deposit = swap
    r._require(call_to == pin['target'] and approve_to == pin['target'], 'wrong swap target')
    r._require(src == input_token and dst == output_token, 'wrong swap tokens')
    r._require(qty == amount and deposit is False, 'wrong swap amount or deposit')
    r._require(data[:4].hex() == pin['selector'][2:], 'wrong swap selector')
    action, estimate = step['action'], step['estimate']
    r._require(r._token(action, 'fromToken') == src and r._token(action, 'toToken') == dst,
               'wrong swap action tokens')
    r._require(r._atomic(action['fromAmount'], positive=True) == amount, 'wrong swap action amount')
    r._require(r._atomic(estimate['toAmountMin'], positive=True) == minimum, 'wrong swap action minimum')
    if pin['tool'] == 'kyberswap':
        target, approve, target_data, desc, _client_data = r.decode_exact(KYBER_TYPES, data[4:])[0]
        src, dst, receivers, amounts, fees, fee_amounts, receiver, qty, nested_min, flags, permit = desc
        r._require(target == pin['executor'] and approve == '0x' + '0' * 40,
                   'wrong Kyber executor')
        r._require(bool(target_data) and receivers == (target,) and amounts == (amount,),
                   'wrong Kyber input recipients')
        r._require(not fees and not fee_amounts and not permit and flags == 512,
                   'unsupported Kyber fees, permit or flags')
    else:
        r.decode_exact(['bytes'], data[4:])
        r._require(len(data) >= 275, 'truncated Fly calldata')
        receiver = '0x' + data[72:92].hex()
        src, dst = '0x' + data[92:112].hex(), '0x' + data[112:132].hex()
        qty = int.from_bytes(data[132:164], 'big')
        nested_min = _packed_number(data, 167)
        r._require(data[208] == 0 and data[274] == 0, 'unsupported Fly transfer or fees')
        r._require(_packed_number(data, 205) <= 10000, 'invalid Fly retention')
    r._require(src == input_token and dst == output_token, 'wrong nested tokens')
    r._require(receiver == recipient, 'wrong nested recipient')
    r._require(qty == amount, 'wrong nested input amount')
    # LI.FI rounds these WETH minima by up to two wei. The outer minimum remains exact.
    r._require(nested_min >= max(1, minimum - 2), 'wrong nested minimum')
    return pin['executor'], nested_min


def decode_holding_quote(response, request, base):
    r._require(r._address(request.get('toToken')) == WETH, 'manual sell must receive WETH')
    r._require(r._address(request.get('fromToken')) != WETH, 'cannot sell the bankroll')
    manifest = manifest_for(base, response.get('tool'))
    r._require(r._address((response.get('estimate') or {}).get('approvalAddress'))
               == manifest['top_level']['spender'], 'wrong approval spender')
    return r.decode_quote(response, request, manifest, swap_validator=_swap)


def verify_holding_quote(decoded, ctx, base):
    """Check deployed code and the Fly core before an approval or a swap signature."""
    from eth_utils import keccak
    from sniper.execution.lifi_executor import prove_quote_authority, _call
    from sniper.util.clock import real_utc_now_iso
    from sniper.services.memecoin_evm_holdings import PUBLIC_RPC
    manifest = manifest_for(base, decoded.tool)
    proof = prove_quote_authority(
        decoded, decoded.request_sha256, decoded.response_sha256,
        ctx['rpc_url'], PUBLIC_RPC, manifest=manifest, stage='holding_sell',
        received_at=real_utc_now_iso(), http=ctx.get('http'))
    if decoded.tool == 'fly':
        call = {'to': PINS['fly']['target'], 'data': '0x' + keccak(text='coreAddress()')[:4].hex()}
        for endpoint in (ctx['rpc_url'], PUBLIC_RPC):
            result = _call(endpoint, 'eth_call', [call, hex(proof.block_number)],
                           http=ctx.get('http')).get('result')
            r._require(result == '0x' + '0' * 24 + PINS['fly']['executor'][2:], 'wrong Fly core')
    return proof
