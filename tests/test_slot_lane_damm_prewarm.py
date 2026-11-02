"""Creation discovery warms the existing DAMM cache before a first copied buy; no funded sends."""
import base64
import copy
import json
import threading

import base58
import pytest
from solders.pubkey import Pubkey

from sniper.execution import meteora_damm_v2_buy as D
from sniper.services import memecoin_slot_signal as sig
from test_slot_lane_damm_pool_store import Chain, CLASSIC, T22, OTHER_POOL, _rows
from test_slot_lane_damm_v2 import _routed
from test_slot_lane_pool_prewarm import _MigrationWS


def _ledger(rows, wallets=(("armed", "solana", 1, 0, 0),)):
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE memecoin_wallets (source_user_id TEXT, real_chains TEXT, "
                 "real_approved INTEGER, paused INTEGER, excluded INTEGER)")
    conn.executemany("INSERT INTO memecoin_wallets VALUES (?,?,?,?,?)", wallets)
    conn.execute("CREATE TABLE memecoin_positions (id INTEGER PRIMARY KEY, source_user_id TEXT, "
                 "network_id TEXT, mint TEXT, executed_pool TEXT, quote_route_target TEXT, opened_at TEXT)")
    conn.executemany("INSERT INTO memecoin_positions VALUES (?,?,?,?,?,?,?)", rows)
    return conn


def test_recent_routes_are_bounded_armed_solana_executed_pool_targets():
    from sniper.persistence.db_memecoin import recent_armed_solana_route_targets
    now = "2026-10-09T16:43:23+00:00"
    valid = ("armed", "solana", CLASSIC["coin"], CLASSIC["pool"], "jupiter", "2026-10-08T23:50:46+00:00")
    rows = [(1, *valid), (2, *valid),
            (3, "watch", *valid[1:]),
            (4, "armed", "56", *valid[2:]),
            (5, *valid[:-1], "2026-10-07T23:50:46+00:00"),
            (6, *valid[:-1], "bad"),
            (7, *valid[:3], None, CLASSIC["pool"], valid[-1]),
            (8, *valid[:-1], "2026-10-09T17:00:00+00:00"),
            (9, *valid[:-1], "2026-10-09T16:00:00")]
    with _ledger(rows, wallets=(("armed", "solana,56", 1, 0, 0), ("watch", "solana", 0, 0, 0))) as conn:
        targets = recent_armed_solana_route_targets(conn, now_iso=now)
        assert targets == [{"coin": CLASSIC["coin"], "pool": CLASSIC["pool"], "age_s": 60757.0}]
        assert recent_armed_solana_route_targets(conn, now_iso=now, scan_limit=3) == []
        assert recent_armed_solana_route_targets(conn, now_iso=now, limit=0) == []


def test_ledger_target_hydrates_an_old_pool_off_the_entry_path(monkeypatch):
    calls = []
    def targets():
        calls.append(True)
        return [{"coin": CLASSIC["coin"], "pool": CLASSIC["pool"], "age_s": 60757.0}]
    rpc = Chain()
    cache = D.DammV2PoolCache(rpc=rpc, route_targets_fn=targets)
    cache.learn_pending()
    assert cache.stats["route_scan_ok"] is True and cache.stats["route_loaded"] == 1
    assert cache.get(CLASSIC["coin"])["learn_slot"] is None  # ledger has no chain slot
    assert rpc.calls == ["getAccountInfo", "getMultipleAccounts"]
    cache.learn_pending()
    assert len(calls) == 1  # slow scans remain off the lane's polling cadence
    rpc.calls.clear()
    buy, why = D.damm_slot_buy(_routed(CLASSIC), cache)
    assert why is None and buy.pool == CLASSIC["pool"] and rpc.calls == []


