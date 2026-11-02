"""Re-broadcasting a pinned exit: it must be the SAME transaction, never a new one.

8 exits entered `submitted_unknown` over 7 days against 448 confirmed, and stayed unresolved 0-30
minutes. Position 48595 spent 17-30 minutes pinned while the price moved. The hold is correct --
an ambiguous sell may land, and a second one sells the bag twice -- so the only safe acceleration
is re-offering the identical bytes.
"""
import base64
import json

import pytest

from sniper.services import memecoin_exit_rebroadcast as R


def _signed_raw(chain_id, nonce=1):
    """A real signed transaction: the EVM-08 intent fence never sends bytes without a chain id."""
    from eth_account import Account
    tx = {"type": 2, "chainId": chain_id, "nonce": nonce, "maxFeePerGas": 10,
          "maxPriorityFeePerGas": 0, "gas": 21000, "to": "0x" + "22" * 20, "value": 0, "data": b""}
    return "0x" + bytes(Account.sign_transaction(tx, b"\x03" * 32).raw_transaction).hex()

RAW = b"\xf9\x08\x4e\x82\x04\xfb"
INTENT = {"tx_identity": "0xb569871d", "signed_payload_b64": base64.b64encode(RAW).decode(),
          "nonce": 1275}
NOW = "2026-09-02T19:55:00+00:00"
OLD = "2026-09-02T19:51:21+00:00"       # 3m39s earlier


@pytest.fixture(autouse=True)
def _clean():
    R.reset_state()
    yield
    R.reset_state()


def _at(seconds):
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(NOW) + timedelta(seconds=seconds)).isoformat()


def _run(intent=None, submitted=OLD, receipt=None, sent=True, at=0, **kw):
    """`at`: seconds after NOW. Copies of one submission are MIN_GAP_S apart (2026-09-30)."""
    seen = []
    got = R.rebroadcast_pinned_exit(
        json.dumps(intent if intent is not None else INTENT), submitted, now_iso=_at(at),
        receipt_fn=lambda h: receipt,
        send_fn=lambda raw: seen.append(raw) or sent, **kw)
    return got, seen


def test_it_resends_the_EXACT_stored_bytes():
    """The entire safety argument: identical bytes are the identical transaction with the
    identical nonce, so the chain can include it at most once."""
    got, seen = _run()
    assert got == "resent"
    assert seen == [RAW], "a rebroadcast that alters the payload is a DIFFERENT transaction"


def test_it_refuses_when_the_chain_already_has_an_answer():
    """Confirmed or reverted, the recovery owns it. Re-sending would be noise at best."""
    got, seen = _run(receipt={"status": "0x0"})
    assert got == "receipt_present" and seen == []


def test_an_unreadable_receipt_does_NOT_resend():
    """Unknown chain state is never a reason to put a transaction back on the wire."""
    def boom(h):
        raise RuntimeError("rpc down")
    got = R.rebroadcast_pinned_exit(json.dumps(INTENT), OLD, now_iso=NOW,
                                    receipt_fn=boom, send_fn=lambda raw: True)
    assert got == "receipt_unreadable"


def test_it_waits_before_acting():
    """Inside the window a pinned sell often simply confirms. Re-sending immediately would make
    every slow confirmation look like a stuck one."""
    got, seen = _run(submitted="2026-09-02T19:54:55+00:00")     # 5s old
    assert got == "too_young" and seen == []


def test_the_wait_is_configurable():
    got, _ = _run(submitted="2026-09-02T19:54:55+00:00", min_age_s=1.0)
    assert got == "resent"


def test_it_never_synthesises_a_payload():
    """No stored bytes means no rebroadcast. Signing a fresh one here would create a SECOND
    transaction that can land alongside the first."""
    got, seen = _run(intent={"tx_identity": "0xb569871d"})
    assert got == "no_signed_payload" and seen == []


def test_a_missing_tx_identity_also_refuses():
    got, seen = _run(intent={"signed_payload_b64": base64.b64encode(RAW).decode()})
    assert got == "no_signed_payload" and seen == []


def test_a_rejected_send_is_reported_not_raised():
    got, _ = _run(sent=False)
    assert got == "send_rejected"


def test_a_raising_send_never_escapes():
    """This runs on the funded path. Nothing here may break an exit sweep."""
    got = R.rebroadcast_pinned_exit(
        json.dumps(INTENT), OLD, now_iso=NOW, receipt_fn=lambda h: None,
        send_fn=lambda raw: (_ for _ in ()).throw(RuntimeError("boom")))
    assert got == "send_raised"


