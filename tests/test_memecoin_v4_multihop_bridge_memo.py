"""v4_multihop provider discovery: both providers at once, and a memo for bridge legs (Alpha GO
2026-10-17).

On the six 4663 sends v4_multihop won since 10-14 the rung waited on GeckoTerminal serially: the
coin's lookup (median ~173 ms per send), then DexScreener behind it, then the bridge legs' lookups
(median ~380 ms per send). (1) GeckoTerminal and DexScreener now run at once in _pools_with_depth,
same results. (2) A bridge leg's lookup is the same URL for every coin, so it is remembered,
served while younger than MEMECOIN_EVM_MULTIHOP_BRIDGE_MEMO_MAX_AGE_S and refreshed off the entry
path. A refused or cut read is never remembered; a coin's own lookup never is.
"""
import threading
import time

import pytest

from sniper.execution import evm_v4_multihop as mh
from sniper.execution import evm_v4_quote as V
from sniper.execution import evm_wallet
from sniper.execution.evm_rpc_pool import RequestNotSent
from sniper.settings import memecoin as cfg

CHAIN = "4663"
CASH = "0x0000000000000000000000000000000000000000"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
COIN = "0x" + "7d" * 20
P = ["0x" + ("%02x" % i) * 32 for i in range(1, 6)]
_REAL_ENSURE = mh._ensure_bridge_refresher       # the conftest replaces it per test


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MULTIHOP_BRIDGE_MEMO_MAX_AGE_S", 180.0)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MULTIHOP_BRIDGE_REFRESH_S", 0.0)   # no thread rounds
    monkeypatch.setattr(V, "_stored_pools_for_mint", lambda chain, token: [])
    mh.reset_bridge_memo()
    yield
    mh.reset_bridge_memo()


def _providers(monkeypatch, gt=((900.0, P[0]),), ds=((500.0, P[1]),), gt_failed=False, ds_failed=False):
    calls = {"gt": 0, "ds": 0}

    def gt_read(chain, mint, gt_http=None, *, request_http=None, pair_token=None):
        calls["gt"] += 1
        return list(gt), gt_failed

    def ds_read(chain, mint, ds_http=None, pair_token=None, *, request_http=None, funded=True):
        calls["ds"] += 1
        return list(ds), ds_failed
    gt_read.__name__, ds_read.__name__ = "_gt_pools_ranked", "_ds_pools_ranked"
    monkeypatch.setattr(V, "_gt_pools_ranked", gt_read)
    monkeypatch.setattr(V, "_ds_pools_ranked", ds_read)
    return calls


# --- (1) both providers at once ------------------------------------------------------------------

def test_dexscreener_starts_before_geckoterminal_returns_and_results_are_unchanged(monkeypatch):
    ds_started, seen = threading.Event(), {}

    def gt_read(chain, mint, gt_http=None, *, request_http=None, pair_token=None):
        seen["overlap"] = ds_started.wait(2.0)            # serial code: DS has not started yet
        return [(900.0, P[0]), (10.0, P[2])], False

    def ds_read(chain, mint, ds_http=None, pair_token=None, *, request_http=None, funded=True):
        ds_started.set()
        return [(500.0, P[1]), (20.0, P[2])], False
    monkeypatch.setattr(V, "_gt_pools_ranked", gt_read)
    monkeypatch.setattr(V, "_ds_pools_ranked", ds_read)
    began = time.monotonic()
    got = mh._pools_with_depth(CHAIN, COIN)
    assert seen["overlap"] is True and time.monotonic() - began < 1.5
    assert got == [(P[0], 900.0), (P[1], 500.0), (P[2], 20.0)]   # the deeper reading per pool


def test_both_calls_land_in_the_rungs_tap_and_a_helper_exception_reaches_the_caller(monkeypatch):
    _providers(monkeypatch)
    calls = evm_wallet.start_http_tap()
    try:
        mh._pools_with_depth(CHAIN, COIN)
    finally:
        evm_wallet.take_http_tap()
    assert sorted(c["label"] for c in calls) == ["v4_multihop:provider:_ds_pools_ranked",
                                                 "v4_multihop:provider:_gt_pools_ranked"]

    def refused(*a, **k):
        raise RequestNotSent("multihop search limit")
    monkeypatch.setattr(V, "_ds_pools_ranked", refused)
    with pytest.raises(RequestNotSent):
        mh._pools_with_depth(CHAIN, COIN)


