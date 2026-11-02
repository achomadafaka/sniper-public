"""The slot lane learns a PumpSwap pool when pump.fun MIGRATES into it, not at its first buy.

Dougherty 65991 and lemon 65992 (2026-09-27) landed S+7: the lane decoded both buys at S and
refused `pool_unknown`, because its pool cache learned a pool only after a buy missed it. 20 of the
27 lane-buildable pools it missed since 2026-09-26 21:07Z were migrated within 24 h of the buy, and
neither incident's mint was in any ledger table before it (scripts/research/
slot_lane_pool_miss_census.py). Every migration's logs carry pAMM's CreatePoolEvent, so a listener
on pump's migration account hands each new canonical wSOL pool to the cache, which reads it from
chain through the same checks as a learn. The event is only a READ TARGET: every fact a build signs
with still comes from the chain.

Frames: tests/fixtures/memecoin/pump_migrate_frames.json (real, 2026-09-27). Pool accounts: the
Tekkerrss pool in tests/fixtures/memecoin/slot_handoff_54bzG3hj.json.
"""
import asyncio
import base64
import copy
import json
import threading
from pathlib import Path

import pytest

from sniper.execution import pumpswap_buy as ps
from sniper.services import memecoin_slot_lane as lane
from sniper.services import memecoin_slot_signal as sig

HERE = Path(__file__).parent / "fixtures" / "memecoin"
MIGRATIONS = json.loads((HERE / "pump_migrate_frames.json").read_text())
FX = json.loads((HERE / "slot_handoff_54bzG3hj.json").read_text())["tekkerrss"]
POOL = "7LwcYUjF8pVACHJtE4J2Te3jmxN83Vn3CnAFPPecwVeu"
MINT = "54bzG3hjWCwL6UoeobcPnrXK5qUjxHn9USpfDY1Zpump"
WSOL = "So11111111111111111111111111111111111111112"
DOUGHERTY_POOL = "qNadZjDebjUwHS5ZRcLTLtoGk4wAU15kydQLFSJGUUe"


class _Rpc:
    """Pool accounts by address (a list = one answer per read, the last one repeating) and the
    fixture's two mints."""

    def __init__(self, pools):
        self.pools, self.reads = pools, []

    def __call__(self, method, params):
        assert method == "getMultipleAccounts" and len(params[0]) <= 100
        assert params[1]["commitment"] == "confirmed"
        self.reads.append(list(params[0]))
        mints = {MINT: FX["mint_accounts"][0], WSOL: FX["mint_accounts"][1]}
        out = []
        for a in params[0]:
            got = self.pools.get(a, mints.get(a))
            if isinstance(got, list):
                got = got.pop(0) if len(got) > 1 else got[0]
            out.append(got)
        return {"value": out}


def test_a_migration_frame_names_its_canonical_wsol_pool():
    ev = ps.create_pool_event(MIGRATIONS["wsol"]["logs"])
    assert (ev["pool"], ev["base_mint"], ev["quote_mint"]) == (
        DOUGHERTY_POOL, "6UTgBWACzB1zX2GA2uEvVn6325XE99wNvkVTXv5qpump", WSOL)
    assert ev["canonical"] is True and ev["pump_migrated"] is True
    usdc = ps.create_pool_event(MIGRATIONS["usdc"]["logs"])
    assert usdc["pump_migrated"] is True and usdc["canonical"] is False and usdc["quote_mint"] != WSOL   # the lane cannot build it
    assert ps.create_pool_event(MIGRATIONS["already_migrated"]["logs"]) is None


def test_a_streamed_pool_is_known_before_its_first_buy(tmp_path):
    cache = ps.PumpSwapPoolCache(rpc=_Rpc({POOL: FX["pool_account"]}),
                                 store_path=str(tmp_path / "pools.json"))
    cache.offer_stream(POOL)
    assert cache.state(POOL) == "unknown"            # not a miss: the handoff's queue is untouched
    cache.learn_pending()
    assert cache.state(POOL) == "known"
    buy = sig.decode_owner_buy(FX["frame"], FX["owner"], sig._owner_bytes(FX["owner"]),
                               FX["source_user_id"], "Tekkerrss", recv_monotonic=0.0, recv_utc="x")
    reason, facts = lane.amm_market_facts(buy, cache, 120.0)
    assert reason is None and facts["mint"] == MINT                   # the S+7 refusal, gone
    assert json.loads((tmp_path / "pools.json").read_text()) == [POOL]  # survives a restart