@pytest.mark.parametrize("bad", ["owner", "layout", "coin", "mint", "partial", "timeout", "rate_limit"])
def test_ledger_target_must_pass_existing_chain_validation(bad, monkeypatch):
    stamp = [100.0]
    monkeypatch.setattr(D.time, "monotonic", lambda: stamp[0])
    rpc = Chain()
    coin, pool = CLASSIC["coin"], CLASSIC["pool"]
    if bad == "owner":
        rpc.pools[pool] = dict(CLASSIC["pool_account"], owner="11111111111111111111111111111111")
    elif bad == "layout":
        rpc.pools[pool] = dict(CLASSIC["pool_account"], data=["AAAA", "base64"])
    elif bad == "coin":
        pool = T22["pool"]
    elif bad == "mint":
        rpc.mints[coin] = None
    elif bad == "partial":
        rpc.short = True
    elif bad == "timeout":
        rpc.down = True
    elif bad == "rate_limit":
        rpc = lambda *_: (_ for _ in ()).throw(RuntimeError("429 rate limit"))
    cache = D.DammV2PoolCache(rpc=rpc)
    assert cache.offer_route_target(coin, pool) == "queued"
    for _ in range(D.WARM_TRIES):
        cache.learn_pending()
        stamp[0] += 15.0
    assert cache.get(coin) is None and cache.stats["route_loaded"] == 0
    attempts = 1 if bad in ("owner", "coin") else D.WARM_TRIES
    assert cache.stats["route_read_failed"] == attempts and not cache._targets


@pytest.mark.parametrize("bad, reason", [("owner", "wrong_program"),
                                        ("discriminator", "wrong_layout"),
                                        ("coin", "coin_not_in_pool")])
def test_permanent_ledger_refusal_is_read_once_across_later_scans(monkeypatch, bad, reason):
    stamp = [100.0]
    monkeypatch.setattr(D.time, "monotonic", lambda: stamp[0])
    rpc = Chain()
    coin, pool = CLASSIC["coin"], CLASSIC["pool"]
    if bad == "owner":
        rpc.pools[pool] = dict(CLASSIC["pool_account"], owner=D.USDC_MINT)
    elif bad == "discriminator":
        raw = b"notapool" + base64.b64decode(CLASSIC["pool_account"]["data"][0])[8:]
        rpc.pools[pool] = dict(CLASSIC["pool_account"], data=[base64.b64encode(raw).decode(), "base64"])
    else:
        pool = T22["pool"]
    cache = D.DammV2PoolCache(rpc=rpc, route_targets_fn=lambda: [{"coin": coin, "pool": pool, "age_s": 0}])
    for _ in range(5):
        cache.learn_pending()
        stamp[0] += cache.ROUTE_SCAN_INTERVAL_S
    assert rpc.calls == ["getAccountInfo"]
    assert cache.stats["route_refused"] == 1
    assert cache.stats["route_last_failure"] == {"kind": "permanent", "reason": reason}
    assert cache.get(coin) is None and not cache._targets


def test_negative_route_identity_keeps_new_pool_and_other_mint_learnable():
    rpc = Chain()
    cache = D.DammV2PoolCache(rpc=rpc)
    assert cache.offer_route_target(CLASSIC["coin"], T22["pool"]) == "queued"
    cache.learn_pending()
    assert cache.offer_route_target(CLASSIC["coin"], T22["pool"]) == "refused"
    assert cache.offer_route_target(CLASSIC["coin"], CLASSIC["pool"]) == "queued"
    cache.learn_pending()
    assert cache.offer_route_target(T22["coin"], T22["pool"]) == "queued"
    cache.learn_pending()
    assert cache.get(CLASSIC["coin"])["pool"] == CLASSIC["pool"]
    assert cache.get(T22["coin"])["pool"] == T22["pool"]


