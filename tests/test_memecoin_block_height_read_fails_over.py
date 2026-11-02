"""The dead-signature proof's height read must not depend on one rate-limited endpoint.

2026-09-09: 1,102 of 1,233 HTTP 429s in two hours were `block_height_fn`, called every sweep by
`sig_provably_dead` for a pinned close on fries (55293). The custody endpoint was rate-limited, the
proof threw, the claim stayed pinned, and the next sweep asked the same endpoint again -- the proof
was the hammer. The close had provably expired 5,000 blocks earlier.
"""
import urllib.error

from sniper.services import memecoin_real_runner as R


def _ok(height):
    return lambda method, url, headers=None, body=None, **_k: (200, {"result": height}, None)


def _429(method, url, headers=None, body=None, **_k):
    raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)


def test_custody_answers_and_is_used():
    assert R.read_block_height("https://custody", _ok(423748817), read_url="https://read") == 423748817


def test_a_rate_limited_custody_falls_over_to_the_read_endpoint(monkeypatch):
    calls = []
    orig = R.__dict__.get("read_block_height")
    from sniper.execution import evm_wallet as W
    real_rpc = W._rpc

    def fake_rpc(url, method, params, http=None):
        calls.append(url)
        if url == "https://custody":
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
        return {"result": 423748817}
    monkeypatch.setattr(W, "_rpc", fake_rpc)
    logs = []
    assert R.read_block_height("https://custody", _429, read_url="https://read", log_fn=logs.append) == 423748817
    assert calls == ["https://custody", "https://read"]
    assert any("read endpoint" in l for l in logs)


def test_every_endpoint_failing_stays_fail_closed(monkeypatch):
    from sniper.execution import evm_wallet as W
    monkeypatch.setattr(W, "_rpc", lambda url, method, params, http=None: (_ for _ in ()).throw(urllib.error.HTTPError(url, 429, "x", {}, None)))
    assert R.read_block_height("https://custody", _429, read_url="https://read") is None


def test_a_boolean_or_garbage_result_is_not_a_height(monkeypatch):
    from sniper.execution import evm_wallet as W
    monkeypatch.setattr(W, "_rpc", lambda url, method, params, http=None: {"result": True})
    assert R.read_block_height("https://custody", None, read_url="") is None
