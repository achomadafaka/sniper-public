"""Verify Axiom address claims against independent follow-activity events.

Three distinct mint matches with no contradiction confirm a candidate.
Incomplete windows and unsupported payment paths remain unknown.
The watcher reads only current proofs and still applies its own wallet authority filter.
"""
import json
import re
import sqlite3
import logging
import time
import urllib.request

from sniper.services.memecoin_chains import canonical_chain_id
from sniper.services.memecoin_evm_onchain_watch import (
    TRANSFER_TOPIC, _integer, _topic_address, wallet_paid_token_flows, RELAY_KYBER_PROOF_VERSION,
)

logger = logging.getLogger(__name__)

BOUGHT, NO_BUY, NO_DATA = "bought", "no_buy", "no_data"
# A positive token delta with no quote-asset spend in the same transaction is someone SENDING the
# coin to the address. It is neither identity evidence nor a contradiction (Codex review, 2026-09-16).
TRANSFER = "transfer"
CONFIRMED, REFUTED, UNVERIFIED = "axiom_confirmed", "REFUTED", "unverified"
SHARED = "shared_executor"          # one address, two named traders: never an identity
METHOD = "axiom_directory"
PROOF_VERSION = "wallet-paid-v2"
WINDOW_S = 120
ANCHOR_MARGIN_S = 180        # around our own fill, both sides
MIN_MATCHES = 3
_RANK = {UNVERIFIED: 0, SHARED: 1, REFUTED: 2, CONFIRMED: 3}

SOLANA_RPC = "https://api.mainnet-beta.solana.com"
SOL_MINT = "So11111111111111111111111111111111111111112"          # wrapped SOL
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
QUOTE_MINTS = (SOL_MINT, USDC_MINT)
MAX_RECEIPT_READS = 3            # candidate transactions verified per EVM read
MAX_TX_READS = 8                 # candidate transactions verified per Solana read
# 56 is NOT rpc.48.club (2026-09-29): it answers `result: null`, no error, for receipts older than
# ~7 days (46 of 46 on 09-12..09-22; same head as ninicoin, so pruning, not lag), which reads as
# no_data on every proof older than a week.
EVM_RPC = {"4663": "https://rpc.mainnet.chain.robinhood.com", "56": "https://bsc-dataseed1.ninicoin.io",
           "8453": "https://mainnet.base.org",
           # Ethereum mainnet (2026-09-30, spec eth-mainnet-funded-design): served 19-day-old and
           # fresh J7 receipts. Not publicnode (its per-IP budget is shared with the funded
           # 4663/8453/5042 read pools) and not drpc (the exact-capture lane's host).
           "1": "https://eth-mainnet.public.blastapi.io"}
# Axiom keeps ONE generic EVM address per trader (Alpha, 2026-09-16), so a chain='evm' row is a
# claim about every EVM chain at once and must be tested on each one separately: the address can
# be the trader on one chain and unproven on another.
GENERIC_EVM = "evm"
EVM_CHAINS = ("4663", "56", "8453", "1")
# A chain whose funded custody URL may not exist yet: its read host is proved non-custody against the
# other chains' custody URLs and every pool and submission endpoint (j7_read_url).
PRE_CUSTODY_CHAINS = ("57073", "1")
LEGACY_CONFIRMED = "fomoscan_confirmed"
# J7 TRANSFER PROOF (2026-09-27, Alpha GO). A wallet's own J7 buy event names its EVM address,
# `swap:raybot:<tx>:<address>:<mint>`, and the buy's receipt shows the coin arriving there. The
# transaction is sent by a relayer (BSC: contract 0xccc88a9d...), never by the wallet, so the proof is
# Transfer(to = address), not the sender.
J7_PROOF_VERSION = "j7-transfer-v1"
J7_MIN_RECEIPTS = 2              # distinct buy receipts of the same armed wallet
J7_MAX_RECEIPTS = 4              # receipts read per wallet per run
# J7 CROSS-CHAIN PROOF (X1, 2026-09-29, Alpha GO). An EVM address holds across chains for one owner
# (an EOA shares its key; a same-address AA wallet shares deployer and salt): for every single-buy
# unproven armed wallet with a proof elsewhere, the address J7 named equalled the proven one, 14 of
# 14. So W is proved on chain B for A when W's J7 buys on B name only A, W holds a current DIRECT
# proof of A on another EVM chain, no other wallet holds or claims A, and no receipt read on B
# contradicts it. The basis is re-checked whenever the proof is read (`_current_proofs`), and a
# cross-chain proof is never the basis of another, so a chain of them cannot hold itself up.
# The address on B must also be an account a key controls (Gamma LOW-1, Alpha 2026-09-29):
# eth_getCode at latest is empty or an EIP-7702 designator. Any other code is a contract, which can
# sit at the same address on B under another controller (the CREATE-nonce collision class); an
# unreadable answer proves nothing. All 14 first cross-chain addresses read a 7702 designator.
J7_CROSSCHAIN_PROOF_VERSION = "j7-crosschain-v1"
# EVM KEY PROOF, INK ONLY (Alpha GO 2026-09-29). J7 reports no Ink (57073) trades, so no Ink address
# can be proven from J7 receipts. An address a key controls is one account on every EVM chain, so W
# is proved on Ink for A when W holds a current DIRECT proof of A on another EVM chain, A reads as a
# key-controlled account on Ink (eth_getCode empty or an EIP-7702 designator), and no other wallet
# holds A anywhere. Proof only; arming stays the operator's. Like a cross-chain proof it is re-checked
# against its basis whenever it is read, and it is never the basis of another proof.
EVM_KEY_PROOF_VERSION = "evm-key-v1"
SOLANA_RELAY_KEY_PROOF_VERSION = "solana-relay-key-v1"
EVM_KEY_CHAINS = ("57073",)
# code_fn's answer for a chain it has NO read host for (Gamma M1, 2026-10-02): told apart from a
# read that failed (None), which is unknown and never skipped.
NO_HOST = object()
_DEPENDENT_PROOFS = (J7_CROSSCHAIN_PROOF_VERSION, EVM_KEY_PROOF_VERSION)


def evm_key_chains():
    """Ink, plus the J7 chains listed in MEMECOIN_EVM_KEY_PROOF_CHAINS (default none)."""
    from sniper.settings import memecoin as cfg
    return EVM_KEY_CHAINS + tuple(getattr(cfg, "MEMECOIN_EVM_KEY_PROOF_CHAINS", ()) or ())
_DESIGNATOR_PREFIX, _DESIGNATOR_LEN = "0xef0100", 2 + 2 * 23      # 0xef0100 ++ 20-byte delegate


def _key_account_code(code):
    """'empty' or '7702' for an account a key controls, 'contract' for other code, None when the
    answer is missing (unknown, never a pass)."""
    if not isinstance(code, str) or not code.strip():
        return None
    code = code.strip().lower()
    if code == "0x":
        return "empty"
    if code.startswith(_DESIGNATOR_PREFIX) and len(code) == _DESIGNATOR_LEN:
        return "7702"
    return "contract"


