"""The 4663 safety check's serial path (Alpha GO 2026-10-16, branch lambda/evm-safety-exit-hint).

0078cc00 spent 794 ms in evm.token_safety: code 19 ‖ sell quote 641, then slot discovery 103, then
simulation 28. The sell probe fell to Relay at 637 ms although the buy ladder found the coin through
the source's own pool in 295 ms, and the Permit2 slot step ran serially after the quote.

Pins:
(1) the sell probe quotes the exit through the V4 pool on the source's own receipt (never a
    remembered pool), in one rung, first in priority; a key that does not hash to that pool or does
    not hold the coin is refused before any quote; a hit only when the built route names that pool
    at the full amount; nothing is remembered; the simulation and verdict are untouched.
(2') a Permit2 sell's balance and allowance slot LAYOUTS are found beside the quote; the live
    allowance read and the amount word stay with the simulation, so no amount is ever pinned.
(3) multihop_quote marks each search step on the rung's http tap.
"""
import json
import threading
import time

import pytest

from sniper.execution import evm_real_seams as S
from sniper.execution import evm_source_route as src
from sniper.execution import evm_v4_quote as v4q
from sniper.execution.evm_token_safety import _simulation_amount_word, build_evm_token_safety
from sniper.execution.evm_v4_launchpad import pool_id_for_key
from test_memecoin_evm_token_safety import PERMIT2, TAKER, _slot, _standard_erc20_http

CHAIN = "4663"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
NATIVE = "0x" + "0" * 40
COIN = "0x" + "22" * 20
OTHER_COIN = "0x" + "33" * 20
BRIDGE = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
AMOUNT = 10 ** 21
KEY = {"currency0": NATIVE, "currency1": COIN, "fee": 10000, "tick_spacing": 200, "hooks": NATIVE}
POOL = pool_id_for_key(KEY).lower()
X_KEY = {"currency0": COIN, "currency1": BRIDGE, "fee": 3000, "tick_spacing": 60, "hooks": NATIVE}
X_POOL = pool_id_for_key(X_KEY).lower()
B_KEY = {"currency0": NATIVE, "currency1": BRIDGE, "fee": 500, "tick_spacing": 10, "hooks": NATIVE}
SLOW_S = 0.6


class _Ref:
    def __init__(self, post):
        self.post = post

    def payload(self):
        return self.post


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("MEMECOIN_EVM_SAFETY_EXIT_HINT_CHAINS", raising=False)
    monkeypatch.delenv("MEMECOIN_EVM_PERMIT2_SLOT_PREWARM_CHAINS", raising=False)
    monkeypatch.setattr(src, "_pools", {})
    monkeypatch.setattr(src, "_bridges", {})
    monkeypatch.setattr(v4q, "_key_cache", {})
    yield


def _sell(out, pool_id):
    return {"sellAmount": str(AMOUNT), "buyAmount": str(out),
            "route": {"protocol": "uniswap_v4", "pool_id": pool_id},
            "transaction": {"to": "0xrouter", "data": "0x"},
            "issues": {"allowance": {"spender": PERMIT2}}}


