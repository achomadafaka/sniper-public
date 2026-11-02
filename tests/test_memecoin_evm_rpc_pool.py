"""Free EVM RPC endpoint rotation.

The funded 4663 lane ran on ONE keyless public endpoint. Measured 2026-08-13, 208 of 210 logged
`HTTP Error 429` tracebacks came from `evm_wallet._rpc`, and a 429 inside the V4 quote surfaced as
`no_route` — an entry refused as though the coin had no pool.
"""
import pytest

from sniper.execution import evm_rpc_pool as pool


PRIMARY = "https://primary.example"
SECOND = "https://second.example"


class _Cfg:
    MEMECOIN_EVM_RPC_POOL_URLS = {"4663": (PRIMARY, SECOND)}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    pool.reset_for_tests()
    import sniper.settings.memecoin as real
    monkeypatch.setattr(real, "MEMECOIN_EVM_RPC_POOL_URLS", _Cfg.MEMECOIN_EVM_RPC_POOL_URLS,
                        raising=False)
    yield
    pool.reset_for_tests()


def test_a_chain_with_no_pool_is_left_exactly_alone():
    """The no-pool path must keep the previous single-call behaviour byte for byte."""
    assert pool.pool_for("https://solo.example") == ["https://solo.example"]
    assert pool.choose("https://solo.example") == "https://solo.example"
    assert pool.chain_of("https://solo.example") is None


def test_the_primary_is_preferred_while_healthy():
    assert pool.choose(PRIMARY, verify_fn=lambda u: True) == PRIMARY


def test_it_rotates_to_the_secondary_only_while_the_primary_is_quarantined():
    pool.penalize(PRIMARY, "429")
    assert pool.choose(PRIMARY, verify_fn=lambda u: True) == SECOND
    pool.penalize(PRIMARY, "429", quarantine_s=0)      # quarantine served
    assert pool.choose(PRIMARY, verify_fn=lambda u: True) == PRIMARY


def test_an_endpoint_on_the_wrong_chain_is_never_used():
    """A funded path reading balances or quotes from another chain is worse than being rate
    limited, so a disproven endpoint is excluded outright rather than deprioritised."""
    pool.penalize(PRIMARY, "429")
    assert pool.choose(PRIMARY, verify_fn=lambda u: False) == PRIMARY   # falls back, never SECOND
    assert pool.identity_state(SECOND) is False
    # and it stays excluded on later calls without re-probing
    assert pool.choose(PRIMARY, verify_fn=lambda u: True) == PRIMARY


def test_an_unproven_secondary_is_not_used_when_no_verifier_is_supplied():
    pool.penalize(PRIMARY, "429")
    assert pool.choose(PRIMARY) == PRIMARY


def test_a_failing_identity_probe_quarantines_rather_than_trusting():
    pool.penalize(PRIMARY, "429")

    def _boom(_u):
        raise RuntimeError("probe transport failed")

    assert pool.choose(PRIMARY, verify_fn=_boom) == PRIMARY
    assert pool.available(SECOND) is False


def test_expected_chain_comes_from_config_not_from_another_endpoint():
    """Probing the primary to validate a secondary recurses, unboundedly if the primary is
    quarantined. The expectation must be static."""
    assert pool.chain_of(PRIMARY) == "4663"
    assert pool.chain_of(SECOND) == "4663"


def test_rpc_rotates_on_a_provider_failure_and_returns_the_good_answer(monkeypatch):
    from sniper.execution import evm_wallet
    seen = []

    def fake_http(method, url, headers=None, body=None, timeout=10):
        seen.append(url)
        if url == PRIMARY:
            raise OSError("429 Too Many Requests")
        if "eth_chainId" in (body or ""):
            return 200, {"result": "0x1237"}, None
        return 200, {"result": "0xdead"}, None

    j = evm_wallet._rpc(PRIMARY, "eth_blockNumber", [], http=fake_http)
    assert j == {"result": "0xdead"}
    assert PRIMARY in seen and SECOND in seen
    assert pool.available(PRIMARY) is False      # the failing endpoint was quarantined