def test_a_streamed_address_that_is_not_a_pumpswap_pool_on_chain_is_never_stored():
    foreign = dict(FX["pool_account"], owner="11111111111111111111111111111111")
    cache = ps.PumpSwapPoolCache(rpc=_Rpc({POOL: foreign}))
    cache.offer_stream(POOL)
    for _ in range(ps.STREAM_RETRY_TICKS + 2):
        cache.learn_pending()
    assert cache.state(POOL) == "unknown" and cache.stats["stream_dropped"] == 1


def test_a_streamed_pool_the_node_does_not_show_yet_is_retried_then_dropped():
    """The listener hears a migration at `confirmed`; the custody node may not show the account
    for a moment. It is re-read on the next ticks, and dropped after STREAM_RETRY_TICKS (the
    ordinary miss-then-learn still covers it)."""
    late = _Rpc({POOL: [None, None, FX["pool_account"]]})
    cache = ps.PumpSwapPoolCache(rpc=late)
    cache.offer_stream(POOL)
    for _ in range(3):
        cache.learn_pending()
    assert cache.state(POOL) == "known" and cache.stats["stream_stored"] == 1

    never = _Rpc({POOL: None})
    cache = ps.PumpSwapPoolCache(rpc=never)
    cache.offer_stream(POOL)
    for _ in range(ps.STREAM_RETRY_TICKS + 5):
        cache.learn_pending()
    pool_reads = sum(POOL in r for r in never.reads)
    assert pool_reads == ps.STREAM_RETRY_TICKS and cache.stats["stream_dropped"] == 1


def test_warm_work_per_tick_is_bounded_and_the_stream_goes_first(tmp_path):
    """2,048 persisted pools must not stall the prefetch thread's first tick (legs, balances):
    at most WARM_PER_TICK pools are read per tick, a streamed pool first, then the newest persisted."""
    addresses = [str(ps._pda([b"warm", i.to_bytes(2, "little")], ps.AMM_PROGRAM))
                 for i in range(ps.WARM_PER_TICK * 2 + 50)]
    store = tmp_path / "pools.json"
    store.write_text(json.dumps(addresses))
    rpc = _Rpc({a: FX["pool_account"] for a in addresses + [POOL]})
    clock = _TickClock()
    cache = ps.PumpSwapPoolCache(rpc=rpc, store_path=str(store), clock=clock)
    cache.offer_stream(POOL)
    cache.learn_pending()
    first_tick = [a for r in rpc.reads for a in r if a not in (MINT, WSOL)]
    assert len(first_tick) == ps.WARM_PER_TICK and first_tick[0] == POOL
    assert set(first_tick[1:]) == set(addresses[-(ps.WARM_PER_TICK - 1):])
    for _ in range(6):
        clock.tick()
        cache.learn_pending()
    assert all(cache.state(a) == "known" for a in addresses)


class _TickClock:
    """The prefetch tick: ~0.857 s apart (n ~700 loops per 10-minute report, 2026-10-07)."""

    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def tick(self, s=0.857):
        self.now += s


def test_the_restart_warm_is_paced_on_the_custody_key_newest_first(tmp_path):
    """8,192 stored pools re-read at the old pace was ~250 calls at ~3.4 a second for ~70 s after
    every restart (Alpha/Gamma on 096ffe442). The warm now spends at most
    WARM_MAX_CALLS_PER_S over any run of ticks, newest first, and still finishes."""
    addresses = [str(ps._pda([b"pace", i.to_bytes(2, "little")], ps.AMM_PROGRAM))
                 for i in range(ps.WARM_PER_TICK * 5)]
    store = tmp_path / "pools.json"
    store.write_text(json.dumps(addresses))
    rpc = _Rpc({a: FX["pool_account"] for a in addresses})
    clock = _TickClock()
    cache = ps.PumpSwapPoolCache(rpc=rpc, store_path=str(store), clock=clock)
    calls_at, order = [], []
    for _ in range(40):
        before = len(rpc.reads)
        cache.learn_pending()
        calls_at += [clock.now] * (len(rpc.reads) - before)
        clock.tick()
    order = [a for r in rpc.reads for a in r if a not in (MINT, WSOL)]
    assert all(cache.state(a) == "known" for a in addresses)
    assert order[:ps.WARM_PER_TICK] == addresses[::-1][:ps.WARM_PER_TICK]      # newest first
    span = calls_at[-1] - calls_at[0]
    assert ps.WARM_MAX_CALLS_PER_S <= 2.0                                     # Alpha's bound
    assert len(calls_at) - 2 <= ps.WARM_MAX_CALLS_PER_S * span, (len(calls_at), span)


