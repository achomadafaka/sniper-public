"""4663 fast lane M2 step 2b: the pre-signal safety cache (Alpha 2026-10-01, option 1). Today's
token_safety_fn runs on a runner background thread, rebound to the 4663 research QuickNode with a
V4-DIRECT quote: the pool key comes from the detector, so no pool discovery (GeckoTerminal,
DexScreener) and no funded host is ever asked."""
import pytest

import sniper.execution.evm_v4_quote as v4

NATIVE = "0x" + "00" * 20
TOKEN = "0xcabb5484150745a3349b956da5d6e97bd7c8dfd9"
HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
KEY = dict(currency0=NATIVE, currency1=TOKEN, fee=0, tick_spacing=200, hooks=HOOK)
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
POOL = "0x" + "a1" * 32


def test_a_direct_quote_asks_only_the_quoter_for_the_given_key(monkeypatch):
    asked = []
    monkeypatch.setattr(v4, "_quote_pools_for_mint",
                        lambda *a, **k: pytest.fail("pool discovery must not run"))
    monkeypatch.setattr(v4, "_quote_single", lambda chain, key, zf1, amount, url, http=None:
                        asked.append((key, zf1, amount, url)) or 777)
    sell = v4.v4_direct_quote("4663", KEY, POOL, TOKEN, WETH, 10 ** 18, rpc_url="https://research")
    assert sell == {"amount_out": 777, "pool_id": POOL, "pool_key": KEY, "zero_for_one": False,
                    "pool_fee": 0, "quote_currency": NATIVE}
    buy = v4.v4_direct_quote("4663", KEY, POOL, WETH, TOKEN, 10 ** 15, rpc_url="https://research")
    assert buy["zero_for_one"] is True
    assert asked == [(KEY, False, 10 ** 18, "https://research"), (KEY, True, 10 ** 15, "https://research")]


@pytest.mark.parametrize("key", [dict(KEY, currency0=WETH), dict(KEY, currency1="0x" + "99" * 20),
                                 dict(KEY, fee=900_000)])
def test_a_direct_quote_refuses_a_foreign_wrong_or_trap_pool(monkeypatch, key):
    monkeypatch.setattr(v4, "_quote_single", lambda *a, **k: 777)
    assert v4.v4_direct_quote("4663", key, POOL, TOKEN, WETH, 10 ** 18, rpc_url="u") is None


