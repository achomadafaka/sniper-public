"""Source-block venue identity for descriptive Wallet Universe measurements.

All RPC reads are injected. This module never constructs a provider, reads a
ledger, signs, submits or changes shared execution settings. Receipt discovery
is restricted to trusted BSC V2/V3 factories; other chains use the caller's
registered metadata. That distinction is retained in each returned pool.
"""
from __future__ import annotations

import copy
import re

from eth_abi import decode, encode

from sniper.services.memecoin_bsc_volume_poll import V2_FACTORIES, V3_FACTORIES, GET_PAIR, GET_POOL
from sniper.services.memecoin_executed_pool import V2_SWAP, V3_SWAP, PANCAKE_V3_SWAP
from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, v4_pool_id
from sniper.services.wallet_universe_metrics import assets_for, native_symbol

METHOD = "wallet-universe-venues-v1"
ZERO = "0x" + "00" * 20
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_PROTOCOLS = frozenset(("v2", "v3", "pancake_v3", "v4", "solidly", "aerodrome", "pons", "pons_v1", "pons_v2"))


class VenuePreparationError(ValueError):
    """Typed failure carrying already prepared pools for a bounded later retry."""
    def __init__(self, reason, prepared_pools=()):
        super().__init__(reason)
        self.reason = reason
        self.prepared_pools = copy.deepcopy(list(prepared_pools))


def _address(value, reason="invalid_address", *, zero=False):
    if not isinstance(value, str) or not _ADDRESS.fullmatch(value) or (not zero and value.lower() == ZERO):
        raise ValueError(reason)
    return value.lower()


def _hash(value, reason="invalid_hash"):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError(reason)
    return value.lower()


def _integer(value, reason, *, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(reason)
    try:
        result = int(value, 16) if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]+", value) else int(value)
    except (TypeError, ValueError):
        raise ValueError(reason) from None
    if result < 0 or maximum is not None and result > maximum:
        raise ValueError(reason)
    return result


def _abi_result(value, kind):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise ValueError("malformed_contract_result")
    try:
        return decode([kind], bytes.fromhex(value[2:]), strict=True)[0]
    except Exception:
        raise ValueError("malformed_contract_result") from None


def _header(call, block, block_hash):
    header = call("eth_getBlockByNumber", [hex(block), False])
    if not isinstance(header, dict) or _integer(header.get("number"), "invalid_source_header") != block:
        raise ValueError("invalid_source_header")
    if _hash(header.get("hash"), "invalid_source_header_hash") != block_hash:
        raise ValueError("source_block_reorg")


def _eth_read(call, target, selector, block, kind, local_cache):
    key = (target, selector)
    if key not in local_cache:
        raw = call("eth_call", [dict(to=target, data=selector), hex(block)])
        local_cache[key] = _abi_result(raw, kind)
    return local_cache[key]


def _quote_identity(chain, mint, p, trusted):
    token0 = _address(p.get("token0"), "invalid_token0", zero=p["protocol"] == "v4")
    token1 = _address(p.get("token1"), "invalid_token1", zero=p["protocol"] == "v4")
    if token0 == token1 or int(token0, 16) >= int(token1, 16):
        raise ValueError("invalid_pool_token_order")
    if mint not in (token0, token1):
        raise ValueError("pool_mint_mismatch")
    side = 1 if token0 == mint else 0
    quote = (token0, token1)[side]
    if quote == ZERO and p["protocol"] == "v4":
        symbol = native_symbol(chain)
        if not symbol:
            raise ValueError("unknown_native_quote")
    elif quote in trusted:
        symbol = trusted[quote]
    else:
        raise ValueError("untrusted_quote_asset")
    p.update(token0=token0, token1=token1, quote_side=side, quote_symbol=symbol)


def _v4_identity(chain, p):
    manager = V4_POOL_MANAGERS.get(chain)
    if not manager:
        raise ValueError("unsupported_v4_chain")
    if p.get("manager") is not None and _address(p["manager"]) != manager:
        raise ValueError("wrong_v4_manager")
    p["manager"] = manager
    key = p.get("pool_key")
    if key is not None:
        if not isinstance(key, dict):
            raise ValueError("malformed_v4_pool_key")
        key = dict(key)
    else:
        key = {name: p.get(name) for name in ("fee", "tick_spacing", "hooks")}
        key.update(currency0=p.get("currency0", p["token0"]), currency1=p.get("currency1", p["token1"]))
    for side in (0, 1):
        if _address(key.get("currency" + str(side)), "invalid_v4_currency", zero=True) != p["token" + str(side)]:
            raise ValueError("v4_currency_identity_mismatch")
    complete = all(key.get(name) is not None for name in ("fee", "tick_spacing", "hooks"))
    p["v4_pool_key_verified"] = complete
    if complete and v4_pool_id(key) != p["pool"]:
        raise ValueError("v4_pool_id_mismatch")


