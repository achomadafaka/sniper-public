"""Nozomi arm B on every funded Solana entry lane (operator order 2026-10-09 via Alpha).

The slot lane builds its own message and already tips Nozomi on arm B. Every other funded Solana
entry (the on-chain lane, its fallback after the slot lane, manual entries, dip and entry retries)
signs a transaction Jupiter (or the pump-native / Raydium fallback) built. Behind
MEMECOIN_NOZOMI_ALL_LANES, a listed wallet's arm-B attempt gets ONE 5,000,000-lamport transfer to a
Nozomi tip account inserted into that prebuilt v0 message before signing, half/half on the same
rule. The insertion is the risky part, so it has the most tests: the v0 header, the index shift
across static and ALT-loaded keys, a system program that is only in a lookup table, and the
1232-byte packet. With the knob off, the signed bytes are today's.
"""
import base64
import contextlib
import json
import sqlite3
import time
import timeit

import pytest
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message, MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from sniper.execution import solana_sender_ab as S
from sniper.execution.submission_transport import AttemptState, SubmissionAttempt, SubmissionTransport
from sniper.persistence import db_memecoin as m
from test_memecoin_slot_lane_sender_ab import _SenderHttp, _arm_by_hand, _ids

SYSTEM = Pubkey.from_string("11111111111111111111111111111111")
CB = Pubkey.from_string("ComputeBudget111111111111111111111111111111")
KEY = "nozomi-all-lanes-key-4b1d"
TIP = 5_000_000


def _ix(program, metas, data=b"\x01\x02"):
    return Instruction(program, data, metas)


def _jupiter_like(payer, *, system_static=True, pad=0):
    """A v0 swap shaped like Jupiter's: compute budget, a router ix with static and ALT-loaded
    writable and readonly accounts, and (when `system_static`) a SystemProgram call."""
    router, pool, out_ata, mint = (Pubkey.new_unique() for _ in range(4))
    alt_writable, alt_readonly = Pubkey.new_unique(), Pubkey.new_unique()
    alt_addrs = [alt_writable, alt_readonly, Pubkey.new_unique()]
    ixs = [_ix(CB, [], b"\x02" + (200_000).to_bytes(4, "little"))]
    if system_static:
        ixs.append(_ix(SYSTEM, [AccountMeta(payer, True, True), AccountMeta(out_ata, False, True)],
                       (2).to_bytes(4, "little") + (1).to_bytes(8, "little")))
    else:
        alt_addrs.append(SYSTEM)                   # passed as an account, loaded from the table
    metas = [AccountMeta(payer, True, True), AccountMeta(pool, False, True),
             AccountMeta(out_ata, False, True), AccountMeta(alt_writable, False, True),
             AccountMeta(alt_readonly, False, False), AccountMeta(mint, False, False)]
    if not system_static:
        metas.append(AccountMeta(SYSTEM, False, False))
    ixs.append(_ix(router, metas, b"\x09" * (8 + pad)))
    alt = AddressLookupTableAccount(key=Pubkey.new_unique(), addresses=alt_addrs)
    msg = MessageV0.try_compile(payer, ixs, [alt], Hash.new_unique())
    return msg


def _unsigned_b64(msg):
    from solders.signature import Signature
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, [Signature.default()]))).decode()


def _resolve(msg):
    """Index -> identity: a static pubkey, or (table, position) for an ALT-loaded account."""
    keys = [str(k) for k in msg.account_keys]
    loaded = []
    for lk in msg.address_table_lookups:
        loaded += [("w", str(lk.account_key), int(i)) for i in bytes(lk.writable_indexes)]
    for lk in msg.address_table_lookups:
        loaded += [("r", str(lk.account_key), int(i)) for i in bytes(lk.readonly_indexes)]
    return keys + loaded


def _ix_identities(msg):
    ident = _resolve(msg)
    return [(ident[ix.program_id_index], [ident[a] for a in bytes(ix.accounts)], bytes(ix.data))
            for ix in msg.instructions]


