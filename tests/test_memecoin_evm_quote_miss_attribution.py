"""Typed attribution for an empty 4663 entry quote (2026-08-07).

Four funded 4663 entries aborted with the bare reason "no quote" while GeckoTerminal was
returning 429. The V3 -> V2 -> V4 ladder collapsed every rung into one None, and the V4 rung
needs GeckoTerminal pool discovery: under the shared 429 cooldown `gt_get_json` returns None
WITHOUT an HTTP call, so a blind rung and a routeless token wrote the identical row. The stored
trace could not separate them afterwards.

These tests pin the separation:
  * the ladder records one entry per rung, including the rungs configuration turned off;
  * a V4 miss taken during the GeckoTerminal cooldown is `blind_reason='discovery_cooldown'`
    (typed `rate_limit` until 2026-09-28: our own cooldown made no HTTP call, so no provider
    refused us, and RATE_LIMIT holds an exact-quote provider group);
  * an infrastructure blindness outranks `no_route` in the verdict;
  * the executor puts the code in the terminal reason and in the reservation release reason;
  * an executor with no attribution seam keeps the old behavior exactly.

RELAY IS EXIT-ONLY ON THE BUY SIDE (2026-09-02). These ladder-mechanism tests use relay as the
low-priority rung because relay IS the lowest, and that is the coverage they exist for -- every
earlier fixture made v3 hit, so the relay-wins path had none.

They exercise the SELL direction (`fn(MINT, WETH, ...)`) rather than the buy. The mechanism under
test is direction-agnostic FOR THE RUNG-ORDER cases: `is_buy` reaches only a v2 tax-buffer
argument and the relay entry gate, never which rungs participate or in what order. Every assertion
in those is preserved and only the fixture direction moved.

RETRACTED CLAIM, left here deliberately. This note previously said `_timed_ladder` on the sell
direction serialises a rung the buy runs independently. IT DOES NOT. Beta measured both directions
with a deliberately slow rung: 404ms buy against 402ms sell, the same five rungs, both completing
in the time of the single slowest rung -- concurrency, identical either way.

My "verification" was `git stash -- <file>` on a file that was already committed and clean, so the
stash was a NO-OP and the control run still contained the change, including the `is_buy` NameError
that was live at the time. I reported the symptom of my own bug as a property of the ladder. The
concurrency cases stay on the BUY direction because relay is the lowest rung there, not because a
sell behaves differently. Do NOT "fix" these by deleting relay from the expected rung list -- that
deletes the coverage rather than the incompatibility.
"""
import sqlite3
import threading

import pytest

from sniper.execution import geckoterminal_quote as gt
from sniper.execution.evm_real_seams import classify_quote_miss, venue_quote_with_v2_fallback
from sniper.execution.evm_swap_executor import execute_entry_evm
from sniper.persistence import db_memecoin as m

WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
MINT = "0x18e674231a58c239dc7daedcffe15ec3a24cff5c"      # HOOKR, the live 2026-08-07 case
# `buyAmount` IS REQUIRED NOW (2026-09-06). The entry ladder measures a round trip by quoting the
# reverse of the bag a hit would buy, and a quote with no `buyAmount` cannot be measured at all --
# so it is refused as `round_trip_unmeasured`. These fixtures pin RUNG MECHANICS, not cost policy,
# so they carry a measurable quote and a direction-aware reverse; the contracts they assert are
# unchanged.
QUOTE = {"sellAmount": "10000000", "buyAmount": "20000000",
         "route": {"protocol": "uniswap_v4", "pool_id": "0x" + "ab" * 32},
         "transaction": {"to": "0xr"},
         "issues": {"allowance": {"spender": "0xspender"}}}


@pytest.fixture(autouse=True)
def _route_cost_is_not_what_this_file_tests():
    """Every hit measures cheap, so these fixtures pin what they are actually about.

    The entry ladder measures a round trip by quoting the reverse of the bag a hit would buy
    (2026-09-06), and an unmeasurable fixture -- no `buyAmount`, or the same amount in both
    directions -- is correctly REFUSED as `round_trip_unmeasured`. Every test in this file is about
    RUNG MECHANICS: durations, cooldowns, which http a rung uses, whether a raising rung can change
    the answer, what the probe consumes. None is about the cost of a route. Without this each would
    be asserting on a refusal it was never written to describe.

    The tests that ARE about measurement -- over budget, unmeasurable, best-measured-wins -- live in
    tests/test_memecoin_evm_measured_ladder.py and do not stub this.
    """
    import sniper.execution.evm_real_seams as _S
    original = _S._round_trip_for_hit
    _S._round_trip_for_hit = lambda *_a, **_k: (-3.0, None)
    try:
        yield
    finally:
        _S._round_trip_for_hit = original


def _measurable(quote_fn):
    """Answer the SELL side with ~97% of the clip, so a measured round trip reads as fees.

    Keyed by TOKEN PAIR rather than call order: a test that quotes twice must not have its second
    entry mistaken for the first one's reverse.
    """
    seen = {}

    def call(sell, buy, amount, *args, **kwargs):
        pair = (str(sell).lower(), str(buy).lower())
        if pair[::-1] in seen:
            return {"sellAmount": str(amount),
                    "buyAmount": str(int(seen[pair[::-1]] * 0.97)),
                    "transaction": {"to": "0xr"}, "issues": {"allowance": None}}
        seen[pair] = int(QUOTE["sellAmount"])
        return quote_fn(sell, buy, amount, *args, **kwargs)
    return call


