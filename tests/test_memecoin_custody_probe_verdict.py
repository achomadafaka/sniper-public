"""The custody probe must say WHICH failure it hit, and must never print the endpoint url.

Both exits of `_probe_custody_with_retry` used to raise the identical string "custody EVM identity
validation failed". The probe returns a distinct verdict for each case -- ("wrong_chain", received),
("fail", "http_429"), ("fail", "transport"), ("fail", "no_result") -- and that information was
discarded at the boundary.

On 2026-09-01 that cost a funded deploy. A transient HTTP 429 on the 4663 custody RPC read as an
identity failure, and it was only ruled out by noticing the SAME error on a release that was already
running healthily. Telling a rate limit from a wrong chain should not require a second observation.

The url must never appear: a custody endpoint may carry an API key in its path or query, and this
string is logged and paged.
"""
import pytest

from sniper.execution import submission_backends as B
from sniper.settings.memecoin_submission import SubmissionConfigError


class _Endpoint:
    url = "https://rpc.example.com/v2/SUPERSECRETAPIKEY?token=alsosecret"


def _probe_returning(*verdicts):
    """A probe that yields each verdict in turn, then repeats the last one."""
    seq = list(verdicts)
    calls = {"n": 0}

    def probe(_http, _endpoint, timeout_s=None):
        v = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return v

    return probe, calls


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(B.time, "sleep", lambda _s: None)


def _run(probe):
    B._probe_custody_with_retry(probe, object(), _Endpoint(), 1.0, "custody EVM identity")


def test_rate_limit_is_named_and_distinguishable_from_wrong_chain():
    probe, calls = _probe_returning(("fail", "http_429"))
    with pytest.raises(SubmissionConfigError) as e:
        _run(probe)
    msg = str(e.value)
    assert "http_429" in msg, "an exhausted rate limit must name itself, not read as an identity fault"
    assert "wrong_chain" not in msg
    assert calls["n"] == B._CUSTODY_PROBE_ATTEMPTS, "a transient failure must use every attempt"


def test_wrong_chain_names_the_received_id_and_does_not_retry():
    probe, calls = _probe_returning(("wrong_chain", "0x2105"))
    with pytest.raises(SubmissionConfigError) as e:
        _run(probe)
    assert "wrong_chain" in str(e.value) and "0x2105" in str(e.value)
    assert calls["n"] == 1, "an identity mismatch is terminal and must reject on the first probe"


def test_the_two_failures_do_not_produce_the_same_message():
    """The regression itself: these were byte-identical strings."""
    msgs = []
    for verdict in (("fail", "http_429"), ("wrong_chain", "0x2105")):
        probe, _ = _probe_returning(verdict)
        with pytest.raises(SubmissionConfigError) as e:
            _run(probe)
        msgs.append(str(e.value))
    assert msgs[0] != msgs[1]


def test_transport_and_no_result_are_named_too():
    for reason in ("transport", "no_result"):
        probe, _ = _probe_returning(("fail", reason))
        with pytest.raises(SubmissionConfigError) as e:
            _run(probe)
        assert reason in str(e.value)


def test_ok_still_returns_and_does_not_raise():
    probe, calls = _probe_returning(("ok", "0x1237"))
    _run(probe)
    assert calls["n"] == 1


def test_a_late_ok_after_transient_failures_still_passes():
    """The retry must still be able to succeed -- this is diagnostic-only, not a behaviour change."""
    probe, calls = _probe_returning(("fail", "http_429"), ("ok", "0x1237"))
    _run(probe)
    assert calls["n"] == 2


def test_skip_is_refused_explicitly_rather_than_burning_every_retry():
    """health_method='none' means no identity check is configured. Unreachable for custody today,
    but without the branch it falls through and reads as an outage instead of a misconfiguration."""
    probe, calls = _probe_returning(("skip", ""))
    with pytest.raises(SubmissionConfigError) as e:
        _run(probe)
    assert "health_method" in str(e.value)
    assert calls["n"] == 1, "a missing identity check is a config fault, not a transient"


def test_the_url_never_reaches_the_message():
    """A custody url may carry an API key. The host identifies the endpoint; the url would leak."""
    for verdict in (("fail", "http_429"), ("wrong_chain", "0x2105"), ("skip", "")):
        probe, _ = _probe_returning(verdict)
        with pytest.raises(SubmissionConfigError) as e:
            _run(probe)
        msg = str(e.value)
        assert "SUPERSECRETAPIKEY" not in msg, "the API key leaked into a logged error string"
        assert "alsosecret" not in msg
        assert _Endpoint.url not in msg
