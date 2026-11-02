"""BSC (56) volume-death poll feed (spec docs/superpowers/specs/2026-10-01-memecoin-bsc-swap-feed-design.md).

A thread polls the operator's archive endpoint for Swap logs of the pools of open funded 56 positions
plus one always-busy canary pool, and feeds a per-chain RobinhoodVolumeState. A failed or over-range
poll is blindness, never silence; every blind verdict is a named, audited `blind_frozen:*` label.
"""
import json

import pytest

from sniper.services import memecoin_bsc_volume_poll as P
from sniper.services import memecoin_volume_death as vd
from sniper.services.memecoin_executed_pool import SOURCE_V3, executed_pool_from_receipt
from sniper.settings import memecoin as cfg

MINT = "0x" + "11" * 20
POOL = "0x" + "aa" * 20
POOL2 = "0x" + "bb" * 20
CANARY = "0x" + "cc" * 20
OPEN = "2026-10-01T12:00:00+00:00"


def _pos(pid=1, *, route=None, pair=POOL, opened=OPEN, mint=MINT):
    contract = {"classification": {"facts": {"liquidity_pair_address": pair}}}
    return {"id": pid, "network_id": "56", "mint": mint, "opened_at": opened,
            "funded_contract_json": json.dumps(contract),
            "evm_route_json": json.dumps(route if route is not None else {"pool_id": pair})}


class Chain:
    """A fake archive host: a head that moves, logs per block, failures on demand."""

    def __init__(self, head=1000):
        self.head, self.logs, self.fail, self.calls = head, {}, set(), []

    def call(self, method, params):
        self.calls.append((method, params))
        if method in self.fail:
            raise RuntimeError("host down")
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getLogs":
            flt = params[0]
            lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
            want = {a.lower() for a in flt["address"]}
            return [{"address": a, "blockNumber": hex(b), "topics": [P.SWAP_TOPICS[0]]}
                    for b in range(lo, hi + 1) for a in self.logs.get(b, ()) if a in want]
        raise AssertionError(method)


def _poller(chain, positions, *, discovered=frozenset({POOL}), state=None):
    state = state or vd.BscPollState(canary_gap_s=10.0)
    return P.BscVolumePoller(state, call=chain.call, positions_fn=lambda: positions,
                             discover_fn=lambda mint, quotes=(): discovered,
                             executed_pool_fn=lambda pos: None,
                             canary=CANARY), state


# --- settings ---

def test_the_archive_pool_parses_56(monkeypatch):
    assert cfg._evm_archive_rpc_pool({"MEMECOIN_EVM_ARCHIVE_RPC_URLS_56": "https://a.example/k"}) \
        == {"56": ("https://a.example/k",)}


@pytest.mark.parametrize("raw,want", [("", frozenset()), ("56", {"56"}), ("56, 4663", {"56", "4663"}),
                                      ("56,bsc", frozenset())])
def test_the_poll_switch_is_call_time_digits_only_default_empty(monkeypatch, raw, want):
    monkeypatch.setenv("MEMECOIN_VOLDEATH_POLL_CHAINS", raw)
    assert cfg.memecoin_voldeath_poll_chains() == frozenset(want)


