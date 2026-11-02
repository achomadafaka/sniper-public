"""BSC 56 fast lane (operator via Alpha 2026-10-02: "BSC needs a fast lane, live as fast as
possible"; Gamma's feasibility read, episode 21178f13: today 5 blocks behind at 0.45 s blocks, 1.1-1.6
s of it our decision-to-broadcast, mostly the quote and the signing).

STEP 1, THE WATCHER-SIDE DECIDE (Alpha's approval (a)). The 56 watcher already holds the source
buy's receipt (C4(b) WS). The decide prices from that receipt's OWN Sync on the coin/WBNB
PancakeSwap V2 pair, which is the exact post-trade reserves at zero lag (no tape can be fresher),
with the buy tax the funded path's own decode measured (memecoin_evm_post_trade.source_post_trade,
the watcher's `evm_post`). v1 route scope is that one pair: PancakeSwap V2 carried 42 of the 57
funded 56 fills since 09-09. A receipt whose V2 leg is not our CREATE2 pair, a tax that cannot be
computed, a reverted source, or no price cap is a named fallback (today's path covers the signal).

The decide reads NOTHING: the receipt in hand, the pair by CREATE2, the knobs. It runs in the watcher
AFTER today's detection hand-off (commit + wake), and the datagram never raises or blocks.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import socket
import threading
import time
from fractions import Fraction
from functools import partial
from datetime import datetime

from eth_abi import decode as abi_decode
from eth_abi.exceptions import DecodingError

logger = logging.getLogger(__name__)

# The inline-on-miss seams live with the shared executor (both lanes use them).
from sniper.services.memecoin_rh_fast_lane import (  # noqa: E402
    SafetyCache, _INLINE, _lane_deadline_s, inline_deadline, lane_inline_http)
from sniper.services.memecoin_source_initiation import (
    DIRECT_PROOF_VERSION, RELAY_DEPOSIT_PROOF_VERSION, bsc_payload_initiation_refusal,
    direct_receipt_initiation)
from sniper.services.memecoin_source_fill import source_fill_from_buy, validate_source_fill
from sniper.services.memecoin_evm_onchain_watch import TRANSFER_TOPIC, wallet_paid_token_flows
from sniper.services.memecoin_bsc_volume_poll import FLAP_PORTAL, FLAP_TOPICS
from sniper.util.receipt_clock import freeze_receipt_clock

CHAIN = "56"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
PCS_V2_ROUTER = "0x10ed43c718714eb63d5aa57b78b54704e256024e"
_PREFIX = "\0sniper-bsc-fast-lane-"
_WATCHER_BEAT_MAX_AGE_S = 60.0
CANDIDATE_LIMIT, CANDIDATE_TTL_S = 32, 600.0
# THE ARMED WARM SET (lambda + Beta, Alpha GO 2026-10-14): 73% of armed 56 buys (10-10..10-14, n=237)
# were coins an armed wallet had decided on within 24 h, while the presignal warm fed only new
# PancakeSwap pairs. The feeder re-reads the shared set this often; a warm job waits this long after
# an entry (the shared rule: no inline read in flight, none started in the last second), and this
# long at most for the process-wide DexScreener warm slot.
WARM_SET_PERIOD_S, WARM_ENTRY_COOLDOWN_S, WARM_STATS_WAIT_S = 10.0, 1.0, 3.0

# THE LANE'S POLICY REFUSAL IS FINAL (73798, 2026-10-12; operator 2026-10-14: the fastest
# execution, with our filters applied better). The lane decides from the source's own receipt; a
# J7 copy of the same buy must not re-decide it on its own event mcap. In memory, in the runner
# process that runs both: no I/O or lock on any fill path. Mechanical refusals (float, snapshot,
# lock, revert, proof) are not decisions and stay open for the slower path.
# ponytail: memory only; a runner restart between the lane refusal and the J7 copy forgets it.
_POLICY_FINAL = ("hook:skipped_mcap:", "hook:skipped_chase:", "source_band:",
                 "hook:rejected:source buy ")
# A tx-less J7 'swap:<uuid>' report binds only an mcap refusal: a coin-level fact that barely
# moves in 120 s. Chase judged one source fill; a spaced second buy has its own (Gamma, 10-14).
_POLICY_WALLET_SCOPE = ("hook:skipped_mcap:",)
POLICY_DECISION_TTL_S, POLICY_DECISION_MAX = 120.0, 512
_POLICY_DECISIONS = {}                     # (tx, mint) -> (source_user_id, reason, at), oldest first
_POLICY_LOCK = threading.Lock()


def note_policy_decision(tx_hash, mint, source_user_id, reason, *, now=None, final=_POLICY_FINAL):
    """Record a final policy refusal of one source buy (`final`: the reason prefixes that count);
    anything else is ignored."""
    if not isinstance(reason, str) or not reason.startswith(final):
        return False
    tx, coin = _fixed_hex(tx_hash, 32), _fixed_hex(mint, 20)
    if not tx or not coin:
        return False
    at = time.time() if now is None else float(now)
    with _POLICY_LOCK:
        _POLICY_DECISIONS.pop((tx, coin), None)
        _POLICY_DECISIONS[(tx, coin)] = (str(source_user_id or ""), reason, at)
        while len(_POLICY_DECISIONS) > POLICY_DECISION_MAX:
            del _POLICY_DECISIONS[next(iter(_POLICY_DECISIONS))]
    return True


def policy_decision_for(*, tx_hash, mint, source_user_id, now=None):
    """The lane's final refusal reason for this source buy, else None. An exact transaction binds
    every policy refusal; a tx-less report binds only the same wallet's mcap refusal of the same
    coin inside the TTL."""
    coin = _fixed_hex(mint, 20)
    if not coin:
        return None
    tx = _fixed_hex(tx_hash, 32) if tx_hash else None
    at = time.time() if now is None else float(now)
    with _POLICY_LOCK:
        if tx is not None:
            row = _POLICY_DECISIONS.get((tx, coin))
            return row[1] if row and 0 <= at - row[2] <= POLICY_DECISION_TTL_S else None
        for (_tx, other), (uid, reason, noted) in reversed(_POLICY_DECISIONS.items()):
            if (other == coin and uid and uid == str(source_user_id or "")
                    and reason.startswith(_POLICY_WALLET_SCOPE)
                    and 0 <= at - noted <= POLICY_DECISION_TTL_S):
                return reason
    return None


def clear_policy_decisions():
    with _POLICY_LOCK:
        _POLICY_DECISIONS.clear()


# WANTED HOP QUOTES (73844/73812/72879, 2026-10-14). An armed buy whose WBNB/quote hop the tape
# did not track (or never synced) names that quote here; the tape pins it for the next buy. Pins
# otherwise came only from our own fills, and PairCreated targets churn out of their 128-slot cap.
# Written by the watcher after its decision; read by the tape's 5-minute pin worker. Never a fill.
HOP_WANTED_MAX_AGE_S, HOP_WANTED_LIMIT = 86400.0, 32
_HOP_WANTED_REASONS = ("hop_state_stale:untracked", "hop_state_stale:no_sync")


def hop_wanted_path():
    from sniper.util.state_paths import state_path
    return state_path("data", "bsc_hop_wanted.txt")


def want_hop_quote(quote, *, now=None, path=None):
    """Append '<quote> <unix seconds>'. Never raises: a lost line is one more untracked miss."""
    quote = _fixed_hex(quote, 20)
    if not quote or quote == WBNB or int(quote, 16) == 0:
        return False
    try:
        path = hop_wanted_path() if path is None else path
        with open(path, "a") as f:
            f.write("%s %d\n" % (quote, int(time.time() if now is None else now)))
        return True
    except Exception:
        logger.warning("bsc hop wanted quote not written", exc_info=True)
        return False


def wanted_hop_quotes(*, now, path=None, max_age_s=HOP_WANTED_MAX_AGE_S, limit=HOP_WANTED_LIMIT):
    """Newest distinct quotes wanted within `max_age_s`; malformed lines are ignored."""
    try:
        path = hop_wanted_path() if path is None else path
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 65536))
            lines = f.read().decode("ascii", "replace").splitlines()
    except (OSError, TypeError, ValueError):   # absent or unreadable: no wanted pins, routes stand
        return []
    out = []
    for line in reversed(lines):
        parts = line.split()
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        quote = _fixed_hex(parts[0], 20)
        if (quote and quote != WBNB and int(quote, 16) and quote not in out
                and 0 <= now - int(parts[1]) <= max_age_s):
            out.append(quote)
            if len(out) >= limit:
                break
    return out


REQUIRED = ("chain", "tx_hash", "seq", "wallet", "mint", "pool", "quote", "coin1", "r_coin",
            "r_quote", "src_in", "src_out", "hop", "tax_num", "tax_den", "cap_num", "cap_den",
            "decimals", "paid_usd", "source_price", "bnb_usd", "bnb_usd_at", "sent_epoch")
V3_REQUIRED = ("chain", "tx_hash", "seq", "wallet", "mint", "pool", "quote", "post_ratio",
               "decimals", "paid_usd", "source_price", "sent_epoch", "route_kind")
# IPortal TokenCreated(uint256 ts, address creator, uint256 nonce, address token, string name,
# string symbol, string meta): keccak verified against position 73815's source receipt.
FLAP_TOKEN_CREATED = "0x504e7f360b2e5fe33cbaaae4c593bc55305328341bf79009e43e0e3b7f699603"
_ZERO_ADDRESS = "0x" + "00" * 20
# A Relay Flap buy's token hops: 73827 had four (Portal -> executor -> router -> Relay -> wallet).
RELAY_FLAP_MAX_HOPS = 6
# Each flap_shared proof keeps its own payment basis (the runner refuses any other pairing).
_FLAP_BASIS = {DIRECT_PROOF_VERSION: "transaction_value_upper_bound",
               RELAY_DEPOSIT_PROOF_VERSION: "relay_receipt_outflow"}
FLAP_REQUIRED = ("chain", "tx_hash", "seq", "wallet", "mint", "pool", "quote", "decimals",
                 "paid_usd", "source_price", "sent_epoch", "route_kind", "arrival_utc",
                 "source_user_id", "source_initiation", "payment_basis")


def presignal_warm_enabled():
    import os
    return os.environ.get("MEMECOIN_BSC_PRESIGNAL_WARM_ENABLED", "").strip().lower() in {
        "1", "true", "on", "yes"}


def _fixed_hex(value, size):
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 2 + size * 2:
        return None
    try:
        return value.lower() if len(bytes.fromhex(value[2:])) == size else None
    except ValueError:
        return None


def _receipt_metadata(row):
    clock = freeze_receipt_clock(row.get("receipt_clock"))
    metadata = {"receipt_clock": dict(clock)} if clock is not None else {}
    stages = _receipt_stages(row.get("receipt_stages"))
    if stages:
        metadata["receipt_stages"] = list(stages)
    return metadata


def _receipt_stages(value):
    if not isinstance(value, (tuple, list)):
        return ()
    stages = []
    for stage in value[:64]:
        if not isinstance(stage, dict):
            continue
        elapsed = stage.get("receipt_elapsed_ms")
        if (isinstance(stage.get("name"), str) and stage["name"]
                and type(stage.get("monotonic_ns")) is int and stage["monotonic_ns"] >= 0
                and (stage.get("boot_id") is None or isinstance(stage["boot_id"], str))
                and (elapsed is None or type(elapsed) in (int, float)
                     and math.isfinite(elapsed) and elapsed >= 0)):
            stages.append({key: stage.get(key) for key in (
                "name", "monotonic_ns", "boot_id", "receipt_elapsed_ms")})
    return tuple(stages)


def _receipt_int(value):
    if type(value) is int:
        return value if value >= 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value, 16 if value.startswith("0x") else 10)
            return parsed if parsed >= 0 else None
        except ValueError:
            pass
    return None


def _flap_arrival_valid(arrival, now):
    from sniper.settings import memecoin as cfg
    try:
        stamp = datetime.fromisoformat(arrival.replace("Z", "+00:00"))
        limit = cfg.MEMECOIN_REAL_ENTRY_MAX_AGE_S
        return (stamp.utcoffset() is not None and not isinstance(now, bool)
                and isinstance(now, (int, float)) and math.isfinite(now)
                and not isinstance(limit, bool) and isinstance(limit, (int, float))
                and math.isfinite(limit) and limit > 0 and 0 <= now - stamp.timestamp() <= limit)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False


def _flap_shared_payload(row, receipt, transaction, *, decimals, now):
    """An exact Portal buy is a shared-executor hint, never a curve price.

    Direct: reuse the watcher's direct initiation/payment proof and accept only the exact
    Portal -> actor -> owner transfer (or Portal -> owner), without tax or extra legs. A
    create-and-buy may lead with the Portal's genesis mint of the owner's own new token.
    Relay (operator option b, 2026-10-14, 73827): only a Solana-deposit-proved Relay envelope,
    its economics through validate_source_fill (the ordinary path's paid USD and price), and the
    whole Portal output passed hop by hop, unchanged, from the Portal to the owner.
    TokenBought's eth/fee/postPrice words do not authenticate the payment or a ratio.
    """
    try:
        buy = row["buy"]
        mint, owner = _fixed_hex(buy["mint"], 20), _fixed_hex(row["wallet"], 20)
        tx_hash = _fixed_hex(row["tx_hash"], 32)
        if (not mint or not owner or not tx_hash or not isinstance(receipt, dict)
                or not isinstance(transaction, dict) or _receipt_int(transaction.get("chainId")) != 56
                or _receipt_int(receipt.get("status")) != 1
                or type(decimals) is not int or not 0 <= decimals <= 36
                or not _flap_arrival_valid(row.get("now"), now)):
            return None
        proof = buy.get("source_initiation")
        relay = isinstance(proof, dict) and proof.get("proof_version") == RELAY_DEPOSIT_PROOF_VERSION
        if relay:
            from sniper.execution.evm_relay_quote import RELAY_APPROVAL_PROXY
            block, block_hash = proof.get("block_number"), _fixed_hex(proof.get("block_hash"), 32)
            if (type(block) is not int or not block_hash or proof.get("tx_hash") != tx_hash
                    or _fixed_hex(transaction.get("hash"), 32) != tx_hash
                    or _fixed_hex(receipt.get("transactionHash"), 32) != tx_hash
                    or _fixed_hex(transaction.get("to"), 20) != RELAY_APPROVAL_PROXY
                    or _fixed_hex(receipt.get("to"), 20) != RELAY_APPROVAL_PROXY
                    # The deposit names this Relay order: the fill calldata's last word (Gamma LOW
                    # on f571dbd85), so a proof bound to another order never rides this receipt.
                    or not _fixed_hex(proof.get("order_id"), 32)
                    or not str(transaction.get("input") or "").lower().endswith(proof["order_id"].lower()[2:])
                    or any(_fixed_hex(x.get("blockHash"), 32) != block_hash
                           or _receipt_int(x.get("blockNumber")) != block for x in (receipt, transaction))):
                return None
            sig = dict(chain=CHAIN, tx_hash=tx_hash, wallet=owner, source_user_id=row.get("source_user_id"),
                       mint=mint, seq=block, block_hash=block_hash)
            fill = validate_source_fill(source_fill_from_buy(buy, sig), sig)
            if (not fill or fill["payment_basis"] != "relay_receipt_outflow"
                    or fill.get("source_initiation", {}).get("proof_version") != RELAY_DEPOSIT_PROOF_VERSION):
                return None
        else:
            expected = direct_receipt_initiation(receipt, transaction, owner, mint, chain=CHAIN)
            if (not expected or not isinstance(proof, dict) or expected["tx_hash"] != tx_hash
                    or any(proof.get(k) != v for k, v in expected.items())):
                return None
            block, block_hash = expected["block_number"], expected["block_hash"]
        portal, native = FLAP_PORTAL[CHAIN], _receipt_int(transaction.get("value"))
        if not relay and not native:
            return None
        logs = receipt.get("logs")
        if not isinstance(logs, list) or not logs:
            return None
        indexes, trades, transfers, creators = set(), [], [], []
        for log in logs:
            index = _receipt_int(log.get("logIndex"))
            if (index is None or index in indexes or log.get("removed") is not False
                    or _fixed_hex(log.get("transactionHash"), 32) != tx_hash
                    or _fixed_hex(log.get("blockHash"), 32) != block_hash
                    or _receipt_int(log.get("blockNumber")) != block):
                return None
            indexes.add(index)
            topics = log.get("topics") or []
            address = _fixed_hex(log.get("address"), 20)
            if address == portal and topics and topics[0] in FLAP_TOPICS:
                raw = _fixed_hex(log.get("data"), 7 * 32)
                if len(topics) != 1 or not raw:
                    return None
                words = abi_decode(["uint256", "address", "address", "uint256", "uint256",
                                    "uint256", "uint256"], bytes.fromhex(raw[2:]))
                if words[1].lower() == mint:
                    if topics[0] != FLAP_TOPICS[0]:
                        return None
                    trades.append((words[2].lower(), words[3], words[4], words[5], words[6]))
            if address == portal and topics and topics[0] == FLAP_TOKEN_CREATED:
                data = log.get("data")
                if len(topics) != 1 or not isinstance(data, str) or not data.startswith("0x"):
                    return None
                words = abi_decode(["uint256", "address", "uint256", "address", "string", "string",
                                    "string"], bytes.fromhex(data[2:]))
                if words[3].lower() == mint:
                    creators.append(words[1].lower())
            if address == mint and topics and topics[0] == TRANSFER_TOPIC:
                if len(topics) != 3 or any(not _fixed_hex(t, 32) or t[2:26] != "0" * 24 for t in topics[1:]):
                    return None
                raw = _fixed_hex(log.get("data"), 32)
                if not raw:
                    return None
                transfers.append(("0x" + topics[1][-40:].lower(), "0x" + topics[2][-40:].lower(),
                                  int(raw, 16), index))
        source_log = row.get("log") or {}
        # WS delivery metadata (e.g. blockTimestamp) may differ from the receipt's log.
        # Bind its actual event identity and contents, never the entire delivery dictionary.
        bound = next((log for log in logs if _receipt_int(log.get("logIndex")) ==
                      _receipt_int(source_log.get("logIndex"))), None)
        if (not bound or source_log.get("removed") is not False or len(trades) != 1
                or _receipt_int(source_log.get("blockNumber")) != block
                or any(_fixed_hex(source_log.get(k), n) != _fixed_hex(bound.get(k), n)
                       for k, n in (("transactionHash", 32), ("blockHash", 32), ("address", 20)))
                or source_log.get("topics") != bound.get("topics")
                or source_log.get("data") != bound.get("data")):
            return None
        actor, amount = trades[0][:2]
        # Measurement only (decision features): the source's own TokenBought eth/fee/postPrice
        # words, in the curve's quote atoms. They authenticate nothing and no gate reads them.
        bought = dict(zip(("source_quote_paid_atomic", "source_fee_atomic", "source_post_price_atomic"),
                          trades[0][2:]))
        if amount <= 0 or (not relay and actor not in (owner, _fixed_hex(transaction.get("to"), 20))):
            return None
        flows = [(sender, receiver, qty) for sender, receiver, qty, _ in transfers]
        if flows and flows[0][0] == _ZERO_ADDRESS:
            # A create-and-buy (73815): the Portal's genesis mint of the source's OWN new token
            # comes first. Exactly one, to the Portal, under one TokenCreated naming the owner.
            if flows[0][1] != portal or flows[0][2] <= 0 or creators != [owner]:
                return None
            flows = flows[1:]
        if relay:
            # Portal -> TokenBought's buyer -> ... -> owner, the full amount at every hop and no
            # address twice: a tax, split, extra leg or foreign recipient stays ordinary fallback.
            path = [portal]
            for sender, receiver, qty in flows:
                if qty != amount or sender != path[-1] or receiver in path:
                    return None
                path.append(receiver)
            if (len(path) < 2 or path[1] != actor or path[-1] != owner or len(flows) > RELAY_FLAP_MAX_HOPS
                    or fill["acquired_atomic"] != str(amount) or fill["token_decimals"] != decimals):
                return None
            return _flap_payload(row, buy, proof, mint=mint, tx_hash=tx_hash, owner=owner, block=block,
                                 decimals=decimals, amount=amount, basis="relay_receipt_outflow", now=now,
                                 bought=bought)
        wanted = [(portal, actor, amount)]
        if actor != owner:
            wanted.append((actor, owner, amount))
        if flows != wanted:  # Extra/taxed/partial/multi-hop transfers stay ordinary fallback.
            return None
        payment = {}
        acquired, spent = wallet_paid_token_flows(receipt, owner, WBNB, transaction,
                                                  payment_details=payment)
        if (acquired != {mint: amount} or spent != native or payment.get("native_paid") != native
                or payment.get("quote_paid") or payment.get("unlinked_quote_outflow")
                or buy.get("payment_token") != "0x" + "00" * 20
                or buy.get("payment_atomic") != str(native) or buy.get("acquired_atomic") != str(amount)
                or type(buy.get("payment_decimals")) is not int or buy["payment_decimals"] != 18
                or type(buy.get("token_decimals")) is not int or buy["token_decimals"] != decimals
                or buy.get("payment_basis") != "transaction_value_upper_bound"
                or buy.get("receipt_proof_version") != expected["proof_version"]):
            return None
        return _flap_payload(row, buy, proof, mint=mint, tx_hash=tx_hash, owner=owner, block=block,
                             decimals=decimals, amount=amount, basis=buy["payment_basis"], now=now,
                             bought=bought)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, DecodingError):
        return None


def _flap_payload(row, buy, proof, *, mint, tx_hash, owner, block, decimals, amount, basis, now,
                  bought):
    paid, price = buy.get("source_size_usd"), buy.get("source_price")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
           for v in (paid, price)) or not math.isclose(price, paid / (amount / 10 ** decimals), rel_tol=1e-12):
        return None
    payload = {"chain": CHAIN, "route_kind": "flap_shared", "mint": mint, "tx_hash": tx_hash,
               "wallet": owner, "seq": block, "pool": FLAP_PORTAL[CHAIN], "quote": None,
               "decimals": decimals, "paid_usd": paid, "source_price": price,
               "payment_basis": basis,
               "source_user_id": row.get("source_user_id"), "handle": row.get("handle"),
               "source_initiation": proof, "arrival_utc": row["now"], "sent_epoch": now, **bought}
    payload.update(_receipt_metadata(row))
    return payload if bsc_payload_initiation_refusal(payload) is None else None


def flap_decision_features(*, decision_at, held, their_usd, payload, sink):
    """decision_features_v1 for a bsc_flap_fast_lane attempt, built at trace finish (spec
    2026-10-14 4.3). `held`: the hook's own stats read; `payload`: the datagram's TokenBought
    words; `sink`: the curve state the Flap safety check read, if it ran. Unknown is null."""
    from datetime import datetime
    from sniper.services.memecoin_decision_features import _null, from_cached_stats
    out = from_cached_stats(decision_at=decision_at, lane="bsc_flap_fast_lane", held=held,
                            their_usd=their_usd)
    for field in ("source_quote_paid_atomic", "source_fee_atomic", "source_post_price_atomic"):
        value = payload.get(field)
        if type(value) is int and value >= 0:
            out[field] = value
        else:
            _null(out, field, "not_in_event")
    state = sink.get("flap_curve_state")
    curve = ("curve_real_quote_atomic", "curve_real_token_atomic", "curve_virtual_quote_atomic",
             "curve_virtual_token_atomic", "curve_progress", "quote_mint", "quote_decimals")
    ints = ("reserve", "coin_reserve", "r", "h", "circulating_supply", "dex_supply_thresh")
    if (not isinstance(state, dict)
            or any(type(state.get(k)) is not int or state[k] < 0 for k in ints)
            or not state["dex_supply_thresh"] or not isinstance(state.get("quote_token"), str)):
        for field in curve:
            _null(out, field, "curve_state_not_held")
        return out
    out.update(curve_real_quote_atomic=state["reserve"], curve_real_token_atomic=state["coin_reserve"],
               curve_virtual_quote_atomic=state["reserve"] + state["r"],
               curve_virtual_token_atomic=state["coin_reserve"] + state["h"],
               curve_progress=state["circulating_supply"] / state["dex_supply_thresh"],
               curve_progress_basis="flap_curve_state_at_safety", quote_mint=state["quote_token"])
    if state["quote_token"] == _ZERO_ADDRESS:
        out["quote_decimals"] = 18
    else:
        _null(out, "quote_decimals", "not_held")
    try:
        age = (datetime.fromisoformat(state["observed_at"]) - datetime.fromisoformat(decision_at)
               ).total_seconds()
        out["curve_state_at"], out["curve_state_age_s"] = state["observed_at"], age
    except (KeyError, TypeError, ValueError):
        _null(out, "curve_state_age_s", "observed_at_unknown")
    return out


def lane_address(root):
    """The 56 lane's abstract socket name for this state root (never the 4663 lane's)."""
    return _PREFIX + hashlib.sha256(str(root).encode()).hexdigest()[:12]


def default_address():
    from sniper.util.state_paths import state_root
    return lane_address(state_root())


def our_pair(token_a, token_b=WBNB):
    """The PancakeSwap V2 pair of two tokens by CREATE2 (the constant proven against our own fills)."""
    from sniper.services.memecoin_evm_post_trade import V2_FACTORIES, _v2_pair
    factory, init_hash = V2_FACTORIES[(CHAIN, PCS_V2_ROUTER)]
    return _v2_pair(factory, init_hash, str(token_a).lower(), str(token_b).lower())


def _words(log):
    data = str(log.get("data") or "0x")[2:]
    return [int(data[i:i + 64], 16) for i in range(0, len(data), 64)]


def _logs_of(receipt, address, topic0):
    return [log for log in receipt.get("logs") or ()
            if str(log.get("address") or "").lower() == address
            and str((log.get("topics") or [""])[0]).lower() == topic0]


def _pair_state(receipt, pair):
    """(reserve0, reserve1) of `pair`'s LAST Sync in the receipt, or None."""
    from sniper.services.memecoin_evm_post_trade import V2_SYNC
    syncs = _logs_of(receipt, pair, V2_SYNC)
    return tuple(_words(syncs[-1])[:2]) if syncs else None