# THE LADDER ANNOTATES WHAT IT RETURNS (2026-09-06), so a hit is no longer the same OBJECT the
# rung handed back -- it is a copy carrying `route.round_trip_pct`. The identity of a quote is its
# AMOUNTS, not its dict shape, so these assert the amounts. On the SELL side nothing is measured,
# so the rung's object is returned unchanged and `is QUOTE` still holds there.
def _is_quote(got):
    """The ladder returned this rung's quote: same amounts, annotated on entry."""
    return (got is QUOTE
            or (isinstance(got, dict)
                and got.get("sellAmount") == QUOTE["sellAmount"]
                and got.get("buyAmount") == QUOTE["buyAmount"]))


def _ladder(rungs, *, v3=None, v2=None, v4=None, chain="4663"):
    return venue_quote_with_v2_fallback(
        _measurable(lambda s, b, a, slippage_bps=None: v3),
        chain=chain, quote_token=WETH, taker="0xtaker",
        v2_quote_fn=_measurable(lambda s, b, a, *ar, **kw: v2),
        v4_quote_fn=_measurable(lambda s, b, a, *ar, **kw: v4),
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append)


def _v4_on(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True)


def _cooldown(monkeypatch, active):
    monkeypatch.setattr(gt, "cooldown_active", lambda *a, **k: active)
    monkeypatch.setattr(
        "sniper.execution.geckoterminal_quote.cooldown_active", lambda *a, **k: active)


# --- the ladder ledger --------------------------------------------------------------------

def test_v3_hit_keeps_a_full_route_trace():
    rungs = []
    assert _is_quote(_ladder(rungs, v3=QUOTE)(WETH, MINT, 10 ** 18))
    v3_rung = next(r for r in rungs if r["rung"] == "v3")
    assert {key: v3_rung[key] for key in ("rung", "hit", "skipped", "blind_reason")} == {
        "rung": "v3", "hit": True, "skipped": False, "blind_reason": None,
    }
    assert [r["rung"] for r in rungs] == [
        "launchpad_curve", "flap_curve", "v3", "v2", "v4", "v4_multihop", "mixed_v3_v2", "kyber"]
    assert all(r["duration_ms"] >= 0 for r in rungs)


def test_independent_rungs_start_before_the_priority_route_returns(monkeypatch):
    """A slow V3 result must not serialise the V2, Pons, V4, and relay route reads."""
    from sniper.settings import memecoin as cfg

    _v4_on(monkeypatch)
    _cooldown(monkeypatch, False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", True)
    # Relay is exit-only by default since 2026-09-02; these exercise it as the lowest BUY
    # rung, which is real restorable behaviour behind this knob rather than a fiction.
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENTRY_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", False)
    release = threading.Event()
    all_started = threading.Event()
    lock = threading.Lock()
    started = []

    def route(name, result):
        def quote(*_args, **_kwargs):
            with lock:
                started.append(name)
                if len(started) == 5:
                    all_started.set()
            release.wait(1)
            return result
        return quote

    v3 = {"buyAmount": "3", "transaction": {"to": "0xv3"}, "issues": {}}
    fn = venue_quote_with_v2_fallback(
        route("v3", v3), chain="4663", quote_token=WETH, taker="0xtaker",
        v2_quote_fn=route("v2", {"buyAmount": "2"}),
        pons_v2_quote_fn=route("pons_v2", {"buyAmount": "2"}),
        v4_quote_fn=route("v4", {"buyAmount": "2"}),
        relay_quote_fn=route("relay", {"buyAmount": "2"}),
        acct=object(), rpc_url="https://rpc",
    )
    result = []
    worker = threading.Thread(target=lambda: result.append(fn(WETH, MINT, 10 ** 18)))
    worker.start()
    try:
        assert all_started.wait(0.3), "the route ladder serialised an independent quote"
    finally:
        release.set()
        worker.join(1)

    # THE LADDER RETURNS A COPY carrying `route.round_trip_pct`, so identity and whole-dict
    # equality no longer hold; the identity of a quote is its amounts. With every rung measuring
    # the same cost, the tie-break is priority, so v3 still wins -- which is the claim here.
    assert result and result[0] is not None
    assert result[0]["buyAmount"] == v3["buyAmount"], \
        "parallel work must retain the established V3 priority on an equal-cost tie"


def test_priority_final_route_returns_without_waiting_for_lower_routes(monkeypatch):
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", True)
    # Relay is exit-only by default since 2026-09-02; these exercise it as the lowest BUY
    # rung, which is real restorable behaviour behind this knob rather than a fiction.
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENTRY_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", False)
    relay_started = threading.Event()
    release_relay = threading.Event()
    done = threading.Event()
    result = []

    def v3(*_args, **_kwargs):
        assert relay_started.wait(0.2)
        return QUOTE

    def relay(*_args, **_kwargs):
        relay_started.set()
        release_relay.wait(1.0)
        return {"buyAmount": "lower-priority"}

    fn = venue_quote_with_v2_fallback(
        v3, chain="4663", quote_token=WETH, taker="0xtaker",
        v2_quote_fn=lambda *_args, **_kwargs: None,
        relay_quote_fn=relay, rpc_url="https://rpc",
    )
    worker = threading.Thread(
        target=lambda: (result.append(fn(WETH, MINT, 10 ** 18)), done.set()))
    worker.start()
    try:
        # WHAT THIS TEST OWNS is that the lower rung STARTED before the priority route returned --
        # the rungs run in parallel. Whether the ladder then RETURNS early is the timing contract,
        # and it moved: on entry the measured ladder waits for every rung by design. See
        # `test_an_entry_waits_for_every_rung_while_an_exit_still_short_circuits`.
        assert relay_started.wait(0.5), "the lower rung must start without waiting for v3"
    finally:
        release_relay.set()
        worker.join(2)
        assert done.wait(2), "the ladder must finish once every rung has answered"

    assert result and result[0] is not None
    assert result[0].get("sellAmount") == QUOTE["sellAmount"]


def test_each_route_rung_records_its_own_duration(monkeypatch):
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True)
    rungs = []
    fn = venue_quote_with_v2_fallback(
        lambda *_a, **_k: None, chain="4663", quote_token=WETH, taker="0xtaker",
        v2_quote_fn=lambda *_a, **_k: QUOTE, on_rung=rungs.append,
    )

    assert _is_quote(fn(WETH, MINT, 10 ** 18))
    assert [r["rung"] for r in rungs][:4] == ["launchpad_curve", "flap_curve", "v3", "v2"]
    assert all(r["duration_ms"] >= 0 for r in rungs)


