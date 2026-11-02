"""PASS replay rules on synthetic prints. Never reads a tape."""
import pytest

from sniper.office import pass_replay as R

T0 = 1_000_000.
BASE = dict(sl=30., tp=100., trail=20., cat=60.)
END = T0 + R.MAX_HOLD_S + 100


def sim(prints, rails=BASE, **kw):
    return R.simulate(sorted(prints), T0, rails, **{**dict(tape_end=END), **kw})


def test_entry_is_the_last_print_at_or_before_entry_time_and_needs_one():
    assert sim([(T0 + 5, 1.)])['status'] == 'no_print_at_t0'                          # nothing at or before the entry
    assert sim([(T0 - R.REF_MAX_AGE_S - 1, 1.), (T0 + 5, 1.)])['status'] == 'no_print_at_t0'      # too old
    assert sim([(T0 - 5, 2.), (T0 + 10, 1.4)])['entry_price'] == 2.0


def test_rails_fire_in_the_ledger_order_on_the_first_crossing_print():
    stop = sim([(T0 - 1, 1.), (T0 + 10, .69)])
    assert (stop['status'], stop['rail'], stop['exit_at']) == ('ok', 'stop', T0 + 10) and stop['gross_ret'] == pytest.approx(-.31)
    assert sim([(T0 - 1, 1.), (T0 + 10, .3)])['rail'] == 'catastrophe'                  # a jump through both levels is the catastrophe
    assert sim([(T0 - 1, 1.), (T0 + 10, 2.1)])['rail'] == 'tp'
    trail = sim([(T0 - 1, 1.), (T0 + 10, 1.5), (T0 + 20, 1.19)])                        # peak 1.5, 20 percent off = 1.2
    assert (trail['rail'], trail['exit_at']) == ('trail', T0 + 20)


def test_trail_needs_a_rise_above_spend_and_stop_off_disables_the_stop():
    held = sim([(T0 - 1, 1.), (T0 + 10, .95), (T0 + 20, .75), (T0 + R.MAX_HOLD_S - 1, .75)], BASE)      # never above spend: the trail is not armed, -25 percent is above the stop
    assert (held['status'], held['rail']) == ('ok', 'max_hold')
    off = sim([(T0 - 1, 1.), (T0 + 10, .5), (T0 + R.MAX_HOLD_S, .5)], {**BASE, 'sl': 0., 'cat': 0.})
    assert off['rail'] == 'max_hold' and off['gross_ret'] == pytest.approx(-.5)


def test_no_look_ahead_a_late_print_cannot_change_an_earlier_exit():
    base = [(T0 - 1, 1.), (T0 + 10, .65)]
    a = sim(base)
    b = sim(base + [(T0 + 500, 5.)])
    assert a == b


def test_coverage_ending_before_the_hold_without_a_rail_is_capture_stopped_not_an_outcome():
    r = sim([(T0 - 1, 1.), (T0 + 10, 1.05)], tape_end=T0 + 500)
    assert r['status'] == 'tape_ended_capture_stopped' and r['gross_ret'] is None and r['exit_price'] is None
    assert sim([(T0 - 1, 1.), (T0 + 10, .5)], tape_end=T0 + 500)['status'] == 'ok'       # a rail that fired inside the covered span is still an exit
    assert sim([(T0 - 1, 1.), (T0 + 10, 1.05)], tape_end=None)['status'] == 'tape_ended_capture_stopped'


def test_covered_but_silent_is_coin_quiet_never_scored():
    r = sim([(T0 - 1, 1.), (T0 + 10, 1.05)])                                          # covered through the hold, last print 3590 s before the end
    assert r['status'] == 'tape_ended_coin_quiet' and r['gross_ret'] is None
    live = sim([(T0 - 1, 1.), (T0 + R.MAX_HOLD_S - 5, 1.05)])
    assert live['status'] == 'ok' and live['rail'] == 'max_hold' and live['exit_price'] == 1.05


