"""Exact-quote DEMAND gate — the fix for the 2026-07-27 provider outage I caused.

`real_exit_sweep` chose which positions to quote exactly from a STATIC policy property
(`trail_pct > 0 or derisk_pct > 0`) — "could this position EVER need an exact price" — not "does it
need one NOW". A position at -5% was quoted as often as one at -59%. At the ~31s bracket cadence
that was wasteful; at a 1s tick it was 120 quotes/min against a ~60/min budget and the exact-quote
path went down with an ops streak of 165, so no rail could confirm an exit.
"""
import pytest

from sniper.services.memecoin_quote_demand import (
    RAIL_REASONS, SKIP, QuoteBudget, exact_quote_demand,
)


FAR = dict(stop_pct=60.0, derisk_pct=40.0, trail_pct=30.0, last_exact_age_s=1.0)


def test_a_position_far_from_every_rail_is_skipped():
    """THE fix. Mid-band positions are the overwhelming majority of ticks and cost nothing now."""
    d = exact_quote_demand(mid_pnl_pct=-5.0, **FAR)
    assert d.needed is False and d.reason == SKIP
    assert not d                                        # __bool__ so callers read naturally


@pytest.mark.parametrize("mid,reason", [
    (-51.0, "near_stop"),      # within 10pts of the -60 stop
    (-60.0, "near_stop"),      # at it
    (-95.0, "near_stop"),      # past it
    (31.0, "near_derisk"),     # within 10pts of the +40 derisk
    (40.0, "near_derisk"),
])
def test_quotes_exactly_when_a_rail_is_imminent(mid, reason):
    d = exact_quote_demand(mid_pnl_pct=mid, **FAR)
    assert d.needed is True and d.reason == reason


def test_trail_boundary_moves_with_the_peak():
    """A trail fires on giveback FROM THE PEAK, so a fixed PnL threshold cannot express it. Peak
    +100 with a 30% trail gives back to +40; at +45 we are within the 10pt margin, at +80 we are
    not."""
    near = exact_quote_demand(mid_pnl_pct=45.0, peak_pnl_pct=100.0, trail_armed=True,
                              stop_pct=60.0, derisk_pct=0.0, trail_pct=30.0, last_exact_age_s=1.0)
    assert near.needed is True and near.reason == "near_trail"
    far = exact_quote_demand(mid_pnl_pct=80.0, peak_pnl_pct=100.0, trail_armed=True,
                             stop_pct=60.0, derisk_pct=0.0, trail_pct=30.0, last_exact_age_s=1.0)
    assert far.needed is False


FOLLOW3050 = dict(stop_pct=50.0, derisk_pct=0.0, trail_pct=0.0, tp_pct=30.0,
                  last_exact_age_s=1.0)


@pytest.mark.parametrize("mid,reason", [
    (21.0, "near_tp"),         # within 10pts of the +30 take-profit
    (30.0, "near_tp"),         # at it
    (95.0, "near_tp"),         # past it
    (-41.0, "near_stop"),      # the loss rail still wins when both could match
])
def test_a_take_profit_is_a_price_rail_too(mid, reason):
    """follow-30-50-v1 zeroes de-risk AND trail, so the TP matched no branch and refreshed only on
    the 60s heartbeat while the stop was watched every tick."""
    d = exact_quote_demand(mid_pnl_pct=mid, **FOLLOW3050)
    assert d.needed is True and d.reason == reason


def test_a_position_far_from_its_take_profit_is_still_skipped():
    """The TP branch must not cancel the economy the gate exists for."""
    d = exact_quote_demand(mid_pnl_pct=5.0, **FOLLOW3050)
    assert d.needed is False and d.reason == SKIP


def test_a_zero_take_profit_never_demands_a_quote():
    """Every band policy sets tp_pct=0 and exits on de-risk/trail. A 0 is 'no rail', not 'at 0%'."""
    d = exact_quote_demand(mid_pnl_pct=0.0, stop_pct=60.0, derisk_pct=40.0, trail_pct=30.0,
                           tp_pct=0.0, last_exact_age_s=1.0)
    assert d.needed is False and d.reason == SKIP