def test_negative_route_cache_is_bounded_and_not_persisted(tmp_path):
    rpc = Chain()
    cache = D.DammV2PoolCache(rpc=rpc, store_cap=2, store_path=str(tmp_path / "pools.json"))
    coins = [str(Pubkey.from_bytes(bytes([i]) * 32)) for i in (8, 9, 10)]
    for coin in coins:
        assert cache.offer_route_target(coin, CLASSIC["pool"]) == "queued"
        cache.learn_pending()
    assert cache.offer_route_target(coins[-1], CLASSIC["pool"]) == "refused"
    assert cache.offer_route_target(coins[0], CLASSIC["pool"]) == "queued"  # oldest refusal evicted
    assert not (tmp_path / "pools.json").exists()
    restarted = D.DammV2PoolCache(rpc=rpc, store_path=cache.store_path)
    assert restarted.offer_route_target(coins[-1], CLASSIC["pool"]) == "queued"


@pytest.mark.parametrize("bad", ["missing", "envelope", "owner_missing", "layout", "base64",
                                "mint", "partial", "timeout", "rate_limit", "ban"])
def test_unknown_route_read_cools_down_then_recovers(monkeypatch, bad):
    stamp = [100.0]
    monkeypatch.setattr(D.time, "monotonic", lambda: stamp[0])
    chain, failing, calls = Chain(), [True], []
    def rpc(method, params):
        calls.append(method)
        if not failing[0]:
            return chain(method, params)
        if bad in ("timeout", "rate_limit", "ban"):
            raise RuntimeError({"timeout": "timed out", "rate_limit": "429 rate limit", "ban": "403 banned"}[bad])
        if method == "getAccountInfo":
            if bad == "missing":
                return {"value": None}
            if bad == "envelope":
                return {"error": {"code": -32000, "message": "read unavailable"}}
            if bad == "owner_missing":
                return {"value": {"data": CLASSIC["pool_account"]["data"]}}
            if bad in ("layout", "base64"):
                return {"value": dict(CLASSIC["pool_account"], data=["AAAA" if bad == "layout" else "!@", "base64"])}
        if method == "getMultipleAccounts":
            return {"value": [None, CLASSIC["mints"][1]] if bad == "mint" else []}
        return chain(method, params)
    cache = D.DammV2PoolCache(rpc=rpc)
    assert cache.offer_route_target(CLASSIC["coin"], CLASSIC["pool"]) == "queued"
    cache.learn_pending()
    attempted = len(calls)
    assert cache.get(CLASSIC["coin"]) is None
    failing[0] = False
    assert cache.offer_route_target(CLASSIC["coin"], CLASSIC["pool"]) == "cooldown"
    assert cache.stats["route_refused"] == 0
    assert cache.stats["route_last_failure"]["kind"] == "unread"
    cache.learn_pending()
    assert len(calls) == attempted  # recovery cannot turn a cooldown into an immediate retry
    stamp[0] += 15.0
    cache.learn_pending()
    assert cache.get(CLASSIC["coin"])["pool"] == CLASSIC["pool"]


def test_repeated_unknown_reads_exhaust_bounded_attempts_and_keep_a_cooldown(monkeypatch):
    stamp = [100.0]
    monkeypatch.setattr(D.time, "monotonic", lambda: stamp[0])
    rpc = Chain()
    rpc.down = True
    cache = D.DammV2PoolCache(rpc=rpc)
    cache.offer_route_target(CLASSIC["coin"], CLASSIC["pool"])
    for _ in range(D.WARM_TRIES):
        cache.learn_pending()
        stamp[0] += 15.0
    assert not cache._targets and len(rpc.calls) == D.WARM_TRIES
    # An exhausted target waits longer than a within-queue retry before a fresh scan can offer it.
    assert cache.offer_route_target(CLASSIC["coin"], CLASSIC["pool"]) == "cooldown"
    stamp[0] += 60.0
    rpc.down = False
    assert cache.offer_route_target(CLASSIC["coin"], CLASSIC["pool"]) == "queued"
    cache.learn_pending()
    assert cache.get(CLASSIC["coin"])["pool"] == CLASSIC["pool"]