def test_rpc_rotates_when_pooled_http_returns_rate_limit_status():
    """Pooled HTTP returns 429 as data, so the RPC layer must rotate it like HTTPError."""
    from sniper.execution import evm_wallet
    seen = []

    def fake_http(method, url, headers=None, body=None, timeout=10):
        seen.append(url)
        if "eth_chainId" in (body or ""):
            return 200, {"result": "0x1237"}, None
        if url == PRIMARY:
            return 429, {"error": "rate limited"}, None
        return 200, {"result": "0xdead"}, None

    assert evm_wallet._rpc(PRIMARY, "eth_blockNumber", [], http=fake_http) == {
        "result": "0xdead",
    }
    assert PRIMARY in seen and SECOND in seen
    assert pool.available(PRIMARY) is False


def test_rpc_raises_when_every_endpoint_fails(monkeypatch):
    """Exhaustion must reach the caller's fail-closed handling, not return a fabricated answer."""
    from sniper.execution import evm_wallet

    def fake_http(method, url, headers=None, body=None, timeout=10):
        raise OSError("down")

    with pytest.raises(OSError):
        evm_wallet._rpc(PRIMARY, "eth_blockNumber", [], http=fake_http)


@pytest.mark.parametrize("backup", ["balance", "zero", "wrong_chain", "rate_limit", "malformed"])
def test_ethereum_quote_balance_recovers_429_only_from_a_chain_proven_answer(monkeypatch, backup):
    import json
    from sniper.execution import evm_wallet as W
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"1": (PRIMARY, SECOND)})
    pool.mark_identity(PRIMARY, True)
    seen = []

    def http(method, url, headers=None, body=None, timeout=10):
        request = json.loads(body)
        seen.append((url, request["method"]))
        if request["method"] == "eth_chainId":
            return 200, {"result": "0x2" if backup == "wrong_chain" else "0x1"}, {}
        if url == PRIMARY or backup == "rate_limit":
            return 429, {"error": {"code": -32005}}, {}
        return 200, {"result": "0x" if backup == "malformed" else hex(
            0 if backup == "zero" else 2 * 10 ** 17)}, {}

    got = W.quote_balance_usd_or_none("0x" + "12" * 20, "1", PRIMARY, http=http,
                                      price_fn=lambda: 2500.0)
    if backup in {"balance", "zero"}:
        assert got == (500.0 if backup == "balance" else 0.0)
        assert seen.index((SECOND, "eth_chainId")) < seen.index((SECOND, "eth_call"))
    else:
        assert got is None, "an unreadable or wrong-chain balance must remain unknown"
    if backup == "wrong_chain":
        assert (SECOND, "eth_call") not in seen


@pytest.mark.parametrize("identity", [None, False, "expired", "valid", "cached"])
def test_a_method_refusal_cannot_make_an_unproven_backup_the_trusted_primary(monkeypatch, identity):
    import json
    from sniper.execution import evm_wallet as W
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"1": (PRIMARY, SECOND)})
    pool.note_refusal(PRIMARY, "-32600", "eth_call")
    if identity in {False, "expired", "cached"}:
        pool.mark_identity(SECOND, identity in {"expired", "cached"})
    if identity == "expired":
        pool._entry(SECOND)["checked"] -= pool._IDENTITY_TTL_S + 1
    seen = []

    def http(method, url, headers=None, body=None, timeout=10):
        request = json.loads(body)
        seen.append(request["method"])
        return 200, {"result": ("0x1" if identity == "valid" else "0x2")
                     if request["method"] == "eth_chainId"
                     else hex(2 * 10 ** 17)}, {}

    got = W.quote_balance_usd_or_none("0x" + "12" * 20, "1", PRIMARY, http=http,
                                     price_fn=lambda: 2500.0)
    if identity in {"valid", "cached"}:
        assert got == 500.0
        assert seen == (["eth_chainId", "eth_call"] if identity == "valid" else ["eth_call"])
    else:
        assert got is None
        assert "eth_call" not in seen


# --------------------------------------------------------------------------------------------
# 2026-09-03: rank by MEASUREMENT with hysteresis. Bench that day, 60 samples/endpoint, 0 errors:
# alchemy p50 116ms lag 0; publicnode p50 60ms lag 0.5; chain-owner p50 191ms lag 8 blocks. The
# configured order was the only order, so a slow, stale primary was preferred unconditionally.
# --------------------------------------------------------------------------------------------

def test_an_unmeasured_pool_keeps_the_configured_order():
    pool.mark_identity(SECOND, True)
    assert pool.choose(PRIMARY, verify_fn=lambda u: True) == PRIMARY


