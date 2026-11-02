"""Every entry rung measures its round trip, and the best measured route wins (2026-09-06).

Spec: docs/superpowers/specs/2026-09-06-memecoin-evm-measured-entry-ladder-design.md.

The ladder took the FIRST hit in a fixed priority list, so a rung that measures nothing could beat
one that had already measured. On 4663 over 2026-08-23..09-06, 12 of 123 funded fills were already
at or below -30% on their first exact exit quote (-$571), and TEN came from the unmeasured rungs.

No network. Every quote function here is a fake.
"""
import pytest

from sniper.execution import evm_real_seams as S

CHAIN = "4663"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
COIN = "0x" + "22" * 20
TAKER = "0x" + "11" * 20
IN = 10 ** 16


@pytest.mark.parametrize('chain', ['56', '4663'])
@pytest.mark.parametrize('first_available', [False, True])
def test_safety_sell_can_finish_before_an_unanswered_higher_priority_route(monkeypatch, chain,
                                                                         first_available):
    import threading
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(S, '_rung_ladder_chains', lambda _: {chain})
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_V4_ENABLED', False)
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_V4_MULTIHOP_ENABLED', False)
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED', False)
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_FLAP_CURVE_ENABLED', False)
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_RELAY_ENABLED', False)
    release, finished = threading.Event(), threading.Event()
    def slow(*args, **kwargs):
        release.wait(0.4)
        finished.set()
        return None
    hit = _quote(1000)
    fn = S.venue_quote_with_v2_fallback(
        slow, chain=chain, quote_token=WETH, taker=TAKER, enabled=True,
        v2_quote_fn=lambda *a, **k: hit)
    try:
        assert fn(COIN, WETH, IN, first_available=first_available) is hit
        assert finished.is_set() == (not first_available)
    finally:
        release.set()


def _v4_tuple(quote):
    """The 2-tuple `(result, blind_reason)` the real v4 rung emits. Accepting only the 3-tuple let
    the one rung this change exists for through UNMEASURED while dict-shaped stubs looked fine."""
    return quote, None


def _quote(out, pool_id=None, protocol="uniswap_v4"):
    route = {"protocol": protocol}
    if pool_id:
        route["pool_id"] = pool_id
    return {"sellAmount": str(IN), "buyAmount": str(out), "route": route,
            "transaction": {"to": "0xrouter", "data": "0x"}, "issues": {"allowance": None}}


def _ladder(monkeypatch, *, v3=None, v4=None, multihop=None, relay=None, on=True,
            v4_reverse=None, v3_reverse=None, relay_reverse=None, rungs=None, is_buy=True, clock=None,
            call_kw=None):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", on, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", multihop is not None,
                        raising=False)
    # RELAY BUYS ARE GATED TWICE (evm_real_seams ~349 and ~477). Setting only the first leaves the
    # rung skipped as `relay_entry_disabled`, which reads as "relay lost" rather than "relay never
    # ran".
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", relay is not None, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENTRY_ENABLED", relay is not None, raising=False)
    monkeypatch.setattr(S, "_entry_tp_pct", lambda _c: 25.0)
    monkeypatch.setattr(S, "_rung_ladder_chains", lambda _c: {CHAIN})
    # the v4 reverse goes through the SAME pool, never through re-discovery
    monkeypatch.setattr(S, "_v4_same_pool_reverse",
                        v4_reverse or (lambda *a, **k: None))
    if multihop is not None:
        monkeypatch.setattr("sniper.execution.evm_v4_multihop.dex_v4_multihop_quote",
                            lambda *a, **k: multihop)

    def primary(s_t, b_t, amt, slippage_bps=None):
        if str(s_t).lower() == WETH:
            return v3
        return v3_reverse() if callable(v3_reverse) else v3_reverse

    def _relay(_chain, s_t, _b_t, _amt, _taker, **_kw):
        """DIRECTION MATTERS NOW. The relay rung asks twice on a buy -- once for the entry and
        once for the reverse -- so a stub that answered the same quote both ways would price the
        round trip against the coin leg and read as a total loss. Only a BUY has a reverse leg:
        on a sell the entry itself sells the coin, so selling-coin alone cannot identify it."""
        if is_buy and str(s_t).lower() == COIN:
            return relay_reverse() if callable(relay_reverse) else relay_reverse
        return relay

    fn = S.venue_quote_with_v2_fallback(
        primary, chain=CHAIN, quote_token=WETH, taker=TAKER,
        v2_quote_fn=lambda *a, **k: None,
        # ALWAYS INJECT A CALLABLE. Passing None makes the ladder fall back to the REAL quoter,
        # and v4 discovery then reaches GeckoTerminal from a unit test.
        v4_quote_fn=lambda *a, **k: v4(*a, **k) if callable(v4) else v4,
        relay_quote_fn=_relay,
        acct=object(), rpc_url="https://rpc", clock=clock,
        on_rung=(rungs.append if rungs is not None else None))
    sell, buy = (WETH, COIN) if is_buy else (COIN, WETH)
    return fn(sell, buy, IN, slippage_bps=100, **(call_kw or {}))


# a. an over-budget hit is refused, and the reason names the cost
def test_a_v4_hit_whose_reverse_loses_40_percent_is_refused(monkeypatch):
    rungs = []
    out = _ladder(monkeypatch, v4=_quote(1000, pool_id="0xdecoy"),
                  v4_reverse=lambda *a, **k: int(IN * 0.60), rungs=rungs)

    assert out is None
    reasons = [r.get("reason") for r in rungs if r.get("reason")]
    assert any(str(r).startswith("round_trip_over_budget:") for r in reasons), rungs


# b. the best measured round trip wins, not the priority order
@pytest.mark.parametrize('call_kw', [{}, {'first_available': True}])
def test_the_cheaper_multihop_beats_the_earlier_v4(monkeypatch, call_kw):
    """BUDDY's shape: v4 hits a decoy and multihop hits the real pool; priority alone chose v4."""
    rungs = []
    multihop = _quote(1000, protocol="uniswap_v4_multihop")
    multihop["route"]["round_trip_pct"] = -3.0
    out = _ladder(monkeypatch, v4=_quote(1000, pool_id="0xdecoy"), multihop=multihop,
                  v4_reverse=lambda *a, **k: int(IN * 0.92), rungs=rungs, call_kw=call_kw)

    assert out is multihop
    seen = {r["rung"]: r.get("round_trip_pct") for r in rungs if r.get("hit")}
    assert seen.get("v4_multihop") == -3.0
    assert seen.get("v4") is not None and seen["v4"] < -3.0     # both round trips in the trace


# c. unmeasured never outranks measured, and alone it is refused
def test_an_unmeasurable_v4_loses_to_a_measured_hit(monkeypatch):
    multihop = _quote(1000, protocol="uniswap_v4_multihop")
    multihop["route"]["round_trip_pct"] = -5.0

    def _raises(*_a, **_k):
        raise RuntimeError("reverse quote unavailable")
    out = _ladder(monkeypatch, v4=_quote(1000, pool_id="0xp"), multihop=multihop,
                  v4_reverse=_raises)

    assert out is multihop


def test_an_unmeasurable_v4_alone_is_refused_and_names_itself(monkeypatch):
    rungs = []
    out = _ladder(monkeypatch, v4=_quote(1000, pool_id="0xp"),
                  v4_reverse=lambda *a, **k: None, rungs=rungs)

    assert out is None
    assert any(str(r.get("reason") or "").startswith("round_trip_unmeasured:v4") for r in rungs)