# --- (2) the bridge-leg memo ---------------------------------------------------------------------

def test_a_bridge_lookup_is_served_from_the_memo_until_it_ages_out(monkeypatch):
    calls = _providers(monkeypatch)
    first = mh._pools_with_depth(CHAIN, USDG, include_store=False, pair_token=CASH, memo=True)
    tap = evm_wallet.start_http_tap()
    try:
        again = mh._pools_with_depth(CHAIN, USDG, include_store=False, pair_token=CASH, memo=True)
    finally:
        evm_wallet.take_http_tap()
    assert again == first and calls == {"gt": 1, "ds": 1}
    assert {c["outcome"] for c in tap} == {"memo"} and len(tap) == 2
    for entry in mh._BRIDGE_MEMO.values():
        entry["at"] -= 181.0                                   # past MAX_AGE_S: today's live call
    mh._pools_with_depth(CHAIN, USDG, include_store=False, pair_token=CASH, memo=True)
    assert calls == {"gt": 2, "ds": 2}


def test_a_coin_lookup_and_an_injected_transport_are_never_remembered(monkeypatch):
    calls = _providers(monkeypatch)
    mh._pools_with_depth(CHAIN, COIN)
    mh._pools_with_depth(CHAIN, COIN)
    assert calls == {"gt": 2, "ds": 2} and mh._BRIDGE_MEMO == {}
    mh._pools_with_depth(CHAIN, USDG, include_store=False, pair_token=CASH, memo=True,
                         gt_http=lambda url: None, ds_http=lambda url: None)
    assert len(mh._BRIDGE_MEMO) == 0


def test_a_refused_read_is_not_remembered_and_the_knob_at_zero_is_off(monkeypatch):
    calls = _providers(monkeypatch, gt_failed=True)
    mh._pools_with_depth(CHAIN, USDG, include_store=False, pair_token=CASH, memo=True)
    mh._pools_with_depth(CHAIN, USDG, include_store=False, pair_token=CASH, memo=True)
    assert calls["gt"] == 2 and calls["ds"] == 1             # DS remembered, the GT refusal not
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MULTIHOP_BRIDGE_MEMO_MAX_AGE_S", 0.0)
    mh._pools_with_depth(CHAIN, USDG, include_store=False, pair_token=CASH, memo=True)
    assert calls == {"gt": 3, "ds": 2}


def test_a_read_the_budget_cut_is_not_remembered(monkeypatch):
    def slow(chain, mint, gt_http=None, *, request_http=None, pair_token=None):
        time.sleep(0.3)
        return [(1.0, P[0])], False
    _providers(monkeypatch)
    monkeypatch.setattr(V, "_gt_pools_ranked", slow)
    budget = mh._SearchBudget(0.05, 100)
    got, failed = mh._provider_pools(V._gt_pools_ranked, CHAIN, USDG, budget=budget, memo=True,
                                     pair_token=CASH)
    assert (got, failed) == ([], False) and mh._BRIDGE_MEMO == {}


def test_the_bridge_and_middle_legs_ask_for_the_memo_and_the_coin_leg_does_not(monkeypatch):
    asked = []
    monkeypatch.setattr(mh, "_direct_bridge_pools", lambda *a, memo=False, **k: asked.append(
        ("direct", memo)) or None)
    monkeypatch.setattr(mh, "_pools_with_depth", lambda chain, token, memo=False, **k: asked.append(
        (token, memo)) or [])
    monkeypatch.setattr(mh, "_onchain_pair_options", lambda *a, **k: [])
    mh._bridge_options(CHAIN, CASH, USDG, "https://rpc.test", limit=2)
    mh._middle_leg_options(CHAIN, USDG, COIN, "https://rpc.test", limit=2)
    assert asked == [("direct", True), (USDG, True), (COIN, True)]


# --- the refresh, off the entry path -------------------------------------------------------------