def _sell_ladder(monkeypatch, *, exit_hint=POOL, keys=None, built_pool=None, multihop_exit=None,
                 rungs=None, buy=False):
    """The 4663 ladder for one sell (or buy) with every discovery rung slow; returns (quote, seconds,
    the pinned quoter calls, the v4 override builds)."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_CURVE_ENABLED", False, raising=False)
    monkeypatch.setenv("MEMECOIN_EVM_SOURCE_ROUTE_HINT_CHAINS", "")
    monkeypatch.setattr(S, "_rung_ladder_chains", lambda _c: {CHAIN})
    monkeypatch.setattr(v4q, "_pool_key_cached", lambda c, p: dict((keys or {}).get(p) or {}) or None)
    monkeypatch.setattr(src, "_tape_key", lambda c, p: None)
    pinned_calls, builds = [], []

    def pinned_fn(chain, pool_id, k, sell, buy_t, amount, *, rpc_url, http=None):
        pinned_calls.append((pool_id, sell.lower(), buy_t.lower(), amount))
        return {"amount_out": 777, "pool_id": pool_id, "pool_key": k, "zero_for_one": False,
                "pool_fee": k["fee"], "quote_currency": NATIVE}
    monkeypatch.setattr(v4q, "v4_pinned_pool_quote", pinned_fn)

    def v4_fn(chain, sell, buy_t, amount, taker, **kw):
        if kw.get("quote_override") is not None:
            builds.append(kw["quote_override"]["pool_id"])
            return _sell(kw["quote_override"]["amount_out"], built_pool or kw["quote_override"]["pool_id"])
        time.sleep(SLOW_S)
        return _sell(900, "0xprovider")

    def slow_multihop(*a, **k):
        time.sleep(SLOW_S)
        return None
    monkeypatch.setattr("sniper.execution.evm_v4_multihop.dex_v4_multihop_quote", slow_multihop)
    if multihop_exit is not None:
        monkeypatch.setattr("sniper.execution.evm_v4_multihop.dex_v4_multihop_exit_quote",
                            multihop_exit)
    fn = S.venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain=CHAIN, quote_token=WETH, taker=TAKER,
        v2_quote_fn=lambda *a, **k: None, v4_quote_fn=v4_fn, relay_quote_fn=lambda *a, **k: None,
        acct=object(), rpc_url="https://rpc", on_rung=(rungs.append if rungs is not None else None))
    kw = {"exit_hint": exit_hint} if exit_hint is not None else {}
    started = time.monotonic()
    if buy:
        out = fn(WETH, COIN, 10 ** 16, slippage_bps=100, **kw)
    else:
        out = fn(COIN, WETH, AMOUNT, slippage_bps=100, first_available=True, **kw)
    return out, time.monotonic() - started, pinned_calls, builds


def _rec(rungs, name):
    return next(r for r in rungs if r["rung"] == name)


# --- (1) the hinted exit -------------------------------------------------------------------------

def test_the_hinted_exit_is_quoted_in_one_rung_and_wins_the_probe(monkeypatch):
    rungs = []
    out, took, pinned, builds = _sell_ladder(monkeypatch, keys={POOL: KEY}, rungs=rungs)
    assert out["_sniper_winner"] == "v4_source_exit" and out["route"]["pool_id"] == POOL
    assert pinned == [(POOL, COIN, WETH, AMOUNT)] and builds == [POOL]    # one quote, one build
    assert took < SLOW_S, "the probe did not wait for discovery"
    assert _rec(rungs, "v4_source_exit")["hit"] and rungs[0]["rung"] == "v4_source_exit"
    assert src.remembered(CHAIN, COIN) is None, "a verdict-path hint is never remembered"
    assert S.LADDER_ORDER[0] == "v4_source_exit" and "v4_source_exit" not in src.SOURCE_RUNGS


def test_split_receipt_probe_uses_only_its_unique_verified_cash_pool(monkeypatch):
    out, took, pinned, builds = _sell_ladder(
        monkeypatch, exit_hint=(X_POOL, POOL), keys={X_POOL: X_KEY, POOL: KEY})
    assert out["_sniper_winner"] == "v4_source_exit"
    assert pinned == [(POOL, COIN, WETH, AMOUNT)] and builds == [POOL]
    assert took < SLOW_S
    assert src.remembered(CHAIN, COIN) is None


@pytest.mark.parametrize("case", ["unknown", "multiple_cash", "no_cash", "wrong_hash"])
def test_unresolved_split_probe_keeps_the_broad_safety_ladder(monkeypatch, case):
    key2 = dict(KEY, fee=3000)
    pool2 = pool_id_for_key(key2).lower()
    keys = {POOL: KEY, pool2: key2}
    if case == "unknown": keys.pop(pool2)
    elif case == "no_cash":
        key2 = dict(X_KEY, fee=10000)
        pool2 = pool_id_for_key(key2).lower()
        keys, pool1 = {X_POOL: X_KEY, pool2: key2}, X_POOL
    elif case == "wrong_hash": keys[pool2] = KEY
    out, _took, pinned, builds = _sell_ladder(
        monkeypatch, exit_hint=(pool1 if case == "no_cash" else POOL, pool2), keys=keys)
    assert out["_sniper_winner"] == "v4"
    assert pinned == [] and builds == []


@pytest.mark.parametrize("keys", [
    {POOL: dict(KEY, currency1=OTHER_COIN)},     # the hinted pool's key holds another coin
    {POOL: dict(KEY, fee=3000)},                 # a key that does not hash to the hinted pool
    {},                                          # no local key at all
])
def test_a_wrong_pool_hint_is_refused_before_the_quote(monkeypatch, keys):
    if keys and keys[POOL]["currency1"] == OTHER_COIN:
        hint = pool_id_for_key(keys[POOL]).lower()
        keys = {hint: keys[POOL]}
    else:
        hint = POOL
    rungs = []
    out, took, pinned, builds = _sell_ladder(monkeypatch, exit_hint=hint, keys=keys, rungs=rungs)
    assert _rec(rungs, "v4_source_exit")["reason"] == "source_key_unknown"
    assert pinned == [] and builds == [], "no quote was asked for a pool not proved to hold the coin"
    assert out["_sniper_winner"] == "v4" and out["route"]["pool_id"] == "0xprovider"


def test_a_built_route_naming_another_pool_is_a_miss(monkeypatch):
    rungs = []
    out, _t, pinned, _b = _sell_ladder(monkeypatch, keys={POOL: KEY}, built_pool="0x" + "ee" * 32,
                                       rungs=rungs)
    assert _rec(rungs, "v4_source_exit")["reason"] == "source_exit_pool_mismatch"
    assert out["_sniper_winner"] == "v4"


def _exit_stub(calls, *, coin_key=X_KEY, partial=False):
    def exit_quote(chain, mint, amount, taker, *, route=None, **kw):
        calls.append(dict(route))
        built = _sell(555, None)
        built["route"] = {"protocol": "uniswap_v4_multihop", "coin_key": coin_key,
                          "bridge": route["bridge"], "bridge_key": route["bridge_key"]}
        if partial:
            built["partial_exit"] = True
        return built
    return exit_quote


def test_a_bridged_coin_exits_through_the_cached_bridge_pool(monkeypatch):
    src.remember_bridge(CHAIN, {"legs_path": ({"currency": BRIDGE, "key": B_KEY},
                                              {"currency": COIN, "key": X_KEY}),
                                "bridge_pool_id": pool_id_for_key(B_KEY), "bridge_depth_usd": 1e6})
    calls, rungs = [], []
    out, took, pinned, _b = _sell_ladder(monkeypatch, exit_hint=X_POOL, keys={X_POOL: X_KEY},
                                         multihop_exit=_exit_stub(calls), rungs=rungs)
    assert out["_sniper_winner"] == "v4_source_exit" and took < SLOW_S
    assert calls == [{"bridge": BRIDGE, "bridge_key": B_KEY, "coin_key": X_KEY}] and pinned == []


@pytest.mark.parametrize("case, why", [("uncached", "source_bridge_uncached"),
                                       ("partial", "source_exit_partial"),
                                       ("other_coin_pool", "source_exit_pool_mismatch")])
def test_a_bridged_exit_that_is_not_the_hinted_full_route_is_a_miss(monkeypatch, case, why):
    if case != "uncached":
        src.remember_bridge(CHAIN, {"legs_path": ({"currency": BRIDGE, "key": B_KEY},
                                                  {"currency": COIN, "key": X_KEY}),
                                    "bridge_pool_id": pool_id_for_key(B_KEY)})
    calls, rungs = [], []
    stub = _exit_stub(calls, partial=case == "partial",
                      coin_key=dict(X_KEY, fee=10000) if case == "other_coin_pool" else X_KEY)
    out, _t, _p, _b = _sell_ladder(monkeypatch, exit_hint=X_POOL, keys={X_POOL: X_KEY},
                                   multihop_exit=stub, rungs=rungs)
    assert _rec(rungs, "v4_source_exit")["reason"] == why
    assert out["_sniper_winner"] == "v4"
    assert (calls == []) is (case == "uncached")


def test_buys_and_unhinted_sells_build_todays_rung_set(monkeypatch):
    for kw in ({"buy": True}, {"exit_hint": None}):
        rungs = []
        _sell_ladder(monkeypatch, keys={POOL: KEY}, rungs=rungs, **kw)
        assert "v4_source_exit" not in {r["rung"] for r in rungs}, kw


def test_the_exit_hint_is_the_receipt_pool_only(monkeypatch):
    ref = _Ref({"venue": "v4", "pool": POOL})
    assert src.safety_exit_hint(CHAIN, ref) == POOL                     # default: 4663 on
    src.remember(CHAIN, COIN, X_POOL)
    assert src.safety_exit_hint(CHAIN, _Ref({"reason": "split_route"})) is None   # never remembered
    assert src.safety_exit_hint("56", ref) is None
    for raw in ("", "4663,x"):
        monkeypatch.setenv("MEMECOIN_EVM_SAFETY_EXIT_HINT_CHAINS", raw)
        assert src.safety_exit_hint(CHAIN, ref) is None, raw


@pytest.mark.parametrize("hint", [POOL, (X_POOL, POOL)])
def test_the_safety_probe_hands_the_hint_to_its_sell_quotes_only(hint):
    calls = []

    def quote(sell_token, buy_token, amount, **kw):
        calls.append((sell_token.lower() == COIN, kw.get("exit_hint")))
        return None                              # every probe misses: buy probe and sells all run

    fn = build_evm_token_safety(chain_id=CHAIN, rpc_url="https://rpc", taker=TAKER,
                                http=_standard_erc20_http(COIN), quote_fn=quote, fast_quote_fn=quote,
                                decimals_fn=lambda _m: 18)
    fn(COIN, exit_hint=hint)
    assert {got for is_sell, got in calls if is_sell} == {hint}
    assert all(hint is None for is_sell, hint in calls if not is_sell)   # a buy probe never
    calls.clear()
    fn(COIN)
    assert {hint for _s, hint in calls} == {None}


@pytest.mark.parametrize("blocked", [False, True])
def test_split_hint_still_requires_the_funded_sell_simulation(blocked):
    from test_memecoin_evm_token_safety import _standard_quote
    inner = _standard_erc20_http(COIN, sell_blocked=blocked)
    simulations, hints = [], []
    standard_quote = _standard_quote(COIN)

    def quote(*args, **kwargs):
        hints.append(kwargs.pop("exit_hint", None))
        return standard_quote(*args, **kwargs)

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        if req["method"] == "eth_call" and req["params"][0].get("data") == "0xdeadbeef":
            simulations.append(req)
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    fn = build_evm_token_safety(chain_id=CHAIN, rpc_url="https://rpc", taker=TAKER,
                                http=http, quote_fn=quote, fast_quote_fn=quote,
                                decimals_fn=lambda _m: 18)
    ok, _reason = fn(COIN, exit_hint=(X_POOL, POOL))
    assert ok is not blocked
    assert simulations and hints[0] == (X_POOL, POOL)


def test_the_early_and_leg_safety_calls_carry_the_receipt_pool(monkeypatch):
    from sniper.services import memecoin_real_runner as R
    from sniper.services.memecoin_evm_leg import safety_exit_hint_kwargs
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_EARLY_CACHED_SIZING", True, raising=False)
    seen = []

    def tsf(mint, *, deadline=None, usd=None, timings=None, exit_hint=None):
        seen.append(exit_hint)
        return True, "ok"
    ref = _Ref({"venue": "v4", "pool": POOL})
    R._early_evm_safety_fn({"token_safety_fn": tsf}, CHAIN, ref)(COIN, 50.0, None, {})
    R._early_evm_safety_fn({"token_safety_fn": tsf}, "56", ref)(COIN, 50.0, None, {})
    assert seen == [POOL, None]
    assert safety_exit_hint_kwargs(lambda m, **kw: None, CHAIN, ref) == {"exit_hint": POOL}
    assert safety_exit_hint_kwargs(lambda m, usd=None: None, CHAIN, ref) == {}
    assert safety_exit_hint_kwargs(tsf, CHAIN, _Ref(None)) == {}


def test_the_4663_curve_wrapper_forwards_the_hint_to_the_standard_check():
    from sniper.execution.evm_launchpad_curve import wrap_token_safety_fn
    seen = []

    def fallback(mint, *, deadline=None, usd=None, bought_amount_atomic=None, timings=None,
                 exit_hint=None):
        seen.append(exit_hint)
        return True, "ok"
    wrapped = wrap_token_safety_fn(CHAIN, "https://rpc", taker=TAKER, fallback_fn=fallback,
                                   state_fn=lambda *a, **k: None)
    assert wrapped(COIN, usd=50.0, exit_hint=POOL) == (True, "ok") and seen == [POOL]


# --- (2') the Permit2 layouts beside the quote ---------------------------------------------------

MINT = "0x" + "ab" * 18 + "4663"
BAL_SLOT, ALLOW_SLOT = 51, 52


def _is_probe(req):
    params = req.get("params") or []
    return (req["method"] == "eth_call" and len(params) >= 3
            and (params[0].get("data") or "")[:10] in ("0x70a08231", "0xdd62ed3e"))


def _is_plain_allowance(req):
    params = req.get("params") or []
    return (req["method"] == "eth_call" and len(params) < 3
            and (params[0].get("data") or "").startswith("0xdd62ed3e"))


def _permit2_http(log, *, existing_allowance=0, fail_probes=0, probe_delay=0.0):
    inner = _standard_erc20_http(MINT, balance_slot=BAL_SLOT, allowance_slot=ALLOW_SLOT,
                                 allowance_spender=PERMIT2)
    left = {"n": fail_probes}
    lock = threading.Lock()

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        if _is_plain_allowance(req):
            log.append(("plain_allowance", time.monotonic()))
            return 200, {"result": hex(existing_allowance)}, None
        if _is_probe(req):
            log.append(("probe", time.monotonic()))
            with lock:
                fail = left["n"] > 0
                left["n"] -= fail
            if fail:
                raise OSError("connection reset")
            time.sleep(probe_delay)
        elif req["method"] == "eth_call" and len(req["params"]) >= 3:
            log.append(("sim", req["params"][2]))
        return inner(method, url, headers=headers, body=body, timeout=timeout)
    return http


def _permit2_quote(log, *, hold_s=0.0, amounts=None):
    amounts = list(amounts or [AMOUNT])

    def quote(sell_token, buy_token, amount, **kw):
        if sell_token.lower() != MINT:
            return None
        time.sleep(hold_s)
        log.append(("quoted", time.monotonic()))
        return {"sellAmount": str(amounts.pop(0) if len(amounts) > 1 else amounts[0]),
                "buyAmount": "1", "transaction": {"to": "0xrouter", "data": "0xdeadbeef"},
                "issues": {"allowance": {"spender": PERMIT2}}}
    return quote


def _permit2_safety(http, quote, opt_in=True):
    return build_evm_token_safety(chain_id=CHAIN, rpc_url="https://rpc", taker=TAKER, http=http,
                                  quote_fn=quote, fast_quote_fn=quote, decimals_fn=lambda _m: 18,
                                  permit2_slot_prewarm=opt_in)


def _first(log, kind):
    return next(t for k, t in log if k == kind)


@pytest.mark.parametrize("knob, opt_in, early", [(None, True, True), ("", True, False),
                                                 ("56", True, False), (None, False, False)])
def test_permit2_layouts_are_found_beside_the_quote_on_listed_chains(monkeypatch, knob, opt_in,
                                                                    early):
    """Only a builder that opts in (the funded leg); the rh fast lane's research and inline hosts
    keep today's reads."""
    if knob is not None:
        monkeypatch.setenv("MEMECOIN_EVM_PERMIT2_SLOT_PREWARM_CHAINS", knob)
    log = []
    ok, reason = _permit2_safety(_permit2_http(log), _permit2_quote(log, hold_s=0.3),
                                 opt_in=opt_in)(MINT)
    assert ok, reason
    assert (_first(log, "probe") < _first(log, "quoted")) is early