# d. exits are never measured and never refused
def test_an_exit_is_never_measured_or_refused(monkeypatch):
    """The alternative to an expensive exit is a bag we cannot sell at all."""
    rungs = []
    exit_quote = _quote(1000, pool_id="0xp")
    out = _ladder(monkeypatch, v4=exit_quote, v4_reverse=lambda *a, **k: 1,
                  rungs=rungs, is_buy=False)

    assert out is exit_quote
    assert not any("round_trip" in str(r.get("reason") or "") for r in rungs)


# e. the switch restores the old ladder exactly
def test_the_switch_off_restores_first_hit_by_priority(monkeypatch):
    """v4 sits before v4_multihop in the priority list, so it wins on order alone."""
    multihop = _quote(9999, protocol="uniswap_v4_multihop")
    multihop["route"]["round_trip_pct"] = -1.0
    v4 = _quote(1000, pool_id="0xdecoy")

    out = _ladder(monkeypatch, v4=v4, multihop=multihop,
                  v4_reverse=lambda *a, **k: int(IN * 0.10), on=False)

    assert out is v4          # measured or not, priority decides when the switch is off


# f. a missing Relay reverse is refused, like every other measured entry rung
def test_a_relay_only_hit_without_a_reverse_is_refused(monkeypatch):
    relay = _quote(1000, protocol="relay")
    rungs = []
    out = _ladder(monkeypatch, relay=relay, rungs=rungs)
    assert out is None
    assert any(r.get("reason") == "round_trip_unmeasured:relay" for r in rungs)


# g. RELAY IS MEASURED NOW. It was the one rung the cap never read, and a relay buy was admitted
# with `round_trip_unmeasured=True` no matter what the exit would have cost (2026-09-22).
def test_a_relay_hit_with_a_healthy_reverse_is_measured_not_stamped(monkeypatch):
    relay = _quote(1000, protocol="relay")

    out = _ladder(monkeypatch, relay=relay, relay_reverse=_quote(int(IN * 0.97), protocol="relay"))

    assert out is relay
    assert "round_trip_unmeasured" not in out["route"]
    assert out["route"]["round_trip_measured_by"] == "relay"
    assert out["route"]["round_trip_pct"] == pytest.approx(-3.0, abs=0.01)


# h. THE POINT OF THE BUILD. A relay buy that cannot pay for its own exit is refused by the same
# per-chain budget every other rung answers to, instead of filling because nothing else hit.
def test_a_relay_hit_whose_reverse_loses_40_percent_is_refused(monkeypatch):
    rungs = []
    out = _ladder(monkeypatch, relay=_quote(1000, protocol="relay"),
                  relay_reverse=_quote(int(IN * 0.60), protocol="relay"), rungs=rungs)

    assert out is None
    reasons = [r.get("reason") for r in rungs if r.get("reason")]
    assert any(str(r).startswith("round_trip_over_budget:") for r in reasons), rungs


@pytest.mark.parametrize("on", [True, False])
def test_relay_cannot_buy_without_a_measured_reverse_even_in_priority_mode(monkeypatch, on):
    """RIZOWEEN 71056: Relay's absent reverse admitted a bag worth under 8% of its cost."""
    rungs = []
    out = _ladder(monkeypatch, relay=_quote(1000, protocol="relay"), on=on, rungs=rungs)
    assert out is None
    assert any(str(r.get("reason")).startswith("round_trip_unmeasured:relay") for r in rungs), rungs


def test_unmeasured_relay_cannot_override_a_measured_99_percent_loss(monkeypatch):
    rungs = []
    out = _ladder(monkeypatch, v4=_quote(1000, pool_id="0xdecoy"),
                  v4_reverse=lambda *a, **k: int(IN * .0001),
                  relay=_quote(1000, protocol="relay"), rungs=rungs)
    assert out is None
    assert any(str(r.get("reason")).startswith("round_trip_over_budget:") for r in rungs)


def test_unmeasured_relay_is_not_a_preflight_alternate(monkeypatch):
    out = _ladder(monkeypatch, v4=_quote(1000, pool_id="0xhealthy"),
                  v4_reverse=lambda *a, **k: int(IN * .97),
                  relay=_quote(1000, protocol="relay"))
    assert out is not None and out["_sniper_winner"] == "v4"
    assert not out.get("_alternates")


def test_disabling_measurement_cannot_add_a_relay_call_or_admit_a_blind_buy(monkeypatch):
    calls = []
    out = _ladder(monkeypatch, relay=_quote(1000, protocol="relay"), on=False,
                  relay_reverse=lambda: calls.append("reverse"))
    assert out is None and calls == []


def test_relay_refresh_refuses_when_its_reverse_disappears(monkeypatch):
    replies = iter([_quote(int(IN * .97), protocol="relay"), None])
    out = _ladder(monkeypatch, relay=_quote(1000, protocol="relay"),
                  relay_reverse=lambda: next(replies))
    assert out is not None and out["_sniper_winner"] == "relay"
    assert out["_requote"]() is None


# i. an unavailable reverse cannot prove an entry safe
def test_a_relay_reverse_that_raises_refuses_the_entry(monkeypatch):
    def _boom():
        raise RuntimeError("relay api down")

    relay = _quote(1000, protocol="relay")
    rungs = []
    out = _ladder(monkeypatch, relay=relay, relay_reverse=_boom, rungs=rungs)
    assert out is None
    assert any(r.get("reason") == "round_trip_unmeasured:relay" for r in rungs)


