"""bsc_v2_live records the BSC fast lane's WBNB -> quote hop pairs and seeds each once per coverage
window. The seed reads the operator's keyed dRPC 56 account through its one cross-process pacer
(Alpha 2026-10-02: 1rpc.io answered -32001 "usage limit" on every call). A quota answer or no host
ends the seed for the window; a rate limit pauses the account and the seed backs off and retries."""
import io
import urllib.error

import pytest

from sniper.services import memecoin_bsc_v2_live as tape

KEYED = "https://lb.drpc.org/ogrpc?network=bsc&dkey=SECRET"


@pytest.fixture(autouse=True)
def _seed_host(monkeypatch):
    monkeypatch.setenv(tape.HOP_SEED_URL_VAR, KEYED)


def test_a_hop_pairs_sync_is_recorded_beside_the_held_pools():
    hops = {"0x" + "4b" * 20: "0xq"}
    assert tape.records_sync("0x" + "4b" * 20, held={}, hops=hops)
    assert tape.records_sync("0x" + "11" * 20, held={"0x" + "11" * 20: "0xm"}, hops=hops)
    assert not tape.records_sync("0x" + "22" * 20, held={}, hops=hops)


def test_the_seed_reads_each_hop_pair_at_one_block_on_the_research_host_one_a_second():
    calls, sleeps = [], []

    def rpc(method, params, url):
        calls.append((method, params, url))
        r0, r1 = 7 * 10 ** 21, 3 * 10 ** 20
        return "0x" + format(r0, "064x") + format(r1, "064x") + format(1, "064x")
    hops = {"0x" + "4b" * 20: "0xq1", "0x" + "4c" * 20: "0xq2"}
    got, stopped = tape.seed_hop_reserves(hops, 1234, rpc=rpc, sleep=sleeps.append)
    assert not stopped and got == [("0x" + "4b" * 20, 7 * 10 ** 21, 3 * 10 ** 20),
                   ("0x" + "4c" * 20, 7 * 10 ** 21, 3 * 10 ** 20)]
    assert all(url == KEYED for _m, _p, url in calls)
    assert [p for _m, p, _u in calls] == [[{"to": "0x" + "4b" * 20, "data": "0x0902f1ac"}, hex(1234)],
                                          [{"to": "0x" + "4c" * 20, "data": "0x0902f1ac"}, hex(1234)]]
    assert sleeps == [tape.HOP_SEED_GAP_S] and tape.HOP_SEED_GAP_S >= 1.0


@pytest.mark.parametrize("limit", [
    urllib.error.HTTPError(KEYED, 429, "Too Many Requests", {}, io.BytesIO(b"")),
    RuntimeError("BSC observation RPC failed: {'code': 15, 'message': 'free plan rate limit'}"),
    RuntimeError("BSC observation RPC failed: {'code': -32005, 'message': 'limit exceeded'}")])
def test_a_rate_limit_keeps_what_it_read_and_backs_off_without_ending_the_window(limit):
    """Alpha 2026-10-02: the pacer is shared, so a 429 may be another reader's; the seed retries."""
    calls = []

    def rpc(method, params, url):
        calls.append(params[0]["to"])
        if len(calls) == 2:
            raise limit
        return "0x" + format(5, "064x") + format(6, "064x") + format(1, "064x")
    hops = {"0x" + "4b" * 20: "a", "0x" + "4c" * 20: "b", "0x" + "4d" * 20: "c"}
    got, stopped = tape.seed_hop_reserves(hops, 1, rpc=rpc, sleep=lambda s: None)
    assert not stopped and got == [("0x" + "4b" * 20, 5, 6)] and len(calls) == 2


@pytest.mark.parametrize("quota", [
    "{'code': -32001, 'message': \"You've reached the usage limit for your current plan\"}",
    "{'code': -32000, 'message': 'monthly quota exceeded'}"])
def test_a_quota_answer_ends_the_seed_for_the_window(quota):
    def rpc(method, params, url):
        raise RuntimeError("BSC observation RPC failed: " + quota)
    got, stopped = tape.seed_hop_reserves({"0x" + "4b" * 20: "a", "0x" + "4c" * 20: "b"}, 1, rpc=rpc,
                                          sleep=lambda s: None)
    assert stopped and got == []


