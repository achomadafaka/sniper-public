"""Research Helius rotation, per-key usage, the credits page, and the funded-key guard."""
import hashlib
import io
import json
import urllib.error

import pytest

from sniper.execution.solana_rpc_pool import RotatingSolanaRpcHttp, key_id
from sniper.services import memecoin_research_rpc as rr

K = ["https://mainnet.helius-rpc.com/?api-key=k%d" % i for i in range(3)]


def _sha(key):
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def _spent(url):
    return urllib.error.HTTPError(url, 429, "Too Many Requests", {},
                                  io.BytesIO(b'{"error":"max usage reached"}'))


def _http(spent, seen):
    def http(method, url, headers=None, body=None, timeout=None):
        seen.append((url, json.loads(body)["method"], (headers or {}).get("user-agent")))
        if url in spent:
            raise _spent(url)
        return 200, {"jsonrpc": "2.0", "id": 1, "result": "ok@" + url[-2:]}, None
    return http


@pytest.fixture
def state(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    return tmp_path


def test_key_id_is_the_api_key_hash_never_the_url():
    assert key_id(K[0]) == _sha("k0")
    assert key_id("https://api.mainnet-beta.solana.com") == "api.mainnet-beta.solana.com"


def test_spent_key_rotates_reports_state_and_pages_once_per_day(state):
    sent, seen = [], []
    env = {"MEMECOIN_RESEARCH_HELIUS_RPC_URLS": ",".join(K)}
    call, pool = rr.build_research_call("pool_prewarm", min_call_s=0.001, send_fn=sent.append,
                                        http=_http({K[0]}, seen), env=env)
    assert [k["state"] for k in pool.key_status()] == ["unknown"] * 3
    assert call("getTransaction", ["sig"]) == "ok@k1"
    # same body the single-key path sent, same user agent, and the spent key was tried first
    assert [(u, m) for u, m, _ in seen] == [(K[0], "getTransaction"), (K[1], "getTransaction")]
    assert {ua for *_, ua in seen} == {"Mozilla/5.0 Sniper/1.0"}
    status = pool.key_status()
    assert [(k["key"], k["state"], k["calls"]) for k in status] == [
        (_sha("k0"), "credit_exhausted", 1), (_sha("k1"), "healthy", 1), (_sha("k2"), "unknown", 0)]
    assert status[0]["last_reason"] == "credit_exhausted" and status[0]["bench_left_s"] > 3000
    assert len(sent) == 1 and "HELIUS-CREDITS-EXHAUSTED" in sent[0]
    assert "lane=pool_prewarm" in sent[0] and _sha("k0") in sent[0] and "api-key" not in sent[0]
    assert "another account" in sent[0]
    # a fresh cron process with the same spent key does not page again the same UTC day
    call2, _ = rr.build_research_call("pool_prewarm", min_call_s=0.001, send_fn=sent.append,
                                      http=_http({K[0]}, []), env=env)
    assert call2("getTransaction", ["sig"]) == "ok@k1"
    assert len(sent) == 1


def test_research_pool_logs_never_say_funded(state, caplog):
    call, _ = rr.build_research_call("pool_prewarm", min_call_s=0.001, send_fn=lambda m: None,
                                     http=_http({K[0]}, []), env={
                                         "MEMECOIN_RESEARCH_HELIUS_RPC_URLS": ",".join(K)})
    with caplog.at_level("INFO"):
        call("getTransaction", ["sig"])
    text = caplog.text
    assert "Solana research:pool_prewarm RPC rotated" in text and "funded" not in text


def test_every_key_spent_raises_the_http_error_so_the_429_retry_still_sees_it(state):
    _, pool = rr.build_research_call("x", send_fn=lambda m: None, http=_http(set(K), []),
                                     env={"MEMECOIN_RESEARCH_HELIUS_RPC_URLS": ",".join(K)})
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": []})
    with pytest.raises(urllib.error.HTTPError) as exc:
        pool("POST", K[0], body=body)
    assert exc.value.code == 429


def test_unset_list_uses_the_single_research_key():
    assert rr.research_urls({"MEMECOIN_RESEARCH_HELIUS_RPC_URL": " %s " % K[0]}) == [K[0]]
    assert rr.research_urls({}) == []
    assert rr.build_research_call("x", env={}) == (None, None)


@pytest.mark.parametrize("funded_var", ["MEMECOIN_SOLANA_RPC_URL", "MEMECOIN_SOLANA_RPC_POOL_URLS"])
def test_a_research_key_that_is_also_funded_refuses_the_lane(funded_var):
    env = {"MEMECOIN_RESEARCH_HELIUS_RPC_URLS": ",".join(K),
           # same key, different host spelling: the key is the credential, not the url
           funded_var: "https://other.helius-rpc.com/?api-key=k2"}
    assert rr.funded_overlap(K, env) == [_sha("k2")]
    assert rr.build_research_call("x", env=env) == (None, None)


def test_funded_pool_pages_once_and_a_page_failure_never_fails_the_read():
    calls = []

    def boom(idx, key):
        calls.append((idx, key))
        raise RuntimeError("telegram down")

    pool = RotatingSolanaRpcHttp(_http({K[0]}, []), K, on_credit_exhausted=boom)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": []})
    assert pool("POST", K[0], body=body)[1]["result"] == "ok@k1"
    pool._blocked_until[0] = 0.0          # bench over; the probe fails, and 0 is spent again
    pool._active = 0
    pool("POST", K[0], body=body)
    assert calls == [(0, _sha("k0"))]


def test_heartbeat_carries_key_ids_only(tmp_path):
    from sniper.services import memecoin_runner as run
    path = tmp_path / "hb.json"
    run.write_heartbeat({}, True, path=str(path), research_rpc_pool={"signal_pool_resolver": [
        {"key": "abcd1234", "state": "healthy", "bench_left_s": None, "last_reason": None,
         "calls": 3, "url": K[0]}]})
    written = json.loads(path.read_text())["research_rpc_pool"]
    assert written == {"signal_pool_resolver": [{"key": "abcd1234", "state": "healthy",
                                                 "bench_left_s": None, "last_reason": None,
                                                 "calls": 3}]}


def test_key_id_distinguishes_shyft_credentials_and_preserves_helius_identity():
    first = "https://rpc.shyft.to/?api_key=shyft-first"
    second = "https://rpc.shyft.to/?api_key=shyft-second"
    assert key_id(first) == _sha("shyft-first")
    assert key_id(second) == _sha("shyft-second")
    assert key_id(first) != key_id(second)
    assert key_id(K[0]) == _sha("k0")


def test_shyft_research_uses_neutral_endpoint_and_preserves_result():
    url = "https://rpc.shyft.to/?api_key=research-test"
    seen = []
    env = {"MEMECOIN_SOLANA_INFRA_PROVIDER": "shyft",
           "MEMECOIN_RESEARCH_SOLANA_RPC_URLS": url,
           "MEMECOIN_RESEARCH_HELIUS_RPC_URL": K[0]}
    assert rr.research_urls(env) == [url]
    call, pool = rr.build_research_call("profile", http=_http(set(), seen), env=env)
    assert call("getBalance", ["P"]) == "ok@st"
    assert seen[0][0] == url
    assert pool.key_status()[0]["key"] == _sha("research-test")


def test_shyft_same_key_funded_overlap_is_credential_based():
    url = "https://rpc.shyft.to/?api_key=same"
    env = {"MEMECOIN_SOLANA_RPC_URL": url + "&region=ams"}
    assert rr.funded_overlap([url], env) == [_sha("same")]
