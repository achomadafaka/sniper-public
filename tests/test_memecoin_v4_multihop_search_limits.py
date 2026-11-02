"""The multihop search limits hold at the request boundary (review of b526531c, 2026-09-17).

The review reproduced three defects: queued discovery reads started after the deadline, one
counted key lookup sent several RPC requests, and a failed block read let quotes run at `latest`.
These tests drive the REAL `_rpc`, `_pool_key`, `_onchain_liquidity` and `_decision_block` through
a fake transport, so they count what would leave the process. Offline: no network.
"""
import threading
import time

import pytest

from sniper.execution import evm_real_seams as S
from sniper.execution import evm_rpc_pool as POOL
from sniper.execution import evm_v4_multihop as MH
from sniper.execution import evm_v4_quote as Q
from sniper.execution import evm_wallet as W

CHAIN = "4663"
NATIVE = "0x" + "00" * 20
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
COIN = "0x" + "22" * 20


def _key(c0, c1, fee):
    return {"currency0": c0, "currency1": c1, "fee": fee, "tick_spacing": 60, "hooks": NATIVE}


class Transport:
    """A fake RPC transport. Records the dispatch time of every request it receives."""

    def __init__(self, delay_s=0.0, result="0x" + "00" * 31 + "05"):
        self.delay_s = delay_s
        self.result = result
        self.sent = []
        self.lock = threading.Lock()

    def __call__(self, method, url, body=None, **_kw):
        with self.lock:
            self.sent.append((time.monotonic(), body))
        if self.delay_s:
            time.sleep(self.delay_s)
        return 200, {"jsonrpc": "2.0", "id": 1, "result": self.result}, None


# 1. the deadline bounds discovery at dispatch -------------------------------------------------

def test_no_discovery_read_is_sent_after_the_deadline(monkeypatch):
    pools = [("0x%064x" % (i + 1), _key(NATIVE, USDG, 100 + i)) for i in range(16)]
    monkeypatch.setattr(MH, "_stored_pair_pools", lambda *a, **k: pools)
    transport = Transport(delay_s=0.05)
    budget = MH._SearchBudget(0.02, 1000)
    started = time.monotonic()

    MH._onchain_pair_options(CHAIN, NATIVE, USDG, "http://rpc", budget=budget,
                             http=MH._BudgetedHttp(budget, transport, discovery=True))
    elapsed = time.monotonic() - started
    deadline = started + 0.02

    assert elapsed < 0.05, "the wait stops at the deadline; it does not join queued reads"
    time.sleep(0.2)                           # let every in-flight read finish
    late = [t for t, _b in transport.sent if t > deadline + 0.005]
    assert late == [], "no read starts after the deadline"
    assert len(transport.sent) <= 8           # only the reads already dispatched by 8 workers
    assert budget.discovery_cut is True


def test_discovery_leaves_the_quote_time(monkeypatch):
    budget = MH._SearchBudget(1.0, 1000, quote_time_s=1.0)

    assert not budget.take(discovery=True)
    assert budget.take()                      # the quote phase still has its whole second


def test_the_search_timer_starts_before_provider_discovery(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_SEARCH_DEADLINE_S", 0.05, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_QUOTE_TIME_S", 0.0, raising=False)

    def slow_provider(*_a, **_kw):
        time.sleep(0.1)                       # provider discovery alone outlives the deadline
        return [("p_coin", 9_000.0)]
    monkeypatch.setattr(MH, "_pools_with_depth", slow_provider)
    reads = []
    monkeypatch.setattr(MH, "_pool_key_for",
                        lambda *a, **k: reads.append(a) or _key(USDG, COIN, 3000))

    assert MH.multihop_quote(CHAIN, NATIVE, COIN, 10 ** 16, rpc_url="http://rpc",
                             bridge_currencies=(USDG,), clip_usd=150.0) is None
    assert reads == [], "no key read starts once the search deadline has passed"


# 2. the request count is the outbound request count ---------------------------------------------

def test_one_request_budget_sends_at_most_one_rpc_request_for_an_uncached_key(monkeypatch):
    monkeypatch.setattr(Q, "_stored_pool_key", lambda *a, **k: None)
    monkeypatch.setattr(Q, "_archive_rpc_urls", lambda *a, **k: [])
    blind = []
    monkeypatch.setattr(Q, "_mark_blind", lambda reason: blind.append(reason))
    Q._key_cache.pop((CHAIN, "0x" + "ab" * 32), None)
    transport = Transport(result="0x100")
    budget = MH._SearchBudget(10.0, 1)

    assert MH._pool_key_for(CHAIN, "0x" + "ab" * 32, "http://rpc",
                            http=MH._BudgetedHttp(budget, transport, discovery=False)) is None
    assert len(transport.sent) == 1 == budget.used
    assert blind == [], "a request our own limit refused is not provider blindness"