# k. THE REVERSE MUST NOT COST THE ENTRY. Relay's HTTP timeout is 12s against an 8s ladder
# deadline. A slow entry skips the reverse and refuses, without extending that deadline.
def test_a_slow_relay_entry_skips_the_reverse_and_refuses(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LADDER_WAIT_S", 8.0, raising=False)
    calls = []

    def _reverse():
        calls.append(1)
        return _quote(int(IN * 0.97), protocol="relay")

    # A MONOTONIC stub, not a fixed sequence: the ladder also calls the clock for rung timing, so
    # any consumed number must still leave the entry call looking slower than the 4s budget.
    import itertools
    ticks = itertools.count(0.0, 5.0)
    relay = _quote(1000, protocol="relay")
    rungs = []
    out = _ladder(monkeypatch, relay=relay, relay_reverse=_reverse, clock=lambda: next(ticks),
                  rungs=rungs)
    assert out is None
    assert calls == [], "no reverse call once the deadline budget is gone"
    assert any(r.get("reason") == "round_trip_unmeasured:relay:relay_reverse_no_deadline_budget"
               for r in rungs)


# j. the sell direction never pays for a reverse quote -- the round trip is an ENTRY question
def test_a_relay_sell_is_not_measured(monkeypatch):
    calls = []

    def _count():
        calls.append(1)
        return _quote(int(IN * 0.97), protocol="relay")

    relay = _quote(1000, protocol="relay")
    out = _ladder(monkeypatch, relay=relay, relay_reverse=_count, is_buy=False)

    assert out is relay
    assert calls == [], "a sell must not spend an extra relay call on a round trip"
    assert "round_trip_pct" not in out["route"]


def test_a_rung_that_returns_a_module_level_dict_is_not_mutated(monkeypatch):
    """The ladder annotates what it RETURNS, never the object the rung handed it.

    Several fixtures across the suite return a module-level quote dict. Stamping the round trip
    onto that object carried one run's measurement into the next test in the same process.
    """
    shared = _quote(1000, pool_id="0xp")
    before = {k: v for k, v in shared["route"].items()}

    out = _ladder(monkeypatch, v4=shared, v4_reverse=lambda *a, **k: int(IN * 0.97))

    assert out is not shared
    assert shared["route"] == before               # the rung's own object is untouched
    assert out["route"]["round_trip_pct"] is not None   # and the annotation is on what we return


# ── the V2 cost measurement uses a tax ALLOWANCE, not the exit's 15% floor (2026-09-13) ──

def test_a_v2_route_is_measured_with_the_tax_allowance_not_the_exit_floor(monkeypatch):
    """红尘飞 (7ba1dc76): every BSC v2 cost refusal read -16..-20% because the sell-back was shaved
    by MEMECOIN_EVM_MAX_TAX_PCT (15%) and counted as cost. Real taxes were 0-2% a side."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ROUNDTRIP_TAX_ALLOWANCE_PCT", 4.0, raising=False)
    monkeypatch.setattr(S, "_entry_tp_pct", lambda _c: 25.0)
    monkeypatch.setattr(S, "_rung_ladder_chains", lambda _c: {CHAIN})
    seen = []

    def v2(chain_, sell, buy, amount, taker, *, http=None, slippage_bps=None, tax_buffer_bps=None,
           **_k):
        if str(sell).lower() == WETH:                        # the buy
            return _quote(1000, protocol="pancake_v2")
        seen.append(tax_buffer_bps)                          # the cost measurement's sell-back
        # the real pool returns 98% of the input before any buffer; the buffer shaves the input
        return {"buyAmount": str(int(IN * 0.98 * (10_000 - int(tax_buffer_bps or 0)) / 10_000)),
                "transaction": {"to": "0xrouter"}}

    rungs = []
    fn = S.venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain=CHAIN, quote_token=WETH, taker=TAKER, v2_quote_fn=v2,
        v4_quote_fn=lambda *a, **k: None, relay_quote_fn=lambda *a, **k: None,
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append)
    out = fn(WETH, COIN, IN, slippage_bps=100)
    assert seen and seen[0] == 400, "the measurement asks for the 4% allowance, not 1500"
    assert out is not None, rungs                            # ~-5.9% fits the 10% budget
    assert -7.0 < out["route"]["round_trip_pct"] < -5.0


@pytest.mark.parametrize("margin,code", [(10.0, "round_trip_over_budget_marginal"),
                                         (0.0, "round_trip_over_budget")])
def test_a_v2_miss_inside_the_retry_margin_is_marginal_like_multihop(monkeypatch, margin, code):
    """Rizo 8fd42cb4 (chain 56, 2026-09-22): v2 read -10.05% against the 10% budget and the miss
    was one-shot, because only the multihop rung named a marginal miss. The measured cost is two
    impacts read off one untouched pool and moves with it; the same margin must apply to every
    measured rung, so the retry lane can re-ask the same class."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_BUDGET_RETRY_MARGIN_PCT", margin,
                        raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ROUNDTRIP_TAX_ALLOWANCE_PCT", 4.0, raising=False)
    monkeypatch.setattr(S, "_entry_tp_pct", lambda _c: 25.0)
    monkeypatch.setattr(S, "_rung_ladder_chains", lambda _c: {CHAIN})

    def v2(chain_, sell, buy, amount, taker, **_k):
        if str(sell).lower() == WETH:
            return _quote(1000, protocol="pancake_v2")
        return {"buyAmount": str(int(IN * 0.8995)), "transaction": {"to": "0xrouter"}}

    rungs = []
    fn = S.venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain=CHAIN, quote_token=WETH, taker=TAKER, v2_quote_fn=v2,
        v4_quote_fn=lambda *a, **k: None, relay_quote_fn=lambda *a, **k: None,
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append)
    assert fn(WETH, COIN, IN, slippage_bps=100) is None
    reasons = [str(r.get("reason") or "") for r in rungs if r.get("rung") == "v2"]
    assert reasons and reasons[0].startswith(code + ":-10.0"), rungs
    assert S.classify_quote_miss(rungs)[0] == code.replace("round_trip", "route")


# h. the runner-up hits ride with the winner, best round trip first (MUSEFUND / AGRIPPA 2026-09-22)
def test_the_winner_carries_the_runner_up_hits_for_a_preflight_fallback(monkeypatch):
    multihop = _quote(1000, protocol="uniswap_v4_multihop")
    multihop["route"]["round_trip_pct"] = -3.0
    relay = _quote(990, protocol="relay_same_chain")
    out = _ladder(monkeypatch, v4=_quote(1000, pool_id="0xp"), multihop=multihop, relay=relay,
                  v4_reverse=lambda *a, **k: int(IN * 0.92),
                  relay_reverse=_quote(int(IN * 0.99), protocol="relay"))

    # the measured ladder picks the best round trip: relay at -1% beats multihop at -3% and v4 at -8%
    assert out is relay and out["_sniper_winner"] == "relay"
    alternates = out["_alternates"]
    assert [a["rung"] for a in alternates] == ["v4_multihop", "v4"]
    assert alternates[0]["quote"] is multihop
    assert all("_alternates" not in a["quote"] for a in alternates)


def test_an_instant_relay_answer_never_reads_as_blind(monkeypatch):
    """The ladder assigned `ladder_deadline_s` AFTER submitting the rung threads, and
    `_quote_relay` reads it on its own thread to bound the reverse (E4). A relay forward that
    answered first raised NameError, recorded as blind rpc_transport, and the win went to
    multihop -- the 1-in-5 flake of 2026-09-23 that refused a deploy. With instant stubs the
    race fired 30 times in 300 before the fix; 100 runs here must all pick relay."""
    for _ in range(100):
        rungs = []
        multihop = _quote(1000, protocol="uniswap_v4_multihop")
        multihop["route"]["round_trip_pct"] = -3.0
        relay = _quote(990, protocol="relay_same_chain")
        out = _ladder(monkeypatch, v4=_quote(1000, pool_id="0xp"), multihop=multihop, relay=relay,
                      v4_reverse=lambda *a, **k: int(IN * 0.92),
                      relay_reverse=_quote(int(IN * 0.99), protocol="relay"), rungs=rungs)
        relay_rung = next(r for r in rungs if r["rung"] == "relay")
        assert relay_rung["blind_reason"] is None, relay_rung
        assert out is relay


def test_a_sell_carries_no_alternates(monkeypatch):
    out = _ladder(monkeypatch, v4=_quote(1000, pool_id="0xp"), is_buy=False)
    assert out is not None and "_alternates" not in out


# i2. E5, 2026-09-23: an aggregator that could not be READ says nothing about the token.
def test_a_kyber_transport_failure_is_blind_not_a_missing_route(monkeypatch):
    """`dex_kyber_quote` swallowed every transport fault and returned a bare None, so the rung
    recorded `blind_reason=None` and `classify_quote_miss` published `no_route` -- a verdict on
    the coin, for a read we never made. The ladder's `_run` has always classified a RAISED
    exception; this rung simply opted out of that contract."""
    from sniper.execution import evm_kyber_quote as K
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_CHAINS", (CHAIN,), raising=False)

    def fail(*_a, **_k):
        raise TimeoutError("offline injected timeout")
    monkeypatch.setattr(K, "_routes", fail)
    rungs = []
    assert _ladder(monkeypatch, rungs=rungs) is None
    kyber = next(r for r in rungs if r["rung"] == "kyber")
    assert kyber["blind_reason"] == "timeout", kyber
    # SCOPED TO THE KYBER RUNG ON PURPOSE. `classify_quote_miss` reports the FIRST blind rung, and
    # the stubbed v4 rung above it is legitimately blind whenever the shared GeckoTerminal cooldown
    # (`cooldown_active(funded=True)`) is active -- which another test in the same worker can trip.
    # Cross-rung ranking is not what this finding changed; kyber's own typed reason is.
    code, detail = S.classify_quote_miss([kyber])
    assert code == "provider_timeout", (code, detail)
    assert "kyber could not read" in detail, detail