def test_every_rung_misses_records_three_clean_misses(monkeypatch):
    _v4_on(monkeypatch)
    _cooldown(monkeypatch, False)
    rungs = []
    assert _ladder(rungs)(WETH, MINT, 10 ** 18) is None
    assert [(r["rung"], r["hit"], r["skipped"], r["blind_reason"]) for r in rungs] == [
        # The launchpad-curve rung is off by default (its own flag, unrelated to this test), so
        # it records SKIPPED first, same as a disabled v4_multihop -- it is ALWAYS evaluated
        # (chain 4663), never conditioned on the fns this fixture injects.
        ("launchpad_curve", False, True, None),
        # flap_curve is gated to chain 56 (2026-09-20); on 4663 it is UNAVAILABLE, a chain-gated
        # build fact rather than a flag someone could flip -- classify_quote_miss reports it
        # separately from the "off" flags below.
        ("flap_curve", False, True, None),
        ("v3", False, False, None), ("v2", False, False, None), ("v4", False, False, None),
        # B's multi-hop rung is present but DISABLED here, so it records SKIPPED rather than a
        # miss -- a disabled rung must not appear in the no_route detail as a route we tried.
        ("v4_multihop", False, True, None),
        # The mixed V3->V2 hop (2026-09-25) has a verified SmartRouter on 56 only: UNAVAILABLE here.
        ("mixed_v3_v2", False, True, None),
        # The Kyber aggregator rung (2026-09-22) is off by default and records SKIPPED the same way.
        ("kyber", False, True, None)]
    # The detail now names the disabled rung. That is deliberate: while B ships dark, every
    # 4663 no_route says so, which makes the dark state VISIBLE instead of silent. The CODE is
    # unchanged, and refusal capture matches gate tokens inside parentheses -- `v4_multihop` is
    # not a gate, so nothing downstream re-buckets on it.
    assert classify_quote_miss(rungs) == (
        "no_route", "no route on v3/v2/v4 (launchpad_curve/v4_multihop/kyber off) "
        "(flap_curve/mixed_v3_v2 unavailable on this chain)")


def test_v4_miss_under_the_geckoterminal_cooldown_is_blind_not_routeless(monkeypatch):
    _v4_on(monkeypatch)
    _cooldown(monkeypatch, True)
    rungs = []
    assert _ladder(rungs)(WETH, MINT, 10 ** 18) is None
    # index the V4 rung by NAME, not by position: a rung appended after it (B's multi-hop) made
    # rungs[-1] point at a different rung, which is a brittleness this test had by accident.
    v4_rung = next(r for r in rungs if r["rung"] == "v4")
    assert {key: v4_rung[key] for key in ("rung", "hit", "skipped", "blind_reason")} == {
        "rung": "v4", "hit": False, "skipped": False, "blind_reason": "discovery_cooldown",
    }
    assert v4_rung["duration_ms"] >= 0
    code, detail = classify_quote_miss(rungs)
    assert code == "provider_discovery_cooldown" and "discovery_cooldown" in detail


def test_a_v4_hit_during_a_cooldown_is_never_marked_blind(monkeypatch):
    _v4_on(monkeypatch)
    _cooldown(monkeypatch, True)
    rungs = []
    assert _is_quote(_ladder(rungs, v4=QUOTE)(WETH, MINT, 10 ** 18))
    assert rungs[-1]["blind_reason"] is None
    assert classify_quote_miss(rungs) == (None, None)


def test_a_healthy_v4_lookup_clears_a_stale_cooldown_attribution(monkeypatch):
    """A prior GT cooldown must not hide a completed on-chain V4 lookup."""
    from sniper.execution import evm_v4_quote as V

    _v4_on(monkeypatch)
    _cooldown(monkeypatch, True)
    monkeypatch.setattr(V, "last_discovery_observed", lambda: True)
    rungs = []

    assert _ladder(rungs)(WETH, MINT, 10 ** 18) is None
    v4_rung = next(r for r in rungs if r["rung"] == "v4")
    assert v4_rung["blind_reason"] is None


def test_a_raising_v4_rung_is_rpc_transport_and_still_returns_none(monkeypatch):
    _v4_on(monkeypatch)
    _cooldown(monkeypatch, False)
    rungs = []
    fn = venue_quote_with_v2_fallback(
        lambda s, b, a, slippage_bps=None: None, chain="4663", quote_token=WETH,
        taker="0xtaker", v2_quote_fn=lambda *a, **k: None,
        v4_quote_fn=_raise, acct=object(), rpc_url="https://rpc", on_rung=rungs.append)
    assert fn(WETH, MINT, 10 ** 18) is None
    assert classify_quote_miss(rungs)[0] == "provider_rpc_transport"


