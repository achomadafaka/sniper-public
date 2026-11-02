"""The deploy-time LI.FI quota probe retries a transport blip before refusing a release."""
from types import SimpleNamespace

from scripts.memecoin_lifi_activate import probe_quota_with_retry, quota_readable


def _res(ok, failure=None, remaining=11998, headers=True):
    quota = {"remaining": remaining, "limit": 12000, "reset": 7200} if headers else {"remaining": None, "limit": None, "reset": None}
    return SimpleNamespace(ok=ok, failure_type=failure, quota=quota)


def test_a_blip_then_ok_returns_the_ok_result_without_refusing():
    seq = iter([_res(False, "rpc_transport", headers=False), _res(True)])
    slept = []
    out = probe_quota_with_retry(lambda: next(seq), tries=3, sleep_s=2.0, sleep_fn=slept.append)
    assert out.ok and slept == [2.0]


def test_a_persistent_failure_returns_the_last_result_after_all_tries():
    calls = []

    def quote():
        calls.append(1)
        return _res(False, "provider_error", headers=False)
    slept = []
    out = probe_quota_with_retry(quote, tries=3, sleep_s=1.0, sleep_fn=slept.append)
    assert out.ok is False and out.failure_type == "provider_error"
    assert len(calls) == 3 and slept == [1.0, 1.0]


def test_an_ok_first_probe_never_sleeps():
    slept = []
    out = probe_quota_with_retry(lambda: _res(True), tries=3, sleep_fn=slept.append)
    assert out.ok and slept == []


def test_no_route_on_the_canary_token_with_quota_headers_is_a_readable_quota():
    """2026-09-22: the 09-01 canary token lost its pool; the reply still carried 11,959/12,000."""
    calls = []

    def quote():
        calls.append(1)
        return _res(False, "no_route", remaining=11959)
    slept = []
    out = probe_quota_with_retry(quote, tries=3, sleep_fn=slept.append)
    assert quota_readable(out) and out.quota["remaining"] == 11959
    assert len(calls) == 1 and slept == []


def test_a_reply_without_quota_headers_is_not_a_readable_quota():
    assert not quota_readable(_res(False, "timeout", headers=False))
    assert not quota_readable(SimpleNamespace(ok=False, failure_type="provider_error", quota={}))
    assert quota_readable(_res(True))
