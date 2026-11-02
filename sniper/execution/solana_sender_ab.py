"""Helius Sender SWQoS-only A/B on the Solana slot lane (2026-10-01).

Spec: docs/superpowers/specs/2026-10-01-memecoin-slot-lane-sender-ab-design.md. Read: Gamma's
pre-registration docs/audits/2026-10-01-sender-swqos-ab-preregistration.md (99679222a).

The operator's YES (2026-10-01, via Alpha) lifts the 07-25 no-paid-submission rule for THIS TEST
ONLY: the paid submit modes still fail startup, and nothing but a slot-lane entry built with a tip
ever reaches Sender. The arm is a pure function of the attempt id, fixed before the build. Arm B
appends one 5,000-lamport SystemProgram transfer to a Helius tip account; its signed bytes go to
today's fan-out and to Sender in the same transport call. The provider decides from the BYTES:
it posts only a payload carrying exactly one tip transfer, so an untipped send (arm A, every exit,
every other lane) is NOT_SENT, which the aggregate treats as neutral.

NOZOMI A/B (operator YES 2026-10-09 via Alpha; Gamma's pre-registration
docs/audits/2026-10-09-nozomi-slot-lane-ab-preregistration.md, f7760ad8b). For eligible wallets the
slot lane splits on the same hash under its own rule: arm A is today's Sender send, arm B replaces the
Sender tip with ONE 5,000,000-lamport transfer to a Nozomi tip account. Nozomi is one more provider of
this class: it posts only a payload carrying exactly that tip, on its own transport worker, in parallel
with free-direct. JSON-RPC (it returns the signature, so the shared classifier applies); the URL holds
the API key, so it is never logged.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import os
import struct
import threading
import time as _time

from sniper.execution.submission_transport import AttemptState, SubmissionAttempt

RULE = "sha256-attempt-odd-v1"
PROVIDER_ID = "solana:sender"
SENDER_URL = "http://fra-sender.helius-rpc.com/fast?swqos_only=true"
SENDER_PING_URL = "http://fra-sender.helius-rpc.com/ping"
TIP_LAMPORTS = 5_000                  # the documented SWQoS-only minimum; a retune is a new stamp
TIP_CAP_LAMPORTS = 10_000             # Gamma's early-stop bound: any tip above it is refused
AB_VENUES = frozenset({"pump_curve", "pump_amm"})
SYSTEM_PROGRAM = "11111111111111111111111111111111"
# Helius Sender tip accounts (mainnet-beta), read from helius.dev/docs/sending-transactions/sender
# on 2026-10-01.
TIP_ACCOUNTS = (
    "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE",
    "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ",
    "9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta",
    "5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn",
    "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD",
    "2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ",
    "wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF",
    "3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT",
    "4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey",
    "4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or",
)
_TIP_SET = frozenset(TIP_ACCOUNTS)
_TRANSFER = struct.pack("<I", 2)       # SystemInstruction::Transfer

NOZOMI_PROVIDER_ID = "solana:nozomi"
NOZOMI_RULE = "nozomi-sha256-attempt-odd-v1"
# Frankfurt direct: warm keep-alive /ping p50 8 ms from this host (Alpha), and a malformed JSON-RPC
# POST p50 7.9 / p90 8.5 ms on the same warm connection (Beta, 2026-10-09; http within 0.3 ms).
NOZOMI_URL = "https://fra2.nozomi.temporal.xyz/?c="
NOZOMI_PING_URL = "https://fra2.nozomi.temporal.xyz/ping"
NOZOMI_TIP_LAMPORTS = 5_000_000       # 0.005 SOL, the pre-registered tip; a retune is a new stamp
NOZOMI_MIN_TIP_LAMPORTS = 1_000_000   # provider minimum verified 2026-10-12
NOZOMI_CAPPED_RULE = "nozomi-capped-bid-v1"
NOZOMI_FEE_SPLIT_RULE = "nozomi-fee-split-v1"
NOZOMI_TREATMENT_TIP_LAMPORTS = 6_000_000
# Nozomi tip accounts (mainnet-beta), read from use.temporal.xyz/nozomi/tipping-and-faq on 2026-10-09.
NOZOMI_TIP_ACCOUNTS = (
    "TEMPaMeCRFAS9EKF53Jd6KpHxgL47uWLcpFArU1Fanq",
    "noz3jAjPiHuBPqiSPkkugaJDkJscPuRhYnSpbi8UvC4",
    "noz3str9KXfpKknefHji8L1mPgimezaiUyCHYMDv1GE",
    "noz6uoYCDijhu1V7cutCpwxNiSovEwLdRHPwmgCGDNo",
    "noz9EPNcT7WH6Sou3sr3GGjHQYVkN3DNirpbvDkv9YJ",
    "nozc5yT15LazbLTFVZzoNZCwjh3yUtW86LoUyqsBu4L",
    "nozFrhfnNGoyqwVuwPAW4aaGqempx4PU6g6D9CJMv7Z",
    "nozievPk7HyK1Rqy1MPJwVQ7qQg2QoJGyP71oeDwbsu",
    "noznbgwYnBLDHu8wcQVCEw6kDrXkPdKkydGJGNXGvL7",
    "nozNVWs5N8mgzuD3qigrCG2UoKxZttxzZ85pvAQVrbP",
    "nozpEGbwx4BcGp6pvEdAh1JoC2CQGZdU6HbNP1v2p6P",
    "nozrhjhkCr3zXT3BiT4WCodYCUFeQvcdUkM7MqhKqge",
    "nozrwQtWhEdrA6W8dkbt9gnUaMs52PdAv5byipnadq3",
    "nozUacTVWub3cL4mJmGCYjKZTnE9RbdY5AP46iQgbPJ",
    "nozWCyTPppJjRuw2fpzDhhWbW355fzosWSzrrMYB1Qk",
    "nozWNju6dY353eMkMqURqwQEoM3SFgEKC6psLCSfUne",
    "nozxNBgWohjR75vdspfxR5H9ceC7XXH99xpxhVGt3Bb",
)
_NOZOMI_SET = frozenset(NOZOMI_TIP_ACCOUNTS)


def knob_on(env=None):
    raw = str((os.environ if env is None else env).get("MEMECOIN_SLOT_LANE_SENDER_AB", "") or "")
    return raw.strip().lower() in ("1", "true", "on", "yes")


def with_shyft_on(env=None):
    """MEMECOIN_SOLANA_SENDER_WITH_SHYFT (2026-10-15, default on): Sender may join the Shyft graph.
    Only `0|false|off|no` keeps the Shyft-only refusal; MEMECOIN_SLOT_LANE_SENDER_AB stays the switch."""
    raw = str((os.environ if env is None else env).get("MEMECOIN_SOLANA_SENDER_WITH_SHYFT", "1") or "1")
    return raw.strip().lower() not in ("0", "false", "off", "no")


def assign(attempt_id):
    """The pre-registered arm: B iff the first 32 bits of sha256(attempt_id) are odd. The tip account
    is digest byte 4 mod 10, so the trace can recompute both."""
    digest = hashlib.sha256(str(attempt_id).encode("utf-8")).digest()
    arm = "B" if int.from_bytes(digest[:4], "big") % 2 == 1 else "A"
    return {"arm": arm, "rule": RULE, "attempt_hash": digest[:4].hex(),
            "tip_lamports": TIP_LAMPORTS if arm == "B" else 0,
            "tip_account": TIP_ACCOUNTS[digest[4] % len(TIP_ACCOUNTS)] if arm == "B" else None}


def nozomi_knob_on(env=None):
    raw = str((os.environ if env is None else env).get("MEMECOIN_SLOT_LANE_NOZOMI_AB", "") or "")
    return raw.strip().lower() in ("1", "true", "on", "yes")


def assign_nozomi(attempt_id):
    """Gamma's rule nozomi-sha256-attempt-odd-v1: assign's hash and parity; the tip account is digest
    byte 4 mod 17, so the grader can recompute both."""
    digest = hashlib.sha256(str(attempt_id).encode("utf-8")).digest()
    arm = "B" if int.from_bytes(digest[:4], "big") % 2 == 1 else "A"
    return {"arm": arm, "rule": NOZOMI_RULE, "attempt_hash": digest[:4].hex(),
            "tip_lamports": NOZOMI_TIP_LAMPORTS if arm == "B" else 0,
            "tip_account": (NOZOMI_TIP_ACCOUNTS[digest[4] % len(NOZOMI_TIP_ACCOUNTS)]
                            if arm == "B" else None)}


# DESK FAST SEND (operator YES via Alpha 2026-10-23; Alpha/Gamma: NO A/B, the desk has too few fills for
# a readable split and the operator objects to randomness in the desk). Every desk entry signs ONE Sender
# tip of TIP_LAMPORTS to a Helius tip account (deterministic; the account is digest byte 4 mod 10 of the
# attempt id). Nozomi stays off on the desk. Recorded on the attempt trace as desk_fast_send.
DESK_FAST_SEND_RULE = "desk-fast-send-sender-always-v1"


def desk_fast_send_on(env=None):
    """MEMECOIN_DESK_FAST_SEND, default on (improvements ship with a kill knob)."""
    raw = str((os.environ if env is None else env).get("MEMECOIN_DESK_FAST_SEND", "1") or "1")
    return raw.strip().lower() not in ("0", "false", "off", "no")


def assign_desk_fast_send(attempt_id):
    digest = hashlib.sha256(("%s:%s" % (DESK_FAST_SEND_RULE, attempt_id)).encode("utf-8")).digest()
    return {"arm": "sender", "rule": DESK_FAST_SEND_RULE, "attempt_hash": digest[:4].hex(),
            "sender_tip_lamports": TIP_LAMPORTS, "nozomi_tip_lamports": 0,
            "tip_account": TIP_ACCOUNTS[digest[4] % len(TIP_ACCOUNTS)]}


def nozomi_all_lanes_on(env=None):
    """MEMECOIN_NOZOMI_ALL_LANES (operator 2026-10-09 via Alpha, default off): the Nozomi arm on every
    funded Solana entry lane, not only the slot lane. The slot lane's own A/B never reads it."""
    raw = str((os.environ if env is None else env).get("MEMECOIN_NOZOMI_ALL_LANES", "") or "")
    return raw.strip().lower() in ("1", "true", "on", "yes")


