"""Nozomi (Temporal) A/B on the Solana slot lane (operator YES 2026-10-09 via Alpha; Gamma's
pre-registration docs/audits/2026-10-09-nozomi-slot-lane-ab-preregistration.md, f7760ad8b).

Arm B replaces the Sender tip with ONE 5,000,000-lamport transfer to a Nozomi tip account, and the
same signed bytes go to free-direct and to Nozomi. Gamma's early stops each have a test here: bytes,
tip, key, arm. Alpha's must-fix: the Nozomi POST starts in parallel with free-direct, never after
an accept.
"""
import base64
import hashlib
import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from sniper.execution import solana_sender_ab as S
from sniper.execution.submission_transport import AttemptState, SubmissionAttempt, SubmissionTransport
from test_memecoin_slot_lane_sender_ab import (  # noqa: F401  (conn is a fixture)
    SOURCE, SPEND, TICKET, BLOCKHASH, _SenderHttp, _arm_by_hand, _force_ids, _ids, _msg, _request,
    _signed, _substages, conn,
)

KEY = "nozomi-test-key-0f3c9a1e"
NOZOMI_URL = "https://fra2.nozomi.temporal.xyz/?c=" + KEY
# The 17 public tip accounts, copied from use.temporal.xyz/nozomi/tipping-and-faq.md on 2026-10-09.
PAGE_ACCOUNTS = (
    "TEMPaMeCRFAS9EKF53Jd6KpHxgL47uWLcpFArU1Fanq", "noz3jAjPiHuBPqiSPkkugaJDkJscPuRhYnSpbi8UvC4",
    "noz3str9KXfpKknefHji8L1mPgimezaiUyCHYMDv1GE", "noz6uoYCDijhu1V7cutCpwxNiSovEwLdRHPwmgCGDNo",
    "noz9EPNcT7WH6Sou3sr3GGjHQYVkN3DNirpbvDkv9YJ", "nozc5yT15LazbLTFVZzoNZCwjh3yUtW86LoUyqsBu4L",
    "nozFrhfnNGoyqwVuwPAW4aaGqempx4PU6g6D9CJMv7Z", "nozievPk7HyK1Rqy1MPJwVQ7qQg2QoJGyP71oeDwbsu",
    "noznbgwYnBLDHu8wcQVCEw6kDrXkPdKkydGJGNXGvL7", "nozNVWs5N8mgzuD3qigrCG2UoKxZttxzZ85pvAQVrbP",
    "nozpEGbwx4BcGp6pvEdAh1JoC2CQGZdU6HbNP1v2p6P", "nozrhjhkCr3zXT3BiT4WCodYCUFeQvcdUkM7MqhKqge",
    "nozrwQtWhEdrA6W8dkbt9gnUaMs52PdAv5byipnadq3", "nozUacTVWub3cL4mJmGCYjKZTnE9RbdY5AP46iQgbPJ",
    "nozWCyTPppJjRuw2fpzDhhWbW355fzosWSzrrMYB1Qk", "nozWNju6dY353eMkMqURqwQEoM3SFgEKC6psLCSfUne",
    "nozxNBgWohjR75vdspfxR5H9ceC7XXH99xpxhVGt3Bb",
)


def _noz_ixs(kp, lamports, k=0):
    return S.tip_instructions(kp.pubkey(), (S.NOZOMI_TIP_ACCOUNTS[k], lamports))


def _sender_ixs(kp, lamports=5_000):
    return S.tip_instructions(kp.pubkey(), (S.TIP_ACCOUNTS[0], lamports))


def _provider(http, **kw):
    return S.NozomiProvider(http, url=NOZOMI_URL, **kw)


# --- the arm rule ---------------------------------------------------------------------------------

def test_the_tip_accounts_are_the_tipping_page_list():
    assert S.NOZOMI_TIP_ACCOUNTS == PAGE_ACCOUNTS
    assert not set(S.NOZOMI_TIP_ACCOUNTS) & set(S.TIP_ACCOUNTS)