def test_the_cache_keeps_at_most_the_cap_and_forgets_the_oldest(tmp_path):
    addresses = [str(ps._pda([b"cap", bytes([i])], ps.AMM_PROGRAM)) for i in range(5)]
    store = tmp_path / "pools.json"
    cache = ps.PumpSwapPoolCache(rpc=_Rpc({a: FX["pool_account"] for a in addresses}),
                                 store_path=str(store), store_cap=3)
    for a in addresses:
        cache.offer_stream(a)
        cache.learn_pending()
    assert [cache.state(a) for a in addresses] == ["unknown"] * 2 + ["known"] * 3
    assert json.loads(store.read_text()) == addresses[2:]
    assert cache.stats["known"] == 3


def test_warm_reads_are_timed_for_the_heartbeat():
    cache = ps.PumpSwapPoolCache(rpc=_Rpc({POOL: FX["pool_account"]}))
    cache.offer_stream(POOL)
    cache.learn_pending()
    assert cache.stats["read_ms_p50"] is not None and cache.stats["read_ms_p90"] is not None


def _notification(frame, sid, err=None):
    value = {"signature": frame["signature"], "err": err, "logs": frame["logs"]}
    return {"jsonrpc": "2.0", "method": "logsNotification",
            "params": {"subscription": sid, "result": {"context": {"slot": frame["slot"]},
                                                       "value": value}}}


class _MigrationWS:
    def __init__(self, stop, frames):
        self.sent, self.out, self.stop, self.frames = [], asyncio.Queue(), stop, frames

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        await self.out.put({"jsonrpc": "2.0", "id": msg["id"], "result": 77})
        for frame in self.frames:
            await self.out.put(frame)

    async def recv(self):
        if self.out.empty():
            self.stop.set()
            await asyncio.sleep(0.05)
            raise asyncio.TimeoutError
        return json.dumps(await self.out.get())


def test_the_listener_hands_over_only_verified_new_migration_pools():
    stop, pools, status = threading.Event(), [], {}
    frames = [_notification(MIGRATIONS["wsol"], 77), _notification(MIGRATIONS["usdc"], 77),
              _notification(MIGRATIONS["already_migrated"], 77),
              _notification(MIGRATIONS["wsol"], 77, err={"InstructionError": [2, "x"]})]
    import struct
    for offset, replacement in ((16, struct.pack("<H", 1)), (18, bytes(32))):
        bad = copy.deepcopy(MIGRATIONS["wsol"])
        for index, line in enumerate(bad["logs"]):
            if line.startswith("Program data: "):
                raw = bytearray(base64.b64decode(line[14:]))
                if raw[:8] == ps.CREATE_POOL_EVENT_DISCRIMINATOR:
                    raw[offset:offset + len(replacement)] = replacement
                    bad["logs"][index] = "Program data: " + base64.b64encode(raw).decode()
        frames.append(_notification(bad, 77))
    frames.append(_notification(MIGRATIONS["wsol"], 77))
    ws = _MigrationWS(stop, frames)
    thread = sig.start_migration_thread(pools.append, ws_url="wss://x", status=status, stop=stop,
                                        connect=lambda url: ws)
    thread.join(timeout=10)
    assert not thread.is_alive()
    (sub,) = ws.sent
    assert sub["method"] == "logsSubscribe"
    assert sub["params"] == [{"mentions": [sig.PUMP_MIGRATION_ACCOUNT]}, {"commitment": "confirmed"}]
    assert pools == [DOUGHERTY_POOL, ps.create_pool_event(MIGRATIONS["usdc"]["logs"])["pool"]]
    assert status["frames"] == 7 and status["pools"] == 2 and status["last_frame_mono"] is not None


def test_a_silent_subscription_reconnects():
    """2026-09-27 probe: the keyless subscription went silent at 12:37:40Z with no close while the
    chain kept migrating. Silence past the bound reconnects and resubscribes."""
    stop, connects, status = threading.Event(), [], {}

    class Silent(_MigrationWS):
        async def recv(self):
            await asyncio.sleep(10)

    def connect(url):
        connects.append(url)
        if len(connects) == 2:
            stop.set()
        return Silent(stop, [])
    thread = sig.start_migration_thread(lambda p: None, ws_url="wss://x", status=status,
                                        stop=stop, connect=connect, silence_s=0.05, backoff_s=0.0)
    thread.join(timeout=10)
    assert not thread.is_alive() and len(connects) == 2
    assert status["reconnects"] == 1 and "silent" in status["last_error"]


@pytest.mark.parametrize("bad", ["", "https://mainnet.helius-rpc.com/?api-key=FUNDED"])
def test_the_listener_never_starts_without_its_own_socket(bad, monkeypatch):
    monkeypatch.setenv("MEMECOIN_SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=FUNDED")
    with pytest.raises(ValueError):
        sig.start_migration_thread(lambda p: None, ws_url=bad.replace("https", "wss"), status={})