NOZOMI_ALL_RULE = "nozomi-all-b-v1"


def nozomi_all_b_on(env=None):
    """MEMECOIN_NOZOMI_ALL_B (operator 2026-10-09: "everything turned on live"): every listed-wallet
    send is arm B, on the slot lane and (with MEMECOIN_NOZOMI_ALL_LANES) every other lane."""
    raw = str((os.environ if env is None else env).get("MEMECOIN_NOZOMI_ALL_B", "") or "")
    return raw.strip().lower() in ("1", "true", "on", "yes")


def assign_nozomi_all(attempt_id):
    """Arm B for every attempt, under its own rule so these sends never mix with the A/B's; the tip
    account is the one the A/B rule would name."""
    return {**assign_nozomi(attempt_id), "arm": "B", "rule": NOZOMI_ALL_RULE,
            "tip_lamports": NOZOMI_TIP_LAMPORTS,
            "tip_account": NOZOMI_TIP_ACCOUNTS[
                hashlib.sha256(str(attempt_id).encode("utf-8")).digest()[4] % len(NOZOMI_TIP_ACCOUNTS)]}


def nozomi_fee_split_on(env=None):
    """Default-OFF allocation experiment; a partial activation must never enlarge a tip cap."""
    env = os.environ if env is None else env
    raw = str(env.get("MEMECOIN_NOZOMI_FEE_SPLIT_AB", "") or "").strip().lower()
    if raw in ("", "0", "false", "off", "no"):
        return False
    if raw not in ("1", "true", "on", "yes"):
        raise ValueError("invalid_nozomi_fee_split")
    required = ("MEMECOIN_SLOT_LANE_NOZOMI_AB", "MEMECOIN_NOZOMI_ALL_B",
                "MEMECOIN_NOZOMI_ALL_LANES", "MEMECOIN_SOLANA_ENTRY_BIDS_ENABLED")
    if (any(str(env.get(key, "") or "").strip().lower() not in ("1", "true", "on", "yes")
            for key in required) or not str(env.get("MEMECOIN_NOZOMI_API_KEY", "") or "").strip()):
        raise ValueError("invalid_nozomi_fee_split")
    return True