def test_retry_cooldown_survives_refusal_cache_eviction(monkeypatch):
    monkeypatch.setattr(D.time, "monotonic", lambda: 100.0)
    rpc = Chain()
    rpc.down = True
    cache = D.DammV2PoolCache(rpc=rpc, store_cap=1)
    cache.offer_route_target(CLASSIC["coin"], CLASSIC["pool"])
    cache.learn_pending()
    cache.offer_route_target(T22["coin"], T22["pool"])
    for _ in range(4):
        cache.learn_pending()
    assert rpc.calls == ["getAccountInfo", "getAccountInfo"]


def test_route_failure_status_never_copies_provider_urls():
    def rpc(*_):
        raise TimeoutError("https://provider.invalid/?api-key=private-marker timed out")
    cache = D.DammV2PoolCache(rpc=rpc)
    cache.offer_route_target(CLASSIC["coin"], CLASSIC["pool"])
    cache.learn_pending()
    assert "private-marker" not in json.dumps(cache.stats)
    assert cache.stats["route_last_failure"] == {"kind": "unread", "reason": "TimeoutError"}


def test_route_refusal_cannot_block_confirmed_source_learning_or_replace_its_mapping():
    rpc = Chain()
    cache = D.DammV2PoolCache(rpc=rpc)
    rpc.pools[CLASSIC["pool"]] = dict(CLASSIC["pool_account"], owner=D.USDC_MINT)
    cache.offer_route_target(CLASSIC["coin"], CLASSIC["pool"])
    cache.learn_pending()
    rpc.pools[CLASSIC["pool"]] = CLASSIC["pool_account"]
    assert cache.queue(CLASSIC["signature"], CLASSIC["coin"]) == "queued"
    cache.learn_pending()
    assert cache.get(CLASSIC["coin"])["pool"] == CLASSIC["pool"]
    assert cache.offer_route_target(CLASSIC["coin"], T22["pool"]) == "known"
    rpc.calls.clear()
    cache.learn_pending()
    buy, why = D.damm_slot_buy(_routed(CLASSIC), cache)
    assert why is None and buy.pool == CLASSIC["pool"] and rpc.calls == []


def test_ledger_route_queue_deduplicates_and_bounds_work():
    rpc = Chain()
    cache = D.DammV2PoolCache(rpc=rpc)
    assert cache.offer_route_target(CLASSIC["coin"], CLASSIC["pool"]) == "queued"
    assert cache.offer_route_target(CLASSIC["coin"], CLASSIC["pool"]) == "pending"
    assert cache.offer_route_target("bad", CLASSIC["pool"]) == "invalid"
    assert cache.offer_route_target(T22["coin"], T22["pool"], age_s=float("nan")) == "invalid"
    assert cache.offer_route_target(T22["coin"], T22["pool"], age_s=D.DammV2PoolCache.HOT_RETENTION_S + 1) == "expired"
    for i in range(1, D.MAX_PENDING):
        coin = str(Pubkey.from_bytes(bytes([i]) * 32))
        assert cache.offer_route_target(coin, CLASSIC["pool"]) == "queued"
    assert cache.offer_route_target(T22["coin"], T22["pool"]) == "full"
    cache.learn_pending()
    assert len(cache._targets) == D.MAX_PENDING - 1
    assert rpc.calls == ["getAccountInfo", "getMultipleAccounts"]


def test_ledger_read_failure_stays_unknown_and_recovers(monkeypatch, caplog):
    stamp = [100.0]
    monkeypatch.setattr(D.time, "monotonic", lambda: stamp[0])
    def unavailable():
        raise RuntimeError("ledger unavailable")
    cache = D.DammV2PoolCache(rpc=Chain(), route_targets_fn=unavailable)
    cache.learn_pending()
    assert cache.stats["route_scan_ok"] is False and cache.stats["route_rows"] is None
    assert cache.stats["route_scan_failed"] == 1 and "route hydration" in caplog.text
    assert cache.queue(CLASSIC["signature"], CLASSIC["coin"]) == "queued"
    cache.learn_pending()
    assert cache.get(CLASSIC["coin"]) is not None  # existing source learner still works
    stamp[0] += cache.ROUTE_SCAN_INTERVAL_S
    cache.route_targets_fn = lambda: []
    cache.learn_pending()
    assert cache.stats["route_scan_ok"] is True and cache.stats["route_rows"] == 0


