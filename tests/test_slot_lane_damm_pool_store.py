"""The DAMM v2 coin -> pool cache survives a restart, and follows a coin that moves pool (Alpha GO,
operator yes 2026-10-02 14:2xZ).

The cache lived only in memory, and 33 runner starts on 2026-10-02 wiped it: B3A4k63n was learned
at 09:56Z and handed off pool_unknown again at 12:49Z and 13:55Z. Now each learn writes one row
{coin, pool, slot} (the learn slot: the source transaction's slot) to a small JSON store, on the
cache's own learner thread. At start the learner re-reads every stored pool and its mints from chain
in bulk, through the same checks as a first learn, so every fact a build signs with comes from
chain, never from the file. What the chain denies is dropped; what it does not answer is kept.

REPOINT: a later cp-amm routed buy of a KNOWN coin queues its confirmed transaction; the learner
reads it and, when the coin swapped on another pool, re-learns that pool through learn_pool's
checks. Never a raw swap of the key, never on the lane thread.

The accounts are real mainnet reads (tests/fixtures/memecoin/damm_v2_sol_pools.json).
"""
import copy
import json
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from sniper.execution import meteora_damm_v2_buy as D
from test_slot_lane_damm_v2 import EXTENSIONS, _routed

FX = json.loads((Path(__file__).parent / "fixtures/memecoin/damm_v2_sol_pools.json").read_text())
CLASSIC, T22 = FX["pools"]
OTHER_POOL = str(Pubkey.from_bytes(bytes([7] * 32)))       # a second cp-amm pool for CLASSIC's coin


@pytest.fixture(autouse=True)
def _recheck_now(monkeypatch):
    monkeypatch.setattr(D.DammV2PoolCache, "RECHECK_AFTER_S", 0.0)


def _mints_of(row):
    import base64
    state = D.parse_pool(base64.b64decode(row["pool_account"]["data"][0]))
    return {state["mint_a"]: row["mints"][0], state["mint_b"]: row["mints"][1]}


class Chain:
    """The chain as the learner reads it: pool accounts and mints by address, transactions by
    signature. `down` makes every read raise (a timeout); `short` drops the last value of a
    getMultipleAccounts answer (a partial answer)."""

    def __init__(self, rows=(CLASSIC, T22), pools=None, mints=None, txs=None):
        self.pools = {r["pool"]: r["pool_account"] for r in rows}
        self.mints = {}
        for r in rows:
            self.mints.update(_mints_of(r))
        self.txs = {r["signature"]: r["tx"] for r in rows}
        self.pools.update(pools or {})
        self.mints.update(mints or {})
        self.txs.update(txs or {})
        self.calls, self.down, self.short = [], False, False

    def __call__(self, method, params):
        self.calls.append(method)
        if self.down:
            raise RuntimeError("timed out")
        if method == "getTransaction":
            assert params[1]["maxSupportedTransactionVersion"] == 1
            return self.txs.get(params[0])
        if method == "getAccountInfo":
            return {"value": self.pools.get(params[0])}
        if method == "getMultipleAccounts":
            table = self.mints if params[1]["encoding"] == "jsonParsed" else self.pools
            values = [table.get(a) for a in params[0]]
            return {"value": values[:-1] if self.short else values}
        raise AssertionError(method)


def _rows(path):
    return json.loads(Path(path).read_text())


def _learn(cache, row):
    assert cache.queue(row["signature"], row["coin"]) == "queued"
    cache.learn_pending()
    assert cache.get(row["coin"]) is not None


def _store(tmp_path, rows):
    path = tmp_path / "slot_lane_damm_v2_pools.json"
    path.write_text(json.dumps(rows))
    return str(path)


def _row(fx, slot=None, pool=None):
    return {"coin": fx["coin"], "pool": pool or fx["pool"], "slot": fx["slot"] if slot is None else slot}


# --- across a restart ------------------------------------------------------------------------------