def test_a_prewarmed_allowance_slot_never_pins_an_amount_override():
    """Alpha's pin: the prewarm finds layouts only. The live allowance read happens after the quote
    (the first call; later calls reuse today's proven override), and each simulation's override
    carries ITS OWN amount word, whatever an earlier call sold."""
    bal_key = _slot(TAKER, BAL_SLOT)
    allow_key = _slot(PERMIT2, _slot(TAKER, ALLOW_SLOT))
    log = []
    second = 7 * 10 ** 24
    fn = _permit2_safety(_permit2_http(log), _permit2_quote(log, hold_s=0.2,
                                                           amounts=[AMOUNT, second]))
    for amount in (AMOUNT, second):
        log.clear()
        ok, reason = fn(MINT)
        assert ok, reason
        reads = [t for k, t in log if k == "plain_allowance"]
        assert all(t > _first(log, "quoted") for t in reads), "the allowance read stays inline"
        assert reads or amount == second
        diff = next(v for k, v in log if k == "sim")[MINT]["stateDiff"]
        word = _simulation_amount_word(amount)
        assert diff == {bal_key: word, allow_key: word}


def test_an_existing_allowance_still_decides_at_the_simulation():
    """The live read covering the amount gives today's balance-only override, prewarm or not."""
    bal_key = _slot(TAKER, BAL_SLOT)
    log = []
    ok, reason = _permit2_safety(_permit2_http(log, existing_allowance=2 ** 255),
                                 _permit2_quote(log, hold_s=0.2))(MINT)
    diff = next(v for k, v in log if k == "sim")[MINT]["stateDiff"]
    assert diff == {bal_key: _simulation_amount_word(AMOUNT)}


