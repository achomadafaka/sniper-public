"""Receipt-only source attribution. No provider reads or cached token-safety verdicts."""
import re
from types import SimpleNamespace

_ADDRESS = re.compile(r"0x[0-9a-f]{40}\Z")
_HASH = re.compile(r"0x[0-9a-f]{64}\Z")
DIRECT_PROOF_VERSION = "direct-wallet-paid-v1"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RELAY_ORIGIN_PROOF_VERSION = "relay-origin-paid-v1"
RELAY_BOUND_PROOF_VERSION = "relay-origin-bound-v1"     # v3, default refused (operator knob)
# 4663 fast lane option C (memecoin_rh_deposit_proof): the user's own signed Solana deposit, seen at
# processed and bound to the fill by order id; no destination receipt exists yet, so no block hash.
RELAY_DEPOSIT_PROOF_VERSION = "relay-deposit-processed-v1"
# 4663 Relay pre-buy (spec 2026-10-14-rh-relay-prebuy-design.md): the signed deposit plus its Relay request.
RELAY_PREBUY_PROOF_VERSION = "relay-request-prebuy-v1"
SOLANA_PROGRAM_PROOF_VERSION = "solana-program-user-paid-v1"
# The slot lane's RabbitStream signal (memecoin_rabbit_signal): owner-signed, BEFORE execution.
PRE_EXECUTION_PROOF_VERSION = "solana-pre-execution-owner-signed-v1"
# The chains whose native fast lane (memecoin_rh_fast_lane.LiveLane) books `rhfast:<chain>:` origins:
# 4663 and Base 8453 (plan 2026-10-14 part A). Receipt-less option C stays 4663's alone.
_NATIVE_LANE_CHAINS = ("4663", "8453")


def _hex(value, pattern):
    value = value.lower() if isinstance(value, str) else ""
    return value if pattern.fullmatch(value) else None


def _quantity(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value, 16 if value.startswith("0x") else 10) if isinstance(value, str) else int(value)
        return parsed if parsed > 0 and not isinstance(value, float) else None
    except (TypeError, ValueError, OverflowError):
        return None


def direct_receipt_initiation(receipt, transaction, wallet, mint, *, chain=None):
    """Called only after wallet_paid_token_flows proves linked receipt payment/acquisition.

    Incoherent or incomplete mined identity leaves initiation unknown while retaining observation.
    """
    receipt, transaction = receipt or {}, transaction or {}
    tx_hash = _hex(transaction.get("hash"), _HASH)
    block_hash = _hex(transaction.get("blockHash"), _HASH)
    block = _quantity(transaction.get("blockNumber"))
    tx_chain = _quantity(transaction.get("chainId"))
    network = str(chain) if chain is not None else str(tx_chain or "")
    owner, token = _hex(wallet, _ADDRESS), _hex(mint, _ADDRESS)
    if (not owner or not token or not tx_hash or not block_hash or not block
            or not network.isdigit() or int(network) <= 0
            or (transaction.get("chainId") is not None and str(tx_chain) != network)
            or str(receipt.get("status")).lower() not in {"1", "0x1", "true"}
            or _hex(receipt.get("transactionHash"), _HASH) != tx_hash
            or _hex(receipt.get("blockHash"), _HASH) != block_hash
            or _quantity(receipt.get("blockNumber")) != block
            or _hex(transaction.get("from"), _ADDRESS) != owner
            or _hex(transaction.get("from"), _ADDRESS) != _hex(receipt.get("from"), _ADDRESS)
            or not _hex(transaction.get("to"), _ADDRESS)
            or _hex(transaction.get("to"), _ADDRESS) != _hex(receipt.get("to"), _ADDRESS)):
        return None
    return dict(schema_version=1, proof_version=DIRECT_PROOF_VERSION, network_id=network,
                tx_hash=tx_hash, block_hash=block_hash, block_number=block, mint=token,
                beneficiary=owner, payer=owner)


