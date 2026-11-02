"""Helius Sender SWQoS-only A/B on the Solana slot lane (spec 2026-10-01; Gamma's pre-registration
docs/audits/2026-10-01-sender-swqos-ab-preregistration.md, 99679222a).

Gamma stops the A/B on: a Sender payload that differs from the TPU path's; an arm-A transaction with
a tip; a tip above 10,000 lamports; a recorded arm that differs from the recomputed one. Each has a
test here, the first two and the last on the production send path (SlotLane.handle, the real curve
builder, the real slot_execute_fn, make_solana_send_fn and SubmissionTransport).
"""
import base64
import contextlib
import hashlib
import logging
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

from sniper.execution import solana_sender_ab as S
from sniper.execution.submission_transport import (
    AttemptState, SubmissionAttempt, SubmissionTransport, build_request,
)
from sniper.persistence import db_memecoin as m

FIX = Path(__file__).parent / "fixtures"
SOURCE = json.loads((FIX / "memecoin" / "slot_price_limit_a47a6c14.json").read_text())[0]
SPEND = 1_418_853_206
TICKET = 175_000_000
BLOCKHASH = "11111111111111111111111111111111"


def _arm_by_hand(attempt_id):
    return "B" if int(hashlib.sha256(attempt_id.encode()).hexdigest()[:8], 16) % 2 else "A"


def _ids():
    """The first attempt id of each arm, so a test can force both."""
    found = {}
    i = 0
    while len(found) < 2:
        aid = "%032x" % i
        found.setdefault(_arm_by_hand(aid), aid)
        i += 1
    return found


def _signed(kp, ixs):
    msg = MessageV0.try_compile(kp.pubkey(), ixs, [], Hash.default())
    return base64.b64encode(bytes(VersionedTransaction(msg, [kp]))).decode()


def _tip_ixs(kp, lamports, n=1):
    return [ix for k in range(n) for ix in S.tip_instructions(kp.pubkey(), (S.TIP_ACCOUNTS[k], lamports))]


def _request(signed_b64):
    from sniper.execution.submission_wiring import solana_tx_identity
    return build_request(chain_id="solana", action="entry", tx_identity=solana_tx_identity(signed_b64),
                         signed_payload=signed_b64)


class _SenderHttp:
    def __init__(self, answer="echo", status=200):
        self.calls, self.answer, self.status = [], answer, status

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        payload = json.loads(body)
        self.calls.append((method, url, payload))
        if self.answer == "echo":
            from sniper.execution.submission_wiring import solana_tx_identity
            return self.status, {"result": solana_tx_identity(payload["params"][0])}, None
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.status, self.answer, None


# --- the arm rule ---------------------------------------------------------------------------------

def test_the_arm_is_the_preregistered_rule_and_only_arm_b_names_a_tip():
    for aid in ["%032x" % i for i in range(64)] + ["deadbeef", "attempt-1"]:
        got = S.assign(aid)
        assert got["arm"] == _arm_by_hand(aid) and got["rule"] == "sha256-attempt-odd-v1"
        if got["arm"] == "B":
            assert got["tip_lamports"] == 5_000 and got["tip_account"] in S.TIP_ACCOUNTS
            assert got["tip_account"] == S.TIP_ACCOUNTS[
                hashlib.sha256(aid.encode()).digest()[4] % 10]
        else:
            assert got["tip_lamports"] == 0 and got["tip_account"] is None
    arms = {S.assign("%032x" % i)["arm"] for i in range(64)}
    assert arms == {"A", "B"}


# --- the Sender provider ---------------------------------------------------------------------------

def test_sender_posts_only_a_tipped_payload_and_posts_it_unchanged():
    kp = Keypair()
    http = _SenderHttp()
    provider = S.SenderProvider(http, timeout_s=1.0)
    untipped = _request(_signed(kp, []))
    assert provider.submit(untipped).state is AttemptState.NOT_SENT and http.calls == []
    tipped = _request(_signed(kp, _tip_ixs(kp, 5_000)))
    attempt = provider.submit(tipped)
    assert attempt.state is AttemptState.ACCEPTED and attempt.provider_id == "solana:sender"
    (method, url, body), = http.calls
    assert (method, url) == ("POST", "http://fra-sender.helius-rpc.com/fast?swqos_only=true")
    assert body["method"] == "sendTransaction" and body["params"][0] == tipped.signed_payload
    assert body["params"][1] == {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}
    seen = provider.outcome(tipped.tx_identity)
    assert seen["sender_state"] == "accepted" and seen["sender_http_status"] == "200"
    assert seen["sender_sha256"] == tipped.signed_payload_sha256 and seen["sender_error_run"] == 0


