"""Pools the slot lane already learned survive a restart as ADDRESSES and are re-read in bulk.

The PumpSwap pool cache lived only in memory, so every deploy sent pools the lane knew back to
`pool_unknown`: hashbergers bought on pool 4TcaXZVY at 01:08:24Z (learned, handed off, filled),
the 01:30Z deploy wiped the cache, and the same pool refused again at 01:36:45Z.

Only pool addresses are written. At start the prefetch thread re-reads every persisted pool and
its mints from chain in bulk, through the same checks as a single learn, so every fact a build
signs with (mint, vaults, creator, virtual quote) comes from chain, never from the file.
The accounts are real: the Tekkerrss pool and mints in tests/fixtures/memecoin/slot_handoff_54bzG3hj.json.
"""
import json
from pathlib import Path

from sniper.execution import pumpswap_buy as ps
from sniper.services import memecoin_slot_lane as lane
from sniper.services import memecoin_slot_signal as sig

FX = json.loads((Path(__file__).parent / "fixtures" / "memecoin" / "slot_handoff_54bzG3hj.json")
                .read_text())["tekkerrss"]
POOL = "7LwcYUjF8pVACHJtE4J2Te3jmxN83Vn3CnAFPPecwVeu"
MINT = "54bzG3hjWCwL6UoeobcPnrXK5qUjxHn9USpfDY1Zpump"
WSOL = "So11111111111111111111111111111111111111112"


class _Rpc:
    """The chain as the learn reads see it: pool accounts by address, the fixture's two mints."""

    def __init__(self, pools):
        self.pools = pools          # address -> account dict | None
        self.calls = []

    def __call__(self, method, params):
        self.calls.append((method, len(params[0]) if method == "getMultipleAccounts" else 1))
        mints = {MINT: FX["mint_accounts"][0], WSOL: FX["mint_accounts"][1]}
        if method == "getAccountInfo":
            return {"value": self.pools.get(params[0])}
        if method == "getMultipleAccounts":
            assert len(params[0]) <= 100 and params[1]["commitment"] == "confirmed"
            return {"value": [self.pools.get(a, mints.get(a)) for a in params[0]]}
        raise AssertionError(method)


def _learned(tmp_path, pools=None, **kw):
    store = tmp_path / "slot_lane_pumpswap_pools.json"
    rpc = _Rpc(pools if pools is not None else {POOL: FX["pool_account"]})
    return ps.PumpSwapPoolCache(rpc=rpc, store_path=str(store), **kw), rpc, store


def test_a_restart_re_learns_a_known_pool_from_chain_and_the_buy_is_priced(tmp_path):
    first, _rpc, store = _learned(tmp_path)
    assert first.get(POOL) is None
    first.learn_pending()
    assert first.state(POOL) == "known"
    assert json.loads(store.read_text()) == [POOL]          # the address, nothing else

    restarted = ps.PumpSwapPoolCache(rpc=_Rpc({POOL: FX["pool_account"]}), store_path=str(store))
    restarted.learn_pending()                                # the prefetch thread's first refresh
    assert restarted.state(POOL) == "known"
    assert restarted.get(POOL) == first.get(POOL)            # same facts, read from chain again
    assert [c[0] for c in restarted.rpc.calls] == ["getMultipleAccounts"] * 2
    assert restarted.take_warm_report()["stored"] == 1 and restarted.take_warm_report() is None

    buy = sig.decode_owner_buy(FX["frame"], FX["owner"], sig._owner_bytes(FX["owner"]),
                               FX["source_user_id"], "Tekkerrss", recv_monotonic=0.0, recv_utc="x")
    reason, facts = lane.amm_market_facts(buy, restarted, 120.0)
    assert reason is None and facts["mint"] == MINT          # not pool_unknown after a restart


def test_many_pools_are_read_a_hundred_at_a_time(tmp_path):
    addresses = [str(ps._pda([b"test", bytes([i])], ps.AMM_PROGRAM)) for i in range(150)]
    store = tmp_path / "slot_lane_pumpswap_pools.json"
    store.write_text(json.dumps(addresses))
    rpc = _Rpc({a: FX["pool_account"] for a in addresses})
    now = [100.0]
    cache = ps.PumpSwapPoolCache(rpc=rpc, store_path=str(store), clock=lambda: now[0])
    cache.learn_pending()
    now[0] += 1.0                  # the warm's pace (2026-10-07): 2 calls at <= 2 a second
    cache.learn_pending()
    assert all(cache.state(a) == "known" for a in addresses)
    # 150 pools in two calls, never one call per pool. Since 2026-09-27 each prefetch tick reads
    # at most one call of pools (WARM_PER_TICK) plus their mints, so a slow custody RPC costs the
    # tick that refreshes the legs one call, not the whole store.
    assert rpc.calls == [("getMultipleAccounts", 100), ("getMultipleAccounts", 2),
                         ("getMultipleAccounts", 50), ("getMultipleAccounts", 2)]