def test_every_kyber_blind_reason_is_in_the_quote_miss_map(monkeypatch):
    """A code missing from QUOTE_MISS_FAILURES silently becomes MALFORMED_RESPONSE, which is how a
    renamed label turns a transport fault into a claim about the token. Pin the whole table."""
    from sniper.execution import evm_kyber_quote as K
    for failure_type in ("rate_limit", "rpc_transport", "timeout", "malformed_response"):
        code, _detail = S.classify_quote_miss(
            [{"rung": "kyber", "hit": False, "blind_reason": failure_type}])
        assert code in S.QUOTE_MISS_FAILURES, (failure_type, code)
    assert K.KyberQuoteUnavailable("timeout").failure_type == "timeout"


def test_a_kyber_reverse_that_raises_never_makes_the_entry_rung_blind(monkeypatch):
    """The raise must not travel out through the round-trip measurement. `_round_trip_for_hit`
    already catches everything a reverse leg throws, so an unreadable exit leg refuses the hit as
    `round_trip_unmeasured` -- the rung PRICED the entry, so it is not blind, and the typed
    failure must not leak into `blind_reason` and claim otherwise."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_CHAINS", (CHAIN,), raising=False)
    kyber = _quote(1000, protocol="kyberswap")

    def fake_quote(chain, s_t, b_t, amt, taker, **kw):
        if str(s_t).lower() == WETH:
            return kyber
        raise TimeoutError("the exit leg could not be read")
    monkeypatch.setattr("sniper.execution.evm_kyber_quote.dex_kyber_quote", fake_quote)
    monkeypatch.setattr("sniper.execution.evm_kyber_quote.dex_kyber_exit_quote", fake_quote)
    rungs = []
    _ladder(monkeypatch, v4=None, rungs=rungs)
    kyber_rung = next(r for r in rungs if r["rung"] == "kyber")
    assert kyber_rung["blind_reason"] is None, kyber_rung
    assert str(kyber_rung.get("reason") or "").startswith(S.UNMEASURED), kyber_rung


def test_a_kyber_sell_uses_the_exit_gas_quote(monkeypatch):
    """The funded sell ladder must use the exit quoter, not just its reverse measurement."""
    from sniper.execution import evm_kyber_quote as K
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_CHAINS", (CHAIN,), raising=False)
    regular = _quote(1000, protocol="kyberswap")
    regular["transaction"]["gas"] = "1035187"
    exit_quote = _quote(1000, protocol="kyberswap")
    exit_quote["transaction"]["gas"] = "3000000"
    monkeypatch.setattr(K, "dex_kyber_quote", lambda *a, **k: regular)
    monkeypatch.setattr(K, "dex_kyber_exit_quote", lambda *a, **k: exit_quote)
    out = _ladder(monkeypatch, v3=None, v4=None, is_buy=False)
    assert out["transaction"]["gas"] == "3000000"


# i. the Kyber aggregator rung: off by default (named as skipped), measured like a native rung when on
def test_kyber_rung_is_skipped_by_default_and_measured_when_enabled(monkeypatch):
    from sniper.settings import memecoin as cfg
    rungs = []
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", False, raising=False)
    _ladder(monkeypatch, v4=None, rungs=rungs)
    assert any(r["rung"] == "kyber" and r.get("skipped") for r in rungs), rungs

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_CHAINS", (CHAIN,), raising=False)
    kyber = _quote(1000, protocol="kyberswap")
    calls = []

    def fake_quote(chain, s_t, b_t, amt, taker, **kw):
        calls.append((str(s_t).lower(), str(b_t).lower()))
        return kyber if str(s_t).lower() == WETH else _quote(int(IN * 0.97), protocol="kyberswap")
    monkeypatch.setattr("sniper.execution.evm_kyber_quote.dex_kyber_quote", fake_quote)
    monkeypatch.setattr("sniper.execution.evm_kyber_quote.dex_kyber_exit_quote", fake_quote)
    rungs = []
    out = _ladder(monkeypatch, v4=None, rungs=rungs)
    assert out["route"]["protocol"] == "kyberswap" and out["buyAmount"] == "1000"   # _measured returns a copy
    hit = next(r for r in rungs if r["rung"] == "kyber" and r.get("hit"))
    assert hit["round_trip_pct"] == pytest.approx(-3.0, abs=0.01)
    assert calls == [(WETH, COIN), (COIN, WETH)]           # the entry, then its own exit leg


# ---------------------------------------------------------------------------------------------
# 2026-09-22: the winner carries its own refresh. `row` 0xb30fcc1d passed its preflight on state
# the chain had already left and the tx reverted two seconds later, 115,763 gas for nothing. Part
# of that staleness is ours: this ladder takes 1.8-3 s and the minimum is computed at the top of
# it. A caller about to simulate re-asks the rung that WON, not the whole ladder.

def test_the_winner_carries_a_requote_and_an_observed_at(monkeypatch):
    """`_measured` COPIES the quote on a buy, so the ladder's return is not the rung's own object.
    Assert on value, never on identity, for anything that goes through the measured path."""
    v4 = _quote(1000, pool_id="0xp")
    out = _ladder(monkeypatch, v4=v4, v4_reverse=lambda *a, **k: int(IN * 0.98))
    assert out is not None and out["buyAmount"] == v4["buyAmount"]
    assert callable(out["_requote"])
    assert out["_sniper_winner"] == "v4", "the refresh reuses the fall-through block's winner"
    # E2, 2026-09-23: this asserted a float in `_sniper_quote_observed_at`, which was the defect --
    # three consumers parse that key as ISO UTC. The staleness clock the refresh needs now has its
    # own field, and the assertion moved with it.
    assert isinstance(out["_sniper_quote_monotonic_at"], float)


def test_the_requote_runs_ONLY_the_winning_rung(monkeypatch):
    """THE PROPERTY THIS BLOCK EXISTS FOR. Re-running every venue to refresh one quote costs the
    seconds that made the quote stale in the first place."""
    rungs = []
    out = _ladder(monkeypatch, v4=_quote(1000, pool_id="0xp"), v3=_quote(900),
                  v4_reverse=lambda *a, **k: int(IN * 0.98), rungs=rungs)
    assert out["_sniper_winner"] == "v4"
    rungs.clear()
    again = out["_requote"]()
    assert again is not None, "the winner must still quote on a refresh"
    assert {r["rung"] for r in rungs} == {"v4"}, rungs


def test_the_requote_can_ask_for_a_different_slippage(monkeypatch):
    """The min-out retry re-asks the winner at the retry slippage instead of re-running a ladder."""
    out = _ladder(monkeypatch, v4=_quote(1000, pool_id="0xp"),
                  v4_reverse=lambda *a, **k: int(IN * 0.98))
    assert out["_requote"](slippage_bps=800) is not None


def test_an_exit_refreshes_only_its_winning_rung(monkeypatch):
    q = _quote(1000, pool_id="0xp")
    rungs = []
    out = _ladder(monkeypatch, v4=q, is_buy=False, rungs=rungs)
    assert out is q and callable(out["_requote"])
    rungs.clear()
    assert out["_requote"](slippage_bps=1000) is not None
    assert {r["rung"] for r in rungs} == {"v4"}


def test_a_refresh_of_an_unconfigured_rung_answers_nothing(monkeypatch):
    """`only_rung` must never fall through to a different venue's quote: a refresh that silently
    changes venue would invalidate the approval the executor already granted."""
    out = _ladder(monkeypatch, v4=_quote(1000, pool_id="0xp"),
                  v4_reverse=lambda *a, **k: int(IN * 0.98))
    fresh = out["_requote"]
    assert fresh.__wrapped__ is None if hasattr(fresh, "__wrapped__") else True
    # relay is not configured in this ladder; asking the combined fn for it directly answers None
    import sniper.execution.evm_real_seams as S2
    assert S2 is S


# ── the wait after the first measured hit is bounded (2026-09-23, Ztach 435622f2) ──
#
# launchpad_curve answered in 3.06 s and the ladder then waited 7.37 s for kyber, v4 and relay; the
# route quote it finally compared against the source was 1.65x. A curve hit ends the wait at once;
# any other measured hit opens MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S for the slower rungs. Real
# threads and a real clock here, because the wait IS the subject: every sleep is short.

def _timed_ladder(monkeypatch, *, v4_fn, relay_fn=None, launchpad_fn=None, grace_s=0.3,
                  rungs=None, wait_s=8.0):
    import time as _t
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", relay_fn is not None, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENTRY_ENABLED", relay_fn is not None, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED", launchpad_fn is not None,
                        raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S", grace_s, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LADDER_WAIT_S", wait_s, raising=False)
    monkeypatch.setattr(S, "_entry_tp_pct", lambda _c: 25.0)
    monkeypatch.setattr(S, "_rung_ladder_chains", lambda _c: {CHAIN})
    monkeypatch.setattr(S, "_v4_same_pool_reverse", lambda *a, **k: int(IN * 0.97))
    if launchpad_fn is not None:
        monkeypatch.setattr("sniper.execution.evm_launchpad_curve.dex_launchpad_curve_quote",
                            launchpad_fn)
        monkeypatch.setattr("sniper.execution.evm_launchpad_curve.dex_launchpad_curve_exit_quote",
                            lambda *a, **k: _quote(int(IN * 0.98), protocol="launchpad_curve"))
    fn = S.venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain=CHAIN, quote_token=WETH, taker=TAKER,
        v2_quote_fn=lambda *a, **k: None, v4_quote_fn=v4_fn,
        relay_quote_fn=relay_fn or (lambda *a, **k: None),
        acct=object(), rpc_url="https://rpc",
        on_rung=(rungs.append if rungs is not None else None))
    started = _t.perf_counter()
    out = fn(WETH, COIN, IN, slippage_bps=100)
    return out, _t.perf_counter() - started


# k. E2, 2026-09-23: one key held two representations, so every consumer was right half the time.
def test_the_ladder_stamps_a_process_local_age_and_leaves_the_instant_to_its_caller(monkeypatch):
    """`_sniper_quote_observed_at` carried the ladder's MONOTONIC clock, and three consumers parse
    it as ISO UTC: the wallet chase window (`_effective_chase_cap`), `quote_age_ms`, and anything
    persisted through `quote_fields`. All three failed closed on every ladder quote -- the chase
    window silently fell back to the base cap, which is money. The capacity-reuse path wrote the
    same key as an ISO string, so the staleness check failed on exactly the quotes the other three
    could read.

    The age now has its own field and the ladder writes nothing else. It must NOT produce the
    instant itself: `real_utc_now_iso` reads the clock-offset file on every call, and the entry
    quote path does not take a file read. Leaving the key absent is what lets the executor's
    existing `or recorded_quote_at` fallback bind a real ISO instant from the trace.
    """
    out = _ladder(monkeypatch, v4=_quote(1000, pool_id="0xp"),
                  v4_reverse=lambda *a, **k: int(IN * 0.97))
    assert out is not None, out
    assert isinstance(out["_sniper_quote_monotonic_at"], float)
    assert not isinstance(out.get("_sniper_quote_observed_at"), float), out.get(
        "_sniper_quote_observed_at")


def test_a_quote_the_ladder_did_not_win_carries_no_refresh_stamps(monkeypatch):
    """The stamps ride with the WINNER's `_requote`, which only a buy gets. A sell must not
    acquire a staleness clock it has no way to act on."""
    out = _ladder(monkeypatch, v4=_quote(1000, pool_id="0xp"),
                  v4_reverse=lambda *a, **k: int(IN * 0.97), is_buy=False)
    assert out is not None
    assert "_sniper_quote_monotonic_at" not in out, out


def _budget_aware_relay(reverse_delay_s, seen):
    """A relay stub whose REVERSE leg honours the timeout it is handed, the way a socket does.

    The forward leg answers at once, so the pre-check at evm_real_seams.py:822 passes and the
    reverse is the only thing that can cross the ladder deadline -- which is the whole of E4.
    What this proves offline is that the right bound is computed and passed; the enforcement in
    production is urllib's own socket timeout and is not exercised here.
    """
    import time as _t

    def relay(_chain, s_t, _b_t, _amt, _taker, **kw):
        if str(s_t).lower() != COIN:
            return _quote(1000, protocol="relay_same_chain")
        budget = kw.get("timeout")
        seen.append(budget)
        _t.sleep(reverse_delay_s if budget is None else min(float(budget), reverse_delay_s))
        if budget is not None and reverse_delay_s > float(budget):
            from sniper.execution.evm_relay_quote import RelayQuoteError
            raise RelayQuoteError("timeout")           # what urllib's own timeout produces
        return _quote(int(IN * 0.97), protocol="relay_same_chain")
    return relay


def test_a_slow_relay_reverse_is_refused_without_extending_the_deadline(monkeypatch):
    """A short deadline refuses without starting a reverse that cannot fit."""
    rungs, seen = [], []
    out, elapsed = _timed_ladder(
        monkeypatch, v4_fn=lambda *a, **k: None,
        relay_fn=_budget_aware_relay(5.0, seen), grace_s=0.0, rungs=rungs, wait_s=0.5)
    assert out is None
    assert seen == []
    assert any(r.get("reason") == "round_trip_unmeasured:relay:relay_reverse_budget_below_expected"
               for r in rungs)
    assert not any(r.get("blind_reason") == "ladder_deadline" for r in rungs), rungs
    assert elapsed < 2.0, elapsed


def test_the_reverse_is_bounded_by_what_is_left_of_the_ladder_deadline(monkeypatch):
    """The bound is the REMAINING budget, not a new constant: positive, strictly under the
    ladder's own wait, and recorded on the route so a reader can tell a tight budget from a dead
    provider.

    Run at the FULL ladder wait since 2026-09-23: below
    `MEMECOIN_EVM_RELAY_REVERSE_MIN_BUDGET_S` the reverse is not started at all, so a half-second
    ladder no longer reaches the bound this test is about. The assertion is unchanged."""
    rungs, seen = [], []
    out, _elapsed = _timed_ladder(
        monkeypatch, v4_fn=lambda *a, **k: None,
        relay_fn=_budget_aware_relay(0.05, seen), grace_s=0.0, rungs=rungs)
    assert seen and seen[0] is not None, seen
    assert 0.0 < float(seen[0]) < 8.0, seen
    assert out["route"].get("round_trip_reverse_budget_s") == pytest.approx(
        float(seen[0]), abs=1e-6), out["route"]


def test_the_reverse_is_not_started_when_the_remaining_deadline_cannot_hold_it(monkeypatch):
    """E4 recheck, 2026-09-23. `timeout=` on `urlopen` bounds each SOCKET OPERATION, not the total
    elapsed time: a connect-plus-headers phase and a body read can each finish inside the timeout
    and still cross the ladder deadline together. The reverse then loses a forward quote that had
    already answered.

    Two phases of 300 ms, each under the ~450 ms socket timeout, against a 500 ms ladder. The
    reverse is never started below the expected-cost floor; the entry is refused promptly.
    """
    import time as _t
    from sniper.execution import evm_wallet as W
    phases = []

    class _Response:
        status = 400
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            _t.sleep(0.30)                     # the body read: legal, and under the timeout
            return b'{"message":"no route"}'

    def fake_urlopen(_request, timeout):
        phases.append(timeout)
        _t.sleep(0.30)                         # connect plus headers: also under the timeout
        return _Response()

    monkeypatch.setattr(W.urllib.request, "urlopen", fake_urlopen)

    def relay(_chain, s_t, _b_t, _amt, _taker, **kw):
        if str(s_t).lower() != COIN:
            return _quote(1000, protocol="relay_same_chain")
        from sniper.execution import evm_relay_quote as R
        return R._payload(None, {}, timeout=kw["timeout"])

    rungs = []
    out, _elapsed = _timed_ladder(monkeypatch, v4_fn=lambda *a, **k: None, relay_fn=relay,
                                  grace_s=0.0, rungs=rungs, wait_s=0.5)
    assert out is None
    assert any(r.get("reason") == "round_trip_unmeasured:relay:relay_reverse_budget_below_expected"
               for r in rungs)
    assert phases == [], "the reverse must not be started below the floor: %r" % (phases,)
    assert not any(r.get("blind_reason") == "ladder_deadline" for r in rungs), rungs


def test_a_reverse_that_fits_the_budget_is_still_measured_normally(monkeypatch):
    """The bound must not turn a healthy measurement into an unmeasured one. With the full ladder
    wait the reverse answers inside its budget and the round trip is priced exactly as before."""
    rungs, seen = [], []
    out, _elapsed = _timed_ladder(
        monkeypatch, v4_fn=lambda *a, **k: None,
        relay_fn=_budget_aware_relay(0.05, seen), grace_s=0.0, rungs=rungs)
    assert out is not None and out["route"].get("round_trip_unmeasured") is not True, out["route"]
    assert out["route"]["round_trip_pct"] == pytest.approx(-3.0, abs=0.01), out["route"]
    assert seen and float(seen[0]) > 1.0, seen


def _slow_relay(delay_s, reverse_factor=0.97):
    import time as _t

    def relay(_chain, s_t, _b_t, _amt, _taker, **_kw):
        if str(s_t).lower() == COIN:                   # the reverse leg
            return _quote(int(IN * reverse_factor), protocol="relay")
        _t.sleep(delay_s)
        return _quote(1000, protocol="relay")
    return relay


def test_a_measured_hit_waits_only_the_grace_for_a_slow_rung(monkeypatch):
    rungs = []
    out, elapsed = _timed_ladder(monkeypatch, v4_fn=lambda *a, **k: _quote(1000, pool_id="0xp"),
                                 relay_fn=_slow_relay(1.5), grace_s=0.3, rungs=rungs)
    assert out is not None and out["route"]["protocol"] == "uniswap_v4"
    assert elapsed < 1.2, "the ladder waited for the slow rung past the grace: %.2fs" % elapsed
    late = [r for r in rungs if r["rung"] == "relay"]
    assert late and late[0]["blind_reason"] == "ladder_grace" and not late[0]["hit"]
    assert not any(r.get("blind_reason") == "ladder_deadline" for r in rungs)


def test_a_curve_hit_ends_the_wait_at_once(monkeypatch):
    import time as _t

    def slow_v4(*a, **k):
        _t.sleep(1.5)
        return _quote(1000, pool_id="0xp")

    rungs = []
    out, elapsed = _timed_ladder(
        monkeypatch, v4_fn=slow_v4, grace_s=5.0, rungs=rungs,
        launchpad_fn=lambda *a, **k: _quote(1000, protocol="launchpad_curve"))
    assert out is not None and out["route"]["protocol"] == "launchpad_curve"
    assert elapsed < 1.2, "a curve hit must not wait the grace: %.2fs" % elapsed
    assert [r["blind_reason"] for r in rungs if r["rung"] == "v4"] == ["ladder_grace"]


def test_a_better_hit_inside_the_grace_still_wins(monkeypatch):
    """The grace exists so BUDDY's case (a decoy pool hit first) keeps its answer: a cheaper
    measured route that lands inside the grace outranks the first hit exactly as before."""
    out, elapsed = _timed_ladder(monkeypatch, v4_fn=lambda *a, **k: _quote(1000, pool_id="0xp"),
                                 relay_fn=_slow_relay(0.2, reverse_factor=0.995), grace_s=1.5)
    assert out is not None and out["route"]["protocol"] == "relay", out["route"]
    assert out["route"]["round_trip_pct"] > -1.0
    assert elapsed < 1.2


# ── the grace per chain (2026-09-28, Alpha): MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S_<chain> ──
# One global knob could not serve Base (1.0 s worse on 0 of 15 ladders) and 4663 (1.0 s worse on
# 10 of 306) at once. Unset stays the global everywhere; a malformed value is the global, never 0.

GRACE_CHAINS = ("8453", "56", "5042", "4663")


def _no_chain_graces(monkeypatch):
    for chain in GRACE_CHAINS:
        monkeypatch.delenv("MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S_%s" % chain, raising=False)


def test_an_unset_chain_grace_is_the_global_on_every_chain(monkeypatch):
    from sniper.settings import memecoin as cfg
    _no_chain_graces(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S", 1.5)
    assert [cfg.evm_ladder_grace_after_hit_s_for(c) for c in GRACE_CHAINS] == [1.5] * 4
    assert cfg.evm_ladder_grace_after_hit_s_for(8453) == 1.5                 # an int chain id too
    monkeypatch.setenv("MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S_8453", "  ")
    assert cfg.evm_ladder_grace_after_hit_s_for("8453") == 1.5               # blank = unset


def test_a_grace_set_for_8453_changes_8453_only(monkeypatch):
    from sniper.settings import memecoin as cfg
    _no_chain_graces(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S", 1.5)
    monkeypatch.setenv("MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S_8453", "1.0")
    assert {c: cfg.evm_ladder_grace_after_hit_s_for(c) for c in GRACE_CHAINS} == {
        "8453": 1.0, "56": 1.5, "5042": 1.5, "4663": 1.5}
    monkeypatch.setenv("MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S_8453", "0")
    assert cfg.evm_ladder_grace_after_hit_s_for(8453) == 0.0                  # 0 = no grace, as the global


@pytest.mark.parametrize("raw", ["1,0", "abc", "-1", "nan", "inf"])
def test_a_malformed_chain_grace_falls_back_to_the_global_and_says_so(monkeypatch, caplog, raw):
    from sniper.settings import memecoin as cfg
    _no_chain_graces(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S", 1.5)
    monkeypatch.setattr(cfg, "_LADDER_GRACE_WARNED", set())
    monkeypatch.setenv("MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S_8453", raw)
    with caplog.at_level("WARNING", logger=cfg.__name__):
        assert cfg.evm_ladder_grace_after_hit_s_for("8453") == 1.5
        assert cfg.evm_ladder_grace_after_hit_s_for("8453") == 1.5
    assert sum("MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S_8453" in r.getMessage() for r in caplog.records) == 1


def test_the_ladder_waits_its_own_chains_grace(monkeypatch):
    """v4 hits at once (-3%); relay is better (-0.5%) and answers at 0.6 s. The global grace 1.5 s
    keeps relay. A 0.2 s grace set for this ladder's chain drops it; one set for 8453 does not."""
    _no_chain_graces(monkeypatch)
    ladder = lambda: _timed_ladder(monkeypatch, v4_fn=lambda *a, **k: _quote(1000, pool_id="0xp"),
                                   relay_fn=_slow_relay(0.6, reverse_factor=0.995), grace_s=1.5)
    monkeypatch.setenv("MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S_8453", "0.2")
    out, _elapsed = ladder()
    assert CHAIN == "4663" and out["route"]["protocol"] == "relay", out["route"]
    monkeypatch.setenv("MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S_4663", "0.2")
    out, elapsed = ladder()
    assert out["route"]["protocol"] == "uniswap_v4" and elapsed < 0.5, (out["route"], elapsed)