def _quote_of(receipt, pool, mint):
    """The pool's other token: the one token the source's transaction sent INTO the pool."""
    from sniper.services.memecoin_evm_post_trade import TRANSFER
    into = {str(log.get("address") or "").lower() for log in receipt.get("logs") or ()
            if len(log.get("topics") or ()) == 3 and str(log["topics"][0]).lower() == TRANSFER
            and "0x" + str(log["topics"][2])[-40:].lower() == pool
            and str(log.get("address") or "").lower() != mint}
    return next(iter(into)) if len(into) == 1 else None


def _split_leg(receipt, mint, owner):
    """v1.1 SPLIT ROUTES (spec 2026-10-02-memecoin-bsc-56-fast-lane-design.md): the coin's one V2
    leg in a Relay-solver transaction that also bought the coin elsewhere, as {"pool", "venue",
    "buy_tax"}, else {"reason"}. The shared decoder refuses to bind one leg to such a buy, rightly:
    its result is the funded path's chase reference for the WHOLE buy. The lane buys on this pair
    only, so it needs the pair's own leg, and the owner having received at least OUT_SLACK of that
    pair's payout is what makes the pair part of the source's buy. The tax rule is the funded one
    (memecoin_evm_post_trade._v2_buy_tax): any other shape is unknown, never guessed."""
    from sniper.services.memecoin_evm_post_trade import OUT_SLACK, TRANSFER, V2_SWAP, _v2_buy_tax
    mint, owner = str(mint).lower(), str(owner).lower()
    sent, delta = {}, 0
    for log in receipt.get("logs") or ():
        topics = log.get("topics") or ()
        if (len(topics) == 3 and str(topics[0]).lower() == TRANSFER
                and str(log.get("address") or "").lower() == mint):
            amount = int(str(log.get("data") or "0x0"), 16)
            sender = "0x" + str(topics[1])[-40:].lower()
            receiver = "0x" + str(topics[2])[-40:].lower()
            sent.setdefault(sender, []).append((receiver, amount))
            delta += (amount if receiver == owner else 0) - (amount if sender == owner else 0)
    pairs = [pool for pool in sent if _logs_of(receipt, pool, V2_SWAP)]
    if len(pairs) != 1:
        return {"reason": "split:no_single_v2_leg"}
    pool = pairs[0]
    out = sum(amount for _receiver, amount in sent[pool])
    if out <= 0 or delta < OUT_SLACK * out:
        return {"reason": "split:owner_short"}
    return {"pool": pool, "venue": "v2",
            "buy_tax": _v2_buy_tax(sent[pool], mint, len(_logs_of(receipt, pool, V2_SWAP)))}


def decide_from_receipt(receipt, *, mint, evm_post, chase_pct, premium, seq=None, hop_state_fn=None,
                        owner=None):
    """The lane's decision from the source's own receipt, or {"reason": <fallback>}. A source buy
    the shared decoder could not bind (`leg_does_not_bind` or `split_route`): with the source wallet
    as `owner`, the lane re-derives the coin's V2 leg itself (_split_leg) and decides on it, and the
    result carries split=True. Everything else is v1 (_decide_v2_leg)."""
    split = False
    if (isinstance(receipt, dict) and isinstance(evm_post, dict) and "pool" not in evm_post
            and evm_post.get("reason") in {"leg_does_not_bind", "split_route"} and owner):
        evm_post = _split_leg(receipt, mint, owner)
        if "pool" not in evm_post:
            return evm_post
        split = True
    out = _decide_v2_leg(receipt, mint=mint, evm_post=evm_post, chase_pct=chase_pct,
                         premium=premium, seq=seq, hop_state_fn=hop_state_fn)
    if split:
        out["split"] = True
    return out


def _decide_v2_leg(receipt, *, mint, evm_post, chase_pct, premium, seq=None, hop_state_fn=None):
    """The lane's decision from the source's own receipt, or {"reason": <fallback>}.

    THE POOL IS THE ONE THE SOURCE TRADED (Gamma/Alpha on b44fae0b2): evm_post's V2 pool, accepted
    only when it is the PancakeSwap V2 pair of the coin and the token the source paid into it (CREATE2
    proves both the factory and the quote). The coin leg's exact post-trade reserves and the source's
    swap come from that pool's own Sync and Swap in the receipt; the cap is in quote per coin atomic.

    A NON-WBNB QUOTE IS A SECOND HOP (WBNB -> quote -> coin, one native-BNB call): its state must be
    exact too, from the WBNB/quote pair's own Sync in the same receipt when the source touched it
    (Alpha 2026-10-02: the receipt first), else `hop_state_fn(pair, seq)`: the bsc_v2_live tape at
    lag 0 for the configured hop pairs (tape_hop_state), else hop_state_stale:<why> with the quote
    (F2: why is the tape's own miss reason, `unknown` from a reader that gives none, `no_reader`
    without one, `zero_reserve` for an empty side).
    Returns {"reason": "would_send", pool, quote, coin1, r_coin, r_quote, src_in, src_out, tax (exact
    Fraction), cap (Fraction, quote per coin atomic), hop (None, or {pair, r_wbnb, r_quote, source,
    height})}. Pure apart from `hop_state_fn`."""
    from sniper.services.memecoin_evm_post_trade import V2_SWAP, v2_buy_with_sell_tax
    if not isinstance(receipt, dict) or str(receipt.get("status") or "").lower() not in (
            "0x1", "1", "true"):
        return {"reason": "source_reverted"}
    if not isinstance(evm_post, dict):
        return {"reason": "post:none"}
    if "pool" not in evm_post:
        return {"reason": "post:%s" % (evm_post.get("reason") or "none")}
    if evm_post.get("venue") != "v2":
        return {"reason": "not_v2:%s" % evm_post.get("venue")}
    mint, pool = str(mint).lower(), str(evm_post.get("pool") or "").lower()
    quote = _quote_of(receipt, pool, mint)
    if quote is None or our_pair(mint, quote) != pool:
        return {"reason": "not_our_pair"}
    tax = evm_post.get("buy_tax")
    if tax is None:
        return {"reason": "tax_unknown"}
    state, swaps = _pair_state(receipt, pool), _logs_of(receipt, pool, V2_SWAP)
    if len(swaps) > 1:
        bound = v2_buy_with_sell_tax(receipt.get('logs') or (), mint, pool)
        swaps = [bound[0]] if bound is not None else []
    if state is None or len(swaps) != 1:
        return {"reason": "pair_state_unreadable"}
    coin1 = int(mint, 16) > int(quote, 16)
    a0in, a1in, a0out, a1out = _words(swaps[0])[:4]
    r_coin, r_quote = (state[1], state[0]) if coin1 else state
    src_in, src_out = (a0in, a1out) if coin1 else (a1in, a0out)
    if min(r_coin, r_quote, src_in, src_out) <= 0:
        return {"reason": "not_a_buy_on_pair"}
    cap = bsc_price_cap(r_quote, r_coin, src_in, src_out, chase_pct=chase_pct, premium=premium)
    if cap is None:
        return {"reason": "no_price_cap"}
    hop = None
    if quote != WBNB:
        hop_pair = our_pair(WBNB, quote)
        got, source = _pair_state(receipt, hop_pair), "receipt"
        height = seq
        if got is None:
            fresh = hop_state_fn(hop_pair, seq) if hop_state_fn is not None else "no_reader"
            if not isinstance(fresh, tuple):
                return {"reason": "hop_state_stale:%s" % (fresh or "unknown"), "quote": quote}
            got, height, source = (fresh[0], fresh[1]), fresh[2], "tape"
        wbnb1 = int(WBNB, 16) > int(quote, 16)
        r_wbnb, r_hop_quote = (got[1], got[0]) if wbnb1 else got
        if min(r_wbnb, r_hop_quote) <= 0:
            return {"reason": "hop_state_stale:zero_reserve", "quote": quote}
        hop = {"pair": hop_pair, "r_wbnb": int(r_wbnb), "r_quote": int(r_hop_quote),
               "source": source, "height": height}
    return {"reason": "would_send", "pool": pool, "quote": quote, "coin1": coin1,
            "r_coin": r_coin, "r_quote": r_quote, "src_in": src_in, "src_out": src_out,
            "tax": _exact_tax(receipt, mint, pool, tax), "cap": cap, "hop": hop}


def v2_amount_out(amount_in, reserve_in, reserve_out):
    """PancakeSwap V2's getAmountOut (a 0.25% fee: 9975 / 10000)."""
    with_fee = int(amount_in) * 9975
    return with_fee * int(reserve_out) // (int(reserve_in) * 10000 + with_fee)


def expected_out_for(clip_wei, payload):
    """The coin atomic units a `clip_wei` buy delivers at the receipt's exact post state, through
    the router's real path (the hop with its impact, then the coin leg), less the coin's tax."""
    amount = int(clip_wei)
    hop = payload.get("hop")
    if hop:
        amount = v2_amount_out(amount, int(hop["r_wbnb"]), int(hop["r_quote"]))
    keep = 1 - Fraction(int(payload["tax_num"]), int(payload["tax_den"]))
    return int(v2_amount_out(amount, int(payload["r_quote"]), int(payload["r_coin"])) * keep)


def bsc_price_cap(r_quote, r_coin, src_in, src_out, *, chase_pct, premium):
    """The lane's price cap in quote per coin atomic, or None without a term: the stricter of the
    source's post-trade price x (1 + chase) and the source's average fill x premium. One function
    for the decide's cap and the lane's cap rule (2026-10-15, Alpha)."""
    from sniper.settings import memecoin as cfg
    if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
        return Fraction(int(r_quote), int(r_coin))  # Metadata; never the signed output floor.
    terms = []
    if chase_pct and chase_pct > 0:
        terms.append(Fraction(int(r_quote), int(r_coin)) * (1 + Fraction(str(chase_pct)) / 100))
    if premium and premium > 0:
        terms.append(Fraction(int(src_in), int(src_out)) * Fraction(str(premium)))
    return min(terms) if terms else None


def _clip_in_quote(clip_wei, payload):
    """The clip in quote units: a WBNB -> quote hop at its FEE-INCLUSIVE SPOT (see min_out_for)."""
    amount = Fraction(int(clip_wei))
    hop = payload.get("hop")
    if hop:
        amount = amount * 9975 / 10000 * Fraction(int(hop["r_quote"]), int(hop["r_wbnb"]))
    return amount


def cap_min_out(clip_wei, payload, cap=None):
    """The coin atomic units a `clip_wei` buy must deliver at the price `cap` (quote per coin atomic;
    default the payload's lane cap), less the coin's tax: the worst gross price we accept is `cap`."""
    if cap is None:
        cap = Fraction(int(payload["cap_num"]), int(payload["cap_den"]))
    keep = 1 - Fraction(int(payload["tax_num"]), int(payload["tax_den"]))
    return int(_clip_in_quote(clip_wei, payload) / cap * keep)


def slow_lane_premium_cap(payload):
    """The onchain lane's execution-premium bound, as a price cap, or None: with E11 off (no display
    price) its premium gate compares our price to the source's POST-TRADE price at
    MEMECOIN_REAL_MAX_EXEC_PREMIUM_POST_TRADE (execution_premium_decision, onchain=True).

    A DIAGNOSTIC since 2026-10-16, never a floor (_floor_evidence). On 56 the onchain lane hands
    the event to this lane (bsc_fast_lane_handoff), so after a miss the next buyer is the J7 follow
    lane, which has no post-trade price: 眉眉 (0xa099…7777, 09:01:42Z) reverted on this x1.2 bound
    at +22% over post, and J7 bought the same coin 4 blocks later at +25.8% over the source average.
    The floor's slow-lane term is follow_lane_chase_cap."""
    from sniper.settings import memecoin as cfg
    if not cfg.dexscreener_off("E11", CHAIN):
        return None
    k = getattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_POST_TRADE", None)
    try:
        k = Fraction(str(float(k)))
    except (TypeError, ValueError):
        return None
    if k <= 0:
        return None
    return Fraction(int(payload["r_quote"]), int(payload["r_coin"])) * k


def follow_lane_chase_cap(payload):
    """The J7 follow lane's bound on this chain, as a price cap, or None (2026-10-16, Alpha D1).

    J7 has no post-trade price, so execution_premium_decision references the source average at
    MEMECOIN_REAL_MAX_EXEC_PREMIUM (already the lane cap's premium term) and the executor's chase
    confirmation allows the source average + the wallet's chase cap: min(MEMECOIN_REAL_CHASE_MAX_PCT,
    MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT), lifted per wallet by wallet_band_cap_pct. J7 lifts a
    nano coin to MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT; this lane cannot classify the band in time,
    so it keeps the unlifted cap (stricter, never looser than J7). None when J7 has no chase cap,
    no source average exists, or E11 is on (J7 then references a display price this lane cannot
    see; the lane cap alone, as before)."""
    from sniper.settings import memecoin as cfg
    if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
        return None
    if not cfg.dexscreener_off("E11", CHAIN):
        return None
    caps = [float(c) for c in (cfg.MEMECOIN_REAL_CHASE_MAX_PCT,
                               cfg.MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT) if float(c) > 0]
    if not caps:
        return None
    pct = cfg.wallet_band_cap_pct(payload.get("source_user_id"), CHAIN, min(caps))
    try:
        pct = Fraction(str(float(pct)))
        src_in, src_out = int(payload["src_in"]), int(payload["src_out"])
    except (KeyError, TypeError, ValueError):
        return None
    if pct <= 0 or src_in <= 0 or src_out <= 0:
        return None
    return Fraction(src_in, src_out) * (1 + pct / 100)


def floor_at_cap_enabled():
    """MEMECOIN_BSC_FAST_LANE_FLOOR_AT_CAP_CHAINS lists 56 (call-time; empty or malformed = off)."""
    from sniper.settings import memecoin as cfg
    return CHAIN in cfg.memecoin_bsc_fast_lane_floor_at_cap_chains()


def min_out_for(clip_wei, payload, *, slippage_pct=None, expected_out=None, floor_at_cap=False):
    """The coin atomic units a `clip_wei` buy must deliver to us: the STRICTER of two rules.

    The cap rule: the clip in quote units, then at the cap on the coin leg, less the coin's tax. A
    WBNB -> quote hop is priced at its FEE-INCLUSIVE SPOT, never its post-impact output (Alpha on
    Gamma's finding: the source paid in quote from inventory, so the hop's impact counts against
    the same chase/premium cap; a thin hop then reverts instead of overpaying).

    The slippage rule (Alpha on Gamma's sandwich finding): the exact expected output at the
    receipt's post state less MEMECOIN_ENTRY_SLIPPAGE_PCT, the funded path's amountOutMinimum
    rule. The cap alone leaves up to the chase allowance (30%) for a sandwich on a public send.

    `floor_at_cap` (MEMECOIN_BSC_FAST_LANE_FLOOR_AT_CAP_CHAINS, 2026-10-15, Alpha (B); D1
    2026-10-16): the worst price the lane that buys after a miss accepts instead -- the lane cap
    and the J7 follow lane's chase bound, whichever is stricter -- for a send that goes to the
    private builders ONLY (BscLive _buy_tx/_post), so no public mempool sees the wider floor. The
    copy-premium floor still applies on top (_buy_tx). The slippage rule and the onchain lane's
    post-trade premium are diagnostics (_floor_evidence), not floors."""
    from sniper.settings import memecoin as cfg
    if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
        expected = expected_out_for(clip_wei, payload) if expected_out is None else expected_out
        slip = cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT if slippage_pct is None else slippage_pct
        keep = 1 - Fraction(str(slip)) / 100
        if expected <= 0 or not 0 < keep <= 1:
            raise ValueError("native output quote unavailable")
        return int(Fraction(expected) * keep)
    cap_rule = cap_min_out(clip_wei, payload)
    if floor_at_cap:
        follow = follow_lane_chase_cap(payload)
        return max(cap_rule, cap_min_out(clip_wei, payload, cap=follow)) if follow else cap_rule
    if slippage_pct is None:
        from sniper.settings import memecoin as cfg
        slippage_pct = cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT
    expected = expected_out_for(clip_wei, payload) if expected_out is None else expected_out
    exact = Fraction(expected) * (1 - Fraction(str(slippage_pct)) / 100)
    return max(cap_rule, int(exact))


def hop_pairs():
    """{WBNB/quote PancakeSwap V2 pair: quote} for MEMECOIN_BSC_FAST_LANE_HOP_QUOTES (CREATE2)."""
    from sniper.settings import memecoin as cfg
    return {our_pair(WBNB, q): q for q in cfg.MEMECOIN_BSC_FAST_LANE_HOP_QUOTES}


def tape_hop_state(conn, pair, seq, *, now, head_ttl_s=3.0):
    """The WBNB/quote pair's (reserve0, reserve1, height) from the bsc_v2_live tape AT LAG 0, else
    the reason it cannot vouch (F2): no_seq, no_head, head_behind (its contiguous head has not
    reached the source's block), head_old (over `head_ttl_s`), untracked (neither a configured hop
    nor a tape target), no_sync (tracked, no Sync or seed in this coverage window), off_canonical,
    tape_error. The pair's last Sync (or seed) must be on a canonical head it kept or deeper than its
    kept heads (final). A coverage reset deletes every Sync, so a row present is inside one unbroken
    coverage window. A pair the tape does not record is unknown, never zero."""
    try:
        if seq is None:
            return "no_seq"
        feed = conn.execute("SELECT height, received FROM feed WHERE id=1").fetchone()
        if not feed or feed[0] is None or feed[1] is None:
            return "no_head"
        if int(feed[0]) < int(seq):
            return "head_behind"
        if now - float(feed[1]) > head_ttl_s:
            return "head_old"
        pair = str(pair).lower()
        row = conn.execute("SELECT reserve0, reserve1, height, hash FROM syncs WHERE pool = ?",
                           (pair,)).fetchone()
        if not row:            # the miss path only: the lane falls back either way
            tracked = pair in hop_pairs() or conn.execute(
                "SELECT 1 FROM hop_targets WHERE pool = ? UNION ALL SELECT 1 FROM hop_pins WHERE pool = ?",
                (pair, pair)).fetchone()
            return "no_sync" if tracked else "untracked"
        oldest = conn.execute("SELECT min(height) FROM heads").fetchone()[0]
        on_head = conn.execute("SELECT 1 FROM heads WHERE height = ? AND hash = ?",
                               (int(row[2]), str(row[3]).lower())).fetchone()
        if not on_head and (oldest is None or int(row[2]) >= int(oldest)):
            return "off_canonical"
        return int(row[0]), int(row[1]), int(row[2])
    except Exception:
        logger.exception("bsc fast lane: tape hop read failed (signal falls back)")
        return "tape_error"


def tape_hop_reader(*, clock=time.time):
    """hop_state_fn over the bsc_v2_live tape (its own SQLite file, read-only, opened on first use):
    a local read, never the network."""
    state = {}

    def read(pair, seq):
        if "conn" not in state:
            from sniper.services.memecoin_bsc_v2_live import connect
            state["conn"] = connect(readonly=True)
        return tape_hop_state(state["conn"], pair, seq, now=clock())
    def close():
        conn = state.pop("conn", None)
        if conn is not None:
            conn.close()
    read.close = close
    return read


def _exact_tax(receipt, mint, pair, decoded):
    """The decode's tax as an exact fraction of the pair's coin payout (its float form is what the
    funded path stores; the lane's minimum output uses the exact one). Zero stays zero."""
    from sniper.services.memecoin_evm_post_trade import TRANSFER
    if not decoded:
        return Fraction(0)
    paid = [_words(log)[0] for log in receipt.get("logs") or ()
            if str(log.get("address") or "").lower() == mint and len(log.get("topics") or ()) == 3
            and str(log["topics"][0]).lower() == TRANSFER
            and "0x" + str(log["topics"][1])[-40:].lower() == pair]
    total, main = sum(paid), max(paid)
    return Fraction(total - main, total)


def lane_payload(row, out, *, decimals, bnb_usd, bnb_usd_at, sent_epoch):
    """The datagram for one would-send decision: the signal's identity and the exact state the
    runner's lane prices from, so it reads nothing before its send. Fractions travel exactly."""
    buy, log = row["buy"], row.get("log") or {}
    cap, tax = out["cap"], out["tax"]
    return {**_receipt_metadata(row), "chain": CHAIN, "tx_hash": str(row["tx_hash"]).lower(),
            "seq": int(str(log.get("blockNumber") or "0x0"), 16),
            "wallet": str(row["wallet"]).lower(), "mint": str(buy["mint"]).lower(),
            "handle": row.get("handle"), "source_user_id": row.get("source_user_id"),
            "source_initiation": buy.get("source_initiation"),
            "arrival_utc": row.get("now"), "pool": out["pool"], "quote": out["quote"],
            "coin1": bool(out["coin1"]), "r_coin": int(out["r_coin"]),
            "r_quote": int(out["r_quote"]), "src_in": int(out["src_in"]),
            "src_out": int(out["src_out"]), "hop": out.get("hop"),
            "tax_num": tax.numerator, "tax_den": tax.denominator,
            "cap_num": cap.numerator, "cap_den": cap.denominator, "decimals": int(decimals),
            "paid_usd": float(buy["source_size_usd"]),
            "source_price": float(buy["source_price"]), "bnb_usd": float(bnb_usd),
            "bnb_usd_at": float(bnb_usd_at), "state_kind": "receipt_post",
            "split": bool(out.get("split")),
            **_source_head(row),
            "sent_epoch": float(sent_epoch)}