def verify_candidates(conn, *, rpc_read_fn, now_iso, max_events=30, max_candidates=None,
                      write_results=True, chain=None):
    """Verify directory claims and explicit research links to existing wallet identities.

    With write_results=False, all database access remains read-only.
    The default write path preserves legacy proofs owned by other methods.
    """
    out = []
    written_now = {}
    lane_written_now = {}
    candidates = _candidates(conn)
    shared = _shared_addresses(conn, candidates)
    selected = [c for c in _fan_out(candidates) if chain is None or c['chain'] == chain]
    for cand in selected[:max_candidates]:
        chain = canonical_chain_id(cand["chain"])
        result = {"source_user_id": cand["source_user_id"], "handle": cand["handle"],
                  "x_handle": cand["x_handle"], "address": cand["address"],
                  "chain": chain or cand["chain"], "matches": 0, "contradictions": 0,
                  "no_data": 0, "transfers": 0, "checked": 0, "unanchored": 0,
                  "distinct_mints": 0, "event_evidence": [], "proof_version": PROOF_VERSION, "verify_status": UNVERIFIED,
                  "written": False, "lane_written": False, "skip_reason": None}
        if not chain:
            result["skip_reason"] = f"unknown_chain:{cand['chain']}"
            out.append(result)
            continue
        if _address_key(chain, cand["address"]) in shared:
            # Two named traders cannot share one identity. Read nothing: no number of buys makes
            # this address one trader's, and a stale confirmation of it is retired below.
            result.update(verify_status=SHARED)
            if write_results:
                _retire_shared(conn, chain, cand["address"], now_iso=now_iso)
                _write_lane(conn, result, now_iso=now_iso, lane_written_now=lane_written_now)
                _write(conn, result, now_iso=now_iso, written_now=written_now)
            out.append(result)
            continue
        counts = {BOUGHT: 0, NO_BUY: 0, NO_DATA: 0, TRANSFER: 0}
        bought_mints = set()
        unanchored = 0
        events = _buy_events(conn, cand["source_user_id"], chain, max_events)
        if not events:
            # Nothing of this trader ever happened on this chain, so there is nothing to anchor.
            # Read nothing and write nothing: an empty sample is not a verdict about the address.
            result["skip_reason"] = "no_events_on_chain"
            out.append(result)
            continue
        for mint, at_iso, anchor in events:
            verdict = rpc_read_fn(chain, cand["address"], mint, window_s=WINDOW_S, at_iso=at_iso,
                                  anchor=anchor)
            purchase = getattr(rpc_read_fn, 'last_purchase', None)
            if purchase:
                result['proof_version'] = RELAY_KYBER_PROOF_VERSION
                if not anchor or purchase.get('mint') != mint or purchase.get('beneficiary') != cand['address'].lower():
                    verdict = NO_DATA
            if verdict not in counts:
                verdict = NO_DATA
            if anchor is None:
                unanchored += 1
                if verdict == NO_BUY:
                    # No fill of ours dates this window, so nothing proves the window found the
                    # trade. An absence there is unknown, never a contradiction.
                    verdict = NO_DATA
            result["event_evidence"].append({"mint": mint, "event_at": at_iso,
                                              "anchor": anchor, "verdict": verdict,
                                              "reason": getattr(rpc_read_fn, "last_reason", None),
                                              "purchase": purchase})
            counts[verdict] += 1
            if verdict == BOUGHT:
                bought_mints.add(mint)
        result.update(matches=counts[BOUGHT], contradictions=counts[NO_BUY],
                      no_data=counts[NO_DATA], transfers=counts[TRANSFER],
                      checked=sum(counts.values()), unanchored=unanchored,
                      distinct_mints=len(bought_mints),
                      verify_status=_verdict(counts, bought_mints))
        result['code_basis'] = _code_basis(result['event_evidence'])
        result['report_status'] = _report_status(result['verify_status'], result['code_basis'])
        if (result['proof_version'] == RELAY_KYBER_PROOF_VERSION
                and result['verify_status'] == CONFIRMED
                and len(_linked_mints(result, cand['address'])) < MIN_MATCHES):
            result['verify_status'] = UNVERIFIED
        if write_results:
            _write_lane(conn, result, now_iso=now_iso, lane_written_now=lane_written_now)
            _write(conn, result, now_iso=now_iso, written_now=written_now)
        out.append(result)
    return out


def _current_proofs(conn):
    """Exclude old confirmations that used unsupported receipt rules. Cross-chain proofs come last:
    each is current only while its owner holds a direct proof of the same address on another EVM
    chain, read from the rows yielded before it."""
    direct = []
    crosschain = []
    for row in _proof_rows(conn):
        if row[5] in _DEPENDENT_PROOFS:
            crosschain.append(row)
        else:
            direct.append(row)
            yield row[:5]
    basis = _direct_basis(direct)
    for row in crosschain:
        if _crosschain_basis(basis, row[2], row[1]):
            yield row[:5]


def _proof_version(detail):
    try:
        return (json.loads(detail or "{}") or {}).get("proof_version")
    except (TypeError, ValueError, AttributeError):
        return None


def _direct_basis(rows):
    """{(source_user_id, address): {chain: proof_version}} of current direct EVM proofs."""
    out = {}
    for row in rows:
        chain = canonical_chain_id(row[0]) or str(row[0])
        if chain in EVM_CHAINS:
            out.setdefault((str(row[2]), str(row[1]).lower()), {})[chain] = row[5]
    return out


def _crosschain_basis(basis, uid, address):
    """[{chain, proof_version}] of the owner's direct proofs of `address` on OTHER EVM chains. One
    row per (wallet, chain) means a cross-chain row never meets a direct one on its own chain, and
    `verify_j7_addresses` keeps a current proof here before it looks for a basis."""
    found = basis.get((str(uid), str(address).lower())) or {}
    return [{"chain": c, "proof_version": v} for c, v in sorted(found.items())]


def _proof_rows(conn):
    """(network_id, address, source_user_id, handle, detail, proof_version) of every CONFIRMED row
    that meets its own proof rule; a cross-chain row still needs its basis (`_current_proofs`)."""
    legacy = {_address_key(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT network_id,address,source_user_id FROM memecoin_wallet_addresses WHERE verify_status=?",
        (LEGACY_CONFIRMED,))}
    origin_identities = {}
    for row in conn.execute(
            "SELECT network_id, address, source_user_id, handle, detail "
            "FROM axiom_verified_addresses WHERE verify_status=?", (CONFIRMED,)):
        owner = legacy.get(_address_key(row[0], row[1]))
        if owner is not None and owner != row[2]:
            continue
        try:
            detail = json.loads(row[4] or "{}")
        except (TypeError, ValueError):
            continue
        version = detail.get("proof_version") if isinstance(detail, dict) else None
        if version == PROOF_VERSION:
            yield (*row, version)
        elif (version == J7_PROOF_VERSION and detail.get("contradictions") == 0
              and len(_j7_bought_txs(detail)) >= J7_MIN_RECEIPTS):
            yield (*row, version)
        elif version == J7_CROSSCHAIN_PROOF_VERSION and detail.get("contradictions") == 0:
            yield (*row, version)
        elif (version == EVM_KEY_PROOF_VERSION and str(row[0]) in evm_key_chains()
              and detail.get("contradictions") == 0):
            yield (*row, version)
        elif version == SOLANA_RELAY_KEY_PROOF_VERSION:
            if _solana_relay_key_valid(conn, row, detail, origin_identities):
                yield (*row, version)
        elif version == RELAY_KYBER_PROOF_VERSION and row[0] == '4663':
            if len(_linked_mints(detail, row[1])) >= MIN_MATCHES and detail.get('contradictions') == 0:
                yield (*row, version)


