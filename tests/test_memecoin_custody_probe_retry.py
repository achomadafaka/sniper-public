"""A transport flake at startup is not an identity mismatch.

The custody identity gates ran ONE probe with zero retries and failed the whole startup on any
answerless attempt. Measured 2026-08-14: 839 failed watchdog launches, a 96-minute funded blackout
after boot, and two deploy rollbacks (13:36Z, 13:38Z) — each caused by a seconds-long RPC flake
that answered normally when probed moments later.

The distinction that must survive: `wrong_chain` is the catastrophe this gate exists for and fails
INSTANTLY — no retry makes a wrong genesis right. Only an answerless probe (`fail`) is retried,
and only a bounded number of times, so a genuinely dead RPC still fails startup.
"""
import pytest

from sniper.execution import submission_backends as B
from sniper.settings.memecoin_submission import SubmissionConfigError


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(B.time, "sleep", lambda _s: None)


def _http_script(script, calls):
    """Each element of `script` is (status, payload) served in order; the last repeats."""
    def http(method, url, body=None, timeout=None):
        calls.append(url)
        status, payload = script[min(len(calls) - 1, len(script) - 1)]
        if status is None:
            raise OSError("transport down")
        return status, payload, b""
    return http


def test_a_transient_flake_recovers_on_retry():
    calls = []
    http = _http_script([(None, None), (200, {"result": "0x1237"})], calls)
    B._validate_evm_custody(http, "https://rpc", "4663", 5.0)   # must not raise
    assert len(calls) == 2


def test_a_dead_rpc_still_fails_after_the_retries():
    calls = []
    http = _http_script([(None, None)], calls)
    with pytest.raises(SubmissionConfigError):
        B._validate_evm_custody(http, "https://rpc", "4663", 5.0)
    assert len(calls) == B._CUSTODY_PROBE_ATTEMPTS, "bounded — a dead RPC must still fail closed"


def test_wrong_chain_fails_instantly_with_no_retry():
    """Signing against a mis-pointed RPC is the catastrophe the gate exists for."""
    calls = []
    http = _http_script([(200, {"result": "0x9999"})], calls)
    with pytest.raises(SubmissionConfigError):
        B._validate_evm_custody(http, "https://rpc", "4663", 5.0)
    assert len(calls) == 1, "a WRONG answer must never be retried"


def test_solana_transient_flake_recovers_on_retry():
    from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
    calls = []
    # attempt 1: getHealth transport-fails; attempt 2: health ok + correct genesis
    script = [(None, None),
              (200, {"result": "ok"}), (200, {"result": SOLANA_MAINNET_GENESIS})]
    http = _http_script(script, calls)
    B._validate_solana_custody(http, "https://rpc", 5.0)        # must not raise
    assert len(calls) == 3


def test_solana_wrong_genesis_fails_instantly():
    calls = []
    script = [(200, {"result": "ok"}), (200, {"result": "WrongGenesisHash1111111111111111"})]
    http = _http_script(script, calls)
    with pytest.raises(SubmissionConfigError):
        B._validate_solana_custody(http, "https://rpc", 5.0)
    assert len(calls) == 2, "health + genesis once; a wrong genesis must never be retried"


# --------------------------------------------------------------------------------------------
# 2026-09-03: a DARK custody endpoint must not keep the runner down when the pool proves the chain.
# The Alchemy custody endpoint died (SSL handshake timeout) with an open funded position, and this
# gate refused every watchdog relaunch for the whole outage while two healthy, identity-checked
# pool endpoints answered eth_chainId in 60-190ms. Reads rotate through the pool and auto_free
# broadcast fans out to the chain-owner endpoints, so the runner could have run the entire time.
# --------------------------------------------------------------------------------------------
from sniper.execution import evm_rpc_pool as POOL

CUSTODY, ALT1, ALT2 = "https://custody", "https://alt1", "https://alt2"


@pytest.fixture
def _pool(monkeypatch):
    POOL._state.clear()
    monkeypatch.setattr(POOL, "pool_for", lambda url, cfg=None: [CUSTODY, ALT1, ALT2])
    yield
    POOL._state.clear()


def _http_by_url(table, calls):
    """`table[url]` -> (status, payload) or None for a transport failure; records every url."""
    def http(method, url, body=None, timeout=None):
        calls.append(url)
        ans = table.get(url)
        if ans is None:
            raise OSError("transport down")
        return ans[0], ans[1], b""
    return http


def test_a_dark_custody_starts_when_a_pool_member_proves_the_chain(_pool):
    calls = []
    http = _http_by_url({ALT1: (200, {"result": "0x1237"})}, calls)
    B._validate_evm_custody(http, CUSTODY, "4663", 5.0)                 # must not raise
    assert calls.count(CUSTODY) == B._CUSTODY_PROBE_ATTEMPTS, "custody still gets its full retries first"
    assert ALT1 in calls and ALT2 not in calls, "stop at the first member that proves the chain"
    assert POOL.available(CUSTODY) is False, "the dark custody endpoint is quarantined, not trusted"
    assert POOL.identity_state(ALT1) is True


def test_a_wrong_chain_custody_is_never_masked_by_a_healthy_pool(_pool):
    """The catastrophe this gate exists for. A healthy alternate must not turn it into a start."""
    calls = []
    http = _http_by_url({CUSTODY: (200, {"result": "0x9999"}), ALT1: (200, {"result": "0x1237"})}, calls)
    with pytest.raises(SubmissionConfigError) as ei:
        B._validate_evm_custody(http, CUSTODY, "4663", 5.0)
    assert "wrong_chain" in str(ei.value)
    assert calls == [CUSTODY], "no retry, and the pool is never consulted"


def test_a_pool_member_on_the_wrong_chain_is_disproven_and_skipped(_pool):
    calls = []
    http = _http_by_url({ALT1: (200, {"result": "0x9999"}), ALT2: (200, {"result": "0x1237"})}, calls)
    B._validate_evm_custody(http, CUSTODY, "4663", 5.0)
    assert POOL.identity_state(ALT1) is False, "a wrong-chain member must never be usable later"
    assert POOL.identity_state(ALT2) is True


def test_every_endpoint_dark_raises_the_ORIGINAL_transport_error(_pool):
    calls = []
    http = _http_by_url({}, calls)
    with pytest.raises(SubmissionConfigError) as ei:
        B._validate_evm_custody(http, CUSTODY, "4663", 5.0)
    assert "after %d attempts" % B._CUSTODY_PROBE_ATTEMPTS in str(ei.value)
    assert getattr(ei.value, "verdict", ("?",))[0] == "fail"
    assert calls.count(ALT1) == 1 and calls.count(ALT2) == 1, "each member probed once, no retry storm"


def test_the_verdict_travels_on_the_exception():
    calls = []
    http = _http_script([(200, {"result": "0x9999"})], calls)
    with pytest.raises(SubmissionConfigError) as ei:
        B._validate_evm_custody(http, "https://rpc", "4663", 5.0)
    assert ei.value.verdict[0] == "wrong_chain"