def test_no_seed_host_ends_the_seed_for_the_window_without_a_call(monkeypatch):
    monkeypatch.delenv(tape.HOP_SEED_URL_VAR, raising=False)
    got, stopped = tape.seed_hop_reserves({"0x" + "4b" * 20: "a"}, 1,
                                          rpc=lambda *a: pytest.fail("no host, no call"), sleep=lambda s: None)
    assert stopped and got == []


def test_the_default_seed_call_takes_the_accounts_pacer_and_never_prints_the_key(monkeypatch, capsys):
    import sniper.services.memecoin_research_pacer as rp
    took = []
    monkeypatch.setattr(rp.SharedPacer, "acquire", lambda self: took.append(self.path))
    monkeypatch.setattr(rp.SharedPacer, "pause", lambda self, s: None)
    asked = []

    def fake_rpc(method, params, url, timeout=3):
        asked.append(url)
        # requests names the URL, sometimes re-encoded (parameters in another order)
        raise RuntimeError("transport to %s failed; retry url: https://lb.drpc.org/ogrpc?dkey=SECRET"
                           "&network=bsc" % url)
    monkeypatch.setattr(tape, "_rpc", fake_rpc)
    got, stopped = tape.seed_hop_reserves({"0x" + "4b" * 20: "a"}, 1, sleep=lambda s: None)
    assert asked == [KEYED] and len(took) == 1 and not stopped and got == []
    assert "SECRET" not in capsys.readouterr().out


def test_a_paused_account_skips_the_seed_without_blocking_a_tape_worker(monkeypatch):
    import time
    import sniper.services.memecoin_research_pacer as rp
    monkeypatch.setattr(rp.SharedPacer, "paused_until", lambda self: time.time() + 60)
    monkeypatch.setattr(rp.SharedPacer, "acquire", lambda self: pytest.fail("blocked on a paused account"))
    got, stopped = tape.seed_hop_reserves({"0x" + "4b" * 20: "a"}, 1, sleep=lambda s: None)
    assert (got, stopped) == ([], False)


def test_a_seed_never_overrides_a_real_sync_in_its_block(tmp_path):
    c = tape.connect(str(tmp_path / "t.db"))
    pool = "0x" + "4b" * 20
    tape.record_sync(c, pool, 100, "0x" + "aa" * 32, 1, 2, 1.0, idx=3)
    tape.record_seed(c, pool, 100, "0x" + "aa" * 32, 9, 9, 2.0)
    assert tuple(c.execute("SELECT reserve0, reserve1 FROM syncs WHERE pool=?", (pool,)).fetchone()) == ("1", "2")
    tape.record_seed(c, "0x" + "4c" * 20, 100, "0x" + "aa" * 32, 9, 8, 2.0)
    assert tuple(c.execute("SELECT reserve0, reserve1, idx FROM syncs WHERE pool=?",
                           ("0x" + "4c" * 20,)).fetchone()) == ("9", "8", -1)