def _solana_relay_key_valid(conn, row, detail, origin_identities):
    """Identity enrollment from audited signed orders, never a live source-initiation proof.

    The same recipient must be a key account on the target chain and have at least three
    distinct, paid order/receipt bindings from the still-current verified Solana identity.
    Provider verification runs during enrollment; this reader performs no network work.
    """
    from sniper.persistence import db_memecoin as m
    try:
        chain, address, uid = str(row[0]), str(row[1]).lower(), str(row[2])
        if (not chain.isdigit() or int(chain) <= 0 or detail.get('contradictions') != 0
                or detail['chain'] != chain or detail['source_user_id'] != uid
                or detail['beneficiary'] != address or not _is_hex(address, 40)
                or _key_account_code(detail.get('chain_code')) not in ('empty', '7702')):
            return False
        orders = detail['orders']
        if not isinstance(orders, list) or len(orders) < MIN_MATCHES:
            return False
        coins, transactions, deposits, commitments = set(), set(), set(), set()
        for order in orders:
            if (not str(order['chain']).isdigit() or int(order['chain']) <= 0
                    or not _is_hex(order['mint'], 40) or not _is_hex(order['tx'], 64)
                    or not _is_hex(order['order_id'], 64)
                    or not isinstance(order['origin'], str) or not 64 <= len(order['origin']) <= 88
                    or int(order['acquired_atomic']) <= 0 or int(order['usdc_deposit_atomic']) <= 0):
                return False
            coins.add((str(order['chain']), order['mint'].lower()))
            transactions.add(order['tx'].lower()); deposits.add(order['origin']); commitments.add(order['order_id'].lower())
        if (len(coins) < MIN_MATCHES or min(len(transactions), len(deposits), len(commitments)) != len(orders)):
            return False
        key = (uid, detail['origin_wallet'], detail['origin_status_at'], address)
        if key not in origin_identities:
            origin_identities[key] = m.current_verified_relay_identity(conn, *key)
        return origin_identities[key]
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _linked_mints(detail, address):
    """Count only anchored, linked purchases for this beneficiary."""
    mints = set()
    events = detail.get('event_evidence') or []
    if not isinstance(events, list):
        return mints
    for event in events:
        if not isinstance(event, dict):
            continue
        purchase, anchor = event.get('purchase'), event.get('anchor')
        if not isinstance(purchase, dict) or not isinstance(anchor, dict):
            continue
        if (event.get('verdict') == BOUGHT and anchor.get('tx_sig')
                and purchase.get('proof_version') == RELAY_KYBER_PROOF_VERSION
                and purchase.get('mint') == event.get('mint')
                and purchase.get('beneficiary') == address.lower()
                and isinstance(event.get('mint'), str)):
            mints.add(event['mint'])
    return mints


def _j7_bought_txs(detail):
    """Distinct transactions whose receipt paid the coin to the address, from a stored J7 proof."""
    events = detail.get("event_evidence") if isinstance(detail, dict) else None
    return {e["tx"] for e in (events if isinstance(events, list) else ())
            if isinstance(e, dict) and e.get("verdict") == BOUGHT and e.get("tx")}


def _is_hex(text, n):
    text = str(text or "")
    if len(text) != n + 2 or not text.startswith("0x"):
        return False
    try:
        int(text[2:], 16)
    except ValueError:
        return False
    return True


def _j7_quote_assets(chain):
    """The chain's quote and stable tokens. A J7 'buy' of one of them is a swap INTO the quote asset
    (the wallet SENDS its coin): 0xLingard's BSC 'buy' of USDC, 2026-09-27. Not a coin buy, so not
    evidence for or against the address."""
    from sniper.execution.evm_wallet import CHAIN_QUOTE, CHAIN_STABLE, CHAIN_USDC
    chain = str(chain)
    return {str(t).lower() for t in ((CHAIN_QUOTE.get(chain) or {}).get("token"),
                                      CHAIN_STABLE.get(chain), CHAIN_USDC.get(chain)) if t}


def _j7_claims(conn, chain):
    """[(source_user_id, handle, tx, address, mint)] from the J7 raybot BUY events on `chain`,
    newest first. A range on the unique fomo_event_id index; EVM ids only (0x transactions).
    A buy of the chain's quote or stable token is left out (_j7_quote_assets)."""
    quote_assets = _j7_quote_assets(chain)
    out = []
    for uid, handle, fid in conn.execute(
            "SELECT source_user_id, source_handle, fomo_event_id FROM memecoin_events "
            "WHERE fomo_event_id >= 'swap:raybot:0x' AND fomo_event_id < 'swap:raybot:0y' "
            "AND network_id=? AND side='buy' ORDER BY created_at DESC, id DESC", (str(chain),)):
        parts = str(fid).split(":")
        if len(parts) != 5:
            continue
        tx, address, mint = parts[2].lower(), parts[3].lower(), parts[4].lower()
        if (_is_hex(tx, 64) and _is_hex(address, 40) and _is_hex(mint, 40) and uid
                and mint not in quote_assets):
            out.append((str(uid), handle, tx, address, mint))
    return out


def _j7_receipt_verdict(receipt, address, mint):
    """BOUGHT when the receipt moves `mint` to `address`; NO_BUY when a successful receipt does not;
    NO_DATA when there is no successful receipt to read."""
    if not isinstance(receipt, dict) or str(receipt.get("status") or "").lower() not in {
            "1", "0x1", "true"}:
        return NO_DATA
    for log in receipt.get("logs") or []:
        topics = log.get("topics") or []
        if (len(topics) == 3 and str(topics[0]).lower() == TRANSFER_TOPIC
                and str(log.get("address") or "").lower() == mint
                and _topic_address(topics[2]) == address and _integer(log.get("data")) > 0):
            return BOUGHT
    return NO_BUY


def verify_j7_addresses(conn, *, receipt_fn, chain, armed, max_receipts=J7_MAX_RECEIPTS,
                        code_fn=None):
    """Verify each armed wallet's J7-named address on one EVM chain. Read-only: the caller writes
    the results through save_verification_results, as for verify_candidates.

    CONFIRMED: at least J7_MIN_RECEIPTS distinct buy receipts show Transfer(to = address) of the
    coin the event names, and none contradicts it. REFUTED: a successful receipt that does not pay
    the coin to the address. SHARED: another wallet's events name the same address, or another
    wallet already holds a current proof of it -- neither wallet is proved. Skipped: a wallet whose
    events name more than one address, or whose (wallet, chain) row already holds a CONFIRMED proof
    of a different address. `armed` is the chain's armed source_user_ids (the caller reads them).

    Short of 2 paid receipts, the wallet is CONFIRMED by J7_CROSSCHAIN_PROOF_VERSION when it holds a
    current direct proof of the address on another EVM chain, no other wallet holds it anywhere,
    and `code_fn(chain, address)` reads it as a key-controlled account (empty or a 7702 designator).
    Without `code_fn` nothing is confirmed cross-chain.
    A result that only failed to confirm (no contradiction) never overwrites the wallet's current
    proof of the same address, nor its CONFIRMED row when no cross-chain proof can take its place:
    an unreadable receipt is unknown, not evidence (skip_reason `keeps_current_proof`). A
    contradiction or a shared address still replaces it.
    """
    chain = str(chain)
    by_uid, handles, claimants = {}, {}, {}
    for uid, handle, tx, address, mint in _j7_claims(conn, chain):
        by_uid.setdefault(uid, {}).setdefault(address, []).append((tx, mint))
        handles.setdefault(uid, handle)
        claimants.setdefault(address, set()).add(uid)
    owners = watcher_address_owners(conn)
    holders, held_here = {}, {}
    for (held_chain, held), (holder, _handle) in owners.items():
        holders.setdefault(str(held).lower(), set()).add(str(holder))
        if str(held_chain) == chain:
            held_here[str(held).lower()] = str(holder)
    basis = _direct_basis([row for row in _proof_rows(conn) if row[5] not in _DEPENDENT_PROOFS])
    current = {(str(r[0]), str(r[1])): (str(r[2]).lower(), r[3], _proof_version(r[4])) for r in conn.execute(
        "SELECT source_user_id, network_id, address, verify_status, detail FROM axiom_verified_addresses")}
    out = []
    for uid in sorted({str(u) for u in armed} & set(by_uid)):
        addresses = by_uid[uid]
        result = {"source_user_id": uid, "handle": handles.get(uid), "x_handle": handles.get(uid),
                  "address": None, "chain": chain, "matches": 0, "contradictions": 0,
                  "no_data": 0, "transfers": 0, "checked": 0, "unanchored": 0,
                  "distinct_mints": 0, "event_evidence": [], "proof_version": J7_PROOF_VERSION,
                  "verify_status": UNVERIFIED, "written": False, "lane_written": False,
                  "skip_reason": None}
        out.append(result)
        if len(addresses) != 1:
            result["skip_reason"] = "j7_multiple_addresses"
            continue
        (address, buys), = addresses.items()
        result["address"] = address
        prior = current.get((uid, chain))
        # An evm-key prior is a DEPENDENT proof from another chain: the chain's own J7 receipts for
        # another address are read, never skipped (MEMECOIN_EVM_KEY_PROOF_CHAINS, 2026-10-01).
        if prior and prior[1] == CONFIRMED and prior[0] != address and prior[2] != EVM_KEY_PROOF_VERSION:
            result["skip_reason"] = "existing_confirmed_other_address"
            continue
        owner = owners.get((chain, address))
        if len(claimants[address]) > 1 or (owner is not None and str(owner[0]) != uid):
            result["verify_status"] = SHARED
            continue
        counts = {BOUGHT: 0, NO_BUY: 0, NO_DATA: 0}
        read, paid = set(), set()
        for tx, mint in buys:
            if tx in read:
                continue
            if len(read) >= max_receipts:
                break
            read.add(tx)
            try:
                receipt = receipt_fn(chain, tx)
            except Exception:
                logger.exception("j7 verify: receipt read failed chain=%s tx=%s", chain, tx)
                receipt = None
            verdict = _j7_receipt_verdict(receipt, address, mint)
            counts[verdict] += 1
            if verdict == BOUGHT:
                paid.add((tx, mint))
            result["event_evidence"].append({"tx": tx, "mint": mint, "verdict": verdict})
        result.update(matches=counts[BOUGHT], contradictions=counts[NO_BUY],
                      no_data=counts[NO_DATA], checked=sum(counts.values()),
                      distinct_mints=len({mint for _tx, mint in paid}),
                      verify_status=(REFUTED if counts[NO_BUY] else CONFIRMED
                                     if len(paid) >= J7_MIN_RECEIPTS else UNVERIFIED))
        if result["verify_status"] == UNVERIFIED:
            elsewhere = _crosschain_basis(basis, uid, address)
            if held_here.get(address) == uid or (
                    prior and prior[1] == CONFIRMED and prior[0] == address and not elsewhere):
                # The wallet's current proof of this address here stays as it is: a weaker
                # cross-chain proof never replaces a direct one.
                result["skip_reason"] = "keeps_current_proof"
            elif elsewhere and holders.get(address, set()) <= {uid} and code_fn is not None:
                try:
                    kind = _key_account_code(code_fn(chain, address))
                except Exception:
                    logger.exception("j7 verify: code read failed chain=%s address=%s", chain, address)
                    kind = None
                result["chain_code"] = kind or "unknown"
                if kind in ("empty", "7702"):
                    result.update(verify_status=CONFIRMED, proof_version=J7_CROSSCHAIN_PROOF_VERSION,
                                  crosschain_basis=elsewhere)
    return out


