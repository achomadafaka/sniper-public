"""V4 pool discovery on chain 4663: filter on pool-id SHAPE, validate on OUR PoolManager.

Regression pin for 2026-08-30. Discovery kept a pool only when "v4" appeared in the provider's
`dex` attribution string. GeckoTerminal attributes pools on the PoolManager we are configured for
to `pons-v2-dex`, so real, deep, quotable pools were discarded and funded entries aborted
`no_route`. See docs/audits/2026-08-30-4663-entry-gate-parity.md.
"""
import pytest

from sniper.execution import evm_v4_quote as V

_REAL_STORED_POOLS = V._stored_pools_for_mint     # the two pool-store tests below restore it
from sniper.execution.evm_wallet import CHAIN_QUOTE

MINT = "0xb2a02fc6fcdad101dc248f540d8f8ba8889c9202"
V4_POOL = "0x08c72ca057714f896cf3f328d9f5f3abf0dcb62aa1a6fc033da49d313df9725c"   # 66 chars
V2_PAIR = "0x474a69a666afda3d1aedc428f0d8e439f1b00fd4"                           # 42 chars
OUR_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
FOREIGN_MANAGER = "0xdeadbeef00000000000000000000000000000001"
# The REAL 4663 quote token. `v4_quote_amount_out` derives is_buy from it, so a placeholder
# here makes the function bail on the currency check and every assertion below passes for the
# wrong reason.
WETH = CHAIN_QUOTE["4663"]["token"]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    V._pool_cache.clear()
    V._key_cache.clear()
    V._reserve_cache.clear()
    monkeypatch.setattr(V, "_stored_pool_key", lambda *_args: None)
    monkeypatch.setattr(V, "_store_pool_key", lambda *_args: None)
    # The pool store reads the REAL ledger, and MINT is a real 4663 coin that now has stored pools
    # there: without this every discovery assertion saw 8 extra live pools (2026-09-13). Same fix
    # as 6ce88311 in test_memecoin_evm_v2_entry.py.
    monkeypatch.setattr(V, "_stored_pools_for_mint", lambda *_a, **_k: [])
    V.begin_quote()
    yield
    V.begin_quote()


def _gt(pools):
    """GeckoTerminal /pools payload. `dex` is deliberately the REAL mis-attribution."""
    return lambda _url: {"data": [
        {"id": f"robinhood_{pid}", "attributes": {"reserve_in_usd": str(res)},
         "relationships": {"dex": {"data": {"id": dex}}}}
        for pid, res, dex in pools]}


def test_a_pool_whose_dex_name_lacks_v4_is_still_discovered():
    """THE FIX. `pons-v2-dex` contains no "v4"; the pool is on our PoolManager regardless.

    Mutation check: restore `if "v4" not in str(dex): continue` and this goes red.
    """
    pools = V._pools_for_mint(
        "4663", MINT,
        gt_http=_gt([(V4_POOL, 26668.85, "pons-v2-dex")]),
        ds_http=lambda _url: None)
    assert pools == [V4_POOL], (
        "a V4 pool id was discarded because the provider's dex NAME lacked 'v4'. "
        "Discovery must filter on pool-id shape, not on a provider's attribution string.")


def test_an_address_shaped_id_is_not_a_v4_pool():
    """A 42-char id is a V2-style pair address. It must never reach the V4 PoolKey resolver."""
    pools = V._pools_for_mint(
        "4663", MINT,
        gt_http=_gt([(V2_PAIR, 20385.05, "pons-v2")]),
        ds_http=lambda _url: None)
    assert pools == [], "an address-shaped pool id was admitted as a V4 pool"


def test_discovery_unions_both_providers_and_dedupes():
    """Neither provider alone covers 4663; each has a blind spot the other fills."""
    other = "0x" + "cd" * 32
    ds = lambda _url: {"pairs": [
        {"chainId": "robinhood", "pairAddress": V4_POOL,
         "baseToken": {"address": MINT}, "liquidity": {"usd": 28685.99}},
        {"chainId": "robinhood", "pairAddress": other,
         "baseToken": {"address": MINT}, "liquidity": {"usd": 999.0}}]}
    pools = V._pools_for_mint("4663", MINT,
                              gt_http=_gt([(V4_POOL, 26668.85, "pons-v2-dex")]), ds_http=ds)
    assert pools == [V4_POOL, other], "union must dedupe by pool id and rank by reserve"
    assert V.top_pool_reserve_usd("4663", MINT) == 28685.99, "deepest reserve across both sources"