def test_a_materially_faster_proven_secondary_outranks_the_primary():
    pool.mark_identity(SECOND, True)
    for _ in range(5):
        pool.record(PRIMARY, 200.0, True)
        pool.record(SECOND, 60.0, True)
    assert pool.choose(PRIMARY, verify_fn=lambda u: True) == SECOND


def test_a_slightly_faster_secondary_does_NOT_flip_the_primary():
    """Hysteresis: the switch needs a material margin, or a jittery pair flaps every read."""
    pool.mark_identity(SECOND, True)
    for _ in range(5):
        pool.record(PRIMARY, 100.0, True)
        pool.record(SECOND, 80.0, True)
    assert pool.choose(PRIMARY, verify_fn=lambda u: True) == PRIMARY


def test_head_lag_is_priced_into_the_rank():
    """A fast endpoint 8 blocks behind is worse for quotes and nonces than a slower fresh one."""
    pool.mark_identity(SECOND, True)
    for _ in range(5):
        pool.record(PRIMARY, 120.0, True)
        pool.record(SECOND, 60.0, True)
    pool.record_heads({PRIMARY: 1000, SECOND: 992})          # SECOND is 8 blocks stale
    assert pool.score(SECOND) > pool.score(PRIMARY)
    assert pool.choose(PRIMARY, verify_fn=lambda u: True) == PRIMARY


def test_a_disproven_endpoint_is_never_ranked_in_however_fast():
    pool.mark_identity(SECOND, False)
    for _ in range(5):
        pool.record(PRIMARY, 500.0, True)
        pool.record(SECOND, 10.0, True)
    assert pool.choose(PRIMARY, verify_fn=lambda u: True) == PRIMARY


def test_failures_decay_into_the_score_and_recovery_returns_to_the_primary():
    pool.mark_identity(SECOND, True)
    for _ in range(5):
        pool.record(PRIMARY, 100.0, False)
        pool.record(SECOND, 90.0, True)
    assert pool.score(PRIMARY) is None or pool.choose(PRIMARY, verify_fn=lambda u: True) in (PRIMARY, SECOND)
    for _ in range(20):
        pool.record(PRIMARY, 90.0, True)
    assert pool.choose(PRIMARY, verify_fn=lambda u: True) == PRIMARY


def test_snapshot_shows_hosts_never_urls():
    pool.record(PRIMARY, 100.0, True)
    snap = pool.snapshot([PRIMARY, SECOND])
    assert [e["host"] for e in snap] == ["primary.example", "second.example"]
    assert all("http" not in str(v) for e in snap for v in e.values()), "a custody url may carry a key"
    assert snap[0]["ewma_ms"] == 100.0 and snap[0]["ok"] == 1


def test_the_sampler_measures_every_member_and_records_lag():
    """WAIT ON THE FIELD THIS ASSERTS. The sampler calls `record` per endpoint as each head comes
    back, then `record_heads` once every future has finished -- so `score()` goes non-None BEFORE
    `lag_blocks` exists. Waiting on the score let a loaded runner assert lag between those two
    steps, which is how this went red in CI on 2026-09-04 while passing everywhere else."""
    import time as _t
    heads = {PRIMARY: 500, SECOND: 497}
    th = pool.start_head_sampler([PRIMARY, SECOND], lambda u: heads[u], period_s=60)
    deadline = _t.time() + 10
    while _t.time() < deadline and pool._entry(SECOND)["lag_blocks"] is None:
        _t.sleep(0.02)
    assert pool._entry(SECOND)["lag_blocks"] == 3, "the sampler never recorded a lag spread"
    assert pool._entry(PRIMARY)["lag_blocks"] == 0
    assert th.daemon


def test_head_lag_is_smoothed_not_last_sample():
    """One noisy sample must not out-vote a second of latency."""
    pool.record_heads({PRIMARY: 1000, SECOND: 992})     # 8 behind
    pool.record_heads({PRIMARY: 1010, SECOND: 1010})    # caught up
    lag = pool._entry(SECOND)["lag_blocks"]
    assert 0 < lag < 8, lag


def test_the_sampler_proves_identity_so_ranking_works_before_the_primary_ever_fails():
    import time as _t
    heads = {PRIMARY: 500, SECOND: 500}
    pool.start_head_sampler([PRIMARY, SECOND], lambda u: heads[u], period_s=60,
                            verify_fn=lambda u: True)
    deadline = _t.time() + 3
    while _t.time() < deadline and pool.identity_state(SECOND) is not True:
        _t.sleep(0.05)
    assert pool.identity_state(SECOND) is True