def inputs_float_ok(snap, clip_wei, gas_limit, price, low_wei):
    """The float check the locked inputs_reason will make for a send signed at `price`: native less
    what the bid pays above the snapshot's price covers the clip, the 3-swap reserve at the snapshot's
    price and the low-water mark (float_covers)."""
    from sniper.execution.evm_native_float import float_covers
    from sniper.services.memecoin_rh_fast_lane import bid_extra_wei
    native = snap.get("native_wei")
    if type(native) is not int:
        return False
    return float_covers(native - bid_extra_wei(snap, gas_limit, price), clip_wei, gas_limit,
                        snap.get("gas_price_wei"), low_wei)


def _source_head(row):
    """S's header milliTimestamp and when its head reached the watcher (the crowded gas rule's paired
    head: memecoin_bsc_crowded_gas). Absent when that block's head was not seen on the socket."""
    seen = row.get("source_head")
    if (not isinstance(seen, (tuple, list)) or len(seen) != 2 or type(seen[1]) is not int
            or not isinstance(seen[0], (int, float)) or isinstance(seen[0], bool)):
        return {}
    return {"source_block_ms": seen[1], "source_head_at": float(seen[0])}


def send_signal(address, payload):
    """One datagram to the lane: never raises, never blocks (False when nothing is bound)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(json.dumps(payload).encode(), socket.MSG_DONTWAIT, address)
        return True
    except Exception:  # noqa: BLE001 - a signal must never fail the watcher
        return False


HOP_LATE_CHECK_S = 2.0


def tape_pair_height(pair):
    """The tape's current Sync height for `pair` (a fresh read-only connection), or None."""
    from sniper.services.memecoin_bsc_v2_live import connect
    conn = connect(readonly=True)
    try:
        row = conn.execute("SELECT height FROM syncs WHERE pool = ?", (str(pair).lower(),)).fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


def hop_late_check(hop, seq, height_fn):
    """Gamma's hop_late measure: a tape-priced hop is LATE when the pair's row, read again after the
    decision, has moved into (the height the decide used, the source block]: a Sync from at or
    before the source's block was still in flight. Returns the record for the log line."""
    try:
        seen = height_fn(hop["pair"])
    except Exception as exc:
        return {"pair": hop["pair"], "used": hop["height"], "seen": None, "seq": seq,
                "late": "unknown:%s" % type(exc).__name__}
    # MASKED (Gamma on 88b99f4a5): the tape keeps only a pair's latest Sync, so a row already past
    # the source block may have overwritten one in (used, seq]: that case is not evidence of 0.
    if seen is None:
        late = "unknown"
    elif seen > int(seq):
        late = "masked"
    else:
        late = int(seen > int(hop["height"]))
    return {"pair": hop["pair"], "used": hop["height"], "seen": seen, "seq": seq, "late": late}


def make_watcher_signal(mode, address, *, send_fn=None, clock=time.time, hop_state_fn=None,
                        hop_check_fn=None, height_fn=None, delay_s=HOP_LATE_CHECK_S):
    """The 56 watcher's hook after today's hand-off, or None unless the runner's 56 lane runs
    (runner_shadow or live). It returns the decision with `decide_ms` and `sent`; it never raises.
    A tape-priced hop is re-read `delay_s` later off the watcher's thread and `hop_check_fn`
    receives hop_late_check's record (measurement only: the decision is already sent)."""
    import threading
    if mode not in ("runner_shadow", "live"):
        return None
    send_fn = send_fn or send_signal
    hop_state_fn = hop_state_fn or tape_hop_reader(clock=clock)
    height_fn = height_fn or tape_pair_height

    def schedule_hop_check(row, hop, seq):
        def check():
            rec = hop_late_check(hop, seq, height_fn)
            rec["tx_hash"] = row.get("tx_hash")
            try:
                hop_check_fn(rec)
            except Exception:
                logger.exception("bsc fast lane: hop check record failed")
        timer = threading.Timer(delay_s, check)
        timer.daemon = True
        timer.start()
        return timer

    def signal(row, receipt, evm_post, *, decimals, bnb_usd, bnb_usd_age_s, transaction=None):
        from sniper.settings import memecoin as cfg
        t0 = time.perf_counter()
        try:
            if isinstance(evm_post, dict) and evm_post.get("venue") == "v3":
                # This is a hint for the existing funded V3 executor, never a reserve-priced
                # native transaction. Its quoter/factory, measured cost and every gate still run.
                if mode != "live":
                    return {"reason": "v3_shadow_unsupported", "sent": False, "decide_ms": 0.0}
                from sniper.services.memecoin_evm_post_trade import source_post_trade
                decoded = source_post_trade(CHAIN, receipt, row["buy"]["mint"], owner=row.get("wallet"))
                source_tx = str(row.get("tx_hash") or "").lower()
                block = (row.get("log") or {}).get("blockNumber")
                seq = int(block, 16) if isinstance(block, str) else int(block or 0)
                receipt_block = receipt.get("blockNumber")
                receipt_seq = (int(receipt_block, 16) if isinstance(receipt_block, str)
                               else int(receipt_block or 0))
                unbound = ("decode_mismatch" if decoded != {k: v for k, v in evm_post.items() if k != "ms"}
                           else "no_pool" if not decoded.get("pool")
                           else "tx_mismatch" if source_tx != str(receipt.get("transactionHash") or "").lower()
                           else "block_mismatch" if seq <= 0 or seq != receipt_seq else None)
                if unbound:            # F2: the clause, and the decode's real time (was a constant 0.0)
                    return {"reason": "v3_source_unbound:" + unbound, "sent": False,
                            "decide_ms": (time.perf_counter() - t0) * 1000}
                mint, pool = str(row["buy"]["mint"]).lower(), decoded["pool"]
                quote = _quote_of(receipt, pool, mint)
                if quote is None or decimals is None:
                    return {"reason": "v3_source_unknown", "sent": False, "decide_ms": 0.0}
                payload = {"chain": CHAIN, "route_kind": "v3_shared", "mint": mint,
                           "tx_hash": source_tx, "wallet": row["wallet"], "seq": seq,
                           "pool": pool, "quote": quote, "post_ratio": decoded["ratio"],
                           "decimals": int(decimals), "paid_usd": float(row["buy"]["source_size_usd"]),
                           "source_price": float(row["buy"]["source_price"]),
                           "source_user_id": row.get("source_user_id"), "handle": row.get("handle"),
                           "source_initiation": row["buy"].get("source_initiation"),
                           "arrival_utc": row.get("now"), "sent_epoch": clock()}
                payload.update(_receipt_metadata(row))
                return {"reason": "would_send", "sent": bool(send_fn(address, payload)),
                        "decide_ms": (time.perf_counter() - t0) * 1000}
            if (mode == "live" and cfg.MEMECOIN_EVM_FLAP_CURVE_ENABLED
                    and isinstance(evm_post, dict) and evm_post.get("reason") == "no_venue_leg"):
                payload = _flap_shared_payload(row, receipt, transaction, decimals=decimals, now=clock())
                if payload is not None:
                    return {"reason": "would_send", "sent": bool(send_fn(address, payload)),
                            "decide_ms": (time.perf_counter() - t0) * 1000}
            if decimals is None:
                out = {"reason": "decimals_unknown"}
            elif not bnb_usd or bnb_usd <= 0 or bnb_usd_age_s is None:
                out = {"reason": "no_bnb_usd"}
            else:
                out = decide_from_receipt(
                    receipt, mint=row["buy"]["mint"], evm_post=evm_post,
                    chase_pct=cfg.MEMECOIN_REAL_CHASE_MAX_PCT,
                    premium=cfg.MEMECOIN_REAL_MAX_EXEC_PREMIUM,
                    seq=int(str((row.get("log") or {}).get("blockNumber") or "0x0"), 16) or None,
                    hop_state_fn=hop_state_fn, owner=row.get("wallet"))
            out["sent"] = False
            if out["reason"] == "would_send":
                now = clock()
                payload = lane_payload(row, out, decimals=decimals, bnb_usd=bnb_usd,
                                       bnb_usd_at=now - float(bnb_usd_age_s), sent_epoch=now)
                try:
                    out["sent"] = bool(send_fn(address, payload))
                except Exception:              # the decision stands; today's path covers it
                    logger.exception("bsc fast lane: datagram send failed")
                hop = out.get("hop")
                if hop and hop.get("source") == "tape" and hop_check_fn is not None:
                    out["hop_check"] = schedule_hop_check(row, hop, payload["seq"])
        except Exception:
            logger.exception("bsc fast lane: watcher signal failed (today's path covers it)")
            out = {"reason": "error", "sent": False}
        out["decide_ms"] = (time.perf_counter() - t0) * 1000.0
        return out

    def intent(mint, tx_hash, pair=None):
        """C1 (2026-10-16): one datagram at the source log's arrival, before the receipt decode, so
        the lane starts the coin's reads. Live and MEMECOIN_EVM_LANE_INTENT_WARM_CHAINS only;
        best-effort, never blocks or raises (send_signal). `pair`: the source Transfer's sender, which
        binds the intent's safety quote when it is the coin's known pool."""
        from sniper.settings import memecoin as cfg
        try:
            if mode != "live" or CHAIN not in cfg.memecoin_evm_lane_intent_warm_chains():
                return False
            return bool(send_fn(address, {"message_kind": "intent_v1", "chain": CHAIN,
                                          "mint": str(mint or "").lower(),
                                          "tx_hash": str(tx_hash or "").lower(),
                                          "pair": str(pair).lower() if pair else None,
                                          "sent_epoch": clock()}))
        except Exception:
            logger.exception("bsc fast lane: intent send failed")
            return False
    signal.intent = intent
    signal.close = getattr(hop_state_fn, "close", lambda: None)
    return signal