def _insert(msg, payer, k=0):
    got = S.with_nozomi_tip(_unsigned_b64(msg), payer, S.NOZOMI_TIP_ACCOUNTS[k], TIP)
    return None if got is None else VersionedTransaction.from_bytes(base64.b64decode(got)).message


# --- the insertion ----------------------------------------------------------------------------------

def test_the_tip_key_lands_in_the_writable_non_signer_region_and_the_header_is_unchanged():
    payer = Keypair().pubkey()
    msg = _jupiter_like(payer)
    out = _insert(msg, payer)
    h0, h1 = msg.header, out.header
    assert (h1.num_required_signatures, h1.num_readonly_signed_accounts,
            h1.num_readonly_unsigned_accounts) == (h0.num_required_signatures,
                                                  h0.num_readonly_signed_accounts,
                                                  h0.num_readonly_unsigned_accounts)
    keys = [str(k) for k in out.account_keys]
    p = keys.index(S.NOZOMI_TIP_ACCOUNTS[0])
    assert h1.num_required_signatures <= p < len(keys) - h1.num_readonly_unsigned_accounts
    assert out.is_maybe_writable(p) and not out.is_signer(p)
    assert len(keys) == len(msg.account_keys) + 1
    assert [str(lk.account_key) for lk in out.address_table_lookups] == \
        [str(lk.account_key) for lk in msg.address_table_lookups]


def test_every_original_instruction_names_the_same_accounts_across_static_and_alt_keys():
    payer = Keypair().pubkey()
    msg = _jupiter_like(payer)
    assert any(i >= len(msg.account_keys) for ix in msg.instructions for i in bytes(ix.accounts)), \
        "the fixture must reference an ALT-loaded account"
    out = _insert(msg, payer)
    assert _ix_identities(out)[:-1] == _ix_identities(msg)
    program, accounts, data = _ix_identities(out)[-1]
    assert program == str(SYSTEM) and accounts == [str(payer), S.NOZOMI_TIP_ACCOUNTS[0]]
    assert data == (2).to_bytes(4, "little") + TIP.to_bytes(8, "little")
    assert out.recent_blockhash == msg.recent_blockhash
    assert S.tip_transfers(out, S._NOZOMI_SET) == [(S.NOZOMI_TIP_ACCOUNTS[0], TIP)]
    assert S.guard_refusal(out, 0, TIP) is None


def test_a_system_program_only_in_a_lookup_table_refuses():
    payer = Keypair().pubkey()
    msg = _jupiter_like(payer, system_static=False)
    assert str(SYSTEM) not in [str(k) for k in msg.account_keys]
    assert _insert(msg, payer) is None


def test_a_tip_that_would_pass_1232_bytes_refuses():
    payer = Keypair().pubkey()
    small = _jupiter_like(payer)
    room = 1232 - len(base64.b64decode(_unsigned_b64(small)))
    big = _jupiter_like(payer, pad=room - 20)              # fits untipped, not with ~49 more bytes
    assert len(base64.b64decode(_unsigned_b64(big))) <= 1232
    assert _insert(big, payer) is None
    assert _insert(small, payer) is not None


def test_a_legacy_message_a_tip_key_already_present_and_a_foreign_payer_refuse():
    payer = Keypair().pubkey()
    legacy = Message.new_with_blockhash([_ix(SYSTEM, [AccountMeta(payer, True, True)])], payer,
                                        Hash.new_unique())
    from solders.signature import Signature
    b64 = base64.b64encode(bytes(VersionedTransaction.populate(legacy, [Signature.default()]))).decode()
    assert S.with_nozomi_tip(b64, payer, S.NOZOMI_TIP_ACCOUNTS[0], TIP) is None
    tip_key = Pubkey.from_string(S.NOZOMI_TIP_ACCOUNTS[1])
    has = MessageV0.try_compile(payer, [_ix(SYSTEM, [AccountMeta(payer, True, True),
                                                     AccountMeta(tip_key, False, True)])], [],
                                Hash.new_unique())
    assert _insert(has, payer, k=1) is None
    assert S.with_nozomi_tip(_unsigned_b64(_jupiter_like(payer)), Keypair().pubkey(),
                             S.NOZOMI_TIP_ACCOUNTS[0], TIP) is None