def test_automatic_bridge_discovery_is_counted(monkeypatch):
    monkeypatch.setattr(Q, "_stored_pool_key", lambda *a, **k: None)
    monkeypatch.setattr(Q, "_archive_rpc_urls", lambda *a, **k: [])
    monkeypatch.setattr(Q, "_mark_blind", lambda reason: None)
    Q._key_cache.pop((CHAIN, "0x" + "cd" * 32), None)
    transport = Transport(result="0x100")
    budget = MH._SearchBudget(10.0, 1000)

    list(MH._candidate_bridges(CHAIN, NATIVE, COIN, "http://rpc", (),
                               pools=[("0x" + "cd" * 32, 9_000.0)],
                               http=MH._BudgetedHttp(budget, transport, discovery=True)))

    assert len(transport.sent) >= 1
    assert budget.used == len(transport.sent)


def test_a_refused_request_is_never_an_endpoint_failure(monkeypatch):
    recorded, penalized = [], []
    monkeypatch.setattr(POOL, "record", lambda *a, **k: recorded.append(a))
    monkeypatch.setattr(POOL, "penalize", lambda *a, **k: penalized.append(a))
    budget = MH._SearchBudget(10.0, 0)
    transport = Transport()

    try:
        W._rpc("http://rpc", "eth_blockNumber", [],
               http=MH._BudgetedHttp(budget, transport, discovery=False))
        raise AssertionError("the refused request must raise RequestNotSent")
    except POOL.RequestNotSent:
        pass
    assert transport.sent == [] and recorded == [] and penalized == []


def test_search_requests_equal_the_rpc_requests_sent(monkeypatch):
    """The reported count equals actual dispatches; cached provider results cost no request."""
    monkeypatch.setattr(MH, "_pools_with_depth",
                        lambda _c, token, **k: [("p_coin", 9_000.0)] if token == COIN else [])
    monkeypatch.setattr(MH, "_direct_bridge_pools",
                        lambda *a, **k: [("p_bridge", 2_000_000.0)])
    keys = {"p_coin": _key(USDG, COIN, 3000), "p_bridge": _key(NATIVE, USDG, 100)}
    monkeypatch.setattr(MH, "_pool_key_for", lambda _c, pid, _r, **k: keys[pid])
    monkeypatch.setattr(MH, "_stored_pair_pools", lambda *a, **k: [])
    transport = Transport(result="0x" + "00" * 31 + "05")

    route = MH.multihop_quote(CHAIN, NATIVE, COIN, 10 ** 16, rpc_url="http://rpc",
                              bridge_currencies=(USDG,), clip_usd=150.0, min_depth_clips=20.0,
                              http=transport)

    assert route is not None
    assert route["search_requests"] == len(transport.sent)
    assert len(transport.sent) == 3            # block read, forward quote, reverse quote


# 3. no fixed block, no comparison ---------------------------------------------------------------

def _two_leg_search(monkeypatch, block_answer):
    monkeypatch.setattr(MH, "_pools_with_depth",
                        lambda _c, token, **k: [("p_coin", 9_000.0)] if token == COIN else [])
    monkeypatch.setattr(MH, "_direct_bridge_pools", lambda *a, **k: [("p_bridge", 2_000_000.0)])
    keys = {"p_coin": _key(USDG, COIN, 3000), "p_bridge": _key(NATIVE, USDG, 100)}
    monkeypatch.setattr(MH, "_pool_key_for", lambda _c, pid, _r, **k: keys[pid])
    monkeypatch.setattr(MH, "_stored_pair_pools", lambda *a, **k: [])
    quotes = []
    monkeypatch.setattr(MH, "_quote_path", lambda *a, **k: quotes.append(k.get("block")) or 5)

    def transport(method, url, body=None, **_kw):
        if isinstance(block_answer, Exception):
            raise block_answer
        return 200, {"jsonrpc": "2.0", "id": 1, "result": block_answer}, None

    route = MH.multihop_quote(CHAIN, NATIVE, COIN, 10 ** 16, rpc_url="http://rpc",
                              bridge_currencies=(USDG,), clip_usd=150.0, min_depth_clips=20.0,
                              http=transport)
    return route, quotes