def test_the_arm_is_the_preregistered_rule_with_a_nozomi_tip_on_b_only():
    for aid in ["%032x" % i for i in range(64)] + ["deadbeef", "attempt-1"]:
        got = S.assign_nozomi(aid)
        digest = hashlib.sha256(aid.encode()).digest()
        assert got["arm"] == _arm_by_hand(aid) and got["rule"] == "nozomi-sha256-attempt-odd-v1"
        assert got["attempt_hash"] == digest[:4].hex()
        if got["arm"] == "B":
            assert got["tip_lamports"] == 5_000_000
            assert got["tip_account"] == PAGE_ACCOUNTS[digest[4] % 17]
        else:
            assert got["tip_lamports"] == 0 and got["tip_account"] is None
    assert {S.assign_nozomi("%032x" % i)["arm"] for i in range(64)} == {"A", "B"}


# --- the guard before signing ---------------------------------------------------------------------

def test_the_guard_refuses_every_tip_the_arm_did_not_ask_for():
    kp = Keypair()
    ok_b = _msg(kp, _noz_ixs(kp, 5_000_000))
    assert S.guard_refusal(ok_b, 0, nozomi_lamports=5_000_000) is None
    assert S.guard_refusal(_msg(kp, _sender_ixs(kp)), 5_000, nozomi_lamports=0) is None   # arm A today
    refusals = [
        S.guard_refusal(ok_b, 5_000, nozomi_lamports=0),                       # Nozomi tip on arm A
        S.guard_refusal(ok_b, 0, nozomi_lamports=0),                           # Nozomi tip, no arm
        S.guard_refusal(_msg(kp, _sender_ixs(kp)), 0, nozomi_lamports=5_000_000),   # Sender tip on B
        S.guard_refusal(_msg(kp, _noz_ixs(kp, 4_999_999)), 0, nozomi_lamports=5_000_000),
        S.guard_refusal(_msg(kp, _noz_ixs(kp, 5_000_001)), 0, nozomi_lamports=5_000_001),
        S.guard_refusal(_msg(kp, _noz_ixs(kp, 5_000_000) + _sender_ixs(kp)), 5_000,
                        nozomi_lamports=5_000_000),                            # two tips
        S.guard_refusal(_msg(kp, _noz_ixs(kp, 2_500_000) + _noz_ixs(kp, 2_500_000, k=1)), 0,
                        nozomi_lamports=5_000_000),
        S.guard_refusal(_msg(kp, []), 0, nozomi_lamports=5_000_000),           # B lost its tip
    ]
    assert all(r and r.startswith("sender_ab_tip_guard") for r in refusals), refusals


# --- the Nozomi provider --------------------------------------------------------------------------

def test_nozomi_posts_only_a_nozomi_tipped_payload_unchanged_and_records_its_answer():
    kp = Keypair()
    http = _SenderHttp()
    provider = _provider(http, timeout_s=1.0)
    for ixs in ([], _sender_ixs(kp), _noz_ixs(kp, 4_000_000)):
        assert provider.submit(_request(_signed(kp, ixs))).state is AttemptState.NOT_SENT
    assert http.calls == []
    req = _request(_signed(kp, _noz_ixs(kp, 5_000_000)))
    attempt = provider.submit(req)
    assert attempt.state is AttemptState.ACCEPTED and attempt.provider_id == "solana:nozomi"
    (method, url, body), = http.calls
    assert (method, url) == ("POST", NOZOMI_URL)
    assert body["method"] == "sendTransaction" and body["params"][0] == req.signed_payload
    assert body["params"][1] == {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}
    seen = provider.outcome(req.tx_identity)
    assert seen["nozomi_state"] == "accepted" and seen["nozomi_http_status"] == "200"
    assert seen["nozomi_sha256"] == req.signed_payload_sha256
    assert seen["nozomi_returned_sig"] == req.tx_identity and seen["nozomi_ms"] >= 0
    assert isinstance(seen["nozomi_post_started"], float)