def test_an_archive_url_on_a_funded_host_is_refused(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_56", "https://bsc.example/funded")
    assert P.archive_host_refusal("56", "https://BSC.example/other-key")
    assert P.archive_host_refusal("56", "https://archive.example/k") is None


# --- the PancakeSwap-V3 Swap topic ---

def test_a_pancakeswap_v3_swap_names_its_pool_in_the_receipt():
    receipt = {"logs": [{"address": POOL, "topics": [P.PANCAKE_V3_SWAP]}]}
    assert executed_pool_from_receipt("56", receipt) == (POOL, SOURCE_V3, 1)
    assert P.PANCAKE_V3_SWAP in P.SWAP_TOPICS


# --- the poller ---

def test_a_good_poll_covers_bound_pools_and_records_swaps_and_the_canary():
    chain = Chain()
    poller, state = _poller(chain, [_pos()])
    poller.tick(now_ts=100.0)                         # first tick: last = head - 1
    chain.head, chain.logs = 1003, {1002: [POOL, CANARY]}
    poller.tick(now_ts=101.5)
    snap = state.coverage_snapshot(101.5)
    assert snap["pools_bound"] == 1 and snap["pools_covered"] == 1 and snap["head_age_s"] == 0.0
    assert state._activity_at[POOL] == 101.5 and list(state._canary) == [101.5]
    flt = [p for m, p in chain.calls if m == "eth_getLogs"][-1][0]
    assert flt["fromBlock"] == hex(1001) and flt["toBlock"] == hex(1003)
    assert set(flt["address"]) == {POOL, CANARY} and flt["topics"] == [list(P.SWAP_TOPICS)]


def test_a_failed_poll_is_blindness_and_coverage_restarts_from_the_head():
    chain = Chain()
    poller, state = _poller(chain, [_pos()])
    poller.tick(now_ts=100.0)
    chain.head = 1002
    poller.tick(now_ts=101.0)
    assert state.coverage_snapshot(101.0)["pools_covered"] == 1
    chain.fail = {"eth_getLogs"}
    chain.head = 1004
    poller.tick(now_ts=102.0)
    assert state.coverage_snapshot(102.0) == {"pools_bound": 1, "pools_covered": 0,
                                              "head_age_s": None, "heads_seen": 0}
    chain.fail, chain.head = set(), 1010
    poller.tick(now_ts=103.0)                         # first good tick after a failure: no range
    flt = [p for m, p in chain.calls if m == "eth_getLogs"][-1][0]
    assert flt["fromBlock"] == hex(1010) and flt["toBlock"] == hex(1010)


def test_a_catch_up_over_max_range_is_blindness_never_back_filled():
    chain = Chain()
    poller, state = _poller(chain, [_pos()])
    poller.tick(now_ts=100.0)
    chain.head = 1000 + P.MAX_RANGE + 1
    poller.tick(now_ts=120.0)
    assert state.coverage_snapshot(120.0)["pools_covered"] == 0
    assert not [p for m, p in chain.calls if m == "eth_getLogs"
                and int(p[0]["toBlock"], 16) - int(p[0]["fromBlock"], 16) >= P.MAX_RANGE]


def test_bindings_take_every_known_pool_and_skip_an_unresolved_mint_and_a_curve():
    chain = Chain()
    poller, _state = _poller(chain, [_pos(1), _pos(2, route={"curve": "flap"}, pair=None)],
                             discovered=frozenset({POOL2}))
    assert poller.bindings() == {POOL: MINT, POOL2: MINT}
    failing, _ = _poller(chain, [_pos(1)], discovered=None)
    assert failing.bindings() == {}                    # discovery failed: nothing bound -> uncovered


# --- the decision (state) ---

def _covered_state(now_ts, *, canary=(), activity=None):
    state = vd.BscPollState(canary_gap_s=10.0)
    state.replace_bindings({POOL: MINT}, now_ts=now_ts - 200, covered=True)
    for t in range(int(now_ts) - 200, int(now_ts) + 1):
        state.record_head(t, now_ts=float(t))
    for t in canary:
        state.record_canary(now_ts=float(t))
    if activity is not None:
        state.record_pool_activity(POOL, now_ts=activity)
    return state


def _decide(state, *, now_ts, exit_s=60, confirm=lambda pools, a, b: [], rediscovered=None,
            rediscovered_age_s=5.0):
    """`rediscovered` is the pool set the POLLER last read for the mint (None: never read)."""
    from datetime import datetime, timezone
    pos = _pos(opened="2020-01-01T00:00:00+00:00")
    now = datetime.fromtimestamp(now_ts, timezone.utc).isoformat()
    found = {POOL} if rediscovered is None and rediscovered_age_s is not None else rediscovered
    if found is not None:
        state.record_rediscovery(MINT, found, now_ts=now_ts - rediscovered_age_s)
    return state.decision(pos, now_iso=now, exit_s=exit_s, confirm_fn=confirm)


NOW = 1_790_000_000.0


def test_a_quiet_vouched_pool_fires():
    state = _covered_state(NOW, canary=range(int(NOW) - 210, int(NOW) + 1, 5))
    assert _decide(state, now_ts=NOW)[0::2] == (True, "measured_state_still")


def test_no_fresh_head_is_poll_stale():
    state = vd.BscPollState(canary_gap_s=10.0)
    assert _decide(state, now_ts=NOW)[2] == "blind_frozen:poll_stale"


def test_an_unbound_mint_is_pool_uncovered():
    state = _covered_state(NOW)
    state.replace_bindings({}, now_ts=NOW)
    assert _decide(state, now_ts=NOW)[2] == "blind_frozen:pool_uncovered"


def test_a_canary_gap_inside_the_silence_is_unvouched():
    ticks = range(int(NOW) - 210, int(NOW) - 20, 5)
    state = _covered_state(NOW, canary=ticks)
    assert _decide(state, now_ts=NOW)[2] == "blind_frozen:poll_unvouched"


@pytest.mark.parametrize("gap_s", [10.0, 15.0, 60.0])
def test_canary_recovery_requires_a_new_full_silence_window(gap_s):
    state = vd.BscPollState(canary_gap_s=gap_s)
    state.replace_bindings({POOL: MINT}, now_ts=NOW - 200, covered=True)
    state.record_canary(now_ts=NOW - 200)
    recovered = NOW - 30
    for t in range(int(recovered), int(NOW) + 1, 5):
        state.record_head(t, now_ts=float(t))
        state.record_canary(now_ts=float(t))
    confirms = []
    confirm = lambda *args: confirms.append(args) or []
    assert _decide(state, now_ts=NOW - 0.001, exit_s=30, confirm=confirm) == (
        False, pytest.approx(29.999), "measured")
    assert confirms == []
    assert _decide(state, now_ts=NOW, exit_s=30, confirm=confirm) == (
        True, 30.0, "measured_state_still")
    assert len(confirms) == 1


def test_first_canary_after_a_dark_start_restarts_only_existing_coverage():
    state = vd.BscPollState()
    state.replace_bindings({POOL: MINT, POOL2: MINT}, now_ts=NOW - 100)
    state.record_coverage(POOL, now_ts=NOW - 100)
    state.record_canary(now_ts=NOW - 20)
    assert state._covered_at == {POOL: NOW - 20}
    state.record_coverage(POOL2, now_ts=NOW)
    state.record_canary(now_ts=NOW - 5)
    assert state._covered_at == {POOL: NOW - 5, POOL2: NOW}


@pytest.mark.parametrize("activity_at_head", [False, True])
@pytest.mark.parametrize("late_swap", [None, "next", "latest"])
def test_bsc_confirm_excludes_the_seen_swap_but_covers_every_later_block(late_swap, activity_at_head):
    chain = Chain()
    poller, state = _poller(chain, [_pos()])
    poller.last = chain.head - 3
    last_swap_block = chain.head if activity_at_head else chain.head - 1
    chain.logs[last_swap_block] = [POOL]
    chain.logs.setdefault(chain.head, []).append(CANARY)
    for t in range(31):
        if t:
            chain.head += 3
            chain.logs[chain.head] = [CANARY]
        assert poller.tick(now_ts=NOW + t + (0.05 if t == 30 else 0)) == "ok"
    if late_swap:
        # A log omitted by an earlier poll must still be caught by the final confirm.
        late_block = chain.head if late_swap == "latest" else last_swap_block + 1
        chain.logs.setdefault(late_block, []).append(POOL)
    confirm = P.confirm_logs_fn(chain.call, CANARY)
    fire, silence, why = _decide(state, now_ts=NOW + 30, exit_s=30, confirm=confirm)
    assert (fire, silence, why) == (
        not late_swap, 30.0, "state_moving" if late_swap else "measured_state_still")
    flt = chain.calls[-1][1][0]
    assert flt["fromBlock"] == hex(last_swap_block + 1)
    assert flt["toBlock"] == hex(chain.head)
    if late_swap:
        assert state._activity_blocks[POOL] == late_block
        for t in range(31, 61):
            chain.head += 3
            chain.logs[chain.head] = [CANARY]
            assert poller.tick(now_ts=NOW + t) == "ok"
        assert _decide(state, now_ts=NOW + 60, exit_s=30, confirm=confirm) == (
            True, 30.0, "measured_state_still")


def test_block_boundary_is_the_latest_activity_across_all_pools():
    state = _covered_state(NOW, canary=range(int(NOW) - 210, int(NOW) + 1, 5))
    state.bind_more({POOL2}, MINT)
    state.record_coverage(POOL2, now_ts=NOW - 100)
    state.record_pool_activity(POOL, now_ts=NOW - 60, block_number="0x64")
    state.record_pool_activity(POOL2, now_ts=NOW - 60, block_number="0x66")
    seen = []
    assert _decide(state, now_ts=NOW, rediscovered={POOL, POOL2},
                   confirm=lambda pools, first, last: seen.append(first) or [])[0]
    assert seen == [103]
    state.replace_bindings({POOL2: MINT}, now_ts=NOW)
    assert POOL not in state._activity_blocks
    state.record_pool_activity(POOL2, now_ts=NOW, block_number="invalid")
    assert POOL2 not in state._activity_blocks


def test_a_new_pool_at_the_threshold_binds_it_and_restarts_the_clock():
    state = _covered_state(NOW, canary=range(int(NOW) - 210, int(NOW) + 1, 5))
    assert _decide(state, now_ts=NOW, rediscovered={POOL, POOL2})[2] == \
        "blind_frozen:pool_set_changed"
    assert state._bindings[POOL2] == MINT and POOL2 not in state._covered_at
    assert _decide(state, now_ts=NOW + 1)[2] == "blind_frozen:pool_uncovered"


def test_a_stale_or_missing_rediscovery_is_pool_uncovered_with_no_network():
    """Gamma MEDIUM on 7a93d73c9: the sweep makes no discovery call; it reads the poller's set and
    treats a set older than REDISCOVER_MAX_AGE_S (or none) as unknown."""
    state = _covered_state(NOW, canary=range(int(NOW) - 210, int(NOW) + 1, 5))
    assert _decide(state, now_ts=NOW, rediscovered_age_s=None)[2] == "blind_frozen:pool_uncovered"
    state = _covered_state(NOW, canary=range(int(NOW) - 210, int(NOW) + 1, 5))
    assert _decide(state, now_ts=NOW, rediscovered_age_s=vd.REDISCOVER_MAX_AGE_S + 1)[2] == \
        "blind_frozen:pool_uncovered"


def test_the_confirm_must_see_the_canary():
    chain = Chain(head=NOW)
    confirm = P.confirm_logs_fn(chain.call, CANARY)
    assert confirm([POOL], 1, 2) is None                 # canary silent: unreadable, not silence
    chain.logs = {2: [CANARY]}
    assert confirm([POOL], 1, 2) == []
    chain.logs = {2: [CANARY, POOL]}
    assert [l["address"] for l in confirm([POOL], 1, 2)] == [POOL]


# --- routing and isolation ---

def test_a_poll_chain_decides_on_its_own_state_and_wins_over_the_tape(monkeypatch):
    monkeypatch.setenv("MEMECOIN_VOLDEATH_POLL_CHAINS", "56")
    monkeypatch.setenv("MEMECOIN_VOLDEATH_TAPE_CHAINS", "56")
    seen = []
    state = vd.BscPollState(canary_gap_s=10.0)
    monkeypatch.setitem(vd.EVM_POLL_STATE, "56", state)
    monkeypatch.setattr(vd, "tape_exit_decision", lambda *a, **k: seen.append("tape") or (False, None, "x"))
    out = vd._armed_exit_decision(None, _pos(), now_iso="2026-10-01T12:05:00+00:00", held_s=300,
                                  exit_s=60, min_opened_at="2026-01-01")
    assert out[2] == "blind_frozen:poll_stale" and seen == []
    assert vd.ROBINHOOD_VOLUME_STATE is not state and vd.EVM_POLL_STATE["56"] is state


def test_an_unlisted_56_keeps_todays_path(monkeypatch):
    monkeypatch.setenv("MEMECOIN_VOLDEATH_POLL_CHAINS", "")
    monkeypatch.setenv("MEMECOIN_VOLDEATH_TAPE_CHAINS", "")
    out = vd._armed_exit_decision(None, _pos(), now_iso="2026-10-01T12:05:00+00:00", held_s=300,
                                  exit_s=60, min_opened_at="2026-01-01", stream_ok=False)
    assert out[2] == "blind_frozen"


def test_every_poll_label_is_audited():
    for label in ("blind_frozen:poll_stale", "blind_frozen:pool_uncovered",
                  "blind_frozen:poll_unvouched", "blind_frozen:pool_set_changed"):
        assert label in vd.POST_THRESHOLD_REASONS


def test_the_runner_starts_the_poller_only_for_a_listed_chain_with_an_archive_url():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    loop = (root / "scripts" / "memecoin_runner_loop.py").read_text()
    assert "start_bsc_volume_poller(" in loop and "_start_bsc_volume_poll()" in loop
    assert 'name="memecoin-bsc-volume-poll"' in (root / "sniper" / "services" /
                                                "memecoin_bsc_volume_poll.py").read_text()


def test_the_poller_does_not_start_unlisted_without_a_url_or_on_a_funded_host(monkeypatch):
    started = []

    def factory(**kw):
        class T:
            def start(self):
                started.append(kw["name"])
        return T()
    args = dict(positions_fn=lambda: [], entry_tx_fn=lambda pos: None, thread_factory=factory,
                log=lambda msg: None)
    monkeypatch.setattr(P, "archive_url", lambda chain="56": "https://archive.example/k")
    monkeypatch.setattr(P, "make_call", lambda url, **kw: (lambda m, p: "0x" + "00" * 12 + "cc" * 20))
    monkeypatch.setenv("MEMECOIN_VOLDEATH_POLL_CHAINS", "")
    assert P.start_bsc_volume_poller(**args) is False and started == []   # unlisted, URL fine
    monkeypatch.setenv("MEMECOIN_VOLDEATH_POLL_CHAINS", "56")
    monkeypatch.setattr(P, "archive_url", lambda chain="56": None)
    assert P.start_bsc_volume_poller(**args) is False
    monkeypatch.setattr(P, "archive_url", lambda chain="56": "https://funded.example/k")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_56", "https://funded.example/x")
    assert P.start_bsc_volume_poller(**args) is False
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_56", "https://other.example/x")
    monkeypatch.setattr(P, "make_call", lambda url, **kw: (lambda m, p: "0x" + "00" * 12 + "cc" * 20))
    assert P.start_bsc_volume_poller(**args) is True and started == ["memecoin-bsc-volume-poll"]


def test_route_tokens_next_to_the_coin_are_discovery_quotes():
    """56 routes hop through other base tokens (67623: WBNB -> 0x4902c5eb... -> coin), so the coin's
    pools pair with tokens outside WBNB/USDT/BUSD/USDC. Every token the route names is a quote."""
    base = "0x" + "49" * 20
    pos = _pos(route={"path": [P.WBNB, base, MINT], "protocol": "uniswap_v2"})
    assert P.route_quote_tokens(pos) == (P.WBNB, base)
    seen = {}
    chain = Chain()
    poller = P.BscVolumePoller(vd.BscPollState(), call=chain.call, positions_fn=lambda: [pos],
                               discover_fn=lambda mint, quotes=(): seen.setdefault("q", quotes) and frozenset(),
                               executed_pool_fn=lambda p: None, canary=CANARY)
    poller.bindings()
    assert base in seen["q"] and poller.quotes[MINT] == seen["q"]


def test_a_canary_that_starts_a_few_seconds_after_the_silence_still_vouches():
    """Coverage starts on a tick whose range may hold no canary swap (13 of 367 soak polls); the
    first canary within the gap of the silence start vouches, or no position could ever fire."""
    state = _covered_state(NOW, canary=range(int(NOW) - 197, int(NOW) + 1, 5))   # silence from NOW-200
    assert _decide(state, now_ts=NOW)[0::2] == (True, "measured_state_still")


def test_with_no_open_56_position_the_poller_is_idle_and_calls_nothing():
    chain = Chain()
    poller, state = _poller(chain, [])
    assert poller.tick(now_ts=100.0) == "idle" and chain.calls == []
    assert state.coverage_snapshot(100.0)["heads_seen"] == 0


def test_the_poller_rediscovers_bound_mints_and_binds_a_new_pool():
    chain = Chain()
    found = {"pools": frozenset({POOL})}
    poller = P.BscVolumePoller(vd.BscPollState(canary_gap_s=10.0), call=chain.call,
                               positions_fn=lambda: [_pos()],
                               discover_fn=lambda mint, quotes=(): found["pools"],
                               executed_pool_fn=lambda p: None, canary=CANARY)
    poller.tick(now_ts=100.0)
    assert poller.state._rediscovered[MINT][0] == 100.0
    found["pools"] = frozenset({POOL, POOL2})
    poller.tick(now_ts=100.0 + P.REDISCOVER_EVERY_S - 1)        # not due yet
    assert POOL2 not in poller.state._bindings
    poller.tick(now_ts=100.0 + P.REDISCOVER_EVERY_S + 1)
    assert poller.state._bindings[POOL2] == MINT
    assert poller.state._rediscovered[MINT] == (100.0 + P.REDISCOVER_EVERY_S + 1, frozenset({POOL, POOL2}))
    found["pools"] = None                                         # a failed refresh keeps the old stamp
    poller.tick(now_ts=200.0)
    assert poller.state._rediscovered[MINT][0] == 100.0 + P.REDISCOVER_EVERY_S + 1


def test_a_partial_discovery_is_unknown_never_a_smaller_set():
    """Gamma L1: one failed sub-call must not leave a live pool unbound (silence on the rest)."""
    from eth_abi import encode
    ok_word = b"\x00" * 12 + bytes.fromhex("aa" * 20)
    n = len(P.V2_FACTORIES) * len(P.QUOTES) + sum(len(f) for _f, f in P.V3_FACTORIES) * len(P.QUOTES)
    results = [(True, ok_word)] * (n - 1) + [(False, b"")]
    raw = "0x" + encode(["(bool,bytes)[]"], [results]).hex()
    assert P.discover_pools(lambda m, p: raw, MINT) is None
    full = "0x" + encode(["(bool,bytes)[]"], [[(True, ok_word)] * n]).hex()
    assert P.discover_pools(lambda m, p: full, MINT) == frozenset({POOL})


@pytest.mark.parametrize("name", ["MEMECOIN_EVM_WS_URL_56", "MEMECOIN_ONCHAIN_EVM_RPC_URL_56",
                                  "MEMECOIN_CAPTURE_EVM_READ_URL_56"])
def test_every_funded_56_url_name_counts(monkeypatch, name):
    """Gamma L2: the funded host set is derived from the env, not two listed names."""
    monkeypatch.setenv(name, "wss://funded.example/k")
    assert P.archive_host_refusal("56", "https://funded.example/other")
    monkeypatch.setenv("MEMECOIN_RESEARCH_WS_URL_56", "wss://research.example/k")
    assert P.archive_host_refusal("56", "https://research.example/k2") is None


def test_the_confirm_caller_has_a_hard_short_timeout(monkeypatch):
    seen = {}

    class R:
        def read(self):
            return b'{"jsonrpc":"2.0","id":1,"result":"0x1"}'
    monkeypatch.setattr(P.urllib.request, "urlopen", lambda req, timeout=None: seen.update(t=timeout) or R())
    assert P.make_call("https://a.example", timeout_s=P.CONFIRM_TIMEOUT_S)("eth_blockNumber", []) == "0x1"
    assert seen["t"] == P.CONFIRM_TIMEOUT_S <= 2.0


def test_the_sweep_confirm_runs_on_the_short_timeout_caller(monkeypatch):
    made = []

    def make_call(url, *, timeout_s=P.POLL_TIMEOUT_S):
        def call(method, params):
            made.append((timeout_s, method))
            if method == "eth_call":
                return "0x" + "00" * 12 + "cc" * 20          # the canary lookup at start
            return [{"address": CANARY}]
        return call
    monkeypatch.setenv("MEMECOIN_VOLDEATH_POLL_CHAINS", "56")
    monkeypatch.setattr(P, "archive_url", lambda chain="56": "https://archive.example/k")
    monkeypatch.setattr(P, "make_call", make_call)

    class T:
        def __init__(self, **kw):
            pass

        def start(self):
            pass
    assert P.start_bsc_volume_poller(positions_fn=lambda: [], entry_tx_fn=lambda p: None,
                                     thread_factory=T, log=lambda m: None)
    made.clear()
    assert P.confirm_fn_for("56")([POOL], 1, 2) == []
    assert made == [(P.CONFIRM_TIMEOUT_S, "eth_getLogs")]


def test_a_failed_refresh_is_retried_next_tick_and_never_refreshes_the_stamp():
    """Gamma's survivor on b52f8e895: a failed re-read must not count as a read."""
    chain, calls = Chain(), []

    def discover(mint, quotes=()):
        calls.append(mint)
        return frozenset({POOL}) if len(calls) == 1 else None
    poller = P.BscVolumePoller(vd.BscPollState(canary_gap_s=10.0), call=chain.call,
                               positions_fn=lambda: [_pos()], discover_fn=discover,
                               executed_pool_fn=lambda p: None, canary=CANARY)
    poller.tick(now_ts=100.0)
    poller.tick(now_ts=100.0 + P.REDISCOVER_EVERY_S)          # due: fails
    poller.tick(now_ts=100.0 + P.REDISCOVER_EVERY_S + 1.5)    # retried at once, fails again
    assert len(calls) == 3
    assert poller._rediscovered_at[1] == 100.0 and poller.state._rediscovered[MINT][0] == 100.0


@pytest.mark.parametrize("raises", [False, True], ids=["unknown", "exception"])
def test_resolver_failed_initial_discovery_does_not_starve_another_position(raises):
    other = "0x" + "22" * 20
    positions = [_pos(), _pos(2, mint=other, pair=POOL2)]
    attempts = []

    def discover(mint, quotes=()):
        attempts.append(mint)
        if mint == MINT:
            if raises:
                raise RuntimeError("discovery unavailable")
            return None
        return frozenset({POOL2})

    poller = P.BscVolumePoller(vd.BscPollState(), call=Chain().call,
                               positions_fn=lambda: positions, discover_fn=discover,
                               executed_pool_fn=lambda p: None, canary=CANARY)
    assert poller.bindings(100) == {}
    assert poller.bindings(101.5) == {POOL2: other}
    assert poller.bindings(103) == {POOL2: other}
    assert attempts == [MINT, other, MINT]
    assert poller._rediscovered_at == {2: 101.5}
    assert MINT not in poller.state._rediscovered


def test_resolver_failed_initial_receipt_does_not_starve_another_position():
    other = "0x" + "22" * 20
    positions = [_pos(), _pos(2, mint=other, pair=POOL2)]
    attempts, receipts = [], []

    def discover(mint, quotes=()):
        attempts.append(mint)
        return frozenset()

    def receipt(pos):
        receipts.append(pos["id"])
        if pos["id"] == 1:
            raise RuntimeError("receipt unavailable")
        return POOL2

    poller = P.BscVolumePoller(vd.BscPollState(), call=Chain().call,
                               positions_fn=lambda: positions, discover_fn=discover,
                               executed_pool_fn=receipt, canary=CANARY)
    assert poller.bindings(100) == {}
    assert poller.bindings(101.5) == {POOL2: other}
    assert poller.bindings(103) == {POOL2: other}
    assert attempts == [MINT, other, MINT] and receipts == [1, 2, 1]
    assert poller._rediscovered_at == {2: 101.5}
    assert MINT not in poller.state._rediscovered


@pytest.mark.parametrize("raises", [False, True], ids=["unknown", "exception"])
def test_resolver_failed_refresh_does_not_starve_another_due_refresh(raises):
    other, added = "0x" + "22" * 20, "0x" + "dd" * 20
    positions = [_pos(), _pos(2, mint=other, pair=POOL2)]
    attempts, refreshing = [], False

    def discover(mint, quotes=()):
        attempts.append(mint)
        if refreshing and mint == MINT:
            if raises:
                raise RuntimeError("refresh unavailable")
            return None
        return frozenset({added}) if refreshing else frozenset()

    poller = P.BscVolumePoller(vd.BscPollState(), call=Chain().call,
                               positions_fn=lambda: positions, discover_fn=discover,
                               executed_pool_fn=lambda p: None, canary=CANARY)
    poller.bindings(100)
    poller.bindings(101.5)
    attempts.clear()
    refreshing = True
    for tick in (122, 123.5):
        count = len(attempts)
        bindings = poller.bindings(tick)
        assert len(attempts) == count + 1
        assert bindings[POOL] == MINT and bindings[POOL2] == other
    assert sorted(attempts) == sorted([MINT, other])
    assert bindings[added] == other
    assert poller._rediscovered_at[1] == 100
    assert poller.state._rediscovered[MINT] == (100, frozenset({POOL}))
    assert poller._rediscovered_at[2] in (122, 123.5)
    assert poller.state._rediscovered[other][1] == frozenset({POOL2, added})


def test_resolver_failed_due_refresh_does_not_starve_a_new_position():
    other = "0x" + "22" * 20
    positions, attempts = [_pos()], []

    def discover(mint, quotes=()):
        attempts.append(mint)
        return None if mint == MINT and len(attempts) > 1 else frozenset()

    poller = P.BscVolumePoller(vd.BscPollState(), call=Chain().call,
                               positions_fn=lambda: positions, discover_fn=discover,
                               executed_pool_fn=lambda p: None, canary=CANARY)
    assert poller.bindings(100) == {POOL: MINT}
    positions.append(_pos(2, mint=other, pair=POOL2))
    for tick in (121, 122.5):
        count = len(attempts)
        bindings = poller.bindings(tick)
        assert len(attempts) == count + 1
    assert bindings == {POOL: MINT, POOL2: other}
    assert sorted(attempts[1:]) == sorted([MINT, other])
    assert poller._rediscovered_at[1] == 100
    assert poller.state._rediscovered[MINT][0] == 100


def test_resolver_fair_retries_survive_new_arrivals_clock_ties_and_cleanup():
    positions, attempts = [_pos()], []

    def discover(mint, quotes=()):
        attempts.append(mint)
        return None if mint == MINT else frozenset()

    poller = P.BscVolumePoller(vd.BscPollState(), call=Chain().call,
                               positions_fn=lambda: positions, discover_fn=discover,
                               executed_pool_fn=lambda p: None, canary=CANARY)
    for pid in range(2, 8):
        positions.append(_pos(pid, mint="0x" + f"{pid:040x}", pair="0x" + f"{pid + 100:040x}"))
        count = len(attempts)
        poller.bindings(100)  # fairness must not depend on advancing wall time
        assert len(attempts) == count + 1
    assert attempts.count(MINT) >= 2
    assert len(set(attempts)) >= 3
    assert 1 not in poller._pools and 1 not in poller._rediscovered_at
    positions.clear()
    assert poller.bindings(101) == {}
    assert poller._pools == {} and poller._rediscovered_at == {}
    assert poller._resolve_order == []
    count = len(attempts)
    positions.append(_pos(8, mint="0x" + "88" * 20, pair=POOL2))
    assert poller.bindings(102) == {POOL2: "0x" + "88" * 20}
    assert len(attempts) == count + 1 and attempts[-1] == "0x" + "88" * 20
