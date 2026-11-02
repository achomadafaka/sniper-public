"""Relay origin verify, Part 1 (spec docs/superpowers/specs/2026-10-07-relay-origin-overlap-design.md,
operator YES via Alpha 2026-10-07): a flat poll instead of 0.1 x 2^n inside the same ~8 s budget, a
per-process pacer on Relay /requests, and an early give-up when Relay has already answered, settled,
with a depositor that can never pass. Watcher worker threads only, never the signing path."""
import copy
import threading
import time

import pytest

from sniper.services import memecoin_relay_initiation as relay
from test_memecoin_relay_initiation import FRAME, UID, inputs

OTHER = "So1ana0ther111111111111111111111111111111111"


class Clock:
    """A fake clock this test owns: sleep and each Relay fetch advance it; nothing global is patched."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.t += s


def _malformed_other(depositor=OTHER, status="success", user=None):
    """Relay's answer, settled, naming `depositor`, with a field Relay indexes late still missing."""
    answer = copy.deepcopy(FRAME["relay"])
    req = answer["requests"][0]
    req["status"] = status
    req["protocol"]["deposit"]["origin"]["depositor"] = depositor
    req["user"] = depositor if user is None else user
    del req["data"]["outTxs"]                       # checked before the identity: malformed
    return answer


def _verifier(clock, answers, *, ready_at=None, poll_s=0.30, early=True, fetch_s=0.25, identity=None):
    """answers(t) -> the Relay /requests answer at fake time t."""
    calls = []

    def fetch(url, **kwargs):
        if "requests/" in url:
            calls.append(clock())
            clock.t += fetch_s
            return answers(clock())
        return FRAME["origin-sol"]
    v = relay.WatcherVerifier(lambda _: identity if identity is not None else FRAME["identity"],
                              chains=FRAME["chains"], fetch=fetch, clock=clock, sleep=clock.sleep,
                              poll_s=poll_s, requests_per_s=0, early_giveup=early)
    return v, calls


def _item():
    _r, _o, proof, destination = inputs()
    return {"row": {"source_user_id": UID, "buy": {"source_initiation": proof}}, "transaction": destination}


@pytest.mark.parametrize("ready_at", [0.66, 2.26, 3.97])
def test_a_flat_poll_answers_within_one_poll_and_one_fetch_of_relays_readiness(ready_at):
    clock = Clock()
    v, calls = _verifier(clock, lambda t: FRAME["relay"] if t >= ready_at else {"requests": []})
    result = v(_item())
    assert result["row"]["buy"]["source_initiation"]["proof_version"] == relay.PROOF_VERSION
    assert calls[-1] <= ready_at + 0.30 + 0.25 + 1e-9
    assert set(clock.sleeps) == {0.30}


def test_the_poll_stops_inside_the_same_eight_second_budget():
    clock = Clock()
    v, calls = _verifier(clock, lambda t: {"requests": []})
    with pytest.raises(relay.ProofError, match="relay_request_not_indexed"):
        v(_item())
    assert clock() <= relay.ORIGIN_VERIFY_BUDGET_S + 0.25 and len(calls) > 7


def test_poll_zero_is_todays_exponential_schedule():
    clock = Clock()
    v, calls = _verifier(clock, lambda t: {"requests": []}, poll_s=0)
    with pytest.raises(relay.ProofError, match="relay_request_not_indexed"):
        v(_item())
    assert clock.sleeps == [0.1, 0.2, 0.4, 0.8, 1.6, 3.2] and len(calls) == 7


def test_two_settled_answers_naming_the_same_unregistered_depositor_give_up():
    """Gamma LOW on 544f1b639: one answer could carry a provisional depositor Relay corrects a poll
    later, so the give-up needs the same depositor on two consecutive settled answers (+1 poll)."""
    clock = Clock()
    v, calls = _verifier(clock, lambda t: _malformed_other())
    with pytest.raises(relay.ProofError, match="origin_wallet_unverified") as err:
        v(_item())
    assert len(calls) == 2 and clock.sleeps == [0.30]
    notes = err.value.origin_attempts
    assert notes[0].startswith("malformed_origin_evidence/") and notes[-1].startswith("origin_wallet_unverified/")
    assert "early" in notes[-1] and OTHER[:12] in notes[-1]          # the depositor rides the line (Beta)


