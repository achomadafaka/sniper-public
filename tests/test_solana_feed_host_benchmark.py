"""Host comparisons require independent UTC calibration; monotonic clocks cannot cross hosts."""
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/research/solana_feed_host_benchmark.py'
SPEC = importlib.util.spec_from_file_location('host_benchmark', SCRIPT)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


def capture(host, correction_ms=0, bound_ms=1, *, calibration=True, end_step_ms=0):
    start_ns = 1_000_000_000_000
    meta = {'format': 'solana-feed-host-v1', 'commitment': 'processed', 'host': host, 'owners': ['owner'],
            'clock_start': {'mono_ns': 0, 'epoch_ns': start_ns, 'bracket_ns': 0},
            'clock_end': {'mono_ns': 30_000_000_000,
                          'epoch_ns': start_ns + 30_000_000_000 + int(end_step_ms * 1e6),
                          'bracket_ns': 0},
            'calibration': None}
    if calibration:
        meta['calibration'] = {'utc_correction_ms': correction_ms, 'uncertainty_ms': bound_ms,
             'frequency_error_ppm': 0, 'measured_at_epoch_ns': start_ns,
             'valid_until_epoch_ns': start_ns + 40_000_000_000, 'source': 'test-calibration'}
    def events(delay_ms=0, sig='sig'):
        return [{'k': 'up', 'feed': 'SHYFT_FRA', 't_ns': 0},
                {'k': 'tx', 'feed': 'SHYFT_FRA', 'sig': sig, 'slot': 1,
                 't_ns': 10_000_000_000 + int(delay_ms * 1e6), 'latency_eligible': True},
                {'k': 'down', 'feed': 'SHYFT_FRA', 't_ns': 30_000_000_000}]
    return meta, events


def test_host_comparison_uses_matched_signatures_with_clock_correction_and_error():
    left, le = capture('home')
    right, re = capture('fra', correction_ms=-5)
    result = bench.compare_captures(left, le(), right, re(20))
    p = result['feeds']['SHYFT_FRA']
    assert p['matched_pairs'] == 1 and p['latency_result'] == 'measured_with_clock_bound'
    assert p['p50_ms'] == pytest.approx(15)
    assert p['uncertainty_ms_max'] == 2
    assert p['left_definitely_earlier'] == 1
    assert p['right_definitely_earlier'] == 0


def test_missing_calibration_never_compares_two_monotonic_clocks():
    left, le = capture('home')
    right, re = capture('fra', calibration=False)
    p = bench.compare_captures(left, le(), right, re(20))['feeds']['SHYFT_FRA']
    assert p['matched_pairs'] == 1
    assert p['latency_result'] == 'unknown:calibration_missing'
    assert p['p50_ms'] is p['p90_ms'] is p['uncertainty_ms_max'] is None


@pytest.mark.parametrize('change,reason', [('step', 'clock_step'), ('stale', 'calibration_expired'),
                                        ('bad', 'calibration_invalid')])
def test_clock_failures_censor_latency(change, reason):
    left, le = capture('home')
    right, re = capture('fra', end_step_ms=50 if change == 'step' else 0)
    if change == 'stale': right['calibration']['valid_until_epoch_ns'] = 1_000_000_000_000
    if change == 'bad': right['calibration']['uncertainty_ms'] = float('nan')
    p = bench.compare_captures(left, le(), right, re(20))['feeds']['SHYFT_FRA']
    assert p['latency_result'] == 'unknown:' + reason
    assert p['p50_ms'] is None


def test_overlapping_error_intervals_are_indeterminate_and_missing_is_not_zero():
    left, le = capture('home', bound_ms=10)
    right, re = capture('fra', bound_ms=10)
    left_rows = le() + [dict(le()[1], sig='left-only')]
    p = bench.compare_captures(left, left_rows, right, re(5))['feeds']['SHYFT_FRA']
    assert p['indeterminate'] == 1 and p['left_definitely_earlier'] == 0
    assert p['unpaired_left'] == 1 and p['unpaired_right'] == 0
    assert p['matched_pairs'] == 1 and p['p50_ms'] == 5


def test_stale_head_disconnected_or_wrong_slot_is_not_a_latency_sample():
    left, le = capture('home')
    right, re = capture('fra')
    rows = re(10)
    rows[1]['latency_eligible'] = False
    p = bench.compare_captures(left, le(), right, rows)['feeds']['SHYFT_FRA']
    assert p['matched_pairs'] == 0 and p['p50_ms'] is None
    rows[1]['latency_eligible'] = True
    rows[1]['slot'] = 2
    p = bench.compare_captures(left, le(), right, rows)['feeds']['SHYFT_FRA']
    assert p['matched_pairs'] == 0 and p['identity_mismatch'] == 1


def test_different_frozen_owner_universes_cannot_be_compared():
    left, le = capture('home')
    right, re = capture('fra')
    right['owners'] = ['another']
    with pytest.raises(ValueError, match='owner universes'):
        bench.compare_captures(left, le(), right, re(10))


def test_no_eligible_pairs_keeps_explicit_feed_and_censor_counts():
    left, le = capture('home')
    right, re = capture('fra')
    left_rows, right_rows = le(), re()
    left_rows[1]['latency_eligible'] = right_rows[1]['latency_eligible'] = False
    p = bench.compare_captures(left, left_rows, right, right_rows)['feeds']['SHYFT_FRA']
    assert p['matched_pairs'] == 0 and p['p50_ms'] is None
    assert p['excluded_left'] == p['excluded_right'] == 1


def test_incomplete_observer_shutdown_invalidates_cross_host_result():
    left, le = capture('home')
    right, re = capture('fra')
    right['threads_still_alive'] = 1
    p = bench.compare_captures(left, le(), right, re(20))['feeds']['SHYFT_FRA']
    assert p['latency_result'] == 'unknown:observer_shutdown_incomplete'
    assert p['p50_ms'] is None


@pytest.mark.parametrize('field,value', [('format', 'unrelated-v999'),
               ('commitment', 'confirmed'), ('commitment', None)])
def test_different_protocol_or_commitment_cannot_be_graded(field, value):
    left, le = capture('home')
    right, re = capture('fra')
    right[field] = value
    with pytest.raises(ValueError, match='protocol|commitment'):
        bench.compare_captures(left, le(), right, re(20))


@pytest.mark.parametrize('seconds', [0, -1, 901, float('nan'), float('inf')])
def test_run_bounds_fail_before_credentials_files_or_network(seconds, tmp_path, monkeypatch):
    monkeypatch.setattr(bench, 'load_owners', lambda *args: pytest.fail('read before bounds'))
    with pytest.raises(ValueError, match='bounds'):
        bench.run(seconds=seconds, out=tmp_path / 'capture', owners_file='missing', host='host')


def test_existing_capture_is_preserved(tmp_path):
    (tmp_path / 'old').write_text('old')
    with pytest.raises(FileExistsError):
        bench.run(seconds=1, out=tmp_path, owners_file='missing', host='host')
    assert (tmp_path / 'old').read_text() == 'old'


def test_owner_input_is_frozen_bounded_and_no_production_database(tmp_path):
    p = tmp_path / 'owners.json'
    owner = '2SQarCLrLGA73Hwqfo5fegUbzff88uFMnEq8Y5jP65Yf'
    p.write_text(json.dumps([owner, owner]))
    assert bench.load_owners(p) == [owner]
    p.write_text(json.dumps(['wrong-address']))
    with pytest.raises(ValueError, match='owners'):
        bench.load_owners(p)