# --------------------------------------------------------------------------------------------
# 2026-09-03: a quarantine carries its reason, and escalates instead of costing 60s on a blip.
# --------------------------------------------------------------------------------------------

def test_the_first_blip_costs_seconds_not_a_minute_and_a_dead_endpoint_reaches_the_cap():
    pool.penalize(PRIMARY, "hedged_read_timeout")
    assert 0 < pool._entry(PRIMARY)["until"] - pool._now() <= pool._QUARANTINE_STEPS_S[0] + 0.01
    pool.penalize(PRIMARY, "RuntimeError")
    pool.penalize(PRIMARY, "RuntimeError")
    pool.penalize(PRIMARY, "RuntimeError")
    assert pool._entry(PRIMARY)["until"] - pool._now() > pool._QUARANTINE_STEPS_S[1]
    assert pool._entry(PRIMARY)["consec_fail"] == 4


def test_a_success_resets_the_escalation():
    for _ in range(3):
        pool.penalize(PRIMARY, "x")
    pool.record(PRIMARY, 50.0, True)
    assert pool._entry(PRIMARY)["consec_fail"] == 0
    pool.penalize(PRIMARY, "y")
    assert pool._entry(PRIMARY)["until"] - pool._now() <= pool._QUARANTINE_STEPS_S[0] + 0.01


def test_an_explicit_quarantine_is_honoured_as_given():
    pool.penalize(PRIMARY, "429", quarantine_s=0)
    assert pool.available(PRIMARY)


def test_the_snapshot_names_the_reason_for_a_quarantine():
    pool.penalize(PRIMARY, "hedged_read_failed")
    snap = pool.snapshot([PRIMARY])[0]
    assert snap["last_penalty"] == "hedged_read_failed" and snap["penalties"] == 1
    assert snap["quarantined_s"] > 0 and "http" not in str(snap)


# --------------------------------------------------------------------------------------------
# 2026-09-03: a 4xx WITH a JSON-RPC error body is a refusal of this REQUEST, not an outage.
# Alchemy free tier: eth_getLogs over 10 blocks -> HTTP 400 {"error": {"code": -32600, ...}}.
# Live: 95 "failures" in one minute, primary quarantined for reads, zero log lines.
# --------------------------------------------------------------------------------------------
from sniper.execution import evm_wallet as W

REFUSAL = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "Under the Free tier plan, you can make eth_getLogs requests with up to a 10 block range."}}
GOOD = {"jsonrpc": "2.0", "id": 1, "result": []}
LOGS = [{"address": "0x8366a39cc670b4001a1121b8f6a443a643e40951", "fromBlock": "0x1", "toBlock": "0x2000"}]


def _http_table(table, calls=None):
    """`table[url]` answers every NON-identity call; eth_chainId always proves 4663 so the
    fixture tests request classification, not identity (a fake that answered 400 to eth_chainId
    made `_verify` disprove the primary and the hedge never called it -- a fixture bug that hid
    the path under test)."""
    import json as _j
    def http(method, url, headers=None, body=None, timeout=None):
        if body and '"eth_chainId"' in (body if isinstance(body, str) else body.decode()):
            return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x1237"}, {}
        if calls is not None:
            calls[url] = calls.get(url, 0) + 1
        v = table[url]
        if isinstance(v, Exception):    # a transport failure / plain 403 from that member
            raise v
        st, j = v
        return st, j, {}
    return http


def test_a_4xx_json_rpc_refusal_rotates_to_the_next_member_without_a_penalty():
    pool.mark_identity(SECOND, True)
    http = _http_table({PRIMARY: (400, REFUSAL), SECOND: (200, GOOD)})
    answer = W._rpc(PRIMARY, "eth_getLogs", LOGS, http=http)
    assert answer == GOOD
    assert pool.available(PRIMARY), "a capability refusal must not quarantine a healthy endpoint"
    e = pool._entry(PRIMARY)
    assert e["fail"] == 0 and e["penalties"] == 0 and e["refused"] == 1 and e["last_refusal"] == "-32600"


def test_a_429_is_still_a_provider_failure_and_is_penalized():
    pool.mark_identity(SECOND, True)
    http = _http_table({PRIMARY: (429, {"error": {"code": -32005}}), SECOND: (200, GOOD)})
    assert W._rpc(PRIMARY, "eth_getLogs", LOGS, http=http) == GOOD
    assert not pool.available(PRIMARY) and pool._entry(PRIMARY)["penalties"] == 1