def test_dexscreener_can_filter_one_exact_pair():
    other = "0x" + "33" * 20
    direct = "0x" + "aa" * 32
    deeper = "0x" + "cc" * 32
    unrelated = "0x" + "bb" * 32
    ds = lambda _url: {"pairs": [
        {"chainId": "robinhood", "pairAddress": direct,
         "baseToken": {"address": MINT}, "quoteToken": {"address": WETH},
         "liquidity": {"usd": 20_000}},
        {"chainId": "robinhood", "pairAddress": unrelated,
         "baseToken": {"address": MINT}, "quoteToken": {"address": other},
         "liquidity": {"usd": 99_000_000}},
        {"chainId": "robinhood", "pairAddress": deeper,
         "baseToken": {"address": MINT}, "quoteToken": {"address": WETH},
         "liquidity": {"usd": 30_000}},
    ]}

    pools, failed = V._ds_pools_ranked("4663", MINT, ds_http=ds, pair_token=WETH)

    assert failed is False
    assert pools == [(30_000.0, deeper), (20_000.0, direct)]


def test_a_total_provider_blackout_still_reads_as_rate_limit():
    """Both indexers dark and no chain scan configured is the original blindness label."""
    V._pools_for_mint("4663", MINT, gt_http=lambda _u: None, ds_http=lambda _u: None)
    assert V.last_provider_blind() == "rate_limit", "both providers dark must read as blind"
    assert ("4663", MINT.lower()) not in V._pool_cache, "a refusal is never cached as 'no pools'"


# --------------------------------------------------------------------------------------------
# 2026-09-03: an EMPTY discovery is unproven, not "no route". This section REPLACES a test that
# asserted "one live provider answering is enough to trust a no-pool answer". A live probe of
# fresh PoolManager Initialize events falsified that premise: the log index surfaced new pools
# 2.1s, 2.7s, 2.3s ... and 61.0s, 58.6s after their block, on both endpoints, and the funded
# endpoint threw HTTPError on eth_getLogs three times running on one of them. An indexer saying
# "no pairs" about an 18-second-old pool is not evidence of anything. ANY (0xd7eec4ab, attempt
# 7d4f6f4f, DegenCapitalLLC) was refused `no_route` on exactly that, quotable at fee 0 minutes on.
# --------------------------------------------------------------------------------------------

def _no_ds(_url):
    return {"pairs": []}           # DexScreener ANSWERED, honestly, "nothing indexed yet"


def test_a_failed_chain_scan_with_an_empty_indexer_is_blind_not_absent(monkeypatch):
    """The chain scan is the only source that can see a pool younger than the indexer lag. If it
    FAILED, one indexer answering empty proves nothing, and the miss must carry a provider code
    the requote re-enters on -- never `no_route`, which is terminal."""
    monkeypatch.setattr(V, "_onchain_pools_for_mint", lambda *_a, **_k: ([], True))
    V._blind.discovery_rpc_url = "http://rpc"
    try:
        pools = V._pools_for_mint("4663", MINT, gt_http=lambda _u: None, ds_http=_no_ds)
    finally:
        V._blind.discovery_rpc_url = None
    assert pools == []
    assert V.last_provider_blind() == "discovery_rpc_failed"
    assert ("4663", MINT.lower()) not in V._pool_cache, "blindness is never cached"


def test_every_source_answering_empty_is_unproven_not_no_route():
    """All three empty is still not proof for a fresh pool; the cost of saying so is one extra
    ladder pass on a genuinely unroutable coin, the cost of `no_route` was the entry."""
    pools = V._pools_for_mint("4663", MINT, gt_http=lambda _u: {"data": []}, ds_http=_no_ds)
    assert pools == []
    assert V.last_provider_blind() == "discovery_unproven"
    assert ("4663", MINT.lower()) not in V._pool_cache, (
        "an empty answer used to be cached for 600s -- ten minutes of 'no pool' on a coin whose "
        "wallet was buying it")


def test_a_found_pool_is_still_cached():
    pools = V._pools_for_mint("4663", MINT,
                              gt_http=_gt([(V4_POOL, 26668.85, "pons-v2-dex")]),
                              ds_http=lambda _url: None)
    assert pools == [V4_POOL]
    assert V.last_provider_blind() is None
    assert V._pool_cache[("4663", MINT.lower())][1] == [V4_POOL]


