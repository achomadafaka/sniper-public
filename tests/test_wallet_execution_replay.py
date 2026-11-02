from copy import deepcopy
import pytest
from test_wallet_execution_evidence import episode
from sniper.research.wallet_execution.replay import replay


def policy(**changes):
    return {'policy_id': 'sealed-immediate', 'entry': 'immediate', 'clip_usd': 100.,
            'max_hold_s': 3., 'tp_pct': 25., 'sl_pct': 35., 'mirror': False, **changes}


def test_timecap_prices_after_exit_delay_and_all_fees():
    e = episode(); e['frames'][125]['sells']['1000000']['floor_usd'] = 90.
    r = replay(e, policy())
    assert r['state'] == 'closed' and r['entry_at'] == 1 and r['exit_at'] == 5
    assert r['net_cash_usd'] == -12. and r['basis'] == 'modeled_quote_floor'
    assert r['same_block'] == 'unknown'


def test_tp_touch_does_not_fill_at_the_touch():
    e = episode(); e['frames'][122]['sells']['1000000'].update(floor_usd=140., expected_usd=141.)
    e['frames'][123]['sells']['1000000']['floor_usd'] = 80.
    r = replay(e, policy())
    assert r['reason'] == 'tp' and r['net_cash_usd'] == -22.


def test_dip_is_first_available_trigger_not_later_trough():
    e = episode(); e['window'].update(entry_wait_s=10., max_hold_s=30.)
    # Actual entry sizes differ from immediate; a matching whole-bag exit is mandatory.
    for f in e['frames'][123:]:
        f['buy']['quantity_atomic'] = f['buy']['floor_atomic'] = '2000000'
        f['sells']['2000000'] = dict(f['sells']['1000000'], floor_usd=120., expected_usd=121.)
    r = replay(e, policy(entry='dip', max_wait_s=5., premium_cap_pct=-20.))
    assert r['entry_at'] == 4 and r['quantity_atomic'] == '2000000'


def test_unmet_dip_retains_source_episode():
    e = episode(); e['window'].update(entry_wait_s=3., max_hold_s=30.)
    r = replay(e, policy(entry='dip', max_wait_s=3., premium_cap_pct=-20.))
    assert r['state'] == 'not_entered' and r['net_cash_usd'] == 0.


def test_mirror_waits_until_sell_observable_and_our_delay():
    e = episode(); e['source_sells'] = [{'source_at': 1., 'available_at': 4., 'fraction': 1.}]
    r = replay(e, policy(mirror=True, max_hold_s=10.))
    assert r['reason'] == 'mirror' and r['exit_at'] == 5.


@pytest.mark.parametrize('mirror', [None, 'false', 1])
def test_nonboolean_mirror_cannot_change_the_exit_rule(mirror):
    e = episode(); e['source_sells'] = [{'source_at': 1., 'available_at': 4., 'fraction': 1.}]
    result = replay(e, policy(mirror=mirror, max_hold_s=10.))
    assert result['state'] == 'censored' and result['reason'] == 'invalid_policy'
    assert replay(e, policy(mirror=False, max_hold_s=10.))['reason'] == 'timecap'


def test_missing_exact_quantity_is_censored_not_linear_scaled():
    e = episode(); e['frames'][125]['sells'] = {'2000000': {'floor_usd': 220., 'fee_usd': 1., 'route': 'pool'}}
    r = replay(e, policy()); assert r['state'] == 'censored' and r['net_cash_usd'] is None


def test_token_unsellable_is_adverse_not_dropped():
    e = episode(); e['frames'][125]['sells']['1000000'] = {'status': 'token_unsellable', 'fee_usd': 1., 'proof': 'e'*64}
    r = replay(e, policy()); assert r['state'] == 'unsellable' and r['net_cash_usd'] == -102.
    assert r['locked_capital_usd'] == 100.


def test_provider_failure_is_unknown_not_total_loss():
    e = episode(); e['frames'][125]['sells']['1000000'] = {'status': 'provider_timeout'}
    r = replay(e, policy()); assert r['state'] == 'censored' and r['net_cash_usd'] is None