def _follow3050_position():
    """A real follow-30-50-v1 contract, not a hand-made one — the wiring must hold for the policy
    that is actually armed."""
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_funded_policy import _follow3050_policy
    contract = {
        "contract_version": 1,
        "identity": {"network_id": "solana", "mint": "MINT"},
        "policy": _follow3050_policy(coin_band="nano").to_dict(),
    }
    payload, digest = m.canonical_funded_json(contract)
    return {
        "id": 1, "entry_price": 1.0, "network_id": "solana", "mint": "MINT",
        "executable_peak_price": None, "peak_price": None,
        "funded_contract_version": 1, "funded_contract_hash": digest,
        "funded_contract_json": payload,
    }


def test_the_fastlane_gate_reads_the_frozen_take_profit():
    """The branch above is dead unless build_demand_fn actually passes the TP. It read only
    stop_pct off the frozen contract."""
    from sniper.services.memecoin_fastlane import build_demand_fn
    pos = _follow3050_position()
    gate = build_demand_fn(None, heartbeat_s=600.0)     # heartbeat far away, so only rails decide
    # A never-quoted position always takes the heartbeat branch, so prime the clock first; after
    # that the rails alone decide and +5% must skip while +25% must quote.
    gate(pos, display_mid=1.0, trail_pct=0.0, derisk_pct=0.0)
    assert gate(pos, display_mid=1.05, trail_pct=0.0, derisk_pct=0.0) is False   # +5%, far
    assert gate(pos, display_mid=1.25, trail_pct=0.0, derisk_pct=0.0) is True    # +25%, TP is +30


def test_heartbeat_keeps_unsellable_detection_alive():
    """The old unconditional pre-pass was ALSO the unsellable detector. A position parked mid-band
    must still be probed periodically or a token that cannot be sold at all goes unnoticed."""
    assert exact_quote_demand(mid_pnl_pct=-5.0, stop_pct=60.0, derisk_pct=40.0,
                              trail_pct=30.0, last_exact_age_s=61.0,
                              heartbeat_s=60.0).reason == "heartbeat"
    assert exact_quote_demand(mid_pnl_pct=-5.0, stop_pct=60.0, derisk_pct=40.0,
                              trail_pct=30.0, last_exact_age_s=None).reason == "heartbeat"


def test_unreadable_mid_fails_OPEN():
    """Missing data must never silently suppress the quote a stop depends on. Skipping is only
    justified when we can PROVE we are far from every rail, and that needs a readable price."""
    for bad in (None, "abc", float("nan"), float("inf")):
        assert exact_quote_demand(mid_pnl_pct=bad, **FAR).needed is True


def test_the_gate_actually_cuts_the_call_volume():
    """Quantifies the fix against the incident: a book sitting mid-band went from every position
    every tick to only the heartbeat."""
    ticks = 60                                          # 60s at a 1s tick, 2 open positions
    before = ticks * 2                                  # old behaviour: always quote
    after = sum(1 for t in range(ticks) for _ in range(2)
                if exact_quote_demand(mid_pnl_pct=-5.0, stop_pct=60.0, derisk_pct=40.0,
                                      trail_pct=30.0, last_exact_age_s=float(t % 60),
                                      heartbeat_s=60.0).needed)
    assert before == 120                                # the volume that took the provider down
    assert after <= 4, f"expected ~heartbeat-only, got {after}"


# ------------------------------------------------------------------------------- budget backstop
def test_budget_refuses_rather_than_blocks():
    clock = [0.0]
    b = QuoteBudget(per_minute=60.0, burst=2, clock=lambda: clock[0])
    assert b.take() and b.take()                        # burst
    assert b.take() is False                            # refused, NOT blocked
    assert b.refused == 1
    clock[0] += 1.0                                     # 1s at 60/min = 1 token
    assert b.take() is True