def test_recent_route_reader_closes_readonly_handle_on_success_and_failure():
    from sniper.services.memecoin_slot_lane import damm_route_targets_reader
    class Conn:
        def __init__(self, fail=False):
            self.closed, self.fail = False, fail
        def execute(self, *_):
            if self.fail:
                raise RuntimeError("read unavailable")
            return []
        def close(self):
            self.closed = True
    for fail in (False, True):
        conn = Conn(fail)
        read = damm_route_targets_reader(lambda: conn)
        if fail:
            with pytest.raises(RuntimeError, match="read unavailable"):
                read()
        else:
            assert read() == []
        assert conn.closed


def test_ledger_hydration_does_not_replace_a_concurrent_source_binding():
    chain = Chain()
    cache = None
    def rpc(method, params):
        values = chain(method, params)
        if method == "getMultipleAccounts":
            cache.adopt(CLASSIC["coin"], {"pool": OTHER_POOL, "learn_slot": CLASSIC["slot"] + 1})
        return values
    cache = D.DammV2PoolCache(rpc=rpc)
    cache.offer_route_target(CLASSIC["coin"], CLASSIC["pool"])
    cache.learn_pending()
    assert cache.get(CLASSIC["coin"])["pool"] == OTHER_POOL


def creation(name="initialize_pool", *, failed=False):
    pool_index = {"initialize_customizable_pool": 5, "initialize_pool": 6,
                  "initialize_pool_with_dynamic_config": 7}[name]
    state = D.parse_pool(base64.b64decode(CLASSIC["pool_account"]["data"][0]))
    keys = [D.PROGRAM, CLASSIC["pool"], state["mint_a"], state["mint_b"], D.POOL_AUTHORITY]
    accounts = [4] * (19 + pool_index - 5)
    accounts[pool_index:pool_index + 4] = [1, 4, 2, 3]
    return {"slot": CLASSIC["slot"], "meta": {"err": "failed" if failed else None},
            "transaction": {"message": {"accountKeys": keys, "instructions": [
                {"programIdIndex": 0, "accounts": accounts,
                 "data": base58.b58encode(D.hashlib.sha256(("global:" + name).encode()).digest()[:8]).decode()}]}}}


@pytest.mark.parametrize("name", ["initialize_pool", "initialize_customizable_pool",
                                 "initialize_pool_with_dynamic_config"])
def test_creation_is_warm_before_first_buy_and_survives_restart(tmp_path, name):
    tx = creation(name)
    rpc = Chain(txs={CLASSIC["signature"]: tx})
    path = str(tmp_path / "pools.json")
    cache = D.DammV2PoolCache(rpc=rpc, store_path=path)
    assert cache.offer_creation(CLASSIC["signature"]) == "queued"
    assert rpc.calls == []
    cache.learn_pending()
    buy, why = D.damm_slot_buy(_routed(CLASSIC), cache)
    assert why is None and buy.pool == CLASSIC["pool"]
    assert rpc.calls == ["getTransaction", "getAccountInfo", "getMultipleAccounts"]
    assert _rows(path)[0]["coin"] == CLASSIC["coin"]
    restarted = D.DammV2PoolCache(rpc=Chain(), store_path=path)
    restarted.learn_pending()
    assert restarted.get(CLASSIC["coin"])["pool"] == CLASSIC["pool"]