def test_default_v4_rung_uses_the_funded_http_for_discovery(monkeypatch):
    from sniper.execution import evm_v4_quote

    _v4_on(monkeypatch)
    seen = {}
    funded_http = object()
    def default_v4(*_args, **kwargs):
        seen["gt_http"] = kwargs.get("gt_http")
        return QUOTE

    monkeypatch.setattr(evm_v4_quote, "dex_v4_quote", default_v4)
    fn = venue_quote_with_v2_fallback(
        lambda *_args, **_kwargs: None, chain="4663", quote_token=WETH,
        taker="0xtaker", http=funded_http, v2_quote_fn=lambda *_args, **_kwargs: None,
        pons_v2_quote_fn=lambda *_args, **_kwargs: None, acct=object(), rpc_url="https://rpc",
    )

    assert _is_quote(fn(WETH, MINT, 10 ** 18))
    assert seen["gt_http"] is funded_http


def test_a_raising_pons_rung_does_not_block_a_v4_quote(monkeypatch):
    _v4_on(monkeypatch)
    _cooldown(monkeypatch, False)
    rungs = []
    fn = venue_quote_with_v2_fallback(
        lambda s, b, a, slippage_bps=None: None,
        chain="4663", quote_token=WETH, taker="0xtaker",
        v2_quote_fn=lambda *a, **k: None, pons_v2_quote_fn=_raise,
        v4_quote_fn=lambda *a, **k: QUOTE, acct=object(), rpc_url="https://rpc",
        on_rung=rungs.append,
    )

    assert _is_quote(fn(WETH, MINT, 10 ** 18))
    assert sum(r["rung"] == "pons_v2" for r in rungs) == 1
    assert next(r for r in rungs if r["rung"] == "pons_v2")["blind_reason"] == "rpc_transport"
    assert next(r for r in rungs if r["rung"] == "v4")["hit"] is True


def test_relay_quotes_after_every_direct_rung_misses(monkeypatch):
    from sniper.settings import memecoin as cfg

    _v4_on(monkeypatch)
    _cooldown(monkeypatch, False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", True)
    rungs = []
    fn = venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain="4663", quote_token=WETH, taker="0xtaker",
        v2_quote_fn=lambda *a, **k: None, pons_v2_quote_fn=lambda *a, **k: None,
        v4_quote_fn=lambda *a, **k: None, relay_quote_fn=lambda *a, **k: QUOTE,
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append,
    )

    assert fn(MINT, WETH, 10**18) is QUOTE
    assert [r["rung"] for r in rungs] == [
        "launchpad_curve", "flap_curve", "v3", "v2", "pons_v2", "v4", "v4_multihop", "mixed_v3_v2",
        "kyber", "relay",
    ]
    assert rungs[-1]["hit"] is True


def test_relay_provider_fault_stays_a_typed_infrastructure_miss(monkeypatch):
    from sniper.settings import memecoin as cfg

    _v4_on(monkeypatch)
    _cooldown(monkeypatch, False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", True)
    error = RuntimeError("bad response")
    error.failure_type = "malformed_response"
    rungs = []
    fn = venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain="4663", quote_token=WETH, taker="0xtaker",
        v2_quote_fn=lambda *a, **k: None, v4_quote_fn=lambda *a, **k: None,
        relay_quote_fn=lambda *a, **k: (_ for _ in ()).throw(error),
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append,
    )

    assert fn(MINT, WETH, 10**18) is None
    assert classify_quote_miss(rungs)[0] == "provider_malformed_response"


def _raise(*_a, **_k):
    raise RuntimeError("node refused")


def test_disabled_rungs_are_recorded_as_skipped_not_as_a_miss(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", False)
    rungs = []
    assert _ladder(rungs)(WETH, MINT, 10 ** 18) is None
    assert [r["rung"] for r in rungs if r["skipped"]] == [
        "launchpad_curve", "flap_curve", "v4", "mixed_v3_v2", "kyber"]
    assert classify_quote_miss(rungs) == (
        "no_route", "no route on v3/v2 (launchpad_curve/v4/kyber off) "
        "(flap_curve/mixed_v3_v2 unavailable on this chain)")


def test_a_raising_on_rung_can_never_change_the_quote():
    def boom(_record):
        raise RuntimeError("instrumentation is not allowed to decide money")
    fn = venue_quote_with_v2_fallback(
        lambda s, b, a, slippage_bps=None: QUOTE, chain="4663", quote_token=WETH,
        taker="0xtaker", on_rung=boom)
    assert _is_quote(fn(WETH, MINT, 10 ** 18))


def test_an_empty_ledger_is_unattributed_never_no_route():
    assert classify_quote_miss([])[0] == "quote_unattributed"


def test_relay_capacity_miss_is_not_a_provider_fault():
    rungs = [{"rung": "relay", "hit": False, "skipped": False,
              "blind_reason": "route_capacity"}]

    assert classify_quote_miss(rungs) == (
        "route_capacity", "relay rejected this clip for route capacity")


def test_executor_entry_probe_reports_relay_route_capacity(monkeypatch):
    from eth_account import Account

    from sniper.execution import evm_real_seams as seams
    from sniper.execution import evm_relay_quote
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", True)
    # Relay is exit-only by default since 2026-09-02; these exercise it as the lowest BUY
    # rung, which is real restorable behaviour behind this knob rather than a fiction.
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENTRY_ENABLED", True)

    def capacity(*_args, **_kwargs):
        raise evm_relay_quote.RelayQuoteError("route_capacity")

    monkeypatch.setattr(evm_relay_quote, "dex_relay_quote", capacity)
    ex = seams.build_real_evm_executor(
        acct=Account.from_key("0x" + "42" * 32), chain_id="4663",
        rpc_url="http://rpc", venue_quote_fn=lambda *a, **k: None,
        price_fn=lambda: 2500.0, usdc_fn=lambda: 100.0,
        token_fn=lambda _mint: (0.0, 18),
    )

    assert ex["entry_quote_probe_fn"](MINT, 31.49) == "route_capacity"


def test_executor_entry_probe_exposes_the_recent_buy_amount(monkeypatch):
    from eth_account import Account

    from sniper.execution import evm_real_seams as seams
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False)
    ex = seams.build_real_evm_executor(
        acct=Account.from_key("0x" + "42" * 32), chain_id="4663",
        rpc_url="http://rpc",
        venue_quote_fn=lambda *_a, **_k: {"buyAmount": "777"},
        price_fn=lambda: 2500.0, usdc_fn=lambda: 100.0,
        token_fn=lambda _mint: (0.0, 18),
    )

    assert ex["entry_quote_probe_fn"](MINT, 31.49) is None
    assert ex["entry_probe_tokens_fn"](MINT) == 777
    assert ex["entry_probe_tokens_fn"]("0x" + "99" * 20) is None