def evm_receipt_reader(*, call_fn=None, urls=None):
    """`receipt_fn(chain, tx)` over each chain's own public RPC (EVM_RPC; `urls` overrides one),
    eth_getTransactionReceipt only. None when the read fails or returns nothing."""
    call = call_fn or _http_call
    endpoints = dict(EVM_RPC, **(urls or {}))

    def read(chain, tx):
        url = endpoints.get(str(chain))
        if not url:
            return None
        result, error = call(url, "eth_getTransactionReceipt", [tx])
        return result if error is None and isinstance(result, dict) else None
    return read


def verify_evm_key_addresses(conn, *, chain, code_fn):
    """evm-key-v1 results for `chain` (EVM_KEY_CHAINS only): one per wallet whose direct proofs on the
    other EVM chains name exactly one address, that address held by no other wallet anywhere.
    CONFIRMED when code_fn(chain, address) reads a key-controlled account (empty or a 7702
    designator) and so does at least one basis chain, with no read chain showing a contract;
    REFUTED for other code (a contract at the address on this chain); UNVERIFIED when the code is
    unreadable, and then a current proof of the same address is kept, never demoted. A basis chain
    code_fn has no host for (it answers NO_HOST: 56 today) is recorded 'unread' in basis_code and
    skipped; a failed read (None) is 'failed' and blocks the proof.
    Read-only: the caller writes through save_verification_results."""
    chain = str(chain)
    if chain not in evm_key_chains():
        raise ValueError("evm-key-v1 is Ink-only unless MEMECOIN_EVM_KEY_PROOF_CHAINS lists %s" % chain)
    direct = {}
    for net, address, uid, handle, _detail, version in _proof_rows(conn):
        net = canonical_chain_id(net) or str(net)
        # The chain being proven is never its own basis: a proof there is the chain's own evidence.
        if version in _DEPENDENT_PROOFS or net not in EVM_CHAINS or net == chain:
            continue
        direct.setdefault(str(uid), {}).setdefault(str(address).lower(), {})[net] = (version, handle)
    owners = watcher_address_owners(conn)
    holders = {}
    for (_net, held), (holder, _handle) in owners.items():
        holders.setdefault(str(held).lower(), set()).add(str(holder))
    held_here = {str(a).lower(): str(u) for (net, a), (u, _h) in owners.items() if str(net) == chain}
    # The wallet's OWN row on this chain (any status but unverified, any version but evm-key) stands:
    # a J7 chain's direct proof, refutation or shared verdict is never replaced by a dependent proof.
    own_rows = set()
    for uid, net, status, detail in conn.execute(
            "SELECT source_user_id, network_id, verify_status, detail FROM axiom_verified_addresses "
            "WHERE network_id=?", (chain,)):
        try:
            version = (json.loads(detail or "{}") or {}).get("proof_version")
        except (TypeError, ValueError, AttributeError):
            version = None
        if status != UNVERIFIED and version != EVM_KEY_PROOF_VERSION:
            own_rows.add(str(uid))
    out = []
    for uid in sorted(direct):
        addresses = direct[uid]
        (address, basis), *more = sorted(addresses.items())
        handle = next(iter(basis.values()))[1]
        result = {"source_user_id": uid, "handle": handle, "x_handle": handle, "address": address,
                  "chain": chain, "matches": 0, "contradictions": 0, "no_data": 0, "transfers": 0,
                  "checked": 0, "unanchored": 0, "distinct_mints": 0, "event_evidence": [],
                  "proof_version": EVM_KEY_PROOF_VERSION, "verify_status": UNVERIFIED,
                  "written": False, "lane_written": False, "skip_reason": None,
                  "crosschain_basis": [{"chain": c, "proof_version": v}
                                       for c, (v, _h) in sorted(basis.items())]}
        if more:
            result["skip_reason"] = "evm_key_multiple_addresses"
            out.append(result)
            continue
        if uid in own_rows:
            result["skip_reason"] = "keeps_chain_row"
            out.append(result)
            continue
        if not holders.get(address, set()) <= {uid}:
            continue                       # another wallet holds it: no basis, nothing to write
        try:
            kind = _key_account_code(code_fn(chain, address))
        except Exception:
            logger.exception("evm-key verify: code read failed chain=%s address=%s", chain, address)
            kind = None
        result["chain_code"] = kind or "unknown"
        result["checked"] = 1
        # THE BASIS ADDRESS MUST BE A KEY ACCOUNT TOO (Gamma LOW-1, 2026-09-29). An empty Ink address
        # only says "nothing deployed there yet": if A is a contract wallet on the basis chain,
        # whoever deploys it on Ink need not be W. One code read per basis chain; unknown is no proof.
        basis_code = {}
        for basis_chain in sorted(basis):
            try:
                raw = code_fn(basis_chain, address)
            except Exception:
                logger.exception("evm-key verify: basis code read failed chain=%s address=%s",
                                 basis_chain, address)
                raw = None
            basis_code[basis_chain] = "unread" if raw is NO_HOST else (_key_account_code(raw) or "failed")
        result["basis_code"] = basis_code
        # ONE KEY READ ON A PROVEN CHAIN IS ENOUGH (Alpha 2026-10-02; narrows Gamma LOW-1 of
        # 2026-09-29, which read EVERY basis chain). An EOA address is derived from its key, so when
        # A reads empty or as a 7702 designator on a chain where W is directly proven to use it,
        # and the same on this chain, a key controls A here too. A contract on ANY chain read still
        # fails it: then A may be a contract wallet whose deployer differs per chain. A basis chain
        # with no research read host (rule 0724bc08: 56 has none; code_fn answers NO_HOST) is
        # skipped as 'unread', never counted as a pass, and is recorded so the proof can be
        # re-checked once a host exists. A read that FAILED on a hosted chain (timeout, error, an
        # exhausted budget) is 'failed': unknown, so no proof this run (Gamma M1).
        read = [k for k in basis_code.values() if k != "unread"]
        basis_ok = bool(read) and all(k in ("empty", "7702") for k in read)
        if kind in ("empty", "7702") and basis_ok:
            result.update(verify_status=CONFIRMED, matches=1)
        elif kind == "contract":
            result.update(verify_status=REFUTED, contradictions=1)
        elif held_here.get(address) == uid:
            result["skip_reason"] = "keeps_current_proof"
        else:
            result["no_data"] = 1
        out.append(result)
    return out


