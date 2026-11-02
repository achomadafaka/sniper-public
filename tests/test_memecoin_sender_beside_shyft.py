"""Helius Sender beside the Shyft infra provider (design 2026-10-15-helius-sender-beside-shyft).

Under Shyft, MEMECOIN_SLOT_LANE_SENDER_AB=1 was refused at startup and the backend never built a
Sender provider. With MEMECOIN_SOLANA_SENDER_WITH_SHYFT on (default), the REAL Shyft backend appends
SenderProvider after custody and TPU; arm B's same signed bytes reach it, arm A never does."""
import base64
import json
import time

import pytest
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from sniper.execution import solana_sender_ab as S
from sniper.execution import submission_backends as B
from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
from sniper.execution.submission_transport import AttemptState
from sniper.settings import memecoin_submission as C
from test_memecoin_shyft_submission import AMS, ENV, Sidecar
from test_memecoin_slot_lane_sender_ab import _ids, _force_ids, _request, _signed, _substages, conn  # noqa: F401

SENDER_ENV = {**ENV, "MEMECOIN_SOLANA_SUBMIT_MODE": "auto_free", "MEMECOIN_SLOT_LANE_SENDER_AB": "1"}


@pytest.fixture(autouse=True)
def clean_backends():
    B.close_all()
    yield
    B.close_all()


class _Http:
    """Custody identity answers and every sendTransaction recorded by URL."""
    def __init__(self):
        self.sends = []

    def __call__(self, method, url, **kwargs):
        if method == "GET":
            return 200, "ok", None
        body = json.loads(kwargs["body"])
        if body["method"] == "sendTransaction":
            self.sends.append((url, body["params"][0]))
            from sniper.execution.submission_wiring import solana_tx_identity
            return 200, {"result": solana_tx_identity(body["params"][0])}, None
        return 200, {"result": "ok" if body["method"] == "getHealth" else SOLANA_MAINNET_GENESIS}, None


def _wait_for(predicate, timeout_s=5.0):
    """Poll until `predicate()` holds or the deadline passes (posts land on transport workers)."""
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    return predicate()


def _backend(env, http, sidecar=None):
    C.validate_submission_config(env)
    transport, _ = B.build_solana_backend(keypair=Keypair(), custody_rpc_url=AMS, env=env, http=http,
                                         tpu_sidecar=sidecar or Sidecar(), warm_external_http=True)
    return transport


def _sender_answer(result):
    """Sender's final answer. After every late future resolved no provider can still post. Custody
    is NOT asserted: a fast TPU accept cancels custody's not-yet-started future (no post)."""
    late = [future.result(timeout=5.0) for future in result.late_results]
    answers = [a for a in (*result.attempts, *late)
               if a.provider_id == "solana:sender" and a.reason_code != "after_accept"]
    assert len(answers) == 1, answers
    return answers[0]


def _carried(http, sidecar):
    """The bytes every carrier that did post received (custody, TPU)."""
    return [p for url, p in http.sends if url == AMS] + list(sidecar.payloads)


def test_shyft_accepts_the_sender_ab_with_the_registration_knob_and_refuses_without_it():
    C.validate_submission_config(SENDER_ENV)                                      # default on
    with pytest.raises(C.SubmissionConfigError, match="MEMECOIN_SLOT_LANE_SENDER_AB"):
        C.validate_submission_config({**SENDER_ENV, "MEMECOIN_SOLANA_SENDER_WITH_SHYFT": "0"})


def test_the_shyft_backend_appends_sender_after_custody_and_tpu():
    transport = _backend(SENDER_ENV, _Http())
    assert [p.provider_id for p in transport._providers] == ["solana:custody_rpc", "solana:tpu", "solana:sender"]


@pytest.mark.parametrize("env", [{**ENV, "MEMECOIN_SOLANA_SUBMIT_MODE": "auto_free"},
                                 {**SENDER_ENV, "MEMECOIN_SOLANA_SENDER_WITH_SHYFT": "off",
                                  "MEMECOIN_SLOT_LANE_SENDER_AB": "0"}])
def test_without_the_sender_knob_the_shyft_graph_is_todays(env):
    transport = _backend(env, _Http())
    assert [p.provider_id for p in transport._providers] == ["solana:custody_rpc", "solana:tpu"]


def test_a_tipped_payload_reaches_sender_with_the_same_bytes_and_an_untipped_one_never_does():
    http, sidecar = _Http(), Sidecar()
    transport = _backend(SENDER_ENV, http, sidecar)
    payer = Keypair()
    tipped = _request(_signed(payer, S.tip_instructions(payer.pubkey(), (S.TIP_ACCOUNTS[0], S.TIP_LAMPORTS))))
    result = transport.submit(tipped)
    assert result.state is AttemptState.ACCEPTED
    assert _sender_answer(result).state is AttemptState.ACCEPTED
    assert [p for url, p in http.sends if url == S.SENDER_URL] == [tipped.signed_payload]
    assert set(_carried(http, sidecar)) == {tipped.signed_payload}      # one payload, every carrier
    http.sends.clear()
    plain = _request(_signed(Keypair(), []))
    result = transport.submit(plain)
    assert result.state is AttemptState.ACCEPTED
    answer = _sender_answer(result)
    assert answer.state is AttemptState.NOT_SENT and answer.reason_code == "untipped"
    assert not any(url == S.SENDER_URL for url, _p in http.sends)