def test_dex_v4_quote_with_an_override_skips_discovery_and_builds_on_its_key(monkeypatch):
    monkeypatch.setattr(v4, "v4_quote_amount_out",
                        lambda *a, **k: pytest.fail("discovery quote must not run"))
    override = {"amount_out": 5000, "pool_id": POOL, "pool_key": KEY, "zero_for_one": True,
                "pool_fee": 0, "quote_currency": NATIVE}
    out = v4._dex_v4_quote("4663", WETH, TOKEN, 10 ** 15, "0x" + "11" * 20, rpc_url="u",
                           quote_override=override, slippage_bps=100)
    assert out["buyAmount"] == "5000" and out["route"]["pool_id"] == POOL
    assert out["minBuyAmount"] == str(5000 * 9900 // 10000)


# ---- the research-host budget, the cache, the candidates datagram ----

from sniper.services import memecoin_rh_fast_lane as lane  # noqa: E402


def test_the_research_seam_spaces_calls_and_pauses_on_a_429():
    now = [100.0]
    slept, sent = [], []

    def base(method, url, headers=None, body=None, timeout=10):
        sent.append(now[0])
        return (429 if len(sent) == 2 else 200), {"result": "0x1"}, {}
    http = lane.PacedHttp(base, min_gap_s=2.0, pause_s=120.0, clock=lambda: now[0],
                          sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    assert http("POST", "u", body="{}")[0] == 200
    assert http("POST", "u", body="{}")[0] == 429                      # waited 2 s, then a 429
    assert slept == [2.0] and http.counts == {"calls": 2, "http_429": 1, "transport_errors": 0, "dead_marked": 0, "quota_403": 0}
    with pytest.raises(lane.ResearchPaused):
        http("POST", "u", body="{}")                                   # paused: nothing is sent
    assert len(sent) == 2
    now[0] += 121.0
    assert http("POST", "u", body="{}")[0] == 200


def _cache(results, ttl=60.0):
    calls = []

    def safety(mint, usd=None):
        calls.append((mint, usd))
        return results.get(mint, (True, None))
    return lane.SafetyCache(safety, ttl_s=ttl, usd=50.0), calls


def test_signal_and_armed_mints_are_checked_before_activity_ranked_ones():
    cache, calls = _cache({})
    cache.note_candidates([{"mint": "0xb", "priority": 5}, {"mint": "0xa", "priority": 0},
                           {"mint": "0xc", "priority": 2}], now=1000.0)
    order = []
    while (m := cache.next_due(1000.0)) is not None:
        order.append(m)
        cache.run_one(m, now=1000.0)
    assert order == ["0xa", "0xc", "0xb"] and calls[0] == ("0xa", 50.0)


def test_a_verdict_is_fresh_for_its_ttl_then_absent_and_due_again():
    cache, _calls = _cache({"0xbad": (False, "sell_sim_reverted")})
    cache.note_candidates([{"mint": "0xgood", "priority": 0}, {"mint": "0xbad", "priority": 0}],
                          now=1000.0)
    for m in ("0xgood", "0xbad"):
        cache.run_one(m, now=1000.0)
    assert cache.reason("0xgood", now=1059.0) is None
    assert cache.reason("0xbad", now=1001.0) == "unsafe:sell_sim_reverted"
    assert cache.reason("0xgood", now=1061.0) == "safety_stale"
    assert cache.reason("0xnever", now=1000.0) == "no_safety_verdict"
    assert cache.next_due(1061.0) in ("0xgood", "0xbad")


def test_a_safety_call_that_raises_or_pauses_is_no_verdict():
    def boom(mint, usd=None):
        raise lane.ResearchPaused("429")
    cache = lane.SafetyCache(boom, ttl_s=60.0, usd=50.0)
    cache.note_candidates([{"mint": "0xa", "priority": 0}], now=1000.0)
    cache.run_one("0xa", now=1000.0)
    assert cache.reason("0xa", now=1000.0) == "no_safety_verdict"


def test_a_candidates_datagram_is_heard_from_the_detector_only(monkeypatch):
    import os
    good = {"kind": "candidates", "sent_epoch": 1.0, "entries": [{"mint": "0xa", "priority": 0}]}
    assert lane.check_sender(good, 7, os.getuid(), detector_pid=7) is None
    assert lane.check_sender(good, 8, os.getuid(), detector_pid=7) == "not_the_detector"
    assert lane.check_sender({"kind": "candidates", "entries": "x"}, 7, os.getuid(),
                             detector_pid=7) == "malformed"


# ---- the production 4663 context carries the lane's two keyed seams ----

def _ctx(monkeypatch, tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from test_memecoin_evm_context_keys import _production_context
    return _production_context(monkeypatch, tmp_path, "4663")


def test_the_context_quotes_a_known_key_on_the_given_host_with_its_own_permit(monkeypatch, tmp_path):
    ctx = _ctx(monkeypatch, tmp_path)
    hosts = []
    monkeypatch.setattr(v4, "_quote_pools_for_mint", lambda *a, **k: pytest.fail("no discovery"))
    monkeypatch.setattr(v4, "_quote_single",
                        lambda chain, key, zf1, amount, url, http=None: hosts.append(url) or 4242)
    monkeypatch.setattr(v4, "_permit2_nonce", lambda *a, **k: hosts.append("nonce") or 0)
    q = ctx["v4_direct_quote_fn"](TOKEN, WETH, 10 ** 18, key=KEY, pool_id=POOL,
                                  rpc_url="https://research", http=None, slippage_bps=100)
    assert q["transaction"]["to"] == v4.V4["4663"]["universal_router"] and q["buyAmount"] == "4242"
    assert hosts == ["https://research", "nonce"]


def test_the_context_signs_with_its_own_address(monkeypatch, tmp_path):
    from eth_account import Account
    ctx = _ctx(monkeypatch, tmp_path)
    raw, tx_hash = ctx["sign_tx_fn"]({"to": ctx["address"], "data": "0x", "value": 0, "gas": 21000,
                                      "gasPrice": 10, "nonce": 1, "chainId": 4663})
    assert Account.recover_transaction(raw).lower() == ctx["address"].lower()
    assert tx_hash.startswith("0x") and len(tx_hash) == 66


# ---- the lane's datagram dispatch: candidates feed the cache, a signal needs a fresh verdict ----

def _dispatch(tmp_path, verdicts=None):
    import json
    import os
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    import test_memecoin_rh_fast_lane as t
    beat = tmp_path / "beat"
    beat.write_text(json.dumps({"ts": 10 ** 10, "pid": os.getpid()}))
    cache = lane.SafetyCache(lambda mint, usd=None: (verdicts or {}).get(mint, (True, None)),
                             ttl_s=60.0, usd=50.0, stats_fn=lambda mint: {"price": 2.05e-05})
    d = lane.LaneDispatch(shadow=t._shadow(tmp_path), cache=cache, registry={}, seen=lane.Seen(),
                          drops=lane.Drops(), db_path=str(tmp_path / "lane.db"), beat=str(beat),
                          host_clock=lambda: 1005.0)
    return d, cache, t


def test_candidates_register_keys_and_queue_verdicts(tmp_path):
    import os
    d, cache, t = _dispatch(tmp_path)
    got = d.on_datagram({"kind": "candidates", "sent_epoch": 1.0, "entries": [
        {"mint": TOKEN, "pool": POOL, "key": KEY, "priority": 3}]}, os.getpid(), os.getuid(), 1000.0)
    assert got == "candidates" and d.registry[TOKEN] == {      # route-shaped (routes rev 3, C4)
        "kind": "v4_native", "key": KEY, "pool": POOL, "bridge": None, "a_key": None, "a_pool": None}
    assert cache.next_due(1000.0) == TOKEN


def test_a_signal_without_a_fresh_verdict_falls_back_and_queues_its_mint_first(tmp_path):
    import os
    d, cache, t = _dispatch(tmp_path)
    rec = d.on_datagram(t._payload(), os.getpid(), os.getuid(), 1000.0)
    assert rec["reason"] == "no_safety_verdict" and rec["lock_busy"] == 0
    assert cache.next_due(1000.0) == t.TOKEN
    cache.run_one(t.TOKEN, now=1004.0)
    rec = d.on_datagram(t._payload(tx_hash="0xnext", permit_nonce=10), os.getpid(), os.getuid(), 1000.0)
    assert rec["reason"] == "built"


def test_an_unsafe_verdict_is_named(tmp_path):
    import os
    d, cache, t = _dispatch(tmp_path, verdicts={TOKEN: (False, "sell_sim_reverted")})
    cache.note_candidates([{"mint": TOKEN, "priority": 0}], now=1000.0)
    cache.run_one(TOKEN, now=1004.0)
    rec = d.on_datagram(t._payload(), os.getpid(), os.getuid(), 1000.0)
    assert rec["reason"] == "unsafe:sell_sim_reverted"


def test_the_lane_heartbeat_counts_candidates_signals_verdicts_and_research_load(tmp_path):
    import os
    import sqlite3
    d, cache, t = _dispatch(tmp_path, verdicts={"0xbad": (False, "x")})
    d.on_datagram({"kind": "candidates", "sent_epoch": 1.0, "entries": [
        {"mint": "0xbad", "priority": 0}, {"mint": TOKEN, "priority": 1}]},
        os.getpid(), os.getuid(), 1000.0)
    d.on_datagram(t._payload(), os.getpid(), os.getuid(), 1000.0)
    for m in ("0xbad", TOKEN):
        cache.run_one(m, now=1004.0)
    research = lane.PacedHttp(lambda *a, **k: (200, {}, {}), min_gap_s=0.0)
    research("POST", "u", body="{}")
    lane.write_heartbeat(d.db_path, d, cache, research)
    row = sqlite3.connect(d.db_path).execute(
        "SELECT candidates_msgs, signals, cache_size, verdicts_ok, verdicts_unsafe, verdict_errors, "
        "research_calls, research_429 FROM rh_fast_lane_heartbeat").fetchone()
    assert row == (1, 1, 2, 1, 1, 0, 1, 0)
    import json
    (tiers,) = sqlite3.connect(d.db_path).execute("SELECT warm_tiers FROM rh_fast_lane_heartbeat").fetchone()
    assert json.loads(tiers)["lookups"] == dict(cache.lookups) and sum(cache.lookups.values()) == 1


def test_a_mint_whose_check_failed_waits_before_it_is_due_again():
    def boom(mint, usd=None):
        raise RuntimeError("transport")
    cache = lane.SafetyCache(boom, ttl_s=60.0, usd=50.0)
    cache.note_candidates([{"mint": "0xa", "priority": 0}, {"mint": "0xb", "priority": 1}], now=1000.0)
    cache.run_one("0xa", now=1000.0)
    assert cache.next_due(1001.0) == "0xb"                 # the failed mint does not spin
    assert cache.next_due(1011.0) == "0xa"                 # and comes back after the backoff


def test_a_candidate_keeps_its_most_urgent_recent_priority():
    # Gamma LOW: the min-merge was untested (a max mutant survived).
    cache, _calls = _cache({})
    cache.note_candidates([{"mint": "0xa", "priority": 0}], now=1000.0)
    cache.note_candidates([{"mint": "0xa", "priority": 7}, {"mint": "0xb", "priority": 3}], now=1010.0)
    assert cache.next_due(1010.0) == "0xa"


def test_a_coins_facts_are_read_once_after_its_first_verdict():
    reads = []
    cache = lane.SafetyCache(lambda mint, usd=None: (True, None), ttl_s=60.0, usd=50.0,
                             facts_fn=lambda mint: reads.append(mint) or (18, 10 ** 27))
    cache.note_candidates([{"mint": "0xa", "priority": 0}], now=1000.0)
    assert cache.facts("0xa") is None
    cache.run_one("0xa", now=1000.0)
    cache.run_one("0xa", now=1070.0)
    assert cache.facts("0xa", now=1070.0) == (18, 10 ** 27) and reads == ["0xa"]   # the test's clock


def test_a_failed_facts_read_is_absent_and_retried():
    calls = []

    def facts(mint):
        calls.append(mint)
        raise RuntimeError("429")
    cache = lane.SafetyCache(lambda mint, usd=None: (True, None), ttl_s=60.0, usd=50.0, facts_fn=facts)
    cache.note_candidates([{"mint": "0xa", "priority": 0}], now=1000.0)
    cache.run_one("0xa", now=1000.0)
    assert cache.facts("0xa") is None
    cache.run_one("0xa", now=1070.0)
    assert len(calls) == 2


# ---- Alpha 2026-10-01: the gates' market data, prefetched with today's function, inside its budget ----

def _ds_pair(mint, *, volume_h1=20000.0, price="0.0000205"):
    """One DexScreener pair for a 4663 coin, as /latest/dex/tokens returns it."""
    import time
    return {"chainId": "robinhood", "dexId": "uniswap", "pairAddress": POOL,
            "baseToken": {"address": mint, "symbol": "TKN"},
            "quoteToken": {"address": NATIVE, "symbol": "ETH"},
            "priceUsd": price, "marketCap": 20500, "fdv": 20500, "liquidity": {"usd": 9000},
            "volume": {"m5": 4000, "h1": volume_h1, "h24": 40000},
            "txns": {"m5": {"buys": 30, "sells": 10}, "h1": {"buys": 90, "sells": 40}},
            "pairCreatedAt": int((time.time() - 3 * 3600) * 1000)}


@pytest.fixture
def ds_cool(monkeypatch):
    """A clean DexScreener breaker per test (a 429 trips the module's shared cooldown)."""
    from sniper.execution import solana_quote as sq
    monkeypatch.setattr(sq, "_ds_cooldown_until", [0.0])
    monkeypatch.setattr(sq, "_ds_funded_cooldown_until", [0.0])
    return sq


def test_market_stats_are_independent_of_the_safety_verdict(monkeypatch):
    monkeypatch.setattr(lane.time, "monotonic", lambda: 1.0)
    asked = []
    verdicts = {"0xa": (True, None), "0xb": (False, "sell_sim_reverted")}
    cache = lane.SafetyCache(lambda mint, usd=None: verdicts[mint], ttl_s=60.0, usd=50.0,
                             stats_fn=lambda mint: asked.append(mint) or {"price": 1.0})
    cache.run_one("0xa", now=1000.0)
    cache.run_one("0xb", now=1000.0)
    assert asked == ["0xa", "0xb"]
    assert cache.stats("0xa", now=1059.0) == ({"price": 1.0}, 1000.0)
    assert cache.stats("0xa", now=1061.0) is None
    assert cache.stats("0xb", now=1000.0) == ({"price": 1.0}, 1000.0)


def test_an_empty_or_raising_stats_read_is_absent_never_an_empty_dict():
    cache = lane.SafetyCache(lambda mint, usd=None: (True, None), ttl_s=60.0, usd=50.0,
                             stats_fn=lambda mint: {})
    cache.run_one("0xa", now=1000.0)
    assert cache.stats("0xa", now=1000.0) is None

    def boom(mint):
        raise TimeoutError("dexscreener")
    cache.stats_fn = boom
    cache.run_one("0xa", now=1100.0)
    assert cache.stats("0xa", now=1100.0) is None and cache.counts["stats_missing"] == 2


def test_the_budget_asks_today_s_function_and_counts_every_request(ds_cool):
    urls = []

    def base(method, url, headers=None, body=None):
        urls.append(url)
        return 200, {"pairs": [_ds_pair(TOKEN)]}, None
    dex = lane.DexBudget(base)
    got = dex.stats(TOKEN)
    assert got["pair_address"] == POOL and got["volume_h1"] > 0
    assert dex.counts["calls"] == 1 and urls and TOKEN in urls[0]


def test_a_429_stops_every_later_lane_request_for_good(ds_cool):
    import io
    import urllib.error
    calls = []

    def base(method, url, headers=None, body=None):
        calls.append(url)
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(b""))
    dex = lane.DexBudget(base)
    assert dex.stats(TOKEN) is None
    assert dex.stopped == "dexscreener_429" and dex.counts["http_429"] == 1
    ds_cool._ds_cooldown_until[0] = 0.0              # today's cooldown has ended...
    assert dex.stats(TOKEN) is None and len(calls) == 1   # ...and the lane still asks nobody


# ---- Gamma 2026-10-02: a signalled coin refreshes before it goes stale ----

def test_a_signalled_coin_is_due_its_refresh_lead_before_the_ttl_and_others_only_at_it():
    cache = lane.SafetyCache(lambda mint, usd=None: (True, None), ttl_s=60.0, usd=50.0,
                             refresh_lead_s=20.0)
    cache.note_candidates([{"mint": "0xhot", "priority": 0}, {"mint": "0xcold", "priority": 3}],
                          now=1000.0)
    for m in ("0xhot", "0xcold"):
        cache.run_one(m, now=1000.0)
    assert cache.next_due(1039.0) is None
    assert cache.next_due(1041.0) == "0xhot"                 # 60 - 20: refreshed while still fresh
    cache.run_one("0xhot", now=1041.0)
    assert cache.next_due(1059.0) is None and cache.next_due(1061.0) == "0xcold"
    assert cache.reason("0xhot", now=1061.0) is None         # the repeat buy finds it fresh


def test_without_a_lead_a_signalled_coin_waits_for_the_ttl_as_before():
    cache = lane.SafetyCache(lambda mint, usd=None: (True, None), ttl_s=60.0, usd=50.0)
    cache.note_candidates([{"mint": "0xhot", "priority": 0}], now=1000.0)
    cache.run_one("0xhot", now=1000.0)
    assert cache.next_due(1059.0) is None and cache.next_due(1061.0) == "0xhot"


@pytest.mark.parametrize("own, research, inline", [
    ("", "https://r.quiknode.pro/k1", ""),
    ("https://r.quiknode.pro/k1/", "https://r.quiknode.pro/k1", ""),      # the same key
    ("https://s.quiknode.pro/k2", "https://r.quiknode.pro/k1", "https://s.quiknode.pro/k2"),
    ("https://own.example.test/k", "", "https://own.example.test/k")])
def test_inline_on_miss_needs_an_inline_host_of_its_own(monkeypatch, own, research, inline):
    from sniper.settings import memecoin as cfg
    monkeypatch.setenv("MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL", own)
    monkeypatch.setenv("MEMECOIN_RH_FAST_SAFETY_RPC_URL", "https://cache.example.test/k")   # never counts
    monkeypatch.setenv("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663", research)
    assert cfg.memecoin_rh_fast_inline_safety_rpc_url() == inline


# ---- Gamma 2026-10-02 (0xdb481ed5, stale 55+ s after going stale): the cache held 12-14 coins,
# all signal or armed-bought ones at priority 0, against ~3.75 verdicts a minute on the research
# host; ties broke by MINT ADDRESS, so a high-address coin starved ----

def test_within_a_priority_the_least_recently_checked_coin_goes_first_not_the_lowest_address():
    cache = lane.SafetyCache(lambda mint, usd=None: (True, None), ttl_s=60.0, usd=50.0)
    cache.note_candidates([{"mint": "0x11", "priority": 0}, {"mint": "0xdb", "priority": 0}],
                          now=1000.0)
    cache.run_one("0xdb", now=1000.0)                         # checked first, so stale first
    cache.run_one("0x11", now=1030.0)
    assert cache.next_due(1100.0) == "0xdb"                   # both stale: the older verdict first


def test_the_runners_own_signal_outranks_an_armed_wallets_coin(tmp_path):
    import os
    d, cache, t = _dispatch(tmp_path)
    cache.note_candidates([{"mint": "0x" + "00" * 20, "priority": 0}], now=1000.0)   # armed-bought
    d.on_datagram(t._payload(), os.getpid(), os.getuid(), 1000.0)                     # our signal
    assert cache.next_due(1000.0) == t.TOKEN
    cache.note_candidates([{"mint": t.TOKEN, "priority": 0}], now=1010.0)  # the detector's re-list
    assert cache.next_due(1010.0) == t.TOKEN                             # keeps the signal's rank


@pytest.mark.parametrize("blocked_at", ["request", "pacing"])
def test_inline_safety_does_not_wait_for_background_http(blocked_at):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    entered, release = threading.Event(), threading.Event()

    def block():
        entered.set()
        assert release.wait(2)

    def background(*args, **kwargs):
        if blocked_at == "request":
            block()
        return 200, {}, {}

    host = lane.PacedHttp(background, min_gap_s=1,
                          sleep=lambda seconds: block())
    if blocked_at == "pacing":
        host._last = host.clock()
    inline = lane.lane_inline_http("u", host, base=lambda *a, **k: (200, {}, {}))
    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = workers.submit(host, "POST", "u")
        try:
            assert entered.wait(1)
            urgent = workers.submit(inline, "POST", "u")
            assert urgent.result(timeout=0.2)[0] == 200
        finally:
            release.set()
        assert pending.result()[0] == 200
    assert host.counts["calls"] == 2


def test_inline_429_during_background_pacing_prevents_background_send():
    sent = []
    host = lane.PacedHttp(lambda *a, **k: sent.append(1) or (200, {}, {}),
                          min_gap_s=2, sleep=lambda seconds: host.pause())
    host._last = host.clock()
    with pytest.raises(lane.ResearchPaused):
        host("POST", "u")
    assert sent == [] and host.counts["calls"] == 0


# ---- Codex EVM-07 (2026-10-06): a fresh safety verdict never carries an indefinitely old supply ----

MINT7 = "0x" + "7a" * 20


def _supply_cache(supply_ttl_s=100.0, ttl_s=30.0):
    state = {"supply": 10 ** 24, "reads": 0, "fail": False}

    def facts(mint):
        state["reads"] += 1
        if state["fail"]:
            raise TimeoutError("facts read timed out")
        return 18, state["supply"]
    cache = lane.SafetyCache(lambda mint, usd=None: (True, None), ttl_s=ttl_s, usd=50.0,
                             facts_fn=facts, supply_ttl_s=supply_ttl_s)
    return cache, state


def test_a_supply_older_than_its_ttl_is_refreshed_by_the_next_passing_verdict():
    # Codex's repro: supply 1e24 then 1e25 (a mint); a passing verdict 120 s later must read it again.
    cache, state = _supply_cache()
    cache.run_one(MINT7, now=1.0)
    state["supply"] *= 10
    cache.run_one(MINT7, now=121.0)
    assert state["reads"] == 2 and cache.facts(MINT7, now=121.0) == (18, 10 ** 25)


def test_the_supply_is_refreshed_ahead_of_its_expiry_and_never_twice_while_fresh():
    cache, state = _supply_cache(supply_ttl_s=100.0)
    cache.run_one(MINT7, now=0.0)
    cache.run_one(MINT7, now=40.0)                                   # young: no read
    assert state["reads"] == 1
    cache.run_one(MINT7, now=60.0)                                   # past half its TTL: refreshed
    assert state["reads"] == 2 and cache.facts(MINT7, now=150.0) == (18, 10 ** 24)


def test_an_expired_supply_is_unknown_even_beside_a_fresh_verdict():
    cache, state = _supply_cache(supply_ttl_s=100.0)
    cache.run_one(MINT7, now=0.0)
    state["fail"] = True
    cache.run_one(MINT7, now=90.0)                                   # refresh fails: old value kept
    assert cache.facts(MINT7, now=95.0) == (18, 10 ** 24)
    assert cache.facts(MINT7, now=101.0) is None                     # expired: unknown, never admits
    assert cache.reason(MINT7, now=101.0) is None                    # the verdict itself is fresh


def test_inline_and_published_facts_carry_their_time_too():
    cache, _ = _supply_cache(supply_ttl_s=100.0)
    cache.put(MINT7, now=10.0, facts=(18, 5 * 10 ** 24))
    assert cache.facts(MINT7, now=100.0) == (18, 5 * 10 ** 24)
    assert cache.facts(MINT7, now=111.0) is None
    cache.put_facts(MINT7, (18, 6 * 10 ** 24), now=200.0)
    assert cache.facts(MINT7, now=250.0) == (18, 6 * 10 ** 24)


def test_the_production_lanes_carry_the_supply_ttl_knob(monkeypatch):
    import inspect
    from sniper.services import memecoin_bsc_fast_lane as bsc
    for fn in (lane.build_lane, bsc.build_live):
        assert "supply_ttl_s=cfg.MEMECOIN_FAST_LANE_SUPPLY_TTL_S" in inspect.getsource(fn), fn


# ---- Codex night review 2026-10-07 (EVM-07 partial): an inline reuse is not a new observation ----

def test_sustained_inline_traffic_rereads_a_supply_past_its_ttl():
    """Codex's repro: supply 1e24 read at t=0, 1e25 upstream after. Inline passes at 200..1000 s with
    TTL 300 used to renew the timestamp on every reuse and serve 1e24 forever with zero reads."""
    import types
    clock = [0.0]
    reads = []

    def facts(mint):
        reads.append(clock[0])
        return 18, (10 ** 24 if clock[0] == 0.0 else 10 ** 25)
    cache = lane.SafetyCache(lambda mint, usd=None: (True, None), ttl_s=30.0, usd=50.0,
                             supply_ttl_s=300.0, clock=lambda: clock[0])
    fake = types.SimpleNamespace(inline_host=None, inline_deadline_s=2.0, cache=cache,
                                 inline_safety_fn=lambda mint, usd=None: (True, None),
                                 inline_facts_fn=facts, stats_fn=None, host_clock=lambda: clock[0])
    for t in (0.0, 200.0, 400.0, 600.0, 800.0, 1000.0):
        clock[0] = t
        assert lane.LiveLane._inline(fake, MINT7, {}) is None
    assert reads == [0.0, 400.0, 800.0]
    assert cache.facts(MINT7, now=1000.0) == (18, 10 ** 25)


def test_an_inline_pass_with_fresh_facts_stamps_them_now():
    import types
    clock = [50.0]
    cache = lane.SafetyCache(lambda mint, usd=None: (True, None), ttl_s=30.0, usd=50.0,
                             supply_ttl_s=300.0, clock=lambda: clock[0])
    fake = types.SimpleNamespace(inline_host=None, inline_deadline_s=2.0, cache=cache,
                                 inline_safety_fn=lambda mint, usd=None: (True, None),
                                 inline_facts_fn=lambda mint: (18, 10 ** 24), stats_fn=None,
                                 host_clock=lambda: clock[0])
    assert lane.LiveLane._inline(fake, MINT7, {}) is None
    assert cache.facts(MINT7, now=349.0) == (18, 10 ** 24) and cache.facts(MINT7, now=351.0) is None


def test_facts_and_stats_publish_before_slow_or_failed_safety():
    import threading
    entered, release = threading.Event(), threading.Event()
    def safety(mint, usd=None):
        entered.set()
        assert release.wait(2)
        raise TimeoutError('safety')
    cache = lane.SafetyCache(safety, ttl_s=60, usd=50,
        facts_fn=lambda m: (0, 123), stats_fn=lambda m: {'price': 1})
    thread = threading.Thread(target=lambda: cache.run_one('0xa', now=1000))
    thread.start()
    try:
        assert entered.wait(2)
        assert cache.facts('0xa', now=1000) == (0, 123)
        assert cache.stats('0xa', now=1000)[0] == {'price': 1}
        assert cache.reason('0xa', now=1000) == 'no_safety_verdict'
    finally:
        release.set()
        thread.join(3)
    assert cache.reason('0xa', now=1000) == 'no_safety_verdict'


def test_only_valid_facts_are_shared_without_a_safety_verdict():
    shared = []
    cache = lane.SafetyCache(lambda m, usd=None: (False, 'unsafe'), ttl_s=60, usd=50,
        facts_fn=lambda m: (0, 123), facts_sink=lambda m, f: shared.append((m, f)))
    cache.run_one('0xa', now=1000)
    assert shared == [('0xa', (0, 123))]
    assert cache.reason('0xa', now=1000) == 'unsafe:unsafe'
    cache.put_facts('0xb', (True, 123), now=1000)
    cache.put_facts('0xb', (18, 0), now=1000)
    assert len(shared) == 1 and cache.facts('0xb', now=1000) is None


@pytest.mark.parametrize("tier, label", [(0, "0"), (1, "1"), (12, "activity"), (None, "other")])
def test_a_signal_lookup_keeps_the_coins_warm_tier_not_the_signals_own_note(tmp_path, tier, label):
    # Gamma MEDIUM on 9a234644b: the dispatch notes the signal's mint at -1 (its queue rank) before
    # reason(), and the tier label read that note, so every 4663 lookup counted as "-1".
    import os
    d, cache, t = _dispatch(tmp_path)
    if tier is not None:
        d.on_datagram({"kind": "candidates", "sent_epoch": 1.0, "entries": [
            {"mint": TOKEN, "pool": POOL, "key": KEY, "priority": tier}]}, os.getpid(), os.getuid(), 1000.0)
    cache.run_one(TOKEN, now=1004.0)                           # a fresh pass at the dispatch clock
    d.on_datagram(t._payload(), os.getpid(), os.getuid(), 1000.0)
    assert dict(cache.lookups) == {label + ":hit": 1}, dict(cache.lookups)
    assert cache.next_due(1005.0 + 61.0) == TOKEN               # the signal still ranks it first