@pytest.mark.parametrize("answer,status", [({"error": {"code": 401}}, 401), ({}, 429),
                                           (OSError("timed out"), 200)])
def test_a_refused_or_lost_nozomi_send_is_submitted_unknown_never_a_rejection(answer, status):
    kp = Keypair()
    provider = _provider(_SenderHttp(answer=answer, status=status))
    req = _request(_signed(kp, _noz_ixs(kp, 5_000_000)))
    assert provider.submit(req).state is AttemptState.AMBIGUOUS
    assert provider.outcome(req.tx_identity)["nozomi_state"] == "submitted_unknown"


# --- Alpha's must-fix: the Nozomi POST never waits on free-direct ----------------------------------

def test_the_nozomi_post_is_issued_before_free_directs_accept_returns():
    kp = Keypair()
    posted, answered = threading.Event(), threading.Event()

    class NozHttp(_SenderHttp):
        def __call__(self, *a, **k):
            posted.set()
            return super().__call__(*a, **k)

    class FreeDirect:
        provider_id = "solana:tpu"
        saw_post = None

        def submit(self, request):
            # Accept only once the Nozomi POST is on the wire: a Nozomi dispatched after this answer
            # (sequential, or gated on an accept) leaves the event unset.
            FreeDirect.saw_post = posted.wait(1.0)
            answered.set()
            return SubmissionAttempt("solana:tpu", AttemptState.ACCEPTED, request.tx_identity,
                                     1.0, "200", None)
    sender = S.SenderProvider(_SenderHttp())
    transport = SubmissionTransport([FreeDirect(), sender, _provider(NozHttp())], deadline_s=2.0)
    result = transport.submit(_request(_signed(kp, _noz_ixs(kp, 5_000_000))))
    assert answered.wait(2.0) and FreeDirect.saw_post is True
    assert result.state is AttemptState.ACCEPTED


# --- the backend ----------------------------------------------------------------------------------

def test_nozomi_is_the_last_provider_only_with_knob_and_key_and_never_logs_the_key(caplog):
    from test_memecoin_submission_backends import FakeClient, FakeSidecar
    from sniper.execution import submission_backends as B
    caplog.set_level(logging.DEBUG)
    try:
        def build(env):
            transport, _ = B.build_solana_backend(
                keypair=Keypair(), custody_rpc_url="https://custody/rpc", env=env,
                client_factory=FakeClient, tpu_sidecar=FakeSidecar())
            return transport
        on = build({"MEMECOIN_SLOT_LANE_SENDER_AB": "1", "MEMECOIN_SLOT_LANE_NOZOMI_AB": "1",
                    "MEMECOIN_NOZOMI_API_KEY": KEY})
        assert [p.provider_id for p in on._providers] == [
            "solana:custody_rpc", "solana:public_rpc", "solana:tpu", "solana:sender", "solana:nozomi"]
        assert on.provider("solana:nozomi")._rpc._url == NOZOMI_URL
        assert build({"MEMECOIN_SLOT_LANE_NOZOMI_AB": "1"}).provider("solana:nozomi") is None
        assert build({"MEMECOIN_NOZOMI_API_KEY": KEY}).provider("solana:nozomi") is None
    finally:
        B.close_all()
    assert "Nozomi" in caplog.text and KEY not in caplog.text
    assert KEY not in repr(_provider(_SenderHttp())) and KEY not in str(_provider(_SenderHttp()))


def test_the_nozomi_knob_needs_the_shared_transport():
    from sniper.settings import memecoin_submission as C
    with pytest.raises(C.SubmissionConfigError, match="shared transport"):
        C.validate_submission_config({"MEMECOIN_SOLANA_SUBMIT_MODE": "rpc",
                                      "MEMECOIN_SLOT_LANE_NOZOMI_AB": "1"})