def test_rail_budget_partitions_the_configured_provider_capacity(monkeypatch):
    from sniper.services.memecoin_fastlane import build_demand_fn
    import sniper.settings.memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EXACT_QUOTE_PER_MINUTE", 8.0)
    gate = build_demand_fn(None)

    assert gate.budget.snapshot()["per_minute"] == 2.0
    assert gate.rail_budget.snapshot()["per_minute"] == 6.0


def test_budget_caps_a_runaway_at_roughly_its_rate():
    """A hard ceiling turns 'a mis-scoped predicate DoSes the provider' into 'degraded but alive'."""
    clock = [0.0]
    b = QuoteBudget(per_minute=30.0, clock=lambda: clock[0])
    granted = 0
    for _ in range(600):                                # 600 attempts across a simulated minute
        clock[0] += 0.1
        granted += bool(b.take())
    assert granted <= 40, f"budget let {granted} through, ceiling is ~30/min + burst"
    assert b.refused > 0 and b.snapshot()["per_minute"] == 30.0


def test_budget_honors_a_provider_rate_limit_hold():
    """A 429 is the provider's own rate answer and outranks the rate we chose."""
    clock = [0.0]
    b = QuoteBudget(per_minute=60.0, burst=5, clock=lambda: clock[0])
    b.penalize(15.0)
    assert b.take() is False and b.snapshot()["hold_s"] == 15.0
    clock[0] += 14.0
    assert b.take() is False, "the hold must outlast a full bucket"
    clock[0] += 1.1
    assert b.take() is True, "the hold must expire on its own"


# --------------------------------------------------------------- wiring: the gate reaches the sweep
def test_prepass_skips_gated_positions_and_never_counts_them_as_failures():
    """A skipped position must not reach `outcomes`. If it did, "we chose not to quote" would be
    recorded as "the provider failed to quote", which is exactly how a false unsellable-CUT gets
    manufactured — the failure mode WO-4 exists to prevent."""
    from sniper.services import memecoin_real_runner as rr

    quoted = []

    class _Ctx(dict):
        pass

    positions = [{"id": 1, "mint": "A", "network_id": "solana", "entry_price": 1.0},
                 {"id": 2, "mint": "B", "network_id": "solana", "entry_price": 1.0}]

    def preview_fn(pos):
        quoted.append(pos["id"])
        return None

    ctx = _Ctx(preview_exit_fn=preview_fn)

    import unittest.mock as mock
    with mock.patch.object(rr, "context_for_position", lambda contexts, pos: ctx), \
         mock.patch("sniper.execution.solana_quote.quote_for", lambda pm, mint, net: {"price": 1.0}), \
         mock.patch.object(rr, "_resolve_override_and_mcap", lambda *a, **k: (None, None)), \
         mock.patch.object(rr, "effective_trail", lambda pos, ov: (30.0, False)), \
         mock.patch.object(rr, "_effective_derisk_pct", lambda pos, ov: 40.0), \
         mock.patch("sniper.analysis.memecoin_quote_failures.sweep_miss_verdicts",
                    lambda outcomes: {}):
        rr._wo4_miss_prepass(
            positions, conn=None, contexts={}, price_map={}, frozen_by_id={1: None, 2: None},
            engine_ids=set(), cfg=type("C", (), {"MEMECOIN_REAL_UNSELLABLE_SWEEPS": 0})(),
            preview_by_position={},
            demand_fn=lambda pos, **kw: pos["id"] == 1)      # gate admits ONLY position 1
    assert quoted == [1], f"gate should have suppressed position 2, quoted={quoted}"