def evm_code_reader(*, call_fn=None, urls=None):
    """`code_fn(chain, address)`: eth_getCode at latest on the same endpoints as
    `evm_receipt_reader`. None when the read fails or answers nothing."""
    call = call_fn or _http_call
    endpoints = dict(EVM_RPC, **(urls or {}))

    def read(chain, address):
        url = endpoints.get(str(chain))
        if not url:
            return None
        result, error = call(url, "eth_getCode", [address, "latest"])
        return result if error is None and isinstance(result, str) else None
    return read


def confirmed_addresses(conn):
    """Return addresses with current payment proof."""
    return {(r[0], r[1]): r[3] for r in _current_proofs(conn)}


def watcher_address_book(conn):
    """Return the handle for each verified chain and address."""
    return {key: owner[1] for key, owner in watcher_address_owners(conn).items()}


_EVM_ROSTER_ID = re.compile(r"^evm:(0x[0-9a-f]{40})$")


def evm_roster_address(uid):
    """The address an `evm:<0x lowercase address>` roster id names, else None (2026-10-07). The id IS
    the wallet, as `pump:<address>` is on Solana: proof is our own detection of its transactions."""
    hit = _EVM_ROSTER_ID.match(str(uid or ""))
    return hit.group(1) if hit else None


def watcher_address_owners(conn):
    """Return the trader identity for each address with current proof, plus every `evm:` roster
    id's own address on each EVM chain in MEMECOIN_EVM_ALLOWLIST. A proof row owning the same
    address keeps it (one trader, one watched identity)."""
    owners = {(r[0], r[1]): (r[2], r[3]) for r in conn.execute(
        "SELECT network_id, address, source_user_id, handle FROM memecoin_wallet_addresses "
        "WHERE verify_status=?", (LEGACY_CONFIRMED,))}
    owners.update({(r[0], r[1]): (r[2], r[3]) for r in _current_proofs(conn)})
    from sniper.settings import memecoin as cfg
    chains = sorted(str(c) for c in (getattr(cfg, "MEMECOIN_EVM_ALLOWLIST", None) or ()))
    try:
        roster = conn.execute(
            "SELECT source_user_id, handle FROM memecoin_wallets WHERE source_user_id LIKE 'evm:%'"
        ).fetchall()
    except sqlite3.OperationalError:                 # no roster table: no evm: ids to add
        roster = []
    for uid, handle in roster:
        address = evm_roster_address(uid)
        if address:
            for chain in chains:
                owners.setdefault((chain, address), (uid, handle))
    return owners


OPERATOR_RECEIPT_IDENTITY = "operator_receipt_identity"


def _operator_registry_owners(conn, chain, existing):
    """Explicit operator attestations for one configured chain; background roster reads only."""
    from sniper.settings import memecoin as cfg
    if canonical_chain_id(chain) != chain or chain not in cfg.MEMECOIN_EVM_ALLOWLIST:
        return {}
    try:
        rows = conn.execute("""
            SELECT r.source_user_id,r.address,r.handle_seen,r.evidence_json,e.actor
            FROM memecoin_address_registry r
            JOIN memecoin_address_registry_events e
              ON e.source_user_id=r.source_user_id AND e.network_id=r.network_id
             AND e.address=r.address AND e.at=r.status_at
             AND e.evidence_kind=r.evidence_kind AND e.evidence_json=r.evidence_json
            WHERE r.network_id=? AND r.status='verified' AND r.conflict_at IS NULL
              AND r.evidence_kind=? AND e.to_status='verified'
              AND substr(e.actor,1,9)='operator:' AND length(trim(substr(e.actor,10)))>0
              AND NOT EXISTS (
                SELECT 1 FROM memecoin_address_registry c
                WHERE c.network_id=r.network_id AND lower(c.address)=lower(r.address)
                  AND (c.source_user_id IS NOT r.source_user_id OR c.status='refuted'
                       OR c.conflict_at IS NOT NULL))
              AND NOT EXISTS (
                SELECT 1 FROM axiom_verified_addresses a
                WHERE a.network_id=r.network_id AND lower(a.address)=lower(r.address)
                  AND (a.source_user_id IS NOT r.source_user_id
                       OR lower(a.verify_status) LIKE 'refuted%'))
              AND NOT EXISTS (
                SELECT 1 FROM memecoin_wallet_addresses a
                WHERE a.network_id=r.network_id AND lower(a.address)=lower(r.address)
                  AND (lower(a.verify_status) LIKE 'refuted%'
                       OR (a.verify_status='fomoscan_confirmed'
                           AND a.source_user_id IS NOT r.source_user_id)))
        """, (chain, OPERATOR_RECEIPT_IDENTITY)).fetchall()
    except sqlite3.OperationalError as exc:
        if str(exc) in ("no such table: memecoin_address_registry",
                        "no such table: memecoin_address_registry_events"):
            return {}
        raise
    claimed = {}
    for (network, address), (uid, _handle) in existing.items():
        claimed.setdefault((str(network), str(address).lower()), set()).add(str(uid))
    out = {}
    for uid, address, handle, raw, actor in rows:
        address = str(address).lower()
        if (not uid or not actor[9:].strip() or not _EVM_ROSTER_ID.fullmatch("evm:" + address)
                or int(address[2:], 16) == 0 or claimed.get((chain, address))):
            continue
        try:
            detail = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if (not isinstance(detail, dict) or not isinstance(detail.get("audit"), str)
                or not detail["audit"].strip() or not isinstance(detail.get("receipts"), list)
                or not detail["receipts"]
                or any(not isinstance(r, dict) or not isinstance(r.get("tx"), str)
                       or not re.fullmatch(r"0x[0-9a-fA-F]{64}", r["tx"])
                       for r in detail["receipts"])):
            continue
        out[address] = (str(uid), handle)
    return out


def evm_watched_owners(conn, chain, *, include_operator=False):
    """{address: (source_user_id, handle)} the EVM watcher subscribes on `chain`: proven addresses
    (`watcher_address_owners`) whose owner is armed on that chain. The ONE definition of "watched"
    on EVM: the watcher builds its transfer filter from it and the J7 shadow gate covers wallets by
    it (2026-09-29), so the two cannot drift. Addresses are lowercased; the handle is the wallet's
    own when it has one. Background readers may opt into explicit operator registry attestations;
    entry-side coverage keeps its existing reads and automatic proof sources by default."""
    from sniper.persistence import db_memecoin as m
    chain = str(chain)
    ids = m.real_approved_user_ids(conn, network_id=chain)
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    handles = {str(r[0]): str(r[1] or "") for r in conn.execute(
        f"SELECT source_user_id, handle FROM memecoin_wallets WHERE source_user_id IN ({marks})",
        tuple(sorted(ids)))}
    owners = watcher_address_owners(conn)
    if include_operator:
        owners.update({(chain, address): owner for address, owner in
                       _operator_registry_owners(conn, chain, owners).items()})
    watched = {}
    for (network, address), (user_id, handle) in owners.items():
        if str(network) != chain or user_id not in handles:
            continue
        watched[str(address).lower()] = (user_id, handles[user_id] or handle or "")
    return watched