def test_the_serial_slot_step_is_gone_when_the_layouts_landed_first(monkeypatch):
    def run(knob):
        monkeypatch.setenv("MEMECOIN_EVM_PERMIT2_SLOT_PREWARM_CHAINS", knob)
        log = []
        ok, reason = _permit2_safety(_permit2_http(log, probe_delay=0.02),
                                     _permit2_quote(log, hold_s=0.3))(MINT)
        assert ok, reason
        quoted = _first(log, "quoted")
        return (len([1 for k, _t in log if k == "probe"]),
                len([1 for k, t in log if k == "probe" and t > quoted]))
    on, off = run("4663"), run("")
    assert on[0] == off[0], "the same probes, moved earlier"
    assert on[1] == 0 and off[1] > 0, "no slot probe after the quote with the prewarm"


def test_a_layout_prewarm_failure_is_discovered_again_at_the_simulation(monkeypatch):
    """Both prewarm layouts fail (prewarm threads only: the faults are the speculative reads', so the
    cold discovery that may now overlap a still-running allowance prewarm meets a healthy host)."""
    import time as clock
    monkeypatch.setattr(clock, "sleep", lambda _s: None)     # _read_with_retry's backoff
    log, inner = [], None

    def http(method, url, headers=None, body=None, timeout=10):
        if (_is_probe(json.loads(body))
                and threading.current_thread().name.startswith("evm-safety-layout")):
            log.append(("failed_prewarm", time.monotonic()))
            raise OSError("connection reset")
        return inner(method, url, headers=headers, body=body, timeout=timeout)
    inner = _permit2_http(log)
    ok, reason = _permit2_safety(http, _permit2_quote(log))(MINT)
    assert ok, reason
    assert any(k == "failed_prewarm" for k, _t in log)