def test_lifi_canary_forces_the_fallback_when_a_direct_route_exists(monkeypatch):
    from eth_account import Account

    from sniper.execution import evm_real_seams as seams
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False)
    calls = []
    conn = _conn()
    pending_id = _reserve(conn)
    ex = seams.build_real_evm_executor(
        acct=Account.from_key("0x" + "42" * 32), chain_id="4663",
        rpc_url="http://rpc", force_lifi=True,
        venue_quote_fn=lambda *_a, **_k: pytest.fail("direct quote ran in canary mode"),
        lifi_entry_fn=lambda **kwargs: calls.append(kwargs) or {"status": "filled"},
        price_fn=lambda: 2500.0, usdc_fn=lambda: 100.0,
        token_fn=lambda _mint: (0.0, 18),
    )

    result = ex["execute_fn"](conn, pending_id, MINT, 5.0, "2026-08-30T20:00:00+00:00")

    assert result["status"] == "filled"
    assert calls[0]["direct_miss"]["code"] == "no_route"


# --- the executor -------------------------------------------------------------------------

def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _reserve(c):
    return m.reserve_pending(c, mint=MINT, source_user_id="frankdegods", strategy="alpha_b",
                             notional=10.0, reserved_at="2026-08-07T15:04:11Z",
                             network_id="4663")


def _abort_on_no_quote(c, pid, **kw):
    return execute_entry_evm(
        c, pending_id=pid, mint=MINT, network_id="4663", usd_amount=10.0, strategy="alpha_b",
        source_user_id="frankdegods", now_iso="2026-08-07T15:04:12Z",
        quote_fn=lambda *_a, **_k: None,
        approve_fn=lambda *_a, **_k: True, sign_fn=lambda *_a, **_k: ("0xraw", "0xhash"),
        send_fn=lambda *_a, **_k: True, confirm_fn=lambda *_a, **_k: "confirmed",
        usdc_fn=lambda: 100.0, token_fn=lambda _m: (0.0, 18), **kw)


def test_rate_limited_abort_names_the_provider_in_reason_and_release():
    c = _conn()
    pid = _reserve(c)
    r = _abort_on_no_quote(c, pid, quote_miss_fn=lambda: {
        "code": "provider_rate_limit", "detail": "v4 could not read: rate_limit",
        "rungs": [{"rung": "v4", "hit": False, "skipped": False,
                   "blind_reason": "rate_limit"}]})
    assert r["status"] == "aborted"
    assert r["quote_miss_code"] == "provider_rate_limit"
    assert "provider_rate_limit" in r["reason"]
    assert m.pending_row(c, pid)["resolution_reason"] == "quote_provider_rate_limit"
    assert m.pending_totals(c) == (0, 0.0)                  # still released, still fail-closed


def test_no_route_abort_stays_distinguishable_from_a_blind_rung():
    c = _conn()
    pid = _reserve(c)
    r = _abort_on_no_quote(c, pid, quote_miss_fn=lambda: {
        "code": "no_route", "detail": "no route on v3/v2/v4", "rungs": []})
    assert r["quote_miss_code"] == "no_route"
    assert m.pending_row(c, pid)["resolution_reason"] == "quote_no_route"


def test_without_the_seam_the_abort_is_byte_for_byte_the_old_behavior():
    c = _conn()
    pid = _reserve(c)
    r = _abort_on_no_quote(c, pid)
    assert r["status"] == "aborted" and r["reason"] == "no quote"
    assert r["quote_miss_code"] is None
    assert m.pending_row(c, pid)["resolution_reason"] == "quote_unavailable"


def test_a_raising_attribution_seam_cannot_block_the_release():
    c = _conn()
    pid = _reserve(c)

    def boom():
        raise RuntimeError("attribution is not allowed to strand a reservation")

    r = _abort_on_no_quote(c, pid, quote_miss_fn=boom)
    assert r["status"] == "aborted" and r["reason"] == "no quote"
    assert m.pending_totals(c) == (0, 0.0)