def test_prepass_cools_the_budget_after_a_provider_rate_limit():
    """2026-08-11 05:27: one Jupiter throttle episode paged QUOTE-INFRA at milestone 1 and again at
    milestone 5 inside the same minute. The sweep kept asking at the same rate while the provider
    answered 429, and every refusal advanced the ops streak. A 429 must hold the budget shut, so
    the next sweeps take the gate's skip path instead of manufacturing a deepening outage."""
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    from sniper.execution.exit_preview import PreviewResult
    from sniper.services import memecoin_real_runner as rr

    clock = [0.0]
    budget = QuoteBudget(per_minute=30.0, clock=lambda: clock[0])

    def demand_fn(pos, **kw):
        return budget.take()

    demand_fn.budget = budget

    def preview_fn(pos):
        return PreviewResult.failed(
            QuoteFailure.RATE_LIMIT, provider_id="solana:jupiter",
            provider_group="solana:exact_quote")

    import unittest.mock as mock
    with mock.patch.object(rr, "context_for_position",
                           lambda c, p: {"preview_exit_fn": preview_fn}), \
         mock.patch("sniper.execution.solana_quote.quote_for", lambda pm, mint, net: {"price": 1.0}), \
         mock.patch.object(rr, "_resolve_override_and_mcap", lambda *a, **k: (None, None)), \
         mock.patch.object(rr, "effective_trail", lambda pos, ov: (30.0, False)), \
         mock.patch.object(rr, "_effective_derisk_pct", lambda pos, ov: 40.0), \
         mock.patch.object(rr, "_executable_unit_price", lambda conn, pos, preview, **k: None), \
         mock.patch("sniper.analysis.memecoin_quote_failures.sweep_miss_verdicts",
                    lambda outcomes: {}):
        rr._wo4_miss_prepass(
            [{"id": 7, "mint": "A", "network_id": "solana", "entry_price": 1.0}],
            conn=None, contexts={}, price_map={}, frozen_by_id={7: None}, engine_ids=set(),
            cfg=type("C", (), {"MEMECOIN_REAL_UNSELLABLE_SWEEPS": 0,
                               "MEMECOIN_EXACT_QUOTE_RATE_LIMIT_COOLDOWN_S": 15.0})(),
            preview_by_position={}, demand_fn=demand_fn)

    assert budget.take() is False, "a 429 must hold the exact-quote budget shut"
    clock[0] += 15.1
    assert budget.take() is True, "the hold must be a cooldown, not a permanent stop"


def test_prepass_cools_the_budget_after_a_cached_evm_rate_limit_without_a_display_mark():
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    from sniper.execution.exit_preview import PreviewResult
    from sniper.services import memecoin_real_runner as rr

    clock = [0.0]
    budget = QuoteBudget(per_minute=30.0, clock=lambda: clock[0])

    def demand_fn(pos, **kw):
        return budget.take()

    demand_fn.budget = budget
    preview = PreviewResult.failed(
        QuoteFailure.RATE_LIMIT, provider_id="evm:quote", provider_group="evm:exact_quote")

    import unittest.mock as mock
    with mock.patch.object(rr, "context_for_position", lambda c, p: {"preview_exit_fn": None}), \
         mock.patch("sniper.execution.solana_quote.quote_for", lambda pm, mint, net: {}), \
         mock.patch.object(rr, "_resolve_override_and_mcap", lambda *a, **k: (None, None)), \
         mock.patch.object(rr, "effective_trail", lambda pos, ov: (30.0, False)), \
         mock.patch.object(rr, "_effective_derisk_pct", lambda pos, ov: 40.0), \
         mock.patch.object(rr, "_executable_unit_price", lambda conn, pos, preview, **k: None), \
         mock.patch("sniper.analysis.memecoin_quote_failures.sweep_miss_verdicts",
                    lambda outcomes: {}):
        rr._wo4_miss_prepass(
            [{"id": 7, "mint": "A", "network_id": "4663", "entry_price": 1.0}],
            conn=None, contexts={}, price_map={}, frozen_by_id={7: None}, engine_ids=set(),
            cfg=type("C", (), {"MEMECOIN_REAL_UNSELLABLE_SWEEPS": 0,
                               "MEMECOIN_EXACT_QUOTE_RATE_LIMIT_COOLDOWN_S": 15.0})(),
            preview_by_position={7: preview}, demand_fn=demand_fn)

    assert budget.take() is False