def test_a_restart_warms_the_learned_pool_from_chain_without_reading_its_transaction(tmp_path):
    path = str(tmp_path / "slot_lane_damm_v2_pools.json")
    first = D.DammV2PoolCache(rpc=Chain(), store_path=path)
    _learn(first, CLASSIC)
    assert _rows(path) == [_row(CLASSIC)]                      # read targets and the learn slot only
    assert first.get(CLASSIC["coin"])["learn_slot"] == CLASSIC["slot"]

    chain = Chain()
    restarted = D.DammV2PoolCache(rpc=chain, store_path=path)
    assert restarted.get(CLASSIC["coin"]) is None             # nothing is trusted from the file
    restarted.learn_pending()                                 # the learner's first pass
    assert restarted.get(CLASSIC["coin"]) == first.get(CLASSIC["coin"])
    assert chain.calls == ["getMultipleAccounts", "getMultipleAccounts"]     # pools, then mints
    assert restarted.stats["warm_rows"] == 1 and restarted.stats["warm_loaded"] == 1
    buy, why = D.damm_slot_buy(_routed(CLASSIC), restarted)
    assert why is None and buy.pool == CLASSIC["pool"]        # built, not pool_unknown after a restart


@pytest.mark.parametrize("case", ["gone", "foreign_owner", "not_a_pool", "coin_not_in_pool", "mint_gone"])
def test_a_stored_pool_the_chain_denies_is_dropped_from_memory_and_the_file(tmp_path, case):
    pools, mints, row = {}, {}, _row(CLASSIC)
    if case == "gone":
        pools[CLASSIC["pool"]] = None
    elif case == "foreign_owner":
        pools[CLASSIC["pool"]] = dict(CLASSIC["pool_account"], owner="11111111111111111111111111111111")
    elif case == "not_a_pool":
        pools[CLASSIC["pool"]] = dict(CLASSIC["pool_account"], data=["AAAA", "base64"])
    elif case == "coin_not_in_pool":
        row = dict(row, pool=T22["pool"])                     # a real cp-amm pool, another coin's
    elif case == "mint_gone":
        mints[CLASSIC["coin"]] = None
    path = _store(tmp_path, [row, _row(T22)])
    cache = D.DammV2PoolCache(rpc=Chain(pools=pools, mints=mints), store_path=path)
    cache.learn_pending()
    assert cache.get(CLASSIC["coin"]) is None and cache.get(T22["coin"]) is not None
    assert cache.stats["warm_dropped"] == 1 and cache.stats["warm_loaded"] == 1
    assert _rows(path) == [_row(T22)]


@pytest.mark.parametrize("failure", ["down", "short"])
def test_an_unread_warm_keeps_the_row_loads_nothing_and_is_read_at_the_next_start(tmp_path, failure):
    path = _store(tmp_path, [_row(CLASSIC)])
    chain = Chain()
    setattr(chain, failure, True)
    cache = D.DammV2PoolCache(rpc=chain, store_path=path)
    for _ in range(D.WARM_TRIES + 2):
        cache.learn_pending()
    assert cache.get(CLASSIC["coin"]) is None
    assert cache.stats["warm_unread"] == 1 and cache.stats["warm_dropped"] == 0
    assert len([c for c in chain.calls if c == "getMultipleAccounts"]) == D.WARM_TRIES   # then it stops
    chain.down = chain.short = False
    _learn(cache, T22)                                         # a live learn rewrites the file...
    assert _rows(path) == [_row(CLASSIC), _row(T22)]           # ...and keeps the unread row
    restarted = D.DammV2PoolCache(rpc=Chain(), store_path=path)
    restarted.learn_pending()
    assert restarted.get(CLASSIC["coin"]) is not None


@pytest.mark.parametrize("text", ["{not json", json.dumps({"coin": "x"}), json.dumps([7, None, "x"]),
                                  json.dumps([{"coin": "not a pubkey", "pool": CLASSIC["pool"], "slot": 1}]),
                                  json.dumps([{"coin": CLASSIC["coin"], "pool": CLASSIC["pool"]}]),
                                  json.dumps([{"coin": CLASSIC["coin"], "pool": CLASSIC["pool"], "slot": "9"}]),
                                  json.dumps([{"coin": CLASSIC["coin"], "pool": CLASSIC["pool"], "slot": True}])])
def test_a_corrupt_store_or_row_warms_nothing_and_breaks_nothing(tmp_path, text):
    path = tmp_path / "slot_lane_damm_v2_pools.json"
    path.write_text(text)
    chain = Chain()
    cache = D.DammV2PoolCache(rpc=chain, store_path=str(path))
    cache.learn_pending()
    assert chain.calls == [] and cache.get(CLASSIC["coin"]) is None


def test_a_bad_row_beside_good_ones_is_skipped_alone(tmp_path):
    path = _store(tmp_path, [{"coin": "bad", "pool": CLASSIC["pool"], "slot": 1}, _row(CLASSIC)])
    cache = D.DammV2PoolCache(rpc=Chain(), store_path=path)
    cache.learn_pending()
    assert cache.get(CLASSIC["coin"])["pool"] == CLASSIC["pool"]