# --- (3) the multihop search step marks ----------------------------------------------------------

def test_the_multihop_search_marks_each_step_on_the_rung_tap(monkeypatch):
    from sniper.execution import evm_v4_multihop as mh
    from sniper.execution import evm_wallet as w
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST", False, raising=False)
    monkeypatch.setattr(mh, "_pools_with_depth", lambda *a, **k: [])
    monkeypatch.setattr(mh, "_candidate_bridges", lambda *a, **k: [BRIDGE])
    monkeypatch.setattr(mh, "_bridge_options", lambda *a, **k: [("0x" + "bb" * 32, B_KEY, 1e6)])
    monkeypatch.setattr(mh, "_coin_leg_options", lambda *a, **k: [])
    w._HTTP_TAP.calls = []
    try:
        assert mh.multihop_quote(CHAIN, NATIVE, COIN, 10 ** 16, rpc_url="https://rpc",
                                 clip_usd=50.0, bridge_currencies=(BRIDGE,)) is None
        calls = list(w._HTTP_TAP.calls)
    finally:
        w._HTTP_TAP.calls = None
    steps = [c for c in calls if c["label"] in ("v4_multihop:candidate_bridges",
                                                "v4_multihop:bridge_options",
                                                "v4_multihop:coin_leg_options")]
    assert [c["label"] for c in steps] == ["v4_multihop:candidate_bridges",
                                           "v4_multihop:bridge_options",
                                           "v4_multihop:coin_leg_options"]
    assert {c["discovery"] for c in steps} == {"provider"}
    assert [c.get("bridge") for c in steps[1:]] == [BRIDGE, BRIDGE]