def watcher_pid(path, now, max_age_s=_WATCHER_BEAT_MAX_AGE_S):
    """The 56 watcher's pid from its own heartbeat (ISO `ts`), or None when the beat is missing,
    stale or carries no pid: then the runner's lane hears no datagram."""
    from datetime import datetime
    try:
        beat = json.loads(open(path).read())
        ts = datetime.fromisoformat(str(beat["ts"]).replace("Z", "+00:00")).timestamp()
        if now - ts > max_age_s:
            return None
        return int(beat["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


class CandidateIdentity:
    """Tape PID/UID/release identity, refreshed by the snapshot owner off the buy path."""

    def __init__(self, root, *, proc_root="/proc"):
        from pathlib import Path
        self.root, self.proc_root = Path(root).resolve(), Path(proc_root)
        self.pid = None

    def refresh(self):
        import os
        from pathlib import Path
        self.pid = None
        try:
            manifest = json.loads((self.root / "data/memecoin_deploy.json").read_text())
            release = Path(__file__).resolve().parents[2]
            if (Path(manifest["release_path"]).resolve() != release
                    or Path(manifest["state_root"]).resolve() != self.root
                    or len(manifest["sha"]) != 40 or int(manifest["sha"], 16) < 0):
                return None
            pid = int((self.root / "data/memecoin_bsc_v2_live.pid").read_text().strip())
            proc = self.proc_root / str(pid)
            args = (proc / "cmdline").read_bytes().split(b"\0")
            script = str(release / "sniper/services/memecoin_bsc_v2_live.py").encode()
            if (pid <= 0 or proc.stat().st_uid != os.getuid() or len(args) < 2
                    or args[1 if args[1] != b"-u" else 2] != script):
                return None
            self.pid = pid
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            pass
        return self.pid


def candidate_refusal(payload, *, now):
    """Canonical PCS read targets only; this shape carries no buy or funding authority."""
    if (not isinstance(payload, dict) or set(payload) != {
            "message_kind", "chain", "mint", "quote", "pool", "observed_at"}
            or payload.get("message_kind") != "candidate_v1" or payload.get("chain") != CHAIN):
        return "malformed_candidate"
    mint, quote, pool = (_fixed_hex(payload.get(k), 20) for k in ("mint", "quote", "pool"))
    observed = payload.get("observed_at")
    if (not mint or not quote or not pool or int(mint, 16) == 0 or int(quote, 16) == 0
            or mint in {quote, WBNB} or pool != our_pair(mint, quote)
            or type(observed) not in (float, int) or not math.isfinite(observed)
            or observed <= 0 or not 0 <= now - observed <= CANDIDATE_TTL_S):
        return "malformed_candidate"
    return None


# ================================================================================================
# STEP 2, THE RUNNER'S 56 LANE IN runner_shadow (Alpha's approval (b): no 56 RPC key yet, so the
# safety verdict and the wallet snapshot are absent; the signal is still judged, live stays
# blocked). The lane thread authenticates the watcher's datagram, builds the unsigned native-BNB
# buy at the pilot clip with the whole-path minimum, and tries the wallet lock without waiting. Off
# the lane thread, one counted DexScreener read gives the funded hook its market data and its
# every gate judges the signal on a READ-ONLY ledger connection inside a rolled-back transaction,
# stopped before the leg. The runner makes no chain call for this lane in runner_shadow.
# ================================================================================================

SWAP_ETH_FOR_TOKENS_FOT = "b6f9de95"   # swapExactETHForTokensSupportingFeeOnTransferTokens
V2_BUY_GAS = 400_000                   # covers the two-hop path
_SEEN_MAX = 4096


def buy_path(payload):
    """[WBNB, coin], or [WBNB, quote, coin] for a pair quoted in another token."""
    quote, mint = str(payload["quote"]).lower(), str(payload["mint"]).lower()
    return [WBNB, mint] if quote == WBNB else [WBNB, quote, mint]


def v2_native_buy_tx(payload, clip_wei, min_out, deadline, taker):
    """The unsigned PancakeSwap V2 buy paid in native BNB: no approval on the path."""
    from eth_abi import encode
    from eth_utils import to_checksum_address
    path = [to_checksum_address(t) for t in buy_path(payload)]
    body = encode(["uint256", "address[]", "address", "uint256"],
                  [int(min_out), path, to_checksum_address(taker), int(deadline)])
    return {"to": PCS_V2_ROUTER, "data": "0x" + SWAP_ETH_FOR_TOKENS_FOT + body.hex(),
            "value": int(clip_wei), "gas": V2_BUY_GAS}


def check_sender(payload, pid, uid, *, watcher_pid):
    """None when the datagram is the 56 watcher's and well formed, else why it is dropped."""
    import os
    if uid != os.getuid():
        return "foreign_uid"
    if watcher_pid is None:
        return "no_watcher"
    if pid != watcher_pid:
        return "not_the_watcher"
    kind = payload.get("route_kind") if isinstance(payload, dict) else None
    required = V3_REQUIRED if kind == "v3_shared" else FLAP_REQUIRED if kind == "flap_shared" else REQUIRED
    if (not isinstance(payload, dict) or any(k not in payload for k in required)
            or payload.get("chain") != CHAIN):
        return "malformed"
    if required is V3_REQUIRED or required is FLAP_REQUIRED:
        try:
            for key, size in (("tx_hash", 32), ("mint", 20), ("wallet", 20), ("pool", 20)) + (
                    (("quote", 20),) if required is V3_REQUIRED else ()):
                value = payload[key]
                if not isinstance(value, str) or not value.startswith("0x") or len(value) != 2 + size * 2:
                    return "malformed"
                bytes.fromhex(value[2:])
            if (type(payload["seq"]) is not int or payload["seq"] <= 0
                    or type(payload["decimals"]) is not int or not 0 <= payload["decimals"] <= 36):
                return "malformed"
            for key in ("paid_usd", "source_price", "sent_epoch") + (
                    ("post_ratio",) if required is V3_REQUIRED else ()):
                value = payload[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    return "malformed"
            if required is FLAP_REQUIRED:
                if (payload["pool"].lower() != FLAP_PORTAL[CHAIN] or payload["quote"] is not None
                        or (set(REQUIRED) - set(FLAP_REQUIRED) | {"post_ratio"}).intersection(payload)
                        or not isinstance(payload["source_initiation"], dict)
                        or payload["payment_basis"] != _FLAP_BASIS.get(
                            payload["source_initiation"].get("proof_version"))
                        or not _flap_arrival_valid(payload["arrival_utc"], payload["sent_epoch"])
                        or bsc_payload_initiation_refusal(payload) is not None):
                    return "malformed"
        except (TypeError, ValueError, OverflowError):
            return "malformed"
    return None


class Seen:
    """Each source buy is judged once (by transaction and coin)."""

    def __init__(self):
        import collections
        self._keys = collections.OrderedDict()

    def first(self, payload):
        key = (str(payload["tx_hash"]).lower(), str(payload["mint"]).lower())
        fresh = key not in self._keys
        self._keys[key] = True
        while len(self._keys) > _SEEN_MAX:
            self._keys.popitem(last=False)
        return fresh


class BscLaneEvent:
    """The event the funded hook judges for one 56 lane signal: OnchainEv's shape for the same buy,
    from the datagram alone (the watcher's paid size and source price, the source's post-trade ratio
    on its pool for the chase gates, the market cap carried as the event's own)."""
    side = "buy"
    signal_source = "bsc_fast_lane"
    delivery_lane = "bsc_fast_lane"
    ticker = None
    clock_domain = "observed_real_utc"
    source_timestamp_trust = "observed_detection"
    no_revival = True

    def __init__(self, payload, *, market_cap):
        from sniper.services.memecoin_evm_post_trade import EvmChaseReference
        self.mint = str(payload["mint"]).lower()
        self.network_id = CHAIN
        self.source_user_id = payload.get("source_user_id")
        self.source_handle = payload.get("handle")
        self.source_wallet = payload.get("wallet")
        self.source_block_number = payload.get("seq")
        self.receipt_clock = freeze_receipt_clock(payload.get("receipt_clock"))
        self.receipt_stages = _receipt_stages(payload.get("receipt_stages"))
        self.source_initiation = payload.get("source_initiation")
        self.their_size_usd = float(payload["paid_usd"])
        self.their_price = float(payload["source_price"])
        self.their_market_cap = market_cap
        v3 = payload.get("route_kind") == "v3_shared"
        flap = payload.get("route_kind") == "flap_shared"
        if v3:
            self.signal_source, self.delivery_lane = "onchain_watch", "bsc_v3_fast_lane"
            ratio = payload["post_ratio"]
        elif flap:
            self.signal_source, self.delivery_lane = "onchain_watch", "bsc_flap_fast_lane"
        else:
            post = Fraction(int(payload["r_quote"]), int(payload["r_coin"]))
            ratio = post / Fraction(int(payload["src_in"]), int(payload["src_out"]))
        self.evm_chase_reference = EvmChaseReference(CHAIN, post={"reason": "no_venue_leg"} if flap else {
            "ratio": float(ratio), "pool": str(payload["pool"]).lower(), "venue": "v3" if v3 else "v2"})
        self.implied_quote_reserve_usd = self.implied_quote_leg = None
        self.fomo_event_id = "bscfast:%s:%s:%s" % (CHAIN, str(payload["tx_hash"]).lower(), self.mint)
        if v3 or flap:
            self.fomo_event_id = "onchain:%s:%s:%s" % (CHAIN, str(payload["tx_hash"]).lower(), self.mint)
            self.no_revival = False
        self.created_at = self.received_at = self.received_at_normalized = payload.get("arrival_utc")
        # The source facts the entry view resolves for a bscfast: origin (memecoin_db.resolve_source_evidence,
        # as the slot lane's), so an alert names the size instead of "bought unknown size" (Alpha,
        # degen_ak AGNET 4663 2026-10-02: the attempt view had no size while the event had $99.62).
        self.source_evidence = {
            "their_size_usd": self.their_size_usd, "source_user_id": self.source_user_id,
            "network_id": CHAIN, "mint": self.mint, "signature": str(payload["tx_hash"]).lower(),
            "source_initiation": self.source_initiation, "source_wallet": self.source_wallet,
            "source_block_number": self.source_block_number,
            "source_timestamp_trust": "observed_detection", "valuation_basis": "lane_paid_usd"}
        if flap:
            self.source_evidence["payment_basis"] = payload["payment_basis"]
        if self.receipt_clock is not None:
            self.source_evidence["receipt_clock"] = dict(self.receipt_clock)
        if self.receipt_stages:
            self.source_evidence["receipt_stages"] = self.receipt_stages


# Gamma on 5424e373e: funded 56 prices supply from chain totalSupply (evm_source); the shadow's is
# DexScreener's fdv / priceUsd. The basis is written on every verdict so the grade can compare.
SHADOW_MCAP_BASIS = "shadow_dex_supply_x_source_price"


def shadow_market_cap(stats, source_price):
    """The shadow's mcap with no 56 key to read supply: DexScreener's supply (fdv / price) at the
    SOURCE's price, so the hook's own supply read never fires. Labelled; None when unknown."""
    try:
        fdv, price = float(stats.get("fdv") or 0), float(stats.get("price") or 0)
        mcap = fdv / price * float(source_price) if fdv > 0 and price > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return mcap if mcap and mcap > 0 else None


class BscShadow:
    """runner_shadow for 56. `handle` is the lane thread's own work; `verdict` runs on a background
    thread with its own read-only ledger connection (see the step-2 header)."""

    def __init__(self, *, lock_key, lock_dir, taker, now_fn, clip_usd, bnb_usd_max_age_s,
                 ctx=None, contexts=None, ro_connect=None, hook_factory=None, fresh_stats_fn=None,
                 verdict_sink=None, host_clock=time.time):
        from sniper.services.memecoin_rh_fast_lane import default_hook_factory
        self.lock_key, self.lock_dir, self.taker, self.now_fn = lock_key, lock_dir, taker, now_fn
        self.clip_usd, self.bnb_usd_max_age_s, self.host_clock = clip_usd, bnb_usd_max_age_s, host_clock
        self.ctx, self.contexts = dict(ctx or {}), dict(contexts or {})
        self.ro_connect, self.hook_factory = ro_connect, hook_factory or default_hook_factory
        self.fresh_stats_fn, self.verdict_sink = fresh_stats_fn, verdict_sink
        self._checks = []

    def handle(self, payload, *, recv_epoch):
        from sniper.execution.exec_lock import wallet_exec_lock
        from sniper.services.memecoin_rh_fast_lane import clip_wei_for
        start, t0, c0, host_now = self.now_fn(), time.perf_counter(), time.thread_time(), self.host_clock()
        rec = {"tx_hash": payload["tx_hash"], "mint": payload["mint"], "pool": payload["pool"],
               "quote": payload["quote"], "recv_epoch": recv_epoch,
               "sent_epoch": payload["sent_epoch"],
               "handoff_ms": (recv_epoch - float(payload["sent_epoch"])) * 1000.0,
               "clip_wei": None, "min_out": None, "tx_bytes": None}
        clip_wei = clip_wei_for(self.clip_usd, payload.get("bnb_usd"))
        rec["clip_wei"] = clip_wei
        if clip_wei is None:
            reason = "no_bnb_usd"
        elif host_now - float(payload["bnb_usd_at"]) > self.bnb_usd_max_age_s:
            reason = "bnb_usd_stale"
        else:
            reason = None
            try:
                min_out = min_out_for(clip_wei, payload)
                tx = v2_native_buy_tx(payload, clip_wei, min_out, int(host_now) + _lane_deadline_s(),
                                       self.taker)
                rec.update(min_out=min_out, tx_bytes=(len(tx["data"]) - 2) // 2)
                if min_out <= 0:
                    reason = "build_refused"
            except (ValueError, ZeroDivisionError, KeyError, TypeError):
                reason = "build_refused"
        if reason is None:
            with wallet_exec_lock(self.lock_key, lock_dir=self.lock_dir, blocking=False) as held:
                reason = "built" if held else "lock_busy"
        rec.update(reason=reason, lock_busy=int(reason == "lock_busy"), busy_start_epoch=start,
                   busy_end_epoch=self.now_fn(), busy_ms=(time.perf_counter() - t0) * 1000.0,
                   cpu_ms=(time.thread_time() - c0) * 1000.0)
        if reason in ("built", "lock_busy") and self.fresh_stats_fn is not None:
            self._start_verdict(payload)
        return rec

    def verdict(self, conn, payload, stats):
        """"pass" when every gate of the funded hook passes, else hook:<status>:<why>, or
        no_market_cap when the stats cannot give the event its mcap."""
        from sniper.services.memecoin_real_runner import J7_SHADOW_LANE
        from sniper.services.memecoin_rh_fast_lane import _never_execute, hook_reason, refusal_pages_off
        mcap = shadow_market_cap(stats, payload["source_price"])
        if mcap is None:
            return "no_market_cap", None
        ev = BscLaneEvent(payload, market_cap=mcap)
        ev.delivery_lane = J7_SHADOW_LANE              # the hook stops before the leg
        contexts = {**self.contexts, CHAIN: dict(
            self.ctx, usdc_fn=lambda: None, native_fn=lambda: None,
            token_safety_fn=lambda mint, usd=None: (False, "no_56_safety_host"),
            execute_fn=_never_execute, warm_quote_price_fn=None, head_fn=None)}
        hook = self.hook_factory(contexts, stats)
        conn.execute("BEGIN")
        try:
            with refusal_pages_off():
                res = hook.raw(conn, ev)
        finally:
            conn.rollback()
        return ("pass" if (res or {}).get("status") == "shadow" else hook_reason(res)), mcap

    def _start_verdict(self, payload):
        def check():
            row = {"tx_hash": payload["tx_hash"], "mint": payload["mint"], "verdict": None,
                   "mcap": None, "mcap_basis": None, "hook_ms": None, "stats": 0}
            try:
                stats = self.fresh_stats_fn(payload["mint"])
                if isinstance(stats, dict) and stats:
                    row["stats"] = 1
                    conn = self.ro_connect()
                    try:
                        t0 = time.perf_counter()
                        row["verdict"], row["mcap"] = self.verdict(conn, payload, stats)
                        row["mcap_basis"] = SHADOW_MCAP_BASIS
                        row["hook_ms"] = (time.perf_counter() - t0) * 1000.0
                    finally:
                        conn.close()
                else:
                    row["verdict"] = "no_market_stats"
            except Exception:
                logger.exception("bsc fast lane: shadow verdict failed (measurement only)")
                row["verdict"] = "error"
            if self.verdict_sink is not None:
                try:
                    self.verdict_sink(row)
                except Exception:
                    logger.exception("bsc fast lane: verdict record failed")
        import threading
        thread = threading.Thread(target=check, name="bsc-fast-verdict", daemon=True)
        self._checks.append(thread)
        thread.start()

    def join_checks(self, timeout=5.0):
        for thread in self._checks:
            thread.join(timeout)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS bsc_lane_events (
    id INTEGER PRIMARY KEY, tx_hash TEXT, mode TEXT, mint TEXT, pool TEXT, quote TEXT, reason TEXT,
    sent_epoch REAL, recv_epoch REAL, handoff_ms REAL, busy_ms REAL, cpu_ms REAL, lock_busy INTEGER,
    clip_wei TEXT, min_out TEXT, tx_bytes INTEGER, written_at TEXT);
CREATE TABLE IF NOT EXISTS bsc_lane_verdicts (
    id INTEGER PRIMARY KEY, tx_hash TEXT, mint TEXT, verdict TEXT, mcap REAL, mcap_basis TEXT,
    hook_ms REAL, stats INTEGER, written_at TEXT);
CREATE TABLE IF NOT EXISTS bsc_lane_beat (
    at TEXT NOT NULL, signals INTEGER, drops TEXT, dex_calls INTEGER, dex_429 INTEGER, dex_stopped TEXT,
    lane_json TEXT);
"""


def ensure_beat_schema(db_path):
    """The beat table with its lane_json column, added to a table made before 2026-10-02. Run ONCE at
    lane build, before the listener starts, so the listener never holds a schema change between
    datagrams (Alpha). Idempotent."""
    import sqlite3
    db = sqlite3.connect(db_path, timeout=2)
    try:
        db.executescript(_SCHEMA)
        if "lane_json" not in [r[1] for r in db.execute("PRAGMA table_info(bsc_lane_beat)")]:
            try:
                db.execute("ALTER TABLE bsc_lane_beat ADD COLUMN lane_json TEXT")
            except sqlite3.OperationalError as exc:    # another process added it first
                if "duplicate column" not in str(exc):
                    raise
        db.commit()
    finally:
        db.close()


def lane_counters(live):
    """The live lane's paced host and wallet snapshot counters (cumulative since start), or None while
    the lane is runner_shadow (no paced host, no snapshot): unknown, never zero. Plain reads of known
    keys, so another thread's increment never breaks the copy."""
    if live is None:
        return None
    out = {"presignal_warm_enabled": int(getattr(live, "presignal_warm_enabled", False))}
    crowded = getattr(live, "crowded", None)
    if crowded is not None:                  # the crowded gas test: arms, not-eligible, spend, cap
        status = dict(crowded.status)
        out["crowded_gas"] = dict(status, not_eligible=dict(status.get("not_eligible") or {}))
    # C1 intents: started, claimed, expired_unused (no buy followed), busy, duplicate, rate_capped,
    # cached, host_dead, error.
    out.update({"intent_" + k: v for k, v in dict(getattr(live, "intent_counts", None) or {}).items()})
    low = getattr(live, "low_rtt", None)
    if low is not None:
        out["low_rtt"] = low.status()        # per family: next host, low_rtt / fallback / lane_host
    paced = getattr(live, "paced", None)
    if paced is not None:
        out.update(paced_calls=paced.counts.get("calls", 0), paced_http_429=paced.counts.get("http_429", 0),
                   # dead-host counters (Beta 230aed62e): failures, times marked dead, dead now
                   paced_transport_errors=paced.counts.get("transport_errors", 0),
                   paced_dead_marked=paced.counts.get("dead_marked", 0), paced_dead=int(paced.dead()))
    snapshot = getattr(live, "snapshot", None)
    if snapshot is not None:
        out.update(snapshot_reads=snapshot.counts.get("reads", 0),
                   snapshot_errors=snapshot.counts.get("errors", 0))
    cache = getattr(live, "cache", None)
    if cache is not None:
        for key in ("candidate_evicted", "candidate_expired", "candidate_identity_changed",
                    "preparation_cancelled", "preparation_skipped", "preparation_request_attempts",
                    "verdicts_ok", "verdicts_unsafe", "verdict_errors",
                    "unknown_first_encounter", "safety_hit", "safety_miss", "ready_at_original_ingress",
                    "warm_set", "warm_routed"):
            out[key] = cache.counts.get(key, 0)
        lock = getattr(cache, "_lock", None)
        if lock is not None and getattr(cache, "lookups", None) is not None:
            with lock:
                out["warm_tiers"] = {"lookups": dict(cache.lookups),
                                     "hit_age_s": {k: list(v) for k, v in cache.hit_age_s.items()}}
        out.update(cache.preparation_counts(now=time.time(), generation=live.generation_fn()))
    return out


def take_warm_stats(wait_s=None, *, pacer=None, sleep=time.sleep, clock=time.monotonic):
    """The process-wide DexScreener warm slot (memecoin_rh_fast_lane.WARM_STATS, shared with 4663's
    warmer), waited for on the background worker only. False when none came within `wait_s`."""
    if pacer is None:
        from sniper.services.memecoin_rh_fast_lane import WARM_STATS as pacer
    deadline = clock() + (WARM_STATS_WAIT_S if wait_s is None else wait_s)
    while True:
        if pacer.take():
            return True
        if clock() >= deadline:
            return False
        sleep(0.05)


def lane_routes(db_path, mints):
    """{mint: (quote, pool)} from each mint's latest signal in the lane's own file (read-only). A mint
    the lane never routed has no entry: the lane could not buy it, so it is not warmed."""
    import sqlite3
    mints = sorted({str(m).lower() for m in mints})
    if not mints:
        return {}
    db = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=0.25)
    try:
        rows = db.execute(
            "SELECT lower(mint), lower(quote), lower(pool) FROM bsc_lane_events WHERE pool IS NOT NULL "
            "AND quote IS NOT NULL AND lower(mint) IN (%s) ORDER BY id" % ",".join("?" * len(mints)),
            mints).fetchall()
    finally:
        db.close()
    return {mint: (quote, pool) for mint, quote, pool in rows}


def warm_entries(warm, routes, now):
    """The shared armed warm set as this cache's candidates: tier 0 (decided on in the last hour),
    tier 1 (decided on or sold within the window), each with its lane route."""
    hour = warm.get("hour") or set()
    out = []
    for mint in sorted(hour | (warm.get("recent") or set())):
        if mint in routes:
            quote, pool = routes[mint]
            out.append({"mint": mint, "quote": quote, "pool": pool,
                        "priority": 0 if mint in hour else 1, "observed_at": now})
    return out


def feed_warm_set(cache, warm, routes, *, ledger_path, db_path, now):
    """One feeder pass: refresh the shared set (an unreadable ledger keeps the last one), the routes (an
    unreadable lane file keeps the last ones), then the candidates. Returns the routes now held."""
    from sniper.services.memecoin_rh_fast_decide import refresh_warm_set
    refresh_warm_set(warm, ledger_path, now, chain=CHAIN)
    try:
        routes = lane_routes(db_path, (warm.get("hour") or set()) | (warm.get("recent") or set()))
    except Exception:
        logger.warning("bsc fast lane: warm routes unreadable; keeping the last ones", exc_info=True)
    entries = warm_entries(warm, routes, now)
    cache.warm_tier = {e["mint"]: e["priority"] for e in entries}
    cache.note_candidates(entries, now=now)
    cache.counts["warm_set"] = len((warm.get("hour") or set()) | (warm.get("recent") or set()))
    cache.counts["warm_routed"] = len(entries)
    return routes


def _write(db_path, sql, values):
    import sqlite3
    db = sqlite3.connect(db_path, timeout=2)
    try:
        db.executescript(_SCHEMA)
        db.execute(sql, values)
        db.commit()
    finally:
        db.close()


def write_event(db_path, rec, mode):
    from sniper.util.clock import real_utc_now_iso
    _write(db_path, "INSERT INTO bsc_lane_events (tx_hash, mode, mint, pool, quote, reason, "
           "sent_epoch, recv_epoch, handoff_ms, busy_ms, cpu_ms, lock_busy, clip_wei, min_out, "
           "tx_bytes, written_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (rec["tx_hash"], mode, rec["mint"], rec.get("pool"), rec.get("quote"), rec["reason"],
            rec["sent_epoch"], rec["recv_epoch"], rec["handoff_ms"], rec["busy_ms"], rec["cpu_ms"],
            rec["lock_busy"], None if rec["clip_wei"] is None else str(rec["clip_wei"]),
            None if rec["min_out"] is None else str(rec["min_out"]), rec["tx_bytes"],
            real_utc_now_iso()))


def verdict_sink(db_path):
    def sink(row):
        from sniper.util.clock import real_utc_now_iso
        _write(db_path, "INSERT INTO bsc_lane_verdicts (tx_hash, mint, verdict, mcap, mcap_basis, "
               "hook_ms, stats, written_at) VALUES (?,?,?,?,?,?,?,?)",
               (row["tx_hash"], row["mint"], row["verdict"], row["mcap"], row.get("mcap_basis"),
                row["hook_ms"], row["stats"], real_utc_now_iso()))
    return sink


class BscDispatch:
    """One heard datagram: authenticated by the kernel (our uid, the 56 watcher's pid from its own
    heartbeat), judged once, recorded in the lane's own SQLite file (never the funded ledger)."""

    def __init__(self, *, shadow, db_path, beat, mode="runner_shadow", host_clock=time.time,
                 candidate_identity=None, presignal_warm_enabled=False):
        import collections
        self.shadow, self.db_path, self.beat, self.mode = shadow, db_path, beat, mode
        self.host_clock, self.seen = host_clock, Seen()
        self.candidate_identity = candidate_identity
        self.presignal_warm_enabled = bool(presignal_warm_enabled)
        self.drops, self.counts = collections.Counter(), collections.Counter()
        # THE RELAY PRE-BUY (spec 2026-10-14 bsc pre-buy §3.2, §3.6): its worker (knob on, live), the
        # 4663 feed detector's heartbeat (its pid alone sends a pre-buy), and each source signal's
        # (uid, mint) -> host time, so a pre-buy yields to a source fill that already arrived.
        self.prebuy, self.rh_beat = None, None
        self.source_seen = collections.OrderedDict()

    def on_datagram(self, payload, pid, uid, recv_epoch):
        if isinstance(payload, dict) and payload.get("message_kind") == "intent_v1":
            return self._on_intent(payload, pid, uid, recv_epoch)
        if isinstance(payload, dict) and payload.get("message_kind") == "candidate_v1":
            if not self.presignal_warm_enabled:
                self.drops["candidate_warm_disabled"] += 1
                return None
            import os
            why = ("foreign_uid" if uid != os.getuid() else "not_the_tape" if
                   self.candidate_identity is None or self.candidate_identity.pid != pid else
                   candidate_refusal(payload, now=recv_epoch))
            if why is not None:
                self.drops[why] += 1
            elif self.mode == "live":
                self.shadow.cache.note_candidates([payload], now=self.host_clock())
                self.counts["candidates"] += 1
            return None
        if isinstance(payload, dict) and payload.get("kind") == "relay_prebuy_v1":
            return self._on_prebuy(payload, pid, uid, recv_epoch)
        why = check_sender(payload, pid, uid, watcher_pid=watcher_pid(self.beat, recv_epoch))
        if why is not None:
            self.drops[why] += 1
            return None
        if payload.get("route_kind") == "flap_shared" and not _flap_arrival_valid(payload["arrival_utc"], recv_epoch):
            self.drops["stale_flap"] += 1
            return None
        if not self.seen.first(payload):
            self.drops["duplicate"] += 1
            return None
        self.counts["signals"] += 1
        if self.prebuy is not None:                  # a dict write: the pre-buy's collision guard
            key = (str(payload.get("source_user_id") or ""), str(payload["mint"]).lower())
            self.source_seen.pop(key, None)
            self.source_seen[key] = recv_epoch
            while len(self.source_seen) > 512:
                self.source_seen.popitem(last=False)
        rec = self.shadow.handle(payload, recv_epoch=recv_epoch)
        if self.mode == "live":                      # the event row keys on the SOURCE tx
            from sniper.services.memecoin_rh_fast_lane import note_prebuy_source
            note_prebuy_source(self.shadow, payload, rec)
            write_event(self.db_path, dict(rec, tx_hash=payload["tx_hash"]), self.mode)
            write_live(self.db_path, rec)
        else:
            write_event(self.db_path, rec, self.mode)
        return rec

    def _on_intent(self, payload, pid, uid, recv_epoch):
        """C1 (2026-10-16): the 56 watcher's note at a source log's arrival, before its receipt decode.
        Authenticated like a signal (our uid, the watcher's pid from its own heartbeat); live lane and
        MEMECOIN_EVM_LANE_INTENT_WARM_CHAINS only. The lane starts reads; nothing is decided here."""
        import os
        from sniper.settings import memecoin as cfg
        mint = str(payload.get("mint") or "").lower()
        why = ("foreign_uid" if uid != os.getuid()
               else "intent_not_the_watcher" if pid != watcher_pid(self.beat, recv_epoch)
               else "intent_off" if (self.mode != "live"
                                     or CHAIN not in cfg.memecoin_evm_lane_intent_warm_chains())
               else "intent_malformed" if (len(mint) != 42 or not mint.startswith("0x")
                                           or any(c not in "0123456789abcdef" for c in mint[2:]))
               else None)
        warm = getattr(self.shadow, "warm_intent", None)
        if why is None and warm is None:
            why = "intent_no_lane"
        if why is not None:
            self.drops[why] += 1
            return None
        self.counts["intents"] += 1
        pair = str(payload.get("pair") or "").lower()
        if len(pair) != 42 or not pair.startswith("0x") or any(c not in "0123456789abcdef" for c in pair[2:]):
            pair = None                      # unnamed or malformed: today's quote-only binding
        return warm(mint, pair=pair)

    def _on_prebuy(self, payload, pid, uid, recv_epoch):
        """A Relay pre-buy: authenticated by the 4663 feed detector's pid (its own heartbeat), never the
        56 watcher's; well formed; once per order; then queued to the worker (no read, no lock here)."""
        import os
        from sniper.services.memecoin_bsc_relay_prebuy import bsc_prebuy_malformed
        from sniper.services.memecoin_rh_fast_lane import detector_pid
        if uid != os.getuid():
            why = "foreign_uid"
        elif self.prebuy is None:
            why = "prebuy_off"
        else:
            feed = detector_pid(self.rh_beat, recv_epoch) if self.rh_beat else None
            why = ("no_rh_feed" if feed is None else "not_the_rh_feed" if pid != feed
                   else bsc_prebuy_malformed(payload))
        if why is not None:
            self.drops[why] += 1
            return None
        if not self.seen.first(payload):
            self.drops["duplicate"] += 1
            return None
        self.counts["prebuys"] += 1
        return self.prebuy.offer(payload, recv_epoch)

    def source_fill_seen(self, uid, mint, *, since):
        """The host time the source's own signal for (uid, mint) reached this lane at or after `since`,
        else None (the pre-buy worker reads; this listener writes)."""
        at = self.source_seen.get((str(uid or ""), str(mint).lower()))
        return at if at is not None and at >= since else None


def build_bsc_lane(contexts, *, log, mode, root=None, ro_connect=None, dex_base=None,
                   hook_factory=None, connect=None, sender=None, env=None, http_base=None):
    """The 56 lane's objects for runner_shadow or live, or None when off, when there is no 56
    context, or when live has no host of its own (one log line). Nothing here starts a thread or
    opens a connection."""
    import os
    from sniper.execution.exec_lock import evm_exec_lock_key
    from sniper.services.memecoin_rh_fast_lane import DexBudget
    from sniper.settings import memecoin as cfg
    from sniper.util.clock import real_utc_now
    from sniper.util.state_paths import state_path, state_root
    if mode not in ("runner_shadow", "live"):
        return None
    ctx = (contexts or {}).get(CHAIN)
    address = ctx.get("address") if isinstance(ctx, dict) else None
    if not address:
        log("[bsc-fast-lane] %s set but no 56 context: lane off" % mode)
        return None
    url = cfg.memecoin_bsc_fast_lane_rpc_url() if mode == "live" else None
    if mode == "live":
        why = lane_host_refusal(url, dict(os.environ if env is None else env))
        if why is not None:
            log("[bsc-fast-lane] live REFUSED at start: %s. The lane is off; today's path covers "
                "every 56 signal." % why)
            return None
    root = state_root() if root is None else root
    if ro_connect is None:
        from sniper.persistence.db_core import get_readonly_connection
        ro_connect = get_readonly_connection
    db_path = str(state_path("data", "memecoin_bsc_fast_lane.db"))
    try:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        ensure_beat_schema(db_path)
    except Exception:
        # Observability only: the lane runs; a beat row then fails loudly on its own write.
        logger.exception("bsc fast lane: beat table schema not ensured")
    dex = DexBudget(dex_base)
    key = evm_exec_lock_key(CHAIN, address)
    shadow = BscShadow(
        lock_key=key, lock_dir=None, taker=address,
        now_fn=real_utc_now, clip_usd=cfg.MEMECOIN_BSC_FAST_LANE_PILOT_CLIP_USD,
        bnb_usd_max_age_s=cfg.MEMECOIN_RH_FAST_LANE_ETH_USD_MAX_AGE_S, ctx=ctx, contexts=contexts,
        ro_connect=ro_connect, hook_factory=hook_factory,
        fresh_stats_fn=lambda mint: dex.stats(mint, network_id=CHAIN),
        verdict_sink=verdict_sink(db_path))
    live = None
    if mode == "live":
        from sniper.services.memecoin_alerts import send_alert
        live = build_live(contexts, ctx, log=log, lock_key=key, db_path=db_path, url=url,
                          connect=connect, sender=sender, dex=dex, hook_factory=hook_factory,
                          http_base=http_base)
        if live is None:
            return None
        live.alert_fn = lambda text: send_alert("bsc_fast_lane", text)
    beat = str(state_path("data", "onchain_evm_watch.%s.heartbeat" % CHAIN))
    dispatch = BscDispatch(shadow=live or shadow, db_path=db_path, beat=beat, mode=mode,
                           candidate_identity=CandidateIdentity(root),
                           presignal_warm_enabled=presignal_warm_enabled())
    if live is not None:
        live.presignal_warm_enabled = dispatch.presignal_warm_enabled
    prebuy = None
    if live is not None and cfg.MEMECOIN_BSC_RELAY_PREBUY:
        from sniper.services.memecoin_bsc_relay_prebuy import build_bsc_prebuy_worker
        dispatch.rh_beat = str(state_path("data", "memecoin_rh_feed_shadow.heartbeat"))
        prebuy = build_bsc_prebuy_worker(live, dispatch, connect=connect, db_path=db_path, log=log)
    return {"dispatch": dispatch, "shadow": shadow, "live": live, "dex": dex, "db_path": db_path,
            "address_name": lane_address(root), "mode": mode, "prebuy": prebuy}


_WARM_MINT = "0x" + "b5c0" * 10                   # a coin no ledger row names


def warm_bsc_hook(shadow, *, log):
    """One read-only, rolled-back shadow verdict at lane start, off any signal: the funded hook's
    first call in a process is cold (4663 measured 376 ms; warm p50 12 ms), so a live signal never
    pays it. Never raises into the lane."""
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_evm_onchain_watch import direct_buy_from_receipt, TRANSFER_TOPIC
    from sniper.services.memecoin_real_runner import _band_source_buy_ratio
    from sniper.settings import memecoin as cfg
    from sniper.util.clock import real_utc_now_iso
    try:
        conn = shadow.ro_connect()
        try:
            armed = sorted(m.real_approved_user_ids(conn, network_id=CHAIN) or ())
            now = time.time()
            uid = armed[0] if armed else "bsc-warm"
            warm_mcap = 6000.0
            ratio = float(_band_source_buy_ratio(
                cfg.MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND or {}, CHAIN, warm_mcap,
                cfg.MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO))
            floor, ceiling = float(cfg.MEMECOIN_REAL_MIN_SOURCE_BUY_USD), float(cfg.MEMECOIN_REAL_MAX_SOURCE_BUY_USD)
            if not all(math.isfinite(v) for v in (ratio, floor, ceiling)):
                raise ValueError("unknown source-buy warm bounds")
            source_usd = max(60.0, floor, warm_mcap * max(0.0, ratio))
            if ceiling > 0:
                source_usd = min(source_usd, ceiling)  # contradictory bounds keep the real decline
            source_in = math.ceil(Fraction(str(source_usd)) * 10 ** 18 / 600)
            if ceiling > 0 and source_in / 10 ** 18 * 600 > ceiling:
                # Find the largest atomic payment whose actual decoder value respects the cap.
                low, high = 0, source_in
                for _ in range(source_in.bit_length()):
                    if low >= high:
                        break
                    mid = (low + high + 1) // 2
                    if mid / 10 ** 18 * 600 <= ceiling:
                        low = mid
                    else:
                        high = mid - 1
                source_in = low
            source_out = source_in * 10 ** 8  # preserve the synthetic $6e-6 unit price
            # Startup-only synthetic receipt. It is consumed solely by the rolled-back shadow
            # verdict below; the real source guard and incoming datagrams stay unchanged.
            wallet, router = "0x" + "b5c1" * 10, "0x" + "b5c2" * 10
            tx_hash, block_hash = "0x" + "b5c3" * 16, "0x" + "b5c4" * 16
            tx = dict(hash=tx_hash, blockHash=block_hash, blockNumber="0x1", chainId="0x38",
                      to=router, value=hex(source_in), **{"from": wallet})
            receipt = dict(transactionHash=tx_hash, blockHash=block_hash, blockNumber="0x1",
                           status="0x1", to=router, **{"from": wallet}, logs=[{
                               "address": _WARM_MINT, "topics": [TRANSFER_TOPIC,
                                 "0x" + router[2:].rjust(64, "0"), "0x" + wallet[2:].rjust(64, "0")],
                               "data": hex(source_out)}])
            buy = direct_buy_from_receipt(receipt, tx, wallet, weth=WBNB, quote_usd=600.0,
                                           decimals_fn=lambda _: 18, chain=CHAIN)
            if not buy or not buy.get("source_initiation"):
                raise ValueError("synthetic warm receipt failed source attribution")
            payload = dict(tx_hash=tx_hash, mint=_WARM_MINT, pool="0x" + "0" * 40,
                           quote=WBNB, r_coin=10 ** 27, r_quote=10 ** 19, src_in=source_in,
                           src_out=source_out, source_user_id=uid, wallet=wallet, seq=1,
                           source_initiation=dict(buy["source_initiation"], source_user_id=uid),
                           handle="warm", arrival_utc=real_utc_now_iso(), paid_usd=buy["source_size_usd"],
                           source_price=buy["source_price"])
            stats = dict(price=6e-06, fdv=warm_mcap, volume_m5=50000.0, volume_h1=200000.0,
                         txns_m5_buys=300, txns_m5_sells=100, liquidity=30000.0,
                         pair_created_at=(now - 3 * 3600) * 1000.0)
            t0 = time.perf_counter()
            verdict, _mcap = shadow.verdict(conn, payload, stats)
            ms = (time.perf_counter() - t0) * 1000.0
        finally:
            conn.close()
    except Exception as exc:
        logger.exception("bsc fast lane: hook warm failed")
        log("[bsc-fast-lane] hook warm failed (%s): the first signal pays the cold call"
            % type(exc).__name__)
        return None, None
    log("[bsc-fast-lane] hook warm: %.0f ms (%s)" % (ms, verdict))
    return ms, verdict


def write_beat(lane):
    from sniper.util.clock import real_utc_now_iso
    dispatch, dex = lane["dispatch"], lane["dex"]
    drops, dispatch.drops = dict(dispatch.drops), type(dispatch.drops)()
    counters = lane_counters(lane.get("live"))
    prebuy = lane.get("prebuy")
    if prebuy is not None and counters is not None:
        counters["prebuy"] = dict(prebuy.beat(), prebuys=dispatch.counts["prebuys"])
        try:
            prebuy.flush_notes(lane["db_path"])
        except Exception:
            logger.exception("bsc relay prebuy: source fill notes not written")
    _write(lane["db_path"], "INSERT INTO bsc_lane_beat VALUES (?,?,?,?,?,?,?)",
           (real_utc_now_iso(), dispatch.counts["signals"], json.dumps(drops, sort_keys=True),
            dex.counts["calls"], dex.counts["http_429"], dex.stopped,
            json.dumps(counters, sort_keys=True) if counters is not None else None))


def start_bsc_lane(contexts, *, log, root=None):
    """Start the 56 lane when MEMECOIN_BSC_FAST_LANE is runner_shadow or live and a 56 context
    exists: the datagram listener (after one hook warm) and, live, the wallet snapshot, the safety
    cache and the members' keep-alive, every pre-signal read on the lane's own host. Returns the
    listener thread, or None. Never raises into the runner."""
    import threading
    from sniper.services.memecoin_rh_fast_lane import (
        LaneSocket, lane_dex_base, apply_stop_file, stop_file_path)
    from sniper.settings import memecoin as cfg
    try:
        lane = build_bsc_lane(contexts, log=log, mode=cfg.memecoin_bsc_fast_lane_mode(), root=root,
                              dex_base=lane_dex_base(None))
        if lane is None:
            return None
    except Exception:
        logger.exception("bsc fast lane: start failed (lane off)")
        return None
    live = lane["live"]
    if live is not None:
        apply_stop_file(live, stop_file_path(), log)

    def stop_watch():
        while True:
            apply_stop_file(live, stop_file_path(), log)
            time.sleep(1.0)

    def run():
        warm_bsc_hook(lane["shadow"], log=log)
        sock, logged, flushed = None, False, time.monotonic()
        while True:
            try:
                if time.monotonic() - flushed >= 60:
                    flushed = time.monotonic()
                    write_beat(lane)
                if sock is None:
                    try:
                        sock = LaneSocket(lane["address_name"])
                    except OSError as exc:              # a deploy overlap still holds the name
                        if not logged:
                            log("[bsc-fast-lane] lane socket name in use (%s); retrying" % exc)
                            logged = True
                        time.sleep(1.0)
                        continue
                got = sock.recv(1.0)
                if got is None:
                    continue
                payload, pid, uid = got
                lane["dispatch"].on_datagram(payload, pid, uid, time.time())
            except Exception:
                logger.exception("bsc fast lane: signal handling failed (lane keeps running)")
                time.sleep(0.1)

    def snapshot_loop():
        while True:
            if lane["dispatch"].presignal_warm_enabled:
                lane["dispatch"].candidate_identity.refresh()
            live.snapshot.poll_once()                  # never raises; a failure ages out
            time.sleep(cfg.MEMECOIN_BSC_FAST_LANE_SNAPSHOT_PERIOD_S)

    def safety_loop():
        while True:
            try:
                mint = live.cache.next_due(time.time())
                if mint is None:
                    time.sleep(0.2)
                    continue
                live.cache.run_one(mint, now=time.time())
            except Exception:
                logger.exception("bsc fast safety: verdict loop failed (keeps running)")
                time.sleep(1.0)

    def keepalive():
        while True:
            live.sender.warm()
            time.sleep(45.0)

    def warm_set_loop():
        from sniper.persistence.db_core import get_db_path
        warm, routes = {"hour": set(), "recent": set()}, {}
        while True:
            try:
                routes = feed_warm_set(live.cache, warm, routes, ledger_path=get_db_path(),
                                       db_path=lane["db_path"], now=time.time())
            except Exception:
                logger.exception("bsc fast lane: warm set pass failed (keeps running)")
            time.sleep(WARM_SET_PERIOD_S)

    threads = [threading.Thread(target=run, name="bsc-fast-lane", daemon=True)]
    if live is not None:
        threads += [threading.Thread(target=snapshot_loop, name="bsc-fast-snapshot", daemon=True),
                    threading.Thread(target=safety_loop, name="bsc-fast-safety", daemon=True),
                    threading.Thread(target=keepalive, name="bsc-fast-members", daemon=True),
                    threading.Thread(target=stop_watch, name="bsc-fast-stop", daemon=True)]
        if lane["dispatch"].presignal_warm_enabled:
            threads.append(threading.Thread(target=warm_set_loop, name="bsc-fast-warmset", daemon=True))
    for thread in threads:
        thread.start()
    if lane.get("prebuy") is not None:
        lane["prebuy"].start()
    if live is not None:
        log("[bsc-fast-lane] LIVE started: $%.0f pilot clip, %d fills, %d sends an hour; safety, "
            "facts and snapshot on the lane's own host" % (
                cfg.MEMECOIN_BSC_FAST_LANE_PILOT_CLIP_USD, cfg.MEMECOIN_BSC_FAST_LANE_PILOT_FILLS,
                cfg.MEMECOIN_BSC_FAST_LANE_MAX_SENDS_PER_HOUR))
    else:
        log("[bsc-fast-lane] runner_shadow started: no signing, no reservation, no send, no chain "
            "call")
    return threads[0]


# ================================================================================================
# STEP 3, LIVE (MEMECOIN_BSC_FAST_LANE=live; built now, RUN only once the operator's own 56 RPC key
# is set as MEMECOIN_BSC_FAST_LANE_RPC_URL: Alpha 2026-10-02). The lane's safety cache, coin facts
# (decimals, chain totalSupply) and wallet snapshot read ONLY that host, refused at start when any
# other role uses it. Before the reservation everything is in memory; then the runner's own funded
# hook (every gate), the 4663 lane's executor with its chain pieces overridden here: the native-BNB
# PancakeSwap V2 buy, the nonce through the shared floor, the write-ahead, one parallel send of the
# identical bytes to 48 Club Puissant, BlockRazor (with the operator's token) and the public
# members, the wallet lock held through the receipt (capped), the receipt booking with the
# block-pinned balance check, the delta fallback and the refused-booking alert.
# ================================================================================================


PUISSANT_URL = "https://puissant-builder.48.club/"
PRIVATE_HOSTS = frozenset({"puissant-builder.48.club"})     # builders that do not gossip a send
BLOCKRAZOR_URL = "https://virginia.builder.blockrazor.io"
# Refusals no other copy of the same bytes can overturn. A balance, fee-cap or base-fee refusal is
# one node's view of state another member or a later block can see differently: ambiguous here.
_STATELESS_REJECT = ("intrinsic gas too low", "invalid sender", "invalid chain id",
                     "transaction type not supported", "oversized data", "exceeds block gas limit",
                     "intent_unwritable")              # EVM-08: that post sent nothing


def lane_float_bounds(bounds, *, mode=None, float_wei=None, refill_below_wei=None):
    """The 56 gas pass's bounds with the lane's native float (Alpha 2026-10-02, option (b)). While
    MEMECOIN_BSC_FAST_LANE=live the float is added above the full gas target. A previously valid
    refill mark keeps at least low-water + float ready before gas eats into a full native clip.
    Any other mode, or a zero float, returns `bounds` unchanged."""
    from sniper.settings import memecoin as cfg
    mode = cfg.memecoin_bsc_fast_lane_mode() if mode is None else mode
    float_wei = cfg.MEMECOIN_BSC_FAST_LANE_FLOAT_WEI if float_wei is None else int(float_wei)
    refill = (cfg.MEMECOIN_BSC_FAST_LANE_FLOAT_REFILL_BELOW_WEI if refill_below_wei is None
              else int(refill_below_wei))
    if mode != "live" or float_wei <= 0:
        return bounds
    out = dict(bounds)
    float_floor = int(bounds["low_water_wei"]) + float_wei
    out["target_wei"] = int(bounds["target_wei"]) + float_wei
    if (int(bounds["low_water_wei"]) < refill < max(int(bounds["target_wei"]), float_floor)
            and max(refill, float_floor) < out["target_wei"]):
        out["low_water_wei"] = max(refill, float_floor)
    return out


def lane_host_refusal(url, env):
    """None when `url` may be the lane's own 56 host, else why not: an https URL whose host no other
    role uses (rule 0724bc08: every other environment URL, the funded EVM read pools and the
    free-submission registry, via taken_budgets)."""
    from urllib.parse import urlsplit
    from sniper.services.memecoin_evm_exact_capture import budget, taken_budgets
    if not url:
        return "no MEMECOIN_BSC_FAST_LANE_RPC_URL"
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return "MEMECOIN_BSC_FAST_LANE_RPC_URL is not an https URL"
    # By registrable domain, as the capture lane's guard: providers budget per account or IP
    # across their hostnames. Host only in the reason: a keyed URL carries its key in the path.
    # The tape's hop seed is the keyed dRPC 56 research variable, an env var: taken like any other.
    # OPERATOR DECISION 2026-10-02 (via Alpha): the 4663 inline safety host
    # (MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL) shares this lane's Chainstack account. Accepted costs:
    # one account's RPS (a ~10-call inline burst can 429 this lane, which then pauses 120 s and its
    # signals fall back to today's path, and the reverse) and one monthly quota. Exactly that key is
    # excused; any other key on the same domain still refuses, and the RH side's full-URL check
    # (safety_host_refusal) still refuses the inline host being this lane's very URL.
    others = {k: v for k, v in env.items()
              if k not in ("MEMECOIN_BSC_FAST_LANE_RPC_URL", "MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL")}
    if budget(url) in taken_budgets(others):
        return ("MEMECOIN_BSC_FAST_LANE_RPC_URL's domain %s is used by another role or a funded "
                "pool" % budget(url))
    return None


def lane_only_http(paced, url):
    """The http seam of everything the lane reads before a signal: its own host through its paced
    budget. Any other URL raises (a safety check then fails closed): nothing the cache or the
    snapshot runs can reach another host."""
    def http(method, target, headers=None, body=None, timeout=10):
        if target != url:
            raise RuntimeError("bsc fast lane: a read off the lane's own host was refused")
        return paced(method, target, headers=headers, body=body, timeout=timeout)
    return http


class LaneHost:
    """One JSON-RPC call on the lane's own host; an error answer raises."""

    def __init__(self, url, http):
        self.url, self.http = url, http

    def __call__(self, method, params):
        _status, answer, _h = self.http("POST", self.url, body=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}))
        if not isinstance(answer, dict) or "result" not in answer:
            raise RuntimeError("lane host %s failed: %s" % (method, str((answer or {}).get(
                "error") if isinstance(answer, dict) else answer)[:80]))
        return answer["result"]

    def batch(self, calls):
        """Several calls in ONE request (BSC cut 4, 2026-10-14), their results in call order, paired
        by id, never by position. Any error item, missing, extra or repeated id, 429 or malformed
        answer raises, as a single call's would. None only when the host answers that batching is
        unsupported (the evm_dex_quote rule): the caller then makes today's single calls."""
        from sniper.execution.evm_dex_quote import _batch_unsupported
        status, answer, _h = self.http("POST", self.url, body=json.dumps(
            [{"jsonrpc": "2.0", "id": i, "method": method, "params": params}
             for i, (method, params) in enumerate(calls, 1)]))
        if not isinstance(answer, list):
            if _batch_unsupported(status, answer):
                return None
            raise RuntimeError("lane host batch failed (status %s): %s" % (status, str(
                (answer or {}).get("error") if isinstance(answer, dict) else answer)[:80]))
        by_id = {}
        for item in answer:
            rid = item.get("id") if isinstance(item, dict) else None
            if type(rid) is not int or rid in by_id:
                raise RuntimeError("lane host batch: malformed or repeated reply id")
            by_id[rid] = item
        if set(by_id) != set(range(1, len(calls) + 1)):
            raise RuntimeError("lane host batch: replies do not match the calls")
        results = []
        for rid, (method, _params) in enumerate(calls, 1):
            if "result" not in by_id[rid]:
                raise RuntimeError("lane host %s failed: %s" % (method, str(by_id[rid].get("error"))[:80]))
            results.append(by_id[rid]["result"])
        return results