# --- the production slot-lane send path -----------------------------------------------------------

def _production_lane(conn, tmp_path, monkeypatch, *, nozomi=True, wallets=("w1",)):
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
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 0.0), ("MEMECOIN_REAL_CHASE_MAX_PCT", 30.0),
                        ("MEMECOIN_SLOT_LANE_SENDER_ALL", "all"),
                        ("MEMECOIN_SLOT_LANE_NOZOMI_AB_WALLETS", frozenset(wallets))):
        monkeypatch.setattr(mc, name, value)
    monkeypatch.setattr(mc, "memecoin_slot_lane_resend_offsets_ms", lambda: ())
    monkeypatch.setattr(pcb, "leg_clip_refusal", lambda leg, clip: None)
    kp = Keypair()
    event = pcb.parse_trade_event(SOURCE["trade_event_log"])
    builder = pcb.SlotLaneCurveBuilder(user=str(kp.pubkey()), jup_call=lambda *a, **k: None,
                                       rpc=lambda *a: None, clock=lambda: 1000.0)
    builder._buyback = event["fee_recipient"]
    builder._leg = {"ixs": [], "tables": [], "usdc_atomic": TICKET, "min_out_lamports": SPEND,
                    "out_lamports": SPEND, "fetched_at": 1000.0}
    tpu = []
    tpu_seen, seen_lock = {}, threading.Lock()

    def seen(signature):
        with seen_lock:
            return tpu_seen.setdefault(signature, threading.Event())

    class Tpu:
        provider_id = "solana:tpu"

        def submit(self, request):
            tpu.append(request.signed_payload)
            seen(request.tx_identity).set()
            return SubmissionAttempt("solana:tpu", AttemptState.ACCEPTED, request.tx_identity,
                                     1.0, "5", None)

    class ObservedHttp(_SenderHttp):
        def __call__(self, method, url, headers=None, body=None, timeout=None):
            from sniper.execution.submission_wiring import solana_tx_identity
            payload = json.loads(body)["params"][0]
            # This fixture compares both transports' bytes. A paid fake cannot cancel a queued
            # TPU fake by accepting first, before that exact signature has been observed.
            assert seen(solana_tx_identity(payload)).wait(2.0), "same-signature TPU evidence missing"
            return super().__call__(method, url, headers=headers, body=body, timeout=timeout)

    sender_http, nozomi_http = ObservedHttp(), ObservedHttp()

    def confirm(signature, *_a, **_k):
        assert seen(signature).wait(2.0), "same-signature TPU evidence missing"
        # Finish this attempt's provider workers before reading outcomes or starting another arm.
        # Cumulative call counts and a fixed sleep cannot prove either boundary.
        deadline = time.monotonic() + 2.0
        while (any(lease.locked() for _, lease in transport._provider_leases)
               and time.monotonic() < deadline):
            time.sleep(0.001)
        assert not any(lease.locked() for _, lease in transport._provider_leases), "fixture fanout unfinished"
        return "failed"
    monkeypatch.setattr(seams, "rpc_confirm", confirm)
    providers = [Tpu(), S.SenderProvider(sender_http, timeout_s=1.0)]
    if nozomi:
        providers.append(_provider(nozomi_http, timeout_s=1.0))
    transport = SubmissionTransport(providers, deadline_s=2.0)
    seam = seams.build_real_solana_executor(keypair=kp, rpc_url="http://unused",
                                            submission_transport=transport)
    assert seam["nozomi_ab_ready"] is nozomi
    snap = lane.PrefetchSnapshot(sol_usd=150.0, usdc_balance=400.0, sol_balance=0.5,
                                 blockhash=BLOCKHASH, last_valid_height=1,
                                 observed_mono={k: time.monotonic() for k in
                                                ("sol_usd", "balances", "blockhash")})
    ln = lane.SlotLane(conn=conn, executor={"slot_execute_fn": seam["slot_execute_fn"],
                                            "sender_ab_ready": seam["sender_ab_ready"],
                                            "nozomi_ab_ready": seam["nozomi_ab_ready"]},
                       build_fn=lane.dispatch_build({"pump_curve": builder}),
                       snapshot_fn=lambda: snap, armed_ids_fn=lambda: {"w1", "w2"},
                       kill_tripped_fn=lambda: False,
                       now_iso_fn=lambda: "2026-10-09T12:30:00+00:00", lock_key="sol-test",
                       mode="live", head_slot_fn=lambda: (450_000_000, time.monotonic()))

    def buy(signature, wallet="w1"):
        return SimpleNamespace(
            source_user_id=wallet, owner="Owner1", mint=event["mint"], signature=signature,
            slot=450_000_000, sol_amount=event["sol_amount"],
            virtual_sol_reserves=event["virtual_sol_reserves"],
            virtual_token_reserves=event["virtual_token_reserves"],
            recv_utc="2026-10-09T12:30:00+00:00", recv_monotonic=time.monotonic(),
            token_program=pcb.TOKEN_PROGRAM, trade_event_log=SOURCE["trade_event_log"])
    return ln, buy, tpu, sender_http, nozomi_http