def test_the_slot_lane_arm_b_sends_sender_the_signed_bytes_through_the_real_shyft_backend(
        conn, tmp_path, monkeypatch):
    """The live seam: build_solana_backend under Shyft -> build_real_solana_executor -> the slot lane."""
    from sniper.execution import solana_real_seams as seams
    from test_memecoin_slot_lane_sender_ab import _production_lane
    http, sidecar = _Http(), Sidecar()
    transport = _backend(SENDER_ENV, http, sidecar)
    ids = _ids()
    _force_ids(monkeypatch, [ids["B"], ids["A"]])
    ln, buy, _tpu, _sender_http = _production_lane(conn, tmp_path, monkeypatch, transport=transport)
    sender_posted = lambda: any(url == S.SENDER_URL for url, _p in http.sends)   # noqa: E731
    expect_sender = []

    def confirm(*_a, **_k):
        # The chain read takes >= 250 ms live; here it waits for Sender's post (its outcome is
        # stamped before the post), so the trace reads Sender's own answer, never a race.
        if expect_sender:
            _wait_for(sender_posted)
        return "failed"
    monkeypatch.setattr(seams, "rpc_confirm", confirm)
    assert ln.sender_ab is True
    expect_sender.append(True)
    ln.handle(buy("SigB"))
    sender = [p for url, p in http.sends if url == S.SENDER_URL]
    assert len(sender) == 1 and set(_carried(http, sidecar)) == set(sender), \
        "Sender gets exactly the bytes the other carriers got"
    tx = VersionedTransaction.from_bytes(base64.b64decode(sender[0]))
    assert S.tip_transfers(tx.message) == [(S.assign(ids["B"])["tip_account"], 5_000)]
    assert _substages(conn, ids["B"])["sender_ab_send"]["sender_payload_sha_match"] is True
    http.sends.clear()
    sidecar.payloads.clear()
    expect_sender.clear()
    ln.handle(buy("SigA"))
    carried = _carried(http, sidecar)                     # the accepting carrier posted before handle returned
    assert carried and all(
        S.tip_transfers(VersionedTransaction.from_bytes(base64.b64decode(p)).message) == [] for p in carried), \
        "arm A is signed untipped, so Sender refuses it (NOT_SENT 'untipped', proven above)"
    assert not sender_posted(), "arm A never reaches Sender"


# --- Gamma's Sender-only-carrier probe on 51a710b38 -------------------------------------------------
# No TPU sidecar and a custody preflight refusal: Sender is the only provider that can carry the bytes.

class _CustodyRefusesHttp(_Http):
    def __init__(self, sender_status):
        super().__init__()
        self.sender_status = sender_status

    def __call__(self, method, url, **kwargs):
        if method != "GET":
            body = json.loads(kwargs["body"])
            if body["method"] == "sendTransaction":
                self.sends.append((url, body["params"][0]))
                if url == AMS:
                    return 200, {"error": {"code": -32002, "message": "Transaction simulation failed: "
                                           "Error processing Instruction 2: custom program error: 0x1772"}}, None
                if self.sender_status != 200:
                    return self.sender_status, {}, None
                from sniper.execution.submission_wiring import solana_tx_identity
                return 200, {"result": solana_tx_identity(body["params"][0])}, None
        return super().__call__(method, url, **kwargs)


def _sender_only(sender_status):
    http = _CustodyRefusesHttp(sender_status)
    C.validate_submission_config(SENDER_ENV)
    transport, _ = B.build_solana_backend(keypair=Keypair(), custody_rpc_url=AMS, env=SENDER_ENV,
                                         http=http, tpu_sidecar=None, warm_external_http=True)
    assert [p.provider_id for p in transport._providers] == ["solana:custody_rpc", "solana:sender"]
    payer = Keypair()
    request = _request(_signed(payer, S.tip_instructions(payer.pubkey(), (S.TIP_ACCOUNTS[0], S.TIP_LAMPORTS))))
    result = transport.submit(request)
    for future in result.late_results:
        future.result(timeout=2.0)
    return result, request, http


def test_a_sender_accept_carries_the_send_when_custody_preflight_refuses():
    result, request, http = _sender_only(200)
    assert result.state is AttemptState.ACCEPTED and result.accepted_by == "solana:sender"
    assert sorted(http.sends) == sorted([(AMS, request.signed_payload), (S.SENDER_URL, request.signed_payload)])


def test_a_sender_429_is_ambiguous_never_a_refusal():
    result, _request_, _http = _sender_only(429)
    assert result.state is AttemptState.AMBIGUOUS                 # never a fabricated safe rejection
    sender = [a for a in result.attempts if a.provider_id == "solana:sender"]
    assert sender and sender[0].state is AttemptState.AMBIGUOUS and sender[0].response_code == "429"


def test_a_sender_only_accept_keeps_the_reservation_until_the_receipt(conn, tmp_path, monkeypatch):
    """Through the slot lane: the custody refusal never releases the reservation while Sender's
    accept stands; the executor waits for the receipt with the pending row still reserved."""
    from sniper.execution import solana_real_seams as seams
    from sniper.persistence import db_memecoin as m
    from test_memecoin_slot_lane_sender_ab import _production_lane
    http = _CustodyRefusesHttp(200)
    C.validate_submission_config(SENDER_ENV)
    transport, _ = B.build_solana_backend(keypair=Keypair(), custody_rpc_url=AMS, env=SENDER_ENV,
                                         http=http, tpu_sidecar=None, warm_external_http=True)
    ids = _ids()
    _force_ids(monkeypatch, [ids["B"]])
    ln, buy, _tpu, _sh = _production_lane(conn, tmp_path, monkeypatch, transport=transport)
    held = []

    def confirm(*_a, **_k):
        held.append([r[0] for r in conn.execute("SELECT status FROM memecoin_pending_entries")])
        return "failed"
    monkeypatch.setattr(seams, "rpc_confirm", confirm)
    result = ln.handle(buy("SigOnly"))
    assert held and held[0] == ["reserved"], held                # still ours while the receipt is read
    assert result["status"] == "failed" and any(url == S.SENDER_URL for url, _p in http.sends)