def test_a_corrupt_file_warms_nothing_and_breaks_nothing(tmp_path):
    store = tmp_path / "slot_lane_pumpswap_pools.json"
    for text in ("{not json", json.dumps({"pool": POOL}), json.dumps(["not a pubkey", 7, None])):
        store.write_text(text)
        rpc = _Rpc({POOL: FX["pool_account"]})
        cache = ps.PumpSwapPoolCache(rpc=rpc, store_path=str(store))
        cache.learn_pending()
        assert rpc.calls == [] and cache.state(POOL) == "unknown"


def test_a_persisted_pool_that_is_gone_or_not_a_pumpswap_pool_is_not_stored(tmp_path):
    gone = str(ps._pda([b"gone"], ps.AMM_PROGRAM))
    store = tmp_path / "slot_lane_pumpswap_pools.json"
    store.write_text(json.dumps([gone, POOL]))
    foreign = dict(FX["pool_account"], owner="11111111111111111111111111111111")
    cache = ps.PumpSwapPoolCache(rpc=_Rpc({gone: None, POOL: foreign}), store_path=str(store))
    cache.learn_pending()
    assert cache.state(gone) == "unknown" and cache.state(POOL) == "unknown"
    assert cache.get(POOL) is None and cache.state(POOL) == "queued"   # ordinary miss-then-learn


def test_the_store_keeps_the_most_recently_learned_pools(tmp_path):
    addresses = [str(ps._pda([b"cap", bytes([i])], ps.AMM_PROGRAM)) for i in range(5)]
    cache, rpc, store = _learned(tmp_path, {a: FX["pool_account"] for a in addresses}, store_cap=3)
    for a in addresses:
        cache.get(a)
        cache.learn_pending()
    assert json.loads(store.read_text()) == addresses[2:]


def test_an_interrupted_restart_warm_keeps_every_unread_address_for_the_next_restart(tmp_path):
    """Codex N4 (night review 2026-10-06): the first warmed batch rewrote the store with only the
    pools read so far, so a second restart before the warm finished forgot the rest (150 seeded,
    100 kept). The store keeps the unread addresses until they have been read, and a pool learned
    live meanwhile is kept as the newest."""
    addresses = [str(ps._pda([b"two", i.to_bytes(2, "little")], ps.AMM_PROGRAM)) for i in range(150)]
    store = tmp_path / "slot_lane_pumpswap_pools.json"
    store.write_text(json.dumps(addresses))
    accounts = {a: FX["pool_account"] for a in addresses + [POOL]}
    first = ps.PumpSwapPoolCache(rpc=_Rpc(accounts), store_path=str(store), clock=lambda: 100.0)
    first.offer_stream(POOL)
    first.learn_pending()                                  # one batch, then the process restarts
    kept = json.loads(store.read_text())
    assert set(kept) == set(addresses) | {POOL} and kept[-1] == POOL
    now = [100.0]
    second = ps.PumpSwapPoolCache(rpc=_Rpc(accounts), store_path=str(store), clock=lambda: now[0])
    for _ in range(4):
        second.learn_pending()
        now[0] += 1.0
    assert all(second.state(a) == "known" for a in addresses + [POOL])
    assert set(json.loads(store.read_text())) == set(addresses) | {POOL}


# --- SOL-1 (Codex two-day review 2026-10-07): an unavailable warm read keeps its addresses ------

class _Flaky(_Rpc):
    """_Rpc whose getMultipleAccounts answers from `faults` in order first: an exception is raised,
    "null" answers value None, "short" drops the last account, None answers normally."""

    def __init__(self, pools, faults):
        super().__init__(pools)
        self.faults = list(faults)

    def __call__(self, method, params):
        if method == "getMultipleAccounts" and self.faults:
            fault = self.faults.pop(0)
            if isinstance(fault, Exception):
                self.calls.append((method, len(params[0])))
                raise fault
            if fault == "null":
                self.calls.append((method, len(params[0])))
                return {"value": None}
            if fault == "short":
                out = super().__call__(method, params)
                return {"value": out["value"][:-1]}
        return super().__call__(method, params)


def _stored_150(tmp_path, faults):
    addresses = [str(ps._pda([b"test", bytes([i])], ps.AMM_PROGRAM)) for i in range(150)]
    store = tmp_path / "slot_lane_pumpswap_pools.json"
    store.write_text(json.dumps(addresses))
    now = [100.0]
    cache = ps.PumpSwapPoolCache(rpc=_Flaky({a: FX["pool_account"] for a in addresses}, faults),
                                 store_path=str(store), clock=lambda: now[0])
    return cache, store, addresses, now