def test_a_pool_loss_inside_the_span_censors_it():
    assert sim([(T0 - 1, 1.), (T0 + 10, .5)], gaps=[T0 + 5])['status'] == 'censored_pool_loss'
    assert sim([(T0 - 1, 1.), (T0 + 10, .5)], gaps=[T0 + 11])['status'] == 'ok'          # after the exit: no effect


def test_mark_blind_ticks_miss_a_dip_that_recovers_between_marks():
    prints = [(T0 - 1, 1.), (T0 + 3, .3), (T0 + 8, 1.0), (T0 + R.MAX_HOLD_S - 1, 1.02)]
    assert sim(prints)['rail'] == 'catastrophe'
    blind = sim(prints, blind_s=15.)
    assert blind['rail'] == 'max_hold'                                                # evaluated only on 15 s ticks


def test_net_return_subtracts_probe_and_fee_and_absent_probe_is_not_free():
    assert R.net_ret(.10, 2.0) == pytest.approx(.10 - .02 - R.FEE_FRAC)
    assert R.net_ret(.10, None) is None and R.net_ret(None, 2.0) is None


def _script():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('office_pass_replay_script', Path(__file__).resolve().parents[2] / 'scripts/office_pass_replay.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


class FakePS:
    def __init__(self, prints, cover, gaps=(), gap_check='checked'):
        self.p, self.cover, self.g, self.gc = prints, cover, list(gaps), gap_check
    def prints(self, mint, lo, hi, start=None): return [x for x in self.p if lo <= x[0] <= hi], self.cover, None, self.g, self.gc
    def coverage_end(self, pool, start, hi): return self.cover


def test_every_ticket_gets_every_variant_with_own_window_costs_and_stored_features():
    S = _script()
    inputs = '{"evidence":{"checks":{"sell_probe":{"round_trip_loss_pct":2.5},"holders":{"top10_pct":9.0,"creator_pct":null}}}}'
    cand = '{"snapshot":{"fields":{"mcap_usd":{"value":50000,"completeness":"complete"},"liq_usd":{"value":9000,"completeness":"partial"},"age_s":{"value":null,"completeness":"missing"}}}}'
    prints = [(T0 - 1, 1.), (T0 + 2, 1.0), (T0 + 31, 2.2), (T0 + 200, 2.2)]
    rows = S.replay_ticket(FakePS(prints, T0 + 4000), ('t1', T0, 'solana', 'm', 'p', inputs, cand), [(0, 'v')], T0 - 600, now=T0 + 9999)
    assert len(rows) == len(S.VARIANTS) and {r['variant'] for r in rows} == set(S.VARIANTS)
    d1 = next(r for r in rows if r['variant'] == 'base_d1')
    assert d1['probe_round_trip_loss_pct'] == 2.5 and d1['top10_pct'] == 9.0 and d1['mcap_usd'] == 50000 and d1['liq_usd'] == 9000 and d1['pair_age_s'] == 600
    assert d1['features_json'] and '"age_s": null' in d1['features_json']                 # a missing field stays null, never filled
    d30 = next(r for r in rows if r['variant'] == 'base_d30')
    assert d30['entry_price'] == 1.0 and d30['rail'] == 'tp' and d30['net_ret'] == pytest.approx(d30['gross_ret'] - .025 - R.FEE_FRAC)
    nop = S.replay_ticket(FakePS(prints, T0 + 4000), ('t2', T0, 'solana', 'm', 'p', '{}', None), [(0, 'v')], None, now=T0 + 9999)
    assert all(r['net_ret'] is None for r in nop)                                          # no probe: net unknown, gross kept
    assert all(r['pair_age_s'] is None for r in nop)


def test_a_later_print_changes_only_the_variants_whose_window_reaches_it():
    S = _script()
    early = [(T0 - 1, 1.), (T0 + 2, 1.)]
    late = early + [(T0 + 3000, 5.)]
    a = {r['variant']: r for r in S.replay_ticket(FakePS(early, T0 + 4000), ('t', T0, 'solana', 'm', 'p', '{}', None), [(0, 'v')], None, now=T0 + 9999)}
    b = {r['variant']: r for r in S.replay_ticket(FakePS(late, T0 + 4000), ('t', T0, 'solana', 'm', 'p', '{}', None), [(0, 'v')], None, now=T0 + 9999)}
    assert a['base_d1']['status'] == 'tape_ended_coin_quiet' and b['base_d1']['rail'] == 'tp'   # the late print is inside the 3600 s hold of base_d1


def test_a_hold_that_has_not_elapsed_at_asof_is_immature_not_a_capture_stop():
    r = sim([(T0 - 1, 1.), (T0 + 10, 1.05)], tape_end=T0 + 50, asof=T0 + 50)
    assert r['status'] == 'immature' and r['gross_ret'] is None
    assert sim([(T0 - 1, 1.), (T0 + 10, .5)], tape_end=T0 + 50, asof=T0 + 50)['status'] == 'ok'          # a rail that fired by asof is an exit
    assert sim([(T0 - 1, 1.)], tape_end=T0 + 5, asof=T0 - 5)['status'] == 'immature'                      # the entry itself is in the future


def test_every_ok_row_has_its_span_proven_through_the_exit_and_inside_asof():
    S = _script()
    prints = [(T0 - 1, 1.), (T0 + 5, 1.0), (T0 + 40, 0.5), (T0 + 90, 2.5)]
    now = T0 + 70
    for cover in (None, T0 + 10, T0 + 45, T0 + 5000):
        rows = S.replay_ticket(FakePS(prints, cover), ('t', T0, 'solana', 'm', 'p', '{}', None), [(0, 'v')], None, now=now)
        for r in rows:
            if r['status'] == 'ok':
                assert r['exit_at'] <= now and cover is not None and r['exit_at'] <= cover       # coverage end >= exit_at, and nothing after asof was read


def test_a_read_error_is_tape_unreadable_not_a_pool_loss():
    S = _script()
    class Bad(FakePS):
        def prints(self, mint, lo, hi, start=None): raise S.OT.TapeUnreadable('x')
    rows = S.replay_ticket(Bad([], T0 + 4000), ('t', T0, 'solana', 'm', 'p', '{}', None), [(0, 'v')], None, now=T0 + 9999)
    assert {r['status'] for r in rows} == {'tape_unreadable'}


def test_rows_record_the_effective_coverage_end_and_asof_for_audit():
    S = _script()
    now = T0 + 70
    rows = S.replay_ticket(FakePS([(T0 - 1, 1.), (T0 + 5, 1.0), (T0 + 40, .5)], T0 + 5000), ('t', T0, 'solana', 'm', 'p', '{}', None), [(0, 'v')], None, now=now)
    for r in rows:
        assert r['asof'] == now and r['coverage_end'] == now                  # coverage is capped at the run's knowledge time
        if r['status'] == 'ok':
            assert r['exit_at'] <= r['coverage_end'] <= r['asof']             # span proven through the exit, nothing read past asof


def test_a_modelled_exit_in_an_unchecked_window_is_ok_unchecked_not_ok():
    S = _script()
    prints = [(T0 - 1, 1.), (T0 + 5, 1.0), (T0 + 40, .5)]
    for gc, want in (('checked', 'ok'), ('unavailable', 'ok_unchecked')):
        rows = S.replay_ticket(FakePS(prints, T0 + 4000, gap_check=gc), ('t', T0, 'solana', 'm', 'p', '{}', None), [(0, 'v')], None, now=T0 + 9999)
        assert next(r for r in rows if r['variant'] == 'base_d1')['status'] == want


def test_a_capture_stop_interval_overlapping_the_whole_span_censors_the_replay():
    assert sim([(T0 - 1, 1.), (T0 + 10, .5)], gap_spans=[(T0 - 500, T0 + 2000)])['status'] == 'censored_pool_loss'     # began before, ends after
    assert sim([(T0 - 1, 1.), (T0 + 10, .5)], gap_spans=[(T0 + 500, T0 + 600)])['status'] == 'ok'                      # after the exit