def _slow_allowance_layout_http(log, *, existing_allowance):
    """The allowance-LAYOUT scan (an allowance read with a state override) hits the safety deadline;
    the plain live allowance read and the balance layout answer normally."""
    from sniper.execution.evm_token_safety import EvmSafetyDeadlineExceeded
    inner = _permit2_http(log, existing_allowance=existing_allowance)

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        params = req.get("params") or []
        if (_is_probe(req) and (params[0].get("data") or "").startswith("0xdd62ed3e")):
            log.append(("allowance_layout", time.monotonic()))
            raise EvmSafetyDeadlineExceeded()
        return inner(method, url, headers=headers, body=body, timeout=timeout)
    return http


@pytest.mark.parametrize("opt_in", [False, True])
def test_a_covering_live_allowance_never_waits_on_the_unneeded_allowance_layout(opt_in):
    """Codex F4 (review through 648ada5d7): the 4663 prewarm awaited BOTH Permit2 layouts before the
    live allowance read. With an allowance that already covers the simulated amount the cold path needs
    no allowance layout, yet a slow scan of it refused the entry as safety_deadline_exceeded (prewarm
    off: ok). Now the live allowance decides whether the allowance layout is waited on at all."""
    bal_key = _slot(TAKER, BAL_SLOT)
    log = []
    _ok, reason = _permit2_safety(_slow_allowance_layout_http(log, existing_allowance=2 ** 255),
                                  _permit2_quote(log, hold_s=0.1), opt_in=opt_in)(MINT)
    # (The fake chain's simulation does not model a standing allowance, so its verdict is not the
    # point; the point is that the simulation runs, on the cold path's override.)
    assert reason != "safety_deadline_exceeded"
    diff = next(v for k, v in log if k == "sim")[MINT]["stateDiff"]
    assert diff == {bal_key: _simulation_amount_word(AMOUNT)}, "balance-only override, as cold"