# ── a rung cut at the grace still says when it finished (2026-09-28, Alpha) ──
# On 8453 v4_multihop was cut at the grace on 14 of 15 ladders and the record kept only the cut
# time. The cut rung now writes ONE warning line when it really finishes, from its own thread,
# after the ladder returned; the ladder's result, timing and records do not change.

def _cut_lines(caplog, run_id):
    """This ladder's lines only: a slow rung from an earlier test can finish during this one."""
    return [r.getMessage() for r in caplog.records
            if "cut rung finished" in r.getMessage() and "run_id=%s " % run_id in r.getMessage()]


def _wait_for(pred, timeout_s=3.0):
    import time as _t
    end = _t.monotonic() + timeout_s
    while not pred() and _t.monotonic() < end:
        _t.sleep(0.02)
    return pred()


def test_a_rung_cut_at_the_grace_logs_its_real_finish_once_and_changes_nothing(monkeypatch, caplog):
    import re
    import time as _t
    rungs = []
    with caplog.at_level("WARNING", logger=S.__name__):
        out, elapsed = _timed_ladder(monkeypatch, v4_fn=lambda *a, **k: _quote(1000, pool_id="0xp"),
                                     relay_fn=_slow_relay(0.8), grace_s=0.2, rungs=rungs)
        recorded = [dict(r) for r in rungs]
        run_id = recorded[0]["run_id"]
        assert out["route"]["protocol"] == "uniswap_v4" and elapsed < 0.6, (out["route"], elapsed)
        assert _cut_lines(caplog, run_id) == []               # the ladder returned before it finished
        assert _wait_for(lambda: _cut_lines(caplog, run_id))
        _t.sleep(0.2)                                         # room for a second line, which must not come
    lines = _cut_lines(caplog, run_id)
    assert len(lines) == 1, lines
    line = lines[0]
    assert "chain=4663" in line and "rung=relay" in line and "cut=ladder_grace" in line, line
    assert "outcome=hit rt=" in line, line
    assert 750 <= int(re.search(r"finish_ms=(\d+)", line).group(1)) < 2000, line
    assert rungs == recorded                                   # no record added or changed afterwards
    relay = [r for r in recorded if r["rung"] == "relay"]
    assert relay[0]["blind_reason"] == "ladder_grace" and relay[0]["duration_ms"] < 600