def head_basis_addresses(conn):
    """{(chain, address)} whose CURRENT proof needed a head-block code read.

    Reads the same rows `watcher_address_owners` arms the watcher from, so the two cannot drift.
    A row written before `code_basis` existed reports None and is not counted, which is correct:
    it passed the old check, and that check only ever accepted a receipt-block answer.
    """
    out = set()
    for row in _current_proofs(conn):
        try:
            detail = json.loads(row[4] or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(detail, dict) and detail.get("code_basis") == "head":
            out.add(_address_key(row[0], row[1]))
    return out


def _verdict(counts, bought_mints):
    """Confirmation counts DISTINCT coins, not reads: three rebuys of one coin are one campaign."""
    if counts[NO_BUY]:
        return REFUTED
    return CONFIRMED if len(bought_mints) >= MIN_MATCHES else UNVERIFIED


def _fan_out(candidates):
    """A generic EVM row becomes one candidate per EVM chain; a named chain passes through."""
    out = []
    for cand in candidates:
        if str(cand["chain"] or "").strip().lower() != GENERIC_EVM:
            out.append(cand)
            continue
        for chain in EVM_CHAINS:
            out.append({**cand, "chain": chain})
    return out


def _address_key(chain, address):
    chain = canonical_chain_id(chain) or str(chain)
    return chain, str(address) if chain == "solana" else str(address).lower()


def _shared_addresses(conn, candidates):
    """Detect chain-specific owner conflicts, including independent Fomo proofs."""
    owners = {}
    for cand in _fan_out(candidates):
        owners.setdefault(_address_key(cand["chain"], cand["address"]), set()).add(cand["source_user_id"])
    stored = list(conn.execute("SELECT network_id,address,source_user_id FROM axiom_verified_addresses"))
    stored.extend(conn.execute("SELECT network_id,address,source_user_id FROM memecoin_wallet_addresses "
                               "WHERE verify_status=?", (LEGACY_CONFIRMED,)))
    for chain, address, uid in stored:
        key = _address_key(chain, address)
        if key in owners:
            owners[key].add(uid)
    return {key for key, uids in owners.items() if len(uids) > 1}


def _retire_shared(conn, chain, address, *, now_iso):
    """Withdraw conflicting Axiom proof. Preserve independent Fomo proof."""
    chain, address = _address_key(chain, address)
    conn.execute("UPDATE axiom_verified_addresses SET verify_status=?, verified_at=? "
                 "WHERE network_id=? AND CASE WHEN network_id='solana' THEN address "
                 "ELSE lower(address) END=? AND verify_status!=?",
                 (SHARED, now_iso(), chain, address, SHARED))
    conn.commit()


def _candidates(conn):
    # Stored rows are claims to re-check, not confirmation evidence.
    rows = list(conn.execute(
        "SELECT w.source_user_id,w.handle,COALESCE(a.x_handle,w.handle),a.address,a.network_id "
        "FROM axiom_verified_addresses a JOIN memecoin_wallets w "
        "ON w.source_user_id=a.source_user_id WHERE w.paused=0 AND w.excluded=0 "
        "ORDER BY a.verified_at,w.source_user_id,a.network_id"))
    rows.extend(conn.execute(
        "SELECT w.source_user_id, w.handle, d.x_handle, d.address, d.chain "
        "FROM axiom_trader_directory d "
        "JOIN memecoin_wallets w ON LOWER(w.handle)=LOWER(d.x_handle) "
        "WHERE w.real_approved=1 AND w.paused=0 AND w.excluded=0 AND d.address IS NOT NULL "
        "ORDER BY w.source_user_id, d.chain, d.address"))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='axiom_research_accounts'").fetchone():
        rows.extend(conn.execute(
            "SELECT w.source_user_id,w.handle,COALESCE(d.x_handle,a.handle,w.handle),a.address,a.chain "
            "FROM axiom_research_accounts a "
            "JOIN memecoin_wallets w ON w.source_user_id=a.requested_source_user_id "
            "LEFT JOIN axiom_trader_directory d ON d.address=a.address "
            "AND (d.chain=a.chain OR (d.chain='evm' AND a.chain IN ('4663','56','8453','1'))) "
            "WHERE a.active=1 AND w.paused=0 AND w.excluded=0"))
    unique = {}
    for row in rows:
        unique[(row[0], row[4], row[3])] = dict(zip(
            ("source_user_id", "handle", "x_handle", "address", "chain"), row))
    return list(unique.values())


def _buy_events(conn, source_user_id, chain, max_events):
    """The trader's most recent BUY events on this chain, each with our own fill when we copied it.

    A sell is never checked: the address holds the coin before a sell, so "did it buy inside this
    window" is false for a correct address, and one such read would refute it.

    The anchor is our own funded entry transaction for that same event. Its block dates the window
    exactly, which no arithmetic on a feed timestamp can do.

    Events we funded come FIRST, newest first, and unfunded ones fill the rest of the budget: only
    an anchored read can refute, so the budget buys more evidence when it is spent there.
    """
    rows = conn.execute(
        "SELECT e.mint, e.created_at, "
        " (SELECT f.tx_sig FROM memecoin_positions p JOIN memecoin_fills f ON f.position_id=p.id "
        "  WHERE p.origin_event_id=e.fomo_event_id AND p.execution_mode='real' "
        "  AND f.kind='entry' AND f.tx_sig IS NOT NULL ORDER BY f.id LIMIT 1) AS our_tx, "
        " (SELECT p.entry_lag_s FROM memecoin_positions p "
        "  WHERE p.origin_event_id=e.fomo_event_id AND p.execution_mode='real' "
        "  ORDER BY p.id LIMIT 1) AS entry_lag_s "
        "FROM memecoin_events e WHERE e.source_user_id=? AND e.network_id=? "
        "AND e.side='buy' AND e.signal_source='follow_activity' AND e.mint IS NOT NULL "
        "ORDER BY (our_tx IS NULL), e.created_at DESC, e.id DESC LIMIT ?",
        (source_user_id, chain, int(max_events))).fetchall()
    out = []
    for mint, created_at, our_tx, lag in rows:
        anchor = {"tx_sig": our_tx, "entry_lag_s": float(lag or 0.0)} if our_tx else None
        out.append((mint, created_at, anchor))
    return out


def _write_lane(conn, result, *, now_iso, lane_written_now):
    """One row per (trader, chain), owned by this lane. A re-run replaces it, upgrade or downgrade.

    Within one run a second directory address for the SAME chain cannot displace a stronger
    verdict, because the key holds one address per chain.
    """
    key = (result["source_user_id"], result["chain"])
    seen = lane_written_now.get(key)
    if seen is not None and _RANK[result["verify_status"]] <= _RANK[seen]:
        result["skip_reason"] = result["skip_reason"] or f"weaker_than:{seen}"
        return
    conn.execute(
        "INSERT INTO axiom_verified_addresses(source_user_id, handle, x_handle, network_id, "
        "address, verify_status, matches, contradictions, no_data, checked, verified_at, detail) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source_user_id, network_id) DO UPDATE SET handle=excluded.handle, "
        "x_handle=excluded.x_handle, address=excluded.address, "
        "verify_status=excluded.verify_status, matches=excluded.matches, "
        "contradictions=excluded.contradictions, no_data=excluded.no_data, "
        "checked=excluded.checked, verified_at=excluded.verified_at, detail=excluded.detail",
        (result["source_user_id"], result["handle"], result["x_handle"], result["chain"],
         result["address"], result["verify_status"], result["matches"], result["contradictions"],
         result["no_data"], result["checked"], now_iso(), _detail(result)))
    conn.commit()
    result["lane_written"] = True
    lane_written_now[key] = result["verify_status"]


VERIFIED_AT_HEAD = "verified_at_head"


def _code_basis(event_evidence):
    """'head' when any confirming event needed a head-block code read, else 'receipt_block'.

    None when no event carried a basis at all, which is every chain other than 4663 and every
    4663 event whose route the decoder does not support.
    """
    seen = {(event.get("purchase") or {}).get("code_basis")
            for event in (event_evidence or []) if isinstance(event, dict)}
    if "head" in seen:
        return "head"
    return "receipt_block" if "receipt_block" in seen else None


def _report_status(verify_status, code_basis):
    """The label a report may print. A head-basis confirmation is never plain 'verified'.

    The verdict itself is unchanged -- the operator accepted a head basis for the WATCHER, whose
    events are live, so `verify_status` still reads CONFIRMED and the watcher still follows the
    wallet. What a head basis cannot support is the historical claim, and a report that prints
    'verified' beside a backfilled row states exactly that claim. So the label, not the verdict,
    carries the weaker proof.
    """
    if verify_status == CONFIRMED and code_basis == "head":
        return VERIFIED_AT_HEAD
    return verify_status


def _detail(result):
    return json.dumps({"proof_version": result.get("proof_version", PROOF_VERSION), "chain": result["chain"], "matches": result["matches"],
                       "contradictions": result["contradictions"], "no_data": result["no_data"],
                       "checked": result["checked"], "unanchored": result["unanchored"],
                       "x_handle": result["x_handle"],
                       "distinct_mints": result["distinct_mints"],
                       "code_basis": result.get("code_basis"),
                       "report_status": result.get("report_status", result["verify_status"]),
                       "event_evidence": result["event_evidence"],
                       **({"crosschain_basis": result["crosschain_basis"],
                           "chain_code": result.get("chain_code")}
                          if result.get("crosschain_basis") else {}),
                       **({"basis_code": result["basis_code"]} if result.get("basis_code") else {})},
                      sort_keys=True)


def _write(conn, result, *, now_iso, written_now):
    uid = result["source_user_id"]
    row = conn.execute("SELECT method, verify_status FROM memecoin_wallet_addresses "
                       "WHERE source_user_id=?", (uid,)).fetchone()
    if row and row[0] != METHOD:
        # Another method owns this trader's row. The table keys on the trader alone, so a write
        # here would destroy a fomoscan-confirmed address.
        result["skip_reason"] = f"existing_method:{row[0]}"
        return
    if uid in written_now and _RANK[result["verify_status"]] <= _RANK[written_now[uid]]:
        # A second chain for the same trader cannot displace a stronger verdict, because the
        # table holds one row per trader.
        result["skip_reason"] = f"weaker_than:{written_now[uid]}"
        return
    detail = _detail(result)
    conn.execute(
        "INSERT OR REPLACE INTO memecoin_wallet_addresses(source_user_id, handle, network_id, "
        "address, resolved_at, method, coins_held, coins_checked, verify_status, verify_detail) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (uid, result["handle"], result["chain"], result["address"], now_iso(), METHOD,
         result["matches"], result["checked"], result["verify_status"], detail))
    conn.commit()
    result["written"] = True
    written_now[uid] = result["verify_status"]