class Snapshot:
    """The wallet's native BNB, WBNB, pending nonce and gas price on the lane's own host, read every
    period off the signal path. `current()` is the last complete read (host clock `read_at`) or
    None; a failed read keeps the previous one, which ages out by the snapshot rule."""

    def __init__(self, host, address, *, clock=time.time, weth=WBNB, lane="bsc"):
        import collections
        import threading
        self.host, self.address, self.clock = host, str(address).lower(), clock
        self.weth = str(weth).lower()             # the chain's wrapped native (Base lane: WETH)
        self.lane = lane                          # names the lane in the warning (Base reuses this class)
        self._last, self._lock = None, threading.Lock()
        self.refreshing = threading.Event()
        self.counts = collections.Counter()

    def poll_once(self):
        from sniper.execution.evm_wallet import SEL_BALANCE_OF
        started = self.clock()
        self.refreshing.set()
        try:
            native = int(self.host("eth_getBalance", [self.address, "latest"]), 16)
            wbnb = int(self.host("eth_call", [{"to": self.weth, "data": "0x" + SEL_BALANCE_OF
                                               + self.address[2:].rjust(64, "0")}, "latest"]), 16)
            nonce = int(self.host("eth_getTransactionCount", [self.address, "pending"]), 16)
            gas = int(self.host("eth_gasPrice", []), 16)
        except Exception as exc:
            self.counts["errors"] += 1
            logger.warning("%s fast lane: snapshot read failed: %s", self.lane, type(exc).__name__)
            return None
        finally:
            self.refreshing.clear()
        snap = {"native_wei": native, "weth_wei": wbnb, "nonce": nonce, "gas_price_wei": gas,
                "read_at": started}
        with self._lock:
            self._last = snap
        self.counts["reads"] += 1
        return snap

    def current(self):
        with self._lock:
            return dict(self._last) if self._last else None


class PreparationReads:
    """One job's immutable read binding, including safety's child/late probe threads."""

    def __init__(self, cache, paced, url):
        self.cache, self.paced, self.url = cache, paced, url
        self.cancelled, self.closed = threading.Event(), threading.Event()
        self._requests = threading.Lock()  # this job's probe threads only; never essential work
        self.deadline = paced.clock() + min(60.0, max(0.0, cache.ttl_s))
        self.binding_fn = lambda: True

    def current(self, *, check_binding=True):
        return (not self.closed.is_set() and not self.cancelled.is_set()
                and self.paced.clock() < self.deadline
                and (not check_binding or self.binding_fn()))

    def available(self):
        # The pacer checks this while holding its account locks; generation reads stay outside.
        return self.current(check_binding=False) and self.cache.admission_fn()

    def wait_for_capacity(self):
        from sniper.execution.evm_rpc_pool import RequestNotSent
        while True:
            if not self.current():
                self.cancelled.set()
                raise RequestNotSent("bsc speculative read unavailable")
            if self.cache.admission_fn():
                return
            if not self.cache.deferral_fn():
                self.cancelled.set()
                raise RequestNotSent("bsc speculative read unavailable")
            remaining = self.deadline - self.paced.clock()
            if remaining <= 0:
                self.cancelled.set()
                raise RequestNotSent("bsc speculative job expired")
            self.paced.sleep(min(0.05, remaining))

    def __call__(self, method, target, headers=None, body=None, timeout=10):
        from sniper.execution.evm_rpc_pool import RequestNotSent
        from sniper.services.memecoin_rh_fast_lane import ResearchPaused
        class PaceUnavailable(RequestNotSent):
            pass  # only the pacer's pre-dispatch refusal may defer; provider errors never retry
        owned = False
        try:
            if target != self.url:
                raise RequestNotSent("bsc speculative read unavailable")
            try:
                payload = json.loads(body)
                requests = payload if isinstance(payload, list) else [payload]
                if (not requests or any(not isinstance(row, dict)
                        or not isinstance(row.get("method"), str) for row in requests)):
                    raise ValueError("invalid RPC request")
                if isinstance(payload, list):
                    ids = [row.get("id") for row in requests]
                    if any(type(i) is not int for i in ids) or len(set(ids)) != len(ids):
                        raise ValueError("invalid RPC batch ids")
            except (TypeError, ValueError) as exc:
                raise RequestNotSent("bsc speculative RPC request invalid") from exc
            while not owned:
                self.wait_for_capacity()
                owned = self._requests.acquire(timeout=0.05)
            self.cache.counts["preparation_request_attempts"] += 1
            while True:
                self.wait_for_capacity()
                remaining = self.deadline - self.paced.clock()
                if remaining <= 0:
                    raise RequestNotSent("bsc speculative job expired")
                try:
                    result = self.paced.try_background(
                        method, target, headers=headers, body=body,
                        timeout=min(timeout, remaining),
                        admission_fn=self.available, wait_for_pace=True, refusal_exc=PaceUnavailable)
                    break
                except PaceUnavailable:
                    if not self.current() or not self.cache.deferral_fn():
                        raise
            if result[0] != 200:
                raise ResearchPaused("bsc speculative provider unavailable")
            answer = result[1]
            if isinstance(payload, list):
                if (not isinstance(answer, list) or len(answer) != len(requests)
                        or any(not isinstance(row, dict) or type(row.get("id")) is not int
                               for row in answer)
                        or {row["id"] for row in answer} != set(ids)):
                    raise ResearchPaused("bsc speculative RPC batch unmatched")
                by_id = {row["id"]: row for row in answer}
                replies = [by_id[row["id"]] for row in requests]
            else:
                replies = [answer]
            for request, reply in zip(requests, replies):
                if not isinstance(reply, dict):
                    raise ResearchPaused("bsc speculative RPC result unavailable")
                error = reply.get("error")
                if request["method"] == "eth_createAccessList":
                    if (type(reply.get("id")) is not int
                            or reply["id"] != request.get("id")):
                        raise ResearchPaused("bsc speculative access-list response unmatched")
                    if (isinstance(error, dict) and type(error.get("code")) is int
                            and error["code"] == -32601 and "result" not in reply):
                        continue  # exact optional-method refusal; ordinary scans still prove slots
                    if error is None:
                        from sniper.execution.evm_token_safety import _validated_access_list_slots
                        try:
                            _validated_access_list_slots(reply.get("result"), request["params"][0]["to"])
                        except (KeyError, IndexError, TypeError, ValueError) as exc:
                            raise ResearchPaused("bsc speculative access-list result unavailable") from exc
                if error is not None:
                    # Explicit eth_call reverts are answers; provider failures remain unknown.
                    from sniper.execution.evm_real_seams import classify_evm_revert, _is_revert_evidence
                    data = error.get("data") if isinstance(error, dict) else None
                    message = error.get("message") if isinstance(error, dict) else None
                    if (request["method"] != "eth_call" or not isinstance(error, dict)
                            or "result" in reply or not _is_revert_evidence(
                                data, message, classify_evm_revert(data, message))):
                        raise ResearchPaused("bsc speculative RPC error unavailable")
                elif reply.get("result") is None:
                    raise ResearchPaused("bsc speculative RPC result unavailable")
            return result
        except Exception:
            self.cancelled.set()       # safety may catch the exception; publication still aborts
            raise
        finally:
            if owned:
                self._requests.release()