def test_a_failing_cut_rung_log_never_reaches_the_rung_thread(monkeypatch, caplog):
    monkeypatch.setattr(S, "_http_summary", lambda *a, **k: 1 / 0)
    rungs = []
    with caplog.at_level("WARNING", logger=S.__name__):
        out, elapsed = _timed_ladder(monkeypatch, v4_fn=lambda *a, **k: _quote(1000, pool_id="0xp"),
                                     relay_fn=_slow_relay(0.5), grace_s=0.1, rungs=rungs)
        run_id = rungs[0]["run_id"]
        assert out["route"]["protocol"] == "uniswap_v4" and elapsed < 0.5
        assert _wait_for(lambda: any("cut rung log failed rung=relay run_id=%s" % run_id in r.getMessage()
                                     for r in caplog.records))
    assert _cut_lines(caplog, run_id) == []


# ── the dip watch's fallback ladder never runs the multihop rung (Alpha, 2026-09-30) ──
# Its discovery reads DexScreener/GeckoTerminal pool lists, which operator order 86d053ce keeps
# out of the watch. `exclude_rungs` drops a rung before it is asked, as `only_rung` keeps one.

def test_an_excluded_rung_is_never_asked_and_the_rest_decide(monkeypatch):
    rungs = []
    multihop = _quote(1000, protocol="uniswap_v4_multihop")
    multihop["route"]["round_trip_pct"] = -3.0
    v4 = _quote(1000, pool_id="0xp")
    out = _ladder(monkeypatch, v4=v4, multihop=multihop, v4_reverse=lambda *a, **k: int(IN * 0.92),
                  rungs=rungs, call_kw={"exclude_rungs": ("v4_multihop",)})
    assert out["_sniper_winner"] == "v4" and out["route"]["pool_id"] == "0xp"
    assert "v4_multihop" not in {r["rung"] for r in rungs}


