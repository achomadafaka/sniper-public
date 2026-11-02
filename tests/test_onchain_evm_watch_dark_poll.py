"""A dark socket's poll reads its logs where they can be read, from where the push left off (2026-09-29).

On 56 the poll fallback asked the read host (defibit) for eth_getLogs, and defibit answers every
topic-filtered getLogs with `-32005 limit exceeded`: 48 failures in two dark episodes (09-28 17:19Z and
09-29 02:52Z), so nothing was read while the socket was dark. Then the 02:53:08Z restart backfilled from
the heartbeat's last_block, and a poll beat carries none. Blocks 124639874-124640018 (~108 s) were
read by nobody (Gamma).

Alpha GO: while dark, and only while dark, the poll's eth_getLogs goes to the chain's backfill host
(its Alchemy pool member: 10-block span, at most one call a second). The poll starts at the last block
the push delivered. Its beat carries the last block it actually read, and a reconnect gap still waiting
for its backfill holds the beat's last_block at the gap, so a restart re-reads what nobody read.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import scripts.research.onchain_evm_wallet_watch as w  # noqa: E402
from sniper.settings import memecoin as cfg  # noqa: E402
from test_onchain_evm_watch_backfill import ALCHEMY, _getlogs, _heads, _run, env  # noqa: E402,F401
from thread_local_time import patch_local  # noqa: E402  (2026-09-29)


class _Clock:
    def __init__(self):
        self.t = 5_000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += max(float(s), 0.001)


@pytest.fixture
def dark(monkeypatch, env):
    """A fake clock for the poll loop, a read host that refuses getLogs like defibit, and a record of
    when each Alchemy getLogs was sent."""
    clock = _Clock()
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_4663", "http://rpc.test")
    # The fake clock answers THIS thread only (2026-09-29): `w.time` is the process-wide time module,
    # so any other thread in the xdist worker that sleeps would otherwise move this clock.
    patch_local(monkeypatch, "monotonic", clock.monotonic)
    patch_local(monkeypatch, "sleep", clock.sleep)
    env.update(rpc_getlogs=[], alchemy_at=[], head=2_040)

    def rpc(url, method, params):
        if method == "eth_blockNumber":
            return {"result": hex(env["head"])}
        if method == "eth_getLogs":
            env["rpc_getlogs"].append(params[0])
            return {"error": {"code": -32005, "message": "limit exceeded"}}
        raise AssertionError(method)
    monkeypatch.setattr(w, "_counted_rpc", rpc)
    stub = w.push_detection                       # the backfill fixture's stub needs `outcome`
    monkeypatch.setattr(w, "push_detection", lambda rpc_url, log, watched, *, outcome=None, **k:
                        stub(rpc_url, log, watched, outcome={} if outcome is None else outcome, **k))
    return clock


def _timed(env, clock, post):
    def timed(url, method, params):
        if method == "eth_getLogs":
            env["alchemy_at"].append(clock.t)
        return post(url, method, params)
    return timed


def _fallback(env):
    def step(state, kw):
        kw["on_fallback"]()
        env["beat_after_dark"] = json.loads(env["hb"].read_text())
    return step


def _reconnect(state, kw):
    state.reconnects += 1


def _run_dark(monkeypatch, env, clock, script, *, hits=(), post=None):
    original = w._backfill_post
    done = _run(monkeypatch, env, [], hits=hits)       # installs the harness, runs nothing
    monkeypatch.setattr(w, "_backfill_post", _timed(env, clock, post or w._backfill_post))

    def fake_stream(ws_url, **kw):
        for step in script:
            step(kw["state"], kw)
        return kw["state"]
    from sniper.services import memecoin_onchain_push as P
    monkeypatch.setattr(P, "run_push_stream", fake_stream)
    price = w.NativePrice(lambda: 2650.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)
    assert original is not None
    return done


def test_a_dark_poll_reads_its_logs_on_the_backfill_host_from_the_last_pushed_block(monkeypatch, env, dark):
    _run_dark(monkeypatch, env, dark, [_heads(2_000, 20), _fallback(env)], hits=[2_025])
    assert env["rpc_getlogs"] == [], "the read host refuses topic getLogs: never ask it while dark"
    assert env["pages"][:3] == [(2_020, 2_028), (2_029, 2_037), (2_038, 2_040)]
    assert len(env["inserts"]) == 1, "the dark window's buy is detected"
    gaps = [b - a for a, b in zip(env["alchemy_at"], env["alchemy_at"][1:])]
    assert gaps and min(gaps) >= 1.0 - 1e-9, "at most one Alchemy getLogs a second"


def test_a_dark_poll_preserves_source_block_time(monkeypatch, env, dark):
    _run_dark(monkeypatch, env, dark, [_heads(2_000, 20), _fallback(env)], hits=[2_025])
    values = env["inserts"][0]
    assert values[9] is not None  # block_time is separate from detected_at
    assert json.loads(values[13])["backfill"] is True   # source_valuation_json; source_slot follows it
    assert values[-1] == 2_025                           # fill timing: the source block (2026-10-02)


def test_unavailable_dark_block_time_keeps_frontier_and_hides_provider_key(monkeypatch, env, dark):
    from test_onchain_evm_watch_backfill import _getlogs
    post = _getlogs(env, [2025])
    def without_time(url, method, params):
        if method == "eth_getBlockByNumber":
            raise RuntimeError("https://provider.invalid/v2/FAKESECRET1234567890")
        return post(url, method, params)
    _run_dark(monkeypatch, env, dark, [_heads(2000, 20), _fallback(env)], post=without_time)
    beat = env["beat_after_dark"]
    assert beat["last_block"] < 2025 and env["inserts"] == []
    assert "FAKESECRET1234567890" not in "\n".join(env["lines"])


def test_the_dark_poll_beat_keeps_the_last_block_it_read(monkeypatch, env, dark):
    def refuse(url, method, params):
        raise RuntimeError("HTTP 429")
    _run_dark(monkeypatch, env, dark, [_heads(2_000, 20), _fallback(env)], post=refuse)
    assert env["beat_after_dark"]["mode"] == "poll"
    assert env["beat_after_dark"]["last_block"] == 2_019, "nothing read: a restart starts at 2020"


def test_a_resumed_push_does_not_reread_what_the_dark_poll_read(monkeypatch, env, dark):
    _run_dark(monkeypatch, env, dark, [_heads(2_000, 20), _fallback(env), _reconnect, _heads(2_050, 20)])
    after_dark = [p for p in env["pages"] if p[0] > 2_040 or p[1] > 2_040]
    assert after_dark and min(lo for lo, _hi in after_dark) == 2_041
    assert sum(1 for lo, hi in env["pages"] if lo <= 2_030 <= hi) == 1, "2030 was read once"


def test_a_pending_reconnect_gap_holds_the_heartbeats_last_block(monkeypatch, env, dark):
    """No block rate yet, so the reconnect backfill waits; a restart now must start at the gap."""
    monkeypatch.setattr(w, "_chain_blocks_per_s", lambda state: None)
    _run_dark(monkeypatch, env, dark, [_heads(2_000, 20), _reconnect, _heads(2_050, 5)])
    assert json.loads(env["hb"].read_text())["last_block"] == 2_019


def test_a_healthy_socket_never_polls_anyone(monkeypatch, env, dark):
    _run_dark(monkeypatch, env, dark, [_heads(2_000, 40)])
    assert env["pages"] == [] and env["rpc_getlogs"] == [] and env["alchemy_at"] == []


def test_explicit_log_host_recovers_a_dark_buy_without_an_alchemy_pool_member(monkeypatch, env, dark):
    monkeypatch.setattr(w, "_DARK_LOGS_LAST", [None])
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {})
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_BACKFILL_RPC_URL_4663", " " + ALCHEMY + " ")
    _run_dark(monkeypatch, env, dark, [_heads(2_000, 20), _fallback(env)], hits=[2_025])
    assert env["rpc_getlogs"] == [], "the restricted read host cannot serve transfer logs"
    assert len(env["inserts"]) == 1, "the explicit log host recovers the dark-window buy"
    assert env["beat_after_dark"]["last_block"] == 2_040


def test_without_a_backfill_host_the_dark_poll_is_todays(monkeypatch, env, dark):
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_BACKFILL_CHAINS", frozenset(), raising=False)
    _run_dark(monkeypatch, env, dark, [_heads(2_000, 20), _fallback(env)])
    assert env["pages"] == [] and env["rpc_getlogs"]
    assert int(env["rpc_getlogs"][0]["fromBlock"], 16) == 2_040, "today: from the poll's own head"


def test_a_long_dark_spell_reads_only_the_lanes_window(monkeypatch, env, dark):
    """Blocks older than the lane's 45 s are refused downstream: never page through them at 1/s."""
    env["head"] = 3_000                  # 2 blocks/s: 45 s is 90 blocks, far inside the 450-block cap
    _run_dark(monkeypatch, env, dark, [_heads(2_000, 20, rate=2.0), _fallback(env)])
    # The rate is measured from 1 s head timestamps (19 blocks / 9 s = 2.11/s here): ~95 blocks, and
    # nowhere near the 450-block cap's 2551.
    assert env["pages"] and 3_000 - 45 * 2.2 <= env["pages"][0][0] <= 3_000 - 45 * 1.9, env["pages"][:2]
