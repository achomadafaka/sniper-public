"""The EVM watcher re-reads the blocks a restart or reconnect missed (2026-09-28, operator YES).

Spec: docs/superpowers/specs/2026-09-28-memecoin-evm-watcher-gap-backfill-design.md. A restart is
dark about 2 s (p50) and a reconnect drops the blocks in between; the poll fallback starts at head.
With MEMECOIN_ONCHAIN_EVM_BACKFILL_CHAINS naming the chain, the watcher reads the gap between the last
head it saw and the first head after the gap with eth_getLogs {Transfer, to = a watched address} on
the chain's Alchemy pool member (the only endpoint that serves a recipient-only query; 10-block pages
on its free tier), newest first, clipped to the lane's 45 s window at the chain's own measured block
rate and to MEMECOIN_ONCHAIN_EVM_BACKFILL_MAX_CALLS. Each log goes through the same transfer path,
with the per-tx dedupe, and a backfilled detection says so in source_valuation_json. Off by default.
"""
import json
import threading

import pytest

import scripts.research.onchain_evm_wallet_watch as w
from sniper.services import memecoin_onchain_push as P
from sniper.settings import memecoin as cfg

WALLET = "0x" + "ab" * 20
MINT = "0x" + "cd" * 20
TX = "0x" + "77" * 32
ALCHEMY = "https://robinhood-mainnet.g.alchemy.com/v2/SECRETKEY123"


class _Conn:
    def __init__(self, inserts):
        self.inserts = inserts

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("INSERT"):
            self.inserts.append(params)
        return self

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    inserts, pages, lines = [], [], []
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn(inserts))
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "_roster_connection", lambda: None, raising=False)
    monkeypatch.setattr(w, "send_wake", lambda address: True)
    monkeypatch.setattr(w, "tx_read_host_from_env", lambda: None)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_RELAY_CODE_TTL_S", 0.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_BACKFILL_CHAINS", frozenset({"4663"}), raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_BACKFILL_MAX_CALLS", 45, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_LANE_MAX_AGE_S", 45.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"4663": ["https://rpc.x", ALCHEMY]}, raising=False)
    monkeypatch.setattr(w, "_say", lambda line: lines.append(line))
    monkeypatch.setattr(w, "push_detection", lambda rpc_url, log, watched, *, outcome, now, **k: (
        outcome.update(reason="detected", retries=0, hosts={}, host_ms={}) or
        dict(tx_hash=log["transactionHash"], wallet=WALLET, source_user_id="u1", handle="h1",
             buy={"mint": MINT, "token_delta": 1.0, "source_size_usd": 100.0, "source_price": 1.0},
             log=log, now=now)))
    return {"inserts": inserts, "pages": pages, "lines": lines, "hb": tmp_path / "hb.json"}


BLOCK_TS = 1_790_000_000


def _getlogs(env, hits, *, block_ts=True):
    """An Alchemy stub: records each page, returns a log for any block listed in `hits`, and answers
    eth_getBlockByNumber with a timestamp (or a failure when `block_ts` is False)."""
    def post(url, method, params):
        assert url == ALCHEMY
        if method == "eth_getBlockByNumber":
            env.setdefault("block_reads", []).append(params[0])
            if not block_ts:
                raise RuntimeError("HTTP 429")
            return {"number": params[0], "timestamp": hex(BLOCK_TS + int(params[0], 16))}
        assert method == "eth_getLogs"
        f = params[0]
        lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
        env["pages"].append((lo, hi))
        assert hi - lo + 1 <= 10, "the free tier caps a getLogs range at 10 blocks"
        return [{"transactionHash": TX, "address": MINT, "blockNumber": hex(b), "topics": f["topics"][:1]}
                for b in hits if lo <= b <= hi]
    return post


def _run(monkeypatch, env, script, *, previous_block=None, hits=(), block_ts=True):
    w._configure("4663")
    monkeypatch.setattr(w, "HEARTBEAT", env["hb"])
    if previous_block is not None:
        env["hb"].write_text(json.dumps({"ts": w.now_iso(), "last_block": previous_block}))
    monkeypatch.setattr(w, "_backfill_post", _getlogs(env, hits, block_ts=block_ts))
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn(env["inserts"]))
    # The startup decimals reads went to the fake RPC host for real: ~20 s per NXDOMAIN on this host,
    # twice per token, so the dark-poll tests passed pytest-timeout's 60 s and killed the deploy's
    # workers (2026-10-01). None is what a failed read returns.
    monkeypatch.setattr(w, "erc20_decimals", lambda token, rpc_url, *a, **k: None)
    done = []
    monkeypatch.setattr(w, "_backfill_thread", lambda target: (target(), done.append(1)))  # run inline

    def fake_stream(ws_url, **kw):
        for step in script:
            step(kw["state"], kw)
        return kw["state"]
    monkeypatch.setattr(P, "run_push_stream", fake_stream)
    price = w.NativePrice(lambda: 2650.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)
    return done