@pytest.mark.parametrize("lamports,n", [(10_001, 1), (5_000, 2)])
def test_sender_never_posts_a_tip_over_the_cap_or_two_tips(lamports, n):
    kp = Keypair()
    http = _SenderHttp()
    attempt = S.SenderProvider(http).submit(_request(_signed(kp, _tip_ixs(kp, lamports, n))))
    assert attempt.state is AttemptState.NOT_SENT and attempt.reason_code == "tip_unreadable"
    assert http.calls == []


def test_consecutive_sender_errors_are_counted_and_an_accept_resets_them():
    provider = S.SenderProvider(_SenderHttp(answer=OSError("reset")))

    def send():
        kp = Keypair()                                 # a fresh signer: a distinct identity
        req = _request(_signed(kp, _tip_ixs(kp, 5_000)))
        return provider.submit(req), provider.outcome(req.tx_identity)
    for expected in (1, 2):
        attempt, seen = send()
        assert attempt.state is AttemptState.AMBIGUOUS and seen["sender_error_run"] == expected
    provider._rpc._http = _SenderHttp()
    attempt, seen = send()
    assert attempt.state is AttemptState.ACCEPTED and seen["sender_error_run"] == 0


# --- the pre-sign tip guard -----------------------------------------------------------------------

def _msg(kp, ixs):
    return MessageV0.try_compile(kp.pubkey(), ixs, [], Hash.default())


def test_the_guard_refuses_any_tip_the_arm_did_not_ask_for():
    kp = Keypair()
    assert S.guard_refusal(_msg(kp, []), 0) is None
    assert S.guard_refusal(_msg(kp, _tip_ixs(kp, 5_000)), 5_000) is None
    assert "asked 0" in S.guard_refusal(_msg(kp, _tip_ixs(kp, 5_000)), 0)        # a tip on arm A
    assert "over the" in S.guard_refusal(_msg(kp, _tip_ixs(kp, 10_001)), 10_001)
    assert "2 tip transfers" in S.guard_refusal(_msg(kp, _tip_ixs(kp, 2_500, n=2)), 5_000)
    assert "asked 5000" in S.guard_refusal(_msg(kp, []), 5_000)                 # arm B lost its tip
    # A transfer to any other account (the PumpSwap native wrap) is not a tip.
    other = S.tip_instructions(kp.pubkey(), (str(Keypair().pubkey()), 7_000))
    assert S.guard_refusal(_msg(kp, other), 0) is None


# --- startup and the backend ----------------------------------------------------------------------

def test_the_paid_modes_still_fail_startup_and_the_knob_needs_the_shared_transport():
    from sniper.settings import memecoin_submission as C
    for mode in ("sender", "jito", "fast_fanout"):
        with pytest.raises(C.SubmissionConfigError):
            C.validate_submission_config({"MEMECOIN_SOLANA_SUBMIT_MODE": mode,
                                          "MEMECOIN_SLOT_LANE_SENDER_AB": "1"})
    with pytest.raises(C.SubmissionConfigError, match="shared transport"):
        C.validate_submission_config({"MEMECOIN_SOLANA_SUBMIT_MODE": "rpc",
                                      "MEMECOIN_SLOT_LANE_SENDER_AB": "1"})
    for mode in ("auto_free", "pooled_rpc"):
        C.validate_submission_config({"MEMECOIN_SOLANA_SUBMIT_MODE": mode,
                                      "MEMECOIN_SLOT_LANE_SENDER_AB": "1"})


