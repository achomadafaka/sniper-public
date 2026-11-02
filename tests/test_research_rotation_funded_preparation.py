"""Funded RH preparation starts on the working isolated dRPC read account."""
import json
import urllib.error

import pytest

from sniper.execution import research_rotation_4663 as rr

BASE = "https://base.robinhood-mainnet.quiknode.pro/k1/"
SPARE = "https://spare.robinhood-mainnet.quiknode.pro/k2/"
DRPC = "https://lb.drpc.org/ogrpc?network=robinhood&dkey=TEST"
OK = {"jsonrpc": "2.0", "id": 1, "result": "0x1"}
CHAIN = {"jsonrpc": "2.0", "id": 1, "result": "0x1237"}
QUOTA = {"jsonrpc": "2.0", "id": 1,
         "error": {"code": -32003, "message": "daily request limit reached"}}


@pytest.fixture
def preparation(monkeypatch):
    from sniper.services import memecoin_research_pacer as rp
    monkeypatch.setattr(rp.SharedPacer, "acquire", lambda self: None)
    now = [100000.0]
    env = {rr.BASE: BASE, rr.BASE + "_2": SPARE,
           "MEMECOIN_RESEARCH_DRPC_RPC_URL_4663": DRPC}
    return rr.Rotation(env, clock=lambda: now[0], log=lambda _: None,
                       drpc_fallback=True), now


def test_preparation_never_revisits_broken_quicknode_while_drpc_is_healthy(preparation):
    rot, now = preparation
    requests = []

    def read(target):
        requests.append((target, "data"))
        if target != DRPC:
            raise urllib.error.URLError("endpoint unavailable")
        return 200, OK

    def identity(target):
        requests.append((target, "identity"))
        return 200, CHAIN

    assert rot.send(BASE, read, identity=identity)[1] == OK
    now[0] += 601.0
    assert rot.send(BASE, read, identity=identity)[1] == OK
    assert requests == [(DRPC, "identity"), (DRPC, "data"), (DRPC, "data")]
    assert [s["slot"] for s in rot.stats()[1]] == [1, 2, 3]
    assert [s["calls"] for s in rot.stats()[1]] == [0, 0, 2]
    assert rot.is_research(BASE) and rot.is_research(SPARE)


def test_preferred_drpc_quota_preserves_fallback_exhaustion_and_utc_reset(preparation):
    rot, now = preparation
    sent = []
    assert rot.send(BASE, lambda target: sent.append(target) or (200, QUOTA),
                    identity=lambda target: (200, CHAIN))[1] == QUOTA
    assert sent == [DRPC, BASE, SPARE]
    assert rot.active() == (1, BASE)
    assert all(s["spent"] for s in rot.stats()[1])
    now[0] += 86400.0
    assert rot.active() == (3, DRPC)
    assert not any(s["spent"] for s in rot.stats()[1])


def test_preferred_drpc_transport_failure_falls_back_without_spending(preparation):
    rot, _now = preparation
    sent = []
    for _ in range(3):
        assert rot.send(BASE, lambda target: sent.append(target) or (
            None, {"error": {"transport": "URLError"}}),
            identity=lambda target: (200, CHAIN))[0] is None
    assert sent == [DRPC, DRPC, DRPC]
    assert rot.active() == (1, BASE)
    assert rot.stats()[1][2]["unusable"] and not rot.stats()[1][2]["spent"]
    assert rot.send(BASE, lambda target: (200, OK, target))[2] == BASE


def test_preferred_drpc_rate_limit_keeps_shared_pause_and_never_spends(preparation, monkeypatch):
    from sniper.services import memecoin_research_pacer as rp
    rot, _now = preparation
    pauses, sent = [], []
    monkeypatch.setattr(rp.SharedPacer, "pause", lambda self, seconds: pauses.append(seconds))
    rate_limit = {"jsonrpc": "2.0", "id": 1,
                  "error": {"code": 15, "message": "free plan rate limit"}}
    assert rot.send(BASE, lambda target: sent.append(target) or (429, rate_limit),
                    identity=lambda target: (200, CHAIN))[1] == rate_limit
    assert sent == [DRPC] and pauses == [60.0]
    assert rot.active() == (3, DRPC)
    assert not any(s["spent"] or s["transport"] for s in rot.stats()[1])


def test_expired_background_admission_never_reads_or_penalizes_preferred_drpc(preparation):
    from sniper.execution.evm_rpc_pool import RequestNotSent
    rot, _now = preparation
    with pytest.raises(RequestNotSent):
        rot.send(BASE, lambda target: pytest.fail("unadmitted physical read"),
                 identity=lambda target: pytest.fail("unadmitted identity read"),
                 admission_fn=lambda: False)
    assert rot.active() == (3, DRPC)
    assert not any(s["calls"] or s["spent"] or s["transport"] for s in rot.stats()[1])


@pytest.mark.parametrize("identity_result", [None, "0x38", "broken"])
def test_preferred_drpc_identity_failure_never_reads_data(preparation, identity_result):
    rot, _now = preparation
    with pytest.raises(OSError):
        rot.send(BASE, lambda target: pytest.fail("data sent before chain proof"),
                 identity=lambda target: (200, {"result": identity_result}))
    assert rot.stats()[1][0]["calls"] == 0
    assert rot.stats()[1][2]["calls"] == 1


def test_safety_seam_proves_preferred_drpc_identity_before_data(preparation):
    rot, _now = preparation
    sent = []

    def physical(method, target, headers=None, body=None, timeout=10):
        request = json.loads(body)
        sent.append((target, request["method"]))
        return 200, CHAIN if request["method"] == "eth_chainId" else OK, {}

    http = rot.http_seam(lambda: physical)
    assert http("POST", BASE, body=json.dumps({"jsonrpc": "2.0", "id": 1,
        "method": "eth_call", "params": [{"to": "0x01", "data": "0x18160ddd"}, "latest"]}))[1] == OK
    assert sent == [(DRPC, "eth_chainId"), (DRPC, "eth_call")]


def test_float_seam_proves_preferred_drpc_identity_before_data(preparation, monkeypatch):
    import io
    from collections import Counter
    from sniper.services.memecoin_rh_fast_decide import _research_rpc
    rot, _now = preparation
    sent = []

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def physical(request, timeout):
        method = json.loads(request.data)["method"]
        sent.append((request.full_url, method))
        return Response(json.dumps(CHAIN if method == "eth_chainId" else OK).encode())

    monkeypatch.setattr("urllib.request.urlopen", physical)
    call = _research_rpc(BASE, Counter(), spacing_s=0, rotation=rot)
    assert call("eth_gasPrice", []) == ("0x1", None)
    assert sent == [(DRPC, "eth_chainId"), (DRPC, "eth_gasPrice")]