def test_when_every_member_refuses_the_last_refusal_is_returned_as_the_answer():
    pool.mark_identity(SECOND, True)
    http = _http_table({PRIMARY: (400, REFUSAL), SECOND: (400, REFUSAL)})
    assert W._rpc(PRIMARY, "eth_getLogs", LOGS, http=http) == REFUSAL
    assert pool.available(PRIMARY) and pool.available(SECOND)


def test_the_snapshot_counts_refusals_apart_from_failures():
    pool.note_refusal(PRIMARY, "-32600")
    snap = pool.snapshot([PRIMARY])[0]
    assert snap["refused"] == 1 and snap["last_refusal"] == "-32600" and snap["fail"] == 0


def test_a_refusal_on_the_identity_probe_is_a_failed_probe_not_a_wrong_chain():
    """A wrong chain answers with a DIFFERENT id. No id at all must not disprove an endpoint for
    an hour; it is a short quarantine and a retry."""
    pool.mark_identity(SECOND, True)
    def http(method, url, headers=None, body=None, timeout=None):
        if url == PRIMARY:
            return 400, REFUSAL, {}          # even eth_chainId is refused
        return 200, GOOD, {}
    pool.penalize(PRIMARY, "x", quarantine_s=0)      # force a fresh identity decision path
    W._rpc(PRIMARY, "eth_getLogs", LOGS, http=http)
    assert pool.identity_state(PRIMARY) is not False, "a missing id is not a wrong id"


def test_one_structural_refusal_of_a_method_excludes_that_member_for_that_method_only():
    """One strike: a plan cap or archive wall is deterministic, and after the backup answers the
    ranking prefers it, so the refusing member would not be asked again in time for a second
    strike -- it would just be re-asked, at a round trip each, whenever the ranking flipped."""
    pool.mark_identity(SECOND, True)
    calls = []
    def http(method, url, headers=None, body=None, timeout=None):
        if '"eth_chainId"' in (body if isinstance(body, str) else body.decode()):
            return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x1237"}, {}
        calls.append(url.split("/")[2])
        if url == PRIMARY and '"eth_getLogs"' in body:
            return 400, REFUSAL, {}
        return 200, GOOD, {}
    assert W._rpc(PRIMARY, "eth_getLogs", LOGS, http=http) == GOOD
    assert pool.refuses(PRIMARY, "eth_getLogs") and not pool.refuses(PRIMARY, "eth_call")
    calls.clear()
    W._rpc(PRIMARY, "eth_getLogs", LOGS, http=http)
    assert "primary.example" not in calls, "excluded for getLogs after one structural refusal"
    # NOT EXCLUDED for other methods: with the alternate quarantined, eth_call must reach the
    # primary. (Whether it is CHOSEN first when both are healthy is the ranking's business, not
    # this test's.)
    calls.clear()
    pool.penalize(SECOND, "x")
    W._rpc(PRIMARY, "eth_call", [{"to": "0x" + "1" * 40, "data": "0x"}, "latest"], http=http)
    assert "primary.example" in calls, "still eligible for every other method"
    assert pool.snapshot([PRIMARY])[0]["refused_methods"] == ["eth_getLogs"]


def test_two_keys_on_one_host_get_distinct_labels_and_neither_shows_its_key():
    """A second Alchemy key shares the host. The heartbeat must tell the rows apart without ever
    printing the path, which is where the key lives."""

    a = "https://robinhood-mainnet.g.alchemy.com/v2/alch_KEYONE"
    b = "https://robinhood-mainnet.g.alchemy.com/v2/alch_KEYTWO"
    labels = [row["host"] for row in pool.snapshot([a, b, SECOND])]
    assert labels[0] == "robinhood-mainnet.g.alchemy.com"
    assert labels[1] == "robinhood-mainnet.g.alchemy.com#2"
    assert len(set(labels)) == 3
    assert not any("KEY" in label for label in labels)


THIRD = "https://third.example"


def _three_member_pool(monkeypatch):
    import sniper.settings.memecoin as real
    monkeypatch.setattr(real, "MEMECOIN_EVM_RPC_POOL_URLS", {"4663": (PRIMARY, SECOND, THIRD)},
                        raising=False)
    pool.mark_identity(SECOND, True)
    pool.mark_identity(THIRD, True)