def test_the_trace_carries_the_rung_ledger():
    class Trace:
        def __init__(self):
            self.substages = []

        def mark_substage(self, name, **meta):
            self.substages.append((name, meta))

    c = _conn()
    pid = _reserve(c)
    trace = Trace()
    _abort_on_no_quote(c, pid, trace=trace, quote_miss_fn=lambda: {
        "code": "provider_rate_limit", "detail": "v4 could not read: rate_limit",
        "rungs": [{"rung": "v3", "hit": False}, {"rung": "v4", "hit": False,
                                                 "blind_reason": "rate_limit"}]})
    misses = [meta for name, meta in trace.substages if name == "evm.quote_miss"]
    assert len(misses) == 1
    assert misses[0]["code"] == "provider_rate_limit"
    assert [r["rung"] for r in misses[0]["rungs"]] == ["v3", "v4"]


# --- the wiring: build_real_evm_executor must thread the ledger through --------------------

def test_build_real_evm_executor_attributes_a_live_4663_rate_limited_miss(monkeypatch):
    """End to end through the real seam builder: V3/V2 miss, V4 blind on the GT cooldown."""
    from eth_account import Account

    from sniper.execution import evm_real_seams as seams
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True)
    # The FUNDED cooldown cell: the money path gates on that one. A research-batch 429 sets only
    # the shared cell and must NOT be attributed to this coin (see the companion test below).
    monkeypatch.setattr(gt, "_funded_cooldown_until", [float("inf")])    # GT is 429 cooling down
    monkeypatch.setattr(gt, "_cooldown_until", [float("inf")])
    monkeypatch.setattr("sniper.execution.evm_v2_exit_quote.dex_v2_exit_quote",
                        lambda *a, **k: None)
    monkeypatch.setattr("sniper.execution.evm_v4_quote.dex_v4_quote", lambda *a, **k: None)

    c = _conn()
    pid = m.reserve_pending(c, mint=MINT, source_user_id="frankdegods", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-08-07T15:04:11Z",
                            network_id="4663")
    ex = seams.build_real_evm_executor(
        acct=Account.from_key("0x" + "42" * 32), chain_id="4663", rpc_url="http://rpc",
        venue_quote_fn=lambda *a, **k: None, price_fn=lambda: 2500.0,
        usdc_fn=lambda: 100.0, token_fn=lambda _m: (0.0, 18))

    r = ex["execute_fn"](c, pid, MINT, 10.0, "2026-08-07T15:04:12Z")

    assert r["status"] == "aborted"
    # Our own GT cooldown: a local limit, typed discovery_cooldown since 2026-09-28 (was
    # provider_rate_limit, which reads as a provider 429 and holds an exact-quote group).
    assert r["quote_miss_code"] == "provider_discovery_cooldown"
    assert m.pending_row(c, pid)["resolution_reason"] == "quote_provider_discovery_cooldown"


def test_research_only_cooldown_is_not_attributed_to_the_funded_quote(monkeypatch):
    """A blind-capture 429 must not make the funded lane report itself rate limited.

    Live on 2026-08-14 this refused three separate 0xsneakky entries as `provider_rate_limit` while
    the funded path had made no failing request — the research batch owned every one of those 429s.
    """
    from eth_account import Account

    from sniper.execution import evm_real_seams as seams
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True)
    monkeypatch.setattr(gt, "_cooldown_until", [float("inf")])          # research is cooling down
    monkeypatch.setattr(gt, "_funded_cooldown_until", [0.0])            # the money path is clear
    monkeypatch.setattr("sniper.execution.evm_v2_exit_quote.dex_v2_exit_quote",
                        lambda *a, **k: None)
    monkeypatch.setattr("sniper.execution.evm_v4_quote.dex_v4_quote", lambda *a, **k: None)

    c = _conn()
    pid = m.reserve_pending(c, mint=MINT, source_user_id="frankdegods", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-08-07T15:04:11Z",
                            network_id="4663")
    ex = seams.build_real_evm_executor(
        acct=Account.from_key("0x" + "42" * 32), chain_id="4663", rpc_url="http://rpc",
        venue_quote_fn=lambda *a, **k: None, price_fn=lambda: 2500.0,
        usdc_fn=lambda: 100.0, token_fn=lambda _m: (0.0, 18))

    r = ex["execute_fn"](c, pid, MINT, 10.0, "2026-08-07T15:04:12Z")

    assert r["quote_miss_code"] not in ("provider_rate_limit", "provider_discovery_cooldown"), \
        "a research 429 was blamed on the funded quote"


def test_build_real_evm_executor_reports_no_route_when_geckoterminal_is_healthy(monkeypatch):
    from eth_account import Account

    from sniper.execution import evm_real_seams as seams
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True)
    monkeypatch.setattr(gt, "_cooldown_until", [0.0])                   # GT is answering
    # This is a FUNDED entry, so the gate reads the funded cell — patching only the research cell
    # left this test at the mercy of whatever ran before it in the same xdist worker, and it failed
    # with `provider_rate_limit` whenever a leaked funded cooldown was still in force. Both sibling
    # tests above already pin both cells.
    monkeypatch.setattr(gt, "_funded_cooldown_until", [0.0])
    monkeypatch.setattr("sniper.execution.evm_v2_exit_quote.dex_v2_exit_quote",
                        lambda *a, **k: None)
    monkeypatch.setattr("sniper.execution.evm_v4_quote.dex_v4_quote", lambda *a, **k: None)

    c = _conn()
    pid = m.reserve_pending(c, mint=MINT, source_user_id="frankdegods", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-08-07T15:04:11Z",
                            network_id="4663")
    ex = seams.build_real_evm_executor(
        acct=Account.from_key("0x" + "42" * 32), chain_id="4663", rpc_url="http://rpc",
        venue_quote_fn=lambda *a, **k: None, price_fn=lambda: 2500.0,
        usdc_fn=lambda: 100.0, token_fn=lambda _m: (0.0, 18))

    r = ex["execute_fn"](c, pid, MINT, 10.0, "2026-08-07T15:04:12Z")

    assert r["quote_miss_code"] == "no_route"
    assert m.pending_row(c, pid)["resolution_reason"] == "quote_no_route"