def _heads(first, n, *, rate=1.0):
    """n delivered heads from block `first` on ONE steady chain clock: block b is at b / rate seconds.

    The clock used to restart at a fixed t0 on every call, so a second batch after a reconnect looked
    like 25 blocks in 0 s: the measured rate read ~260 blocks/s and the lane window always hit the
    page cap, which hid a page-failure mutant (2026-09-29). The default 1 block/s keeps the chain's
    integer timestamps exact, so the lane window is exactly 45 blocks (5 pages)."""
    def step(state, kw):
        for i in range(n):
            b = first + i
            ts = b / rate
            state.head(ts, b, int(ts))
            kw["on_head"]()
    return step


def test_a_restart_gap_is_read_newest_first_and_detects(monkeypatch, env):
    _run(monkeypatch, env, [_heads(1_030, 30)], previous_block=1_000, hits=[1_012])
    assert env["pages"][:3] == [(1_021, 1_030), (1_011, 1_020), (1_001, 1_010)]
    assert len(env["inserts"]) == 1
    valuation = json.loads(next(p for p in env["inserts"][0] if isinstance(p, str) and '"basis"' in p))
    assert valuation["backfill"] is True


def test_a_long_gap_is_clipped_to_the_lane_window(monkeypatch, env):
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_BACKFILL_MAX_CALLS", 10_000, raising=False)
    _run(monkeypatch, env, [_heads(100_000, 30)], previous_block=1_000)       # the cap cannot bind
    # 45 s at the measured 1 block/s back from the head the rate was first known at (100,001),
    # not the 99,000-block gap
    assert min(p[0] for p in env["pages"]) == 100_001 - 45
    assert max(p[1] for p in env["pages"]) == 100_000 and len(env["pages"]) == 5


def test_the_call_cap_holds(monkeypatch, env):
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_BACKFILL_MAX_CALLS", 3, raising=False)
    _run(monkeypatch, env, [_heads(100_000, 30)], previous_block=1_000)
    assert len(env["pages"]) == 3


def test_a_reconnect_gap_is_read_too(monkeypatch, env):
    def reconnect(state, kw):
        state.reconnects += 1
    _run(monkeypatch, env, [_heads(2_000, 20), reconnect, _heads(2_050, 20)])
    assert (2_041, 2_050) in env["pages"], "the blocks between the last head before and the first after"


def test_off_by_default_reads_nothing(monkeypatch, env):
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_BACKFILL_CHAINS", frozenset(), raising=False)
    _run(monkeypatch, env, [_heads(1_030, 30)], previous_block=1_000, hits=[1_012])
    assert env["pages"] == [] and env["inserts"] == []


def test_no_alchemy_member_is_off_and_says_so(monkeypatch, env):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"4663": ["https://rpc.x"]}, raising=False)
    _run(monkeypatch, env, [_heads(1_030, 30)], previous_block=1_000)
    assert env["pages"] == [] and any("backfill off" in l for l in env["lines"])


def test_the_key_is_never_printed(monkeypatch, env):
    _run(monkeypatch, env, [_heads(1_030, 30)], previous_block=1_000, hits=[1_012])
    assert not any("SECRETKEY123" in l for l in env["lines"])
    assert any("robinhood-mainnet.g.alchemy.com" in l for l in env["lines"])


def test_a_failed_page_is_a_named_hole_and_the_rest_still_reads(monkeypatch, env):
    calls = []

    def flaky(url, method, params):
        calls.append(params[0]["fromBlock"])
        if len(calls) == 1:
            raise RuntimeError("HTTP 429")
        return []
    w._configure("4663")
    monkeypatch.setattr(w, "HEARTBEAT", env["hb"])
    env["hb"].write_text(json.dumps({"ts": w.now_iso(), "last_block": 1_000}))
    monkeypatch.setattr(w, "_backfill_post", flaky)
    monkeypatch.setattr(w, "_backfill_thread", lambda target: target())

    def fake_stream(ws_url, **kw):
        _heads(1_030, 30)(kw["state"], kw)
        return kw["state"]
    monkeypatch.setattr(P, "run_push_stream", fake_stream)
    price = w.NativePrice(lambda: 2650.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)
    # [1000, 1030] inclusive since 2026-10-07 (the anchor block is re-read): 4 pages, and the
    # durable frontier holds just below the anchor while the failed page leaves the read incomplete.
    assert len(calls) == 4 and any("backfill page" in l and "failed" in l for l in env["lines"])
    assert json.loads(env["hb"].read_text())["last_block"] == 999