@pytest.mark.parametrize("case", ["registered_unknown_timing", "not_settled", "user_differs", "knob_off"])
def test_anything_short_of_a_provable_other_depositor_keeps_polling(case):
    clock = Clock()
    identity = None
    answer = _malformed_other()
    if case == "registered_unknown_timing":
        identity = [dict(FRAME["identity"][0], address=OTHER, status_at="2099-01-01T00:00:00+00:00")]
    elif case == "not_settled":
        answer = _malformed_other(status="pending")
    elif case == "user_differs":
        answer = _malformed_other(user="SomeoneE1se11111111111111111111111111111111")
    v, calls = _verifier(clock, lambda t: answer, identity=identity, early=case != "knob_off")
    with pytest.raises(relay.ProofError):
        v(_item())
    assert len(calls) > 1, case


def test_an_evm_origin_whose_depositor_is_not_the_beneficiary_gives_up_at_once():
    clock = Clock()
    answer = _malformed_other(depositor="0x" + "ab" * 20)
    answer["requests"][0]["protocol"]["deposit"]["origin"]["chainId"] = 8453
    answer["requests"][0]["protocol"]["deposit"]["origin"]["transactionId"] = "0x" + "77" * 32
    calls = []

    def fetch(url, **kwargs):
        if "requests/" in url:
            calls.append(url)
            return answer
        return [{"id": i, "result": {"hash": "0x"}} for i in range(3)]       # the origin batch
    v = relay.WatcherVerifier(lambda _: FRAME["identity"], chains=FRAME["chains"], fetch=fetch,
                              evm_rpcs={"8453": "http://origin.invalid"}, clock=clock, sleep=clock.sleep,
                              poll_s=0.30, requests_per_s=0, early_giveup=True)
    with pytest.raises(relay.ProofError, match="origin_wallet_unverified") as err:
        v(_item())
    assert len(calls) == 2 and "early" in err.value.origin_attempts[-1]


def test_two_workers_on_one_pacer_are_spaced_by_its_rate():
    """Alpha: two workers sharing the process pacer start their Relay calls 0.5 s apart at 2/s."""
    pacer = relay.RelayRequestPacer(2.0)
    starts, lock = [], threading.Lock()

    def worker():
        for _ in range(2):
            pacer.wait()
            with lock:
                starts.append(time.monotonic())
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    starts.sort()
    assert len(starts) == 4
    assert all(b - a >= 0.49 for a, b in zip(starts, starts[1:])), starts


def test_a_zero_rate_pacer_never_waits():
    pacer = relay.RelayRequestPacer(0)
    t0 = time.monotonic()
    for _ in range(50):
        pacer.wait()
    assert time.monotonic() - t0 < 0.05


def test_a_depositor_that_changes_between_polls_never_gives_up_early():
    """A then B: no two consecutive answers agree, so it keeps polling to the budget; a third answer
    that then verifies passes."""
    clock = Clock()
    seq = iter([_malformed_other(depositor=OTHER), _malformed_other(depositor="Another1111111111111111111111111111111111111")])

    def answers(t):
        return next(seq, FRAME["relay"])
    v, calls = _verifier(clock, answers)
    result = v(_item())
    assert result["row"]["buy"]["source_initiation"]["proof_version"] == relay.PROOF_VERSION
    assert len(calls) == 3


def test_a_depositor_seen_once_then_absent_starts_over():
    clock = Clock()
    seq = iter([_malformed_other(), {"requests": []}, _malformed_other()])
    v, calls = _verifier(clock, lambda t: next(seq, FRAME["relay"]))
    result = v(_item())
    assert result["row"]["buy"]["source_initiation"]["proof_version"] == relay.PROOF_VERSION
    assert len(calls) == 4