def test_capacity_probe_quote_is_consumed_by_the_funded_executor(monkeypatch):
    """The route-capacity quote must not run again after the reservation."""
    from eth_account import Account

    from sniper.execution import evm_real_seams as seams
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False)
    calls = []
    quote = {
        "sellAmount": str(4 * 10 ** 15), "buyAmount": str(10 ** 18),
        "transaction": {"to": "0xrouter", "data": "0xdead"},
        "issues": {"allowance": {"spender": "0xspender"}},
    }

    def venue_quote(*_args, **_kwargs):
        calls.append(1)
        return quote

    ex = seams.build_real_evm_executor(
        acct=Account.from_key("0x" + "42" * 32), chain_id="4663",
        rpc_url="http://rpc", venue_quote_fn=venue_quote,
        price_fn=lambda: 2500.0, usdc_fn=lambda: 100.0,
        token_fn=lambda _mint: (0.0, 18),
        now_fn=lambda: "2026-08-07T15:04:11+00:00",
    )
    assert ex["entry_quote_probe_fn"](MINT, 10.0) is None
    c = _conn()
    pid = _reserve(c)

    result = ex["execute_fn"](
        c, pid, MINT, 10.0, "2026-08-07T15:04:12Z",
        safety_gate_fn=lambda: (False, "test stop"),
    )

    assert result["status"] == "unsafe_gate"
    assert len(calls) == 1
    assert result["quote_observed_at"] == "2026-08-07T15:04:11+00:00"


# --- the relay-wins path, and the unioned ledger (2026-09-02) -----------------------------------
#
# The existing fixtures make v3 hit. v3 is the HIGHEST-priority rung, so the scan finds it on the
# first name and returns without waiting for anything — the one path that is always fast. It
# proves the fast path and never touches the expensive one.
#
# On the 14d 4663 tape relay hit on 51 of 76 quote events, and relay is LAST in ordered_names. So
# the common case is the one no test covered.

import time as _t

from sniper.execution.evm_real_seams import venue_quote_with_v2_fallback


def _timed_ladder(on_rung, **rungs):
    """A ladder whose rungs sleep for a named number of ms and hit or miss as told.

    A hit is MEASURABLE (2026-09-06): the entry ladder quotes the reverse of the bag a hit would
    buy, and `{"buyAmount": 1}` with no `sellAmount` reads as a ~-100% round trip, so every rung
    here would be refused and these timing contracts would measure nothing. The reverse answers
    ~97% of the clip, which is what a real pool at ordinary fees does; the TIMING each test asserts
    is untouched.
    """
    def mk(ms, hit):
        def fn(sell=None, buy=None, amount=None, *a, **k):
            _t.sleep(ms / 1000.0)
            if not hit:
                return None
            if str(sell or "").lower() == "0xm":        # the reverse leg: selling the coin back
                return {"sellAmount": "1000", "buyAmount": "970"}
            return {"sellAmount": "1000", "buyAmount": "1"}
        return fn
    return venue_quote_with_v2_fallback(
        mk(*rungs["v3"]), chain="4663", quote_token="0xq", taker="0xt",
        enabled=True,
        v2_quote_fn=lambda *a, **k: mk(*rungs["v2"])(),
        pons_v2_quote_fn=lambda *a, **k: mk(*rungs["pons_v2"])(),
        v4_quote_fn=lambda *a, **k: mk(*rungs["v4"])(),
        relay_quote_fn=lambda *a, **k: mk(*rungs["relay"])(),
        on_rung=on_rung)