def test_insertion_cost_is_measured():
    payer = Keypair().pubkey()
    b64 = _unsigned_b64(_jupiter_like(payer))
    runs = sorted(timeit.repeat(lambda: S.with_nozomi_tip(b64, payer, S.NOZOMI_TIP_ACCOUNTS[0], TIP),
                                number=1, repeat=300))
    p50, p90 = runs[150] * 1e6, runs[270] * 1e6
    print("with_nozomi_tip p50 %.1f us p90 %.1f us" % (p50, p90))
    assert p90 < 2000                                       # a bound, not a benchmark


# --- the arm --------------------------------------------------------------------------------------

def test_the_arm_needs_the_knob_and_a_listed_wallet(monkeypatch):
    from sniper.settings import memecoin as cfg
    ids = _ids()
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_NOZOMI_AB_WALLETS", frozenset({"w1"}))
    monkeypatch.delenv("MEMECOIN_NOZOMI_ALL_LANES", raising=False)
    assert S.nozomi_entry_arm(ids["B"], "w1") is None
    monkeypatch.setenv("MEMECOIN_NOZOMI_ALL_LANES", "1")
    assert S.nozomi_entry_arm(ids["B"], "w2") is None
    assert S.nozomi_entry_arm(None, "w1") is None
    assert S.nozomi_entry_arm(ids["B"], "w1") == S.assign_nozomi(ids["B"])
    assert S.nozomi_entry_arm(ids["A"], "w1")["arm"] == "A"
    assert S.nozomi_gas_extra_sol(ids["B"], "w1") == pytest.approx(0.005)
    assert S.nozomi_gas_extra_sol(ids["A"], "w1") == 0.0
    assert S.nozomi_gas_extra_sol(ids["B"], "w2") == 0.0


# --- the SOL gas gate -------------------------------------------------------------------------------

def test_the_gas_gate_holds_the_tip_back_for_an_arm_b_attempt_only(monkeypatch):
    from sniper.services import memecoin_risk as risk
    from sniper.settings import memecoin as cfg
    ids = _ids()
    for name, value in (("MEMECOIN_REAL_ENABLED", True), ("MEMECOIN_MIN_TRADE_FLOOR_USD", 0.0),
                        ("MEMECOIN_DAILY_LOSS_LIMIT_USD", 0.0), ("MEMECOIN_GAS_ENTRY_SOL", 0.002),
                        ("MEMECOIN_GAS_EXIT_SOL", 0.002), ("MEMECOIN_GAS_RETRY_FACTOR", 2.0),
                        ("MEMECOIN_SLOT_LANE_NOZOMI_AB_WALLETS", frozenset({"w1"}))):
        monkeypatch.setattr(cfg, name, value)
    monkeypatch.setenv("MEMECOIN_NOZOMI_ALL_LANES", "1")
    need = risk.required_entry_sol(0, 0.002, 0.002, 2.0)
    sol = need + 0.004                                     # covers the fee, not fee + 0.005 tip

    def reserve(attempt_id, mint):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        m.ensure_memecoin_tables(c)
        return risk.reserve_entry(c, mint=mint, source_user_id="w1", strategy=cfg.MEMECOIN_REAL_POLICY,
                                  day="2026-10-09", usdc_balance=300.0, sol_balance=sol,
                                  now_iso="2026-10-09T14:00:00+00:00", attempt_id=attempt_id)
    refused, pid = reserve(ids["B"], "MintB")
    assert pid is None and "insufficient SOL" in refused.reason, refused.reason
    allowed, pid = reserve(ids["A"], "MintA")
    assert pid is not None, allowed.reason
    monkeypatch.delenv("MEMECOIN_NOZOMI_ALL_LANES")
    allowed, pid = reserve(ids["B"], "MintB2")
    assert pid is not None, allowed.reason


# --- the production entry path (execute_fn) ----------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    with contextlib.closing(c):
        yield c