def test_demand_gate_failure_fails_open_and_still_quotes():
    """A bug in the gate must not silently stop quoting — that would disable stop confirmation."""
    from sniper.services import memecoin_real_runner as rr
    quoted = []

    def preview_fn(pos):
        quoted.append(pos["id"])
        return None

    import unittest.mock as mock
    with mock.patch.object(rr, "context_for_position", lambda c, p: {"preview_exit_fn": preview_fn}), \
         mock.patch("sniper.execution.solana_quote.quote_for", lambda pm, mint, net: {"price": 1.0}), \
         mock.patch.object(rr, "_resolve_override_and_mcap", lambda *a, **k: (None, None)), \
         mock.patch.object(rr, "effective_trail", lambda pos, ov: (30.0, False)), \
         mock.patch.object(rr, "_effective_derisk_pct", lambda pos, ov: 40.0), \
         mock.patch("sniper.analysis.memecoin_quote_failures.sweep_miss_verdicts",
                    lambda outcomes: {}):
        rr._wo4_miss_prepass(
            [{"id": 9, "mint": "A", "network_id": "solana", "entry_price": 1.0}],
            conn=None, contexts={}, price_map={}, frozen_by_id={9: None}, engine_ids=set(),
            cfg=type("C", (), {"MEMECOIN_REAL_UNSELLABLE_SWEEPS": 0})(),
            preview_by_position={},
            demand_fn=lambda pos, **kw: (_ for _ in ()).throw(RuntimeError("gate bug")))
    assert quoted == [9], "a raising gate must fail OPEN"


def test_slow_path_is_unchanged_when_no_gate_is_supplied():
    """demand_fn defaults to None so the ~31s bracket pass keeps its exact prior behaviour —
    the change is scoped to the fast lane."""
    import inspect
    from sniper.services import memecoin_real_runner as rr
    assert inspect.signature(rr._wo4_miss_prepass).parameters["demand_fn"].default is None
    assert inspect.signature(rr.real_exit_sweep).parameters["demand_fn"].default is None


def test_prepass_passes_the_authoritative_armed_flag_not_a_guess():
    """A peak of +7.33% has NOT armed a trail that arms at +15%. Deriving armed from `bool(peak)`
    made an unarmed position demand an exact quote EVERY tick — caught only by rehearsing the gate
    against the live book, because the unit tests encoded the same wrong assumption. The prepass
    already computes the authoritative flag; it must hand it to the gate."""
    from sniper.services import memecoin_real_runner as rr
    seen = {}
    import unittest.mock as mock
    with mock.patch.object(rr, "context_for_position", lambda c, p: {"preview_exit_fn": lambda x: None}), \
         mock.patch("sniper.execution.solana_quote.quote_for", lambda pm, mint, net: {"price": 1.0}), \
         mock.patch.object(rr, "_resolve_override_and_mcap", lambda *a, **k: (None, None)), \
         mock.patch.object(rr, "effective_trail", lambda pos, ov: (30.0, False)), \
         mock.patch.object(rr, "_effective_derisk_pct", lambda pos, ov: 40.0), \
         mock.patch("sniper.analysis.memecoin_quote_failures.sweep_miss_verdicts", lambda o: {}):
        def gate(pos, **kw):
            seen.update(kw)
            return False
        rr._wo4_miss_prepass(
            [{"id": 1, "mint": "A", "network_id": "solana", "entry_price": 1.0}],
            conn=None, contexts={}, price_map={}, frozen_by_id={1: None}, engine_ids=set(),
            cfg=type("C", (), {"MEMECOIN_REAL_UNSELLABLE_SWEEPS": 0})(),
            preview_by_position={}, demand_fn=gate)
    assert "trail_armed" in seen, "the gate must receive the sweep's armed flag"
    assert seen["trail_armed"] is False