@pytest.mark.parametrize("opt_in", [False, True])
def test_a_short_live_allowance_still_needs_its_layout_and_fails_closed_on_its_deadline(opt_in):
    log = []
    ok, reason = _permit2_safety(_slow_allowance_layout_http(log, existing_allowance=0),
                                 _permit2_quote(log, hold_s=0.1), opt_in=opt_in)(MINT)
    assert (ok, reason) == (False, "safety_deadline_exceeded")
    assert not [1 for k, _v in log if k == "sim"], "no simulation without its allowance slot"


def _failed_balance_prewarm_http(log, *, existing_allowance):
    """Codex F4 residual: the speculative BALANCE layout fails (a recoverable transport error, prewarm
    threads only), and the speculative ALLOWANCE layout hits the safety deadline. The cold path's own
    balance scan and the plain live allowance read answer normally."""
    from sniper.execution.evm_token_safety import EvmSafetyDeadlineExceeded
    inner = _permit2_http(log, existing_allowance=existing_allowance)

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        first = (req.get("params") or [None])[0]
        data = (first.get("data") or "") if isinstance(first, dict) else ""
        if _is_probe(req) and data.startswith("0xdd62ed3e"):
            log.append(("allowance_layout", time.monotonic()))
            raise EvmSafetyDeadlineExceeded()
        if (_is_probe(req) and data.startswith("0x70a08231")
                and threading.current_thread().name.startswith("evm-safety-layout")):
            log.append(("failed_balance_prewarm", time.monotonic()))
            raise OSError("connection reset")
        return inner(method, url, headers=headers, body=body, timeout=timeout)
    return http


def test_a_failed_balance_prewarm_still_never_waits_on_the_unneeded_allowance_layout(monkeypatch):
    """Codex verification of F4 (8cab385f2): after a failed speculative balance layout, the code joined
    the allowance layout before discovery and the live allowance read, so a slow unused allowance scan
    refused a covering allowance as safety_deadline_exceeded with no live read at all."""
    import time as clock
    monkeypatch.setattr(clock, "sleep", lambda _s: None)     # _read_with_retry's backoff
    bal_key = _slot(TAKER, BAL_SLOT)
    log = []
    _ok, reason = _permit2_safety(_failed_balance_prewarm_http(log, existing_allowance=2 ** 255),
                                  _permit2_quote(log, hold_s=0.1))(MINT)
    assert any(k == "failed_balance_prewarm" for k, _t in log), "the prewarm did fail"
    assert reason != "safety_deadline_exceeded"
    assert any(k == "plain_allowance" for k, _t in log), "the live allowance decided"
    diff = next(v for k, v in log if k == "sim")[MINT]["stateDiff"]
    assert diff == {bal_key: _simulation_amount_word(AMOUNT)}, "balance-only override, as cold"


def test_a_failed_balance_prewarm_with_a_short_allowance_still_fails_closed(monkeypatch):
    import time as clock
    monkeypatch.setattr(clock, "sleep", lambda _s: None)
    log = []
    ok, reason = _permit2_safety(_failed_balance_prewarm_http(log, existing_allowance=0),
                                 _permit2_quote(log, hold_s=0.1))(MINT)
    assert (ok, reason) == (False, "safety_deadline_exceeded")
    assert not [1 for k, _v in log if k == "sim"], "no simulation without its allowance slot"