def _tips(signed_b64):
    msg = VersionedTransaction.from_bytes(base64.b64decode(signed_b64)).message
    return S.tip_transfers(msg), S.tip_transfers(msg, S.NOZOMI_TIP_ACCOUNTS)


def test_production_arm_b_sends_nozomi_the_free_direct_bytes_and_no_sender_post(
        conn, tmp_path, monkeypatch):
    ids = _ids()
    _force_ids(monkeypatch, [ids["B"]])
    ln, buy, tpu, sender_http, nozomi_http = _production_lane(conn, tmp_path, monkeypatch)
    assert ln.handle(buy("SigB"))["status"] == "failed"
    (sent,) = tpu
    (_m, url, body), = nozomi_http.calls
    assert url == NOZOMI_URL and body["params"][0] == sent, "Nozomi gets the exact free-direct bytes"
    assert sender_http.calls == [], "arm B carries no Sender tip, so no Sender POST"
    arm = S.assign_nozomi(ids["B"])
    assert _tips(sent) == ([], [(arm["tip_account"], 5_000_000)])
    stages = _substages(conn, ids["B"])
    ab = stages["nozomi_ab"]
    assert (ab["arm"], ab["rule"], ab["attempt_hash"], ab["tip_lamports"], ab["tip_account"]) == (
        "B", "nozomi-sha256-attempt-odd-v1", arm["attempt_hash"], 5_000_000, arm["tip_account"])
    assert "sender_ab" not in stages and "sender_ab_send" not in stages
    send = stages["nozomi_ab_send"]
    assert send["tip_lamports"] == 5_000_000 and send["nozomi_http_status"] == "200"
    assert send["nozomi_state"] == "accepted" and send["nozomi_payload_sha_match"] is True
    assert send["nozomi_sig_match"] is True and send["nozomi_ms"] >= 0
    # The POST start, from the decision, on the trace's clock: after the decision, before this mark.
    assert 0 <= send["nozomi_post_start_ms"] <= send["elapsed_ms"]
    deadline = time.monotonic() + 2.0                  # the attempt rows are written off-thread
    providers = set()
    while "solana:nozomi" not in providers and time.monotonic() < deadline:
        providers = {r[0] for r in conn.execute("SELECT provider_id FROM memecoin_submission_attempts")}
        time.sleep(0.01)
    assert "solana:nozomi" in providers