def relay_receipt_initiation(proof):
    """Preserve beneficiary-only Relay evidence without granting initiating-wallet authority."""
    return dict(schema_version=1, proof_version=proof.get("proof_version"),
                network_id=proof.get("network_id"), tx_hash=proof.get("transaction_hash"),
                block_hash=proof.get("block_hash"), block_number=_quantity(proof.get("block_number")),
                mint=proof.get("mint"), beneficiary=proof.get("beneficiary"), payer=proof.get("payer"),
                acquired_atomic=str(proof["output_atomic"]) if _quantity(proof.get("output_atomic")) else None)


def solana_receipt_initiation(tx, owner, facts):
    """Use the already-read successful receipt's signer and linked swap payment/output."""
    try:
        message = tx['transaction']['message']
        keys = message['accountKeys']
        count = (message.get('header') or {}).get('numRequiredSignatures')
        signed = any(k.get('pubkey') == owner and k.get('signer') is True for k in keys if isinstance(k, dict))
        if all(isinstance(k, str) for k in keys) and type(count) is int and 0 < count <= len(keys):
            signed = owner in keys[:count]
        payment = None
        if facts.get('basis') == 'pump_event_net_sol':
            payment = facts.get('gross_quote_atomic')
        elif facts.get('basis') in {'route_owner_quote_out', 'unknown'}:
            # Linked route amounts are known before the independent USD conversion can fail.
            payment = next((amount for amount in (facts.get('quote_out_atomic') or {}).values()
                            if _quantity(amount)), None)
        if (tx['meta']['err'] is not None or not signed or not _quantity(tx.get('slot'))
                or not _quantity(payment) or not _quantity(facts.get('token_amount_atomic'))
                or facts.get('owner') != owner or facts.get('source_slot') != tx['slot']):
            return None
        proof = dict(schema_version=1, proof_version=DIRECT_PROOF_VERSION, network_id='solana',
                     tx_hash=tx['transaction']['signatures'][0], block_number=tx['slot'],
                     mint=facts['mint'], beneficiary=owner, payer=owner,
                     acquired_atomic=facts['token_amount_atomic'], payment_atomic=payment)
        usdc_out = (facts.get('quote_out_atomic') or {}).get(USDC)
        if facts.get('basis') in {'route_owner_quote_out', 'unknown'} and _quantity(usdc_out):
            # Gamma's N1 residual 1: the USDC that went INTO the route, fees and unrelated
            # transfers excluded; absent when it cannot be bound (a routed resume then refuses).
            from sniper.services import memecoin_source_valuation as valuation
            route, why = valuation.route_payment_evidence(tx, owner, int(usdc_out))
            if route:
                proof['route_payment_atomic'] = str(route)
            elif why:
                proof['route_payment_unknown'] = why     # EVM-03: the class, for the resume line
        return proof
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return None


