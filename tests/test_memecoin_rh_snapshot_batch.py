"""PAPA: four independently paced reads made a fresh wallet snapshot expire between polls."""
import collections
import io
import json

import pytest

from sniper.execution import research_rotation_4663 as rotation
from sniper.services import memecoin_research_pacer as pacer
from sniper.services import memecoin_rh_fast_decide as fast


URL = "https://lb.drpc.org/ogrpc?network=robinhood&dkey=TEST"
OWN = "0x" + "11" * 20
WETH = "0x" + "22" * 20


def client(monkeypatch, reply_fn, *, paced_wait=0, status=200):
    now, requests, grants, pauses = [1000.0], [], [], []
    monkeypatch.setattr(pacer.SharedPacer, "acquire",
                        lambda self: (grants.append(self.path), now.__setitem__(0, now[0] + paced_wait)))
    monkeypatch.setattr(pacer.SharedPacer, "pause", lambda self, seconds: pauses.append(seconds))
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    monkeypatch.setattr("time.sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    class Response(io.BytesIO):
        pass

    def post(req, **kwargs):
        payload = json.loads(req.data)
        requests.append(payload)
        response = Response(json.dumps(reply_fn(payload)).encode())
        response.status = status
        return response

    monkeypatch.setattr("urllib.request.urlopen", post)
    rpc = fast._research_rpc(URL, collections.Counter(), spacing_s=0,
                             rotation=rotation.Rotation({rotation.BASE: URL}, log=lambda _: None))
    return rpc, now, requests, grants, pauses


def good(payload):
    if isinstance(payload, list):
        return [{"jsonrpc": "2.0", "id": p["id"],
                 "result": "0x1" if p.get("method") == "eth_gasPrice" else "0x0"}
                for p in reversed(payload)]
    return {"jsonrpc": "2.0", "id": payload["id"], "result": "0x1"}


def test_snapshot_uses_two_paced_posts_not_four_and_keeps_poll_start(monkeypatch):
    rpc, now, requests, grants, _ = client(monkeypatch, good, paced_wait=8)
    snap, error = fast.poll_snapshot(rpc, OWN, WETH, clock=lambda: now[0])
    assert error is None and snap == dict(native_wei=0, weth_wei=0, nonce=0,
                                          gas_price_wei=1, read_at=1000.0)
    assert len(requests) == len(grants) == 2
    assert now[0] - snap["read_at"] == 16  # batch completion never resets poll-start freshness
    assert [p["method"] for p in requests[0]] == ["eth_getBalance", "eth_call", "eth_getTransactionCount"]
    assert len(requests[0]) <= 3  # dRPC free tier's documented batch limit
    assert requests[0][2]["params"] == [OWN, "pending"]
    assert requests[1][0]["method"] == "eth_gasPrice"


@pytest.mark.parametrize("bad", [
    [], [{"id": 1, "result": "0x1"}],
    [{"id": 1, "result": "0x1"}] * 3,
    [{"id": True, "result": "0x1"}, {"id": 2, "result": "0x2"}, {"id": 3, "result": "0x3"}],
    [{"id": 1, "result": "0x1"}, {"id": 2, "result": "0x2"}, {"id": 4, "result": "0x3"}],
    [{"id": 1, "result": "0x1"}, {"id": 2, "result": "0x2"}, {"id": 3, "result": "0x"}],
    [{"id": 1, "result": "0x1"}, {"id": 2, "result": "0x2"}, {"id": 3, "result": "0x-1"}],
    [{"id": 1, "result": "0x1"}, {"id": 2, "result": "0x2"}, {"id": 3, "result": "1"}],
    [{"id": 1, "result": "0x1"}, {"id": 2, "result": "0x2"}, {"id": 3, "result": False}],
    [{"id": 1, "result": "0x1"}, {"id": 2, "result": "0x2"}, {"id": 3, "error": {"code": -32000}}],
    [None, {"id": 2, "result": "0x2"}, {"id": 3, "result": "0x3"}],
])
def test_partial_wrong_identity_or_malformed_batch_never_publishes_snapshot(monkeypatch, bad):
    rpc, _, requests, _, _ = client(monkeypatch, lambda payload: bad if isinstance(payload, list) else good(payload))
    assert fast.poll_snapshot(rpc, OWN, WETH) == (None, "error")
    assert len(requests) == 1  # do not ask for gas after a failed snapshot batch


def test_batch_rate_limit_pauses_the_shared_account_and_snapshot_backoff(monkeypatch):
    limited = [{"id": 1, "result": "0x0"}, {"id": 2, "error": {"code": 15, "message": "rate limit"}},
               {"id": 3, "result": "0x0"}]
    rpc, _, requests, _, pauses = client(monkeypatch, lambda payload: limited if isinstance(payload, list) else good(payload))
    assert fast.poll_snapshot(rpc, OWN, WETH) == (None, "rate_limit")
    assert len(requests) == 1 and pauses == [60.0]


@pytest.mark.parametrize("reply", [[{"id": 1, "result": "0x1"}],
                                  [{"id": 1, "error": {"code": 3, "message": "execution reverted"}}]])
def test_answered_batch_is_not_a_transport_failure_or_fabricated_provider_error(reply):
    rot = rotation.Rotation({rotation.BASE: URL}, log=lambda _: None)
    assert rot._observe((200, reply), allow_batch=True)[1] is reply
    assert rot.provider_errors() == 0


def test_explicit_batch_quota_rotates_the_same_read_body(monkeypatch):
    base, spare = "https://research-one.example", "https://research-two.example"
    rot = rotation.Rotation({rotation.BASE: base, rotation.BASE + "_2": spare}, log=lambda _: None)
    sent = []
    quota = [{"id": 1, "error": {"code": -32003, "message": "daily request limit reached"}}]
    out = rot.send(base, lambda target: sent.append(target) or (200, quota if target == base else good([{"id": 1}])),
                   allow_batch=True)
    assert sent == [base, spare] and out[1][0]["result"] == "0x0"


@pytest.mark.parametrize("status,reason", [(500, "error"), (429, "rate_limit"), (403, "error")])
def test_http_failure_cannot_publish_an_otherwise_valid_batch(monkeypatch, status, reason):
    rpc, _, requests, _, _ = client(monkeypatch, good, status=status)
    assert fast.poll_snapshot(rpc, OWN, WETH) == (None, reason)
    assert len(requests) == 1


def test_empty_error_object_is_unknown_even_with_a_quantity(monkeypatch):
    def reply(payload):
        rows = good(payload)
        rows[0]["error"] = {}
        return rows
    rpc, _, requests, _, _ = client(monkeypatch, reply)
    assert fast.poll_snapshot(rpc, OWN, WETH) == (None, "error")
    assert len(requests) == 1