def test_sender_is_the_last_provider_and_only_with_the_knob():
    from test_memecoin_submission_backends import FakeClient, FakeSidecar
    from sniper.execution import submission_backends as B
    try:
        on, _ = B.build_solana_backend(keypair=Keypair(), custody_rpc_url="https://custody/rpc",
                                       env={"MEMECOIN_SLOT_LANE_SENDER_AB": "1"},
                                       client_factory=FakeClient, tpu_sidecar=FakeSidecar())
        assert [p.provider_id for p in on._providers] == [
            "solana:custody_rpc", "solana:public_rpc", "solana:tpu", "solana:sender"]
        assert on.provider("solana:sender") is on._providers[-1]
        off, _ = B.build_solana_backend(keypair=Keypair(), custody_rpc_url="https://custody/rpc",
                                        env={}, client_factory=FakeClient, tpu_sidecar=FakeSidecar())
        assert off.provider("solana:sender") is None
    finally:
        B.close_all()


def test_a_native_fill_keeps_the_tip_out_of_token_cost():
    from sniper.execution.solana_swap_executor import receipt_sol_entry_fill
    spend, fee, tip = 500_000_000, 100_000, 5_000
    tx = {"transaction": {"signatures": ["sig"], "message": {"accountKeys": [
        {"pubkey": "owner", "signer": True}, {"pubkey": "token", "signer": False}]}},
        "meta": {"err": None, "fee": fee, "preBalances": [1_000_000_000, 0],
                 "postBalances": [1_000_000_000 - spend - fee - tip - 2_039_280, 2_039_280],
                 "preTokenBalances": [], "postTokenBalances": [{
                     "accountIndex": 1, "mint": "coin", "owner": "owner",
                     "uiTokenAmount": {"amount": "1000000", "decimals": 6}}]}}
    fill = receipt_sol_entry_fill(tx, signature="sig", owner="owner", mint="coin",
                                  maximum_lamports=spend, tip_lamports=tip)
    assert fill["sol_spent_lamports"] == spend and fill["tip_lamports"] == tip
    # Without the tip the same receipt reads as spend over the reservation: unreadable.
    assert receipt_sol_entry_fill(tx, signature="sig", owner="owner", mint="coin",
                                  maximum_lamports=spend) is None


# --- the production slot-lane send path -----------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "ledger.db")
    # WAL from the start, as the funded ledger is: the async submission-audit writer's first
    # get_connection runs PRAGMA journal_mode=WAL, and converting a rollback-journal file while the
    # lane's connection holds a lock fails at once with "database is locked" (no busy wait), which
    # dropped the audit row under -n 4 load (4 of 25 runs, 2026-10-16).
    c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    with contextlib.closing(c):
        yield c