def test_the_chain_scan_falls_back_to_an_identity_checked_read_endpoint(monkeypatch):
    """Primary raises on eth_getLogs; the configured archive (after proving eth_chainId) answers.
    Same helper `_pool_key` already trusts, same proof, read-only."""
    PRIMARY, ARCHIVE = "http://primary", "http://archive"
    init_log = {"topics": ["0xinit", V4_POOL, "0x" + "0" * 64, "0x" + MINT[2:].rjust(64, "0")]}

    def fake_rpc(url, method, params, http=None):
        if url == PRIMARY:
            raise RuntimeError("EVM RPC provider HTTP failure")
        if method == "eth_blockNumber":
            return {"result": "0x100000"}
        return {"result": [init_log]}

    monkeypatch.setattr(V, "_rpc", fake_rpc)
    monkeypatch.setattr(V, "_archive_rpc_urls", lambda _c, primary: (primary, ARCHIVE))
    checked = []
    monkeypatch.setattr(V, "_archive_is_chain",
                        lambda url, chain, http=None: checked.append(url) or True)
    pools, failed = V._onchain_pools_for_mint("4663", MINT, PRIMARY)
    assert (pools, failed) == ([V4_POOL], False)
    assert checked == [ARCHIVE], "the archive must prove its chain id before it is read"