@pytest.mark.parametrize("bad", ["not json", "", None])
def test_an_unreadable_intent_refuses(bad):
    got = R.rebroadcast_pinned_exit(bad, OLD, now_iso=NOW,
                                    receipt_fn=lambda h: None, send_fn=lambda raw: True)
    assert got in ("unreadable_intent", "no_signed_payload")


def test_an_unreadable_timestamp_refuses():
    got, seen = _run(submitted="whenever")
    assert got == "unreadable_timestamp" and seen == []


def test_the_counters_separate_the_outcomes():
    """A rebroadcast that never fires and one that fires and is rejected are different facts."""
    _run()
    _run(receipt={"status": "0x1"}, at=15)
    _run(sent=False, at=15)
    s = R.snapshot()
    assert s["resent"] == 1 and s["receipt_present"] == 1 and s["send_failed"] == 1


# --- the bound (Alpha's condition 3) -------------------------------------------------------------
#
# Idempotent is not free. Three bags pinned on 2026-09-02 were never lost in transit: a node
# refuses a transaction when gasLimit * gasPrice exceeds the wallet balance whatever the swap
# consumes -- 900,000 * 0.6628 gwei = 0.00059648 ETH against a wallet holding 0.00051674 -- so
# every sell was rejected BEFORE the mempool. Re-offering identical bytes to a node that refused
# them for insufficient funds earns the identical refusal, forever.


def test_re_offers_are_bounded_per_submission():
    for i in range(R.MAX_REOFFERS):
        assert _run(at=15 * i)[0] == "resent"
    got, seen = _run(at=60)
    assert got == "reoffers_exhausted" and seen == [], (
        "an unbounded retry against a node that keeps refusing is the 08-21 quota finding again")


def test_the_bound_is_per_TRANSACTION_not_global():
    """A second pinned position must not inherit the first one's exhausted budget."""
    for i in range(R.MAX_REOFFERS):
        _run(at=15 * i)
    other = dict(INTENT, tx_identity="0xdifferent")
    got, seen = _run(intent=other)
    assert got == "resent" and seen, "the bound leaked across submissions"


def test_an_exhausted_submission_is_COUNTED_not_silent():
    for i in range(R.MAX_REOFFERS + 2):
        _run(at=15 * i)
    s = R.snapshot()
    assert s["exhausted"] == 2 and s["resent"] == R.MAX_REOFFERS


def test_a_rejected_send_still_consumes_an_attempt():
    """The gas case rejects every time. If a refusal did not count, the bound would never bind."""
    for i in range(R.MAX_REOFFERS):
        assert _run(sent=False, at=15 * i)[0] == "send_rejected"
    assert _run(sent=False, at=60)[0] == "reoffers_exhausted"


def test_the_snapshot_exposes_how_many_submissions_are_mid_retry():
    _run()
    assert R.snapshot()["tracked"] == 1


# --- the wiring, and Alpha's condition 4 --------------------------------------------------------


def test_it_is_wired_into_the_recovery_sweep():
    import inspect

    from sniper.services import memecoin_real_runner as RR
    src = inspect.getsource(RR.recover_unverified_closes)
    assert "rebroadcast_pinned_exit" in src, "the resolver is built but nothing calls it"


def test_it_only_fires_when_the_chain_has_NOT_seen_the_tx():
    import inspect

    from sniper.services import memecoin_real_runner as RR
    src = inspect.getsource(RR.recover_unverified_closes)
    i = src.index("rebroadcast_pinned_exit")
    window = src[max(0, i - 1200):i]
    assert 'state == "not_found"' in window, (
        "a re-offer must be gated on the chain never having seen the transaction")


def test_it_runs_AFTER_the_provably_dead_release():
    """A transaction we can prove will never land should be released, not re-offered."""
    import inspect

    from sniper.services import memecoin_real_runner as RR
    src = inspect.getsource(RR.recover_unverified_closes)
    assert src.index("release_dropped_close_claim") < src.index("rebroadcast_pinned_exit")


def test_submitted_unknown_remains_reachable():
    """Alpha's condition 4: if a bag can no longer ARRIVE at submitted_unknown, every recovery
    path built tonight goes blind and we would not know."""
    import pathlib
    src = pathlib.Path("sniper/execution/evm_exit_executor.py").read_text()
    assert '"submitted_unknown"' in src, (
        "the state the recovery keys on must still be reachable from the executor")