def test_production_arm_a_is_todays_sender_send_and_never_reaches_nozomi(conn, tmp_path, monkeypatch):
    ids = _ids()
    _force_ids(monkeypatch, [ids["A"]])
    ln, buy, tpu, sender_http, nozomi_http = _production_lane(conn, tmp_path, monkeypatch)
    assert ln.handle(buy("SigA"))["status"] == "failed"
    (sent,) = tpu
    assert nozomi_http.calls == [] and len(sender_http.calls) == 1
    assert _tips(sent) == ([(S.assign_all(ids["A"])["tip_account"], 5_000)], [])
    stages = _substages(conn, ids["A"])
    assert stages["nozomi_ab"]["arm"] == "A" and stages["nozomi_ab"]["tip_lamports"] == 0
    assert stages["sender_ab"]["rule"] == "all-venues-b-v1" and "nozomi_ab_send" not in stages


def test_the_only_signed_difference_between_the_arms_is_the_tip_account_and_amount(
        conn, tmp_path, monkeypatch):
    from sniper.execution.solana_slot_guard import LIGHTHOUSE_PROGRAM
    ids = _ids()
    _force_ids(monkeypatch, [ids["A"], ids["B"]])
    ln, buy, tpu, _s, _n = _production_lane(conn, tmp_path, monkeypatch)
    results = [ln.handle(buy("SigA")), ln.handle(buy("SigB"))]
    assert len(tpu) == 2, [(r.get('status'), r.get('reason')) for r in results]
    a, b = (VersionedTransaction.from_bytes(base64.b64decode(x)).message for x in tpu)
    assert len(a.instructions) == len(b.instructions)
    assert _tips(tpu[0]) == ([(S.assign_all(ids['A'])['tip_account'], 5_000)], [])
    assert _tips(tpu[1]) == ([], [(S.assign_nozomi(ids['B'])['tip_account'], 5_000_000)])
    # PR #486 (2026-10-20): no Lighthouse source-slot guards on automatic entries. The only signed
    # difference between the arms is the one system transfer that carries the tip.
    data_a = [bytes(i.data) for i in a.instructions]
    data_b = [bytes(i.data) for i in b.instructions]
    diff = [k for k, (x, y) in enumerate(zip(data_a, data_b)) if x != y]
    assert len(diff) == 1 and data_a[diff[0]][:4] == data_b[diff[0]][:4] == b'\x02\x00\x00\x00', diff
    for msg in (a, b):
        assert all(str(msg.account_keys[ix.program_id_index]) != LIGHTHOUSE_PROGRAM for ix in msg.instructions)


@pytest.mark.parametrize("arm", ["A", "B"])
def test_production_fixture_waits_for_the_same_signature_when_tpu_is_late(
        conn, tmp_path, monkeypatch, arm):
    """A tipped ACK may win before a running TPU fake has recorded the signed bytes."""
    ids = _ids()
    _force_ids(monkeypatch, [ids[arm]])
    started, release, returned = threading.Event(), threading.Event(), threading.Event()
    early_return = []
    run_one, http_call = SubmissionTransport._run_one, _SenderHttp.__call__

    def delayed_tpu(self, provider, request):
        if provider.provider_id == "solana:tpu":
            started.set()
            assert release.wait(1.0)
        return run_one(self, provider, request)

    def started_http(self, *a, **kw):
        assert started.wait(1.0)  # Prove a running late TPU, rather than a cancelled queued job.
        return http_call(self, *a, **kw)

    monkeypatch.setattr(SubmissionTransport, "_run_one", delayed_tpu)
    monkeypatch.setattr(_SenderHttp, "__call__", started_http)
    ln, buy, tpu, sender_http, nozomi_http = _production_lane(conn, tmp_path, monkeypatch)

    def unblock():
        try:
            if started.wait(1.0):
                early_return.append(returned.wait(0.05))
        finally:
            release.set()

    observer = threading.Thread(target=unblock, daemon=True)
    observer.start()
    try:
        result = ln.handle(buy("SigLate" + arm))
    finally:
        returned.set()
        release.set()
        observer.join(1.0)
    assert not observer.is_alive() and early_return == [False], "fixture returned before TPU evidence"
    assert result["status"] == "failed"
    (sent,) = tpu
    paid = sender_http if arm == "A" else nozomi_http
    assert paid.calls[0][2]["params"][0] == sent