def test_the_hedge_walks_past_a_refusal_and_a_dead_backup_to_the_third_member(monkeypatch):
    """Live 2026-09-03: Alchemy refused a filtered eth_getLogs, the chain-owner backup answered a
    plain 403, and the refusal envelope became the answer while publicnode would have served it."""
    _three_member_pool(monkeypatch)
    calls = {}
    http = _http_table({PRIMARY: (400, REFUSAL), SECOND: OSError("403 Forbidden"),
                        THIRD: (200, GOOD)}, calls)
    assert W._rpc(PRIMARY, "eth_getLogs", LOGS, http=http) == GOOD
    assert calls == {PRIMARY: 1, SECOND: 1, THIRD: 1}
    assert pool._entry(PRIMARY)["penalties"] == 0 and pool._entry(PRIMARY)["refused"] == 1
    assert not pool.available(SECOND)


def test_a_quarantined_member_is_never_the_hedge_backup(monkeypatch):
    """The backup used to be picked without a quarantine check: the dead endpoint was re-asked and
    re-penalized (5s -> 15s) while a healthy third member sat idle."""
    _three_member_pool(monkeypatch)
    pool.penalize(SECOND, "dead")
    calls = {}
    http = _http_table({PRIMARY: (400, REFUSAL), SECOND: OSError("must not be asked"),
                        THIRD: (200, GOOD)}, calls)
    assert W._rpc(PRIMARY, "eth_getLogs", LOGS, http=http) == GOOD
    assert calls.get(SECOND, 0) == 0


def test_a_chain_level_error_is_returned_without_asking_another_member(monkeypatch):
    """A revert is the chain's answer, not the endpoint declining; re-asking buys the same revert
    from every node and spends pool quota for nothing."""
    _three_member_pool(monkeypatch)
    revert = {"jsonrpc": "2.0", "id": 1, "error": {"code": 3, "message": "execution reverted"}}
    calls = {}
    http = _http_table({PRIMARY: (200, revert), SECOND: (200, GOOD), THIRD: (200, GOOD)}, calls)
    assert W._rpc(PRIMARY, "eth_call", [{"to": "0x" + "1" * 40, "data": "0x"}, "latest"],
                  http=http) == revert
    assert calls == {PRIMARY: 1}


# ── the HTTP status rides on the failure, at every raise site (2026-09-30, Gamma LOW on 58009f134) ──
# `_http_failure(status)` replaced four identical `RuntimeError("EVM RPC provider HTTP failure")`
# raises. The dip watch pauses a chain on a 429 and only on a 429, and 4663 reads through a POOL,
# so the pooled paths must carry the status too, not only the single-endpoint one.

def _both(status, calls=None):
    return _http_table({PRIMARY: (status, {"error": "x"}), SECOND: (status, {"error": "x"})}, calls)


@pytest.mark.parametrize("method, params", [
    ("eth_getLogs", LOGS),                                   # not hedged: the sequential rotation
    ("eth_call", [{"to": "0x" + "11" * 20, "data": "0x"}, "latest"])])    # a hedged read
@pytest.mark.parametrize("status", [429, 503])
def test_a_pool_whose_members_all_fail_surfaces_the_http_status(method, params, status):
    pool.mark_identity(SECOND, True)
    calls = {}
    with pytest.raises(RuntimeError, match="EVM RPC provider HTTP failure") as err:
        W._rpc(PRIMARY, method, params, http=_both(status, calls))
    assert err.value.http_status == status
    assert calls.get(PRIMARY) and calls.get(SECOND)          # both members were really asked


def test_a_failed_identity_probe_carries_its_status_too():
    """`_verify` (the eth_chainId probe) raises the same failure; its status must not be lost."""
    statuses = []

    def http(method, url, headers=None, body=None, timeout=None):
        if '"eth_chainId"' in (body or ""):
            return 429, None, {}
        return 200, GOOD, {}
    real = pool.choose

    def choose(url, verify_fn=None, candidates=None):
        try:
            verify_fn(SECOND)
        except RuntimeError as exc:
            statuses.append(getattr(exc, "http_status", "missing"))
        return real(url, verify_fn=None, candidates=candidates)
    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(pool, "choose", choose)
        W._rpc(PRIMARY, "eth_getLogs", LOGS, http=http)
    finally:
        mp.undo()
    assert statuses == [429]