def nozomi_tip_cap_lamports(env=None):
    return NOZOMI_TREATMENT_TIP_LAMPORTS if nozomi_fee_split_on(env) else NOZOMI_TIP_LAMPORTS


def valid_nozomi_tip_budget(lamports):
    """Keep the dynamic range at 0..5M; exactly 6M belongs only to the activated treatment."""
    return type(lamports) is int and (0 <= lamports <= NOZOMI_TIP_LAMPORTS or
            lamports == NOZOMI_TREATMENT_TIP_LAMPORTS and nozomi_fee_split_on())


def nozomi_arm_for(attempt_id, *, tip_lamports_fn=None):
    """Freeze provider membership, recipient and optional C/T allocation before any lane builds."""
    fee_split = nozomi_fee_split_on()
    if fee_split and not attempt_id:
        raise ValueError("invalid_nozomi_fee_split_attempt")
    arm = (assign_nozomi_all if nozomi_all_b_on() else assign_nozomi)(attempt_id)
    if fee_split:
        digest = hashlib.sha256((NOZOMI_FEE_SPLIT_RULE + ':' + str(attempt_id)).encode('utf-8')).digest()
        fee_arm = 'T' if int.from_bytes(digest[:4], 'big') % 2 else 'C'
        arm = {**arm, 'rule': NOZOMI_FEE_SPLIT_RULE, 'fee_arm': fee_arm,
               'fee_assignment_hash': digest[:4].hex()}
        if fee_arm == 'T':
            arm['tip_lamports'] = NOZOMI_TREATMENT_TIP_LAMPORTS
    if arm["arm"] == "B" and tip_lamports_fn is not None and arm.get('fee_arm') != 'T':
        if not fee_split:
            arm = {**arm, "rule": NOZOMI_CAPPED_RULE}
        try:
            tip = tip_lamports_fn()
            if type(tip) is not int or not NOZOMI_MIN_TIP_LAMPORTS <= tip <= NOZOMI_TIP_LAMPORTS:
                raise ValueError("invalid_tip")
            arm["tip_lamports"] = tip
        except Exception:
            arm["tip_failure"] = "invalid_tip"
    if fee_split:
        from sniper.execution.solana_entry_bids import MAX_TOTAL_LAMPORTS
        arm['priority_budget_lamports'] = MAX_TOTAL_LAMPORTS - arm['tip_lamports']
    return arm


def nozomi_wallet_enabled(source_user_id):
    """Transport scope only; the funded caller retains its own arming gate."""
    from sniper.settings import memecoin as cfg
    uid = str(source_user_id or "")
    wallets = cfg.MEMECOIN_SLOT_LANE_NOZOMI_AB_WALLETS
    return bool(uid.strip()) and ("*" in wallets or uid in wallets)


def nozomi_entry_arm(attempt_id, source_user_id, *, tip_lamports_fn=None):
    """The Nozomi arm for a non-slot-lane funded Solana entry, or None: the knob is off, the wallet is
    outside the explicit or wildcard scope, or there is no attempt id to hash. Half/half on the
    slot lane's rule. The caller still needs the Nozomi provider in its transport."""
    if not attempt_id or not nozomi_all_lanes_on():
        return None
    if not nozomi_wallet_enabled(source_user_id):
        return None
    return nozomi_arm_for(attempt_id, tip_lamports_fn=tip_lamports_fn)


def nozomi_gas_extra_sol(attempt_id, source_user_id):
    """SOL the entry gas gate holds back for an arm-B tip (0 otherwise): the tip leaves the same SOL
    balance the fee and the full-book exit reserve come from."""
    arm = nozomi_entry_arm(attempt_id, source_user_id)
    return arm["tip_lamports"] / 1e9 if arm and arm["arm"] == "B" else 0.0


PACKET_LIMIT = 1232