def test_the_re_offer_can_never_break_the_sweep():
    import inspect

    from sniper.services import memecoin_real_runner as RR
    src = inspect.getsource(RR.recover_unverified_closes)
    i = src.index("rebroadcast_pinned_exit")
    window = src[max(0, i - 400):i + 900]
    assert "try:" in window and "except Exception:" in window


# --- spacing, the detached entry point, the refusal line (Alpha and Gamma, 2026-09-30) -----------


def _young(age_s, **kw):
    from datetime import datetime, timedelta
    submitted = (datetime.fromisoformat(NOW) - timedelta(seconds=age_s)).isoformat()
    return _run(submitted=submitted, **kw)


def test_the_two_copies_go_at_twenty_and_thirty_five_seconds():
    assert _young(19)[0] == "too_young"
    assert _young(20)[0] == "resent"
    assert _young(34, at=14)[0] == "not_due"                    # copy 2 waits for 35 s of age
    assert _young(20, at=15)[0] == "resent"                     # the same sell, 15 s later
    assert _young(20, at=30)[0] == "reoffers_exhausted"         # no third copy near the release
    assert R.MAX_REOFFERS == 2 and R.FIRST_AT_S + R.MIN_GAP_S == 35.0
    s = R.snapshot()
    assert (s["resent"], s["too_young"], s["not_due"], s["exhausted"]) == (2, 1, 1, 1)


def test_a_sell_first_seen_old_still_gets_its_copies_fifteen_seconds_apart():
    """After a restart the count is gone and the sell may be minutes old: no burst of two."""
    assert _run()[0] == "resent"                                # 3m39s old
    assert _run(at=5)[0] == "not_due"
    assert _run(at=15)[0] == "resent"


def test_a_refused_copy_logs_one_line_with_the_nodes_words(caplog):
    send = R.stored_payload_sender(lambda text: False, reason_fn=lambda: "nonce too low")
    payload = base64.b64encode(b"0xabcdef").decode()
    with caplog.at_level("WARNING"):
        got = R.rebroadcast_pinned_exit(json.dumps(dict(INTENT, signed_payload_b64=payload)), OLD,
                                        now_iso=NOW, receipt_fn=lambda h: None, send_fn=send)
    lines = [r.getMessage() for r in caplog.records if "re-offer refused" in r.getMessage()]
    assert got == "send_rejected" and len(lines) == 1
    assert "tx=0xb569871d" in lines[0] and "attempt=1/2" in lines[0] and "nonce too low" in lines[0]


def _detached(monkeypatch, jobs=None, **kw):
    monkeypatch.setattr(R, "_spawn", (jobs.append if jobs is not None else (lambda job: job())))
    seen = []
    got = R.rebroadcast_pinned_exit_detached(
        json.dumps(INTENT), kw.pop("submitted", OLD), now_iso=kw.pop("now", NOW),
        receipt_fn=kw.pop("receipt_fn", lambda h: None),
        send_fn=lambda raw: seen.append(raw) or True)
    return got, seen


def test_the_detached_entry_point_does_no_io_on_the_callers_thread(monkeypatch):
    jobs, touched = [], []
    got, seen = _detached(monkeypatch, jobs, receipt_fn=lambda h: touched.append(h))
    assert got == "dispatched" and len(jobs) == 1
    assert touched == [] and seen == [], "the receipt read and the send belong to the thread"
    jobs[0]()
    assert touched == ["0xb569871d"] and seen == [RAW]
    assert R.snapshot()["resent"] == 1 and R.snapshot()["in_flight_now"] == 0


def test_a_sweep_that_finds_a_re_offer_still_running_starts_nothing(monkeypatch):
    jobs = []
    assert _detached(monkeypatch, jobs)[0] == "dispatched"
    assert _detached(monkeypatch, jobs)[0] == "in_flight" and len(jobs) == 1
    jobs[0]()
    assert _detached(monkeypatch, jobs, now=_at(15))[0] == "dispatched"


def test_the_detached_entry_point_refuses_without_starting_a_thread(monkeypatch):
    jobs = []
    assert _detached(monkeypatch, jobs, submitted=_at(-5))[0] == "too_young"
    for i in range(R.MAX_REOFFERS):
        _run(at=15 * i)
    assert _detached(monkeypatch, jobs, now=_at(60))[0] == "reoffers_exhausted"
    assert jobs == []