def test_a_refresh_updates_the_answer_and_a_failed_one_keeps_the_last_until_the_age_bound(monkeypatch):
    state = {"pools": [(900.0, P[0])], "failed": False, "raise": False}

    def ds_read(chain, mint, ds_http=None, pair_token=None, *, request_http=None, funded=True):
        if state["raise"]:
            raise RuntimeError("provider down")
        return list(state["pools"]), state["failed"]
    ds_read.__name__ = "_ds_pools_ranked"
    monkeypatch.setattr(V, "_ds_pools_ranked", ds_read)
    assert mh._direct_bridge_pools(CHAIN, CASH, USDG, memo=True) == [(P[0], 900.0)]
    state["pools"] = [(700.0, P[3])]
    mh._bridge_refresh_once()
    assert mh._direct_bridge_pools(CHAIN, CASH, USDG, memo=True) == [(P[3], 700.0)]
    state.update(pools=[(1.0, P[4])], failed=True)
    mh._bridge_refresh_once()
    state.update(failed=False, **{"raise": True})
    mh._bridge_refresh_once()
    assert mh._direct_bridge_pools(CHAIN, CASH, USDG, memo=True) == [(P[3], 700.0)]
    for entry in mh._BRIDGE_MEMO.values():
        entry["at"] -= 181.0
    state.update(pools=[(5.0, P[2])], **{"raise": False})
    assert mh._direct_bridge_pools(CHAIN, CASH, USDG, memo=True) == [(P[2], 5.0)]   # live again


def test_the_geckoterminal_refresh_never_waits_on_the_shared_gate(monkeypatch):
    seen = []
    from sniper.execution import geckoterminal_quote as G
    monkeypatch.setattr(G, "gt_get_json", lambda url, **k: seen.append(k) or {"data": []})
    real_gt = V._gt_pools_ranked
    _providers(monkeypatch)
    monkeypatch.setattr(V, "_gt_pools_ranked", real_gt)
    monkeypatch.setitem(V.V4, CHAIN, dict(V.V4[CHAIN], gt_network="robinhood"))
    mh._bridge_memo_put((real_gt, CHAIN, USDG, CASH, None), ([(1.0, P[0])], False))
    mh._bridge_refresh_once()
    assert seen and seen[0]["gate_timeout_s"] == 0.0


def test_a_bridge_nobody_asks_for_is_dropped(monkeypatch):
    _providers(monkeypatch)
    mh._direct_bridge_pools(CHAIN, CASH, USDG, memo=True)
    assert len(mh._BRIDGE_MEMO) == 1
    mh._bridge_refresh_once(now_fn=lambda: time.monotonic() + mh._BRIDGE_MEMO_IDLE_S + 1)
    assert mh._BRIDGE_MEMO == {}


@pytest.mark.parametrize("raw,want", [("180", 180.0), ("0", 0.0), ("-1", 0.0), ("nan", 0.0),
                                      ("inf", 0.0), ("x", 0.0)])
def test_the_knobs_parse_fail_off(monkeypatch, raw, want):
    monkeypatch.setenv("MEMECOIN_EVM_MULTIHOP_BRIDGE_MEMO_MAX_AGE_S", raw)
    assert cfg._nonneg_seconds("MEMECOIN_EVM_MULTIHOP_BRIDGE_MEMO_MAX_AGE_S", "180") == want


def test_the_refresher_starts_once_as_a_daemon(monkeypatch):
    started = []

    class _Thread:
        def __init__(self, target, name, daemon):
            self.name, self.daemon, self.alive = name, daemon, False

        def start(self):
            self.alive = True
            started.append(self)

        def is_alive(self):
            return self.alive
    monkeypatch.setattr(mh.threading, "Thread", _Thread)
    monkeypatch.setattr(mh, "_BRIDGE_REFRESHER", [None])
    _REAL_ENSURE()
    _REAL_ENSURE()
    assert [(t.name, t.daemon) for t in started] == [("evm-multihop-bridge-memo", True)]