def test_the_run_loop_records_hop_syncs_and_seeds_once_per_coverage_window(tmp_path, monkeypatch):
    import json
    import time as _time
    from sniper.execution import evm_wallet
    from sniper.services import memecoin_bsc_fast_lane as bsc
    hop, other = "0x" + "4b" * 20, "0x" + "22" * 20
    monkeypatch.setattr(tape, "db_path", lambda: tmp_path / "tape.db")
    monkeypatch.setattr(bsc, "hop_pairs", lambda: {hop: "0xq"})
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE", "runner_shadow")
    monkeypatch.setattr(tape, "_open_pools", lambda: ({}, {}))
    monkeypatch.setattr(evm_wallet, "quote_usd_price", lambda chain: 600.0)
    monkeypatch.setattr(tape, "HOP_SEED_GAP_S", 0.0)
    seeds = []

    def rpc(method, params, url=tape.RPC, timeout=3):
        if method == "eth_chainId":
            return "0x38"
        seeds.append((params, url))
        return "0x" + format(70, "064x") + format(30, "064x") + format(1, "064x")
    monkeypatch.setattr(tape, "_rpc", rpc)
    word = lambda v: format(v, "064x")
    head = {"number": "0x64", "hash": "0x" + "64" * 32, "timestamp": hex(1790000100),
            "parentHash": "0x" + "63" * 32}

    def sync(pool, r0, r1, idx):
        return {"address": pool, "topics": [tape.V2_SYNC], "data": "0x" + word(r0) + word(r1),
                "blockNumber": "0x64", "blockHash": "0x" + "64" * 32,
                "transactionHash": "0x" + "ab" * 32, "logIndex": hex(idx)}
    msgs = [{"id": 1, "result": "0x38"}, {"id": 2, "result": "s2"}, {"id": 3, "result": "s3"},
            {"id": 4, "result": "s4"}, {"id": 5, "result": "s5"},
            {"params": {"subscription": "s2", "result": head}},
            {"params": {"subscription": "s4", "result": sync(other, 1, 2, 4)}},
            {"params": {"subscription": "s4", "result": sync(hop, 11, 22, 5)}}]

    class WS:
        def __init__(self):
            self.waits = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send(self, text):
            pass

        def ping(self):
            pass

        def recv(self, timeout=None):
            if msgs:
                return json.dumps(msgs.pop(0))
            self.waits += 1
            if self.waits < 40:
                _time.sleep(0.01)                 # let the seed job finish on its worker
                raise TimeoutError()
            raise KeyboardInterrupt               # ends run(): not an Exception it retries
    import websockets.sync.client as wsc
    monkeypatch.setattr(wsc, "connect", lambda *a, **k: WS())
    with pytest.raises(KeyboardInterrupt):
        tape.run(ws_url="wss://test", rpc_url="https://test")
    c = tape.connect(str(tmp_path / "tape.db"))
    rows = {r[0]: tuple(r[1:]) for r in c.execute("SELECT pool, reserve0, reserve1, idx FROM syncs")}
    # The hop pair's own Sync is recorded and the later seed of the same block never overrides it;
    # the unwatched pool is not recorded.
    assert rows == {hop: ("11", "22", 5)}
    assert seeds == [([{"to": hop, "data": "0x0902f1ac"}, "0x64"], KEYED)]


# ---- Alpha/Gamma on 8423374ac: one seed per window, failed pairs retried later in it ----

A, B = "0x" + "4b" * 20, "0x" + "4c" * 20


def test_a_window_seeds_every_pair_once_and_a_pair_seen_trading_needs_no_seed():
    s = tape.HopSeeder({A: "q1", B: "q2"})
    s.window(1000.0, 100)
    assert s.due(0.0) and s.pending == {A, B} and s.height_for(100) == 100   # never before the start
    assert s.height_for(110) == 110 - tape.HOP_SEED_LAG_BLOCKS
    s.saw_sync(B)
    s.done([A], False, 5.0)
    assert not s.due(10 ** 9)
    s.window(1000.0, 140)                       # the same window again: nothing to redo
    assert not s.due(10 ** 9)
    s.window(2000.0, 300)                       # a new window: every pair again
    assert s.pending == {A, B} and s.height_for(300) == 300


def test_a_failed_pair_backs_off_and_a_stop_ends_the_window():
    s = tape.HopSeeder({A: "q1", B: "q2"})
    s.window(1000.0, 100)
    s.done([A], False, 50.0)
    assert s.pending == {B} and not s.due(50.0 + tape.HOP_SEED_BACKOFF_S[0] - 0.1)
    assert s.due(50.0 + tape.HOP_SEED_BACKOFF_S[0])
    s.done([], False, 60.0)
    assert not s.due(60.0 + tape.HOP_SEED_BACKOFF_S[1] - 0.1) and s.due(60.0 + tape.HOP_SEED_BACKOFF_S[1])
    s.done([], True, 70.0)
    assert not s.due(10 ** 9)


def test_the_lane_off_means_no_hop_pair_and_no_seed(monkeypatch):
    from sniper.services import memecoin_bsc_fast_lane as bsc
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE", "")
    assert tape.tape_hop_pairs() == {}
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE", "runner_shadow")
    assert tape.tape_hop_pairs() == bsc.hop_pairs() != {}


def test_the_seed_waits_longer_than_the_tapes_own_reads(monkeypatch):
    asked = []
    monkeypatch.setattr(tape, "_rpc", lambda method, params, url, timeout=3: asked.append(timeout) or (
        "0x" + format(5, "064x") + format(6, "064x")))
    tape.seed_hop_reserves({A: "q"}, 1, sleep=lambda s: None)
    assert asked == [tape.HOP_SEED_TIMEOUT_S] and tape.HOP_SEED_TIMEOUT_S > 3