def test_a_failed_block_read_refuses_the_comparison(monkeypatch):
    monkeypatch.setattr(POOL, "record", lambda *a, **k: None)
    route, quotes = _two_leg_search(monkeypatch, TimeoutError("block read timed out"))

    assert route is None and quotes == []
    assert MH.last_multihop_refusal() == "decision_block_unavailable"
    # EX1 (2026-09-20): this exact note carried no blind_reason before the fix, so the ladder
    # reported `no_route` -- proved route absence -- for a search that never ran, and the entry
    # executor could hand that verdict to a fallback venue (LI.FI) that bypasses this rung's own
    # checks. Same real failed-block-read path as above, run through the real classifier.
    blind = S.multihop_blind_reason(MH.last_multihop_refusal(), gt_cold=False,
                                    discovery_observed=True)
    assert blind == "search_limit"
    code, _detail = S.classify_quote_miss(
        [{"rung": "v4_multihop", "hit": False, "blind_reason": blind}])
    assert code == "provider_search_limit", "a failed block read must never classify as no_route"


def test_a_malformed_or_missing_block_refuses_the_comparison(monkeypatch):
    for answer in (None, "latest", "0x", "0xzz", "0x0"):
        route, quotes = _two_leg_search(monkeypatch, answer)
        assert route is None and quotes == [], answer
        assert MH.last_multihop_refusal() == "decision_block_unavailable"


def test_a_fixed_block_is_a_hex_number_on_every_quote(monkeypatch):
    route, quotes = _two_leg_search(monkeypatch, "0x3e69b82")

    assert route["quote_block"] == "0x3e69b82"
    assert quotes and set(quotes) == {"0x3e69b82"}


@pytest.mark.parametrize("blocked_at_guard", [False, True])
def test_provider_discovery_stops_before_quote_time(monkeypatch, blocked_at_guard):
    """A slow provider or rate guard must not permit a second late dispatch. Since 2026-10-17 the
    coin's GeckoTerminal and DexScreener reads are dispatched together at the start, so DexScreener
    is sent beside the slow GeckoTerminal call, never after it."""
    from sniper.execution import geckoterminal_quote as gt
    from sniper.execution import solana_quote as sol
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_SEARCH_DEADLINE_S", 0.1)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_QUOTE_TIME_S", 0.06)
    monkeypatch.setattr(gt, "_gt_disabled", lambda: False)
    monkeypatch.setattr(gt, "_funded_cooldown_until", [0.0])
    gate = threading.Semaphore(0 if blocked_at_guard else 1)
    monkeypatch.setattr(gt, "_http_gate", gate)
    monkeypatch.setattr(sol, "dexscreener_cooldown_active", lambda **kw: False)
    monkeypatch.setattr(Q, "_stored_pools_for_mint", lambda *a: [])
    release = threading.Event()
    sent = []

    def gt_http(*a, **kw):
        sent.append("gt")
        release.wait(0.3)
        return 200, {"data": []}, None

    def ds_http(*a, **kw):
        sent.append("ds")
        return 200, {"pairs": []}, None

    monkeypatch.setattr(gt, "_http", gt_http)
    monkeypatch.setattr(sol, "_http", ds_http)
    started = time.monotonic()
    try:
        route = MH.multihop_quote(CHAIN, NATIVE, COIN, 1000,
                                  rpc_url="http://rpc", clip_usd=150)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        if blocked_at_guard:
            gate.release()
        time.sleep(0.03)  # Complete the offline request before fixture cleanup.
    assert route is None
    assert elapsed < 0.12, "discovery must retain the quote time"
    assert sorted(sent) == (["ds"] if blocked_at_guard else ["ds", "gt"])


@pytest.mark.parametrize("primary_delay", [0.04, 0.25])
def test_unsent_hedge_preserves_primary_only_until_search_deadline(monkeypatch, primary_delay):
    """Budget refusal cannot discard an answer or extend the caller's deadline."""
    from sniper.settings import memecoin as cfg
    primary, backup = "https://primary.example", "https://backup.example"
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {CHAIN: (primary, backup)})
    monkeypatch.setattr(POOL, "_state", {})
    for url in (primary, backup):
        POOL._entry(url).update(chain_ok=True, checked=POOL._now())
    monkeypatch.setattr(W, "_HEDGE_AFTER_S", 0.005)
    monkeypatch.setattr(W, "_HEDGE_TOTAL_S", 1.0)
    penalties = []
    monkeypatch.setattr(POOL, "penalize", lambda *a, **kw: penalties.append(a))
    transport = Transport(delay_s=primary_delay, result="0xaa")
    budget = MH._SearchBudget(0.08, 1)
    started = time.monotonic()
    try:
        if primary_delay < 0.08:
            result = W._rpc(primary, "eth_blockNumber", [],
                            http=MH._BudgetedHttp(budget, transport, discovery=False))
            assert result["result"] == "0xaa"
        else:
            with pytest.raises(POOL.RequestNotSent):
                W._rpc(primary, "eth_blockNumber", [],
                       http=MH._BudgetedHttp(budget, transport, discovery=False))
            assert time.monotonic() - started >= 0.05, "retain the paid primary request"
        assert time.monotonic() - started < 0.18
        assert len(transport.sent) == 1 and penalties == []
    finally:
        time.sleep(primary_delay + 0.02)