def test_a_gated_skip_suppresses_the_ENGINE_LOOP_fetch_too():
    """REGRESSION (2026-07-27): there are TWO exact-quote fetch sites. Gating only the WO-4 pre-pass
    moved the call to the engine loop instead of removing it — that loop refetches whenever a
    position is ABSENT from preview_by_position (`preview_cached`). The gate looked live while
    Helius kept 429ing (5/12 identity probes failed, and the deploy verifier refused 3 times).

    A skip must therefore be RECORDED, not merely skipped."""
    from sniper.services import memecoin_real_runner as rr
    import unittest.mock as mock

    pbp = {}
    with mock.patch.object(rr, "context_for_position",
                           lambda c, p: {"preview_exit_fn": lambda x: "SHOULD-NOT-BE-CALLED"}), \
         mock.patch("sniper.execution.solana_quote.quote_for", lambda pm, mint, net: {"price": 1.0}), \
         mock.patch.object(rr, "_resolve_override_and_mcap", lambda *a, **k: (None, None)), \
         mock.patch.object(rr, "effective_trail", lambda pos, ov: (30.0, False)), \
         mock.patch.object(rr, "_effective_derisk_pct", lambda pos, ov: 40.0), \
         mock.patch("sniper.analysis.memecoin_quote_failures.sweep_miss_verdicts", lambda o: {}):
        out = rr._wo4_miss_prepass(
            [{"id": 7, "mint": "A", "network_id": "solana", "entry_price": 1.0}],
            conn=None, contexts={}, price_map={}, frozen_by_id={7: None}, engine_ids=set(),
            cfg=type("C", (), {"MEMECOIN_REAL_UNSELLABLE_SWEEPS": 0})(),
            preview_by_position=pbp, demand_fn=lambda pos, **kw: False)

    assert 7 in out, "a gated skip must be RECORDED so the engine loop treats it as resolved"
    assert out[7] is None, "the recorded value means 'no exact price this tick'"
    # This is the exact predicate the engine loop uses; it must now read True.
    assert (7 in out) is True, "preview_cached must be True for a deliberately skipped position"


# --- always-exact (2026-09-02) -------------------------------------------------------------
# A funded 4663 position's DexScreener mid froze at one value for 21 marks over 24s while the
# executable price rose 51% past its take-profit. The gate declined correctly on that input and the
# TP never fired. A frozen mid can never look near a rail, so the failure is self-sealing.

def test_always_quotes_regardless_of_distance_from_every_rail():
    """The exact 2026-09-02 state: mid says -5.4%, TP is +25%, true price was +42.8%."""
    kwargs = dict(mid_pnl_pct=-5.4, stop_pct=35.0, tp_pct=25.0,
                  derisk_pct=0.0, trail_pct=0.0, last_exact_age_s=5.0)
    gated = exact_quote_demand(**kwargs)
    assert not gated.needed and gated.reason == "mid_only"      # today's behaviour, unchanged

    forced = exact_quote_demand(**kwargs, always=True)
    assert forced.needed and forced.reason == "always_exact"


def test_always_does_not_depend_on_a_readable_mid():
    """This mode exists to stop trusting the mid, so it must not require one to decide."""
    for mid in (None, float("nan"), "not-a-number"):
        d = exact_quote_demand(mid_pnl_pct=mid, stop_pct=35.0, tp_pct=25.0,
                               last_exact_age_s=1.0, always=True)
        assert d.needed and d.reason == "always_exact"


def test_always_exact_draws_the_rail_budget():
    """Per-position rail work is bounded by open positions; it must not draw the small refresh
    bucket that a heartbeat shares, or a take-profit can be starved by a periodic refresh."""
    assert "always_exact" in RAIL_REASONS


def test_always_off_is_the_default_and_changes_nothing():
    """Every existing reason string and outcome must survive, since `always` defaults off."""
    near = exact_quote_demand(mid_pnl_pct=-26.0, stop_pct=35.0, last_exact_age_s=1.0)
    assert near.needed and near.reason == "near_stop"
    tp = exact_quote_demand(mid_pnl_pct=16.0, tp_pct=25.0, last_exact_age_s=1.0)
    assert tp.needed and tp.reason == "near_tp"
    far = exact_quote_demand(mid_pnl_pct=0.0, stop_pct=35.0, tp_pct=25.0, last_exact_age_s=1.0)
    assert not far.needed and far.reason == "mid_only"
    unreadable = exact_quote_demand(mid_pnl_pct=None, last_exact_age_s=1.0)
    assert unreadable.needed and unreadable.reason == "mid_unreadable"