def test_excluding_every_configured_rung_answers_nothing(monkeypatch):
    out = _ladder(monkeypatch, v4=_quote(1000, pool_id="0xp"),
                  v4_reverse=lambda *a, **k: int(IN * 0.98),
                  call_kw={"exclude_rungs": ("v4", "v3", "v2", "pons_v2", "launchpad_curve",
                                             "flap_curve", "kyber", "relay", "mixed_v3_v2")})
    assert out is None


@pytest.mark.parametrize("chain,first_available,want", [("1", False, 1000),
    ("4663", False, 100), ("1", True, 100)])
def test_eth_actual_exit_chooses_best_output_safety_keeps_first(monkeypatch, chain,
                                                               first_available, want):
    import sys, time
    monkeypatch.setattr(sys.modules[__name__], "CHAIN", chain)
    def better(*args, **kwargs):
        time.sleep(0.03)
        return _quote(1000)
    out = _ladder(monkeypatch, v3_reverse=_quote(100), v4=better, is_buy=False,
                  call_kw={"first_available": first_available})
    assert int(out["buyAmount"]) == want


# --- KLN (chain 1, 2026-10-08): an over-budget max-output pool falls back to the next by buy output ---
# Replayed at block 26148721: the stale 8% decoy quoted 241.7 KLN (round trip -33.51%) while the real
# hooked pool quoted 204.6 (-2.19%). The rung picked the decoy, measured it alone, and refused.

DECOY, HOOKED, TEN = "0xdecoy8", "0xhooked", "0xdecoy10"


def _v4_alts(pools):
    """The v4 builder: the max-output quote carries the other candidates as `_v4_alternates`; a
    `quote_override` rebuilds the executable quote on that one pool."""
    built = []

    def v4(chain, s_t, b_t, amt, taker, **kw):
        alt = kw.get("quote_override")
        if alt is not None:
            built.append(alt["pool_id"])
            return dict(_quote(alt["amount_out"], pool_id=alt["pool_id"]), _rebuilt=True)
        first = pools[0]
        q = _quote(first["amount_out"], pool_id=first["pool_id"])
        q["_v4_alternates"] = [dict(p, pool_key={"fee": p.get("fee", 0), "hooks": p.get("hooks", "0x0")})
                               for p in pools[1:]]
        return q
    return v4, built


def _reverse(rts):
    """The same-pool reverse: back = IN x (1 + rt) for the pool the quote names."""
    def rev(_chain, quote, _mint, _out, _rpc, http=None):
        rt = rts.get((quote.get("route") or {}).get("pool_id"))
        return None if rt is None else int(IN * (1 + rt))
    return rev