def test_the_chain_scan_reports_failure_only_when_every_endpoint_failed(monkeypatch):
    monkeypatch.setattr(V, "_rpc", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(V, "_archive_rpc_urls", lambda _c, primary: (primary, "http://archive"))
    monkeypatch.setattr(V, "_archive_is_chain", lambda *_a, **_k: True)
    assert V._onchain_pools_for_mint("4663", MINT, "http://primary") == ([], True)


def test_both_miss_code_maps_carry_the_new_labels():
    """A renamed or new blind label falling off the end of a lookup silently reclassifies the
    miss (the 2026-09-02 split had to add four labels to two maps for the same reason)."""
    import pathlib as _pl
    seams = _pl.Path("sniper/execution/evm_real_seams.py").read_text()
    runner = _pl.Path("sniper/services/memecoin_real_runner.py").read_text()
    for label in ("provider_discovery_rpc_failed", "provider_discovery_unproven"):
        assert label in seams, f"{label} must map to RPC_TRANSPORT on the exit path"
        assert label in runner, f"{label} must be in the entry requote set"


# --------------------------------------------------------------------------------------------
# THE LOAD-BEARING TEST. Everything above decides what we LOOK at; this decides what we SIGN.
# --------------------------------------------------------------------------------------------

def test_a_pool_on_a_FOREIGN_poolmanager_never_quotes(monkeypatch):
    """A well-formed 66-char pool id that is NOT on our configured PoolManager must resolve to
    None and must never reach the quoter.

    This is what makes shape-filtering safe. We no longer read a provider's claim about which
    venue a pool belongs to, so the guarantee has to come from the chain: `_pool_key` reads the
    Initialize event from OUR PoolManager address specifically, and a pool on a fork emits no
    such event there. If this ever passes a foreign pool through, we would quote — and then sign
    — against a contract nobody cross-validated.

    Mutation check: make `_pool_key` query any address other than V4[chain]["pool_manager"], or
    have it fall back to a second manager, and this goes red.
    """
    quoted = []
    monkeypatch.setattr(V, "_quote_single",
                        lambda *a, **k: quoted.append(a) or 12345)

    seen_addresses = []

    def rpc(url, method, params, http=None):
        assert method == "eth_getLogs"
        addr = str(params[0]["address"]).lower()
        seen_addresses.append(addr)
        if addr == FOREIGN_MANAGER.lower():
            return {"result": [{"topics": ["0x0", "0x0", "0x" + "11" * 32, "0x" + "22" * 32],
                                "data": "0x" + "00" * 96}]}
        return {"result": []}              # our manager has never heard of this pool

    monkeypatch.setattr(V, "_rpc", rpc)
    monkeypatch.setattr(V, "_pools_for_mint", lambda *a, **k: [V4_POOL])

    out = V.v4_quote_amount_out("4663", WETH, MINT, 10 ** 16, rpc_url="https://rpc")

    assert out is None, "a pool absent from our PoolManager produced a quote"
    assert V._pool_key("4663", V4_POOL, "https://rpc") is None, (
        "the PoolKey resolved despite our manager having no Initialize event for it")
    assert quoted == [], "the quoter was reached for a pool our PoolManager does not know"
    assert seen_addresses and all(a == OUR_MANAGER.lower() for a in seen_addresses), (
        f"_pool_key must query ONLY our configured PoolManager; it queried {set(seen_addresses)}")


def test_our_own_poolmanager_does_resolve(monkeypatch):
    """The positive control: same code path, Initialize event present on OUR manager -> a key."""
    def rpc(url, method, params, http=None):
        assert str(params[0]["address"]).lower() == OUR_MANAGER.lower()
        return {"result": [{
            "topics": ["0x0", "0x0", "0x" + "00" * 12 + "00" * 20, "0x" + "00" * 12 + MINT[2:]],
            "data": "0x" + "00" * 32 + format(200, "064x") + "00" * 32}]}
    monkeypatch.setattr(V, "_rpc", rpc)
    key = V._pool_key("4663", V4_POOL, "https://rpc")
    assert key is not None and key["tick_spacing"] == 200


# --------------------------------------------------------------------------------------------
# Extortionate-fee traps. Widening discovery made these REACHABLE; before it, nothing on 4663
# was discovered at all so there was no trap to hit.
# --------------------------------------------------------------------------------------------

def test_an_extortionate_fee_pool_is_never_selected(monkeypatch):
    """A 89-93% fee pool is buyable and not sellable. Measured live on 0xb2a02fc6… 2026-08-30:
    three such pools sat beside the real one at round trips of -99.58%, -99.88% and -99.92%
    against its -6.28%.

    `max(amount_out)` is NOT a defence — it protects the buy only while the honest pool happens to
    quote best, and on one live exit run the sell fell through to a trap and returned 0.6% of the
    bag. Mutation check: delete the `_fee_is_sane` call in `v4_quote_amount_out` and this goes red.
    """
    TRAP, REAL = "0x" + "aa" * 32, "0x" + "bb" * 32
    keys = {TRAP: {"currency0": V._NATIVE, "currency1": MINT, "fee": 890000,
                   "tick_spacing": 19988, "hooks": "0x" + "00" * 20},
            REAL: {"currency0": V._NATIVE, "currency1": MINT, "fee": 0,
                   "tick_spacing": 200, "hooks": "0x" + "e5" * 20}}
    monkeypatch.setattr(V, "_pools_for_mint", lambda *a, **k: [TRAP, REAL])
    monkeypatch.setattr(V, "_pool_key", lambda chain, pid, *a, **k: keys[pid])
    # the trap quotes a BIGGER amount_out than the honest pool — exactly the manipulation shape
    monkeypatch.setattr(V, "_quote_single",
                        lambda chain, key, zf1, amt, *a, **k:
                        10 ** 24 if key["fee"] == 890000 else 10 ** 20)

    out = V.v4_quote_amount_out("4663", WETH, MINT, 10 ** 16, rpc_url="https://rpc")
    assert out is not None
    assert out["pool_fee"] == 0, (
        "the router selected an extortionate-fee pool because it quoted a larger amount_out")


def test_dynamic_fee_pools_stay_allowed():
    """The real Pons pools price through a hook. Rejecting the dynamic-fee sentinel would refuse
    exactly the pools this whole change exists to reach."""
    assert V._fee_is_sane(0)                      # static zero + hook (the live Pons shape)
    assert V._fee_is_sane(3000)                   # ordinary 0.30%
    assert V._fee_is_sane(V._V4_DYNAMIC_FEE_FLAG)  # hook-priced
    assert not V._fee_is_sane(890000)             # 89%  — measured trap
    assert not V._fee_is_sane(933267)             # 93.33% — measured trap
    assert not V._fee_is_sane(None)               # unparseable is not "small"


# --- the 13-minute discovery window needed a wider fallback (2026-09-03) -----------------------

def _mint_topic(mint):
    return "0x" + mint[2:].lower().rjust(64, "0")


def _discovery_http(window_logs, full_logs, calls):
    """Answers the narrow window and the full range differently, and records every range asked."""
    import json as _j

    def http(method, url, headers=None, body=None, timeout=None):
        payload = _j.loads(body if isinstance(body, str) else body.decode())
        if payload["method"] == "eth_chainId":
            return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x1237"}, {}
        if payload["method"] == "eth_blockNumber":
            return 200, {"jsonrpc": "2.0", "id": 1, "result": hex(50_000_000)}, {}
        rng = payload["params"][0]["fromBlock"]
        calls.append(rng)
        logs = full_logs if rng == "0x0" else window_logs
        return 200, {"jsonrpc": "2.0", "id": 1, "result": list(logs)}, {}
    return http


POOL_A = "0x" + "a1" * 32
MINT_X = "0x" + "cd" * 20


def _init_log(pool_id):
    return {"topics": [V._INIT_TOPIC, pool_id, _mint_topic("0x" + "11" * 20), _mint_topic(MINT_X)]}


def test_a_pool_older_than_the_window_is_found_by_the_full_range_read():
    """Measured 2026-09-03: four coins refused as unroutable had 7, 4, 7 and 17 pools on our own
    PoolManager. The 8192-block window is thirteen minutes on 4663 and saw none of them."""
    calls = []
    http = _discovery_http(window_logs=[], full_logs=[_init_log(POOL_A)], calls=calls)
    pools, blind = V._onchain_pools_for_mint(4663, MINT_X, "https://rpc.test", http=http)
    assert pools == [POOL_A]
    assert blind is False
    assert "0x0" in calls, "the full-range read must run once the window comes back empty"


def test_the_window_finding_a_pool_never_pays_for_a_full_range_read():
    """A pool minted seconds ago answers in the window; the wide read must not run."""
    calls = []
    http = _discovery_http(window_logs=[_init_log(POOL_A)], full_logs=[], calls=calls)
    pools, blind = V._onchain_pools_for_mint(4663, MINT_X, "https://rpc.test", http=http)
    assert pools == [POOL_A] and blind is False
    assert "0x0" not in calls


def test_an_empty_full_range_is_absence_not_blindness():
    calls = []
    http = _discovery_http(window_logs=[], full_logs=[], calls=calls)
    pools, blind = V._onchain_pools_for_mint(4663, MINT_X, "https://rpc.test", http=http)
    assert pools == [] and blind is False
    assert "0x0" in calls


def test_a_raising_full_range_still_reports_blind():
    import json as _j

    def http(method, url, headers=None, body=None, timeout=None):
        payload = _j.loads(body if isinstance(body, str) else body.decode())
        if payload["method"] == "eth_chainId":
            return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x1237"}, {}
        if payload["method"] == "eth_blockNumber":
            return 200, {"jsonrpc": "2.0", "id": 1, "result": hex(50_000_000)}, {}
        raise RuntimeError("provider down")

    pools, blind = V._onchain_pools_for_mint(4663, MINT_X, "https://rpc.test", http=http)
    assert pools == [] and blind is True


def test_the_durable_key_cache_answers_discovery_without_a_provider(monkeypatch, tmp_path):
    """A pool is paid for once, ever. Measured 2026-09-03: the cache answered 21 of 23 coins in
    about 3 ms, where the wide chain read it replaces is p90 2157 ms and fails 3 times in 20."""
    monkeypatch.setattr(V, "_stored_pools_for_mint", _REAL_STORED_POOLS)
    import json as _j
    import sqlite3
    from sniper.persistence import db_memecoin as dbm

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dbm.ensure_memecoin_tables(conn)
    pm = V.V4["4663"]["pool_manager"]
    key = {"currency0": "0x" + "00" * 20, "currency1": MINT_X,
           "fee": 0, "tick_spacing": 200, "hooks": "0x" + "00" * 20}
    conn.execute(
        "INSERT INTO memecoin_evm_v4_pool_keys "
        "(network_id, pool_id, pool_manager, key_json, verified_at) VALUES (?,?,?,?,?)",
        ("4663", POOL_A, pm, _j.dumps(key), "2026-09-03T00:00:00+00:00"))
    conn.commit()
    class _Keep:
        """The helper closes what it opens; hand it a proxy so the in-memory db survives."""
        def __init__(self, real): self._real = real
        def execute(self, *a, **k): return self._real.execute(*a, **k)
        def close(self): pass

    monkeypatch.setattr("sniper.persistence.db_core.get_readonly_connection",
                        lambda *a, **k: _Keep(conn))

    assert V._stored_pools_for_mint(4663, MINT_X) == [POOL_A]
    assert V._stored_pools_for_mint(4663, "0x" + "ee" * 20) == []


def test_a_cache_failure_is_silent_and_falls_through(monkeypatch):
    """Cache trouble must never refuse an entry; the live sources still run."""
    def boom(*_a, **_k):
        raise RuntimeError("db locked")
    monkeypatch.setattr("sniper.persistence.db_core.get_readonly_connection", boom)
    assert V._stored_pools_for_mint(4663, MINT_X) == []


def test_the_cache_lookup_seeks_both_indexes(monkeypatch):
    """At 124,245 rows an `OR` across the two currency sides measured 168 ms because SQLite fell
    back to the leading equality columns, which every row shares. A UNION seeks both expression
    indexes and measured 0.06 ms. Assert the PLAN, so growth cannot quietly bring the scan back."""
    monkeypatch.setattr(V, "_stored_pools_for_mint", _REAL_STORED_POOLS)
    import sqlite3
    from sniper.persistence import db_memecoin as dbm

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dbm.ensure_memecoin_tables(conn)
    plans = []

    class _Spy:
        def execute(self, sql, params=()):
            plans.extend(r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
            return conn.execute(sql, params)

        def close(self):
            pass

    monkeypatch.setattr("sniper.persistence.db_core.get_readonly_connection",
                        lambda *a, **k: _Spy())
    V._stored_pools_for_mint(4663, MINT_X)
    assert plans, "the lookup must have run a query"
    assert not any("SCAN" in p for p in plans), "a full table scan came back: %s" % plans
    # idx_mc_v4_key_pair (2026-10-17) leads with the currency0 index's three terms, so the planner
    # may seek it for the currency0 branch: still one index seek per side, never a scan.
    assert sum(("idx_mc_v4_key_currency" in p or "idx_mc_v4_key_pair" in p) and "<expr>=?" in p
               for p in plans) == 2, plans


def _cache_only(monkeypatch, pools):
    """No provider answers; only the durable PoolKey cache has pools, so depth is unknown."""
    monkeypatch.setattr(V, "_gt_pools_ranked", lambda *a, **k: ([], False))
    monkeypatch.setattr(V, "_ds_pools_ranked", lambda *a, **k: ([], False))
    monkeypatch.setattr(V, "_stored_pools_for_mint", lambda *a, **k: list(pools))
    V._pool_cache.clear(); V._reserve_cache.clear()


def test_cached_pools_never_publish_a_fabricated_liquidity(monkeypatch):
    """The cache carries no depth. Recording 0.0 as this mint's reserve also OVERWROTE a real
    reading taken minutes earlier, turning a measured liquidity into an unknown one downstream."""
    ids = ["0x%064x" % i for i in range(1, 4)]
    _cache_only(monkeypatch, ids)
    V._reserve_cache[("4663", MINT_X.lower())] = (28511.88, __import__("time").monotonic())
    V._pools_for_mint(4663, MINT_X)
    assert V.top_pool_reserve_usd(4663, MINT_X) == 28511.88, \
        "a measured reserve must survive a later cache-sourced lookup"


def test_without_depth_more_pools_are_offered_to_the_quoter(monkeypatch):
    """With every reserve equal the 'deepest first' sort falls through to comparing POOL IDS, so a
    cap of four became an arbitrary four. DUMBMONEY (2026-09-04) had nine pools, one at $28.5k and
    several under $200; the quote came back 15.8% off market and the sell simulation reverted. The
    quoter keeps the BEST output of whatever it is given, so a wider list cannot make a fill worse."""
    ids = ["0x%064x" % i for i in range(1, 10)]
    _cache_only(monkeypatch, ids)
    assert len(V._pools_for_mint(4663, MINT_X)) == 8


def test_measured_depth_still_ranks_and_caps_at_four(monkeypatch):
    """When a provider DID give reserves, nothing changes: deepest first, four candidates."""
    ranked = [(500.0, "0x" + "a" * 64), (28511.88, "0x" + "b" * 64), (10.0, "0x" + "c" * 64),
              (7.0, "0x" + "d" * 64), (1.0, "0x" + "e" * 64)]
    monkeypatch.setattr(V, "_gt_pools_ranked", lambda *a, **k: (ranked, False))
    monkeypatch.setattr(V, "_ds_pools_ranked", lambda *a, **k: ([], False))
    V._pool_cache.clear(); V._reserve_cache.clear()
    pools = V._pools_for_mint(4663, MINT_X)
    assert len(pools) == 4
    assert pools[0] == "0x" + "b" * 64, "the deepest pool must lead"
    assert V.top_pool_reserve_usd(4663, MINT_X) == 28511.88


# ---- 2026-09-05: a trap-only indexer list is as blind as an empty one (YIELDPAD) ----------------

REAL_POOL = "0xe0f03ae2d8000000000000000000000000000000000000000000000000000001"
TRAP_POOL = "0xc5f67ce34d000000000000000000000000000000000000000000000000000002"


def _key(fee, other=None, hooks="0x" + "00" * 20):
    return {"currency0": other or V._NATIVE, "currency1": MINT.lower(), "fee": fee,
            "tick_spacing": 60, "hooks": hooks}


def test_a_trap_only_indexer_list_still_runs_the_chain_scan(monkeypatch):
    """YIELDPAD 12:53:53: GeckoTerminal/DexScreener listed only 80-99% fee traps; the real hooked
    ETH pool was indexed eleven seconds later. `merged` was non-empty, so the chain scan -- the
    one source that sees a pool younger than the indexer lag -- never ran, and the coin was
    refused `no_route`. A list with no known sane ETH pool must scan."""
    V._key_cache[("4663", TRAP_POOL)] = _key(810000)
    calls = []
    monkeypatch.setattr(V, "_onchain_pools_for_mint",
                        lambda *_a, **_k: (calls.append(1), ([REAL_POOL], False))[1])
    V._blind.discovery_rpc_url = "http://rpc"
    try:
        pools = V._pools_for_mint("4663", MINT, gt_http=_gt([(TRAP_POOL, 10.0, "pons-v2-dex")]),
                                  ds_http=_no_ds)
    finally:
        V._blind.discovery_rpc_url = None
    assert calls, "the chain scan did not run on a trap-only list"
    assert REAL_POOL in pools and TRAP_POOL in pools
    # provisional list: no known sane ETH pool even after the scan (REAL_POOL's key is unknown
    # here), so it must expire in 30s, not 600s
    assert V._pool_cache[("4663", MINT.lower())][0] <= __import__("time").time() + 31


def test_a_list_with_a_known_sane_eth_pool_does_not_pay_for_a_chain_scan(monkeypatch):
    V._key_cache[("4663", V4_POOL)] = _key(3000)
    calls = []
    monkeypatch.setattr(V, "_onchain_pools_for_mint",
                        lambda *_a, **_k: (calls.append(1), ([REAL_POOL], False))[1])
    V._blind.discovery_rpc_url = "http://rpc"
    try:
        pools = V._pools_for_mint("4663", MINT, gt_http=_gt([(V4_POOL, 5000.0, "pons-v2-dex")]),
                                  ds_http=_no_ds)
    finally:
        V._blind.discovery_rpc_url = None
    assert not calls
    assert pools == [V4_POOL]
    assert V._pool_cache[("4663", MINT.lower())][0] > __import__("time").time() + 500


def test_a_pending_launchpad_pool_keeps_the_candidate_cache_short(monkeypatch):
    """A pool the launchpad names but the chain has not initialised yet must not freeze a
    trap-only list for 30s: the requote that would catch the pool runs within seconds."""
    from sniper.execution import evm_v4_launchpad as L
    V._key_cache[("4663", TRAP_POOL)] = _key(810000)
    monkeypatch.setattr(L, "derived_pool_keys", lambda *a, **k: [(REAL_POOL, _key(0), False)])
    monkeypatch.setattr(V, "_onchain_pools_for_mint", lambda *_a, **_k: ([], False))
    V._blind.discovery_rpc_url = "http://rpc"
    try:
        pools = V._pools_for_mint("4663", MINT, gt_http=_gt([(TRAP_POOL, 10.0, "pons-v2-dex")]),
                                  ds_http=_no_ds)
    finally:
        V._blind.discovery_rpc_url = None
    assert REAL_POOL not in pools, "a pending pool must not enter the list on derivation alone"
    assert V._pool_cache[("4663", MINT.lower())][0] <= __import__("time").time() + 3.5