class _Trace:
    def __init__(self, attempt_id):
        self.attempt_id, self.stages, self._t0 = attempt_id, {}, time.perf_counter()

    def mark_substage(self, name, **kw):
        self.stages[name] = kw

    def mark(self, *_a, **_k):
        pass

    def elapsed_ms_at(self, perf):
        return (perf - self._t0) * 1000.0


def _executor(conn, monkeypatch, *, knob, wallets=("w1",), funding_asset=None, pad=0, system_static=True):
    from sniper.execution import solana_real_seams as seams, solana_swap_executor as executor
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_NOZOMI_AB_WALLETS", frozenset(wallets))
    if knob:
        monkeypatch.setenv("MEMECOIN_NOZOMI_ALL_LANES", "1")
    else:
        monkeypatch.delenv("MEMECOIN_NOZOMI_ALL_LANES", raising=False)
    kp = Keypair()
    msg = _jupiter_like(kp.pubkey(), pad=pad, system_static=system_static)
    swap_b64 = _unsigned_b64(msg)
    monkeypatch.setattr(seams, "jupiter_swap_tx",
                        lambda quote, user, **kw: ({"swapTransaction": swap_b64, "lastValidBlockHeight": 9}
                                                   if kw.get("return_metadata") else swap_b64))
    tpu = []

    class Tpu:
        provider_id = "solana:tpu"

        def submit(self, request):
            tpu.append(request.signed_payload)
            return SubmissionAttempt("solana:tpu", AttemptState.ACCEPTED, request.tx_identity,
                                     1.0, "5", None)
    sender_http, nozomi_http = _SenderHttp(), _SenderHttp()
    transport = SubmissionTransport(
        [Tpu(), S.SenderProvider(sender_http, timeout_s=1.0),
         S.NozomiProvider(nozomi_http, url=S.NOZOMI_URL + KEY, timeout_s=1.0)], deadline_s=2.0)
    signed = []

    def execute(conn_, *, pending_id, sign_fn, send_fn, **kw):
        out = sign_fn({"outputMint": "Coin"})
        signed.append(out)
        if out:
            send_fn(out[0], validity=out[2] if len(out) > 2 else None)
            deadline = time.monotonic() + 2.0
            while not nozomi_http.calls and S.payload_tip(out[0], S._NOZOMI_SET, TIP, TIP) \
                    and time.monotonic() < deadline:
                time.sleep(0.005)
        return {"status": "failed", "reason": "fixture", "sig": out[1] if out else None}
    monkeypatch.setattr(executor, "execute_entry", execute)
    seam = seams.build_real_solana_executor(keypair=kp, rpc_url="http://unused",
                                            submission_transport=transport)
    pid = m.reserve_pending(conn, mint="Coin", source_user_id="w1", strategy="alpha_b", notional=50.0,
                            reserved_at="2026-10-09T14:00:00+00:00", network_id="solana",
                            origin_event_id="ev:1", signal_source="onchain_watch", source_price=0.001,
                            **({"funding_asset": funding_asset, "funding_amount_atomic": 10 ** 8,
                                "funding_sol_usd": 150.0} if funding_asset else {}))
    return seam, pid, swap_b64, kp, tpu, sender_http, nozomi_http, signed


def _run(conn, monkeypatch, attempt_id, **kw):
    seam, pid, swap_b64, kp, tpu, sender_http, nozomi_http, signed = _executor(conn, monkeypatch, **kw)
    trace = _Trace(attempt_id)
    seam["execute_fn"](conn, pid, "Coin", 50.0, "2026-10-09T14:00:01+00:00", trace=trace)
    return trace, swap_b64, kp, tpu, sender_http, nozomi_http, signed


def _today(swap_b64, kp):
    from sniper.execution.solana_real_seams import sign_swap_tx
    return sign_swap_tx({"swapTransaction": swap_b64, "lastValidBlockHeight": 9}, kp)


def test_with_the_knob_off_the_entry_signs_todays_bytes(conn, monkeypatch):
    ids = _ids()
    trace, swap_b64, kp, tpu, sender_http, nozomi_http, signed = _run(conn, monkeypatch, ids["B"],
                                                                      knob=False)
    assert signed[0][:2] == _today(swap_b64, kp)[:2]
    assert nozomi_http.calls == [] and "nozomi_ab" not in trace.stages