def _production_lane(conn, tmp_path, monkeypatch, *, knob=True, transport=None):
    # Source initiation is tested with real receipts in test_memecoin_solana_source_initiation.
    from sniper.services import memecoin_source_initiation as initiation
    monkeypatch.setattr(initiation, "source_initiation_refusal", lambda *_: None)
    from sniper.execution import exec_lock, pump_curve_buy as pcb, solana_real_seams as seams
    from sniper.services import memecoin_slot_lane as lane
    from sniper.settings import memecoin as mc
    monkeypatch.setattr(exec_lock, "_lock_path",
                        lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    for name, value in (("MEMECOIN_REAL_ENABLED", True), ("MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0),
                        ("MEMECOIN_REAL_MAX_MCAP_USD", 1_000_000.0),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", 0.0),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND", {}),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 0.0),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 0.0), ("MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)):
        monkeypatch.setattr(mc, name, value)
    monkeypatch.setattr(mc, "memecoin_slot_lane_resend_offsets_ms", lambda: ())
    monkeypatch.setattr(pcb, "leg_clip_refusal", lambda leg, clip: None)   # the leg is a fixture
    kp = Keypair()
    event = pcb.parse_trade_event(SOURCE["trade_event_log"])
    builder = pcb.SlotLaneCurveBuilder(user=str(kp.pubkey()), jup_call=lambda *a, **k: None,
                                       rpc=lambda *a: None, clock=lambda: 1000.0)
    builder._buyback = event["fee_recipient"]
    builder._leg = {"ixs": [], "tables": [], "usdc_atomic": TICKET, "min_out_lamports": SPEND,
                    "out_lamports": SPEND, "fetched_at": 1000.0}
    tpu = []

    class Tpu:
        provider_id = "solana:tpu"

        def submit(self, request):
            tpu.append(request.signed_payload)
            return SubmissionAttempt("solana:tpu", AttemptState.ACCEPTED, request.tx_identity,
                                     1.0, "5", None)
    sender_http = _SenderHttp()

    def confirm(*_a, **_k):
        # Stop after the send. The chain read takes >= 250 ms live, so Sender (on its own worker,
        # possibly after the TPU accept returned the transport) has answered by then; wait for it.
        deadline = time.monotonic() + 2.0
        while knob and tpu and len(sender_http.calls) < len(tpu) and time.monotonic() < deadline \
                and S.payload_tip(tpu[-1]):
            time.sleep(0.005)
        time.sleep(0.02)
        return "failed"
    monkeypatch.setattr(seams, "rpc_confirm", confirm)
    if transport is None:
        providers = [Tpu()] + ([S.SenderProvider(sender_http, timeout_s=1.0)] if knob else [])
        transport = SubmissionTransport(providers, deadline_s=2.0)
    seam = seams.build_real_solana_executor(keypair=kp, rpc_url="http://unused",
                                            submission_transport=transport)
    assert seam["sender_ab_ready"] is knob
    snap = lane.PrefetchSnapshot(sol_usd=150.0, usdc_balance=400.0, sol_balance=0.5,
                                 blockhash=BLOCKHASH, last_valid_height=1,
                                 observed_mono={k: time.monotonic() for k in
                                                ("sol_usd", "balances", "blockhash")})
    ln = lane.SlotLane(conn=conn, executor={"slot_execute_fn": seam["slot_execute_fn"],
                                            "sender_ab_ready": seam["sender_ab_ready"]},
                       build_fn=lane.dispatch_build({"pump_curve": builder}),
                       snapshot_fn=lambda: snap, armed_ids_fn=lambda: {"w1"},
                       kill_tripped_fn=lambda: False,
                       now_iso_fn=lambda: "2026-10-01T20:30:00+00:00", lock_key="sol-test",
                       mode="live", head_slot_fn=lambda: (450_000_000, time.monotonic()))

    def buy(signature):
        return SimpleNamespace(
            source_user_id="w1", owner="Owner1", mint=event["mint"], signature=signature,
            slot=450_000_000, sol_amount=event["sol_amount"],
            virtual_sol_reserves=event["virtual_sol_reserves"],
            virtual_token_reserves=event["virtual_token_reserves"],
            recv_utc="2026-10-01T20:30:00+00:00", recv_monotonic=time.monotonic(),
            token_program=pcb.TOKEN_PROGRAM, trade_event_log=SOURCE["trade_event_log"])
    return ln, buy, tpu, sender_http


def _substages(conn, attempt_id):
    row = conn.execute("SELECT trace_json FROM memecoin_entry_attempts WHERE attempt_id=?",
                       (attempt_id,)).fetchone()
    return {e["name"]: e for e in json.loads(row[0])["substage_events"]}


def _force_ids(monkeypatch, ids):
    from sniper.services import memecoin_entry_trace as T
    queue = list(ids)
    monkeypatch.setattr(T, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=queue.pop(0))))


def test_forced_attempt_ids_leave_background_uuid_generation_independent(monkeypatch):
    import uuid
    from sniper.services import memecoin_entry_trace as T
    ids = [_ids()["A"], _ids()["B"]]
    _force_ids(monkeypatch, ids)
    assert isinstance(uuid.uuid4(), uuid.UUID)
    assert [T.uuid.uuid4().hex, T.uuid.uuid4().hex] == ids