@pytest.mark.parametrize("method", ["eth_getBalance", "eth_getTransactionCount"])
def test_ethereum_healthy_custody_primary_keeps_balance_and_nonce_reads(monkeypatch, method):
    import json
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"1": (PRIMARY, SECOND)})
    for url, rtt in ((PRIMARY, 150), (SECOND, 80)):
        pool.mark_identity(url, True)
        pool.record(url, rtt, True)
    calls = []

    def http(_method, url, body=None, **_kwargs):
        calls.append((url, json.loads(body)["method"]))
        return 200, {"result": "0x2a"}, {}

    assert W._rpc(PRIMARY, method, ["0x" + "12" * 20, "latest"], http=http) == {
        "result": "0x2a"}
    assert calls == [(PRIMARY, method)]


@pytest.mark.parametrize("message", [
    "service temporarily unavailable", "Server busy, try again later",
    "Too many requests", "Rate limit exceeded",
])
def test_provider_busy_body_walks_from_secondary_to_healthy_primary(message):
    for url, rtt in ((PRIMARY, 150), (SECOND, 80)):
        pool.mark_identity(url, True)
        pool.record(url, rtt, True)
    calls = []

    def http(_method, url, body=None, **_kwargs):
        calls.append(url)
        if url == SECOND:
            return 200, {"error": {"code": -32603, "message": message}}, {}
        return 200, {"result": "0x2a"}, {}

    assert W._rpc(PRIMARY, "eth_getBalance", ["0x" + "12" * 20, "latest"],
                  http=http) == {"result": "0x2a"}
    assert calls == [SECOND, PRIMARY]
    assert not pool.available(SECOND)
    assert pool.available(PRIMARY)
    assert pool._entry(SECOND)["fail"] == 1
    assert pool._entry(SECOND)["consec_fail"] == 1


@pytest.mark.parametrize("code", [-32603, -32000, 3])
def test_internal_code_or_contract_revert_alone_does_not_rotate(code):
    pool.mark_identity(PRIMARY, True)
    pool.mark_identity(SECOND, True)
    calls = []
    answer = {"error": {"code": code, "message": "execution reverted: unavailable funds"}}

    def http(_method, url, body=None, **_kwargs):
        calls.append(url)
        return 200, answer, {}

    assert W._rpc(PRIMARY, "eth_call", [{"to": "0x" + "12" * 20}, "latest"],
                  http=http) == answer
    assert calls == [PRIMARY]
    assert pool.available(PRIMARY)


def test_full_range_refusals_are_noted_on_each_physical_member_without_excluding_pages():
    pool.mark_identity(PRIMARY, True)
    pool.mark_identity(SECOND, True)
    refusal = {"error": {"code": -32602, "message": "full range unsupported"}}
    calls = {}
    http = _http_table({PRIMARY: (200, refusal), SECOND: (200, refusal)}, calls)
    answer = W._rpc(PRIMARY, "eth_getLogs", [{"address": "0x" + "12" * 20,
                  "fromBlock": "0x0", "toBlock": "latest"}], http=http)
    assert answer == refusal
    assert calls == {PRIMARY: 1, SECOND: 1}
    for url in (PRIMARY, SECOND):
        assert pool.refuses(url, "eth_getLogs:full_range")
        assert not pool.refuses(url, "eth_getLogs")
        assert not pool.refuses(url, "eth_getLogs:full_range", ttl_s=0)
    assert W.answer_target(answer) in {PRIMARY, SECOND}
    assert W.answer_target(dict(answer)) is None


def test_full_range_refusal_does_not_hide_a_successful_backup():
    pool.mark_identity(PRIMARY, True)
    pool.mark_identity(SECOND, True)
    refusal = {"error": {"code": -32602, "message": "full range unsupported"}}
    calls = {}
    http = _http_table({PRIMARY: (200, refusal), SECOND: (200, GOOD)}, calls)
    assert W._rpc(PRIMARY, "eth_getLogs", [{"address": "0x" + "12" * 20,
                 "fromBlock": "0x0", "toBlock": "latest"}], http=http) == GOOD
    assert pool.refuses(PRIMARY, "eth_getLogs:full_range")
    assert not pool.refuses(SECOND, "eth_getLogs:full_range")
    assert calls == {PRIMARY: 1, SECOND: 1}