def test_the_heartbeat_carries_the_last_block(monkeypatch, env):
    _run(monkeypatch, env, [_heads(1_030, 5)])
    assert json.loads(env["hb"].read_text())["last_block"] == 1_034


def test_queued_unread_backfill_keeps_durable_frontier(monkeypatch, env):
    from sniper.services import memecoin_onchain_push as P
    w._configure("4663")
    monkeypatch.setattr(w, "HEARTBEAT", env["hb"])
    env["hb"].write_text(json.dumps({"last_block": 1000}))
    queued = []
    monkeypatch.setattr(w, "_backfill_thread", lambda target: queued.append(target))
    monkeypatch.setattr(w, "_backfill_post", _getlogs(env, [1012]))
    monkeypatch.setattr(P, "run_push_stream", lambda url, **kw: (
        _heads(1030, 5)(kw["state"], kw), kw["state"])[1])
    price = w.NativePrice(lambda: 2650.0, log_fn=lambda _: None)
    price.refresh()
    w.run_push("wss://test", price=price)
    assert len(queued) == 1 and env["pages"] == []
    assert json.loads(env["hb"].read_text())["last_block"] == 999    # the anchor block is queued too


# --- the row's age (2026-09-28, Alpha): the source block's time, in its own field ----------------

def _insert(env):
    cols = ("signature,source_user_id,handle,address,network_id,mint,token_delta,source_size_usd,"
            "source_price,block_time,detected_at,detect_latency_s,created_at,source_valuation_json").split(",")
    return dict(zip(cols, env["inserts"][0]))


def test_a_backfilled_row_carries_its_block_time_and_keeps_its_arrival(monkeypatch, env):
    before = w.now_iso()
    _run(monkeypatch, env, [_heads(1_030, 30)], previous_block=1_000, hits=[1_012])
    row = _insert(env)
    assert row["block_time"] == BLOCK_TS + 1_012, "the block's own timestamp, read per block"
    assert row["detected_at"] >= before, "detected_at stays our arrival stamp, one meaning per column"
    assert json.loads(row["source_valuation_json"])["backfill"] is True
    assert env["block_reads"] == [hex(1_012)]


def test_a_backfilled_log_without_a_block_time_is_skipped_and_named(monkeypatch, env):
    _run(monkeypatch, env, [_heads(1_030, 30)], previous_block=1_000, hits=[1_012], block_ts=False)
    assert env["inserts"] == []
    assert any("block time unavailable" in l for l in env["lines"])


def test_a_live_log_is_decided_while_the_backfill_is_blocked_mid_read(monkeypatch, env):
    """The backfill's read of an old tx blocks; a LIVE transfer arriving meanwhile must not wait."""
    release, in_backfill = threading.Event(), threading.Event()
    live_tx = "0x" + "88" * 32

    def detect(rpc_url, log, watched, *, outcome, now, **k):
        if log["transactionHash"] == TX:                     # the backfilled one: slow read
            in_backfill.set()
            release.wait(3.0)
        outcome.update(reason="detected", retries=0, hosts={}, host_ms={})
        return dict(tx_hash=log["transactionHash"], wallet=WALLET, source_user_id="u1", handle="h1",
                    buy={"mint": MINT, "token_delta": 1.0, "source_size_usd": 100.0, "source_price": 1.0},
                    log=log, now=now)
    monkeypatch.setattr(w, "push_detection", detect)
    threads = []

    def start(target):
        t = threading.Thread(target=target, daemon=True)
        threads.append(t)
        t.start()
    w._configure("4663")
    monkeypatch.setattr(w, "HEARTBEAT", env["hb"])
    env["hb"].write_text(json.dumps({"ts": w.now_iso(), "last_block": 1_000}))
    monkeypatch.setattr(w, "_backfill_post", _getlogs(env, [1_012]))
    monkeypatch.setattr(w, "_backfill_thread", start)
    live_done = []

    def fake_stream(ws_url, **kw):
        _heads(1_030, 30)(kw["state"], kw)
        assert in_backfill.wait(5.0), "the backfill never reached its read"
        import time as _time
        started = _time.monotonic()
        kw["on_transfer"]({"transactionHash": live_tx, "address": MINT})
        waited = _time.monotonic() - started
        live_done.append(waited < 1.0 and not release.is_set()
                         and any(live_tx in json.dumps(p) for ins in env["inserts"] for p in ins))
        release.set()
        return kw["state"]
    monkeypatch.setattr(P, "run_push_stream", fake_stream)
    price = w.NativePrice(lambda: 2650.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)
    for t in threads:
        t.join(5.0)
    assert live_done == [True], "the live transfer was committed while the backfill read was still blocked"