def test_two_rows_for_one_coin_keep_the_newer_learn_slot(tmp_path):
    chain = Chain(pools={OTHER_POOL: CLASSIC["pool_account"]})
    for order in (1, -1):                                       # whichever comes first in the file
        path = _store(tmp_path, [_row(CLASSIC), _row(CLASSIC, slot=CLASSIC["slot"] + 50, pool=OTHER_POOL)][::order])
        assert D.load_store(path) == [_row(CLASSIC, slot=CLASSIC["slot"] + 50, pool=OTHER_POOL)]
    cache = D.DammV2PoolCache(rpc=chain, store_path=path)
    cache.learn_pending()
    facts = cache.get(CLASSIC["coin"])
    assert facts["pool"] == OTHER_POOL and facts["learn_slot"] == CLASSIC["slot"] + 50


def test_the_store_keeps_the_newest_learn_slots_up_to_its_cap(tmp_path):
    coins = [str(Pubkey.from_bytes(bytes([i + 1] * 32))) for i in range(5)]
    rows = [{"coin": c, "pool": CLASSIC["pool"], "slot": 100 + i} for i, c in enumerate(coins)]
    cache = D.DammV2PoolCache(rpc=Chain(), store_path=_store(tmp_path, rows), store_cap=3)
    assert [r["coin"] for r in cache._warm] == coins[2:]      # loaded: the newest three
    path = str(tmp_path / "capped.json")
    capped = D.DammV2PoolCache(rpc=Chain(), store_path=path, store_cap=1)
    _learn(capped, CLASSIC)
    _learn(capped, T22)
    assert _rows(path) == [_row(T22)]                          # written: the newest learn slot


def test_recent_source_lookup_survives_creation_churn_then_expires(tmp_path, monkeypatch):
    stamp = [100.0]
    monkeypatch.setattr(D.time, "monotonic", lambda: stamp[0])
    cache = D.DammV2PoolCache(rpc=Chain(), store_cap=2, store_path=str(tmp_path / "pools.json"))
    cache.learn_pool(CLASSIC["pool"], CLASSIC["coin"], slot=1)
    cache.learn_pool(T22["pool"], T22["coin"], slot=2)
    cache.get(CLASSIC["coin"])
    newer = str(Pubkey.from_bytes(bytes([13]) * 32))
    cache._remember(newer, {"pool": OTHER_POOL, "learn_slot": 3})
    cache._save()
    assert CLASSIC["coin"] in cache._by_coin and T22["coin"] not in cache._by_coin
    assert {r["coin"] for r in _rows(cache.store_path)} == {CLASSIC["coin"], newer}
    stamp[0] += cache.HOT_RETENTION_S + 0.1
    newest = str(Pubkey.from_bytes(bytes([14]) * 32))
    cache._remember(newest, {"pool": OTHER_POOL, "learn_slot": 4})
    assert CLASSIC["coin"] not in cache._by_coin and len(cache._by_coin) == 2
    assert len(cache._hot) <= cache.store_cap


def test_ledger_retention_uses_event_age_and_does_not_reset_it(monkeypatch):
    stamp = [100.0]
    monkeypatch.setattr(D.time, "monotonic", lambda: stamp[0])
    cache = D.DammV2PoolCache(rpc=Chain(), store_cap=1)
    cache.offer_route_target(CLASSIC["coin"], CLASSIC["pool"], age_s=cache.HOT_RETENTION_S - 1)
    cache.learn_pending()
    assert CLASSIC["coin"] in cache._by_coin
    stamp[0] += 2
    cache.learn_pool(T22["pool"], T22["coin"], slot=CLASSIC["slot"] + 1)
    assert CLASSIC["coin"] not in cache._by_coin  # ledger lookback doesn't become a fresh source touch