def test_production_path_arm_b_sends_sender_the_tpu_bytes_and_arm_a_never_reaches_sender(
        conn, tmp_path, monkeypatch):
    ids = _ids()
    _force_ids(monkeypatch, [ids["B"], ids["A"]])
    ln, buy, tpu, sender_http = _production_lane(conn, tmp_path, monkeypatch)

    b = ln.handle(buy("SigB"))
    assert b["status"] == "failed", b                  # sent, then the stubbed chain said failed
    (sent_b,) = tpu
    (_m, _u, body), = sender_http.calls
    assert body["params"][0] == sent_b, "Sender must get the exact bytes the TPU path got"
    tx_b = VersionedTransaction.from_bytes(base64.b64decode(sent_b))
    assert S.tip_transfers(tx_b.message) == [(S.assign(ids["B"])["tip_account"], 5_000)]
    stages = _substages(conn, ids["B"])
    assert stages["sender_ab"]["arm"] == "B" == _arm_by_hand(ids["B"])
    assert stages["sender_ab"]["rule"] == "sha256-attempt-odd-v1"
    send = stages["sender_ab_send"]
    assert send["tip_lamports"] == 5_000 and send["sender_http_status"] == "200"
    assert send["sender_state"] == "accepted" and send["sender_payload_sha_match"] is True
    assert send["sender_ms"] >= 0

    a = ln.handle(buy("SigA"))
    assert a["status"] == "failed", a
    sent_a = tpu[1]
    assert len(sender_http.calls) == 1, "an arm-A send never reaches Sender"
    assert S.tip_transfers(VersionedTransaction.from_bytes(base64.b64decode(sent_a)).message) == []
    stages = _substages(conn, ids["A"])
    assert stages["sender_ab"]["arm"] == "A" == _arm_by_hand(ids["A"])
    assert "sender_ab_send" not in stages
    # PR #486 (2026-10-20): no Lighthouse source-slot guards on automatic entries. The only signed
    # difference is arm B's one extra system transfer, the Sender tip.
    from sniper.execution.solana_slot_guard import LIGHTHOUSE_PROGRAM
    ixs_a = VersionedTransaction.from_bytes(base64.b64decode(sent_a)).message.instructions
    assert len(tx_b.message.instructions) == len(ixs_a) + 1
    data_a = [bytes(i.data) for i in ixs_a]
    data_b = [bytes(i.data) for i in tx_b.message.instructions]
    extra = [k for k in range(len(data_b)) if data_b[:k] + data_b[k + 1:] == data_a]
    assert len(extra) == 1 and data_b[extra[0]][:4] == b'\x02\x00\x00\x00', (data_a, data_b)
    for payload in (sent_a, sent_b):
        msg = VersionedTransaction.from_bytes(base64.b64decode(payload)).message
        assert all(str(msg.account_keys[ix.program_id_index]) != LIGHTHOUSE_PROGRAM for ix in msg.instructions)


def test_production_path_with_the_knob_off_is_todays_send(conn, tmp_path, monkeypatch):
    ids = _ids()
    _force_ids(monkeypatch, [ids["B"]])
    ln, buy, tpu, sender_http = _production_lane(conn, tmp_path, monkeypatch, knob=False)
    assert ln.handle(buy("SigOff"))["status"] == "failed"
    (sent,) = tpu
    assert S.tip_transfers(VersionedTransaction.from_bytes(base64.b64decode(sent)).message) == []
    stages = _substages(conn, ids["B"])
    assert "sender_ab" not in stages and "sender_ab_send" not in stages


def test_production_path_refuses_a_tip_the_arm_did_not_ask_for_before_signing(
        conn, tmp_path, monkeypatch):
    ids = _ids()
    _force_ids(monkeypatch, [ids["A"]])
    ln, buy, tpu, sender_http = _production_lane(conn, tmp_path, monkeypatch)
    real = ln.build_fn

    def tipped_anyway(buy_, clip, snap, **kw):
        return real(buy_, clip, snap, **kw, tip=(S.TIP_ACCOUNTS[0], 5_000))
    ln.build_fn = tipped_anyway                        # a builder bug tips an arm-A transaction
    result = ln.handle(buy("SigBug"))
    assert result["status"] == "aborted" and "sender_ab_tip_guard" in result["reason"]
    assert tpu == [] and sender_http.calls == [], "nothing is signed or sent"
    row = conn.execute("SELECT status FROM memecoin_pending_entries").fetchone()
    assert row["status"] != "reserved"


# --- the transport never drops an arm-B Sender send after an accept -------------------------------

class _HeldExecutor:
    """Runs every provider at once except the held one, whose future never starts."""

    def __init__(self, held_id):
        self.held_id, self.held = held_id, []

    def submit(self, fn, provider, request, lease):
        from concurrent.futures import Future
        fut = Future()
        if provider.provider_id == self.held_id:
            self.held.append(fut)
            return fut
        fut.set_result(fn(provider, request, lease))
        return fut


class _Fixed:
    def __init__(self, provider_id, state):
        self.provider_id, self.state = provider_id, state

    def submit(self, request):
        return SubmissionAttempt(self.provider_id, self.state,
                                 request.tx_identity if self.state is AttemptState.ACCEPTED else None,
                                 1.0, "200", None)