def test_the_capped_transport_never_waits_longer_than_the_re_offers_timeout():
    seen = []
    capped = R.short_timeout_http(lambda method, url, **kw: seen.append(kw["timeout"]))
    capped("POST", "u", body="{}")
    capped("POST", "u", body="{}", timeout=10)
    capped("POST", "u", body="{}", timeout=1)
    assert seen == [R.SEND_TIMEOUT_S, R.SEND_TIMEOUT_S, 1.0] and R.SEND_TIMEOUT_S < 3.0
    assert capped.request_remaining_s() == float("inf")         # no copy on this thread


def test_a_request_that_could_outlast_the_copys_deadline_is_never_started(monkeypatch):
    """One absolute deadline for the whole copy. The transport refuses BEFORE the request, with
    the error the RPC pool reads as "never sent": no rotation, no quarantine. A started request
    keeps its full timeout, so a member is never blamed for a timeout we cut short."""
    from sniper.execution.evm_rpc_pool import RequestNotSent
    clock, seen = [100.0], []
    monkeypatch.setattr(R, "_monotonic", lambda: clock[0])
    capped = R.short_timeout_http(lambda method, url, **kw: seen.append(kw["timeout"]) or "sent")
    monkeypatch.setattr(R._COPY, "deadline", 108.0, raising=False)
    assert capped("POST", "u") == "sent" and capped.request_remaining_s() == 8.0
    clock[0] = 106.0                                            # exactly one timeout left
    assert capped("POST", "u") == "sent"
    clock[0] = 106.01
    with pytest.raises(RequestNotSent):
        capped("POST", "u")
    clock[0] = 120.0
    with pytest.raises(RequestNotSent):
        capped("POST", "u")
    assert seen == [R.SEND_TIMEOUT_S] * 2                       # never a shortened timeout
    assert not issubclass(RequestNotSent, (TimeoutError, OSError))


def test_the_deadline_belongs_to_the_copys_thread_and_ends_with_it(monkeypatch):
    seen = []
    monkeypatch.setattr(R, "_spawn", lambda job: job())
    monkeypatch.setattr(R, "_wall", lambda: R._epoch(NOW))
    young = _at(-30)

    def send(raw):
        seen.append(R._copy_remaining_s())
        return True
    got = R.rebroadcast_pinned_exit_detached(
        json.dumps(INTENT), young, now_iso=NOW, receipt_fn=lambda h: None, send_fn=send,
        release_age_s=60)
    assert got == "dispatched" and 7.9 < seen[0] <= 8.0
    assert R._copy_remaining_s() == float("inf")                # cleared when the copy ends
    R.reset_state()
    R.rebroadcast_pinned_exit_detached(                         # no release age: no deadline
        json.dumps(INTENT), young, now_iso=NOW, receipt_fn=lambda h: None, send_fn=send)
    assert seen[1] == float("inf")


def test_a_thread_that_cannot_start_does_not_leave_the_sell_in_flight(monkeypatch):
    """Gamma LOW 2 on d2ae84678: the identity is cleared, so the next sweep can dispatch."""
    def no_threads(job):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(R, "_spawn", no_threads)
    got = R.rebroadcast_pinned_exit_detached(json.dumps(INTENT), OLD, now_iso=NOW,
                                             receipt_fn=lambda h: None, send_fn=lambda raw: True)
    assert got == "spawn_failed" and R.snapshot()["in_flight_now"] == 0
    assert _detached(monkeypatch)[0] == "dispatched"


def _dispatch(monkeypatch, jobs, sent, age_s, real_age_s=None, release_age_s=60):
    """A sell `age_s` old by the sweep's stamp and `real_age_s` old by the real clock."""
    monkeypatch.setattr(R, "_spawn", jobs.append)
    monkeypatch.setattr(R, "_wall", lambda: R._epoch(NOW) - age_s + (
        age_s if real_age_s is None else real_age_s))
    return R.rebroadcast_pinned_exit_detached(
        json.dumps(INTENT), _at(-age_s), now_iso=NOW, receipt_fn=lambda h: None,
        send_fn=lambda raw: sent.append(raw) or True, release_age_s=release_age_s)


