"""Pure validation of stored receipt-backed wallet fill economics; no admission authority."""
from __future__ import annotations

import math
import re


PROOF_VERSION = "wallet-net-fill-v1"
RECEIPT_PROOF_VERSIONS = frozenset({
    "direct-wallet-paid-v1", "relay-kyber-linked-v1", "relay-envelope-linked-v1",
    "relay-envelope-other-target-v1",
})
_ADDRESS = re.compile(r"0x[0-9a-f]{40}\Z")
_HASH = re.compile(r"0x[0-9a-f]{64}\Z")
_POSITIVE_TEXT = re.compile(r"[1-9][0-9]*\Z")
_NATIVE = "0x" + "00" * 20
_FIELDS = ("proof_version", "receipt_proof_version", "network_id", "tx_hash", "block_number",
           "block_hash", "wallet", "source_user_id", "mint", "payment_token", "payment_atomic",
           "acquired_atomic", "payment_decimals", "token_decimals", "paid_usd")


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _atomic(value, bits=256):
    return (_matches(_POSITIVE_TEXT, value) and len(value) <= 78 and int(value) < 2**bits)


def _positive_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _source_post(post):
    if not isinstance(post, dict):
        return None
    if post.get('reason') == 'split_route' and 'pools' in post:
        pools = post['pools']
        if (post.get('venue') != 'v4' or post.get('receipt_routes_complete') is not True
                or not isinstance(pools, list) or not 2 <= len(pools) <= 8
                or not all(_matches(_HASH, pool) for pool in pools)
                or len(set(pools)) != len(pools) or not _atomic(post.get('pool_output_atomic'))):
            return None
        return dict(reason='split_route', venue='v4', pools=sorted(pools),
                    pool_output_atomic=post['pool_output_atomic'], receipt_routes_complete=True)
    if post.get('venue') == 'pons_v2':
        fields = ('reason', 'venue', 'pool', 'pool_input_atomic', 'pool_output_atomic',
                  'receipt_routes_complete')
        if (post.get('reason') != 'pons_post_state_unavailable'
                or not _matches(_ADDRESS, post.get('pool'))
                or not all(_atomic(post.get(k)) for k in ('pool_input_atomic', 'pool_output_atomic'))
                or post.get('receipt_routes_complete') is not True):
            return None
        return {k: post[k] for k in fields}
    if "reason" in post:
        reason = post["reason"]
        return {"reason": reason} if isinstance(reason, str) and 0 < len(reason) <= 128 else None
    if type(post.get("receipt_routes_complete")) is not bool:
        return None
    if not post["receipt_routes_complete"]:
        return {"reason": "v4_key_unknown"}
    venue = post.get("venue")
    if (venue not in ("v2", "v3", "v4")
            or not _matches(_HASH if venue == "v4" else _ADDRESS, post.get("pool"))
            or not _positive_number(post.get("ratio"))
            or type(post.get("mint_is_c1")) is not bool
            or not _atomic(post.get("pool_input_atomic"))
            or not _atomic(post.get("pool_output_atomic"))):
        return None
    fields = ("venue", "pool", "ratio", "mint_is_c1", "pool_input_atomic", "pool_output_atomic",
              "receipt_routes_complete")
    if venue == "v2":
        if not all(_atomic(post.get(key), 112) for key in ("reserve0_atomic", "reserve1_atomic")):
            return None
        fields += ("reserve0_atomic", "reserve1_atomic")
        if type(post.get("buy_tax")) in (int, float) and 0 <= post["buy_tax"] < 1:
            fields += ("buy_tax",)
    else:
        if not _atomic(post.get("sqrt_price_x96"), 160):
            return None
        fields += ("sqrt_price_x96",)
    if "liquidity_atomic" in post:
        liquidity = post["liquidity_atomic"]
        if venue != "v4" or (liquidity != "0" and not _atomic(liquidity, 128)):
            return None
        if liquidity != "0":
            fields += ("liquidity_atomic",)
    return {key: post[key] for key in fields}


def _payment_basis(proof):
    if proof["receipt_proof_version"] != "direct-wallet-paid-v1":
        # Existing Relay receipt proof authenticates its payer's complete ERC20 payment delta.
        basis = proof.get("payment_basis", "relay_receipt_outflow")
        return basis if basis == "relay_receipt_outflow" and proof["payment_token"] != _NATIVE else None
    basis = proof.get("payment_basis")
    if basis == "wallet_quote_outflow" and proof["payment_token"] != _NATIVE:
        return basis
    if (basis == "transaction_value_upper_bound" and proof["payment_token"] == _NATIVE
            and proof["payment_decimals"] == 18):
        return basis
    return None