# --------------------------------------------------------------- public-RPC readers


def public_rpc_reader(*, call_fn=None, sleep_s=0.3, sleep_fn=time.sleep, quote_token_fn=None):
    """A `rpc_read_fn` over public endpoints. No funded key, no custody RPC.

    `call_fn(url, method, params) -> (result, error)` is injectable so tests run without network.
    """
    call = call_fn or _http_call
    quote_token_fn = quote_token_fn or _chain_quote_token
    state = {}

    def read(chain, address, mint, *, window_s=WINDOW_S, at_iso=None, anchor=None):
        read.last_reason = None
        read.last_purchase = None
        at = _epoch(at_iso)
        if at is None and not (anchor or {}).get("tx_sig"):
            return NO_DATA
        try:
            if chain == "solana":
                evidence = {}
                verdict = _solana_bought(call, address, mint, at, window_s, sleep_s, sleep_fn,
                                         anchor, evidence=evidence)
                read.last_reason = evidence.get("reason")
                return verdict
            url = EVM_RPC.get(chain)
            if not url:
                return NO_DATA
            evidence = {}
            verdict = _evm_bought(call, url, chain, address, mint, at, window_s, sleep_s, sleep_fn,
                                  state, anchor, quote_token_fn(chain), evidence=evidence)
            read.last_purchase = evidence.get('purchase')
            read.last_reason = evidence.get('reason')
            return verdict
        except Exception:
            logger.exception("axiom verify: read failed chain=%s mint=%s", chain, mint)
            return NO_DATA

    return read


def _chain_quote_token(chain):
    """The chain's quote asset. A swap moves it OUT of the buyer; a gift moves nothing."""
    try:
        from sniper.execution.evm_wallet import CHAIN_QUOTE
        return str((CHAIN_QUOTE.get(str(chain)) or {}).get("token") or "").lower()
    except Exception:
        logger.exception("axiom verify: no quote token for chain %s", chain)
        return ""