@pytest.mark.parametrize("provider", ["gt", "ds"])
def test_provider_budget_refusal_does_not_change_cooldown(monkeypatch, provider):
    """A local request refusal must retain its type through each provider guard."""
    from sniper.execution import geckoterminal_quote as gt
    from sniper.execution import solana_quote as sol
    monkeypatch.setattr(gt, "_gt_disabled", lambda: False)
    monkeypatch.setattr(gt, "_funded_cooldown_until", [0.0])
    monkeypatch.setattr(gt, "_http_gate", threading.Semaphore(1))
    monkeypatch.setattr(sol, "dexscreener_cooldown_active", lambda **kw: False)
    cooldowns = []
    monkeypatch.setattr(gt, "_start_cooldown", lambda *a, **kw: cooldowns.append(a))
    monkeypatch.setattr(sol, "_ds_start_cooldown", lambda *a, **kw: cooldowns.append(a))
    transport = Transport()
    budget = MH._SearchBudget(1.0, 0)
    read = Q._gt_pools_ranked if provider == "gt" else Q._ds_pools_ranked
    with pytest.raises(POOL.RequestNotSent):
        read(CHAIN, COIN, request_http=MH._BudgetedHttp(budget, transport, discovery=True))
    assert not transport.sent and not cooldowns and budget.used == 0


def test_search_count_includes_actual_provider_dispatches(monkeypatch):
    """A complete route must count three provider calls and three RPC calls."""
    from sniper.execution import geckoterminal_quote as gt
    from sniper.execution import solana_quote as sol
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS", 2)
    monkeypatch.setattr(gt, "_gt_disabled", lambda: False)
    monkeypatch.setattr(gt, "_funded_cooldown_until", [0.0])
    monkeypatch.setattr(gt, "_http_gate", threading.Semaphore(1))
    monkeypatch.setattr(sol, "dexscreener_cooldown_active", lambda **kw: False)
    coin_pid, bridge_pid = "0x" + "aa" * 32, "0x" + "bb" * 32
    keys = {coin_pid: _key(USDG, COIN, 3000), bridge_pid: _key(NATIVE, USDG, 100)}
    monkeypatch.setattr(MH, "_pool_key_for", lambda c, pid, r, **kw: keys[pid])
    monkeypatch.setattr(MH, "_stored_pair_pools", lambda *a: [])
    monkeypatch.setattr(Q, "_stored_pools_for_mint", lambda *a: [])
    providers = []

    def gt_http(method, url, **kw):
        providers.append("gt")
        return 200, {"data": [{"id": coin_pid, "attributes": {"reserve_in_usd": "9000"}}]}, None

    def ds_http(method, url, **kw):
        providers.append("ds")
        pairs = [{"chainId": sol.FOMO_TO_DEX[CHAIN], "baseToken": {"address": USDG},
                  "quoteToken": {"address": NATIVE}, "pairAddress": bridge_pid,
                  "liquidity": {"usd": 2000000}}] if url.endswith(USDG) else []
        return 200, {"pairs": pairs}, None

    monkeypatch.setattr(gt, "_http", gt_http)
    monkeypatch.setattr(sol, "_http", ds_http)
    transport = Transport()
    route = MH.multihop_quote(CHAIN, NATIVE, COIN, 1000, rpc_url="http://rpc",
                              bridge_currencies=(USDG,), clip_usd=150,
                              min_depth_clips=20, http=transport)
    assert route is not None
    assert sorted(providers) == ["ds", "ds", "gt"]   # the coin's GT and DS run at once (2026-10-17)
    assert len(transport.sent) == 3
    assert route["search_requests"] == 6


