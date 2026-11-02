"""4663 fast lane routes rev 3, the lane part (spec 2026-10-02-memecoin-4663-fast-lane-routes-design.md
section 6): the runner rebuilds M1's WETH and bridged buys at its own clip, keeps a route-shaped
registry (an older entry reads v4_native: Gamma C4), checks a WETH or bridged coin's safety on the
lane's own host only (C1), classifies a bridged coin on coin -> X and treats a failed X -> native
leg as the route's, never the coin's (C2), and notes every verdict's wall time by kind (C3)."""
import json
import os
import sqlite3
from fractions import Fraction

import pytest

from sniper.execution import evm_v4_quote as v4
from sniper.services import memecoin_rh_fast_decide as fast
from sniper.services import memecoin_rh_fast_lane as lane
from test_memecoin_rh_fast_decide import CTX, SIG, ZUP_IN, ZUP_PRE, _paid_for
from test_memecoin_rh_fast_routes import (A_SQRT, APOOL, B, B_LIQ, B_SQRT, BPOOL, COIN, USDG, WPOOL2,
                                          _brow, _btape)

HOST = "https://lane-host.example/k/"
FUNDED = ("https://funded-a.example/", "https://funded-b.example/")


def _bridged():
    src_in_x, src_out = 10 ** 8, 899_000_000
    tape = _btape(_brow(APOOL, B - 2, "0xa", A_SQRT),
                  _brow(BPOOL, B, "0xsrc", B_SQRT + 1000, liq=B_LIQ, a0=src_in_x, a1=-src_out))
    out = fast.decide(dict(SIG, mint=COIN, payment_token=USDG, paid=-(-src_in_x * 100 // 99)), tape=tape, **CTX)
    assert out["reason"] == "would_send" and out["route_kind"] == "v4_bridge"
    return out


def _weth():
    tape = _btape(_brow(WPOOL2, B - 2, "0xw", ZUP_PRE["sqrt"], liq=ZUP_PRE["liquidity"]))
    out = fast.decide(dict(SIG, mint=COIN, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)
    assert out["reason"] == "would_send" and out["route_kind"] == "v4_weth"
    return out


def _payload(out, mint=COIN):
    p = lane.lane_payload(dict(SIG, mint=mint, permit_nonce=1), out, sent_epoch=1000.0)
    return json.loads(json.dumps(p))                     # what the datagram carries


# --- 6.1 / 6.2: the datagram and the build ---------------------------------------------------------

@pytest.mark.parametrize("make", [_bridged, _weth])
def test_the_lane_rebuilds_m1s_exact_buy_from_the_datagram(make):
    out = make()
    tx, min_out = lane.lane_buy_tx(_payload(out), out["clip_wei"], 1060, CTX["taker"])
    assert (tx, min_out) == (out["tx"], out["min_out"])


def test_a_bridged_buy_prices_hop_a_at_the_mid_for_the_lanes_own_clip():
    out = _bridged()
    clip = out["clip_wei"] // 3
    tx, min_out = lane.lane_buy_tx(_payload(out), clip, 1060, CTX["taker"])
    cap = out["cap_price"]
    assert min_out == (clip * A_SQRT ** 2 >> 192) * cap.denominator // cap.numerator
    assert int(tx["value"], 16) == clip


def test_the_post_price_is_wei_per_coin_for_every_kind():
    b = _payload(_bridged())
    post_x = Fraction(2 ** 192, int(b["post_sqrt"]) ** 2)                 # X per coin (USDG is currency0)
    assert Fraction(b["post_wei_num"], b["post_wei_den"]) == post_x * Fraction(2 ** 192, A_SQRT ** 2)
    w = _payload(_weth())
    assert Fraction(w["post_wei_num"], w["post_wei_den"]) == fast.coin_price(int(w["post_sqrt"]), w["in_is_c0"])


def test_lane_event_reads_the_post_price_and_an_older_datagram_keeps_v1s():
    b = dict(_payload(_bridged()), eth_usd=2500.0, tx_hash="0xs", arrival_utc=None)
    ev = lane.LaneEvent(b, decimals=18, supply=10 ** 27)
    post = Fraction(b["post_wei_num"], b["post_wei_den"])
    assert ev.their_market_cap == pytest.approx(float(10 ** 27 * post / 10 ** 18 * 2500))
    old = {k: v for k, v in b.items() if k not in ("post_wei_num", "post_wei_den")}
    ev = lane.LaneEvent(old, decimals=18, supply=10 ** 27)
    v1 = Fraction(1 << 192, int(b["post_sqrt"]) ** 2)
    assert ev.their_market_cap == pytest.approx(float(10 ** 27 * v1 / 10 ** 18 * 2500))


def test_a_route_without_its_build_fields_is_refused():
    p = _payload(_bridged())
    for field in ("a_sqrt", "a_key", "bridge"):
        with pytest.raises((KeyError, TypeError, ValueError)):
            lane.lane_buy_tx(dict(p, **{field: None}), 10 ** 16, 1060, CTX["taker"])
    with pytest.raises(ValueError):
        lane.lane_buy_tx(dict(_payload(_weth()), in_is_c0=None), 10 ** 16, 1060, CTX["taker"])


# --- 6.3: the live gate --------------------------------------------------------------------------

def test_the_live_lane_refuses_a_kind_outside_its_routes_before_anything():
    live = lane.LiveLane.__new__(lane.LiveLane)
    live.stopped, live.routes, live.host_clock = False, frozenset({"v4_native"}), lambda: 1000.0
    rec = live.handle(dict(_payload(_bridged()), tx_hash="0xs"), recv_epoch=1000.0, safety_reason=None)
    assert rec["reason"] == "route_off:v4_bridge"
    old = {k: v for k, v in _payload(_weth()).items() if k != "route_kind"}
    live.routes = frozenset()
    assert live.handle(dict(old, tx_hash="0xs"), recv_epoch=1000.0,
                       safety_reason=None)["reason"] == "route_off:v4_native"


# --- 6.4: the registry (C4) ----------------------------------------------------------------------

def test_an_old_registry_entry_and_a_kindless_candidate_read_as_v4_native():
    key = {"currency0": v4._NATIVE, "currency1": COIN}
    assert lane.registry_entry((key, "0xp")) == {"kind": "v4_native", "key": key, "pool": "0xp",
                                                 "bridge": None, "a_key": None, "a_pool": None}
    assert lane.registry_entry({"mint": COIN, "key": key, "pool": "0xp", "priority": 0})["kind"] == "v4_native"


def test_candidates_carry_weth_and_bridged_coins_with_their_route():
    tape = _btape(_brow(APOOL, B - 2, "0xa", A_SQRT), _brow(BPOOL, B - 2, "0xb", B_SQRT, liq=B_LIQ))
    entries = fast.make_candidates(tape, 1000.0, signal_mints=[COIN], armed_mints=[], k=5)
    (entry,) = [e for e in entries if e["mint"] == COIN]          # USDG lists too, by its native pool
    assert (entry["kind"], entry["pool"], entry["bridge"], entry["a_pool"], entry["priority"]) == (
        "v4_bridge", BPOOL, USDG, APOOL, 0)
    assert lane.registry_entry(entry)["a_key"] == entry["a_key"]


# --- 6.5: safety by kind (C1, C2) ---------------------------------------------------------------

def _seam(outs, seen):
    """The lane's http seam: answers each eth_call with the next amount (0 = an empty answer)."""
    def http(method, target, headers=None, body=None, timeout=10):
        seen.append(target)
        call = json.loads(body)
        assert call["method"] == "eth_call"
        out = outs.pop(0)
        return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x" + "%064x" % out + "0" * 64}, {}
    return http


def _registry():
    tape = _btape(_brow(APOOL, B - 2, "0xa", A_SQRT), _brow(BPOOL, B - 2, "0xb", B_SQRT, liq=B_LIQ))
    entry = lane.registry_entry(fast.lane_route(tape, COIN, 1000.0))
    assert entry["kind"] == "v4_bridge"
    return {"bridge": entry}


@pytest.fixture
def funded_pool(monkeypatch):
    """A funded read pool is configured; the lane host is not in it (safety_host_refusal)."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"4663": list(FUNDED)}, raising=False)
    from sniper.execution import evm_v4_multihop
    monkeypatch.setattr(evm_v4_multihop, "_quote_path",
                        lambda *a, **k: pytest.fail("the lane must never use _quote_path"))


def test_a_bridged_sell_classifies_on_coin_to_x_on_the_lane_host_only(funded_pool):
    entry = _registry()["bridge"]
    seen, built = [], []

    def quote_fn(*args, **kw):
        built.append((args, kw))
        return {"transaction": {"to": "0xur"}}
    got = lane.route_quote(quote_fn, entry, COIN, "0xweth", 10 ** 18, host=HOST, http=_seam([777], seen))
    assert got == {"transaction": {"to": "0xur"}} and seen == [HOST]
    (_args, kw), = built
    o = kw["quote_override"]
    # USDG is currency0 of B, so the coin sells one-for-zero into X.
    assert (o["amount_out"], o["zero_for_one"], o["quote_currency"], o["pool_id"]) == (777, False, USDG, BPOOL)
    assert kw["rpc_url"] == HOST


def test_a_weth_sell_quotes_coin_to_weth(funded_pool):
    tape = _btape(_brow(WPOOL2, B - 2, "0xw", ZUP_PRE["sqrt"], liq=ZUP_PRE["liquidity"]))
    entry = lane.registry_entry(fast.lane_route(tape, COIN, 1000.0))
    seen, built = [], []
    lane.route_quote(lambda *a, **kw: built.append(kw) or {}, entry, COIN, "0xweth", 5, host=HOST,
                     http=_seam([9], seen))
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    assert built[0]["quote_override"]["quote_currency"] == CHAIN_QUOTE["4663"]["token"].lower()
    assert seen == [HOST]


def test_the_bridge_leg_quotes_the_full_path_on_the_lane_host(funded_pool):
    entry = _registry()["bridge"]
    seen = []
    assert lane.bridge_leg_ok(entry, COIN, 10 ** 18, HOST, http=_seam([5], seen)) is True
    assert lane.bridge_leg_ok(entry, COIN, 10 ** 18, HOST, http=_seam([0], seen)) is False
    assert lane.bridge_leg_ok(entry, COIN, None, HOST, http=_seam([], seen)) is False   # no amount: no call
    assert seen == [HOST, HOST]


def _routed(check, outs, seen, times):
    registry = {COIN: _registry()["bridge"]}

    def wrapped(mint, usd=None, probe_out=None):
        probe_out["amount"] = 10 ** 18            # what the safety's sell probe records on a hit
        return check(mint, usd=usd)
    return lane.routed_safety(wrapped, registry, HOST, _seam(outs, seen), path="cache", times=times)


def test_a_failed_bridge_leg_is_never_cached_as_the_coins_verdict(funded_pool):
    times, seen = lane.SafetyTimes(), []
    safety = _routed(lambda mint, usd=None: (True, None), [0], seen, times)
    cache = lane.SafetyCache(safety, ttl_s=300, usd=20.0)
    cache.note_candidates([{"mint": COIN, "priority": 0}], now=1000.0)
    assert cache.run_one(COIN, now=1000.0) is None
    assert cache.reason(COIN, now=1000.0) == "no_safety_verdict" and cache.counts["verdict_errors"] == 1
    assert [r[2:4] + r[5:] for r in times._rows] == [("v4_bridge", "cache", "bridge_leg_failed")]


def test_a_coin_that_fails_coin_to_x_is_the_coins_verdict_and_skips_the_leg(funded_pool):
    times, seen = lane.SafetyTimes(), []
    safety = _routed(lambda mint, usd=None: (False, "sell simulation reverted"), [], seen, times)
    cache = lane.SafetyCache(safety, ttl_s=300, usd=20.0)
    cache.run_one(COIN, now=1000.0)
    assert cache.reason(COIN, now=1000.0) == "unsafe:sell simulation reverted" and seen == []
    ok_times, ok_seen = lane.SafetyTimes(), []
    assert _routed(lambda mint, usd=None: (True, None), [3], ok_seen, ok_times)(COIN) == (True, None)
    assert ok_seen == [HOST] and ok_times._rows[0][5] == "ok"


def test_inline_names_a_failed_bridge_leg_as_the_route():
    live = lane.LiveLane.__new__(lane.LiveLane)

    def boom(mint, usd=None):
        raise lane.BridgeLegFailed(mint)

    class Cache:
        usd = 20.0
    live.inline_safety_fn, live.inline_deadline_s, live.cache, live.inline_host = boom, 2.0, Cache(), None
    assert live._inline(COIN, {}) == "bridge_leg_failed"


# --- 6.6: measurement (C3) -----------------------------------------------------------------------

def test_verdict_times_flush_once_into_the_lane_db(tmp_path):
    times = lane.SafetyTimes()
    times.note(COIN, "v4_bridge", "cache", 812.5, "ok")
    times.note(COIN, "v4_native", "inline", 140.25, "unsafe")
    db = str(tmp_path / "lane.db")
    times.flush(db)
    times.flush(db)                                              # drained: nothing twice
    rows = sqlite3.connect(db).execute("SELECT route_kind, path, wall_ms, outcome FROM "
                                       "rh_fast_lane_safety_times ORDER BY rowid").fetchall()
    assert rows == [("v4_bridge", "cache", 812.5, "ok"), ("v4_native", "inline", 140.25, "unsafe")]


# --- the leg: the sell is built over the lane's quote with the leg's own signer ---------------------

def test_the_legs_quote_fn_builds_over_an_override_without_its_own_quote(monkeypatch):
    from sniper.services import memecoin_evm_leg as leg
    got = []
    monkeypatch.setattr(v4, "v4_direct_quote", lambda *a, **k: pytest.fail("no quote of its own"))
    monkeypatch.setattr(v4, "dex_v4_quote", lambda *a, **k: got.append(k) or {"ok": 1})
    fn = leg._v4_direct_quote_fn("4663", "acct", "0xme")
    override = {"amount_out": 7}
    assert fn(COIN, "0xweth", 5, key={}, pool_id="0xp", rpc_url=HOST, quote_override=override) == {"ok": 1}
    assert got[0]["quote_override"] is override and got[0]["acct"] == "acct" and got[0]["rpc_url"] == HOST


def test_the_routes_knob_parses_known_kinds_only(monkeypatch):
    import importlib
    from sniper.settings import memecoin as cfg
    monkeypatch.setenv("MEMECOIN_RH_FAST_ROUTES", " v4_native, v4_bridge ,v3_shared,bogus")
    try:
        assert importlib.reload(cfg).MEMECOIN_RH_FAST_ROUTES == ("v4_native", "v4_bridge", "v3_shared")
        monkeypatch.delenv("MEMECOIN_RH_FAST_ROUTES")
        assert importlib.reload(cfg).MEMECOIN_RH_FAST_ROUTES == ("v4_native",)
    finally:
        monkeypatch.delenv("MEMECOIN_RH_FAST_ROUTES", raising=False)
        importlib.reload(cfg)


# --- Gamma LOWs on a8d5ba8da --------------------------------------------------------------------

def test_a_failed_bridge_leg_waits_a_minute_and_any_other_raise_ten_seconds(monkeypatch):
    """LOW1: a known-bad route is not re-checked every 10 s on the paced host."""
    from types import SimpleNamespace

    def bridge(mint, usd=None):
        clock[0] = 103.0
        raise lane.BridgeLegFailed(mint)

    def blip(mint, usd=None):
        clock[0] = 103.0
        raise TimeoutError("read timed out")
    thread_clock = lane.time.thread_time
    for fn, wait in ((bridge, 60.0), (blip, 10.0)):
        # Backoff begins after the failed work completes, not at its caller's start time.
        # Replace only this module's clock reference; do not alter Python's shared time module.
        clock = [100.0]  # Work completes at103; telemetry may read the same instant repeatedly.
        monkeypatch.setattr(lane, "time", SimpleNamespace(
            thread_time=thread_clock, monotonic=lambda: clock[0]))
        cache = lane.SafetyCache(fn, ttl_s=300, usd=20.0)
        cache.note_candidates([{"mint": COIN, "priority": 0}], now=1000.0)
        assert cache.run_one(COIN, now=1000.0) is None
        assert cache.next_due(1000.0 + wait) is None
        assert cache.next_due(1003.0 + wait - 0.5) is None
        assert cache.next_due(1003.0 + wait) == COIN


def test_a_bridge_leg_failure_while_the_host_failed_a_read_is_the_hosts():
    """LOW3: a bridge check during which the inline host failed a read is labelled the host's."""
    host = lane.PacedHttp(min_gap_s=0.0)

    def failing(mint, usd=None):
        host.transport(False)                     # a read failed on the host during this check
        raise lane.BridgeLegFailed(mint)

    class Cache:
        usd = 20.0
    live = lane.LiveLane.__new__(lane.LiveLane)
    live.inline_safety_fn, live.inline_deadline_s, live.cache, live.inline_host = failing, 2.0, Cache(), host
    assert live._inline(COIN, {}) == "inline_host_error"


@pytest.mark.parametrize("make", [_bridged, _weth])
def test_every_route_signs_the_stricter_of_its_cap_and_the_copy_cap_floor(make):
    # Codex EVM-01: the copy price cap's floor (coin atomic at the final clip) binds every kind.
    out = make()
    p = _payload(out)
    _tx, cap_min = lane.lane_buy_tx(p, out["clip_wei"], 1060, CTX["taker"])
    tx, min_out = lane.lane_buy_tx(p, out["clip_wei"], 1060, CTX["taker"], floor=cap_min + 7)
    assert min_out == cap_min + 7 and format(cap_min + 7, "x") in tx["data"]
    assert lane.lane_buy_tx(p, out["clip_wei"], 1060, CTX["taker"], floor=cap_min - 7)[1] == cap_min