def _drive_tape(monkeypatch, tmp_path, heads, rpc, *, waits_between=15, open_pools=True):
    """The tape's run loop over a fake WS: the subscriptions, then one head per entry of `heads`
    with `waits_between` empty receives after each (the seed job runs on its worker meanwhile)."""
    import json
    import time as _time
    from sniper.execution import evm_wallet
    from sniper.services import memecoin_bsc_fast_lane as bsc
    monkeypatch.setattr(tape, "db_path", lambda: tmp_path / "tape.db")
    monkeypatch.setattr(bsc, "hop_pairs", lambda: {A: "0xq"})
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE", "runner_shadow")
    # The account's pacer is pinned by its own tests; here it would outlive the loop on its workers.
    import sniper.services.memecoin_research_pacer as rp
    monkeypatch.setattr(rp.SharedPacer, "acquire", lambda self: None)
    monkeypatch.setattr(rp.SharedPacer, "pause", lambda self, s: None)
    if open_pools:
        monkeypatch.setattr(tape, "_open_pools", lambda: ({}, {}))
    monkeypatch.setattr(evm_wallet, "quote_usd_price", lambda chain: 600.0)
    monkeypatch.setattr(tape, "HOP_SEED_GAP_S", 0.0)
    monkeypatch.setattr(tape, "_rpc", rpc)
    script = [{"id": 1, "result": "0x38"}, {"id": 2, "result": "s2"}, {"id": 3, "result": "s3"},
              {"id": 4, "result": "s4"}, {"id": 5, "result": "s5"}]
    word = lambda v: format(v, "064x")
    for h in heads:
        if h == ("bare_head",):                   # a head message with no number or hash
            script.append({"params": {"subscription": "s2", "result": {"timestamp": hex(1790000000)}}})
            continue
        if isinstance(h, tuple):                  # ("sync", pool, height): that pool's own Sync
            _k, pool, height = h
            script.append({"params": {"subscription": "s4", "result": {
                "address": pool, "topics": [tape.V2_SYNC], "data": "0x" + word(11) + word(22),
                "blockNumber": hex(height), "blockHash": "0x%064x" % height,
                "transactionHash": "0x" + "ab" * 32, "logIndex": hex(3)}}})
            continue
        script.append({"params": {"subscription": "s2", "result": {
            "number": hex(h), "hash": "0x%064x" % h, "timestamp": hex(1790000000 + h),
            "parentHash": "0x%064x" % (h - 1)}}})
        script += [None] * waits_between

    class WS:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send(self, text):
            pass

        def ping(self):
            pass

        def recv(self, timeout=None):
            if not script:
                raise KeyboardInterrupt           # ends run(): not an Exception it retries
            msg = script.pop(0)
            if msg is None:
                _time.sleep(0.01)
                raise TimeoutError()
            return json.dumps(msg)
    import websockets.sync.client as wsc
    monkeypatch.setattr(wsc, "connect", lambda *a, **k: WS())
    with pytest.raises(KeyboardInterrupt):
        tape.run(ws_url="wss://test", rpc_url="https://test")
    c = tape.connect(str(tmp_path / "tape.db"))
    return {r[0]: tuple(r[1:]) for r in c.execute("SELECT pool, height, idx FROM syncs")}


def _reserves():
    return "0x" + format(70, "064x") + format(30, "064x") + format(1, "064x")


def test_three_heads_in_one_window_seed_once(tmp_path, monkeypatch):
    seeds = []

    def rpc(method, params, url=tape.RPC, timeout=3):
        if method == "eth_chainId":
            return "0x38"
        seeds.append(params[1])
        return _reserves()
    rows = _drive_tape(monkeypatch, tmp_path, [100, 101, 102, 103], rpc)
    assert seeds == ["0x64"] and rows == {A: (100, -1)}