def test_kln_the_decoy_over_budget_falls_back_to_the_hooked_pool(monkeypatch):
    v4, built = _v4_alts([{"pool_id": DECOY, "amount_out": 2417, "fee": 80000},
                          {"pool_id": HOOKED, "amount_out": 2046, "fee": 0, "hooks": "0x0136fecf"},
                          {"pool_id": TEN, "amount_out": 2006, "fee": 100000}])
    rungs = []
    out = _ladder(monkeypatch, v4=v4, rungs=rungs,
                  v4_reverse=_reverse({DECOY: -0.3351, HOOKED: -0.0219, TEN: -0.2199}))
    assert out is not None and out.get("_rebuilt") and built == [HOOKED]
    assert out["route"]["pool_id"] == HOOKED and out["buyAmount"] == "2046"
    assert out["route"]["round_trip_pct"] == pytest.approx(-2.19, abs=0.01)
    [v4_rung] = [r for r in rungs if r["rung"] == "v4"]
    assert [(p["pool_id"], p["outcome"]) for p in v4_rung["pools"]] == [
        (DECOY, "over_budget"), (HOOKED, "chosen")]
    assert v4_rung["pools"][0]["rt_pct"] == pytest.approx(-33.51, abs=0.01)
    assert v4_rung["pools"][1]["fee"] == 0 and v4_rung["pools"][1]["hooks"] == "0x0136fecf"


def test_every_candidate_over_budget_refuses_exactly_as_today(monkeypatch):
    v4, built = _v4_alts([{"pool_id": DECOY, "amount_out": 2417}, {"pool_id": TEN, "amount_out": 2006}])
    rungs = []
    out = _ladder(monkeypatch, v4=v4, rungs=rungs, v4_reverse=_reverse({DECOY: -0.3351, TEN: -0.40}))
    assert out is None and built == []
    [v4_rung] = [r for r in rungs if r["rung"] == "v4"]
    assert v4_rung["reason"].startswith("round_trip_over_budget:-33.51")          # the max-output pool's
    assert [p["outcome"] for p in v4_rung["pools"]] == ["over_budget", "over_budget"]


def test_a_max_output_pool_within_budget_is_chosen_with_no_fallback(monkeypatch):
    v4, built = _v4_alts([{"pool_id": DECOY, "amount_out": 2417}, {"pool_id": HOOKED, "amount_out": 2046}])
    asked = []
    rungs = []
    rev = _reverse({DECOY: -0.05, HOOKED: -0.02})
    out = _ladder(monkeypatch, v4=v4, rungs=rungs,
                  v4_reverse=lambda *a, **k: asked.append(a[1]["route"]["pool_id"]) or rev(*a, **k))
    assert out["route"]["pool_id"] == DECOY and built == [] and asked == [DECOY]
    assert "pools" not in [r for r in rungs if r["rung"] == "v4"][0]


def test_at_most_three_alternates_are_measured_and_the_knob_turns_it_off(monkeypatch):
    from sniper.settings import memecoin as cfg
    pools = [{"pool_id": "0xp%d" % i, "amount_out": 3000 - i} for i in range(6)]
    v4, built = _v4_alts(pools)
    asked = []
    rev = _reverse({p["pool_id"]: -0.5 for p in pools})
    _ladder(monkeypatch, v4=v4, v4_reverse=lambda *a, **k: asked.append(a[1]["route"]["pool_id"]) or rev(*a, **k))
    assert asked == ["0xp0", "0xp1", "0xp2", "0xp3"]
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_BUDGET_FALLBACK_MAX", 0, raising=False)
    asked.clear()
    _ladder(monkeypatch, v4=v4, v4_reverse=lambda *a, **k: asked.append(a[1]["route"]["pool_id"]) or rev(*a, **k))
    assert asked == ["0xp0"]


def test_an_unmeasurable_or_unbuildable_alternate_is_skipped(monkeypatch):
    v4, built = _v4_alts([{"pool_id": DECOY, "amount_out": 2417}, {"pool_id": "0xblind", "amount_out": 2100},
                          {"pool_id": HOOKED, "amount_out": 2046}])
    rungs = []
    out = _ladder(monkeypatch, v4=v4, rungs=rungs, v4_reverse=_reverse({DECOY: -0.3351, HOOKED: -0.02}))
    assert out["route"]["pool_id"] == HOOKED
    assert [p["outcome"] for p in [r for r in rungs if r["rung"] == "v4"][0]["pools"]] == [
        "over_budget", "unmeasured", "chosen"]


def test_the_real_quoter_returns_its_other_candidates_by_output(monkeypatch):
    from sniper.execution import evm_v4_quote as V
    keys = {"0xa": {"currency0": V._NATIVE, "currency1": COIN, "fee": 80000, "tick_spacing": 800,
                    "hooks": "0x" + "00" * 20},
            "0xb": {"currency0": V._NATIVE, "currency1": COIN, "fee": 0, "tick_spacing": 10,
                    "hooks": "0x0136fecf49e0217bfd2d9551e8e7b05f174ba8ec"},
            "0xc": {"currency0": V._NATIVE, "currency1": COIN, "fee": 100000, "tick_spacing": 1000,
                    "hooks": "0x" + "00" * 20}}
    outs = {"0xa": 2417, "0xb": 2046, "0xc": 2006}
    monkeypatch.setattr(V, "_quote_pools_for_mint", lambda *a, **k: list(keys))
    monkeypatch.setattr(V, "_pool_key", lambda chain, pid, *a, **k: keys[pid])
    monkeypatch.setattr(V, "_quote_single", lambda chain, key, zf1, amt, *a, **k: outs[
        next(p for p, kk in keys.items() if kk is key)])
    got = V.v4_quote_amount_out("1", "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", COIN, 10 ** 16,
                                rpc_url="https://rpc")
    assert got["pool_id"] == "0xa"
    assert [(a["pool_id"], a["amount_out"]) for a in got["alternates"]] == [("0xb", 2046), ("0xc", 2006)]


def test_a_rebuild_that_fails_or_moves_is_never_chosen(monkeypatch):
    v4, built = _v4_alts([{"pool_id": DECOY, "amount_out": 2417}, {"pool_id": HOOKED, "amount_out": 2046},
                          {"pool_id": TEN, "amount_out": 2006}, {"pool_id": "0xother", "amount_out": 2000}])

    def v4_flaky(chain, s_t, b_t, amt, taker, **kw):
        alt = kw.get("quote_override")
        if alt is not None and alt["pool_id"] == HOOKED:
            built.append(HOOKED)
            return None                                               # the build failed
        if alt is not None and alt["pool_id"] == TEN:
            built.append(TEN)
            return _quote(alt["amount_out"] - 1, pool_id=TEN)          # not the measured output
        if alt is not None:
            built.append(alt["pool_id"])
            return _quote(alt["amount_out"], pool_id=DECOY)            # not the measured pool
        return v4(chain, s_t, b_t, amt, taker, **kw)
    rungs = []
    out = _ladder(monkeypatch, v4=v4_flaky, rungs=rungs,
                  v4_reverse=_reverse({DECOY: -0.3351, HOOKED: -0.02, TEN: -0.03, "0xother": -0.03}))
    assert out is None and built == [HOOKED, TEN, "0xother"]
    [v4_rung] = [r for r in rungs if r["rung"] == "v4"]
    assert v4_rung["reason"].startswith("round_trip_over_budget:-33.51")
    assert [p["outcome"] for p in v4_rung["pools"]] == ["over_budget", "rebuild_failed", "rebuild_failed",
                                                          "rebuild_failed"]