@pytest.mark.parametrize("other,cancelled,sender_state", [
    (AttemptState.ACCEPTED, False, AttemptState.AMBIGUOUS),          # left to send the same bytes
    (AttemptState.EXPLICIT_REJECTED, True, AttemptState.NOT_SENT)])  # no accept: cancelled, today
def test_a_sender_send_not_yet_started_is_kept_only_after_an_accept(other, cancelled, sender_state):
    kp = Keypair()
    sender = S.SenderProvider(_SenderHttp())
    transport = SubmissionTransport([_Fixed("solana:tpu", other), sender], deadline_s=0.05)
    held = _HeldExecutor("solana:sender")
    transport._executor = held
    result = transport.submit(_request(_signed(kp, _tip_ixs(kp, 5_000))))
    (fut,) = held.held
    assert fut.cancelled() is cancelled
    assert next(a for a in result.attempts if a.provider_id == "solana:sender").state is sender_state
    assert result.state is (AttemptState.ACCEPTED if other is AttemptState.ACCEPTED
                            else AttemptState.EXPLICIT_REJECTED)


def test_production_path_flags_a_sender_payload_that_is_not_the_signed_bytes(
        conn, tmp_path, monkeypatch):
    """Gamma's bytes stop: the trace compares the sha256 Sender sent with the bytes we signed."""
    ids = _ids()
    _force_ids(monkeypatch, [ids["B"]])
    ln, buy, tpu, sender_http = _production_lane(conn, tmp_path, monkeypatch)
    real = S.SenderProvider.outcome

    def other_bytes(self, identity):
        row = real(self, identity)
        return {**row, "sender_sha256": "0" * 64} if row else row
    monkeypatch.setattr(S.SenderProvider, "outcome", other_bytes)
    assert ln.handle(buy("SigSha"))["status"] == "failed"
    assert _substages(conn, ids["B"])["sender_ab_send"]["sender_payload_sha_match"] is False


# --- Sender for every slot-lane venue (Alpha 2026-10-07, operator yes after Gamma's read) ----------

def test_assign_all_is_arm_b_under_its_own_rule_with_the_ab_tip_account():
    for attempt_id in ("a" * 32, "b" * 32, "0123456789abcdef" * 2):
        arm = S.assign_all(attempt_id)
        assert arm["arm"] == "B" and arm["rule"] == "all-venues-b-v1" != S.RULE
        assert arm["tip_lamports"] == S.TIP_LAMPORTS <= S.TIP_CAP_LAMPORTS
        assert arm["tip_account"] == S.TIP_ACCOUNTS[
            hashlib.sha256(attempt_id.encode()).digest()[4] % len(S.TIP_ACCOUNTS)]


class _Trace:
    attempt_id = "c" * 32

    def __init__(self):
        self.marks = []

    def mark_substage(self, name, **kw):
        self.marks.append((name, kw))


@pytest.mark.parametrize("venue", ["raydium_cpmm", "raydium_launchlab", "meteora_damm_v2", "meteora_dbc",
                                   "pump_curve", "pump_amm"])
def test_the_all_venues_knob_tips_every_venue_under_its_own_rule(venue, monkeypatch):
    from sniper.services import memecoin_slot_lane as lane
    from sniper.settings import memecoin as mc
    monkeypatch.setattr(mc, "MEMECOIN_SLOT_LANE_SENDER_ALL", "all")
    trace = _Trace()
    arm = lane.SlotLane._sender_arm(SimpleNamespace(sender_ab=True), SimpleNamespace(venue=venue), trace)
    assert arm["arm"] == "B" and arm["rule"] == "all-venues-b-v1"
    assert trace.marks == [("sender_ab", arm)], "the trace names the rule, so the A/B read can drop it"


@pytest.mark.parametrize("venue", ["raydium_cpmm", "meteora_dbc"])
def test_with_the_knob_off_other_venues_stay_untipped_and_pump_keeps_the_ab(venue, monkeypatch):
    from sniper.services import memecoin_slot_lane as lane
    from sniper.settings import memecoin as mc
    monkeypatch.setattr(mc, "MEMECOIN_SLOT_LANE_SENDER_ALL", "off")
    trace = _Trace()
    assert lane.SlotLane._sender_arm(SimpleNamespace(sender_ab=True), SimpleNamespace(venue=venue),
                                     trace) is None
    assert trace.marks[0][1]["reason"] == "venue_not_in_ab"
    pump = lane.SlotLane._sender_arm(SimpleNamespace(sender_ab=True), SimpleNamespace(venue="pump_amm"),
                                     _Trace())
    assert pump["rule"] == S.RULE and pump == S.assign(_Trace.attempt_id)


