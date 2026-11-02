"""A cold signal's facts and safety reads on the funded 56 pool's Alchemy member (Alpha GO 2026-10-16).

e54dddf6 (19:49Z) decided ~115 ms after the watcher's wake, waiting for the coin's facts batch on the
paced Chainstack lane host (facts p50 101 / p90 174 ms over 13 cold signals since 10-14); the runner's
56 pool measures Alchemy at ewma 45 ms. Pins: per family and knob; Alchemy only while the funded pool
shows it available and identity-proven; a 429 / 403 / 5xx / transport failure is recorded and
quarantined in that pool and the read runs on the lane host once; an error item or a partial batch is
the coin's answer, never re-asked; the lane beat names each family's host and its fallbacks.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from sniper.execution import evm_rpc_pool as pool  # noqa: E402
from sniper.execution.evm_token_safety import names_timings  # noqa: E402
from sniper.services import memecoin_bsc_fast_lane as bsc  # noqa: E402
from test_memecoin_bsc_fast_lane_live import live56  # noqa: E402,F401

ALCHEMY = "https://bnb-mainnet.g.alchemy.com/v2/test-key"
MINT = "0x" + "34" * 20
SUPPLY = 10 ** 27


@pytest.fixture(autouse=True)
def _pool(monkeypatch):
    pool.reset_for_tests()
    pool.mark_identity(ALCHEMY, True)
    monkeypatch.delenv("MEMECOIN_BSC_FAST_LOW_RTT_READS", raising=False)
    yield
    pool.reset_for_tests()


class Host:
    """The Alchemy seam's transport: records each request and answers from `script` (a status and a
    JSON answer per request, or an exception)."""

    def __init__(self, *script):
        self.script, self.sent = list(script), []

    def __call__(self, method, target, headers=None, body=None, timeout=10):
        self.sent.append((target, json.loads(body)))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step[0], step[1], {}


def _facts_answer(decimals=18, supply=SUPPLY):
    return 200, [{"jsonrpc": "2.0", "id": 1, "result": hex(decimals)},
                 {"jsonrpc": "2.0", "id": 2, "result": hex(supply)}]


def _lane(host):
    """A LowRttReads over `host`, wrapped round lane-host stand-ins that record their calls."""
    low = bsc.LowRttReads(ALCHEMY, host)
    lane = {"facts": [], "safety": []}

    def slow_facts(mint):
        lane["facts"].append(mint)
        return 18, SUPPLY

    def slow_safety(mint, usd=None, timings=None):
        lane["safety"].append(mint)
        return True, None

    def fast_safety(mint, usd=None, timings=None):
        # one read on the Alchemy seam; a host failure inside a safety check turns into a refusal
        try:
            bsc.LaneHost(ALCHEMY, low.http)("eth_call", [{"to": mint, "data": "0x"}, "latest"])
        except Exception:
            return False, "code read failed"
        return True, None
    seam = low.wrap({"inline_facts_fn": slow_facts, "inline_safety_fn": slow_safety, "inline_host": None},
                    fast_facts=bsc.lane_facts(bsc.LaneHost(ALCHEMY, low.http)), fast_safety=fast_safety)
    return low, seam, lane


def test_the_facts_batch_goes_to_alchemy_bound_to_the_mint():
    host = Host(_facts_answer())
    low, seam, lane = _lane(host)
    assert seam["inline_facts_fn"](MINT) == (18, SUPPLY)
    (target, batch), = host.sent
    assert target == ALCHEMY and [call["params"][0]["to"] for call in batch] == [MINT, MINT]
    assert lane["facts"] == [] and low.counts["facts"]["low_rtt"] == 1
    assert pool.snapshot([ALCHEMY])[0]["ok"] == 1


@pytest.mark.parametrize("failure", [(429, {"error": "rate"}), (403, {"error": "no"}), (503, {"x": 1}),
                                     ConnectionResetError("reset")])
def test_a_host_failure_falls_back_to_the_lane_host_once_and_quarantines_alchemy(failure):
    host = Host(failure)
    low, seam, lane = _lane(host)
    assert seam["inline_facts_fn"](MINT) == (18, SUPPLY)
    assert lane["facts"] == [MINT] and low.counts["facts"]["fallback"] == 1
    assert not pool.available(ALCHEMY), "the pool's own quarantine: no retry storm"
    assert pool.snapshot([ALCHEMY])[0]["fail"] == 1
    seam["inline_facts_fn"](MINT)                       # quarantined: straight to the lane host
    assert len(host.sent) == 1 and lane["facts"] == [MINT, MINT]
    assert low.counts["facts"]["lane_host"] == 1


def test_a_safety_refusal_caused_by_a_host_failure_is_read_again_on_the_lane_host():
    host = Host((429, {"error": "rate"}))
    low, seam, lane = _lane(host)
    assert names_timings(seam["inline_safety_fn"]), "the lane passes its timings through the wrapper"
    assert seam["inline_safety_fn"](MINT, usd=100.0, timings={}) == (True, None)
    assert lane["safety"] == [MINT] and low.counts["safety"]["fallback"] == 1


def test_a_partial_batch_is_refused_as_today_and_never_re_asked():
    host = Host((200, [{"jsonrpc": "2.0", "id": 1, "result": "0x12"}]))
    low, seam, lane = _lane(host)
    with pytest.raises(RuntimeError, match="do not match"):
        seam["inline_facts_fn"](MINT)
    assert lane["facts"] == [] and pool.available(ALCHEMY), "an answer is not a host failure"


def test_an_error_item_is_the_coins_answer():
    host = Host((200, [{"jsonrpc": "2.0", "id": 1, "result": "0x12"},
                       {"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "execution reverted"}}]))
    low, seam, lane = _lane(host)
    with pytest.raises(RuntimeError):
        seam["inline_facts_fn"](MINT)
    assert lane["facts"] == [] and low.counts["facts"] == {"low_rtt": 0, "fallback": 0, "lane_host": 0}


@pytest.mark.parametrize("state", ["quarantined", "unproven", "disproven"])
def test_a_member_the_pool_does_not_vouch_for_is_never_asked(state):
    if state == "quarantined":
        pool.penalize(ALCHEMY, "test")
    else:
        pool.reset_for_tests()
        if state == "disproven":
            pool.mark_identity(ALCHEMY, False)
    host = Host()
    low, seam, lane = _lane(host)
    seam["inline_facts_fn"](MINT)
    seam["inline_safety_fn"](MINT)
    assert host.sent == [] and lane == {"facts": [MINT], "safety": [MINT]}


@pytest.mark.parametrize("raw, facts_fast, safety_fast", [
    (None, True, True), ("facts,safety", True, True), ("facts", True, False), ("safety", False, True),
    ("", False, False), ("0", False, False), ("facts,bogus", False, False)])
def test_the_knob_is_per_family(monkeypatch, raw, facts_fast, safety_fast):
    if raw is not None:
        monkeypatch.setenv("MEMECOIN_BSC_FAST_LOW_RTT_READS", raw)
    host = Host(_facts_answer(), (200, {"jsonrpc": "2.0", "id": 1, "result": "0x"}))
    low, seam, lane = _lane(host)
    seam["inline_facts_fn"](MINT)
    seam["inline_safety_fn"](MINT)
    assert (lane["facts"] == []) is facts_fast and (lane["safety"] == []) is safety_fast


def test_the_alchemy_member_comes_from_the_funded_pool_config():
    pools = {"56": ["https://bsc-dataseed.bnbchain.org", "https://bsc-rpc.publicnode.com", ALCHEMY]}
    assert bsc.low_rtt_url(SimpleNamespace(MEMECOIN_EVM_RPC_POOL_URLS=pools)) == ALCHEMY
    assert bsc.low_rtt_url(SimpleNamespace(MEMECOIN_EVM_RPC_POOL_URLS={"56": pools["56"][:2]})) is None
    assert bsc.low_rtt_url(SimpleNamespace(MEMECOIN_EVM_RPC_POOL_URLS={})) is None


def test_the_lane_beat_names_each_familys_host_and_its_fallbacks(monkeypatch):
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LOW_RTT_READS", "facts")
    low, seam, _lane_calls = _lane(Host((429, {"error": "rate"})))
    seam["inline_facts_fn"](MINT)
    out = bsc.lane_counters(SimpleNamespace(low_rtt=low))["low_rtt"]
    assert out["host"] == "bnb-mainnet.g.alchemy.com" and "test-key" not in json.dumps(out)
    assert out["facts"] == {"low_rtt": 0, "fallback": 1, "lane_host": 0, "next": "lane_host"}
    assert out["safety"]["next"] == "lane_host" and out["available"] is False


@pytest.fixture
def alchemy_member(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS",
                        {"56": ["https://bsc-dataseed.bnbchain.org", ALCHEMY]})


def test_the_live_lane_reads_through_it_when_the_pool_has_an_alchemy_member(alchemy_member, live56):
    live = live56[0]["live"]
    assert live.low_rtt is not None and live.low_rtt.url == ALCHEMY
    assert live.inline_facts_fn.__name__ == "inline_facts"
    assert live.inline_safety_fn.__name__ == "inline_safety" and names_timings(live.inline_safety_fn)


def test_the_funded_pool_owns_alchemys_health_not_a_private_ten_minute_dead_mark(monkeypatch):
    """Codex F1 [P1] (review through 648ada5d7): three transport failures marked LowRttReads' private
    pacer dead for 600 s while usable() checked only the pool, whose quarantine ends in 5-60 s. The next
    cold facts call raised InlineHostDead before the failure recorder, so _run rethrew and skipped the
    lane-host fallback: a ten-minute cold-entry outage after a transient incident."""
    host = Host(*([ConnectionResetError("transient")] * 3), _facts_answer())
    low, seam, lane = _lane(host)
    monkeypatch.setattr(pool, "available", lambda url: True)        # each quarantine already over
    for _ in range(3):
        assert seam["inline_facts_fn"](MINT) == (18, SUPPLY)        # each one falls back, once
    assert lane["facts"] == [MINT] * 3 and len(host.sent) == 3
    assert low.usable("facts")
    assert seam["inline_facts_fn"](MINT) == (18, SUPPLY), "no InlineHostDead after recovery"
    assert len(host.sent) == 4 and lane["facts"] == [MINT] * 3, "the recovered member answered"
    assert low.counts["facts"]["low_rtt"] == 1