def test_older_ledger_hint_cannot_overwrite_a_concurrent_source_adoption(monkeypatch):
    import threading
    stamp = [100.0]
    monkeypatch.setattr(D.time, "monotonic", lambda: stamp[0])
    cache = D.DammV2PoolCache(rpc=None, store_cap=2)
    facts = {"pool": CLASSIC["pool"], "learn_slot": 1}
    cache._remember(CLASSIC["coin"], facts)
    cache._remember(T22["coin"], {"pool": T22["pool"], "learn_slot": 10})
    paused, resume, adopted = threading.Event(), threading.Event(), threading.Event()

    class AdoptionLock:
        """Observe whether source adoption can enter while the ledger's stale write is paused."""
        def __init__(self):
            self.lock, self.checked, self.blocked = threading.RLock(), threading.Event(), None
        def __enter__(self):
            if threading.current_thread().name == "source-adopt" and not self.checked.is_set():
                acquired = self.lock.acquire(blocking=False)
                self.blocked = not acquired
                self.checked.set()
                if acquired:
                    return self
            self.lock.acquire()
            return self
        def __exit__(self, *_):
            self.lock.release()

    class PausedHot(dict):
        def __setitem__(self, coin, value):
            if threading.current_thread().name == "ledger-hint":
                paused.set()  # value is the old max, computed before any source adoption
                assert resume.wait(2)
            super().__setitem__(coin, value)

    cache._facts_lock, cache._hot = AdoptionLock(), PausedHot()
    errors = []
    def hint():
        try:
            cache.offer_route_target(CLASSIC["coin"], CLASSIC["pool"],
                                     age_s=cache.HOT_RETENTION_S - 1)
        except BaseException as exc:
            errors.append(exc)
    def adopt():
        try:
            cache.adopt(CLASSIC["coin"], facts)
        except BaseException as exc:
            errors.append(exc)
        finally:
            adopted.set()

    ledger = threading.Thread(target=hint, name="ledger-hint")
    source = threading.Thread(target=adopt, name="source-adopt")
    ledger.start()
    try:
        assert paused.wait(2)
        source.start()
        assert cache._facts_lock.checked.wait(2)
        if not cache._facts_lock.blocked:
            assert adopted.wait(2)  # deterministically force the original lost-update interleaving
    finally:
        resume.set()
        ledger.join(2)
        if source.ident is not None:
            source.join(2)
    assert not ledger.is_alive() and not source.is_alive() and not errors
    stamp[0] = 102.0
    newer = str(Pubkey.from_bytes(bytes([13]) * 32))
    cache._remember(newer, {"pool": OTHER_POOL, "learn_slot": 20})
    assert CLASSIC["coin"] in cache._by_coin  # fresh adoption wins over creation churn
    assert cache._hot[CLASSIC["coin"]] == 100.0 and not cache._used


# --- nothing on the lane thread ----------------------------------------------------------------------

def test_get_queue_recheck_and_conversion_never_touch_the_chain_or_the_file(tmp_path):
    path = _store(tmp_path, [_row(CLASSIC)])
    learned = D.DammV2PoolCache(rpc=Chain(), store_path=path)
    learned.learn_pending()
    before = Path(path).read_bytes()

    def forbidden(*_a, **_k):
        raise AssertionError("I/O on the lane thread")
    learned.rpc = forbidden
    assert learned.get(CLASSIC["coin"]) is not None and learned.get("missing") is None
    assert learned.queue(T22["signature"], T22["coin"]) == "queued"
    buy, why = D.damm_slot_buy(_routed(CLASSIC), learned)      # known: built, and a recheck queued
    assert why is None and CLASSIC["coin"] in learned._recheck
    assert Path(path).read_bytes() == before


def test_a_warm_refuses_a_coin_whose_fresh_mint_now_carries_an_uncovered_extension(tmp_path):
    path = _store(tmp_path, [_row(T22)])
    mint = copy.deepcopy(_mints_of(T22)[T22["coin"]])
    info = mint["data"]["parsed"]["info"]
    info["extensions"] = list(info.get("extensions") or []) + [EXTENSIONS["permanentDelegate"]]
    cache = D.DammV2PoolCache(rpc=Chain(mints={T22["coin"]: mint}), store_path=path)
    cache.learn_pending()
    assert cache.get(T22["coin"])["coin_refusal"] == "extension_permanentDelegate"
    assert D.damm_slot_buy(_routed(T22), cache) == (None, "coin_extension_permanentDelegate")


# --- repoint --------------------------------------------------------------------------------------------

def _moved_tx(fx, pool, slot):
    tx = copy.deepcopy(fx["tx"])
    keys = tx["transaction"]["message"]["accountKeys"]
    tx["transaction"]["message"]["accountKeys"] = [pool if k == fx["pool"] else k for k in keys]
    tx["slot"] = slot
    return tx


def _known(tmp_path, chain):
    cache = D.DammV2PoolCache(rpc=chain, store_path=str(tmp_path / "slot_lane_damm_v2_pools.json"))
    _learn(cache, CLASSIC)
    chain.calls.clear()
    return cache