def test_the_concurrency_knob_off_reads_one_after_the_other(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MULTIHOP_PROVIDERS_CONCURRENT", False)
    order = []

    def gt_read(chain, mint, gt_http=None, *, request_http=None, pair_token=None):
        order.append("gt-start"); time.sleep(0.05); order.append("gt-end")
        return [(1.0, P[0])], False

    def ds_read(chain, mint, ds_http=None, pair_token=None, *, request_http=None, funded=True):
        order.append("ds-start")
        return [(2.0, P[1])], False
    monkeypatch.setattr(V, "_gt_pools_ranked", gt_read)
    monkeypatch.setattr(V, "_ds_pools_ranked", ds_read)
    assert mh._pools_with_depth(CHAIN, COIN) == [(P[1], 2.0), (P[0], 1.0)]
    assert order == ["gt-start", "gt-end", "ds-start"]


# --- Gamma MED-1 / MED-2 / LOW on 35fcad830 -----------------------------------------------------

def _gt_refresh_key(monkeypatch, transport):
    from sniper.execution import geckoterminal_quote as G
    monkeypatch.setattr(G, "_gt_disabled", lambda: False)
    monkeypatch.setattr(G, "_funded_cooldown_until", [0.0])
    monkeypatch.setattr(G, "_cooldown_until", [0.0])
    monkeypatch.setitem(V.V4, CHAIN, dict(V.V4[CHAIN], gt_network="robinhood"))
    key = (V._gt_pools_ranked, CHAIN, USDG, CASH, transport)
    mh._bridge_memo_put(key, ([(1.0, P[0])], False))
    return G, key


def test_a_refresh_429_books_the_shared_cooldown_and_never_the_funded_one(monkeypatch):
    """MED-1: the funded cell exists so background GeckoTerminal traffic cannot decide whether an
    entry quote may be attempted; a refresh 429 must not close it."""
    G, key = _gt_refresh_key(monkeypatch, lambda method, url, **k: (429, {}, {}))
    mh._bridge_refresh_once()
    assert G._funded_cooldown_until[0] == 0.0 and G._cooldown_until[0] > 0
    status = mh.bridge_memo_status()
    assert status["gt_429"] == 1 and status["refreshed"] == 0
    assert mh._bridge_memo_get(key) == ([(1.0, P[0])], False)      # the last answer stands


def test_the_gt_refresh_waits_out_a_cooldown_and_a_running_search(monkeypatch):
    calls = []
    G, _key = _gt_refresh_key(monkeypatch, lambda method, url, **k: calls.append(url) or (200, {"data": []}, {}))
    G._cooldown_until[0] = time.monotonic() + 60
    mh._bridge_refresh_once()
    G._cooldown_until[0] = 0.0
    monkeypatch.setattr(mh, "_SEARCHES_IN_FLIGHT", [1])
    mh._bridge_refresh_once()
    monkeypatch.setattr(mh, "_SEARCHES_IN_FLIGHT", [0])
    mh._bridge_refresh_once()
    status = mh.bridge_memo_status()
    assert status["gt_skipped_cooldown"] == 1 and status["gt_skipped_search"] == 1
    assert len(calls) == 1 and status["refreshed"] == 1 and status["rounds"] == 3


def test_a_search_is_counted_while_it_runs_even_when_it_raises(monkeypatch):
    seen = []

    def search(*a, **k):
        seen.append(mh._SEARCHES_IN_FLIGHT[0])
        if k.get("boom"):
            raise RuntimeError("x")
        return "route"
    monkeypatch.setattr(mh, "_multihop_quote_search", search)
    assert mh.multihop_quote(CHAIN, CASH, COIN, 1, rpc_url="r") == "route"
    with pytest.raises(RuntimeError):
        mh.multihop_quote(CHAIN, CASH, COIN, 1, rpc_url="r", boom=True)
    assert seen == [1, 1] and mh._SEARCHES_IN_FLIGHT[0] == 0


def test_an_entry_gt_read_records_its_gate_wait_in_the_tap(monkeypatch):
    """MED-2 (a): the coin lookup's wait on the shared gate, so a refresh holding it is visible."""
    from sniper.execution import geckoterminal_quote as G
    monkeypatch.setattr(G, "_gt_disabled", lambda: False)
    monkeypatch.setattr(G, "_funded_cooldown_until", [0.0])
    monkeypatch.setitem(V.V4, CHAIN, dict(V.V4[CHAIN], gt_network="robinhood"))
    calls = evm_wallet.start_http_tap()
    try:
        got = mh._provider_pools(V._gt_pools_ranked, CHAIN, COIN, budget=mh._SearchBudget(5.0, 50),
                                 request_http=lambda method, url, **k: (200, {"data": []}, None))
    finally:
        evm_wallet.take_http_tap()
    assert got == ([], False)
    record = next(c for c in calls if c["label"].endswith("_gt_pools_ranked"))
    assert record["gt_outcome"] == "ok" and record["gate_wait_ms"] >= 0.0


def test_a_result_that_returns_after_the_budget_expired_is_not_remembered(monkeypatch):
    """LOW: a read the deadline overtook may be partial; it is returned but never stored."""
    _providers(monkeypatch)

    class _Late:
        discovery_cut = False

        def __init__(self):
            self.checks = 0

        def expired(self, discovery=False):
            self.checks += 1
            return self.checks > 1                 # live at the start, expired once the read is back

        def remaining_s(self, discovery=False):
            return 1.0

        def take(self, discovery=False):
            return True
    got = mh._provider_pools(V._ds_pools_ranked, CHAIN, USDG, budget=_Late(), memo=True, pair_token=CASH)
    assert got == ([(500.0, P[1])], False) and mh._BRIDGE_MEMO == {}


def test_the_heartbeat_carries_the_memo_counters(monkeypatch, tmp_path):
    from sniper.services import memecoin_runner as runner
    _providers(monkeypatch)
    mh._direct_bridge_pools(CHAIN, CASH, USDG, memo=True)
    mh._direct_bridge_pools(CHAIN, CASH, USDG, memo=True)
    path = tmp_path / "hb.json"
    runner.write_heartbeat({}, path=path, auth_ok=True)
    import json
    memo = json.loads(path.read_text())["multihop_bridge_memo"]
    assert memo["entries"] == 1 and memo["stored"] == 1 and memo["served"] == 1
    assert {"rounds", "gt_skipped_busy", "gt_429", "refresher_alive"} <= set(memo)


def test_a_background_read_honors_a_shared_only_cooldown_without_a_call(monkeypatch):
    """Gamma LOW on f9ad45410: gt_get_json(funded=False) reads the SHARED cooldown cell. During a
    shared-only cooldown (a research 429; the funded cell is open) it returns None with no HTTP
    call, outcome "cooldown"; an entry read (funded=True) still goes out."""
    from sniper.execution import geckoterminal_quote as G
    monkeypatch.setattr(G, "_gt_disabled", lambda: False)
    monkeypatch.setattr(G, "_funded_cooldown_until", [0.0])
    monkeypatch.setattr(G, "_cooldown_until", [time.monotonic() + 60])
    calls = []

    def transport(method, url, **k):
        calls.append(url)
        return 200, {"data": []}, None
    assert G.gt_get_json("https://gt.test/x", http=transport, funded=False) is None
    assert calls == [] and G.last_get()["outcome"] == "cooldown"
    assert G.gt_get_json("https://gt.test/x", http=transport) == {"data": []}
    assert calls == ["https://gt.test/x"] and G.last_get()["outcome"] == "ok"


# --- Codex review through 648ada5d7: F5, F6, F10 --------------------------------------------------

def test_owned_metadata_adapters_share_one_memo_key_and_refresh_after_their_budget_ends():
    """F5: every search wrapped the owned 4663 metadata transport in a new _metadata_http closure with
    no _base, so equal lookups in two searches had different memo keys, and the key kept the first
    search's budget: the refresh raised RequestNotSent before dispatch."""
    sent = []

    def owned(method, url, headers=None, body=None, timeout=10):
        sent.append((url, dict(headers or {})))
        if url.endswith("/old"):
            return 302, None, {"Location": "https://x.test/new"}
        return 200, {"data": []}, {}
    a, b = mh._SearchBudget(5.0, 10), mh._SearchBudget(5.0, 10)
    wrap_a, wrap_b = mh._metadata_http(owned, a, "UA-1"), mh._metadata_http(owned, b, "UA-1")
    read = V._gt_pools_ranked
    key_a = mh._bridge_memo_key(read, (CHAIN, USDG), {"request_http": wrap_a})
    key_b = mh._bridge_memo_key(read, (CHAIN, USDG), {"request_http": wrap_b})
    assert key_a == key_b, "one stable transport identity across searches"
    assert mh._bridge_memo_key(read, (CHAIN, USDG), {"request_http": mh._metadata_http(
        owned, b, "UA-2")}) != key_a, "another user agent is another transport"
    expired = mh._SearchBudget(0.0, 0)
    stale = mh._bridge_memo_key(read, (CHAIN, USDG), {"request_http": mh._metadata_http(owned, expired, "UA-1")})
    refresh = stale[-1]
    assert refresh("GET", "https://x.test/old")[0] == 200, "the refresh is outside any search budget"
    assert [u for u, _h in sent] == ["https://x.test/old", "https://x.test/new"], "redirects kept"
    assert all(h["user-agent"] == "UA-1" and h["accept"] == "*/*" for _u, h in sent), "headers kept"
    with pytest.raises(RequestNotSent):
        mh._metadata_http(owned, expired, "UA-1")("GET", "https://x.test/new")   # a search stays budgeted


def test_a_background_dexscreener_refresh_failure_never_closes_the_funded_cell(monkeypatch):
    """F6: the refresher called _ds_pools_ranked with its funded default, so one background failure
    started the funded DexScreener cooldown (60 s, every chain and coin)."""
    from sniper.execution import solana_quote as sq
    monkeypatch.setattr(sq, "_ds_cooldown_until", [0.0])
    monkeypatch.setattr(sq, "_ds_funded_cooldown_until", [0.0])
    sent = []

    def refused(method, url, headers=None, body=None, timeout=10):
        sent.append(url)
        raise OSError("provider unavailable")
    key = (V._ds_pools_ranked, CHAIN, USDG, "", refused)
    now = time.monotonic()
    mh._BRIDGE_MEMO[key] = {"asked": now, "at": now, "result": ([], False)}
    mh._bridge_refresh_once()
    assert len(sent) == 1
    assert sq.dexscreener_cooldown_active() and not sq.dexscreener_cooldown_active(funded=True)
    mh._bridge_refresh_once()
    assert len(sent) == 1, "a refresh honours the shared cooldown without sending"
    assert V._ds_pools_ranked(CHAIN, USDG, request_http=refused) == ([], True)
    assert len(sent) == 2, "a funded lookup still dispatches"
    assert sq.dexscreener_cooldown_active(funded=True), "a funded failure still closes the funded cell"


def test_concurrent_first_memo_writes_start_one_refresher(monkeypatch):
    """F10: the thread was stored under the lock but started after it; a second first write saw
    is_alive() False and started a second permanent refresher (Codex: 39 of 50 trials)."""
    monkeypatch.setattr(mh, "_ensure_bridge_refresher", _REAL_ENSURE)
    monkeypatch.setattr(mh, "_BRIDGE_REFRESHER", [None])
    stop, started = threading.Event(), []
    monkeypatch.setattr(mh, "_bridge_refresh_loop", lambda: started.append(1) or stop.wait(5))
    real_start = threading.Thread.start

    def slow_start(self):
        time.sleep(0.01)                      # widen the window between store and start
        real_start(self)
    monkeypatch.setattr(threading.Thread, "start", slow_start)
    try:
        for _ in range(10):
            mh._BRIDGE_REFRESHER[0] = None
            stop.clear()
            started.clear()
            barrier = threading.Barrier(2)
            workers = [threading.Thread(target=lambda: (barrier.wait(), mh._ensure_bridge_refresher()))
                       for _ in range(2)]
            for t in workers:
                real_start(t)
            for t in workers:
                t.join(2)
            time.sleep(0.05)
            assert len(started) == 1, "two refreshers for one memo"
            stop.set()
            mh._BRIDGE_REFRESHER[0].join(2)
    finally:
        stop.set()