@pytest.mark.parametrize("bad", ["failed", "foreign_program", "wrong_mint", "wrong_owner", "short_mints"])
def test_unverified_creation_cannot_publish_facts(bad):
    tx = creation(failed=bad == "failed")
    if bad == "foreign_program":
        tx["transaction"]["message"]["accountKeys"][0] = D.USDC_MINT
    if bad == "wrong_mint":
        tx["transaction"]["message"]["accountKeys"][2] = D.USDC_MINT
    rpc = Chain(txs={CLASSIC["signature"]: tx})
    if bad == "wrong_owner":
        rpc.pools[CLASSIC["pool"]] = dict(CLASSIC["pool_account"], owner=D.USDC_MINT)
    rpc.short = bad == "short_mints"
    cache = D.DammV2PoolCache(rpc=rpc)
    cache.offer_creation(CLASSIC["signature"])
    cache.learn_pending()
    assert cache.get(CLASSIC["coin"]) is None


def test_unknown_read_retries_and_queue_is_bounded():
    rpc = Chain(txs={CLASSIC["signature"]: creation()})
    cache = D.DammV2PoolCache(rpc=rpc)
    cache.offer_creation(CLASSIC["signature"])
    rpc.down = True
    cache.learn_pending()
    assert cache.stats["creation_read_failed"] == 1 and cache.get(CLASSIC["coin"]) is None
    rpc.down = False
    cache.learn_pending()
    assert cache.get(CLASSIC["coin"]) is not None
    for i in range(D.MAX_PENDING):
        assert cache.offer_creation(base58.b58encode(bytes([i]) * 64).decode()) == "queued"
    assert cache.offer_creation(base58.b58encode(bytes([99]) * 64).decode()) == "full"


def test_creation_does_not_replace_a_pool_already_bound_by_a_source_buy():
    rpc = Chain(txs={CLASSIC["signature"]: creation()})
    cache = D.DammV2PoolCache(rpc=rpc)
    cache.learn_pool(CLASSIC["pool"], CLASSIC["coin"], slot=CLASSIC["slot"] + 1)
    known = copy.deepcopy(cache.get(CLASSIC["coin"]))
    cache.offer_creation(CLASSIC["signature"])
    cache.learn_pending()
    assert cache.get(CLASSIC["coin"]) == known


def test_creation_listener_reuses_migration_transport_and_ignores_swaps_and_spoofed_logs():
    stop, offered, status = threading.Event(), [], {}
    def frame(program=D.PROGRAM, name="InitializePool", err=None):
        return {"method": "logsNotification", "params": {"result": {"value": {
            "signature": CLASSIC["signature"], "err": err, "logs": [
                "Program %s invoke [1]" % program, "Program log: Instruction: " + name,
                "Program %s success" % program]}}}}
    ws = _MigrationWS(stop, [frame(name="Swap"), frame(program=D.USDC_MINT),
                             frame(err="failed"), frame()])
    thread = sig.start_migration_thread(offered.append, ws_url="wss://fixture", status=status,
        stop=stop, connect=lambda _: ws, program=D.PROGRAM, notification_target=D.creation_signature)
    thread.join(timeout=3)
    assert not thread.is_alive() and offered == [CLASSIC["signature"]]
    assert ws.sent[0]["params"][0] == {"mentions": [D.PROGRAM]}
    assert status["pools"] == 1


def test_router_cpi_initialization_and_loaded_addresses_are_discovered():
    tx = creation()
    msg = tx["transaction"]["message"]
    tx["meta"]["innerInstructions"] = [{"index": 0, "instructions": msg.pop("instructions")}]
    tx["meta"]["loadedAddresses"] = {"writable": msg["accountKeys"][1:], "readonly": []}
    msg["accountKeys"] = msg["accountKeys"][:1]
    assert D.created_pools_from_transaction(tx)[0][0] == CLASSIC["pool"]


def test_memory_and_store_remain_bounded(tmp_path):
    from test_slot_lane_damm_pool_store import T22
    cache = D.DammV2PoolCache(rpc=Chain(), store_cap=1, store_path=str(tmp_path / "pools.json"))
    cache.learn_pool(CLASSIC["pool"], CLASSIC["coin"], slot=10)
    cache.learn_pool(T22["pool"], T22["coin"], slot=11)
    cache._save()
    assert cache.get(CLASSIC["coin"]) is None and cache.get(T22["coin"]) is not None
    assert len(_rows(cache.store_path)) == 1 and cache.stats["cache_evicted"] == 1