def solana_program_initiation(buy, mint):
    """A known Pump buy event names the signing user; routed balance changes alone do not."""
    if getattr(getattr(buy, 'routed', None), 'requires_receipt_proof', False):
        return None  # An intermediate quote-token leg does not prove the whole routed payment.
    from sniper.execution.pump_curve_buy import parse_trade_event
    from sniper.execution.pumpswap_buy import parse_buy_event, AMM_PROGRAM
    from sniper.services.memecoin_slot_signal import PUMP_PROGRAM
    try:
        venue = getattr(buy, 'venue', 'pump_curve')
        if venue not in {'pump_curve', 'pump_amm'}:
            return None
        event = (parse_trade_event if venue == 'pump_curve' else parse_buy_event)(buy.trade_event_log)
        amount = event['token_amount'] if venue == 'pump_curve' else event['base_amount_out']
        paid = event['sol_amount'] if venue == 'pump_curve' else event['quote_amount_in_with_lp_fee']
        quote_curve = venue == 'pump_curve' and not paid and event.get('quote_amount')
        if quote_curve:
            # A curve quoted in another token (2026-10-07) pays its quote, not SOL. pump's `user` is
            # a Signer: an on-curve wallet named there signed the transaction (a PDA could only
            # have been signed for by a program), so the event binds the copied wallet as signer.
            from solders.pubkey import Pubkey
            if not Pubkey.from_string(buy.owner).is_on_curve():
                return None
            # payment_atomic is then QUOTE-token atoms (WBTC, LINK...), not lamports: no reader may
            # convert it as SOL (Gamma INFO on 3581d7c62; today only its positivity is checked).
            paid = event['quote_amount']
        if (event['user'] != buy.owner or not event.get('is_buy', True) or not _quantity(amount)
                or not _quantity(paid) or (venue == 'pump_curve' and event['mint'] != mint)):
            return None
        return dict(schema_version=1, proof_version=SOLANA_PROGRAM_PROOF_VERSION, network_id='solana',
                    tx_hash=buy.signature, block_number=buy.slot, mint=mint, beneficiary=buy.owner,
                    payer=buy.owner, source_user_id=buy.source_user_id,
                    acquired_atomic=str(amount), payment_atomic=str(paid),
                    program_id=PUMP_PROGRAM if venue == 'pump_curve' else AMM_PROGRAM)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _solana_refusal(ev, proof):
    import base58
    def key(value, size):
        try:
            low, high = (32, 44) if size == 32 else (64, 88)
            return isinstance(value, str) and low <= len(value) <= high and len(base58.b58decode(value)) == size
        except ValueError:
            return False
    wallet, mint = getattr(ev, 'source_wallet', None), getattr(ev, 'mint', None)
    signature, slot = proof.get('tx_hash'), _quantity(proof.get('block_number'))
    uid = getattr(ev, 'source_user_id', None)
    if (type(proof.get('schema_version')) is not int or proof['schema_version'] != 1
            or proof.get('network_id') != 'solana' or not key(wallet, 32) or not key(mint, 32)
            or not key(signature, 64) or not slot or slot != _quantity(getattr(ev, 'source_block_number', None))
            or proof.get('beneficiary') != wallet or proof.get('payer') != wallet
            or proof.get('mint') != mint or not isinstance(uid, str) or not uid or proof.get('source_user_id') != uid
            or not _quantity(proof.get('acquired_atomic')) or not _quantity(proof.get('payment_atomic'))
            or getattr(ev, 'fomo_event_id', None) not in {f'onchain:{signature}', f'slotlane:{signature}'}):
        return 'receipt_identity_mismatch'
    if proof.get('proof_version') == SOLANA_PROGRAM_PROOF_VERSION:
        from sniper.services.memecoin_slot_signal import PUMP_PROGRAM, PUMP_AMM_PROGRAM
        if proof.get('program_id') not in {PUMP_PROGRAM, PUMP_AMM_PROGRAM}:
            return 'wallet_initiation_unproved'
    return None