class BscSafetyCache(SafetyCache):
    """One bounded speculative worker; a completion belongs to its captured route/generation."""

    def __init__(self, *args, quotes, generation_fn, **kw):
        super().__init__(*args, **kw)
        self.quotes, self.generation_fn = quotes, generation_fn
        self.signal_decimals = {}               # mint -> the signal's decimals (sizes the sell probe)
        self.admission_fn = lambda: True
        self.deferral_fn = lambda: False
        self.http_factory = None
        self._identities, self._prepared_generation = {}, {}
        self._serial = 0
        # mint -> its armed warm tier (0, 1), replaced whole by the feeder: the signal's own
        # note_candidates sets priority 0 before reason(), so _priority cannot name the tier.
        self.warm_tier = {}
        self._job_lock = threading.Lock()       # worker only; never acquired by a buy
        from sniper.util.receipt_clock import clock_domain
        self.boot_id = clock_domain()           # load before any entry

    def _discard(self, mint):
        for values in (self._verdicts, self._stats, self._facts, self._facts_at,
                       self._retry_at, self._prepared_generation):
            values.pop(mint, None)
        warm = self._warmup.get(mint)
        if warm is not None:
            for key in tuple(warm):
                if not key.startswith("candidate_"):
                    warm.pop(key, None)

    def put(self, mint, *, now, facts=None, stats=None, reason=None):
        """Inline replacement has no prior worker completion; preserve one short entry lock."""
        mint = str(mint).lower()
        with self._lock:
            identity = self._identities.get(mint)
            if identity is not None:
                self._serial += 1
                self._identities[mint] = (*identity[:2], self._serial)
            self._prepared_generation.pop(mint, None)
            warm = self._warmup.get(mint, {})
            for key in tuple(warm):
                if not key.startswith("candidate_"):
                    warm.pop(key, None)
            self._verdicts[mint] = (True, reason, now, 0.0)
            self.counts["verdicts_inline"] += 1
            if facts and facts[0] is not None and facts[1]:
                self._facts[mint] = (int(facts[0]), int(facts[1]))
                self._facts_at[mint] = now
            if isinstance(stats, dict) and stats:
                self._stats[mint] = (dict(stats), now)

    def _expire(self, now):
        for mint, (_pri, at) in tuple(self._priority.items()):
            if now - at > CANDIDATE_TTL_S:
                self._drop(mint)
                self.counts["candidate_expired"] += 1

    def _drop(self, mint):
        self._discard(mint)
        for values in (self._priority, self._warmup, self._identities, self.quotes,
                       self.signal_decimals):
            values.pop(mint, None)

    def identity(self, mint):
        """(quote, pool) noted for the coin, or None."""
        with self._lock:
            noted = self._identities.get(str(mint).lower())
        return None if noted is None else noted[:2]

    def note_candidates(self, entries, *, now, generation=None):
        with self._lock:
            self._expire(now)
            for entry in entries:
                mint = str(entry["mint"]).lower()
                quote, pool = str(entry["quote"]).lower(), str(entry["pool"]).lower()
                previous = self._identities.get(mint)
                if previous is None or previous[:2] != (quote, pool):
                    if previous is not None:
                        self._discard(mint)
                        self.counts["candidate_identity_changed"] += 1
                    self._serial += 1
                    self._identities[mint] = (quote, pool, self._serial)
                if (generation is not None and mint in self._prepared_generation
                        and generation != self._prepared_generation[mint]):
                    self._discard(mint)
                    self.counts["preparation_cancelled"] += 1
                self.quotes[mint] = quote
                if "decimals" in entry:
                    self.signal_decimals[mint] = entry["decimals"]      # validated where it is used
                priority, observed = int(entry.get("priority", 99)), entry.get("observed_at", now)
                old = self._priority.get(mint)
                self._priority[mint] = (min(priority, old[0]) if old and now - old[1] < 60
                                        else priority, observed)
                warm = self._warmup.setdefault(mint, {"candidate_first_at": observed})
                warm["candidate_last_at"] = observed
            while len(self._priority) > CANDIDATE_LIMIT:
                mint = max(self._priority, key=lambda m: (
                    self._priority[m][0], -self._priority[m][1]))
                self._drop(mint)
                self.counts["candidate_evicted"] += 1

    def next_due(self, now):
        with self._lock:
            self._expire(now)
        if not self.admission_fn():
            return None
        with self._lock:
            # A cancelled preparation has no verdict, but still spent the worker's turn.
            due = [(pri, self._warmup.get(mint, {}).get("warm_worker_start_at",
                        self._verdicts[mint][2] if mint in self._verdicts else -1e18), mint)
                   for mint, (pri, _at) in self._priority.items()
                   if (mint not in self._verdicts or now - self._verdicts[mint][2] > self.ttl_s - (
                       self.refresh_lead_s if pri <= 0 else 0.0))
                   and now >= self._retry_at.get(mint, -1e18)]
        return min(due)[2] if due else None

    def preparation_counts(self, *, now, generation):
        ready = queued = 0
        with self._lock:
            for mint, (priority, _at) in self._priority.items():
                verdict, facts, stats = self._verdicts.get(mint), self._facts.get(mint), self._stats.get(mint)
                facts_at = self._facts_at.get(mint)
                ready += bool(verdict and verdict[0] and 0 <= now - verdict[2] <= self.ttl_s
                              and self._prepared_generation.get(mint) == generation
                              and facts and facts[1] and (facts_at is None or
                                  0 <= now - facts_at <= self.supply_ttl_s)
                              and stats and 0 <= now - stats[1] <= self.ttl_s)
                queued += bool((verdict is None or now - verdict[2] > self.ttl_s - (
                    self.refresh_lead_s if priority <= 0 else 0.0))
                    and now >= self._retry_at.get(mint, -1e18))
            return {"candidates_active": len(self._priority), "candidates_ready": ready,
                    "candidates_queued": queued}

    def run_one(self, mint, *, now):
        if not self.admission_fn() or not self._job_lock.acquire(blocking=False):
            self.counts["preparation_skipped"] += 1
            return None
        reads = None
        try:
            reads = self.http_factory() if self.http_factory is not None else None
            generation = self.generation_fn()
            with self._lock:
                identity = self._identities.get(mint)
                if (identity is None or mint not in self._priority
                        or now - self._priority[mint][1] > CANDIDATE_TTL_S
                        or type(generation) not in (int, float) or not math.isfinite(generation)):
                    self.counts["preparation_skipped"] += 1
                    return None
                warm = dict(self._warmup[mint])
                warm["warm_worker_start_at"] = now
                self._warmup[mint] = warm
                facts, facts_at = self._facts.get(mint), self._facts_at.get(mint)
            if reads is not None:
                def binding_current():
                    candidate = self._priority.get(mint)
                    return (identity == self._identities.get(mint)
                            and generation == self.generation_fn() and candidate is not None
                            and self.clock() - candidate[1] <= CANDIDATE_TTL_S)
                reads.binding_fn = binding_current
            factory = getattr(self.safety_fn, "for_quote", None)
            safety = (factory(mint, identity[0], request_http=reads) if factory is not None
                      else self.safety_fn)
            facts_fn = (self.facts_fn.for_http(reads) if reads is not None
                        and hasattr(self.facts_fn, "for_http") else self.facts_fn)
            def stats_fn(coin):
                if reads is not None:
                    reads.wait_for_capacity()        # refused at once when there is none
                if not take_warm_stats():
                    # Absent stats for this verdict (stats_missing), never a guess or a burst.
                    from sniper.services.memecoin_rh_fast_lane import ResearchPaused
                    raise ResearchPaused("warm stats slot unavailable")
                if reads is not None:
                    # Again after the slot wait (Gamma LOW 2): an entry that started meanwhile wins.
                    reads.wait_for_capacity()
                return self.stats_fn(coin)
            job = SafetyCache(safety, ttl_s=self.ttl_s, usd=self.usd, facts_fn=facts_fn,
                              stats_fn=stats_fn if self.stats_fn is not None else None,
                              supply_ttl_s=self.supply_ttl_s, clock=self.clock)
            if facts is not None and facts_at is not None:
                job._facts[mint], job._facts_at[mint] = facts, facts_at
            job.note_candidates([{"mint": mint}], now=now)
            job._warmup[mint] = warm
            requests_before = self.counts["preparation_request_attempts"]
            result = job.run_one(mint, now=now)
            if reads is not None:
                if (not reads.current()
                        or not (self.admission_fn() or self.deferral_fn())):
                    reads.cancelled.set()
                reads.closed.set()     # late probes retain this job, never a later job's marker
            job._warmup[mint]["warm_worker_end_monotonic_ns"] = time.monotonic_ns()
            job._warmup[mint]["warm_request_attempts"] = (
                self.counts["preparation_request_attempts"] - requests_before)
            current_generation = self.generation_fn()
            with self._lock:
                if ((reads is not None and reads.cancelled.is_set())
                        or identity != self._identities.get(mint) or generation != current_generation
                        or mint not in self._priority
                        or self.clock() - self._priority[mint][1] > CANDIDATE_TTL_S):
                    self.counts["preparation_cancelled"] += 1
                    return None
                for name in ("_facts", "_facts_at", "_stats", "_verdicts", "_retry_at"):
                    target, source = getattr(self, name), getattr(job, name)
                    if mint in source:
                        target[mint] = source[mint]
                    else:
                        target.pop(mint, None)
                self._prepared_generation[mint] = generation
                self._warmup[mint].update({k: v for k, v in job._warmup[mint].items()
                                          if not k.startswith("candidate_")})
                self.counts.update(job.counts)
                published_facts = self._facts.get(mint)
            if published_facts is not None and self.facts_sink is not None:
                try:
                    self.facts_sink(mint, published_facts)
                except Exception:
                    logger.exception("bsc fast safety: facts publication failed")
            return result
        finally:
            if reads is not None:
                reads.closed.set()
            self._job_lock.release()

    def decimals_hint(self, mint):
        """The safety's decimals candidates: the facts cache's (lane host, the value the entry gate's
        decimals_disagree checks), then the signal's (Alpha/Gamma 2026-10-14: facts first)."""
        mint = str(mint).lower()
        with self._lock:
            facts = self._facts.get(mint)
            return [("facts", facts[0] if facts else None), ("signal", self.signal_decimals.get(mint))]

    def reason(self, mint, *, now, evidence=None):
        with self._lock:
            verdict = self._verdicts.get(str(mint).lower())
            if evidence is not None:
                warm = self._warmup.get(str(mint).lower(), {})
                evidence.update({key: warm.get(key) for key in (
                    "candidate_first_at", "candidate_last_at", "warm_worker_start_at",
                    "warm_worker_end_at", "warm_worker_outcome", "warm_request_attempts",
                    "warm_worker_end_monotonic_ns")})
                evidence["verdict_at"] = verdict[2] if verdict is not None else None
                evidence["lookup_at"] = now
                facts, at, stats = self._facts.get(mint), self._facts_at.get(mint), self._stats.get(mint)
                evidence["prepared_inputs_complete"] = bool(
                    facts and facts[1] and (at is None or 0 <= now - at <= self.supply_ttl_s)
                    and stats and 0 <= now - stats[1] <= self.ttl_s)
            tier = str(self.warm_tier.get(str(mint).lower(), "other"))
            fresh = verdict is not None and now - verdict[2] <= self.ttl_s
            self.lookups["%s:%s" % (tier, "hit" if fresh and verdict[0] else "miss")] += 1
            if fresh and verdict[0]:
                age = self.hit_age_s.setdefault(tier, [0.0, 0])
                age[0] += now - verdict[2]
                age[1] += 1
        if verdict is None:
            return "no_safety_verdict"
        if now - verdict[2] > self.ttl_s:
            return "safety_stale"
        return None if verdict[0] else "unsafe:%s" % (verdict[1] or "refused")


def lane_facts(host, _batching=None):
    """The safety cache's facts_fn: (decimals, chain totalSupply) on the lane's own host, read once
    per coin before its verdict. Live supply is never DexScreener's (Alpha)."""
    # A host that answered "batch not supported" is not asked again (Gamma LOW on 80ad4e078); the
    # memory is shared with every job seam on the same host (for_http).
    batching = {"on": True} if _batching is None else _batching

    def facts(mint):
        calls = [("eth_call", [{"to": mint, "data": "0x313ce567"}, "latest"]),
                 ("eth_call", [{"to": mint, "data": "0x18160ddd"}, "latest"])]
        # ONE round trip (cut 4): both reads in one batch on the same host. A host that does not
        # batch gets today's two calls, decimals first.
        got = host.batch(calls) if batching["on"] else None
        if got is None:
            batching["on"] = False
        decimals_hex, supply_hex = got if got is not None else (host(*calls[0]), host(*calls[1]))
        decimals = int(decimals_hex, 16)
        supply = int(supply_hex, 16)
        return decimals, (supply or None)
    facts.for_http = lambda http: lane_facts(LaneHost(host.url, http), batching)
    return facts


def lane_safety_fn(url, http, taker, quotes, *, deadline_scope=None, deadline_s=8.0, decimals_hint=None):
    """Today's EVM token safety (build_evm_token_safety) with every read on the lane's own host:
    the PancakeSwap V2 exit quote over Router02 and its connectors, plus the coin's own quote token
    from its signal (`quotes`: mint -> quote), so no pair lookup leaves the host. With
    `deadline_scope` (the inline check) every read, on every worker thread, ends at `deadline_s`."""
    from sniper.execution.evm_token_safety import build_evm_token_safety
    from sniper.execution.evm_v2_exit_quote import _make_call_fn, dex_v2_exit_quote
    from sniper.settings import memecoin as cfg
    from collections import OrderedDict
    from sniper.execution.metadata_cache import BoundedLru
    checks = OrderedDict()
    proofs = BoundedLru(256)         # immutable found slots survive job-bound checker instances
    prewarm_gate = threading.Lock()  # shared by warm-job and cached inline checkers

    def proof_load(key):
        return proofs.get_or_load(key, lambda: None)

    def proof_store(key, value):
        proofs.get_or_load(key, lambda: value)

    def for_quote(mint, quote, *, request_http=None):
        key = (str(mint).lower(), quote)
        check = checks.get(key) if request_http is None else None
        if check is None:
            bound_http = http if request_http is None else request_http
            call_fn = _make_call_fn(CHAIN, bound_http, rpc=url)
            def pair_tokens(_chain, coin):
                return [quote] if str(coin).lower() == key[0] and quote and quote != WBNB else []

            def quote_fn(sell, buy, amount, slippage_bps=None, **_ignored):
                return dex_v2_exit_quote(CHAIN, sell, buy, amount, taker, http=bound_http,
                                        slippage_bps=slippage_bps, call_fn=call_fn,
                                        pair_tokens_fn=pair_tokens)
            check = build_evm_token_safety(
                chain_id=CHAIN, rpc_url=url, taker=taker, quote_fn=quote_fn, http=bound_http,
                sell_slippage_bps=int(round(cfg.MEMECOIN_EVM_MIN_OUT_RETRY_SLIPPAGE_PCT * 100)),
                sell_route_label="bsc_fast_v2_direct", safety_deadline_scope=deadline_scope,
                safety_deadline_s=deadline_s,
                # dex_v2_exit_quote always names the configured Router02 as its spender.
                sell_spender_hint=(cfg.memecoin_evm_v2_exit_config(CHAIN) or {}).get("router"),
                # The facts cache's, then the signal's decimals size the probe (BscSafetyCache).
                decimals_hint_fn=decimals_hint,
                slot_prewarm_gate=prewarm_gate,
                early_slot_hints=request_http is not None,
                slot_proof_load=proof_load if request_http is not None else None,
                slot_proof_store=proof_store if request_http is not None else None)
            if request_http is None:
                checks[key] = check
                while len(checks) > CANDIDATE_LIMIT:
                    checks.popitem(last=False)
        return check

    def checked(mint, usd=None, timings=None):
        return for_quote(mint, quotes.get(str(mint).lower()))(
            mint, usd=usd, **({"timings": timings} if timings is not None else {}))
    checked.for_quote = for_quote
    return checked



def low_rtt_url(cfg=None):
    """The 56 funded pool's Alchemy member (MEMECOIN_EVM_RPC_POOL_URLS), or None: the host the cold
    signal's facts and safety reads move to (LowRttReads). Taken from the pool config, never a new key."""
    from urllib.parse import urlsplit
    if cfg is None:
        from sniper.settings import memecoin as cfg
    pools = getattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {}) or {}
    for url in list(pools.get(CHAIN) or pools.get(str(CHAIN)) or ()):
        host = (urlsplit(str(url)).hostname or "").lower()
        if host.endswith(".alchemy.com"):
            return str(url)
    return None


class LowRttReads:
    """A COLD SIGNAL'S FACTS AND SAFETY READS ON THE FUNDED POOL'S ALCHEMY MEMBER (Alpha GO 2026-10-16,
    option b). e54dddf6 (19:49Z) decided ~115 ms after the watcher's wake, waiting for the coin's facts
    batch on the paced Chainstack lane host (facts p50 101 / p90 174 ms on 13 cold signals since
    10-14), and its safety read ran ~180 ms there; the runner's 56 pool measures Alchemy at ewma 45 ms.

    Per read family (MEMECOIN_BSC_FAST_LOW_RTT_READS, default "facts,safety"): the cold signal's and
    the intent's facts batch and inline safety read. The presignal warm, the snapshot and the Relay
    pre-buy stay on the lane host. Alchemy is asked only while the funded pool shows it available
    (not quarantined) and identity-proven. A 429, 403, 5xx or transport failure on it is recorded in
    that pool (record + penalize: the pool's 5/15/60 s quarantine, so no retry storm; a cap hit shows
    as the member's failures) and that read runs again on the lane host, once. A JSON-RPC error item
    or a partial batch is an answer, refused as today, never re-asked elsewhere."""

    FAMILIES = ("facts", "safety")

    def __init__(self, url, base):
        from urllib.parse import urlsplit
        from sniper.services.memecoin_rh_fast_lane import PacedHttp
        self.url, self.host = url, (urlsplit(url).hostname or "?")
        self._lock, self._failures = threading.Lock(), 0
        self.counts = {f: {"low_rtt": 0, "fallback": 0, "lane_host": 0} for f in self.FAMILIES}
        self._base = base
        # The inline seam's deadline and host check on this URL; no pacing, no own 429 pause and no own
        # dead mark: the funded pool's quarantine (5/15/60 s, read by usable()) owns this member's
        # health. A private 600 s dead mark (PacedHttp.DEAD_AFTER) was invisible to usable(), so after
        # the pool recovered a cold read raised InlineHostDead past _run's fallback (Codex F1 [P1]).
        paced = PacedHttp(None, min_gap_s=0.0, pause_s=0.0)
        paced.DEAD_AFTER = math.inf
        self.http = lane_inline_http(url, paced, base=self._recorded)

    def _recorded(self, method, target, headers=None, body=None, timeout=10):
        from sniper.execution import evm_rpc_pool
        from sniper.services.memecoin_rh_fast_lane import is_transport_failure
        t0 = time.perf_counter()
        try:
            status, answer, hdrs = self._base(method, target, headers=headers, body=body, timeout=timeout)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if type(code) is int and (code in (403, 429) or code >= 500):
                self._failed("http_%d" % code)
            elif is_transport_failure(exc):
                self._failed("transport:%s" % type(exc).__name__)
            raise
        if type(status) is int and (status in (403, 429) or status >= 500):
            self._failed("http_%d" % status)
        else:
            evm_rpc_pool.record(self.url, (time.perf_counter() - t0) * 1000.0, True)
        return status, answer, hdrs

    def _failed(self, reason):
        from sniper.execution import evm_rpc_pool
        with self._lock:
            self._failures += 1
        evm_rpc_pool.record(self.url, 0.0, False)
        evm_rpc_pool.penalize(self.url, "bsc_fast_low_rtt:%s" % reason)

    def usable(self, family):
        from sniper.execution import evm_rpc_pool
        from sniper.settings import memecoin as cfg
        return (family in cfg.memecoin_bsc_fast_low_rtt_reads() and evm_rpc_pool.available(self.url)
                and evm_rpc_pool.identity_state(self.url) is True)

    def _count(self, family, how):
        with self._lock:
            self.counts[family][how] += 1

    def _run(self, family, fast, slow, args, kw):
        if not self.usable(family):
            self._count(family, "lane_host")
            return slow(*args, **kw)
        with self._lock:
            before = self._failures
        try:
            result = fast(*args, **kw)
        except Exception:
            with self._lock:
                failed = self._failures != before
            if not failed:
                raise                    # the coin's answer (an error item, a partial batch): as today
            self._count(family, "fallback")
            return slow(*args, **kw)
        with self._lock:
            failed = self._failures != before
        if failed:
            # A host failure during the check (a safety read turns some into a refusal): never the
            # coin's verdict. That read again on the lane host, once.
            self._count(family, "fallback")
            return slow(*args, **kw)
        self._count(family, "low_rtt")
        return result

    def wrap(self, seam, *, fast_safety, fast_facts):
        slow_safety, slow_facts = seam["inline_safety_fn"], seam["inline_facts_fn"]

        def inline_safety(mint, usd=None, timings=None):
            return self._run("safety", fast_safety, slow_safety, (mint,), {"usd": usd, "timings": timings})

        def inline_facts(mint):
            return self._run("facts", fast_facts, slow_facts, (mint,), {})
        return dict(seam, inline_safety_fn=inline_safety, inline_facts_fn=inline_facts)

    def status(self):
        """Per family: where its next read goes, and what went where (the lane beat)."""
        from sniper.execution import evm_rpc_pool
        with self._lock:
            counts = {f: dict(c) for f, c in self.counts.items()}
        return {"host": self.host, "available": evm_rpc_pool.available(self.url),
                "identity": evm_rpc_pool.identity_state(self.url),
                **{f: dict(counts[f], next=self.host if self.usable(f) else "lane_host")
                   for f in self.FAMILIES}}

def live_market_cap(payload, *, decimals, supply):
    """The coin's market cap at the source's exact post-trade price: chain totalSupply x the coin's
    post price on its pair, through the receipt's WBNB/quote hop when there is one, at the
    datagram's BNB price. None when a non-WBNB quote has no hop."""
    price = Fraction(int(payload["r_quote"]), int(payload["r_coin"]))     # quote atomic per coin atomic
    hop = payload.get("hop")
    if hop:
        price *= Fraction(int(hop["r_wbnb"]), int(hop["r_quote"]))       # wei per coin atomic
    elif str(payload["quote"]).lower() != WBNB:
        return None
    usd = Fraction(int(supply)) * price / 10 ** 18 * Fraction(str(payload["bnb_usd"]))
    return float(usd)


class BscSender:
    """One parallel send of the IDENTICAL signed bytes to every member (48 Club Puissant and the
    chain owner's public endpoints by eth_sendRawTransaction; BlockRazor's bundle with the
    operator's token, when set) on warm threads. The first acceptance returns at once while the
    other sends finish; a refusal counts only when stateless and unanimous; anything else is
    ambiguous and the receipt decides. Nothing is ever re-signed."""

    def __init__(self, members, *, bundle=None, timeout_s=2.0):
        from concurrent.futures import ThreadPoolExecutor
        self.members, self.bundle, self.timeout_s = list(members), bundle, timeout_s
        self._pool = ThreadPoolExecutor(max_workers=len(self.members) + 1,
                                        thread_name_prefix="bsc-send")

    def warm(self):
        for member in self.members:
            try:
                member.warm()
            except Exception:
                logger.exception("bsc fast lane: member keep-alive failed")

    def send_set(self, private_only=False):
        """(members, labels) one send goes to. `private_only`: the private builders alone -- 48 Club
        Puissant and the BlockRazor bundle -- never a public endpoint."""
        members = [m for m in self.members
                   if not private_only or getattr(m, "host", None) in PRIVATE_HOSTS]
        labels = [str(getattr(m, "host", type(m).__name__)) for m in members]
        if self.bundle is not None:
            labels.append("blockrazor_bundle")
        return members, labels

    def send(self, raw_hex, max_block=None, private_only=False):
        """`max_block`: the bundle's maxBlockNumber (EVM-08 section 6.2); the public posts ignore it.
        `private_only`: see send_set. With no private member at all nothing is sent: rejected."""
        from concurrent.futures import TimeoutError as FutureTimeout, as_completed
        members, _labels = self.send_set(private_only)
        if private_only and not members and self.bundle is None:
            return "rejected", "no_private_member"
        futures = [self._pool.submit(m.send, raw_hex) for m in members]
        if self.bundle is not None:
            futures.append(self._pool.submit(self.bundle, raw_hex, max_block=max_block))
        replies = []
        try:
            for future in as_completed(futures, timeout=self.timeout_s):
                try:
                    kind, detail = future.result()
                except Exception as exc:
                    kind, detail = "ambiguous", type(exc).__name__
                if kind == "rejected" and not any(m in str(detail).lower()
                                                  for m in _STATELESS_REJECT):
                    kind = "ambiguous"
                if kind == "accepted":
                    return kind, detail
                replies.append((kind, detail))
        except FutureTimeout:
            replies.append(("ambiguous", "timeout"))
        if len(replies) == len(futures) and all(kind == "rejected" for kind, _d in replies):
            return "rejected", replies[0][1]
        return "ambiguous", "; ".join("%s:%s" % (k, str(d)[:40]) for k, d in replies)[:300]