def with_nozomi_tip(unsigned_b64, payer, tip_account, lamports):
    """The unsigned base64 transaction with ONE SystemProgram transfer payer -> tip_account appended,
    or None when it cannot be done exactly (then the caller sends the original bytes untipped).

    A prebuilt v0 message (Jupiter /swap, the pump-native and Raydium fallbacks) is extended in place,
    with no network read: the tip account goes in as the LAST writable non-signer static key, so
    every account index at or after it, static or loaded from a lookup table, moves up by one and
    every instruction keeps naming the same accounts. The header does not change. Refused: a legacy
    message, a payer that is not static key 0, a tip account already among the static keys, a
    system program that is not a static key (a program id cannot come from a lookup table), an index
    past 255, and a result over the 1232-byte packet."""
    from solders.instruction import CompiledInstruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    try:
        tx = VersionedTransaction.from_bytes(base64.b64decode(unsigned_b64))
        msg = tx.message
        if not isinstance(msg, MessageV0):
            return None
        keys = list(msg.account_keys)
        names = [str(k) for k in keys]
        if not names or names[0] != str(payer) or str(tip_account) in names \
                or SYSTEM_PROGRAM not in names:
            return None
        header = msg.header
        at = len(keys) - header.num_readonly_unsigned_accounts
        if at < header.num_required_signatures:
            return None

        def moved(index):
            return index + 1 if index >= at else index
        instructions = [CompiledInstruction(moved(ix.program_id_index), bytes(ix.data),
                                            bytes(moved(a) for a in bytes(ix.accounts)))
                        for ix in msg.instructions]
        keys.insert(at, Pubkey.from_string(str(tip_account)))
        instructions.append(CompiledInstruction(
            [str(k) for k in keys].index(SYSTEM_PROGRAM),
            _TRANSFER + struct.pack("<Q", int(lamports)), bytes([0, at])))
        out = MessageV0(header, keys, msg.recent_blockhash, instructions,
                        list(msg.address_table_lookups))
        raw = bytes(VersionedTransaction.populate(out, list(tx.signatures)))
    except Exception:
        return None
    return base64.b64encode(raw).decode() if len(raw) <= PACKET_LIMIT else None


def nozomi_send_trace(trace, provider, sig, signed_b64, tip_lamports):
    """The `nozomi_ab_send` substage after a send: Nozomi's own answer, whether it posted the bytes we
    signed and named our signature, and its POST start on the trace's clock."""
    from sniper.execution.submission_transport import sha256_hex
    outcome = provider.outcome(str(sig)) or {"nozomi_state": "not_sent"}
    sha = outcome.pop("nozomi_sha256", None)
    started = outcome.pop("nozomi_post_started", None)
    finished = outcome.pop("nozomi_post_finished", None)
    returned = outcome.get("nozomi_returned_sig")
    trace.mark_substage(
        "nozomi_ab_send", tip_lamports=int(tip_lamports), **outcome,
        nozomi_payload_sha_match=None if sha is None else sha == sha256_hex(signed_b64),
        nozomi_sig_match=None if returned is None else returned == str(sig),
        nozomi_post_finish_ms=(None if finished is None or not hasattr(trace, "elapsed_ms_at")
                               else round(trace.elapsed_ms_at(finished), 3)),
        nozomi_post_start_ms=(None if started is None or not hasattr(trace, "elapsed_ms_at")
                              else round(trace.elapsed_ms_at(started), 3)))


RULE_ALL = "all-venues-b-v1"
RULE_AB_ALL = "sha256-attempt-odd-all-venues-v1"


def assign_ab_all(attempt_id):
    """MEMECOIN_SLOT_LANE_SENDER_ALL=ab: the A/B's own arm and tip account on every venue, renamed so
    the new cohort never mixes with the pump-only one."""
    return {**assign(attempt_id), "rule": RULE_AB_ALL}


def assign_all(attempt_id):
    """MEMECOIN_SLOT_LANE_SENDER_ALL: arm B for every attempt, under its own rule (not the A/B's), with
    the tip account the A/B rule would name, so the trace can recompute it."""
    digest = hashlib.sha256(str(attempt_id).encode("utf-8")).digest()
    return {"arm": "B", "rule": RULE_ALL, "attempt_hash": digest[:4].hex(), "tip_lamports": TIP_LAMPORTS,
            "tip_account": TIP_ACCOUNTS[digest[4] % len(TIP_ACCOUNTS)]}


def tip_instructions(payer, tip):
    """[] or the one tip transfer for a `tip` = (tip_account, lamports), appended last by a builder."""
    if not tip:
        return []
    from solders.pubkey import Pubkey
    from solders.system_program import transfer
    account, lamports = tip
    return [transfer({"from_pubkey": Pubkey.from_string(str(payer)),
                      "to_pubkey": Pubkey.from_string(account), "lamports": int(lamports)})]


_TRANSFER_WITH_SEED = struct.pack("<I", 11)   # SystemInstruction::TransferWithSeed


def loaded_transfer_destination(message):
    """True when a transfer-shaped SystemProgram instruction (Transfer / TransferWithSeed) sends to an account that
    is NOT a static key (loaded from an address lookup table): the destination cannot be read from the bytes, so it
    cannot be shown not to be a known tip account (Codex/Gamma on 20cff3792). Live check 10-22: 0 of 272 signed
    entries carry one."""
    keys = [str(k) for k in message.account_keys]
    for ix in message.instructions:
        if not 0 <= ix.program_id_index < len(keys) or keys[ix.program_id_index] != SYSTEM_PROGRAM:
            continue
        data, accounts = bytes(ix.data), bytes(ix.accounts)
        if data[:4] in (_TRANSFER, _TRANSFER_WITH_SEED) and any(i >= len(keys) for i in accounts):
            return True
    return False