def test_arm_b_sends_nozomi_the_free_direct_bytes_with_one_tip(conn, monkeypatch):
    ids = _ids()
    trace, swap_b64, kp, tpu, sender_http, nozomi_http, signed = _run(conn, monkeypatch, ids["B"],
                                                                      knob=True)
    (sent,) = tpu
    (_m, url, body), = nozomi_http.calls
    assert url == S.NOZOMI_URL + KEY and body["params"][0] == sent
    assert sender_http.calls == []
    tx = VersionedTransaction.from_bytes(base64.b64decode(sent))
    arm = S.assign_nozomi(ids["B"])
    assert S.tip_transfers(tx.message, S._NOZOMI_SET) == [(arm["tip_account"], TIP)]
    assert S.tip_transfers(tx.message) == []
    ab = trace.stages["nozomi_ab"]
    assert (ab["arm"], ab["rule"], ab["tip_lamports"], ab["tip_account"]) == (
        "B", "nozomi-sha256-attempt-odd-v1", TIP, arm["tip_account"])
    send = trace.stages["nozomi_ab_send"]
    assert send["nozomi_state"] == "accepted" and send["nozomi_payload_sha_match"] is True
    assert send["nozomi_sig_match"] is True and send["tip_lamports"] == TIP
    assert send["nozomi_post_start_ms"] >= 0


def test_arm_a_signs_todays_bytes_and_never_reaches_nozomi(conn, monkeypatch):
    ids = _ids()
    trace, swap_b64, kp, tpu, sender_http, nozomi_http, signed = _run(conn, monkeypatch, ids["A"],
                                                                      knob=True)
    assert signed[0][:2] == _today(swap_b64, kp)[:2]
    assert nozomi_http.calls == [] and trace.stages["nozomi_ab"]["arm"] == "A"
    assert "nozomi_ab_send" not in trace.stages


@pytest.mark.parametrize("kw", [dict(wallets=("w2",)), dict(funding_asset="sol")])
def test_an_unlisted_wallet_or_a_native_funded_entry_gets_no_arm(conn, monkeypatch, kw):
    ids = _ids()
    trace, swap_b64, kp, tpu, sender_http, nozomi_http, signed = _run(conn, monkeypatch, ids["B"],
                                                                      knob=True, **kw)
    assert signed[0][:2] == _today(swap_b64, kp)[:2]
    assert nozomi_http.calls == [] and "nozomi_ab" not in trace.stages


@pytest.mark.parametrize("kw", [dict(system_static=False), dict(pad=1232)])
def test_an_insertion_refusal_sends_todays_bytes_and_says_so(conn, monkeypatch, kw):
    ids = _ids()
    if "pad" in kw:
        kw["pad"] = 1232 - len(base64.b64decode(_unsigned_b64(_jupiter_like(Keypair().pubkey())))) - 20
    trace, swap_b64, kp, tpu, sender_http, nozomi_http, signed = _run(conn, monkeypatch, ids["B"],
                                                                      knob=True, **kw)
    assert signed[0][:2] == _today(swap_b64, kp)[:2]
    assert nozomi_http.calls == []
    assert trace.stages["nozomi_ab"]["arm"] == "B"
    assert trace.stages["nozomi_ab_fallback"]["sent"] == "untipped"


def test_a_tip_the_guard_refuses_is_never_signed(conn, monkeypatch):
    """The insertion is checked by the slot lane's pre-sign guard: a wrong amount falls back."""
    ids = _ids()
    real = S.with_nozomi_tip
    monkeypatch.setattr(S, "with_nozomi_tip",
                        lambda b64, payer, account, lamports: real(b64, payer, account, lamports - 1))
    trace, swap_b64, kp, tpu, sender_http, nozomi_http, signed = _run(conn, monkeypatch, ids["B"],
                                                                      knob=True)
    assert signed[0][:2] == _today(swap_b64, kp)[:2]
    assert nozomi_http.calls == []
    assert "sender_ab_tip_guard" in trace.stages["nozomi_ab_fallback"]["reason"]