def test_a_known_coins_later_buy_on_another_pool_repoints_it_through_the_learn_checks(tmp_path):
    later = CLASSIC["slot"] + 900
    chain = Chain(pools={OTHER_POOL: CLASSIC["pool_account"]},
                  txs={"Later1": _moved_tx(CLASSIC, OTHER_POOL, later)})
    cache = _known(tmp_path, chain)
    buy, _why = D.damm_slot_buy(_routed(dict(CLASSIC, signature="Later1")), cache)
    assert buy.pool == CLASSIC["pool"]                         # this buy builds on what is known
    cache.learn_pending()
    facts = cache.get(CLASSIC["coin"])
    assert facts["pool"] == OTHER_POOL and facts["learn_slot"] == later
    assert chain.calls == ["getTransaction", "getAccountInfo", "getMultipleAccounts"]   # learn_pool
    assert cache.stats["repointed"] == 1
    assert _rows(tmp_path / "slot_lane_damm_v2_pools.json") == [_row(CLASSIC, slot=later, pool=OTHER_POOL)]


def test_a_later_buy_on_the_same_pool_changes_nothing(tmp_path):
    chain = Chain(txs={"Later1": _moved_tx(CLASSIC, CLASSIC["pool"], CLASSIC["slot"] + 900)})
    cache = _known(tmp_path, chain)
    before = cache.get(CLASSIC["coin"])
    D.damm_slot_buy(_routed(dict(CLASSIC, signature="Later1")), cache)
    cache.learn_pending()
    assert cache.get(CLASSIC["coin"]) is before and cache.stats["recheck_same"] == 1
    assert chain.calls == ["getTransaction"]


@pytest.mark.parametrize("bad", ["foreign_owner", "unread"])
def test_a_repoint_the_learn_checks_refuse_keeps_the_known_pool(tmp_path, bad):
    account = (dict(CLASSIC["pool_account"], owner="11111111111111111111111111111111")
               if bad == "foreign_owner" else CLASSIC["pool_account"])
    chain = Chain(pools={OTHER_POOL: account},
                  txs={"Later1": _moved_tx(CLASSIC, OTHER_POOL, CLASSIC["slot"] + 900)})
    cache = _known(tmp_path, chain)
    D.damm_slot_buy(_routed(dict(CLASSIC, signature="Later1")), cache)
    if bad == "unread":
        real = chain.__call__

        class Flaky:
            def __call__(self, method, params):
                if method == "getAccountInfo":
                    raise RuntimeError("timed out")
                return real(method, params)
        cache.rpc = Flaky()
    cache.learn_pending()
    assert cache.get(CLASSIC["coin"])["pool"] == CLASSIC["pool"]
    assert cache.stats["recheck_failed"] == 1 and cache.stats["repointed"] == 0


def test_an_older_transaction_never_undoes_a_newer_learn(tmp_path):
    chain = Chain(pools={OTHER_POOL: CLASSIC["pool_account"]},
                  txs={"Older1": _moved_tx(CLASSIC, OTHER_POOL, CLASSIC["slot"] - 5)})
    cache = _known(tmp_path, chain)
    D.damm_slot_buy(_routed(dict(CLASSIC, signature="Older1")), cache)
    cache.learn_pending()
    assert cache.get(CLASSIC["coin"])["pool"] == CLASSIC["pool"]
    assert chain.calls == ["getTransaction"]


def test_an_unconfirmed_recheck_is_retried_then_dropped_and_one_runs_per_pass(tmp_path):
    chain = Chain()
    cache = _known(tmp_path, chain)
    for sig in ("Pending1", "Pending2"):                       # never confirmed
        cache.recheck(sig, CLASSIC["coin"])
    assert list(cache._recheck) == [CLASSIC["coin"]]           # one in flight per coin
    for _ in range(D.LEARN_TRIES):
        cache.learn_pending()
    assert chain.calls == ["getTransaction"] * D.LEARN_TRIES and not cache._recheck


def test_a_recheck_waits_for_its_delay_and_does_not_wake_the_learner(tmp_path, monkeypatch):
    monkeypatch.setattr(D.DammV2PoolCache, "RECHECK_AFTER_S", 60.0)
    chain = Chain()
    cache = _known(tmp_path, chain)
    cache._wake.clear()
    cache.recheck("Later1", CLASSIC["coin"])
    assert not cache._wake.is_set()
    cache.learn_pending()
    assert chain.calls == [] and CLASSIC["coin"] in cache._recheck
