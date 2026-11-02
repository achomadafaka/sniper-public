"""R6.1: a window whose only losses are bracketed boost/init-boost buys is ok_bracketed, a stratum apart from ok. Synthetic rows only."""
import pytest

from sniper.office import outcomes as O

T0 = 1_000_000.
KW = dict(now=T0 + 10_000., tape_end=T0 + 10_000., gap_check='checked')
BOOST, SWEEP = '3f451c16305cc2b9', '82a42461e48287a5'


def r(slot, price, sig=None, pre=False, ordinal=0):
    return dict(slot=slot, price_sol=price, signature=(sig or 's%d' % slot) + (':pre' if pre else ''), received_at=T0 + slot, ordinal=ordinal - 0.5 if pre else ordinal, price_only=pre)


def tape(post_prev=1.0, pre_next=1.02):
    return [r(10, post_prev), r(30, 1.03), r(30, pre_next, 's30', True)]      # the ':pre' row is printed for the print right after the lost swap


def test_a_single_bracketed_boost_loss_is_ok_bracketed_with_its_displacement():
    b = O.bracket_losses(tape(), [(T0 + 20, 20, BOOST)])
    assert b['times'] == {T0 + 20} and b['n'] == 1 and b['disp_max'] == pytest.approx(0.02)
    prints = [(T0 - 1, 1.0), (T0 + 10, 1.0), (T0 + 30, 1.03), (T0 + 55, 1.01)]
    x = O.horizon_row(O.clean(prints), T0, 60, gaps=[T0 + 20], loss=(1, 0), bracketed=b, **KW)
    assert x['status'] == 'ok_bracketed' and x['ret'] == pytest.approx(0.01) and x['bracket_n'] == 1 and x['bracket_endpoint_displacement_max'] == pytest.approx(0.02)
    assert x['collapsed'] is None and x['collapse_s'] is None                                 # collapsed=0 is unknown in this stratum
    plain = O.horizon_row(O.clean(prints), T0, 60, gaps=[T0 + 20], loss=(1, 0), **KW)
    assert plain['status'] == 'censored_gap'                                                  # without the bracket the same window still censors


def test_collapsed_one_stays_valid_in_the_stratum():
    b = O.bracket_losses(tape(), [(T0 + 20, 20, BOOST)])
    prints = [(T0 - 1, 1.0), (T0 + 10, 1.0), (T0 + 30, 0.4), (T0 + 55, 0.45)]
    x = O.horizon_row(O.clean(prints), T0, 60, gaps=[T0 + 20], loss=(1, 0), bracketed=b, **KW)
    assert x['status'] == 'ok_bracketed' and x['collapsed'] == 1 and x['collapse_s'] == pytest.approx(30)


@pytest.mark.parametrize('why', ['screen', 'other_loss_in_gap', 'sweep_kind', 'edge_no_next', 'edge_no_prev', 'no_pre_row', 'same_slot_as_print', 'no_slot'])
def test_every_other_shape_is_not_bracketed_and_stays_censored(why):
    rows, losses = tape(), [(T0 + 20, 20, BOOST)]
    if why == 'screen': rows = tape(pre_next=1.11)                                      # displacement 11 percent is over the screen
    if why == 'other_loss_in_gap': losses.append((T0 + 22, 22, SWEEP))
    if why == 'sweep_kind': losses = [(T0 + 20, 20, SWEEP)]
    if why == 'edge_no_next': rows = rows[:1]
    if why == 'edge_no_prev': losses = [(T0 + 5, 5, BOOST)]
    if why == 'no_pre_row': rows = rows[:2]
    if why == 'same_slot_as_print': losses = [(T0 + 10, 10, BOOST)]                     # strict slots: a loss in the previous print's own slot has no earlier print
    if why == 'no_slot': losses = [(T0 + 20, None, BOOST)]
    b = O.bracket_losses(rows, losses)
    assert b['times'] == set() and b['n'] == 0
    assert b['blocked'][losses[0][0]] == {'screen': 'screen', 'other_loss_in_gap': 'other_loss_in_gap', 'sweep_kind': 'kind:' + SWEEP, 'edge_no_next': 'no_next_print',
                                          'edge_no_prev': 'no_prev_print', 'no_pre_row': 'no_pre_row', 'same_slot_as_print': 'no_prev_print', 'no_slot': 'no_slot'}[why]      # the reason is named, for the run stats
    prints = [(T0 - 1, 1.0), (T0 + 10, 1.0), (T0 + 30, 1.03), (T0 + 55, 1.01)]
    assert O.horizon_row(O.clean(prints), T0, 60, gaps=[losses[0][0]], loss=(1, 0), bracketed=b, **KW)['status'] == 'censored_gap'