# ------------------------------------------------ the 429 hold is per provider group (2026-09-28)
# Base 66253's RPC trouble (133 INFRA lines, 263 holds) held the exact-quote budgets for EVERY chain:
# new Solana fills went quote-blind 6.5-20.2 s inside their first minute. A 429 now holds the group
# that answered it; a 429 from a quote that named no provider still holds everything.
BASE, SOL = "8453:direct", "solana:exact_quote"


def test_a_429_in_one_group_leaves_the_other_groups_quoting():
    clock = [0.0]
    b = QuoteBudget(per_minute=600.0, clock=lambda: clock[0])
    b.penalize(15.0, group=BASE)
    assert b.take(group=SOL) is True, "a Base 429 must not hold Solana"
    assert b.take(group=BASE) is False
    assert b.take() is False, "an unknown group is covered by any hold, as before"
    assert b.snapshot()["hold_s"] == 0.0 and b.snapshot()["group_hold_s"] == {BASE: 15.0}
    clock[0] += 14.0
    assert b.take(group=BASE) is False, "the 15 s hold still applies within the group"
    clock[0] += 1.1
    assert b.take(group=BASE) is True


def test_each_group_escalates_and_relieves_on_its_own():
    clock = [0.0]
    b = QuoteBudget(per_minute=600.0, clock=lambda: clock[0])
    b.penalize(15.0, group=BASE)
    clock[0] += 16.0
    b.penalize(15.0, group=BASE)          # a second Base 429: Base waits longer
    b.penalize(15.0, group=SOL)           # Solana's first: the base cooldown
    clock[0] += 16.0
    assert b.take(group=SOL) is True and b.take(group=BASE) is False
    b.relieve(group=BASE)
    clock[0] += 200.0
    b.penalize(15.0, group=BASE)          # after a success the next Base episode starts fresh
    clock[0] += 16.0
    assert b.take(group=BASE) is True


def test_an_unnamed_429_still_holds_every_group():
    clock = [0.0]
    b = QuoteBudget(per_minute=600.0, clock=lambda: clock[0])
    b.penalize(15.0)
    assert b.take(group=SOL) is False and b.take(group=BASE) is False
    clock[0] += 15.1
    assert b.take(group=SOL) is True


def _prepass_with(previews, demand_fn, positions):
    import unittest.mock as mock
    from sniper.services import memecoin_real_runner as rr
    good = {pid for pid, p in previews.items() if getattr(p, "failure", None) is None}
    with mock.patch.object(rr, "context_for_position", lambda c, p: {"preview_exit_fn": None}), \
         mock.patch("sniper.execution.solana_quote.quote_for", lambda pm, mint, net: {"price": 1.0}), \
         mock.patch.object(rr, "_resolve_override_and_mcap", lambda *a, **k: (None, None)), \
         mock.patch.object(rr, "effective_trail", lambda pos, ov: (30.0, False)), \
         mock.patch.object(rr, "_effective_derisk_pct", lambda pos, ov: 40.0), \
         mock.patch.object(rr, "_executable_unit_price",
                           lambda conn, pos, preview, **k: 1.0 if pos["id"] in good else None), \
         mock.patch("sniper.analysis.memecoin_quote_failures.sweep_miss_verdicts",
                    lambda outcomes: {}):
        rr._wo4_miss_prepass(
            positions, conn=None, contexts={}, price_map={},
            frozen_by_id={p["id"]: None for p in positions}, engine_ids=set(),
            cfg=type("C", (), {"MEMECOIN_REAL_UNSELLABLE_SWEEPS": 0,
                               "MEMECOIN_EXACT_QUOTE_RATE_LIMIT_COOLDOWN_S": 15.0})(),
            preview_by_position=dict(previews), demand_fn=demand_fn)


def _pos(pid, network):
    return {"id": pid, "mint": "M%d" % pid, "network_id": network, "entry_price": 1.0}