def test_the_lane_starts_the_listener_on_its_pool_cache_and_survives_its_failure(monkeypatch):
    starts, handed = [], {}
    import threading
    real_thread = threading.Thread
    expected_starts = ["slot-lane-miss-learn", "slot-lane-prefetch-sol_usd",
                       "slot-lane-prefetch-balances", "slot-lane-prefetch-blockhash",
                       "slot-lane-prefetch", "slot-lane"]

    class Thread:
        def __init__(self, *, target, name, daemon):
            self.name = name

        def start(self):
            starts.append(self.name)

        def is_alive(self):
            return False

    def fixture_thread(*args, **kwargs):
        # Other tests can leave background learners alive while this patch is installed.
        return Thread(**kwargs) if kwargs.get("name") in expected_starts else real_thread(*args, **kwargs)
    monkeypatch.setattr("threading.Thread", fixture_thread)
    amm = type("Amm", (), {"pools": ps.PumpSwapPoolCache(rpc=_Rpc({}))})()
    rc = dict(usdc_fn=lambda: None, sol_fn=lambda: None, latest_blockhash_fn=lambda: None,
              slot_lock_key="fixture")

    def migration_start_fn(on_pool, *, status):
        handed.update(on_pool=on_pool, status=status)
        raise RuntimeError("socket refused")
    status = lane.start_slot_lane(
        rc, builders={"pump_amm": amm}, signal_start_fn=lambda *a, **k: None,
        sol_usd_fn=lambda: None, log=lambda _: None, roster_path="unused",
        conn_factory=object, readonly_conn_factory=object, migration_start_fn=migration_start_fn)
    # The listener's callback feeds the pool cache (and, when graduation direct is on, its watcher).
    handed["on_pool"]("PoolHeardMigrating111111111111111111111111")
    assert amm.pools.stats["streamed"] == 1 and handed["status"] is status["prewarm"]
    assert status["pool_cache"] is amm.pools.stats
    # A dead listener costs no lane. The miss learner (MEMECOIN_SLOT_LANE_MISS_LEARN) rides the cache.
    assert starts == expected_starts


def test_the_heartbeat_names_the_prewarm_fields(tmp_path):
    import time
    from sniper.services import memecoin_runner as run
    path = tmp_path / "hb.json"
    run.write_heartbeat({}, True, path=str(path), slot_lane_status={
        "mode": "live", "signal": {},
        "prewarm": {"connected": True, "frames": 9, "pools": 6, "reconnects": 1,
                    "last_frame_mono": time.monotonic() - 2.0, "last_error": None, "junk": 1},
        "pool_cache": {"known": 40, "streamed": 6, "stream_stored": 5, "stream_dropped": 1,
                       "read_ms_p50": 81.0, "read_ms_p90": 140.0, "warm_unresolved_streak": 3,
                       "warm_retry_n": 2}})
    got = json.loads(path.read_text())["slot_lane"]["prewarm"]
    assert 1.5 < got.pop("last_frame_age_s") < 10
    assert got == {"connected": True, "frames": 9, "pools": 6, "reconnects": 1, "last_error": None,
                   "known": 40, "streamed": 6, "stream_stored": 5, "stream_dropped": 1,
                   "read_ms_p50": 81.0, "read_ms_p90": 140.0, "warm_unresolved_streak": 3,
                   "warm_retry_n": 2}


def test_one_tick_reads_pools_in_one_call_so_a_slow_custody_rpc_costs_the_legs_one_call(tmp_path):
    """On Wi-Fi the custody RPC took ~4.2 s per getMultipleAccounts (2 calls, 8,399 ms at 12:49:35Z).
    The prefetch tick that warms pools also refreshes the legs, balances and blockhash, so a tick
    may spend ONE pool read (<= 100 accounts) plus its mints, never more (Alpha, 2026-09-27)."""
    addresses = [str(ps._pda([b"tick", i.to_bytes(2, "little")], ps.AMM_PROGRAM)) for i in range(300)]
    store = tmp_path / "pools.json"
    store.write_text(json.dumps(addresses))
    rpc = _Rpc({a: FX["pool_account"] for a in addresses})
    ps.PumpSwapPoolCache(rpc=rpc, store_path=str(store)).learn_pending()
    pool_reads = [r for r in rpc.reads if set(r) - {MINT, WSOL}]
    assert len(pool_reads) == 1 and len(pool_reads[0]) <= 100


def test_validated_pool_snapshot_carries_its_address_for_preparation():
    from test_slot_lane_pump_nonsol import SPCX, _facts
    cache = _facts(SPCX)
    assert cache.get(SPCX['pool'])['pool'] == SPCX['pool']