def tip_transfers(message, tip_set=_TIP_SET):
    """[(tip_account, lamports)] for every SystemProgram transfer to an account in `tip_set` (the Helius
    tip accounts unless given) in a compiled message. A destination loaded from a lookup table cannot be
    a tip account we appended (try_compile keeps a key static unless a table holds it) and is not
    counted."""
    keys = [str(k) for k in message.account_keys]
    found = []
    for ix in message.instructions:
        if keys[ix.program_id_index] != SYSTEM_PROGRAM:
            continue
        data, accounts = bytes(ix.data), bytes(ix.accounts)
        if len(data) != 12 or data[:4] != _TRANSFER or len(accounts) < 2 or accounts[1] >= len(keys):
            continue
        if keys[accounts[1]] in tip_set:
            found.append((keys[accounts[1]], struct.unpack("<Q", data[4:])[0]))
    return found


def guard_refusal(message, expected_lamports, nozomi_lamports=0, *, dynamic_nozomi=False):
    """None when the message tips exactly what the arm asked for, else the reason: the Sender tip
    `expected_lamports` (0 on arm A, never above the cap) and the Nozomi tip `nozomi_lamports` (0, or
    its fixed or activated dynamic Nozomi stamp), at most one tip in all. Runs before signing; a
    refusal means nothing is signed or sent."""
    tips = tip_transfers(message)
    nozomi = tip_transfers(message, _NOZOMI_SET)
    seen = sum(lamports for _a, lamports in tips)
    seen_nozomi = sum(lamports for _a, lamports in nozomi)
    if len(tips) + len(nozomi) > 1:
        return "sender_ab_tip_guard: %d tip transfers" % (len(tips) + len(nozomi))
    if seen > TIP_CAP_LAMPORTS:
        return "sender_ab_tip_guard: tip %d over the %d cap" % (seen, TIP_CAP_LAMPORTS)
    if seen != int(expected_lamports or 0):
        return "sender_ab_tip_guard: tip %d where the arm asked %d" % (seen, int(expected_lamports or 0))
    asked = int(nozomi_lamports or 0)
    allowed = (asked == 0 or NOZOMI_MIN_TIP_LAMPORTS <= asked and valid_nozomi_tip_budget(asked)) \
        if dynamic_nozomi else asked in (0, NOZOMI_TIP_LAMPORTS)
    if not allowed:
        return "sender_ab_tip_guard: Nozomi tip %d is not the %d stamp" % (asked, NOZOMI_TIP_LAMPORTS)
    if seen_nozomi != asked:
        return "sender_ab_tip_guard: Nozomi tip %d where the arm asked %d" % (seen_nozomi, asked)
    return None


def payload_tip(signed_b64, tip_set=_TIP_SET, cap=TIP_CAP_LAMPORTS, exact=None, minimum=1):
    """The one tip to `tip_set` in a signed payload: lamports, 0 for none, None when unreadable or not
    exactly one tip of 1..cap (or of exactly `exact`, when given)."""
    from solders.transaction import VersionedTransaction
    try:
        tips = tip_transfers(VersionedTransaction.from_bytes(base64.b64decode(signed_b64)).message,
                             tip_set)
    except Exception:
        return None
    if not tips:
        return 0
    if len(tips) != 1 or not minimum <= tips[0][1] <= cap or (exact is not None and tips[0][1] != exact):
        return None
    return tips[0][1]