def test_a_failed_seed_is_retried_later_in_the_window_at_the_then_current_head(tmp_path, monkeypatch):
    seeds = []
    monkeypatch.setattr(tape, "HOP_SEED_BACKOFF_S", (0.0,))

    def rpc(method, params, url=tape.RPC, timeout=3):
        if method == "eth_chainId":
            return "0x38"
        seeds.append(params[1])
        if len(seeds) == 1:
            raise RuntimeError("BSC observation RPC failed: {'code': -32000, 'message': 'header not found'}")
        return _reserves()
    rows = _drive_tape(monkeypatch, tmp_path, [100, 101, 102, 103, 104], rpc)
    assert seeds == ["0x64", "0x64"] and rows == {A: (100, -1)}   # head 101 - 2 floors at the start
    seeds.clear()
    monkeypatch.setattr(tape, "HOP_SEED_LAG_BLOCKS", 0)
    tape.connect(str(tmp_path / "tape.db")).execute("DELETE FROM syncs").connection.commit()
    rows = _drive_tape(monkeypatch, tmp_path, [200, 201, 202], rpc)
    assert seeds == ["0xc8", "0xc9"] and rows == {A: (201, -1)}  # the retry reads the newer head


def test_a_seed_that_hits_a_429_is_retried_and_a_later_success_seeds_the_pair(tmp_path, monkeypatch):
    """Alpha's pin (2026-10-02): a rate limit backs off on the 2/5/15/30/60 s schedule; it never
    ends the window. The account pause itself is the pacer's (not exercised here)."""
    import io
    import sniper.services.memecoin_research_pacer as rp
    monkeypatch.setattr(rp.SharedPacer, "pause", lambda self, s: None)
    seeds = []
    monkeypatch.setattr(tape, "HOP_SEED_BACKOFF_S", (0.0,))

    def rpc(method, params, url=tape.RPC, timeout=3):
        if method == "eth_chainId":
            return "0x38"
        seeds.append(params[1])
        if len(seeds) == 1:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(b""))
        return _reserves()
    rows = _drive_tape(monkeypatch, tmp_path, [100, 101, 102, 103], rpc)
    assert seeds == ["0x64", "0x64"] and rows == {A: (100, -1)}


def test_a_quota_answer_is_not_retried_in_that_window(tmp_path, monkeypatch):
    seeds = []
    monkeypatch.setattr(tape, "HOP_SEED_BACKOFF_S", (0.0,))

    def rpc(method, params, url=tape.RPC, timeout=3):
        if method == "eth_chainId":
            return "0x38"
        seeds.append(params[1])
        raise RuntimeError("BSC observation RPC failed: {'code': -32001, 'message': 'usage limit'}")
    assert _drive_tape(monkeypatch, tmp_path, [100, 101, 102, 103], rpc) == {}
    assert seeds == ["0x64"]


def test_a_head_without_a_number_is_skipped_and_counted_never_a_reset(tmp_path, monkeypatch, capsys):
    """Alpha 2026-10-02: a kind-2 message with no number reset the observer (KeyError 'number'),
    which wiped the Syncs and needed a re-seed."""
    seeds = []

    def rpc(method, params, url=tape.RPC, timeout=3):
        if method == "eth_chainId":
            return "0x38"
        seeds.append(params[1])
        return _reserves()
    rows = _drive_tape(monkeypatch, tmp_path, [("bare_head",), 100, 101], rpc)
    out = capsys.readouterr().out
    assert "BSC observer reset" not in out and "head without number/hash skipped (n=1)" in out
    assert seeds == ["0x64"] and rows == {A: (100, -1)}


def test_an_observer_reset_line_carries_its_time():
    line = tape.reset_line(KeyError("number"), now=1790000000.0)
    assert line == "2026-09-21T14:13:20+00:00 BSC observer reset: KeyError: 'number'"


def test_a_pair_seen_trading_after_a_failed_seed_is_not_seeded_again(tmp_path, monkeypatch):
    seeds = []
    monkeypatch.setattr(tape, "HOP_SEED_BACKOFF_S", (0.0,))

    def rpc(method, params, url=tape.RPC, timeout=3):
        if method == "eth_chainId":
            return "0x38"
        seeds.append(params[1])
        raise RuntimeError("BSC observation RPC failed: timeout")
    rows = _drive_tape(monkeypatch, tmp_path, [100, ("sync", A, 100), 101, 102, 103], rpc)
    assert seeds == ["0x64"] and rows == {A: (100, 3)}