def test_exotic_creation_keeps_each_mints_own_supply_decimals_and_safety():
    from test_slot_lane_damm_pool_store import T22, _mints_of
    from solders.pubkey import Pubkey
    from sniper.execution.raydium_cpmm_buy import coin_facts, quote_facts
    raw = bytearray(base64.b64decode(CLASSIC["pool_account"]["data"][0]))
    raw[D.OFF_MINT_A:D.OFF_MINT_A+32] = bytes(Pubkey.from_string(CLASSIC["coin"]))
    raw[D.OFF_MINT_B:D.OFF_MINT_B+32] = bytes(Pubkey.from_string(T22["coin"]))
    tx = creation()
    tx["transaction"]["message"]["accountKeys"][2:4] = [CLASSIC["coin"], T22["coin"]]
    rpc = Chain(pools={CLASSIC["pool"]: dict(CLASSIC["pool_account"],
                                           data=[base64.b64encode(raw).decode(), "base64"])},
                txs={CLASSIC["signature"]: tx})
    cache = D.DammV2PoolCache(rpc=rpc)
    cache.offer_creation(CLASSIC["signature"])
    cache.learn_pending()
    for row, other in [(CLASSIC, T22), (T22, CLASSIC)]:
        facts = cache.get(row["coin"])
        assert facts["coin_facts"] == coin_facts(_mints_of(row)[row["coin"]])
        assert facts["quote_facts"] == quote_facts(_mints_of(other)[other["coin"]])
        assert facts["quote"] == other["coin"]
    assert len(rpc.calls) == 3


def test_confirmed_creation_reads_confirmed_accounts():
    chain = Chain(txs={CLASSIC['signature']: creation()})
    def rpc(method, params):
        if method in ('getAccountInfo', 'getMultipleAccounts'):
            if params[1].get('commitment') != 'confirmed':
                return {'value': None}
        return chain(method, params)
    cache = D.DammV2PoolCache(rpc=rpc)
    cache.offer_creation(CLASSIC['signature'])
    cache.learn_pending()
    assert cache.get(CLASSIC['coin']) is not None


def test_shared_discovery_key_preserves_other_solana_builders(monkeypatch):
    from types import SimpleNamespace
    from sniper.services import memecoin_slot_lane as lane
    import sniper.execution.pump_curve_buy as curve
    import sniper.execution.pumpswap_buy as amm
    import sniper.execution.raydium_cpmm_buy as cpmm
    import sniper.execution.raydium_launchlab_buy as launch
    monkeypatch.setenv('ONCHAIN_WATCH_HELIUS_RPC_URL', 'https://watch.invalid')
    monkeypatch.setattr(curve, 'SlotLaneCurveBuilder', lambda **kw: SimpleNamespace())
    monkeypatch.setattr(amm, 'SlotLanePumpSwapBuilder', lambda **kw: SimpleNamespace(rpc=lambda *a: None))
    monkeypatch.setattr(cpmm, 'lane_mode', lambda: 'off')
    monkeypatch.setattr(launch, 'lane_mode', lambda: 'off')
    monkeypatch.setattr(D, 'lane_mode', lambda: 'live')
    monkeypatch.setattr(lane, 'routed_mode', lambda: 'live')
    monkeypatch.setattr(sig, '_shares_funded_key', lambda _: True)
    monkeypatch.setattr(lane.cfg, 'solana_read_rpc', lambda: 'https://shared.invalid')
    monkeypatch.setattr(lane.cfg, 'MEMECOIN_SLOT_LANE_MIGRATION_PREWARM', False)
    monkeypatch.setattr(lane, 'start_slot_lane', lambda rc, **kwargs: kwargs['builders'])
    builders = lane.start_from_context({'pubkey': CLASSIC['coin']}, log=lambda _: None)
    assert set(builders) == {'pump_curve', 'pump_amm'}