def test_a_relay_win_WAITS_for_every_rung_above_it(monkeypatch):
    """The mechanism. Relay is last, so selecting it requires all five above to finish first —
    the quote costs max(those), not relay's own time. This is the largest reducible term in the
    4663 entry path and no fixture exercised it."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_DEX_BLIND_CHAINS", {"4663"}, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", True, raising=False)
    # Relay is exit-only by default since 2026-09-02; this exercises it on the BUY side,
    # which is real restorable behaviour behind this knob rather than a fiction.
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENTRY_ENABLED", True, raising=False)
    led = []
    # only relay hits; v4 above it is the slow one
    lad = _timed_ladder(led.append, v3=(10, False), v2=(10, False), pons_v2=(10, False),
                  v4=(400, False), relay=(20, True))
    t0 = _t.perf_counter()
    got = lad("0xq", "0xm", 1000)
    elapsed = (_t.perf_counter() - t0) * 1000.0
    # the ladder annotates the hit it returns, so the identity here is the amount, not the shape
    assert got and got.get("buyAmount") == "1", "relay is the only hit, so it must be selected"
    assert elapsed >= 350, (
        "a relay win must wait for the slow HIGHER-priority rung; got %.0fms" % elapsed)


def test_an_entry_waits_for_every_rung_while_an_exit_still_short_circuits(monkeypatch):
    """THE OWNER OF THE LADDER'S TIMING CONTRACT (2026-09-06). Three claims, one place:

      1. on ENTRY under the measured ladder the ladder waits for every rung, because the winner is
         the best measured round trip and that is not known until all of them answer;
      2. on EXIT the priority short-circuit is unchanged -- an exit is never measured;
      3. with `MEMECOIN_EVM_ENTRY_MEASURED_LADDER=0` entry short-circuits exactly as before.

    A rung resolved by priority still records 0.0ms, never a real duration, which is what makes a
    real duration proof that the rung was awaited. Other tests in this file assert their own
    non-timing claims and defer to this one for latency.
    """
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_DEX_BLIND_CHAINS", {"4663"}, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", True, raising=False)
    led = []
    lad = _timed_ladder(led.append, v3=(10, True), v2=(10, False), pons_v2=(10, False),
                  v4=(400, False), relay=(20, True))
    t0 = _t.perf_counter()
    lad("0xq", "0xm", 1000)
    elapsed = (_t.perf_counter() - t0) * 1000.0
    # REWRITTEN FOR THE MEASURED LADDER (2026-09-06). On ENTRY the ladder now waits for every rung
    # and picks the best measured round trip, so a v3 hit no longer returns early and nothing below
    # it is stamped `priority_resolved`. The old contract is not gone -- it still holds on the SELL
    # side and with the switch off, and both are asserted below, which is what keeps this a
    # rewrite rather than a deletion.
    assert elapsed >= 300, "an entry now waits for every rung, including the slow one"
    assert not [r for r in led if r.get("blind_reason") == "priority_resolved"], \
        "nothing is resolved by priority on entry any more"

    # EXIT: unchanged. An exit is never measured, so priority still short-circuits.
    led_exit = []
    lad_exit = _timed_ladder(led_exit.append, v3=(10, True), v2=(10, False), pons_v2=(10, False),
                             v4=(400, False), relay=(20, True))
    t1 = _t.perf_counter()
    lad_exit("0xm", "0xq", 1000)
    assert (_t.perf_counter() - t1) * 1000.0 < 300, "an exit still returns on the first hit"
    assert [r for r in led_exit if r.get("blind_reason") == "priority_resolved"]

    # SWITCH OFF: the old entry behaviour exactly.
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", False, raising=False)
    led_off = []
    lad_off = _timed_ladder(led_off.append, v3=(10, True), v2=(10, False), pons_v2=(10, False),
                            v4=(400, False), relay=(20, True))
    t2 = _t.perf_counter()
    lad_off("0xq", "0xm", 1000)
    assert (_t.perf_counter() - t2) * 1000.0 < 300, "switch off restores the priority short-circuit"
    resolved_off = [r for r in led_off if r.get("blind_reason") == "priority_resolved"]
    assert resolved_off
    assert all(r["duration_ms"] == 0.0 for r in resolved_off), \
        "a priority_resolved rung records 0.0ms — a real duration proves it was awaited"


def test_two_ladder_runs_are_separable_in_one_shared_ledger(monkeypatch):
    """`_record_rung` appends and most callers never reset, so one ledger can hold the UNION of
    several runs. Measured: 13 of 76 4663 quote events carry more than one hit, which a single
    run cannot produce. The durations were always real; the run they belong to was not recorded."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False, raising=False)
    led = []
    lad = _timed_ladder(led.append, v3=(1, True), v2=(1, False), pons_v2=(1, False),
                  v4=(1, False), relay=(1, False))
    lad("0xq", "0xm", 1000)
    lad("0xq", "0xm", 1000)
    assert len({r["run_id"] for r in led}) == 2, \
        "each ladder invocation must stamp its own run_id so rungs can be attributed"
    assert all(r.get("run_id") for r in led), "every record carries its run"


def test_every_blind_rung_is_named_with_the_host_that_failed():
    """PEA on Base, 2026-09-25 20:22Z (attempt 14d813ec). The refusal read "v3 could not read:
    unavailable" and nothing else, while v4's pool discovery had also failed -- its full-range
    Initialize scan answered -32602 on mainnet.base.org. The code stays the first blind rung's (the
    retry lane keys on it); the sentence names every blind rung and the failing call it recorded."""
    rungs = [
        {"rung": "launchpad_curve", "hit": False, "skipped": True, "unavailable": True},
        {"rung": "v3", "hit": False, "skipped": False, "blind_reason": "unavailable"},
        {"rung": "v2", "hit": False, "skipped": False},
        {"rung": "v4", "hit": False, "skipped": False, "blind_reason": "discovery_rpc_failed",
         "calls": [
             {"method": "eth_getLogs", "outcome": "ok", "url_requested": "https://base-rpc.publicnode.com"},
             {"method": "eth_getLogs", "outcome": "rpc_error:-32602",
              "url_requested": "https://mainnet.base.org"},
         ]},
        {"rung": "v4_multihop", "hit": False, "skipped": False, "reason": "no_bridge_candidates"},
    ]
    code, detail = classify_quote_miss(rungs)
    assert code == "provider_unavailable"
    assert detail == ("v3 could not read: unavailable; v4 could not read: discovery_rpc_failed "
                      "(mainnet.base.org eth_getLogs rpc_error:-32602)")
    # One blind rung with no recorded calls reads exactly as it always did.
    assert classify_quote_miss([rungs[1], rungs[2]]) == (
        "provider_unavailable", "v3 could not read: unavailable")