def validate_source_fill(proof, sig):
    """Return a canonical proof bound to the exact signal, else None.

    Signal keys: wallet/source_user_id/mint/tx_hash and seq (or block_number). A chain or
    network_id must be canonical decimal text; absent chain denotes the RH reader's 4663.
    An optional signal block_hash binds the receipt hash too. Positive atoms remain TEXT.
    Missing source_post is allowed for measurement; a native consumer must require unique post.
    Native transaction value is an upper bound, not refund-net payment; strict funded consumers
    must reject that basis unless no-refund semantics are independently proved. This checks
    decoder provenance and identity, not signatures, trader intent or admission.
    """
    if not isinstance(proof, dict) or not isinstance(sig, dict):
        return None
    chain = sig.get("chain", sig.get("network_id", "4663"))
    block = sig.get("seq", sig.get("block_number"))
    if (proof.get("proof_version") != PROOF_VERSION
            or not isinstance(proof.get("receipt_proof_version"), str)
            or proof.get("receipt_proof_version") not in RECEIPT_PROOF_VERSIONS
            or not _matches(_POSITIVE_TEXT, proof.get("network_id"))
            or proof["network_id"] != chain
            or ("chain" in sig and "network_id" in sig and sig["chain"] != sig["network_id"])
            or type(proof.get("block_number")) is not int or proof["block_number"] <= 0
            or type(block) is not int or proof["block_number"] != block
            or not _matches(_HASH, proof.get("block_hash"))
            or ("block_hash" in sig and proof["block_hash"] != sig["block_hash"])
            or not _matches(_HASH, proof.get("tx_hash"))
            or proof["tx_hash"] != sig.get("tx_hash")
            or not isinstance(proof.get("source_user_id"), str) or not proof["source_user_id"]
            or proof["source_user_id"] != sig.get("source_user_id")
            or any(not _matches(_ADDRESS, proof.get(key)) for key in ("wallet", "mint", "payment_token"))
            or any(proof[key] != sig.get(key) for key in ("wallet", "mint"))
            or not all(_atomic(proof.get(key)) for key in ("payment_atomic", "acquired_atomic"))
            or any(type(proof.get(key)) is not int or not 0 <= proof[key] <= 36
                   for key in ("payment_decimals", "token_decimals"))
            or not _positive_number(proof.get("paid_usd"))):
        return None
    basis = _payment_basis(proof)
    if basis is None:
        return None
    result = {key: proof[key] for key in _FIELDS}
    result["payment_basis"] = basis
    if "source_post" in proof:
        post = _source_post(proof["source_post"])
        if post is None:
            return None
        if post.get('reason') == 'split_route' and 'pools' in post:
            acquired, output = int(proof['acquired_atomic']), int(post['pool_output_atomic'])
            if not acquired <= output or output * 9 > acquired * 10:
                return None
        result["source_post"] = post
    if 'source_initiation' in proof:
        from types import SimpleNamespace
        from sniper.services.memecoin_source_initiation import source_initiation_refusal
        ev = SimpleNamespace(signal_source='onchain_watch', source_wallet=proof['wallet'],
            source_user_id=proof['source_user_id'], mint=proof['mint'],
            source_block_number=proof['block_number'], source_initiation=proof['source_initiation'],
            fomo_event_id='onchain:%s:%s:%s' % (proof['network_id'], proof['tx_hash'], proof['mint']))
        if source_initiation_refusal(ev, proof['network_id']) is not None:
            return None
        init = proof['source_initiation']
        if (init['tx_hash'] != proof['tx_hash'] or init['block_hash'] != proof['block_hash']
                or init.get('acquired_atomic', proof['acquired_atomic']) != proof['acquired_atomic']):
            return None
        result['source_initiation'] = dict(init)
    return result


def source_fill_from_buy(buy, sig):
    """Build only from existing decoded receipt evidence; never replace missing facts with zero."""
    if not isinstance(buy, dict) or not isinstance(sig, dict) or "source_fill_reason" in buy:
        return None
    init = buy.get("source_initiation")
    if not isinstance(init, dict):
        return None
    receipt_version = buy.get("receipt_proof_version")
    if (not isinstance(receipt_version, str) or receipt_version not in RECEIPT_PROOF_VERSIONS
            or type(init.get("schema_version")) is not int or init["schema_version"] != 1
            or not _matches(_ADDRESS, init.get("payer"))
            or (receipt_version == "direct-wallet-paid-v1" and init["payer"] != sig.get("wallet"))
            or (init.get("proof_version") != receipt_version
                and not (receipt_version.startswith("relay-")
                         and init.get("proof_version") in ("relay-origin-paid-v1",
                                                           "relay-origin-bound-v1",
                                                           "relay-deposit-processed-v1")))
            or ("acquired_atomic" in init and init["acquired_atomic"] != buy.get("acquired_atomic"))
            or (init.get("source_user_id") is not None
                and init["source_user_id"] != sig.get("source_user_id"))):
        return None
    proof = dict(proof_version=PROOF_VERSION, receipt_proof_version=receipt_version,
        network_id=init.get("network_id"), tx_hash=init.get("tx_hash"),
        block_number=init.get("block_number"), block_hash=init.get("block_hash"),
        wallet=init.get("beneficiary"), source_user_id=sig.get("source_user_id"), mint=init.get("mint"),
        payment_token=buy.get("payment_token"), payment_atomic=buy.get("payment_atomic"),
        acquired_atomic=buy.get("acquired_atomic"), payment_decimals=buy.get("payment_decimals"),
        token_decimals=buy.get("token_decimals"), paid_usd=buy.get("source_size_usd"))
    if "payment_basis" in buy:
        proof["payment_basis"] = buy["payment_basis"]
    if "source_post" in buy:
        proof["source_post"] = buy["source_post"]
    if init.get('proof_version') in ('relay-origin-paid-v1', 'relay-origin-bound-v1',
                                     'relay-deposit-processed-v1'):
        proof['source_initiation'] = dict(init)
    return validate_source_fill(proof, sig)