def _fill_decimals(call, block, p, local_cache):
    for side in (0, 1):
        field = "token" + str(side) + "_decimals"
        token = p["token" + str(side)]
        # The only native-currency pools admitted here are the canonical V4
        # managers on ETH-denominated chains. Native ETH has 18 atomic decimals.
        observed = 18 if token == ZERO else _integer(
            _eth_read(call, token, "0x313ce567", block, "uint8", local_cache), "invalid_decimals", maximum=36)
        if p.get(field) is not None and _integer(p[field], "invalid_cached_decimals", maximum=36) != observed:
            raise ValueError("cached_decimals_mismatch")
        p[field] = observed


def _bsc_authenticate(call, block, p, local_cache, *, discovered=False):
    pool = p["pool"]
    token0 = _address(_eth_read(call, pool, "0x0dfe1681", block, "address", local_cache))
    token1 = _address(_eth_read(call, pool, "0xd21220a7", block, "address", local_cache))
    if not discovered and (token0 != p["token0"] or token1 != p["token1"]):
        raise ValueError("cached_pool_tokens_mismatch")
    p.update(token0=token0, token1=token1)
    factory = _address(_eth_read(call, pool, "0xc45a0155", block, "address", local_cache))
    if p["protocol"] == "v2":
        if factory not in V2_FACTORIES:
            raise ValueError("untrusted_v2_factory")
        data = "0x" + (GET_PAIR + encode(["address", "address"], [token0, token1])).hex()
    elif p["protocol"] in ("v3", "pancake_v3"):
        trusted = dict(V3_FACTORIES)
        if factory not in trusted:
            raise ValueError("untrusted_v3_factory")
        fee = _integer(_eth_read(call, pool, "0xddca3f43", block, "uint24", local_cache), "invalid_pool_fee")
        if fee not in trusted[factory]:
            raise ValueError("unsupported_pool_fee")
        if p.get("fee") is not None and _integer(p["fee"], "invalid_cached_fee") != fee:
            raise ValueError("cached_pool_fee_mismatch")
        p["fee"] = fee
        data = "0x" + (GET_POOL + encode(["address", "address", "uint24"], [token0, token1, fee])).hex()
    else:
        raise ValueError("unsupported_bsc_pool_protocol")
    actual = _address(_eth_read(call, factory, data, block, "address", local_cache), "factory_pool_not_found")
    if actual != pool:
        raise ValueError("factory_pool_identity_mismatch")
    p["factory"] = factory