def test_a_capture_gap_point_in_the_window_still_censors_a_bracketed_window():
    b = O.bracket_losses(tape(), [(T0 + 20, 20, BOOST)])
    prints = [(T0 - 1, 1.0), (T0 + 10, 1.0), (T0 + 30, 1.03), (T0 + 55, 1.01)]
    assert O.horizon_row(O.clean(prints), T0, 60, gaps=[T0 + 20, T0 + 40], loss=(1, 0), bracketed=b, **KW)['status'] == 'censored_gap'
    assert O.horizon_row(O.clean(prints), T0, 60, gaps=[T0 + 20], gap_spans=[(T0 + 40, T0 + 50)], loss=(1, 0), bracketed=b, **KW)['status'] == 'censored_gap'


def test_a_bracket_never_lifts_an_unchecked_or_quiet_window():
    b = O.bracket_losses(tape(), [(T0 + 20, 20, BOOST)])
    prints = [(T0 - 1, 1.0), (T0 + 10, 1.0), (T0 + 30, 1.03)]                             # last print 30 s old at 60 s: fine; make it quiet instead
    quiet = O.horizon_row(O.clean(prints[:2]), T0, 300, gaps=[T0 + 20], bracketed=b, **{**KW, 'now': T0 + 10_000.})
    assert quiet['status'] == 'tape_ended_quiet'
    assert O.horizon_row(O.clean(prints), T0, 60, gaps=[T0 + 20], bracketed=b, **{**KW, 'gap_check': 'unavailable'})['status'] == 'ok_unchecked'


def test_the_pre_row_is_bound_to_signature_and_ordinal_not_signature_alone():
    # one tx, two swaps: the 20 percent state after the first is the loss's pre state; a later ordinal's 1 percent pre row must not replace it
    rows = [r(10, 1.0), r(30, 1.03, 's30', ordinal=1), r(30, 1.20, 's30', pre=True, ordinal=1), r(30, 1.21, 's30', ordinal=2), r(30, 1.01, 's30', pre=True, ordinal=2)]
    assert O.bracket_losses(rows, [(T0 + 20, 20, BOOST)])['times'] == set()           # 20 percent displacement is over the screen
    rows = [r(10, 1.0), r(30, 1.03, 's30', ordinal=1), r(30, 1.01, 's30', pre=True, ordinal=2)]
    assert O.bracket_losses(rows, [(T0 + 20, 20, BOOST)])['blocked'][T0 + 20] == 'no_pre_row'      # a pre row of another ordinal is not this print's


def test_a_capture_gap_point_sharing_a_bracketed_losss_receipt_time_is_never_forgiven():
    b = O.bracket_losses(tape(), [(T0 + 20, 20, BOOST)])
    prints = [(T0 - 1, 1.0), (T0 + 10, 1.0), (T0 + 30, 1.03), (T0 + 55, 1.01)]
    x = O.horizon_row(O.clean(prints), T0, 60, gaps=[T0 + 20], loss=(1, 0), bracketed=b, capture_points=[T0 + 20], **KW)
    assert x['status'] == 'censored_gap'
    assert O.horizon_row(O.clean(prints), T0, 60, gaps=[T0 + 20], loss=(1, 0), bracketed=b, capture_points=[], **KW)['status'] == 'ok_bracketed'