@pytest.mark.parametrize("nozomi,wallet", [(False, "w1"), (True, "w2")])
def test_no_provider_or_an_unlisted_wallet_is_todays_send_without_a_nozomi_arm(
        conn, tmp_path, monkeypatch, nozomi, wallet):
    ids = _ids()
    _force_ids(monkeypatch, [ids["B"]])
    ln, buy, tpu, sender_http, nozomi_http = _production_lane(conn, tmp_path, monkeypatch,
                                                              nozomi=nozomi)
    assert ln.handle(buy("SigOff", wallet=wallet))["status"] == "failed"
    (sent,) = tpu
    assert nozomi_http.calls == [] and len(sender_http.calls) == 1
    assert _tips(sent)[1] == []
    stages = _substages(conn, ids["B"])
    assert "nozomi_ab" not in stages and "nozomi_ab_send" not in stages


def test_production_refuses_a_nozomi_tip_on_arm_a_before_signing(conn, tmp_path, monkeypatch):
    ids = _ids()
    _force_ids(monkeypatch, [ids["A"]])
    ln, buy, tpu, sender_http, nozomi_http = _production_lane(conn, tmp_path, monkeypatch)
    real = ln.build_fn

    def nozomi_anyway(buy_, clip, snap, **kw):
        kw["tip"] = (S.NOZOMI_TIP_ACCOUNTS[0], 5_000_000)
        return real(buy_, clip, snap, **kw)
    ln.build_fn = nozomi_anyway
    result = ln.handle(buy("SigBug"))
    assert result["status"] == "aborted" and "sender_ab_tip_guard" in result["reason"]
    assert tpu == [] and sender_http.calls == [] and nozomi_http.calls == []


def test_a_native_funded_arm_b_reserves_the_tip_in_its_funding_overhead(conn, tmp_path, monkeypatch):
    from sniper.services import memecoin_risk as risk, memecoin_slot_lane as lane
    ids = _ids()
    seen = []
    real = risk.reserve_entry

    def spy(*a, **k):
        seen.append(k.get("funding_overhead_atomic"))
        return real(*a, **k)
    monkeypatch.setattr(risk, "reserve_entry", spy)
    for arm in ("A", "B"):
        _force_ids(monkeypatch, [ids[arm]])
        ln, buy, *_ = _production_lane(conn, tmp_path, monkeypatch)
        ln.sol_buffer_cap_usd, ln.shadow_clip_fn = 200.0, (lambda: 50.0)
        ln.snapshot_fn = lambda: lane.PrefetchSnapshot(
            sol_usd=150.0, usdc_balance=400.0, sol_balance=5.0, blockhash=BLOCKHASH,
            last_valid_height=1, observed_mono={k: time.monotonic() for k in
                                                ("sol_usd", "balances", "blockhash")})
        ln.handle(buy("Sig" + arm))
    native = [s for s in seen if s is not None]
    assert native[:2] == [lane.NATIVE_ATA_RENT_RESERVE_LAMPORTS,
                          lane.NATIVE_ATA_RENT_RESERVE_LAMPORTS + 5_000_000], seen