def test_the_all_venues_knob_needs_the_sender_provider(monkeypatch):
    """Without MEMECOIN_SLOT_LANE_SENDER_AB the transport holds no Sender: nothing is tipped."""
    from sniper.services import memecoin_slot_lane as lane
    from sniper.settings import memecoin as mc
    monkeypatch.setattr(mc, "MEMECOIN_SLOT_LANE_SENDER_ALL", "all")
    assert lane.SlotLane._sender_arm(SimpleNamespace(sender_ab=False), SimpleNamespace(venue="raydium_cpmm"),
                                     _Trace()) is None


def test_production_path_all_venues_tips_an_arm_a_attempt(conn, tmp_path, monkeypatch):
    from sniper.settings import memecoin as mc
    monkeypatch.setattr(mc, "MEMECOIN_SLOT_LANE_SENDER_ALL", "all")
    ids = _ids()
    _force_ids(monkeypatch, [ids["A"]])
    ln, buy, tpu, sender_http = _production_lane(conn, tmp_path, monkeypatch)
    assert ln.handle(buy("SigA"))["status"] == "failed"
    (sent,) = tpu
    tx = VersionedTransaction.from_bytes(base64.b64decode(sent))
    assert S.tip_transfers(tx.message) == [(S.assign_all(ids["A"])["tip_account"], 5_000)]
    (_m, _u, body), = sender_http.calls
    assert body["params"][0] == sent
    stages = _substages(conn, ids["A"])
    assert stages["sender_ab"]["rule"] == "all-venues-b-v1" and stages["sender_ab_send"]["tip_lamports"] == 5_000


# --- tri-state (Alpha 2026-10-07 after Gamma's Sender read: off | ab | all) ---------------------------

@pytest.mark.parametrize("venue", ["raydium_cpmm", "raydium_launchlab", "meteora_damm_v2", "meteora_dbc",
                                   "pump_curve", "pump_amm"])
def test_ab_mode_runs_the_same_hash_rule_on_every_venue_under_a_new_rule_name(venue, monkeypatch):
    from sniper.services import memecoin_slot_lane as lane
    from sniper.settings import memecoin as mc
    monkeypatch.setattr(mc, "MEMECOIN_SLOT_LANE_SENDER_ALL", "ab")
    ids = _ids()
    for arm_name in ("A", "B"):
        trace = _Trace()
        trace.attempt_id = ids[arm_name]
        arm = lane.SlotLane._sender_arm(SimpleNamespace(sender_ab=True), SimpleNamespace(venue=venue), trace)
        ab = S.assign(ids[arm_name])
        assert arm["arm"] == ab["arm"] == arm_name, "the same sha256-attempt-odd hash decides"
        assert (arm["tip_account"], arm["tip_lamports"]) == (ab["tip_account"], ab["tip_lamports"])
        assert arm["rule"] == S.RULE_AB_ALL != S.RULE, "a new name splits the new cohort from the old"
        assert trace.marks == [("sender_ab", arm)]


@pytest.mark.parametrize("raw, mode", [("", "off"), ("off", "off"), ("0", "off"), (" AB ", "ab"),
                                       ("all", "all"), ("ALL", "all")])
def test_the_knob_reads_off_ab_or_all(raw, mode):
    from sniper.settings import memecoin as mc
    assert mc.slot_lane_sender_all_mode(raw) == mode


@pytest.mark.parametrize("raw", ["1", "true", "on", "both", "b"])
def test_an_unknown_knob_value_reads_off_loudly(raw, caplog):
    from sniper.settings import memecoin as mc
    with caplog.at_level(logging.WARNING):
        assert mc.slot_lane_sender_all_mode(raw) == "off"
    assert any("MEMECOIN_SLOT_LANE_SENDER_ALL" in r.getMessage() and raw in r.getMessage()
               for r in caplog.records)