def test_per_read_candidate_filter_never_dispatches_to_parked_member():
    pool.mark_identity(PRIMARY, True)
    pool.mark_identity(SECOND, True)
    calls = {}
    http = _http_table({PRIMARY: (200, GOOD), SECOND: (200, GOOD)}, calls)
    http.read_candidate_allowed = lambda target: target == SECOND
    assert W._rpc(PRIMARY, "eth_getLogs", LOGS, http=http) == GOOD
    assert calls == {SECOND: 1}
    assert W.answer_target(GOOD) == SECOND


def test_empty_per_read_candidate_filter_refuses_before_dispatch():
    calls = {}
    http = _http_table({PRIMARY: (200, GOOD), SECOND: (200, GOOD)}, calls)
    http.read_candidate_allowed = lambda target: False
    with pytest.raises(pool.RequestNotSent):
        W._rpc(PRIMARY, "eth_getLogs", LOGS, http=http)
    assert calls == {}
    assert pool._entry(PRIMARY)["penalties"] == pool._entry(SECOND)["penalties"] == 0


@pytest.mark.parametrize("method, params", [("eth_call", [{}, "latest"]),
                                            ("eth_getLogs", LOGS)])
def test_http_failure_carries_actual_answering_member(method, params):
    pool.mark_identity(PRIMARY, True)
    pool.mark_identity(SECOND, True)
    pool.note_refusal(PRIMARY, "unsupported", method)
    with pytest.raises(RuntimeError) as err:
        W._rpc(PRIMARY, method, params,
               http=_http_table({SECOND: (429, {"error": "rate limited"})}))
    assert err.value.http_status == 429
    assert err.value.rpc_url == SECOND


@pytest.mark.parametrize("method, params", [
    ("eth_getBalance", ["0x" + "12" * 20, "latest"]),
    ("eth_call", [{"to": "0x" + "12" * 20, "data": "0x70a08231"}, "latest"]),
    ("eth_getTransactionCount", ["0x" + "12" * 20, "pending"]),
])
def test_slow_healthy_ethereum_custody_reads_never_race_an_unmeasured_backup(
        monkeypatch, method, params):
    import time
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"1": (PRIMARY, SECOND)})
    monkeypatch.setattr(W, "_HEDGE_AFTER_S", 0.001)
    for url, ms in ((PRIMARY, 150), (SECOND, 300)):
        pool.mark_identity(url, True)
        pool.record(url, ms, True)
    calls = []

    def http(_method, url, **_kwargs):
        calls.append(url)
        if url == PRIMARY:
            time.sleep(0.025)
        return 200, {"result": "0x2a" if url == PRIMARY else "0xbad"}, {}

    assert W._rpc(PRIMARY, method, params, http=http) == {"result": "0x2a"}
    assert calls == [PRIMARY]


def test_scoped_chain_identity_read_never_dispatches_to_a_filtered_endpoint():
    pool.mark_identity(PRIMARY, True)
    pool.mark_identity(SECOND, True)
    calls = []

    def http(_method, url, **_kwargs):
        calls.append(url)
        return 200, {"result": "0x1237"}, {}

    http.read_candidate_allowed = lambda target: target == SECOND
    assert W._rpc(PRIMARY, "eth_chainId", [], http=http) == {"result": "0x1237"}
    assert calls == [SECOND]
    http.read_candidate_allowed = lambda target: False
    with pytest.raises(pool.NoEligibleReadEndpoint):
        W._rpc(PRIMARY, "eth_chainId", [], http=http)
    assert calls == [SECOND]


@pytest.mark.parametrize('status', [200, 413])
def test_bounded_log_range_refusal_does_not_disable_smaller_reads(status):
    pool.mark_identity(PRIMARY, True)
    pool.mark_identity(SECOND, True)
    refusal = {'error': {'code': -32614,
                         'message': 'eth_getLogs is limited to a 2,000 range'}}
    calls = {}
    http = _http_table({PRIMARY: (status, refusal), SECOND: (status, refusal)}, calls)
    wide = [{'address': '0x' + '12' * 20, 'fromBlock': '0x100', 'toBlock': '0x2100'}]
    assert W._rpc(PRIMARY, 'eth_getLogs', wide, http=http) == refusal
    assert not pool.refuses(PRIMARY, 'eth_getLogs')
    assert not pool.refuses(SECOND, 'eth_getLogs')
    http = _http_table({PRIMARY: (200, GOOD), SECOND: (200, GOOD)}, {})
    assert W._rpc(PRIMARY, 'eth_getLogs', LOGS, http=http) == GOOD