def prepare_pools(chain, mint, source_signature, call, cached_pools=()):
    """Return verified metadata dictionaries accepted by ``collect_evm``.

    All contract reads use the exact source receipt's block, with canonical hash
    checks before and after. Unknown source venues never become an empty pool
    population. Same-source successful preparations can be supplied again in
    ``cached_pools``; their identity and source block are checked on each call.
    Existing non-BSC pool identities remain caller-registered evidence rather
    than newly factory-authenticated discovery. V4 currencies must come from
    registered metadata: Swap logs alone do not disclose them.
    """
    prepared = []
    try:
        chain = str(chain)
        _integer(chain, "invalid_chain")
        mint = _address(mint, "invalid_mint")
        signature = _hash(source_signature, "invalid_source_signature")
        if _integer(call("eth_chainId", []), "invalid_provider_chain") != int(chain):
            raise ValueError("wrong_chain")
        receipt = call("eth_getTransactionReceipt", [signature])
        if not isinstance(receipt, dict):
            raise ValueError("source_receipt_unavailable")
        if _hash(receipt.get("transactionHash"), "missing_receipt_signature") != signature:
            raise ValueError("source_signature_mismatch")
        if _integer(receipt.get("status"), "missing_receipt_status") != 1:
            raise ValueError("source_transaction_failed")
        block = _integer(receipt.get("blockNumber"), "missing_receipt_block")
        block_hash = _hash(receipt.get("blockHash"), "missing_receipt_block_hash")
        index = _integer(receipt.get("transactionIndex"), "missing_receipt_order")
        _header(call, block, block_hash)
        trusted = {_address(token): symbol for token, symbol, _decimals in assets_for(chain)}
        candidates = {}
        for raw in cached_pools:
            if not isinstance(raw, dict):
                raise ValueError("malformed_cached_pool")
            p = copy.deepcopy(raw)
            p["protocol"] = str(p.get("protocol", "")).lower()
            if p["protocol"] not in _PROTOCOLS:
                raise ValueError("unsupported_pool_protocol")
            pool = _hash(p.get("pool"), "invalid_v4_pool") if p["protocol"] == "v4" else _address(p.get("pool"), "invalid_pool")
            if pool in candidates:
                raise ValueError("duplicate_cached_pool")
            if p.get("chain") is not None and str(p["chain"]) != chain:
                raise ValueError("cached_pool_chain_mismatch")
            p["pool"] = pool
            _quote_identity(chain, mint, p, trusted)
            if p["protocol"] == "v4":
                _v4_identity(chain, p)
            candidates[pool] = p
        logs = receipt.get("logs")
        if not isinstance(logs, list):
            raise ValueError("missing_receipt_logs")
        discovery = set()
        for log in logs:
            if not isinstance(log, dict) or not isinstance(log.get("topics"), list) or not log["topics"]:
                raise ValueError("malformed_receipt_log")
            topic = _hash(log["topics"][0], "invalid_receipt_topic")
            if topic not in (V2_SWAP, V3_SWAP, PANCAKE_V3_SWAP):
                continue
            if (log.get("removed") is not False
                    or _hash(log.get("transactionHash"), "missing_log_signature") != signature
                    or _hash(log.get("blockHash"), "missing_log_block_hash") != block_hash
                    or _integer(log.get("blockNumber"), "missing_log_block") != block
                    or _integer(log.get("transactionIndex"), "missing_log_order") != index):
                raise ValueError("source_log_anchor_mismatch")
            _integer(log.get("logIndex"), "missing_log_index")
            pool = _address(log.get("address"), "invalid_source_pool")
            protocol = "v2" if topic == V2_SWAP else "pancake_v3" if topic == PANCAKE_V3_SWAP else "v3"
            if chain == "56":
                if pool not in candidates:
                    candidates[pool] = dict(pool=pool, protocol=protocol)
                    discovery.add(pool)
                elif candidates[pool]["protocol"] != protocol and {candidates[pool]["protocol"], protocol} != {"v3", "pancake_v3"}:
                    raise ValueError("source_pool_protocol_mismatch")
        local_cache = {}
        for pool, p in sorted(candidates.items()):
            cached_proof = (p.get("metadata_method") == METHOD and p.get("verified_source_signature") == signature
                            and p.get("verified_block") == block and p.get("verified_block_hash") == block_hash
                            and p.get("chain") == chain)
            if not cached_proof:
                if chain == "56":
                    # Receipt routes can also contain bridge pools. Discover token
                    # identities first and exclude only those proved not to contain
                    # the requested mint; an unreadable identity is still a gap.
                    if pool in discovery:
                        token0 = _address(_eth_read(call, pool, "0x0dfe1681", block, "address", local_cache))
                        token1 = _address(_eth_read(call, pool, "0xd21220a7", block, "address", local_cache))
                        if mint not in (token0, token1):
                            continue
                        p.update(token0=token0, token1=token1)
                    _quote_identity(chain, mint, p, trusted)
                    _bsc_authenticate(call, block, p, local_cache, discovered=pool in discovery)
                    p["verification"] = "trusted_factory_at_source_block"
                else:
                    p["verification"] = "registered_metadata_and_source_block_decimals"
                _fill_decimals(call, block, p, local_cache)
            else:
                # A reusable proof never excuses absent/invalid denominations.
                for side in (0, 1):
                    _integer(p.get("token" + str(side) + "_decimals"), "invalid_verified_decimals", maximum=36)
            p.update(chain=chain, metadata_method=METHOD, verified_source_signature=signature,
                     verified_block=block, verified_block_hash=block_hash)
            prepared.append(p)
        if not prepared:
            raise ValueError("no_verified_pools")
        _header(call, block, block_hash)
        return prepared
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc) else "malformed_venue_metadata"
        raise VenuePreparationError(reason, prepared) from exc