def _pre_execution_refusal(ev, proof):
    """The pre-execution proof (memecoin_rabbit_signal), accepted ONLY for the Solana slot lane's own
    pre-execution buy: a RabbitStream frame with MEMECOIN_SLOT_LANE_RABBIT_SIGNAL and _RABBIT_BUY on,
    or a Helius preconf frame (proof feed preconf:*) with MEMECOIN_SLOT_LANE_PRECONF on and the owner's
    signature verified locally (2026-10-17). It proves the owner signed (a required signer), never
    that it paid or landed."""
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_rabbit_signal import PROVENANCE, SIGNATURE_BASES, feed_kind
    # The curve program, or PumpSwap for the DFlow route only (memecoin_rabbit_dflow_amm, Alpha GO
    # 2026-10-17), and only while MEMECOIN_SLOT_LANE_RABBIT_DFLOW_AMM is on.
    program_ok = (_okx_amm_route_ok(proof, cfg) or proof.get('program_id') == '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
                  or (proof.get('program_id') == 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA'
                      and proof.get('route') == 'dflow_pump_amm'
                      and bool(getattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_DFLOW_AMM', False))))
    preconf = feed_kind(proof.get('feed')) == 'preconf'
    armed = (bool(getattr(cfg, 'MEMECOIN_SLOT_LANE_PRECONF', False))
             and proof.get('signature_basis') == 'ed25519_local' if preconf
             else cfg.MEMECOIN_SLOT_LANE_RABBIT_SIGNAL and cfg.MEMECOIN_SLOT_LANE_RABBIT_BUY)
    if (not armed
            or getattr(ev, 'signal_source', None) != 'slot_lane'
            or getattr(ev, 'source_timestamp_trust', None) != PROVENANCE
            or proof.get('signature_basis') not in SIGNATURE_BASES
            or not program_ok
            or proof.get('payment_basis') != 'pre_execution_estimate'):
        return 'wallet_initiation_unproved'
    wallet, mint = getattr(ev, 'source_wallet', None), getattr(ev, 'mint', None)
    signature, slot = proof.get('tx_hash'), _quantity(proof.get('block_number'))
    uid = getattr(ev, 'source_user_id', None)
    if (type(proof.get('schema_version')) is not int or proof['schema_version'] != 1
            or proof.get('network_id') != 'solana' or not wallet or not mint or not signature
            or not slot or slot != _quantity(getattr(ev, 'source_block_number', None))
            or proof.get('beneficiary') != wallet or proof.get('payer') != wallet
            or proof.get('mint') != mint or not isinstance(uid, str) or not uid
            or proof.get('source_user_id') != uid or not _quantity(proof.get('acquired_atomic'))
            or getattr(ev, 'fomo_event_id', None) != 'slotlane:' + str(signature)):
        return 'receipt_identity_mismatch'
    return None


# Relay option A (spec 2026-10-09): an unproved Relay row's watcher error, named for the refusal.
_RELAY_REFUSAL_CLASSES = {
    "relay_request_not_indexed": "relay_origin_not_indexed",
    "origin_wallet_unverified": "relay_origin_foreign_depositor",
    "foreign_depositor": "relay_origin_foreign_depositor",
    "shared_depositor": "relay_origin_shared_depositor",
    "origin_rate_limit": "relay_origin_rate_limit",
}


_ROUTER_AMM_KNOBS = {'okx_pump_amm': 'MEMECOIN_SLOT_LANE_RABBIT_OKX_AMM',
                     'jupiter_pump_amm': 'MEMECOIN_SLOT_LANE_RABBIT_JUPITER_AMM'}


def _okx_amm_route_ok(proof, cfg):
    """PumpSwap for the OKX and Jupiter routes (memecoin_rabbit_okx_amm, memecoin_rabbit_jupiter_amm,
    Alpha GO 2026-10-17), each only while its own knob is on."""
    knob = _ROUTER_AMM_KNOBS.get(proof.get('route'))
    return (proof.get('program_id') == 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA'
            and knob is not None and bool(getattr(cfg, knob, False)))


def relay_refusal_reason(error):
    """The refusal of an unproved Relay row: its class when the watcher named one; a label-only
    pre-gate's own reason, marked unproved; else wallet_initiation_unproved as before."""
    text = str(error or "")
    if text.startswith("pregate:"):
        return "%s [relay origin unproved]" % text[len("pregate:"):].strip()
    return _RELAY_REFUSAL_CLASSES.get(text.split(":")[0], "wallet_initiation_unproved")


def source_initiation_refusal(ev, chain):
    """One bounded local guard for automatic on-chain observations on every chain."""
    chain = str(chain)
    from sniper.services.memecoin_origin import bypasses_selection
    if bypasses_selection(getattr(ev, "signal_source", None)):   # no source wallet to prove
        return None
    origin = str(getattr(ev, "fomo_event_id", None) or "").lower()
    # relay_prebuy / rhprebuy: are GUARDED (2026-10-14): an unlisted source used to return None here,
    # which would have let a pre-buy through with no proof at all.
    if (getattr(ev, "signal_source", None) not in {"onchain_watch", "bsc_fast_lane", "rh_fast_lane", "slot_lane",
                                                   "relay_prebuy"}
            and not origin.startswith(("onchain:", "bscfast:", "rhfast:", "slotlane:", "rhprebuy:"))):
        return None
    proof = getattr(ev, "source_initiation", None)
    if not isinstance(proof, dict):
        return "missing_receipt_proof"
    if getattr(ev, "signal_source", None) == "relay_prebuy" or origin.startswith("rhprebuy:"):
        return _prebuy_refusal(ev, str(chain), proof, origin)
    version = proof.get("proof_version")
    if chain == 'solana' and version == PRE_EXECUTION_PROOF_VERSION:
        return _pre_execution_refusal(ev, proof)
    if chain == 'solana':
        if version not in {DIRECT_PROOF_VERSION, SOLANA_PROGRAM_PROOF_VERSION}:
            return 'wallet_initiation_unproved'
        return _solana_refusal(ev, proof)
    from sniper.settings import memecoin as cfg
    relay_versions = ((RELAY_ORIGIN_PROOF_VERSION, RELAY_BOUND_PROOF_VERSION)
                      if getattr(cfg, "MEMECOIN_RELAY_BOUND_PROOF_ACCEPT", False)
                      else (RELAY_ORIGIN_PROOF_VERSION,))
    # Option C: only the 4663 fast lane's own events, only with MEMECOIN_RH_FAST_DEPOSIT_PROOF on.
    deposit_lane = (version == RELAY_DEPOSIT_PROOF_VERSION and chain == "4663"
                    and getattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_PROOF", False)
                    and origin.startswith("rhfast:4663:"))
    # Receipt-backed watcher proofs retain their identity when handed to the BSC V2 lane.
    # Only the 4663 processed lane above may omit the receipt hash and acquired amount.
    deposit_onchain = (version == RELAY_DEPOSIT_PROOF_VERSION
                       and chain in getattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", ())
                       and (origin.startswith("onchain:%s:" % chain)
                            or (chain == "56" and origin.startswith("bscfast:56:"))
                            or (chain in _NATIVE_LANE_CHAINS and origin.startswith("rhfast:%s:" % chain)
                                and _hex(proof.get('block_hash'), _HASH)
                                and _quantity(proof.get('acquired_atomic')))))
    deposit = deposit_lane or deposit_onchain
    if version not in (DIRECT_PROOF_VERSION, *relay_versions) and not deposit:
        if str(version or "").startswith("relay-"):
            return relay_refusal_reason(getattr(ev, "relay_origin_error", None))
        return "wallet_initiation_unproved"
    wallet = _hex(getattr(ev, "source_wallet", None), _ADDRESS)
    mint = _hex(getattr(ev, "mint", None), _ADDRESS)
    tx_hash = _hex(proof.get("tx_hash"), _HASH)
    block = _quantity(proof.get("block_number"))
    uid = getattr(ev, "source_user_id", None)
    if (type(proof.get("schema_version")) is not int or proof["schema_version"] != 1
            or proof.get("network_id") != chain or not wallet or not mint or not tx_hash
            or (not deposit_lane and not _hex(proof.get("block_hash"), _HASH)) or not block
            or _quantity(getattr(ev, "source_block_number", None)) != block
            or not isinstance(uid, str) or not uid or proof.get("source_user_id") != uid
            or _hex(proof.get("mint"), _ADDRESS) != mint
            or _hex(proof.get("beneficiary"), _ADDRESS) != wallet
            or (version == DIRECT_PROOF_VERSION and _hex(proof.get("payer"), _ADDRESS) != wallet)
            or origin not in ({f"onchain:{chain}:{tx_hash}:{mint}"}
                              | ({f"bscfast:56:{tx_hash}:{mint}"} if chain == '56' else set())
                              | ({f"rhfast:{chain}:{tx_hash}:{mint}"} if chain in _NATIVE_LANE_CHAINS
                                 else set()))):
        return "receipt_identity_mismatch"
    if version in relay_versions or deposit:
        origin_wallet = proof.get("origin_wallet")
        origin_tx = proof.get("origin_tx")
        solana = proof.get("origin_network_id") == "solana"
        if deposit and not solana:
            return "receipt_identity_mismatch"
        origin_chain = proof.get("origin_network_id")
        origin_valid = (isinstance(origin_wallet, str) and 32 <= len(origin_wallet) <= 44
                        and isinstance(origin_tx, str) and 64 <= len(origin_tx) <= 88) if solana else (
                            isinstance(origin_chain, str) and origin_chain.isdigit() and int(origin_chain) > 0
                            and _hex(origin_wallet, _ADDRESS) == wallet and _hex(origin_tx, _HASH))
        if (not origin_valid
                or not _quantity(proof.get("origin_slot"))
                or not _quantity(proof.get("origin_amount_atomic"))
                or (not deposit_lane and not _quantity(proof.get("acquired_atomic")))
                or not _hex(proof.get("order_id"), _HASH)
                or proof.get("origin_identity_user_id") != uid
                or not _hex(proof.get("payer"), _ADDRESS)):
            return "receipt_identity_mismatch"
    return None


# The chains a Relay pre-buy may run on, each behind its own knob (4663, then BSC 56 on 2026-10-14).
PREBUY_CHAIN_KNOBS = {"4663": "MEMECOIN_RH_RELAY_PREBUY", "56": "MEMECOIN_BSC_RELAY_PREBUY"}


def _prebuy_refusal(ev, chain, proof, origin):
    """A Relay pre-buy's proof (relay-request-prebuy-v1), only on a pre-buy chain with its knob on: the
    signal source AND the origin say pre-buy on THIS chain, the deposit is the user's own (origin identity
    == uid), and the request binds that deposit to this user's wallet, this mint, this chain and the
    deposit's USDC."""
    from sniper.settings import memecoin as cfg
    if (chain not in PREBUY_CHAIN_KNOBS or not getattr(cfg, PREBUY_CHAIN_KNOBS[chain], False)
            or proof.get("proof_version") != RELAY_PREBUY_PROOF_VERSION):
        return "wallet_initiation_unproved"
    wallet = _hex(getattr(ev, "source_wallet", None), _ADDRESS)
    mint = _hex(getattr(ev, "mint", None), _ADDRESS)
    order = _hex(proof.get("order_id"), _HASH)
    uid = getattr(ev, "source_user_id", None)
    origin_wallet, origin_tx = proof.get("origin_wallet"), proof.get("origin_tx")
    amount = _quantity(proof.get("origin_amount_atomic"))
    if (getattr(ev, "signal_source", None) != "relay_prebuy" or not wallet or not mint or not order
            or origin != "rhprebuy:%s:%s:%s" % (chain, order, mint)
            or type(proof.get("schema_version")) is not int or proof["schema_version"] != 1
            or proof.get("network_id") != chain or proof.get("origin_network_id") != "solana"
            or not isinstance(uid, str) or not uid or proof.get("source_user_id") != uid
            or proof.get("origin_identity_user_id") != uid
            or _hex(proof.get("mint"), _ADDRESS) != mint
            or _hex(proof.get("beneficiary"), _ADDRESS) != wallet
            or _hex(proof.get("request_recipient"), _ADDRESS) != wallet
            or proof.get("request_out_chain") != int(chain) or not amount
            or _quantity(proof.get("request_in_amount")) != amount
            or not (isinstance(origin_wallet, str) and 32 <= len(origin_wallet) <= 44)
            or not (isinstance(origin_tx, str) and 64 <= len(origin_tx) <= 88)
            or not _quantity(proof.get("origin_slot")) or not proof.get("request_id")):
        return "receipt_identity_mismatch"
    return None


def bsc_payload_initiation_refusal(payload):
    """Check the same identity before a cold fast-lane signal schedules any provider work."""
    shared = payload.get("route_kind") in {"v3_shared", "flap_shared"}
    mint, tx_hash = str(payload.get("mint") or "").lower(), str(payload.get("tx_hash") or "").lower()
    ev = SimpleNamespace(signal_source="onchain_watch" if shared else "bsc_fast_lane",
                         fomo_event_id=f"{'onchain' if shared else 'bscfast'}:56:{tx_hash}:{mint}",
                         mint=mint, source_user_id=payload.get("source_user_id"),
                         source_wallet=payload.get("wallet"), source_block_number=payload.get("seq"),
                         source_initiation=payload.get("source_initiation"))
    return source_initiation_refusal(ev, "56")