def test_no_copy_starts_without_room_for_a_read_and_a_send_before_the_last_send_time(monkeypatch):
    jobs, sent = [], []
    assert R.MIN_COPY_BUDGET_S == R.RECEIPT_TIMEOUT_S + R.SEND_TIMEOUT_S == 4.0
    assert _dispatch(monkeypatch, jobs, sent, 39.01) == "too_late" and jobs == []
    assert _dispatch(monkeypatch, jobs, sent, 30, real_age_s=39.01) == "too_late" and jobs == []
    assert R.snapshot()["too_late"] == 2 and R.snapshot()["tracked"] == 0   # no attempt used
    assert _dispatch(monkeypatch, jobs, sent, 39.0) == "dispatched" and len(jobs) == 1
    assert _dispatch(monkeypatch, [], sent, 300, release_age_s=None) == "in_flight"


def test_a_copy_whose_receipt_read_left_no_time_for_a_send_is_dropped(monkeypatch):
    jobs, sent = [], []
    clock = [500.0]
    monkeypatch.setattr(R, "_monotonic", lambda: clock[0])      # the module's own clock name
    assert _dispatch(monkeypatch, jobs, sent, 30) == "dispatched"   # deadline 508
    clock[0] = 506.01                                           # the read took 6.01 s
    jobs[0]()
    snap = R.snapshot()
    assert sent == [] and snap["late"] == 1 and snap["tracked"] == 1 and snap["in_flight_now"] == 0
    R.reset_state()
    clock[0] = 500.0
    assert _dispatch(monkeypatch, jobs, sent, 30) == "dispatched"
    clock[0] = 506.0                                            # exactly one send's timeout left
    jobs[1]()
    assert sent == [RAW] and R.snapshot()["late"] == 0


def test_a_release_asks_whether_a_copy_is_in_flight_and_the_answer_is_counted(monkeypatch):
    jobs, sent = [], []
    assert R.copy_in_flight("0xb569871d") is False and R.copy_in_flight(None) is False
    assert _dispatch(monkeypatch, jobs, sent, 30) == "dispatched"
    assert R.copy_in_flight("0xB569871D") is True               # a hash compares whatever its case
    assert R.copy_in_flight("0xother") is False
    jobs[0]()
    assert R.copy_in_flight("0xb569871d") is False
    assert R.snapshot()["release_deferred"] == 1


def test_a_copy_thread_that_never_returns_cannot_hold_a_claim_for_ever(monkeypatch):
    """Past the copy's deadline none of its requests can start; one send timeout later none is
    in the air. The release is safe from there, whatever the thread is doing."""
    jobs, sent, clock = [], [], [500.0]
    monkeypatch.setattr(R, "_monotonic", lambda: clock[0])
    assert _dispatch(monkeypatch, jobs, sent, 30) == "dispatched"   # deadline 508; the job never runs
    clock[0] = 508.0 + R.SEND_TIMEOUT_S
    assert R.copy_in_flight("0xb569871d") is True
    clock[0] += 0.01
    assert R.copy_in_flight("0xb569871d") is False
    assert R.snapshot()["in_flight_now"] == 1                   # still counted as running


def test_a_send_the_gate_refuses_never_blames_a_verified_pool_member(monkeypatch):
    """Gamma LOW on 8868b1403. The common path: every member's identity is already proven, so
    `choose` probes nothing and the gate refuses the SEND itself inside `_rpc_impl`'s rotation.
    That refusal is ours: no request left, so no member is penalized, quarantined or charged a
    failure, and the rotation stops instead of trying the next member."""
    from sniper.execution import evm_rpc_pool as pool
    from sniper.execution import evm_real_seams as seams
    from sniper.settings import memecoin as cfg
    members = ("https://rpc-4663", "https://pool-2", "https://pool-3")
    pool.reset_for_tests()
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"4663": members})
    for url in members:
        pool.mark_identity(url, True)
    seen = []
    capped = R.short_timeout_http(
        lambda method, url, **kw: seen.append(url) or (200, {"result": "0x" + "ab" * 32}, None))
    monkeypatch.setattr(R._COPY, "deadline", R._monotonic() + 1.0, raising=False)  # < a send's timeout
    try:
        assert seams.eth_send_raw(_signed_raw(4663), members[0], http=capped) is None     # ambiguous, unsent
        assert seen == []
        for row in pool.snapshot(members):
            assert (row["fail"], row["penalties"], row["quarantined_s"]) == (0, 0, 0), row
        assert all(pool.available(url) for url in members)
    finally:
        pool.reset_for_tests()