class SenderProvider:
    """Helius Sender as one more fan-out provider. It posts only a payload carrying exactly one tip.

    The transport may return on another provider's accept while this call is still in flight, and
    then records Sender as ambiguous at its deadline. So the provider keeps its OWN answer per
    identity (HTTP status, ms, state, the sha256 it sent), and the slot lane reads it into the trace
    after the send. Capped, under its own lock; never read on the send path.

    NozomiProvider is this class with Nozomi's endpoint, tip accounts and exact tip; its outcome keys
    carry the `nozomi_` prefix."""

    provider_id = PROVIDER_ID
    send_after_accept = True       # the transport never cancels it once another provider accepted
    prefix = "sender"
    tip_set, tip_cap, tip_exact = _TIP_SET, TIP_CAP_LAMPORTS, None
    tip_min = 1
    _dedup_entries = False

    def __init__(self, http, *, timeout_s=None, url=SENDER_URL, clock=_time.monotonic, cap=256):
        from sniper.execution.solana_fast_submit import SolanaRpcProvider
        self._rpc = SolanaRpcProvider(http, url, provider_id=self.provider_id, skip_preflight=True,
                                      max_retries=0, timeout_s=timeout_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._outcomes = collections.OrderedDict()
        self._cap = int(cap)
        self._error_run = 0

    def submit(self, request):
        tip = payload_tip(request.signed_payload, self.tip_set, self.tip_cap, self.tip_exact, self.tip_min)
        if (not tip or self.prefix == 'nozomi'
                and NOZOMI_TIP_LAMPORTS < tip < NOZOMI_TREATMENT_TIP_LAMPORTS):
            return SubmissionAttempt(self.provider_id, AttemptState.NOT_SENT, None, 0.0, None,
                                     "untipped" if tip == 0 else "tip_unreadable")
        p = self.prefix
        previous = self._put(request.tx_identity, {p + "_state": "pending",
                                                  p + "_sha256": request.signed_payload_sha256},
                             once=self._dedup_entries and request.action == "entry")
        if previous is not None:
            reason = "already_dispatched" if previous[p + "_sha256"] == request.signed_payload_sha256 \
                else "identity_payload_mismatch"
            return SubmissionAttempt(self.provider_id, AttemptState.NOT_SENT, None, 0.0, None, reason)
        started = self._clock()
        http_timing = {}
        if p == "nozomi":
            def stamp(phase):
                http_timing[phase] = self._clock()
            attempt = self._rpc.submit(request, observe_http_fn=stamp)
        else:
            attempt = self._rpc.submit(request)
        finished = self._clock()
        accepted = attempt.state is AttemptState.ACCEPTED
        with self._lock:
            self._error_run = 0 if accepted else self._error_run + 1
            run = self._error_run
        self._put(request.tx_identity, {
            p + "_state": self._state(attempt.state), p + "_http_status": attempt.response_code,
            p + "_ms": round((finished - started) * 1000.0, 3),
            p + "_reason": attempt.reason_code, p + "_sha256": request.signed_payload_sha256,
            p + "_error_run": run, **self._extra(attempt, started, finished, http_timing)})
        if p == "nozomi":
            from dataclasses import replace
            attempt = replace(attempt, provider_timing={
                "post_started_monotonic": http_timing.get("started"),
                "post_finished_monotonic": http_timing.get("finished"),
                "clock": "perf_counter"})
        return attempt

    @staticmethod
    def _state(state):
        return state.value

    def _extra(self, attempt, started, finished, http_timing=None):
        return {}

    def outcome(self, tx_identity):
        with self._lock:
            row = self._outcomes.get(str(tx_identity))
            return dict(row) if row is not None else None

    def _put(self, tx_identity, row, *, once=False):
        with self._lock:
            if once and str(tx_identity) in self._outcomes:
                return self._outcomes[str(tx_identity)]
            self._outcomes[str(tx_identity)] = row
            self._outcomes.move_to_end(str(tx_identity))
            # shortcut: dedup ends on eviction; raise the cap if dispatch overlap exceeds it.
            while len(self._outcomes) > self._cap:
                self._outcomes.popitem(last=False)


class NozomiProvider(SenderProvider):
    """Nozomi (Temporal) JSON-RPC. Posts only a payload with exactly one 5,000,000-lamport transfer to
    a Nozomi tip account. Its state reads Gamma's words: an ambiguous answer is `submitted_unknown`.
    `nozomi_post_started` is the perf_counter at the POST, the trace's clock domain."""

    provider_id = NOZOMI_PROVIDER_ID
    prefix = "nozomi"
    tip_set, tip_cap, tip_exact = _NOZOMI_SET, NOZOMI_TIP_LAMPORTS, NOZOMI_TIP_LAMPORTS

    def __init__(self, http, *, url, timeout_s=None, cap=256, dynamic_tips=False, fee_split=False):
        if fee_split and not dynamic_tips:
            raise ValueError('invalid_nozomi_fee_split')
        self.tip_cap = NOZOMI_TREATMENT_TIP_LAMPORTS if fee_split else NOZOMI_TIP_LAMPORTS
        self._dedup_entries = dynamic_tips  # Nozomi retries server-side; client repeats consume quota.
        self.tip_exact = None if dynamic_tips else NOZOMI_TIP_LAMPORTS
        self.tip_min = NOZOMI_MIN_TIP_LAMPORTS if dynamic_tips else NOZOMI_TIP_LAMPORTS
        super().__init__(http, timeout_s=timeout_s, url=url, clock=_time.perf_counter, cap=cap)

    def __repr__(self):
        return "NozomiProvider()"            # the URL carries the API key

    @staticmethod
    def _state(state):
        return "submitted_unknown" if state is AttemptState.AMBIGUOUS else state.value

    def _extra(self, attempt, started, finished, http_timing=None):
        begin = (http_timing or {}).get("started")
        end = (http_timing or {}).get("finished")
        return {"nozomi_returned_sig": attempt.returned_identity, "nozomi_post_started": begin,
                "nozomi_post_finished": end,
                "nozomi_ms": round((end - begin) * 1000.0, 3)
                if begin is not None and end is not None else None}


def nozomi_provider(http, *, env=None, timeout_s=None):
    """The Nozomi provider when MEMECOIN_SLOT_LANE_NOZOMI_AB is on and MEMECOIN_NOZOMI_API_KEY is set,
    else None (a knob without a key logs why, never the key)."""
    env = os.environ if env is None else env
    fee_split = nozomi_fee_split_on(env)
    if not nozomi_knob_on(env):
        return None
    key = str(env.get("MEMECOIN_NOZOMI_API_KEY", "") or "").strip()
    if not key:
        import logging
        logging.getLogger("memecoin").warning(
            "solana submit: MEMECOIN_SLOT_LANE_NOZOMI_AB is on but MEMECOIN_NOZOMI_API_KEY is empty; "
            "no Nozomi provider")
        return None
    dynamic = str(env.get("MEMECOIN_SOLANA_ENTRY_BIDS_ENABLED", "")).strip().lower() in (
        "1", "true", "on", "yes")
    return NozomiProvider(http, url=NOZOMI_URL + key, timeout_s=timeout_s, dynamic_tips=dynamic,
                         fee_split=fee_split)


def cap_entry_priority_fee(message, *, tip_lamports=0, entry_bids=None, pool=None, crowded_lamports=None):
    """Apply a cached floor and fee cap before signing; preserve every swap account/index.

    `crowded_lamports` (a crowded-fee TREATED slot-lane send, memecoin_crowded_fee) lifts the ceiling
    to that priority, never past MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS with the tip. Without
    it the cap is MAX_TOTAL_LAMPORTS minus the tip, which clamped every treated send of 2026-10-15
    to 520k / 1.52M lamports after the build had priced 10M."""
    from solders.instruction import CompiledInstruction
    from solders.message import MessageV0
    from sniper.execution.solana_entry_bids import MAX_TOTAL_LAMPORTS
    if not isinstance(message, MessageV0) or not valid_nozomi_tip_budget(tip_lamports):
        raise ValueError("invalid_entry_bid")
    keys = [str(k) for k in message.account_keys]
    limit, price, at = 1_400_000, 0, None
    limits = 0
    instructions = list(message.instructions)
    for i, ix in enumerate(instructions):
        if not 0 <= ix.program_id_index < len(keys):
            # a program id outside the static keys (e.g. from a lookup table) is unreadable: refuse, never crash
            raise ValueError("invalid_entry_compute_budget")
        if keys[ix.program_id_index] != "ComputeBudget111111111111111111111111111111":
            continue
        data = bytes(ix.data)
        if data[:1] == b"\x02":
            if len(data) != 5 or limits:
                raise ValueError("invalid_entry_compute_budget")
            limit = struct.unpack("<I", data[1:])[0]
            limits += 1
        elif data[:1] == b"\x03":
            if len(data) != 9 or at is not None:
                raise ValueError("invalid_entry_compute_budget")
            price, at = struct.unpack("<Q", data[1:])[0], i
    if not 0 < limit <= 1_400_000:
        raise ValueError("invalid_entry_compute_budget")
    # Tips actually IN the bytes (Codex P1 on ffbaa9e43): a known Sender transfer above the Sender cap, or a Nozomi
    # transfer above the tip this clamp accounts for, refuses the ENTRY unsigned; the recorded ceiling counts them.
    if loaded_transfer_destination(message):
        raise ValueError("sender_ab_tip_guard: entry_loaded_transfer_destination")
    sender_tip = sum(lamports for _a, lamports in tip_transfers(message))
    nozomi_tip = sum(lamports for _a, lamports in tip_transfers(message, _NOZOMI_SET))
    if sender_tip > TIP_CAP_LAMPORTS:
        raise ValueError("sender_ab_tip_guard: entry_sender_tip_over_cap")
    if nozomi_tip > tip_lamports:
        raise ValueError("sender_ab_tip_guard: entry_nozomi_tip_unaccounted")
    ceiling = MAX_TOTAL_LAMPORTS - tip_lamports
    if type(crowded_lamports) is int and crowded_lamports > 0:
        from sniper.settings import memecoin as _cfg
        send_cap = int(getattr(_cfg, "MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS", 0) or 0)
        ceiling = max(ceiling, min(crowded_lamports, send_cap - tip_lamports))
    original = price
    bid = {}
    if entry_bids is not None and limits:
        bid = entry_bids.select(pool=pool, cu_limit=limit, tip_lamports=tip_lamports)
        selected = bid['micro_lamports_per_cu']
        if type(selected) is not int or selected < 0:
            raise ValueError('invalid_entry_bid')
        price = max(price, selected)
    saturated = price > ceiling * 1_000_000 // limit
    price = min(price, ceiling * 1_000_000 // limit)
    if at is None and price:
        try:
            program = keys.index('ComputeBudget111111111111111111111111111111')
        except ValueError:
            raise ValueError('invalid_entry_compute_budget') from None
        instructions.append(CompiledInstruction(program, b'\x03' + struct.pack('<Q', price), b''))
    elif price != original:
        ix = instructions[at]
        instructions[at] = CompiledInstruction(ix.program_id_index,
                                               b"\x03" + struct.pack("<Q", price), ix.accounts)
    if instructions != list(message.instructions):
        message = MessageV0(message.header, message.account_keys, message.recent_blockhash,
                            instructions, message.address_table_lookups)
    priority = (price * limit + 999_999) // 1_000_000
    info = {**bid, "cu_limit": limit if limits else None, "micro_lamports_per_cu": price,
            "priority_fee_lamports": priority if limits else None, "tip_lamports": tip_lamports,
            "combined_fee_lamports": priority + tip_lamports if limits else None,
            "cap_saturated": saturated or bool(bid.get('cap_saturated')),
            # the enforced per-leg ceiling the desk cost gate reads (priority cap + accounted Nozomi tip + Sender tip)
            "fee_ceiling_lamports": ceiling + tip_lamports + sender_tip, "sender_tip_lamports": sender_tip}
    if not limits:
        info.update(priority_source='unknown_cu_limit', cu_limit_upper_bound=limit,
                    priority_fee_upper_bound_lamports=priority,
                    combined_fee_upper_bound_lamports=priority + tip_lamports)
    return message, info


def signed_fee_footprint_lamports(payload_b64):
    """Priority fee + known tip transfers (Nozomi, Sender) that THESE signed bytes can charge, from the bytes alone
    (Gamma/Codex 2026-10-22: provenance verified at the write-ahead bind, never carried). None (unproven) when the
    bytes cannot be read, a program index is outside the static keys, the compute budget is missing a limit or
    repeats a limit/price. Pure: no I/O. It counts priority + KNOWN Sender/Nozomi tips only: a system transfer to
    any other address (a Jito or unknown tip account) cannot be told from a legitimate swap transfer and is not
    counted, so the bound covers the fee machinery we know, not arbitrary provider-injected transfers."""
    try:
        from solders.transaction import VersionedTransaction
        message = VersionedTransaction.from_bytes(base64.b64decode(payload_b64)).message
        keys = [str(k) for k in message.account_keys]
        limit = price = None
        for ix in message.instructions:
            if not 0 <= ix.program_id_index < len(keys):
                return None
            if keys[ix.program_id_index] != "ComputeBudget111111111111111111111111111111":
                continue
            data = bytes(ix.data)
            if data[:1] == b"\x02":
                if len(data) != 5 or limit is not None:
                    return None
                limit = struct.unpack("<I", data[1:])[0]
            elif data[:1] == b"\x03":
                if len(data) != 9 or price is not None:
                    return None
                price = struct.unpack("<Q", data[1:])[0]
        if limit is None or not 0 < limit <= 1_400_000:
            return None
        if loaded_transfer_destination(message):
            return None                          # a transfer to a lookup-table account is unproven, never a number
        priority = ((price or 0) * limit + 999_999) // 1_000_000
        tips = sum(lamports for _a, lamports in tip_transfers(message))
        tips += sum(lamports for _a, lamports in tip_transfers(message, _NOZOMI_SET))
        return int(priority + tips)
    except Exception:      # noqa: BLE001 - any unreadable footprint is unproven, never a number
        return None


def clamp_exit_priority(swap, *, cap):
    """Clamp an UNSIGNED exit build's compute-unit price to `cap` uL/CU (operator yes 2026-10-23). Returns
    (swap, info) where `swap` keeps the input's shape (base64 str or Jupiter metadata dict).

    An exit is never refused, delayed or dropped here: bytes this cannot read (bad base64, already signed, a
    program index outside the static keys, a repeated limit/price) come back UNCHANGED with
    fee_ceiling_lamports None (the leg is unbounded). Otherwise fee_ceiling_lamports = cap x CU limit (1.4M
    when the build sets none) + the known Sender/Nozomi tips in the bytes; a transfer to a lookup-table
    account leaves the price clamped but the leg unbounded. Pure: no I/O."""
    from solders.instruction import CompiledInstruction
    from solders.message import MessageV0
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction
    unbounded = {"bounded": False, "clamped": False, "fee_ceiling_lamports": None, "cap_micro_lamports": cap}
    meta = swap if isinstance(swap, dict) else None
    if type(cap) is not int or cap <= 0:     # cap 0 would price every exit at 0 (slowest landing): no clamp
        return swap, {**unbounded, "reason": "exit_cap_not_positive"}
    try:
        tx = VersionedTransaction.from_bytes(base64.b64decode(meta["swapTransaction"] if meta else swap))
        message = tx.message
        if not isinstance(message, MessageV0) or any(s != Signature.default() for s in tx.signatures):
            return swap, {**unbounded, "reason": "not_unsigned_v0"}
        keys = [str(k) for k in message.account_keys]
        instructions = list(message.instructions)
        limit = price = at = None
        for i, ix in enumerate(instructions):
            if not 0 <= ix.program_id_index < len(keys):
                return swap, {**unbounded, "reason": "program_index_outside_static_keys"}
            if keys[ix.program_id_index] != "ComputeBudget111111111111111111111111111111":
                continue
            data = bytes(ix.data)
            if data[:1] == b"\x02":
                if len(data) != 5 or limit is not None:
                    return swap, {**unbounded, "reason": "compute_limit_unreadable"}
                limit = struct.unpack("<I", data[1:])[0]
            elif data[:1] == b"\x03":
                if len(data) != 9 or price is not None:
                    return swap, {**unbounded, "reason": "compute_price_unreadable"}
                price, at = struct.unpack("<Q", data[1:])[0], i
        bound_limit = limit if limit is not None and 0 < limit <= 1_400_000 else 1_400_000
        clamped = price is not None and price > cap
        if clamped:
            ix = instructions[at]
            instructions[at] = CompiledInstruction(ix.program_id_index, b"\x03" + struct.pack("<Q", cap),
                                                   ix.accounts)
            message = MessageV0(message.header, message.account_keys, message.recent_blockhash,
                                instructions, message.address_table_lookups)
            out = base64.b64encode(bytes(VersionedTransaction.populate(message, list(tx.signatures)))).decode()
            swap = {**meta, "swapTransaction": out} if meta else out
        tips = (sum(l for _a, l in tip_transfers(message))
                + sum(l for _a, l in tip_transfers(message, _NOZOMI_SET)))
        ceiling = None if loaded_transfer_destination(message) else (cap * bound_limit + 999_999) // 1_000_000 + tips
        return swap, {"bounded": ceiling is not None, "clamped": clamped, "fee_ceiling_lamports": ceiling,
                      "cap_micro_lamports": cap, "cu_limit": limit, "requested_micro_lamports": price,
                      "tip_lamports": tips}
    except Exception as exc:     # noqa: BLE001 - an unreadable exit is signed unchanged, never refused
        return swap, {**unbounded, "reason": type(exc).__name__}


def cap_unsigned_entry_priority(unsigned_b64, *, tip_lamports=0, entry_bids=None, pool=None):
    from solders.transaction import VersionedTransaction
    from solders.signature import Signature
    tx = VersionedTransaction.from_bytes(base64.b64decode(unsigned_b64))
    if any(sig != Signature.default() for sig in tx.signatures):
        raise ValueError('entry_bid_payload_already_signed')
    message, bid = cap_entry_priority_fee(tx.message, tip_lamports=tip_lamports,
                                          entry_bids=entry_bids, pool=pool)
    if bytes(message) == bytes(tx.message):
        return unsigned_b64, bid
    capped = VersionedTransaction.populate(message, list(tx.signatures))
    # The entry executor owns packet-size refusal and its one account-capped re-quote.
    return base64.b64encode(bytes(capped)).decode(), bid