def test_a_search_cut_by_the_discovery_limit_names_the_limit_not_a_missing_route(monkeypatch):
    """Worktree benchmark 2026-09-17: slow pool-key reads used the discovery window, and the
    refusal read `coin_leg_missing_or_under_floor` although a route existed."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_SEARCH_DEADLINE_S", 0.2, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_QUOTE_TIME_S", 0.1, raising=False)
    monkeypatch.setattr(MH, "_pools_with_depth",
                        lambda _c, token, **k: [("p_coin", 9_000.0)] if token == COIN else [])
    monkeypatch.setattr(MH, "_direct_bridge_pools", lambda *a, **k: [("p_bridge", 2_000_000.0)])
    monkeypatch.setattr(MH, "_stored_pair_pools", lambda *a, **k: [])

    def slow_key(_c, pid, _r, **k):
        time.sleep(0.15)                      # an uncached key read that outlives discovery
        return {"p_coin": _key(USDG, COIN, 3000), "p_bridge": _key(NATIVE, USDG, 100)}[pid]
    monkeypatch.setattr(MH, "_pool_key_for", slow_key)

    route = MH.multihop_quote(CHAIN, NATIVE, COIN, 10 ** 16, rpc_url="http://rpc",
                              bridge_currencies=(USDG,), clip_usd=150.0, min_depth_clips=20.0,
                              http=Transport())

    assert route is None
    assert MH.last_multihop_refusal() == "search_stopped:discovery_limit"


def test_the_liquidity_burst_is_capped_per_decision(monkeypatch):
    """SHIT 0x685dfd38 2026-09-17 18:38Z: a cold pair scan at eight concurrent reads 429'd the
    funded v2 rung on the next coin. The readings cache, so a capped scan finishes over decisions.
    Since 2026-09-28 the cap counts batched extsload CALLS (one per chunk of pools), not pools."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_LIQUIDITY_READS", 3, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_LIQUIDITY_WORKERS", 2, raising=False)
    monkeypatch.setattr(MH, "_LIQUIDITY_BATCH_SLOTS", 2)
    pools = [("0x%064x" % (i + 1), _key(NATIVE, USDG, 100 + i)) for i in range(20)]
    monkeypatch.setattr(MH, "_stored_pair_pools", lambda *a, **k: pools)
    calls = []
    monkeypatch.setattr(MH, "_onchain_liquidities",
                        lambda _c, pids, _u, **k: calls.append(list(pids)) or {p: 10 ** 18 for p in pids})

    first = MH._onchain_pair_options(CHAIN, NATIVE, USDG, "http://rpc")
    second = MH._onchain_pair_options(CHAIN, NATIVE, USDG, "http://rpc")

    assert len(calls) == 6 and all(len(c) == 2 for c in calls), (
        "three calls of two pools per decision, and the second decision continues")
    assert len(first) == 6 and len(second) == 12


def test_a_second_pair_starved_by_the_shared_cap_is_marked_incomplete(monkeypatch):
    """Codex EX2 review, 2026-09-20: two bridges in the SAME decision share one liquidity
    allowance (audit E5). The first bridge's pair scan can spend the whole cap; the second
    bridge's pair then has its candidate list truncated to zero before a single read is sent --
    not refused by a request, just never asked. Nothing marked that as an incomplete search: the
    second pair came back exactly as empty as one with no pool at all, and its one real pool
    (never read) could have won the route. Two REAL `_onchain_pair_options` calls sharing one
    `_SearchBudget`, the way two bridges inside one `multihop_quote` call do.
    """
    from sniper.settings import memecoin as cfg
    pools = {
        (NATIVE.lower(), USDG.lower()): [
            ("bridge_a1", _key(NATIVE, USDG, 3000)), ("bridge_a2", _key(NATIVE, USDG, 500))],
        (USDG.lower(), COIN.lower()): [("bridge_b1", _key(USDG, COIN, 3000))],
    }
    monkeypatch.setattr(MH, "_stored_pair_pools",
                        lambda _c, a, b: pools.get((str(a).lower(), str(b).lower()), []))
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_LIQUIDITY_READS", 1, raising=False)
    reads = []
    monkeypatch.setattr(MH, "_onchain_liquidities",
                        lambda _c, pids, _u, **k: reads.append(list(pids)) or {p: 9_000.0 for p in pids})

    budget = MH._SearchBudget(deadline_s=30.0, max_requests=1000)
    first_bridge = MH._onchain_pair_options(CHAIN, NATIVE, USDG, "http://rpc", budget=budget)
    second_bridge = MH._onchain_pair_options(CHAIN, USDG, COIN, "http://rpc", budget=budget)

    assert len(first_bridge) == 2, "the first bridge's own two pools are both read, in one call"
    assert second_bridge == [], "the second bridge's real pool is never read -- the cap is spent"
    assert reads == [["bridge_a1", "bridge_a2"]], "only the first bridge's call was ever sent"
    assert budget.discovery_cut is True, (
        "a truncated candidate list must mark the search incomplete, the same as a refused "
        "request already does")