def test_incomplete_path_never_becomes_flat_timecap():
    e = episode(); e['frames'] = []; e['complete'] = True
    assert replay(e, policy())['state'] == 'censored'


def test_trail_arms_and_prices_after_giveback():
    e = episode()
    for i, price in [(122, 140), (123, 130), (124, 110), (125, 95)]:
        e['frames'][i]['sells']['1000000'].update(floor_usd=price, expected_usd=price + 1)
    r = replay(e, policy(tp_pct=0., max_hold_s=10., trail_arm_pct=20., trail_pct=15.))
    assert r['reason'] == 'trail' and r['exit_at'] == 5 and r['net_cash_usd'] == -7.


def sizing_episode():
    e = episode()
    for frame in e['frames']:
        frame['buy']['spend_usd'] = 131.25
        frame['sells']['1000000'].update(floor_usd=146., expected_usd=147.)
        frame['entry_quotes'] = [dict(frame['buy'], spend_usd=175.,
                                     quantity_atomic='1250000', floor_atomic='1250000')]
        frame['sells']['1250000'] = dict(frame['sells']['1000000'], floor_usd=180., expected_usd=181.)
    return e


def test_paired_sizes_use_their_own_quantities_costs_and_exits_without_mutation():
    from sniper.research.wallet_execution.evidence import digest
    e = sizing_episode(); original = digest(e)
    small = replay(e, policy(clip_usd=131.25))
    large = replay(e, policy(policy_id='full', clip_usd=175.))
    assert small['net_cash_usd'] == 12.75
    assert large['state'] == 'closed' and large['net_cash_usd'] == 3.
    assert large['quantity_atomic'] == '1250000'
    assert large['net_cash_usd'] != small['net_cash_usd'] / .75
    assert digest(e) == original and small['episode_digest'] == large['episode_digest']


@pytest.mark.parametrize('damage', ['missing', 'exit_missing', 'fee_unknown', 'duplicate'])
def test_size_quote_failure_censors_instead_of_scaling_or_fake_dip_expiry(damage):
    e = sizing_episode()
    for frame in e['frames']:
        if damage == 'missing': frame.pop('entry_quotes')
        elif damage == 'exit_missing': frame['sells'].pop('1250000')
        elif damage == 'fee_unknown': frame['entry_quotes'][0]['fee_usd'] = None
        else: frame['entry_quotes'].append(dict(frame['entry_quotes'][0]))
    for entry in ['immediate', 'dip']:
        e['window']['entry_wait_s'] = 3.
        result = replay(e, policy(clip_usd=175., entry=entry, max_wait_s=3., premium_cap_pct=100.))
        assert result['state'] == 'censored' and result['net_cash_usd'] is None


def test_a_late_published_frame_is_not_a_fill_at_its_old_state():
    # Codex W7: availability shifted 100 s with the state clock unchanged was eligible and closed +$8.
    e = episode()
    for f in e['frames']: f['available_at'] += 100.
    r = replay(e, policy())
    assert r['state'] == 'censored' and r['reason'] == 'entry_state_stale' and r['net_cash_usd'] is None


def test_a_fresh_entry_with_a_stale_exit_frame_is_censored():
    e = episode()
    for f in e['frames'][125:]: f['available_at'] += 5.
    r = replay(e, policy())
    assert r['state'] == 'censored' and r['reason'] == 'exit_state_stale'


def test_the_dossier_fast_move_uses_the_modeled_clips_own_cost():
    # Codex W3: the $175 alternative clip's return was divided by the $131.25 primary quote.
    from sniper.services.wallet_execution_research import dossier_fast_move
    e = sizing_episode()
    large = replay(e, policy(policy_id='full', clip_usd=175.))
    assert (large['entry_spend_usd'], large['entry_fee_usd']) == (175., 1.)
    fast = dossier_fast_move(e, large)
    measured = [v for v in fast.values() if v['state'] == 'measured']
    assert measured
    want = (179. / 176. - 1) * 100                              # ($180 - $1 exit fee) / ($175 + $1)
    assert all(abs(v['endpoint_pct'] - want) < 1e-9 and abs(v['peak_pct'] - want) < 1e-9 for v in measured)