def _http_call(url, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "content-type": "application/json", "user-agent": "sniper-research/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as exc:
        return None, str(exc)
    if "error" in payload:
        return None, payload["error"]
    return payload.get("result"), None


def _epoch(iso):
    from datetime import datetime
    try:
        text = str(iso).replace("Z", "+00:00").replace(" ", "T")
        return int(datetime.fromisoformat(text).timestamp())
    except (TypeError, ValueError, AttributeError):
        return None


def _evm_window(call, url, chain, lo_time, hi_time, sleep_s, sleep_fn, state):
    """Find both inclusive block limits from historical timestamps with 64 block reads."""
    if chain not in state:
        sleep_fn(sleep_s)
        result, error = call(url, "eth_blockNumber", [])
        if error or not result:
            return None
        state[chain] = {"head": _integer(result)}
    head = state[chain]["head"]
    cache = {}

    def read_ts(block):
        if block not in cache:
            if len(cache) >= 64:
                return None
            sleep_fn(sleep_s)
            result, error = call(url, "eth_getBlockByNumber", [hex(block), False])
            cache[block] = (None if error or not result or "timestamp" not in result
                            else _integer(result["timestamp"]))
        return cache[block]

    first, last = read_ts(0), read_ts(head)
    if first is None or last is None or first > lo_time or last <= hi_time:
        return None
    lo = _block_at_time(read_ts, lo_time, 0, head, 0)
    after = _block_at_time(read_ts, hi_time + 1, 0, head, 0)
    if lo is None or after is None or after <= lo:
        return None
    sleep_fn(sleep_s)
    return {"fromBlock": hex(lo), "toBlock": hex(after - 1)}


def _evm_bought(call, url, chain, address, mint, at, window_s, sleep_s, sleep_fn, state,
                anchor=None, quote_token="", evidence=None):
    """Read a complete historical window. Missing coverage remains unknown."""
    address, mint = (address or "").lower(), (mint or "").lower()
    if (anchor or {}).get("tx_sig"):
        return _evm_anchored(call, url, address, mint, anchor, sleep_s, sleep_fn, state, chain,
                             quote_token, evidence=evidence)
    window = _evm_window(call, url, chain, at - window_s, at + window_s,
                         sleep_s, sleep_fn, state)
    if window is None:
        return NO_DATA
    logs, err = call(url, "eth_getLogs", [{**window, "address": mint, "topics": [TRANSFER_TOPIC]}])
    if err or not isinstance(logs, list) or not logs:
        return NO_DATA
    receivers, _ = _transfer_parties(logs)
    if address not in receivers:
        return NO_BUY
    return _evm_spend_verdict(call, url, address, quote_token, logs, sleep_s, sleep_fn, mint=mint, chain=chain, evidence=evidence)


def _evm_anchored(call, url, address, mint, anchor, sleep_s, sleep_fn, state, chain,
                  quote_token="", evidence=None):
    """Date both window limits from the block timestamp of our entry receipt."""
    sleep_fn(sleep_s)
    receipt, err = call(url, "eth_getTransactionReceipt", [anchor["tx_sig"]])
    if err or not isinstance(receipt, dict) or "blockNumber" not in receipt:
        return NO_DATA
    sleep_fn(sleep_s)
    block, err = call(url, "eth_getBlockByNumber", [receipt["blockNumber"], False])
    if err or not block or "timestamp" not in block:
        return NO_DATA
    at = _integer(block["timestamp"])
    lag = max(0.0, float(anchor.get("entry_lag_s") or 0.0))
    window = _evm_window(call, url, chain, int(at - lag - ANCHOR_MARGIN_S),
                         at + ANCHOR_MARGIN_S, sleep_s, sleep_fn, state)
    if window is None:
        return NO_DATA
    logs, err = call(url, "eth_getLogs", [{**window, "address": mint, "topics": [TRANSFER_TOPIC]}])
    if err or not isinstance(logs, list) or not logs:
        return NO_DATA
    controls = [log for log in logs if str(log.get("transactionHash") or "").lower()
                == str(anchor["tx_sig"]).lower()]
    control_receivers, control_senders = _transfer_parties(controls)
    receivers, _ = _transfer_parties(logs)
    ours = str(receipt.get("from") or "").lower()
    if ours not in control_receivers and ours not in control_senders:
        return NO_DATA
    if address not in receivers:
        return NO_BUY
    return _evm_spend_verdict(call, url, address, quote_token, logs, sleep_s, sleep_fn, mint=mint, chain=chain, evidence=evidence)


def _evm_spend_verdict(call, url, address, quote_token, logs, sleep_s, sleep_fn, *, mint=None,
                       chain=None, evidence=None):
    """Require net acquisition and linked quote payment. Unsupported receipts remain unknown."""
    candidates = []
    for log in logs:
        topics = log.get("topics") or []
        if len(topics) != 3 or topics[0] != TRANSFER_TOPIC or _integer(log.get("data")) <= 0:
            continue
        if _topic_address(topics[2]) != address:
            continue
        tx = log.get("transactionHash")
        if tx and tx not in candidates:
            candidates.append(tx)
    if not candidates:
        return NO_BUY
    unknown = False
    for tx in candidates[:MAX_RECEIPT_READS]:
        sleep_fn(sleep_s)
        receipt, err = call(url, "eth_getTransactionReceipt", [tx])
        if err or not isinstance(receipt, dict) or str(receipt.get("status")) not in {"1", "0x1"}:
            unknown = True
            continue
        acquired, spent = wallet_paid_token_flows(receipt, address, quote_token)
        if spent > 0 and mint in acquired:
            return BOUGHT
        if str(chain) == '4663':
            from sniper.services.memecoin_evm_onchain_watch import read_relayer_purchase
            def read_rpc(method, params):
                sleep_fn(sleep_s)
                value, error = call(url, method, params)
                if error:
                    raise RuntimeError('purchase evidence unavailable')
                return value
            try:
                transaction = read_rpc('eth_getTransactionByHash', [tx])
                purchase = read_relayer_purchase(receipt, transaction, address, chain=chain, read_fn=read_rpc, evidence=evidence)
            except Exception:
                purchase = None
            if purchase and purchase['mint'] == mint:
                if evidence is not None:
                    evidence['purchase'] = purchase
                return BOUGHT
            if evidence is not None:
                evidence.setdefault('reason', 'unsupported_or_unavailable_swap_proof')
        tokens = {str(log.get("address") or "").lower() for log in receipt.get("logs") or []
                  if (log.get("topics") or [None])[0] == TRANSFER_TOPIC}
        unknown |= len(tokens - {""}) > 1
    if unknown or len(candidates) > MAX_RECEIPT_READS:
        return NO_DATA
    return TRANSFER


def _transfer_parties(logs):
    receivers, senders = set(), set()
    for log in logs:
        topics = log.get("topics") or []
        if len(topics) < 3 or _integer(log.get("data")) <= 0:
            continue
        senders.add(_topic_address(topics[1]))
        receivers.add(_topic_address(topics[2]))
    return receivers, senders


def _block_at_time(read_ts, at, seed, head, span, max_reads=32):
    """Return the first block at or after the timestamp, or None when the budget expires."""
    lo, hi = 0, head
    for _ in range(max_reads):
        if lo >= hi:
            return lo
        mid = (lo + hi) // 2
        timestamp = read_ts(mid)
        if timestamp is None:
            return None
        if timestamp < at:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo == hi else None


def _solana_bought(call, address, mint, at, window_s, sleep_s, sleep_fn, anchor=None, max_pages=3,
                   max_tx_reads=MAX_TX_READS, evidence=None):
    """Read bounded history. Balance deltas alone cannot prove a Solana purchase."""
    if (anchor or {}).get("tx_sig"):
        sleep_fn(sleep_s)
        ours, err = call(SOLANA_RPC, "getTransaction", [anchor["tx_sig"], {
            "encoding": "jsonParsed", "commitment": "confirmed",
            "maxSupportedTransactionVersion": 1}])
        block_time = (ours or {}).get("blockTime") if not err else None
        if block_time is None:
            return NO_DATA
        lag = max(0.0, float(anchor.get("entry_lag_s") or 0.0))
        lo, hi = int(block_time - lag - ANCHOR_MARGIN_S), int(block_time + ANCHOR_MARGIN_S)
    elif at is None:
        return NO_DATA
    else:
        lo, hi = at - window_s, at + window_s

    before, candidates, covered, timeless = None, [], False, False
    for _ in range(max_pages):
        sleep_fn(sleep_s)
        params = [address, {"limit": 1000, "commitment": "confirmed"}]
        if before:
            params[1]["before"] = before
        sigs, err = call(SOLANA_RPC, "getSignaturesForAddress", params)
        if err or sigs is None:
            return NO_DATA
        if not sigs:
            break
        for entry in sigs:
            block_time = entry.get("blockTime")
            if block_time is None:
                timeless = True                 # its position in the window is unknown
                continue
            if lo <= block_time <= hi and not entry.get("err"):
                candidates.append(entry["signature"])
        oldest = min((e.get("blockTime") for e in sigs if e.get("blockTime")), default=None)
        before = sigs[-1].get("signature")
        if oldest is not None and oldest <= lo:
            covered = True
            break
    if not covered:
        return NO_DATA
    saw_transfer = False
    for signature in candidates[:max_tx_reads]:
        sleep_fn(sleep_s)
        tx, err = call(SOLANA_RPC, "getTransaction", [signature, {
            "encoding": "jsonParsed", "commitment": "confirmed",
            "maxSupportedTransactionVersion": 1}])
        if err or not tx:
            return NO_DATA                      # an unread candidate is not a negative
        meta = tx.get("meta") or {}
        if _token_delta(meta, address, mint) <= 0:
            continue
        if _quote_spent(meta, tx, address):
            if evidence is not None:
                evidence["reason"] = "solana_payment_relation_unsupported"
            return NO_DATA
        saw_transfer = True
    if len(candidates) > max_tx_reads:
        return NO_DATA                          # candidates left unread
    if timeless:
        return NO_DATA                          # coverage of the window is not provable
    return TRANSFER if saw_transfer else NO_BUY


def _quote_spent(meta, tx, address):
    """Did the address PAY in this transaction: SOL beyond the fee, or a quote token out."""
    for quote in QUOTE_MINTS:
        if _token_delta(meta, address, quote) < 0:
            return True
    keys = _account_keys(tx, meta)
    try:
        index = keys.index(address)
    except ValueError:
        return False
    pre = (meta.get("preBalances") or [])
    post = (meta.get("postBalances") or [])
    if index >= len(pre) or index >= len(post):
        return False
    spent = int(pre[index]) - int(post[index])
    return spent > int(meta.get("fee") or 0)


def _account_keys(tx, meta):
    keys = meta.get("accountKeys")
    if keys is None:
        message = ((tx.get("transaction") or {}).get("message") or {})
        keys = message.get("accountKeys") or []
    out = []
    for key in keys:
        out.append(key.get("pubkey") if isinstance(key, dict) else key)
    return out


def _token_delta(meta, owner, mint):
    def total(key):
        out = 0
        for entry in meta.get(key) or []:
            if entry.get("owner") == owner and entry.get("mint") == mint:
                out += int((entry.get("uiTokenAmount") or {}).get("amount") or 0)
        return out
    return total("postTokenBalances") - total("preTokenBalances")