def _ticks(cache, now, n):
    for _ in range(n):
        cache.learn_pending()
        now[0] += 1.0


def test_a_failed_warm_batch_keeps_its_addresses_through_a_later_save(tmp_path):
    """The audit repro: 150 persisted, the first 100-account read times out, a later read succeeds
    and saves. Before: the store held 50."""
    cache, store, addresses, now = _stored_150(tmp_path, [TimeoutError("read timed out")])
    _ticks(cache, now, 2)
    assert sorted(json.loads(store.read_text())) == sorted(addresses)
    _ticks(cache, now, 2)
    assert all(cache.state(a) == "known" for a in addresses)


def test_a_restart_between_every_stage_keeps_every_address(tmp_path):
    cache, store, addresses, now = _stored_150(tmp_path, [TimeoutError("read timed out")])
    _ticks(cache, now, 1)
    restarted = ps.PumpSwapPoolCache(rpc=_Rpc({a: FX["pool_account"] for a in addresses}),
                                     store_path=str(store), clock=lambda: now[0])
    assert sorted(restarted._warm) == sorted(addresses)


def test_a_null_or_short_pool_answer_is_unavailable_not_absent(tmp_path):
    for fault in ("null", "short"):
        sub = tmp_path / fault
        sub.mkdir()
        cache, store, addresses, now = _stored_150(sub, [fault])
        _ticks(cache, now, 2)
        assert sorted(json.loads(store.read_text())) == sorted(addresses), fault
        _ticks(cache, now, 2)
        assert all(cache.state(a) == "known" for a in addresses), fault


def test_an_incomplete_mint_answer_leaves_its_pools_unresolved(tmp_path):
    cache, store, addresses, now = _stored_150(tmp_path, [None, "short"])   # pools ok, mints short
    _ticks(cache, now, 2)
    assert sorted(json.loads(store.read_text())) == sorted(addresses)
    _ticks(cache, now, 2)
    assert all(cache.state(a) == "known" for a in addresses)


def test_a_stuck_batch_goes_behind_the_others_and_shows_its_streak(tmp_path):
    """Alpha's GO on SOL-1: never give up on an address (that is data loss), but a batch whose read
    keeps failing must not block the pending batches behind it, and the streak shows it."""
    addresses = [str(ps._pda([b"test", bytes([i])], ps.AMM_PROGRAM)) for i in range(250)]
    stuck = set(addresses[-100:])                  # the newest batch: the warm reads it first
    store = tmp_path / "slot_lane_pumpswap_pools.json"
    store.write_text(json.dumps(addresses))
    rpc = _Rpc({a: FX["pool_account"] for a in addresses})

    def flaky(method, params):
        if method == "getMultipleAccounts" and stuck & set(params[0]):
            raise TimeoutError("this batch always times out")
        return rpc(method, params)
    now = [100.0]
    cache = ps.PumpSwapPoolCache(rpc=flaky, store_path=str(store), clock=lambda: now[0])
    _ticks(cache, now, 1)
    assert cache.stats["warm_unresolved_streak"] == 1
    _ticks(cache, now, 2)
    assert all(cache.state(a) == "known" for a in addresses[:150])     # the others progressed
    _ticks(cache, now, 3)
    assert cache.stats["warm_unresolved_streak"] >= 2                   # only the stuck batch is left
                                                                        # (its retries back off)
    assert sorted(json.loads(store.read_text())) == sorted(addresses)   # and nothing was dropped


def test_a_stuck_batch_backs_off_on_the_funded_rpc_and_resets_on_a_complete_read(tmp_path):
    """Gamma LOW / Alpha on 366f28aa1: the cache reads through the custody RPC, and a batch that
    never resolves was re-read every pace window (~2 getMultipleAccounts a second) for the life of
    the process. Its next retry now waits min(60 s, pace x 2^streak); nothing is dropped, and the
    streak resets on a complete read."""
    addresses = [str(ps._pda([b"test", bytes([i])], ps.AMM_PROGRAM)) for i in range(150)]
    stuck = set(addresses[-100:])
    store = tmp_path / "slot_lane_pumpswap_pools.json"
    store.write_text(json.dumps(addresses))
    rpc = _Rpc({a: FX["pool_account"] for a in addresses})
    healthy, stuck_reads = [False], []

    def flaky(method, params):
        if method == "getMultipleAccounts" and stuck & set(params[0]):
            stuck_reads.append(1)
            if not healthy[0]:
                raise TimeoutError("this batch always times out")
        return rpc(method, params)
    now = [100.0]
    cache = ps.PumpSwapPoolCache(rpc=flaky, store_path=str(store), clock=lambda: now[0])
    for _ in range(480):                       # 240 s of half-second prefetch ticks
        cache.learn_pending()
        now[0] += 0.5
    assert all(cache.state(a) == "known" for a in addresses[:50])        # the other batch: normal pace
    assert 5 <= len(stuck_reads) <= 12                                    # backed off, not ~480
    assert sorted(json.loads(store.read_text())) == sorted(addresses)    # nothing dropped
    healthy[0] = True
    for _ in range(130):                       # within one 60 s backoff plus a tick
        cache.learn_pending()
        now[0] += 0.5
    assert all(cache.state(a) == "known" for a in addresses)
    assert cache.stats["warm_unresolved_streak"] == 0
    report = cache.take_warm_report()
    assert report["unresolved_streak_max"] >= 5 and report["stored"] == 150