def blockrazor_bundle(token, *, url=BLOCKRAZOR_URL, post=None, timeout_s=2.0):
    """BlockRazor's eth_sendBundle with the operator's `Authorization` token. A bundle hash is the
    builder's acceptance, not inclusion, so every reply reads ambiguous (the receipt decides)."""
    from sniper.execution import exec_lock

    def default_post(target, body, headers):
        import urllib.request
        req = urllib.request.Request(target, data=body.encode(), headers={
            "Content-Type": "application/json", **headers})
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return json.loads(r.read() or b"{}")

    def send(raw_hex, max_block=None):
        # EVM-08 6.2: the explicit expiry is what lets the sweep resolve a bundle-only identity.
        bundle = {"txs": [raw_hex]}
        if max_block is not None and exec_lock.intents_enabled():
            bundle["maxBlockNumber"] = int(max_block)
        else:
            max_block = None
        try:
            intent = exec_lock.intent_send(raw_hex, "bundle", max_block=max_block)
        except exec_lock.IntentUnwritable as exc:
            return "rejected", "intent_unwritable: %s" % exc           # nothing was sent
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_sendBundle",
                           "params": [bundle]})
        started, said = time.perf_counter(), None
        try:
            answer = (post or default_post)(url, body, {"Authorization": token})
            why = "bundle" if (answer or {}).get("result") else str((answer or {}).get("error"))[:60]
            said = _bundle_answer(answer)
        except Exception as exc:
            why = type(exc).__name__
            said = _bundle_refusal(exc)
        finally:
            exec_lock.stamp_send(raw_hex, intent=intent)
        # BlockRazor's own answer, from this (possibly detached) post: BscSender returns on the
        # first member acceptance and drops it (Gamma/Alpha 2026-10-07, maxBlockNumber go-live).
        try:
            logger.warning("bsc.bundle max_block=%s answer=%s ms=%.1f", max_block,
                           _without(said, token), (time.perf_counter() - started) * 1000.0)
        except Exception:                      # the log never turns a sent bundle into a raise
            logger.exception("bsc.bundle answer log failed")
        return "ambiguous", "blockrazor %s" % why
    return send


def _bundle_answer(answer):
    if not isinstance(answer, dict):
        return "malformed"
    if answer.get("result"):
        return "ok"
    error = answer.get("error")
    if isinstance(error, dict):
        return "error code=%s message=%s" % (error.get("code"), error.get("message"))
    return "error %s" % error if error is not None else "no_result"


def _bundle_refusal(exc):
    import urllib.error
    if isinstance(exc, urllib.error.HTTPError):
        try:
            text = exc.read()[:300].decode("utf-8", "replace")
        except Exception:
            text = ""
        return "http_%s %s" % (exc.code, text)
    return "transport:%s" % type(exc).__name__


def _without(text, secret):
    """`text` bounded, with the operator's token removed in its raw and URL-coded forms."""
    from urllib.parse import quote, quote_plus
    text = " ".join(str(text).split())
    for form in sorted({secret, quote(secret, safe=""), quote_plus(secret, safe="")},
                       key=len, reverse=True):
        if form:
            text = text.replace(form, "[REDACTED]")
    return text[:200]


def default_members():
    """Puissant and the chain owner's free endpoints (publicnode refuses a request without a
    browser user agent, as the funded path found)."""
    from sniper.execution.evm_wallet import _UA
    from sniper.execution.free_endpoint_registry import free_endpoints
    from sniper.services.memecoin_rh_fast_lane import SequencerSender
    return [SequencerSender(PUISSANT_URL)] + [
        SequencerSender(e.url, headers={"User-Agent": _UA}) for e in free_endpoints(CHAIN)]


def coin_pool_of(transaction):
    """The coin's PancakeSwap V2 pair of one of the lane's own buys, from its calldata (the last
    two path tokens), or None when the transaction is not that buy."""
    from eth_abi import decode
    data = str((transaction or {}).get("input") or "")
    if not data.startswith("0x" + SWAP_ETH_FOR_TOKENS_FOT):
        return None
    try:
        _min, path, _to, _deadline = decode(["uint256", "address[]", "address", "uint256"],
                                            bytes.fromhex(data[10:]))
    except Exception:
        return None
    return our_pair(path[-1], path[-2]) if len(path) >= 2 else None


def _revert_reason(tx_hash, rpc_url):
    from sniper.services.memecoin_rh_fast_lane import _revert_reason as reason
    return reason(tx_hash, rpc_url)


def _live_base():
    from sniper.services.memecoin_rh_fast_lane import LiveLane
    return LiveLane


class BscLive(_live_base()):
    """MEMECOIN_BSC_FAST_LANE=live: the 4663 lane's executor (lock, nonce floor, write-ahead, send,
    capped receipt wait, booking, delta fallback, refused-booking alert, fill check) with the 56
    chain's pieces below."""

    def _safety_binding(self, mint):
        """The quote the safety read checks the coin's pairs in (lane_safety_fn reads quotes[mint])."""
        return (self.quotes or {}).get(str(mint).lower())

    def _intent_binding(self, mint, pair):
        """At the log's arrival (e54dddf6, 2026-10-16: a first-seen coin's intent read ran under no quote,
        the signal noted 0x4902c5eb, so it was never adopted): the coin's noted quote, only when the
        source's token came out of the pool noted with it; else UNKNOWN_BINDING. No pair named keeps
        today's quote-only binding; no quote noted is always unknown (the signal notes one)."""
        from sniper.services.memecoin_rh_fast_lane import UNKNOWN_BINDING
        mint = str(mint).lower()
        quote = (self.quotes or {}).get(mint)
        if quote is None:
            return UNKNOWN_BINDING
        if pair is None:
            return quote
        identity = self.cache.identity(mint)
        return quote if identity is not None and identity[1] == pair else UNKNOWN_BINDING

    def _parallel_safety_on(self):
        """The intent warm's safety leg follows this lane's own parallel-safety knob."""
        from sniper.settings import memecoin as cfg
        return cfg.memecoin_bsc_fast_parallel_safety()

    CHAIN_ID = 56
    BACKEND = "bsc_fast_lane"
    crowded = None              # build_live's BscCrowdedGas; None = today's price (a class default:
                                # object.__new__ fixtures skip __init__)
    RELEASE_PREFIX = "bsc_fast_"
    ORIGIN_PREFIX = "bscfast"

    def __init__(self, *, snapshot, cache, quotes, gas_floor_wei, v3_hook_factory=None, **kw):
        super().__init__(facts_fn=cache.facts, require_exact_state=False, cache=cache, **kw)
        self.snapshot, self.quotes = snapshot, quotes
        self.gas_floor_wei = int(gas_floor_wei)
        self.v3_hook_factory = v3_hook_factory
        self.entry_active = False               # owned by the existing serialized lane handler
        self.entry_ended_at = float("-inf")     # monotonic; warm jobs wait WARM_ENTRY_COOLDOWN_S

    def float_snapshot(self):
        """The native-float stand-in (evm_native_float.LANE_SNAPSHOT): the snapshot under the same
        freshness and generation rules as early_quote_balances, else None. A snapshot read before
        the latest wallet change cannot stand in; a wrong-high one meets the executor's existing
        insufficient-native fallback."""
        snap = self.snapshot.current()
        now = self.host_clock()
        try:
            if (not snap or snap['read_at'] <= self.generation_fn() + self.generation_margin_s
                    or not 0 <= now - snap['read_at'] <= self.max_snapshot_age_s):
                return None
        except (TypeError, ValueError):
            return None
        return {"native_wei": snap.get("native_wei"), "gas_price_wei": snap.get("gas_price_wei"),
                "age_ms": (now - snap['read_at']) * 1000.0}

    def early_quote_balances(self, quote_usd):
        """Speculative sizing from the existing snapshot; the funded gate still reads live."""
        snap = self.snapshot.current()
        if (not snap or snap['read_at'] <= self.generation_fn() + self.generation_margin_s
                or not 0 <= self.host_clock() - snap['read_at'] <= self.max_snapshot_age_s
                or type(snap.get('weth_wei')) is not int or snap['weth_wei'] <= 0
                or type(snap.get('native_wei')) is not int or snap['native_wei'] < 0):
            return None
        return snap['weth_wei'] / 10**18 * quote_usd, snap['native_wei'] / 10**18

    def entry_signing_snapshot(self):
        """Copy the existing pending nonce; its caller checks generation under the wallet lock."""
        snap = self.snapshot.current()
        address = _fixed_hex(self.ctx.get('address'), 20)
        if not isinstance(snap, dict) or not address or self.snapshot.address != address:
            return None
        read_at, now = snap.get('read_at'), self.host_clock()
        max_age, margin = self.max_snapshot_age_s, self.generation_margin_s
        if (any(type(value) not in (int, float) or not math.isfinite(value)
                for value in (read_at, now, max_age, margin))
                or max_age <= 0 or margin < 0 or not 0 <= now - read_at <= max_age
                or any(type(snap.get(name)) is not int or snap[name] < 0
                       for name in ('nonce', 'native_wei', 'weth_wei', 'gas_price_wei'))
                or snap['gas_price_wei'] == 0):
            return None
        return {'chain': CHAIN, 'address': address, 'nonce': snap['nonce'], 'read_at': read_at,
                'max_age_s': max_age, 'generation_margin_s': margin}

    def _post(self, raw, payload):
        """The bundle is valid to the source block + K (EVM-08 6.2); no source block, no expiry."""
        from sniper.settings import memecoin as cfg
        seq = payload.get("seq")
        max_block = (int(seq) + int(cfg.MEMECOIN_BSC_BUNDLE_MAX_BLOCKS)
                     if isinstance(seq, int) and seq > 0 else None)
        if payload.get("_private_only"):
            return self.sender.send(raw, max_block=max_block, private_only=True)
        return self.sender.send(raw, max_block=max_block)

    def _send_evidence(self, payload):
        """Which members carried this send, and whether it was private-only (the inclusion read)."""
        private = bool(payload.get("_private_only"))
        labels = getattr(self.sender, "send_set", None)
        return {"private_only": private,
                "send_members": labels(private)[1] if callable(labels) else None}

    def _fanout_allowed(self, payload):
        """A cap-floor send is never re-offered: the fan-out goes through the funded public path."""
        return not payload.get("_private_only")

    def _handle_v3(self, payload, recv_epoch):
        """The existing funded shared engine, with fresh lane stats instead of a repeated GET.
        Cold data is read normally while the attempt's early quote runs beside it. Token safety
        stays on the original context; the V2 verdict and reserve arithmetic are never used."""
        from sniper.services.memecoin_rh_fast_lane import hook_reason, refusal_pages_off
        from sniper.services.memecoin_real_runner import make_real_hook
        from sniper.settings import memecoin as cfg
        from sniper.util.clock import real_utc_now_iso
        t0, cpu = time.perf_counter(), time.thread_time()
        held = {}                     # DECISION FEATURES: what the hook's own stats read returned

        def stats_fn(mint, network_id=None):
            now = self.host_clock()
            got = self.cache.stats(mint, now=now)
            if got:
                held.update(entry=got, read_at=now, source="lane_cache")
                return got[0]
            started = self.host_clock()
            stats = self.stats_fn(mint) if self.stats_fn is not None else None
            self.cache.put_market_stats(mint, stats, now=started)
            held.update(entry=(stats, started) if stats else None, read_at=started, source="inline_read")
            return stats
        context = self.contexts.get(CHAIN) or {}
        decimals = payload.get("decimals")
        sink = context.get("put_token_decimals_fn")
        if (payload.get("chain") == CHAIN and context.get("chain") == CHAIN
                and payload.get("route_kind") in {"v3_shared", "flap_shared"}
                and _fixed_hex(payload.get("mint"), 20) is not None
                and type(decimals) is int and 0 <= decimals <= 36 and callable(sink)
                and bsc_payload_initiation_refusal(payload) is None):
            # The checked source already read this immutable fact; balances and quotes stay live.
            sink(payload["mint"], decimals)
        factory = getattr(self, "v3_hook_factory", None)
        hook = (factory(self.contexts, stats_fn) if factory else make_real_hook(
            None, self.contexts, now_fn=real_utc_now_iso, trade_size=cfg.MEMECOIN_TRADE_SIZE_USD,
            max_age_s=cfg.MEMECOIN_REAL_ENTRY_MAX_AGE_S, stats_fn=stats_fn))
        ev = BscLaneEvent(payload, market_cap=None)
        if payload.get("route_kind") == "flap_shared":
            # Measurement only (spec 2026-10-14 4.3): references now, built at the trace's finish.
            ev.decision_features_sink = {}
            ev.decision_features = partial(flap_decision_features, held=held, payload=payload,
                                           their_usd=ev.their_size_usd, sink=ev.decision_features_sink)
        if self.stopped or getattr(self, "_stop_owner", self).stopped:
            res = {"status": "skipped", "reason": "operator_stop"}
        else:
            from sniper.execution.evm_native_float import LANE_SNAPSHOT
            token = LANE_SNAPSHOT.set((str(self.CHAIN_ID), self.float_snapshot))
            try:
                with refusal_pages_off():
                    # Normal on-chain MCAP resolution and retry enqueue remain authoritative.
                    res = hook(self.conn, ev) or {}
            finally:
                LANE_SNAPSHOT.reset(token)
        return {"source_tx": payload["tx_hash"], "tx_hash": res.get("tx_hash") or res.get("tx_sig"),
                "mint": payload["mint"], "pool": payload["pool"], "quote": payload["quote"],
                "reason": hook_reason(res), "sent_epoch": payload["sent_epoch"], "recv_epoch": recv_epoch,
                "handoff_ms": (recv_epoch - payload["sent_epoch"]) * 1000,
                "busy_ms": (time.perf_counter() - t0) * 1000, "cpu_ms": (time.thread_time() - cpu) * 1000,
                "lock_busy": 0, "clip_wei": None, "min_out": None, "tx_bytes": None,
                "pending_id": res.get("pending_id"), "position_id": res.get("position_id")}

    def handle(self, payload, *, recv_epoch):
        observer = self.ctx.get("bsc_execution_observer")
        if observer is not None:
            prefix = "onchain" if payload.get("route_kind") in {"v3_shared", "flap_shared"} else "bscfast"
            signal = {"signal_id": "%s:56:%s:%s" % (
                prefix, str(payload.get("tx_hash") or "").lower(), str(payload.get("mint") or "").lower()),
                "source_tx": payload.get("tx_hash"), "mint": payload.get("mint"),
                "source_user_id": payload.get("source_user_id"), "source_feed": "onchain_watch",
                "receipt_clock": payload.get("receipt_clock"), "eligibility": None}
            try:
                observer.record_signal(signal)
            except Exception:
                pass
        self.entry_active = True
        result = None
        try:
            result = self._handle(payload, recv_epoch=recv_epoch)
            return result
        finally:
            # The end time first (Gamma LOW 1): a capacity check between the two never sees "no
            # entry" with an old end time.
            self.entry_ended_at = time.monotonic()
            self.entry_active = False
            note_policy_decision(payload.get("tx_hash"), payload.get("mint"),
                                 payload.get("source_user_id"), (result or {}).get("reason"))
            if observer is not None:
                try:
                    observer.record_outcome({**signal, "reason": (result or {}).get("reason", "raised"),
                                             "tx_hash": (result or {}).get("tx_hash"),
                                             "eligibility": True if (result or {}).get("status") == "filled"
                                             else None})
                except Exception:
                    pass

    def _handle(self, payload, *, recv_epoch):
        """V3/Flap use the shared executor. V2 keeps its native builder and lane checks."""
        guard_started, guard_cpu = time.perf_counter(), time.thread_time()
        initiation_refusal = self._initiation_refusal(payload)
        if initiation_refusal is None and self.prebuy_inflight_fn is not None and self.prebuy_inflight_fn(
                payload["mint"]):
            # A Relay pre-buy of this coin is deciding or awaiting its receipt (spec 2026-10-14 bsc
            # pre-buy §3.7): no hook here, so no attempt row; the on-chain lane waits on the pre-buy's row.
            initiation_refusal = "relay_prebuy_in_flight"
        if initiation_refusal is not None:
            return {"source_tx": payload["tx_hash"], "tx_hash": None, "mint": payload["mint"],
                    "pool": payload["pool"], "quote": payload["quote"],
                    "reason": initiation_refusal if initiation_refusal.startswith(("relay_prebuy_", "prebuy_"))
                    else "source_initiation_unknown:" + initiation_refusal,
                    "sent_epoch": payload["sent_epoch"], "recv_epoch": recv_epoch,
                    "handoff_ms": (recv_epoch - float(payload["sent_epoch"])) * 1000,
                    "busy_ms": (time.perf_counter() - guard_started) * 1000,
                    "cpu_ms": (time.thread_time() - guard_cpu) * 1000, "lock_busy": 0,
                    "clip_wei": None, "min_out": None, "tx_bytes": None}
        if payload.get("route_kind") in {"v3_shared", "flap_shared"}:
            return self._handle_v3(payload, recv_epoch)
        from sniper.services.memecoin_rh_fast_lane import (
            clip_wei_for, hook_reason, inputs_reason, refusal_pages_off, stamp_input_legs)
        t0, c0, host_now = time.perf_counter(), time.thread_time(), self.host_clock()
        mint = str(payload["mint"]).lower()
        rec = {"tx_hash": None, "source_tx": payload["tx_hash"], "mint": mint,
               "pool": payload["pool"], "quote": payload["quote"], "recv_epoch": recv_epoch,
               "sent_epoch": payload["sent_epoch"],
               "handoff_ms": (recv_epoch - float(payload["sent_epoch"])) * 1000.0,
               "clip_wei": None, "min_out": None, "tx_bytes": None, "lock_busy": 0,
               "busy_start_epoch": host_now, "_perf0": t0}

        def done(reason, **fields):
            rec.pop("_perf0", None)
            rec.pop("_intent_entry", None)
            rec.pop("_inline_deadline", None)
            rec.pop("_safety_before", None)
            rec.pop("_safety_timings", None)
            if rec.pop("_safety_future", None) is not None:
                rec["parallel_safety"] = "abandoned"
            rec.update(fields, reason=reason, busy_end_epoch=self.host_clock(),
                       busy_ms=(time.perf_counter() - t0) * 1000.0,
                       cpu_ms=(time.thread_time() - c0) * 1000.0)
            stamp_input_legs(rec)
            return rec

        if self.stopped or getattr(self, "_stop_owner", self).stopped:
            return done("operator_stop")
        if self.fills >= self.pilot_fills:
            return done("pilot_complete")
        while self.send_times and host_now - self.send_times[0] > 3600:
            self.send_times.popleft()
        if len(self.send_times) >= self.max_sends_per_hour:
            return done("pilot_rate_cap")
        clip_wei = clip_wei_for(self.clip_usd, payload.get("bnb_usd"))
        rec["clip_wei"] = clip_wei
        if clip_wei is None:
            return done("no_bnb_usd")
        snap = self.snapshot.current()
        from sniper.settings import memecoin as cfg
        # A pilot cap is an upper bound, not the funded clip. Exact sizing is checked under lock.
        minimum_clip = max(1, clip_wei_for(min(self.clip_usd, cfg.MEMECOIN_MIN_TRADE_FLOOR_USD),
                                           payload.get("bnb_usd")) or 0)
        generation = self.generation_fn()
        self.cache.note_candidates([{"mint": mint, "quote": payload["quote"],
                                    "pool": payload["pool"], "priority": 0,
                                    "decimals": payload.get("decimals")}],
                                   now=host_now, generation=generation)
        reason = inputs_reason({"snapshot": snap, "eth_usd": payload.get("bnb_usd"),
                                "eth_usd_at": payload.get("bnb_usd_at")},
                               generation=generation, now_host=host_now,
                               clip_wei=minimum_clip, gas_limit=V2_BUY_GAS, low_wei=self.low_wei,
                               max_snapshot_age_s=self.max_snapshot_age_s,
                               eth_usd_max_age_s=self.eth_usd_max_age_s,
                               generation_margin_s=self.generation_margin_s)
        if reason is not None:
            return done(reason.replace("eth_usd", "bnb_usd"))
        safety_evidence = {}
        reason = self.cache.reason(mint, now=host_now, evidence=safety_evidence)
        ready_at = safety_evidence.get("warm_worker_end_at")
        safety_evidence["ready_at_ingress"] = bool(reason is None and ready_at is not None
                                                  and ready_at <= recv_epoch)
        clock = freeze_receipt_clock(payload.get("receipt_clock"))
        ready_mono = safety_evidence.get("warm_worker_end_monotonic_ns")
        safety_evidence["ready_at_original_ingress"] = (bool(
            reason is None and ready_mono is not None and ready_mono <= clock["monotonic_ns"]
            and safety_evidence["prepared_inputs_complete"])
            if clock is not None and self.cache.boot_id and clock["boot_id"] == self.cache.boot_id else None)
        if safety_evidence["ready_at_original_ingress"]:
            self.cache.counts["ready_at_original_ingress"] += 1
        safety_evidence["verdict_age_s"] = (host_now - safety_evidence["verdict_at"]
                                            if safety_evidence.get("verdict_at") is not None else None)
        self.cache.counts["safety_hit" if reason is None else "safety_miss"] += 1
        if safety_evidence.get("candidate_first_at") == host_now:
            self.cache.counts["unknown_first_encounter"] += 1
        safety_evidence["cache_miss_reason"] = reason
        rec.setdefault("execution_evidence", {})["safety_warmup"] = safety_evidence
        rec["safety_path"] = "cache"
        needs_inline = False
        if reason in ("no_safety_verdict", "safety_stale") and self.inline_safety_fn is not None:
            reason = self._band_refusal(payload)
            if reason is None:
                # The worker only reads; admission still precedes consuming its verdict. Rollback is the
                # knob (Gamma LOW 1 on 223c22065): off, the hook reads safety sequentially.
                if cfg.memecoin_bsc_fast_parallel_safety():
                    self._start_safety(mint, rec)
                reason = self._inline_inputs(mint, rec)
            needs_inline = reason is None
        if reason is not None:
            return done(reason)
        facts = self.cache.facts(mint)
        if not facts or not facts[1]:
            return done("no_supply")
        if int(facts[0]) != int(payload["decimals"]):
            return done("decimals_disagree")
        stats = self.cache.stats(mint, now=host_now)
        if not stats:
            return done("no_market_stats")
        min_out = min_out_for(clip_wei, payload)
        rec["min_out"] = min_out
        if min_out <= 0:
            return done("build_refused")
        mcap = live_market_cap(payload, decimals=facts[0], supply=facts[1])
        if not mcap:
            return done("no_market_cap")
        payload = dict(payload, snapshot=snap)
        decimals = int(facts[0])

        def execute_fn(conn, pending_id, mint_, trade_size, now_iso, trace=None):
            return self.execute_reserved(payload, rec, conn, pending_id, trade_size,
                                         decimals=decimals, trace=trace)
        def native_funding_precheck(approved_clip_usd):
            # Unknown inputs cannot prove shortage; the locked signing check remains final.
            now = self.host_clock()
            try:
                required = clip_wei_for(approved_clip_usd, payload.get("bnb_usd"))
                if (any(type(snap.get(key)) is not int or snap[key] < 0
                        for key in ("native_wei", "gas_price_wei"))
                        or snap["gas_price_wei"] <= 0
                        or isinstance(snap.get("read_at"), bool)
                        or not math.isfinite(float(snap.get("read_at")))
                        or not 0 <= now - float(snap["read_at"]) <= self.max_snapshot_age_s
                        or isinstance(payload.get("bnb_usd"), bool)
                        or not math.isfinite(float(payload.get("bnb_usd")))
                        or float(payload.get("bnb_usd")) <= 0
                        or isinstance(payload.get("bnb_usd_at"), bool)
                        or not math.isfinite(float(payload.get("bnb_usd_at")))
                        or not 0 <= now - float(payload["bnb_usd_at"]) <= self.eth_usd_max_age_s):
                    return None
                generation = self.generation_fn()
                if (isinstance(generation, bool) or generation is None
                        or not math.isfinite(float(generation))):
                    return None
                why = inputs_reason(dict(payload, eth_usd=payload.get("bnb_usd"),
                                         eth_usd_at=payload.get("bnb_usd_at")),
                                    generation=generation, now_host=now,
                                    clip_wei=required, gas_limit=V2_BUY_GAS,
                                    low_wei=self.low_wei,
                                    max_snapshot_age_s=self.max_snapshot_age_s,
                                    eth_usd_max_age_s=self.eth_usd_max_age_s,
                                    generation_margin_s=self.generation_margin_s)
            except (TypeError, ValueError, OverflowError, OSError):
                return None
            rec.setdefault("execution_evidence", {})["funding_precheck"] = {
                "approved_clip_usd": approved_clip_usd, "clip_wei": required,
                "native_wei": snap["native_wei"], "gas_price_wei": snap["gas_price_wei"],
                "gas_reserve_wei": V2_BUY_GAS * snap["gas_price_wei"] * 3,
                "base_reserve_wei": self.low_wei,
                "snapshot_age_ms": (now - float(snap["read_at"])) * 1000,
                "reason": why}
            return "float_short" if why == "float_short" else None

        quote_usd = int(snap["weth_wei"]) / 10 ** 18 * float(payload["bnb_usd"])
        native = int(snap["native_wei"]) / 10 ** 18
        contexts = {**self.contexts, CHAIN: dict(
            self.ctx, usdc_fn=lambda: quote_usd, native_fn=lambda: native,
            token_safety_fn=lambda m_, usd=None: self.cache.cached_answer(m_, "bsc_fast_safety_cache"),
            execute_fn=execute_fn, warm_quote_price_fn=None, head_fn=None,
            native_funding_precheck_fn=native_funding_precheck)}
        if needs_inline:
            # Match Robinhood: the shared hook checks admission and claims the coin
            # before cold safety reads. A passing facts/market read is not safety.
            def inline_safety(mint, usd=None):
                why = self._inline(mint, rec)
                if why is not None:
                    rec["_outcome"] = why
                    return False, why
                return self.cache.cached_answer(mint, "bsc_fast_inline")
            contexts[CHAIN] = dict(contexts[CHAIN], token_safety_fn=inline_safety)
        hook = self.hook_factory(contexts, stats[0])
        if self.stopped or getattr(self, "_stop_owner", self).stopped:
            return done("operator_stop")
        with refusal_pages_off():
            res = hook.attempt(self.conn, self._stamp_intent(
                self._lane_event(payload, market_cap=mcap), payload, rec)) or {}
        return done(rec.pop("_outcome", None) or ("float_short" if
                    res.get("status") == "aborted" and res.get("reason") == "float_short"
                    else hook_reason(res)),
                    pending_id=res.get("pending_id"), position_id=res.get("position_id"),
                    status=res.get("status"))

    def _initiation_refusal(self, payload):
        """None when the source's initiation is proved for this datagram, else the reason (the Relay
        pre-buy's lane overrides it with its own proof)."""
        return bsc_payload_initiation_refusal(payload)

    def _lane_event(self, payload, *, market_cap):
        return BscLaneEvent(payload, market_cap=market_cap)

    def _quote_asset_usd(self, payload):
        return payload["bnb_usd"]

    def _expected_out(self, payload, clip_wei):
        return expected_out_for(clip_wei, payload)

    def _buy_tx(self, payload, clip_wei, deadline, floor=0):
        expected = expected_out_for(clip_wei, payload)
        # ONE DECISION PER TRANSACTION: the floor these bytes are signed with and the send set they
        # go to are read together here and carried on the payload, so a knob flip between the build
        # and the send can never put a cap-floor transaction in a public mempool.
        at_cap = floor_at_cap_enabled()
        payload["_private_only"] = at_cap
        min_out = max(min_out_for(clip_wei, payload, expected_out=expected, floor_at_cap=at_cap),
                      int(floor))
        # Use the final approved clip: a smaller clip may fit a hop the pilot cap cannot.
        if payload.get("hop") and expected < min_out:
            return {}, 0  # existing build_refused path releases before lock/signing/send
        return v2_native_buy_tx(payload, clip_wei, min_out, deadline, self.taker), min_out

    def _floor_evidence(self, payload, clip_wei, floor, min_out):
        from sniper.settings import memecoin as cfg
        if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
            return {"slippage": str(min_out_for(clip_wei, payload)), "copy": "0",
                    "signed_minimum": str(min_out), "binding": ["slippage"],
                    "policy": "quote_slippage"}
        # The existing builder's policies evaluated separately, with identical arithmetic. Under the
        # cap floor the 3% slippage rule is still computed: a diagnostic, so the read can count the
        # fills it would have reverted (delivered < slippage).
        cap = cap_min_out(clip_wei, payload)
        expected = expected_out_for(clip_wei, payload)
        from sniper.settings import memecoin as cfg
        slippage = int(Fraction(expected) * (1 - Fraction(str(cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT)) / 100))
        at_cap = bool(payload.get("_private_only"))
        floors = {"cap": cap, "copy": int(floor)}
        diagnostics = {"slippage": str(slippage)}
        if at_cap:
            follow = follow_lane_chase_cap(payload)
            if follow is not None:
                floors["follow_chase"] = cap_min_out(clip_wei, payload, cap=follow)
            # The retired post x 1.2 term, kept so a read can count the fills it would have
            # reverted (眉眉, 2026-10-16).
            slow = slow_lane_premium_cap(payload)
            if slow is not None:
                diagnostics["slow_premium"] = str(cap_min_out(clip_wei, payload, cap=slow))
        else:
            floors["slippage"] = slippage
        return {**{k: str(v) for k, v in floors.items()}, **diagnostics,
                "policy": "cap" if at_cap else "strict", "signed_minimum": str(min_out),
                "binding": [k for k, v in floors.items() if v == min_out]}

    def _gas_price(self, snap):
        return max(super()._gas_price(snap), self.gas_floor_wei)

    def _gas_bid(self, payload, rec, trace, clip_wei, gas_limit):
        """The crowded gas bid (memecoin_bsc_crowded_gas): the treated arm's gasPrice, else None
        (today's price). In memory: the snapshot float, the paired head, the arm's hash."""
        crowded = self.crowded
        if crowded is None:
            return None
        from sniper.services.memecoin_bsc_crowded_gas import SUBSTAGE
        snap = payload["snapshot"]
        try:
            static = self._gas_price(snap)
            out = crowded.arm(
                getattr(trace, "attempt_id", None) or rec.get("attempt_id"),
                private_only=bool(payload.get("_private_only")),
                source_block_ms=payload.get("source_block_ms"), source_head_at=payload.get("source_head_at"),
                now_ms=self.host_clock() * 1000.0, static_wei=static, gas_limit=gas_limit,
                float_ok_fn=lambda price: inputs_float_ok(snap, clip_wei, gas_limit, price, self.low_wei))
        except Exception:
            logger.exception("bsc fast lane: crowded gas arm failed (today's price)")
            return None
        price, bound = out["gas_price_wei"], out["fee_bound_wei"]
        try:
            fee_usd = bound / 1e18 * float(payload["bnb_usd"])
        except (KeyError, TypeError, ValueError):
            fee_usd = None
        fields = dict(out, gas_price_gwei=price / 1e9, fee_bnb_est=bound / 1e18, fee_usd_est=fee_usd)
        rec.setdefault("execution_evidence", {})["crowded_gas"] = fields
        if trace is not None:
            try:
                trace.mark_substage(SUBSTAGE, **fields)
            except Exception:
                pass
        if out.get("arm") != "treated":
            return None
        rec["gas_bound_wei"] = bound
        return price

    def _send(self, payload, rec, conn, pending_id, tx, clip_wei, trace):
        """Today's send; a treated crowded bid counts its bound once its signed bytes may land: not
        on a give-back (status aborted: unsigned, unbound or rejected by every member)."""
        def count():
            if self.crowded is not None and rec.get("gas_bid_wei") and rec.get("tx_hash"):
                self.crowded.sent(rec["tx_hash"], rec.get("gas_bound_wei"))
        try:
            result = super()._send(payload, rec, conn, pending_id, tx, clip_wei, trace)
        except BaseException:
            count()
            raise
        if not (isinstance(result, dict) and result.get("status") == "aborted"):
            count()
        return result

    def _settle(self, payload, rec, conn, pending_id, tx_hash, decimals, receipt, trace):
        """A treated bid's receipt fee replaces its bound, landed or reverted (both paid gas)."""
        if self.crowded is not None and rec.get("gas_bid_wei") and isinstance(receipt, dict):
            from sniper.execution.evm_swap_executor import _receipt_fee_paid
            from sniper.services.memecoin_bsc_crowded_gas import SUBSTAGE_PAID
            fee = (_receipt_fee_paid(receipt)[0]
                   if str(receipt.get("transactionHash") or "").lower() == str(tx_hash).lower() else None)
            if fee is not None:
                self.crowded.paid(tx_hash, fee)
                rec.setdefault("execution_evidence", {}).setdefault("crowded_gas", {})["fee_bnb_actual"] = (
                    fee / 1e18)
                if trace is not None:
                    try:
                        trace.mark_substage(SUBSTAGE_PAID, fee_wei=fee, tx_sig=tx_hash)
                    except Exception:
                        pass
        return super()._settle(payload, rec, conn, pending_id, tx_hash, decimals, receipt, trace)

    def _book(self, conn, pending, receipt, tx_seen, block, payload, decimals):
        from sniper.execution.evm_swap_executor import recover_confirmed_lane_entry
        return recover_confirmed_lane_entry(conn, pending, receipt, tx_seen, block,
                                            owner=self.ctx["address"],
                                            decimals_fn=lambda _mint: int(decimals),
                                            network_id=CHAIN, source=payload["pool"])

    def _receipt_amounts(self, receipt, tx_seen, payload):
        from sniper.execution.evm_swap_executor import lane_receipt_amounts
        return lane_receipt_amounts(receipt, tx_seen, self.ctx["address"], payload["mint"],
                                    pool_manager=payload["pool"])


_LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS bsc_lane_live (
    id INTEGER PRIMARY KEY, source_tx TEXT, our_tx TEXT, mint TEXT, reason TEXT, pending_id INTEGER,
    position_id INTEGER, blocks_behind INTEGER, send_ms REAL, decide_to_send_ms REAL,
    send_reply TEXT, lock_ms REAL, safety_path TEXT, safety_ms REAL, busy_ms REAL,
    written_at TEXT, parallel_safety TEXT);
CREATE TABLE IF NOT EXISTS bsc_lane_fill_checks (
    id INTEGER PRIMARY KEY, our_tx TEXT, position_id INTEGER, receipt_got TEXT, delta_got TEXT,
    agree INTEGER, written_at TEXT);
"""


def _write_live(db_path, sql, values, *, live_row=False):
    import sqlite3
    db = sqlite3.connect(db_path, timeout=2)
    try:
        db.executescript(_LIVE_SCHEMA)
        if live_row:            # only the lane thread's own row: the fill-check thread never ALTERs
            from sniper.services.memecoin_rh_fast_lane import add_missing_columns
            add_missing_columns(db, "bsc_lane_live", (("copy_floor", "TEXT"), ("parallel_safety", "TEXT"), ("execution_evidence_json", "TEXT")))
        db.execute(sql, values)
        db.commit()
    finally:
        db.close()


def write_live(db_path, rec):
    from sniper.util.clock import real_utc_now_iso
    _write_live(db_path, "INSERT INTO bsc_lane_live (source_tx, our_tx, mint, reason, pending_id, "
                "position_id, blocks_behind, send_ms, decide_to_send_ms, send_reply, lock_ms, "
                "safety_path, safety_ms, busy_ms, copy_floor, written_at, parallel_safety, execution_evidence_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec.get("source_tx"), rec.get("tx_hash"), rec.get("mint"), rec.get("reason"),
                 rec.get("pending_id"), rec.get("position_id"), rec.get("blocks_behind"),
                 rec.get("send_ms"), rec.get("decide_to_send_ms"), rec.get("send_reply"),
                 rec.get("lock_ms"), rec.get("safety_path"), rec.get("safety_ms"),
                 rec.get("busy_ms"), None if rec.get("copy_floor") is None else str(rec["copy_floor"]),
                 real_utc_now_iso(), rec.get("parallel_safety"),
                 json.dumps(rec.get("execution_evidence"), sort_keys=True)), live_row=True)
    if (rec.get("reason") == "float_short" and rec.get("pending_id") is not None
            and rec.get("tx_hash") is None and rec.get("position_id") is None):
        # The first watcher wake saw a pending native owner; recheck its unsigned fallback now.
        from pathlib import Path
        from sniper.services.memecoin_onchain_wake import send_wake, wake_address
        send_wake(wake_address(str(Path(db_path).resolve().parent.parent)))


def fill_check_sink(db_path):
    def sink(row):
        from sniper.util.clock import real_utc_now_iso
        _write_live(db_path, "INSERT INTO bsc_lane_fill_checks (our_tx, position_id, receipt_got, "
                    "delta_got, agree, written_at) VALUES (?,?,?,?,?,?)",
                    (row["tx_hash"], row["position_id"], str(row["receipt_got"]),
                     None if row["delta_got"] is None else str(row["delta_got"]),
                     int(bool(row["agree"])), real_utc_now_iso()))
    return sink


def build_live(contexts, ctx, *, log, lock_key, db_path, url, connect=None, sender=None,
               dex=None, hook_factory=None, http_base=None):
    """The live lane's objects on the lane's own host `url` (already host-checked), or None. Nothing
    here starts a thread or sends a request."""
    from sniper.execution.evm_wallet import _rpc
    from sniper.services.memecoin_evm_gas_topup import gas_pass_bounds
    from sniper.services.memecoin_rh_fast_lane import (
        DexBudget, PacedHttp, SafetyCache, _seed_pilot, lane_generation_fn)
    from sniper.settings import memecoin as cfg
    if not callable(ctx.get("lane_claim_nonce_fn")):
        log("[bsc-fast-lane] live refused: the 56 context has no lane nonce floor")
        return None
    address = ctx["address"]
    paced = PacedHttp(http_base, min_gap_s=cfg.MEMECOIN_BSC_FAST_SAFETY_MIN_GAP_S, pause_s=120.0)
    http = lane_only_http(paced, url)
    host = LaneHost(url, http)
    quotes = {}
    dex = dex or DexBudget()
    generation_fn = lane_generation_fn(lock_key, CHAIN)
    def decimals_hint(mint):                 # the cache below; resolved when a check runs
        return cache.decimals_hint(mint)
    cache = BscSafetyCache(lane_safety_fn(url, http, address, quotes, decimals_hint=decimals_hint),
                        ttl_s=cfg.MEMECOIN_BSC_FAST_SAFETY_TTL_S,
                        usd=cfg.MEMECOIN_BSC_FAST_LANE_PILOT_CLIP_USD, facts_fn=lane_facts(host),
                        stats_fn=lambda mint: dex.stats(mint, network_id=CHAIN),
                        supply_ttl_s=cfg.MEMECOIN_FAST_LANE_SUPPLY_TTL_S,
                        facts_sink=ctx.get("put_token_facts_fn"), quotes=quotes,
                        generation_fn=generation_fn)
    cache.http_factory = lambda: PreparationReads(cache, paced, url)
    if connect is None:
        from sniper.persistence.db_core import get_connection as connect
    conn = connect()
    conn.execute("PRAGMA busy_timeout=200")
    fills, sends = _seed_pilot(conn, signal_source="bsc_fast_lane")
    rpc_url = ctx.get("rpc_url")

    def rpc_read(method, params):
        # The fill's own receipt, transaction and block on the funded custody host, as today's
        # executor reads them (never the lane's paced budget: the receipt wait is on the fill path).
        return (_rpc(rpc_url, method, params) or {}).get("result")

    def balance_at(mint, block):
        from sniper.execution.evm_wallet import SEL_BALANCE_OF
        data = "0x" + SEL_BALANCE_OF + str(address)[2:].lower().rjust(64, "0")
        for _ in range(10):                  # the custody node can trail the head by a block
            result = rpc_read("eth_call", [{"to": mint, "data": data}, hex(int(block))])
            if isinstance(result, str) and len(result) > 2:
                return int(result, 16)
            time.sleep(0.5)
        return None
    token = cfg.memecoin_bsc_blockrazor_auth()
    reoffer = ctx.get("reoffer_send_fn")
    inline_base = http_base
    if inline_base is None:
        # KEPT ALIVE (BSC cut 4b, 2026-10-14): the inline seam paid a new TCP+TLS connection per
        # request through urllib, on the fill path. Its own pool, for this lane host only (the seam
        # refuses any other target), sized to one inline check's widest burst (the safety's three
        # readers, both slot tails, the facts) so no read queues in it. Never retried: a stale
        # connection is one transport failure, counted by the seam, and nothing is cached from it.
        # No warm ping: the 4663 inline host has none. The check's deadline still bounds each read.
        from sniper.execution.evm_token_safety import _SLOT_TAIL_WORKERS
        from sniper.execution import fast_http
        from sniper.execution.submission_backends import register_client
        size = 8 + 2 * int(_SLOT_TAIL_WORKERS)
        inline_base = register_client(fast_http.PooledHttpClient(
            max_connections=size, max_keepalive=size)).request
    def inline_seam(own_paced):
        """The inline seam's pieces over `own_paced` (its 429 pause, dead marking and counts), on the same
        host and kept-alive pool: the live lane's over `paced`; the Relay pre-buy's over its own pacer, so
        its failures never pause or kill the live lane's inline path (Gamma LOW 4, Alpha 2026-10-14)."""
        http = lane_inline_http(url, own_paced, base=inline_base)
        return dict(
            inline_safety_fn=lane_safety_fn(
                url, http, address, quotes, deadline_scope=inline_deadline,
                decimals_hint=decimals_hint,
                deadline_s=cfg.MEMECOIN_BSC_FAST_INLINE_SAFETY_DEADLINE_S),
            inline_facts_fn=lane_facts(LaneHost(url, http)), inline_host=own_paced), LaneHost(url, http)
    seam, lane_host = inline_seam(paced)
    low_rtt, low_url = None, low_rtt_url(cfg)
    if low_url is not None:
        # The cold signal's and the intent's facts and safety reads, on the funded pool's Alchemy
        # member while it is healthy (LowRttReads); the lane host stays every read's fallback.
        if http_base is not None:
            low_base = http_base
        else:
            from sniper.execution.evm_token_safety import _SLOT_TAIL_WORKERS
            from sniper.execution import fast_http
            from sniper.execution.submission_backends import register_client
            size = 8 + 2 * int(_SLOT_TAIL_WORKERS)
            low_base = register_client(fast_http.PooledHttpClient(
                max_connections=size, max_keepalive=size)).request
        low_rtt = LowRttReads(low_url, low_base)
        seam = low_rtt.wrap(seam, fast_facts=lane_facts(LaneHost(low_url, low_rtt.http)),
                            fast_safety=lane_safety_fn(
                                low_url, low_rtt.http, address, quotes, deadline_scope=inline_deadline,
                                decimals_hint=decimals_hint,
                                deadline_s=cfg.MEMECOIN_BSC_FAST_INLINE_SAFETY_DEADLINE_S))
    live_kwargs = dict(
        **seam,
        stats_fn=lambda mint: dex.stats(mint, network_id=CHAIN),
        inline_deadline_s=cfg.MEMECOIN_BSC_FAST_INLINE_SAFETY_DEADLINE_S,
        snapshot=Snapshot(host, address), cache=cache, quotes=quotes,
        gas_floor_wei=cfg.MEMECOIN_BSC_FAST_LANE_GAS_FLOOR_WEI, ctx=ctx, contexts=contexts,
        conn=conn, sender=sender or BscSender(
            default_members(), bundle=blockrazor_bundle(token) if token else None),
        fanout_fn=(lambda raw: reoffer(str(raw).encode("ascii"))) if callable(reoffer) else (
            lambda raw: None),
        rpc_read=rpc_read, lock_key=lock_key, lock_dir=None,
        clip_usd=cfg.MEMECOIN_BSC_FAST_LANE_PILOT_CLIP_USD,
        # 56's own BNB low-water mark (Gamma): 4663's ETH value would let a lane fill leave native
        # under the mark funded 56 entries refuse for gas below.
        low_wei=gas_pass_bounds(CHAIN)["low_water_wei"],
        generation_fn=generation_fn,
        host_clock=time.time, max_snapshot_age_s=cfg.MEMECOIN_BSC_FAST_LANE_SNAPSHOT_MAX_AGE_S,
        eth_usd_max_age_s=cfg.MEMECOIN_RH_FAST_LANE_ETH_USD_MAX_AGE_S,
        generation_margin_s=cfg.MEMECOIN_RH_FAST_LANE_GENERATION_MARGIN_S,
        pilot_fills=cfg.MEMECOIN_BSC_FAST_LANE_PILOT_FILLS,
        max_sends_per_hour=cfg.MEMECOIN_BSC_FAST_LANE_MAX_SENDS_PER_HOUR,
        lock_receipt_cap_s=cfg.MEMECOIN_BSC_FAST_LANE_LOCK_RECEIPT_CAP_S, hook_factory=hook_factory,
        fills=fills, send_times=sends, balance_at_fn=balance_at, check_sink=fill_check_sink(db_path),
        revert_reason_fn=lambda h: _revert_reason(h, rpc_url))
    live = BscLive(**live_kwargs)
    # The crowded gas bid on this lane only (never in live_kwargs: the Relay pre-buy's lane has none),
    # seeded with today's treated spend so a restart keeps the cap.
    from sniper.services.memecoin_bsc_crowded_gas import BscCrowdedGas
    from sniper.util.clock import real_utc_now_iso
    live.crowded = BscCrowdedGas(now_iso_fn=real_utc_now_iso)
    live.crowded.seed(conn)
    # The Relay pre-buy (memecoin_bsc_relay_prebuy) builds its own lane from these, and prices on the
    # lane's own host through the inline seam (unqueued, inside its own deadline).
    live.live_kwargs, live.lane_host, live.inline_seam = live_kwargs, lane_host, inline_seam
    live.low_rtt = low_rtt
    live.paced = paced
    def live_capacity():
        return (not paced.paused() and not paced.dead() and not live.entry_active
                and time.monotonic() - live.entry_ended_at >= WARM_ENTRY_COOLDOWN_S
                and (live._safety_future is None or live._safety_future.done())
                and (live._inputs_future is None or live._inputs_future.done()))
    def speculative_capacity():
        snap = live.snapshot.current()
        return (live_capacity() and not live.snapshot.refreshing.is_set() and snap is not None
                and 0 <= time.time() - snap["read_at"] < cfg.MEMECOIN_BSC_FAST_LANE_SNAPSHOT_PERIOD_S)
    def snapshot_deferral():
        snap = live.snapshot.current()
        return (live_capacity() and (live.snapshot.refreshing.is_set() or snap is None
                or not 0 <= time.time() - snap["read_at"] < cfg.MEMECOIN_BSC_FAST_LANE_SNAPSHOT_PERIOD_S))
    cache.admission_fn = speculative_capacity
    cache.deferral_fn = snapshot_deferral
    ctx['early_quote_balances_fn'] = live.early_quote_balances
    ctx['entry_signing_snapshot_fn'] = live.entry_signing_snapshot
    return live