def test_a_native_arm_b_fill_books_its_spend_without_the_nozomi_tip(conn, monkeypatch):
    """Gamma's M7: the receipt reader must subtract the Nozomi tip the guard proved the message
    carries, through slot_execute_fn's own receipt path, or a native arm-B fill reads spent over the
    reservation and books receipt-unreadable."""
    from solders.hash import Hash
    from sniper.execution import solana_real_seams as seams, solana_slot_executor as slot
    from sniper.persistence import db_memecoin as m
    kp = Keypair()
    owner = str(kp.pubkey())
    funding, fee, rent, tip = 500_000_000, 100_000, 2_039_280, 5_000_000
    pid = m.reserve_pending(conn, mint="native-coin", source_user_id="w1", strategy="alpha_b",
                            notional=50.0, reserved_at="2026-10-09T12:00:00Z", network_id="solana",
                            funding_asset="sol", funding_amount_atomic=funding, funding_sol_usd=100.0)

    def cached(_read):
        def receipt(sig):
            return {"transaction": {"signatures": [sig], "message": {"accountKeys": [
                {"pubkey": owner, "signer": True}, {"pubkey": "token", "signer": False}]}},
                    "meta": {"err": None, "fee": fee,
                             "preBalances": [1_000_000_000, 0],
                             "postBalances": [1_000_000_000 - funding - fee - tip - rent, rent],
                             "preTokenBalances": [],
                             "postTokenBalances": [{"accountIndex": 1, "mint": "native-coin",
                                                    "owner": owner, "uiTokenAmount": {
                                                        "amount": "1000000", "decimals": 6}}]}}
        return receipt
    monkeypatch.setattr(seams, "successful_receipt_cache", cached)

    def inspect(_conn, **kw):
        return {"status": "probe", "proof": kw["receipt_fill_fn"](kw["sig"], "native-coin", 50.0)}
    monkeypatch.setattr(slot, "execute_prebuilt_entry", inspect)
    transport = SubmissionTransport([_provider(_SenderHttp())], deadline_s=1.0)
    seam = seams.build_real_solana_executor(keypair=kp, rpc_url="http://unused",
                                            submission_transport=transport)
    assert seam["nozomi_ab_ready"] is True
    result = seam["slot_execute_fn"](
        conn, m.pending_row(conn, pid), _msg(kp, _noz_ixs(kp, tip)),
        validity={"funding_asset": "sol", "funding_amount_atomic": funding,
                  "nozomi_tip_lamports": tip},
        now_iso="2026-10-09T12:00:01Z", entry_min_slot=100, entry_max_slot=101,
        entry_head_fn=lambda: (100, time.monotonic()))
    assert result["proof"]["sol_spent_lamports"] == funding
    assert result["proof"]["tip_lamports"] == tip


def test_nozomi_retains_actual_post_finish_on_its_monotonic_clock():
    kp = Keypair()
    provider = _provider(_SenderHttp())
    ticks = iter([9.0, 10.0, 10.25, 11.0])
    provider._clock = lambda: next(ticks)
    req = _request(_signed(kp, _noz_ixs(kp, 5_000_000)))
    attempt = provider.submit(req)
    seen = provider.outcome(req.tx_identity)
    assert attempt.provider_timing == {'post_started_monotonic': 10.0,
                                       'post_finished_monotonic': 10.25,
                                       'clock': 'perf_counter'}
    assert seen['nozomi_post_started'] == 10.0
    assert seen['nozomi_post_finished'] == 10.25
    assert seen['nozomi_ms'] == 250.0


def test_nozomi_post_clock_excludes_serialization_and_response_classification(monkeypatch):
    from sniper.execution import solana_fast_submit as rpc
    clock = [9.0]
    dumps = rpc.json.dumps

    def serialize(*a, **kw):
        clock[0] = 10.0
        return dumps(*a, **kw)

    class Answer(dict):
        def get(self, *a, **kw):
            clock[0] = 13.0
            return super().get(*a, **kw)

    def http(method, url, *, body, **kw):
        assert clock[0] == 10.0
        clock[0] = 12.0
        return 200, Answer(result=req.tx_identity), None

    provider = _provider(http)
    provider._clock = lambda: clock[0]
    kp = Keypair()
    req = _request(_signed(kp, _noz_ixs(kp, 5_000_000)))
    monkeypatch.setattr(rpc.json, 'dumps', serialize)
    provider.submit(req)
    seen = provider.outcome(req.tx_identity)
    assert seen['nozomi_post_started'] == 10.0
    assert seen['nozomi_post_finished'] == 12.0
    assert seen['nozomi_ms'] == 2000.0