# --- SOL03 (engineering review 2026-10-08): a null ITEM in a full-length answer -----------------
# warm() kept only a short or null-valued ANSWER unresolved; a full-length answer with one null item
# skipped the item, and the next save erased its persisted address. Option (a), pending the
# operator: a null item is unavailable and retries on the existing backoff.

def _two_persisted(tmp_path, fault):
    addresses = [POOL, str(ps._pda([b"other"], ps.AMM_PROGRAM))]
    store = tmp_path / "slot_lane_pumpswap_pools.json"
    store.write_text(json.dumps(addresses))
    healthy = _Rpc({a: FX["pool_account"] for a in addresses})
    calls = [0]

    def rpc(method, params):
        answer = healthy(method, params)
        if method == "getMultipleAccounts":
            calls[0] += 1
            fault(calls[0], params[0], answer["value"])
        return answer
    now = [100.0]
    cache = ps.PumpSwapPoolCache(rpc=rpc, store_path=str(store), clock=lambda: now[0])
    return cache, store, addresses, now


def test_review_sol03_a_null_pool_item_stays_persisted_and_is_warmed_later(tmp_path):
    def null_first_pool(call, _keys, values):
        if call == 1:
            values[0] = None
    cache, store, addresses, now = _two_persisted(tmp_path, null_first_pool)
    cache.learn_pending()
    assert sorted(json.loads(store.read_text())) == sorted(addresses)
    assert cache._warm_retry                                          # requeued, not forgotten
    _ticks(cache, now, 10)
    assert all(cache.state(a) == "known" for a in addresses)


def test_a_null_mint_item_leaves_its_pools_unresolved(tmp_path):
    def null_coin_mint(call, keys, values):
        if call == 2:
            values[keys.index(MINT)] = None
    cache, store, addresses, now = _two_persisted(tmp_path, null_coin_mint)
    cache.learn_pending()
    assert sorted(json.loads(store.read_text())) == sorted(addresses)
    assert sorted(cache._warm_retry) == sorted(addresses)
    _ticks(cache, now, 10)
    assert all(cache.state(a) == "known" for a in addresses)


def test_a_foreign_owner_item_is_still_a_definitive_drop(tmp_path):
    def foreign(call, _keys, values):
        if call == 1:
            values[0] = dict(values[0], owner="11111111111111111111111111111111")
    cache, store, addresses, now = _two_persisted(tmp_path, foreign)
    cache.learn_pending()
    assert len(json.loads(store.read_text())) == 1 and not cache._warm_retry


def test_a_pool_whose_item_stays_null_retries_on_the_backoff_for_ever(tmp_path):
    """Operator 2026-10-08 (SOL03 option (a), retry for ever): never retired, never a busy loop."""
    addresses = [str(ps._pda([b"test", bytes([i])], ps.AMM_PROGRAM)) for i in range(60)]
    dead = addresses[-1]
    store = tmp_path / "slot_lane_pumpswap_pools.json"
    store.write_text(json.dumps(addresses))
    rpc = _Rpc({a: FX["pool_account"] for a in addresses})
    reads = []

    def null_dead(method, params):
        answer = rpc(method, params)
        if method == "getMultipleAccounts" and dead in params[0]:
            reads.append(1)
            answer["value"][params[0].index(dead)] = None
        return answer
    now = [100.0]
    cache = ps.PumpSwapPoolCache(rpc=null_dead, store_path=str(store), clock=lambda: now[0])
    for _ in range(480):                                   # 240 s of half-second prefetch ticks
        cache.learn_pending()
        now[0] += 0.5
    assert all(cache.state(a) == "known" for a in addresses[:-1])
    assert 3 <= len(reads) <= 12                             # backed off, not ~480
    assert dead in json.loads(store.read_text()) and dead in cache._warm_retry
    assert cache.stats["warm_retry_n"] == len(cache._warm_retry) == 1   # the dead set, visible (Gamma)