def test_a_code_15_answer_through_the_default_seed_call_pauses_the_whole_account(monkeypatch):
    import sniper.services.memecoin_research_pacer as rp
    paused = []
    monkeypatch.setattr(rp.SharedPacer, "acquire", lambda self: None)
    monkeypatch.setattr(rp.SharedPacer, "pause", lambda self, s: paused.append(s))
    monkeypatch.setattr(tape, "_rpc", lambda method, params, url, timeout=3: (_ for _ in ()).throw(
        RuntimeError("BSC observation RPC failed: {'code': 15, 'message': 'free plan rate limit'}")))
    got, stopped = tape.seed_hop_reserves({"0x" + "4b" * 20: "a"}, 1, sleep=lambda s: None)
    assert paused == [rp.DRPC_LIMIT_PAUSE_S] and (got, stopped) == ([], False)


def test_a_pause_that_starts_mid_seed_stops_before_the_next_pair_without_blocking(monkeypatch):
    """Gamma LOW-1 (3aefc28e6): the pause was checked once, before the first pair. Another process
    can pause the account between pairs (a code-15 answer); the next pair then waited in the pacer
    up to 60 s on the tape worker. The check now runs before every pair."""
    import time
    import sniper.services.memecoin_research_pacer as rp
    state = {"paused_until": 0.0}
    monkeypatch.setattr(rp.SharedPacer, "paused_until", lambda self: state["paused_until"])
    monkeypatch.setattr(rp.SharedPacer, "pause", lambda self, s: None)
    monkeypatch.setattr(rp.SharedPacer, "acquire", lambda self: (
        pytest.fail("blocked on a paused account") if state["paused_until"] > time.time() else None))
    asked = []

    def fake_rpc(method, params, url, timeout=3):
        asked.append(params[0]["to"])
        state["paused_until"] = time.time() + 60        # someone else's code-15 lands here
        return "0x" + "0" * 63 + "1" + "0" * 63 + "2"
    monkeypatch.setattr(tape, "_rpc", fake_rpc)
    pairs = {"0x" + "4b" * 20: "a", "0x" + "4c" * 20: "b"}
    got, stopped = tape.seed_hop_reserves(pairs, 1, sleep=lambda s: None)
    assert asked == ["0x" + "4b" * 20] and got == [("0x" + "4b" * 20, 1, 2)] and not stopped


# ---- F1a (Alpha 2026-10-07, dfeaef22): a "0x" answer is no contract at that block ----
# 28,204 seed reads failed with "invalid literal for int() with base 16: ''" from 10-05 18:08Z to
# 10-07 12:27Z (~667 an hour on the shared dRPC 56 pacer), 17,070 of them retries of a pair that had
# already answered "0x" in the same window. A created pair's own Sync is recorded live.

def test_a_no_contract_answer_is_absent_not_a_failure(capsys):
    def rpc(method, params, url):
        return "0x" if params[0]["to"] == A else _reserves()
    got, stopped = tape.seed_hop_reserves({A: "q1", B: "q2"}, 7, rpc=rpc, sleep=lambda s: None)
    assert not stopped and got == [(A, None, None), (B, 70, 30)]
    out = capsys.readouterr().out
    assert "BSC hop seed absent %s at 7" % A in out and "seed failed" not in out


def test_an_absent_pair_leaves_the_window_without_a_backoff():
    s = tape.HopSeeder({A: "q1", B: "q2"})
    s.window(1000.0, 100)
    s.done([A, B], False, 50.0, attempted={A, B})       # A answered "0x", B seeded
    assert s.pending == set() and s.failures == 0


def test_a_no_contract_pair_is_read_once_a_window_and_its_own_sync_still_lands(tmp_path, monkeypatch):
    seeds = []
    monkeypatch.setattr(tape, "HOP_SEED_BACKOFF_S", (0.0,))

    def rpc(method, params, url=tape.RPC, timeout=3):
        if method == "eth_chainId":
            return "0x38"
        seeds.append(params[1])
        return "0x"
    rows = _drive_tape(monkeypatch, tmp_path, [100, 101, 102, 103, ("sync", A, 104), 104], rpc)
    assert seeds == ["0x64"], "no retry of a no-contract answer inside the window"
    assert rows == {A: (104, 3)}