def _wants(demand_fn, pos):
    return demand_fn(pos, display_mid=1.0, trail_pct=30.0, derisk_pct=40.0, trail_armed=False)


def test_a_base_429_in_the_sweep_leaves_a_new_solana_fill_quoting():
    """The live shape: Base 66253 answers 429 while a Solana position quotes. A NEW Solana fill
    (no quote yet) must still get its first exact quote; Base, old and new, waits its 15 s."""
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    from sniper.execution.exit_preview import PreviewResult
    from sniper.services.memecoin_fastlane import build_demand_fn
    clock = [0.0]
    demand_fn = build_demand_fn(None, always=True, clock=lambda: clock[0])
    _prepass_with({
        1: PreviewResult.failed(QuoteFailure.RATE_LIMIT, provider_id=BASE),
        2: PreviewResult.succeeded({"expected_usdc": 1.0}, provider_id="solana:jupiter",
                                   provider_group=SOL),
    }, demand_fn, [_pos(1, "8453"), _pos(2, "solana")])
    assert _wants(demand_fn, _pos(3, "solana")) is True, "a new Solana fill must quote"
    assert _wants(demand_fn, _pos(1, "8453")) is False, "the failing Base position waits"
    assert _wants(demand_fn, _pos(4, "8453")) is False, "and so does any other Base position"
    clock[0] += 15.1
    assert _wants(demand_fn, _pos(1, "8453")) is True, "the Base hold is a cooldown"


def test_a_429_that_named_no_provider_keeps_the_global_hold():
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    from sniper.execution.exit_preview import PreviewResult
    from sniper.services.memecoin_fastlane import build_demand_fn
    clock = [0.0]
    demand_fn = build_demand_fn(None, always=True, clock=lambda: clock[0])
    _prepass_with({1: PreviewResult.failed(QuoteFailure.RATE_LIMIT)}, demand_fn,
                  [_pos(1, "8453")])
    assert _wants(demand_fn, _pos(3, "solana")) is False, "unknown provider: hold everything"
    clock[0] += 15.1
    assert _wants(demand_fn, _pos(3, "solana")) is True


def test_a_position_is_held_by_the_group_its_own_quote_named():
    """A lifi-dependent 4663 position quotes through `4663:lifi:nordstern`, not `4663:direct`. Its
    429 must hold that position even though its chain default names another group."""
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    from sniper.execution.exit_preview import PreviewResult
    from sniper.services.memecoin_fastlane import build_demand_fn
    clock = [0.0]
    demand_fn = build_demand_fn(None, always=True, clock=lambda: clock[0])
    _prepass_with({5: PreviewResult.failed(QuoteFailure.RATE_LIMIT,
                                           provider_id="4663:lifi:nordstern")},
                  demand_fn, [_pos(5, "4663")])
    assert _wants(demand_fn, _pos(5, "4663")) is False
    assert _wants(demand_fn, _pos(6, "4663")) is True, "a direct-quoted 4663 position is not held"


def test_a_base_429_leaves_the_solana_refresh_bucket_open_too():
    """A position that already has an exact price draws the small REFRESH bucket on a heartbeat;
    a Base 429 must not shut that bucket for Solana either."""
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    from sniper.execution.exit_preview import PreviewResult
    from sniper.services.memecoin_fastlane import build_demand_fn
    clock = [0.0]
    demand_fn = build_demand_fn(None, always=False, heartbeat_s=0.0, near_pct=0.0,
                                clock=lambda: clock[0])
    sol = _pos(2, "solana")
    assert _wants(demand_fn, sol) is True                 # the bootstrap quote (rail bucket)
    refused_before = demand_fn.budget.snapshot()["refused"]
    _prepass_with({1: PreviewResult.failed(QuoteFailure.RATE_LIMIT, provider_id=BASE)},
                  demand_fn, [_pos(1, "8453")])
    assert _wants(demand_fn, sol) is True, "the Solana heartbeat must still draw the refresh bucket"
    assert demand_fn.budget.snapshot()["refused"] == refused_before
