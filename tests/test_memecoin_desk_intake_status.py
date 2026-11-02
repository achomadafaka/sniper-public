"""Read-only intake reporting keeps absent, empty and failed sources distinct."""
import json
import sqlite3

import pytest
from datetime import datetime, timezone

from scripts import memecoin_desk_intake_status as S
from sniper.services import memecoin_desk_intake as I

NOW = datetime(2026, 10, 27, 13, tzinfo=timezone.utc).timestamp()


def poll(root, feed='pumpfun', filt='live', age=10, rows=(), error=None, offset=0):
    conn = I.open_day(root / 'data/desk/intake', feed, NOW - age)
    try:
        I.record_poll(conn, feed, filt, NOW - age, rows, offset_s=offset, error=error)
    finally:
        conn.close()


def coin(mint, network='solana', venue='pump_curve'):
    return dict(network_id=network, chain_raw=network or 'unmapped', mint=mint, venue=venue, raw={})


def clock(root, offset=0):
    (root / 'data').mkdir(exist_ok=True)
    (root / 'data/clock_offset.json').write_text(json.dumps({'offset_s': offset}))


def metric(report, feed='pumpfun', window='1h', filt='live'):
    return report['feeds'][feed]['windows'][window]['filters'][filt]


def test_absent_directory_is_not_running_and_never_created(tmp_path):
    root = tmp_path / 'absent'
    report = S.build_report(root, now=NOW)
    assert not root.exists()
    assert report['feeds']['pumpfun']['source'] == 'not running'
    assert metric(report)['polls'] is None
    assert report['emitter']['source'] == 'not running'
    assert report['emitter']['windows']['1h'] is None


def test_windows_filters_chain_and_first_sighting_denominators(tmp_path):
    clock(tmp_path)
    poll(tmp_path, rows=[coin('s1'), coin('e1', '56'), coin('u1', None, None)])
    poll(tmp_path, filt='active', rows=[coin('s1'), coin('s2', venue='pumpswap')])
    poll(tmp_path, age=7200, rows=[coin('old')])
    poll(tmp_path, age=86401, rows=[coin('outside')])
    report = S.build_report(tmp_path, now=NOW)
    live = metric(report)
    assert live['polls'] == {'ok': 1, 'error': 0, 'refused': 0, 'other': 0}
    assert live['items'] == 3 and live['new_first_sightings'] == 2
    assert live['chains'] == {'solana': 1, '56': 1, 'unknown': 1}
    assert live['solana_share'] == 1 / 3
    active = metric(report, filt='active')
    assert active['new_first_sightings'] == 1
    assert active['provider_graduated'] == {'yes': 1, 'no': 1, 'unknown': 0}
    assert active['provider_graduated_share'] == .5
    assert active['locally_mapped_share'] is None
    assert metric(report, window='24h')['items'] == 4


def test_empty_day_has_known_zero_polls_but_error_only_items_are_unknown(tmp_path):
    clock(tmp_path)
    conn = I.open_day(tmp_path / 'data/desk/intake', 'pumpfun', NOW)
    conn.close()
    empty = metric(S.build_report(tmp_path, now=NOW))
    assert empty['polls'] == {'ok': 0, 'error': 0, 'refused': 0, 'other': 0}
    assert empty['items'] == 0 and empty['solana_share'] is None
    poll(tmp_path, error='timeout', age=30)
    poll(tmp_path, error='refused:challenge')
    failed = metric(S.build_report(tmp_path, now=NOW))
    assert failed['polls'] == {'ok': 0, 'error': 1, 'refused': 1, 'other': 0}
    assert failed['items'] is None and failed['newest_error_class'] == 'refused:challenge'


def test_fomo_unknown_venue_is_not_counted_as_not_graduated(tmp_path):
    poll(tmp_path, feed='fomo', filt='trending24h', rows=[coin('s', venue=None)])
    row = metric(S.build_report(tmp_path, now=NOW), feed='fomo', filt='trending24h')
    assert row['provider_graduated'] == {'yes': 0, 'no': 0, 'unknown': 1}
    assert row['provider_graduated_share'] is None
    assert row['new_first_sightings'] is None  # no trusted current clock offset


def test_readonly_connections_and_corrupt_source_are_explicit(tmp_path, monkeypatch):
    clock(tmp_path)
    poll(tmp_path)
    original = sqlite3.connect
    opened = []
    def readonly(path, *args, **kwargs):
        assert kwargs.get('uri') is True and 'mode=ro' in str(path)
        conn = original(path, *args, **kwargs)
        try:
            conn.execute('CREATE TABLE reporter_must_not_write(id)')
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError('reporter DB is writable')
        opened.append(str(path))
        return conn
    monkeypatch.setattr(S.sqlite3, 'connect', readonly)
    assert metric(S.build_report(tmp_path, now=NOW))['items'] == 0
    assert opened
    day = tmp_path / 'data/desk/intake/pumpfun/2026-10-27.db'
    day.write_bytes(b'not a sqlite database')
    bad = S.build_report(tmp_path, now=NOW)['feeds']['pumpfun']
    assert bad['source'] == 'unknown' and bad['errors']
    assert bad['windows']['1h']['filters']['live']['items'] is None


def test_cli_text_and_json_do_not_create_missing_sources(tmp_path, capsys):
    assert S.main(['--state-root', str(tmp_path), '--json']) == 0
    assert json.loads(capsys.readouterr().out)['emitter']['source'] == 'not running'
    assert S.main(['--state-root', str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert 'pumpfun' in text and 'not running' in text and 'unknown' in text
    assert not (tmp_path / 'data').exists()


def test_emitter_outcomes_and_jsonl_appends_are_separate_and_windowed(tmp_path):
    desk = tmp_path / 'data/desk'
    desk.mkdir(parents=True)
    conn = sqlite3.connect(desk / 'intake_emitter.db')
    conn.execute('CREATE TABLE outcomes(intake_row_ref TEXT PRIMARY KEY,feed TEXT,filter TEXT,network_id TEXT,mint TEXT,reason TEXT,opportunity_id TEXT,at_host REAL)')
    for index, reason in enumerate(('claimed', 'emitted', 'cooldown', 'no_print')):
        conn.execute('INSERT INTO outcomes VALUES(?,?,?,?,?,?,?,?)',
                     (str(index), 'fomo', 'graduated', 'solana', 'S', reason, 'o', NOW - 10))
    conn.execute('INSERT INTO outcomes VALUES(?,?,?,?,?,?,?,?)',
                 ('old', 'fomo', 'graduated', 'solana', 'S', 'no_print', None, NOW - 7200))
    conn.commit()
    conn.close()
    folder = desk / 'candidate_emissions_intake'
    folder.mkdir()
    candidate = {'ticket_id': 'desk:U:intake:one', 'network_id': 'solana', 'mint': 'S',
                 'emitted_at': datetime.fromtimestamp(NOW - 10, timezone.utc).isoformat(),
                 'intake': {'feed': 'fomo', 'filter': 'graduated'},
                 'snapshot': {'fields': {'pool_state': {'value': {'map_source': 'intake_canonical_pda'}}}}}
    # Restart can re-append the same ticket. Expose lines and distinct tickets separately.
    (folder / '2026-10-27.jsonl').write_text(json.dumps(candidate) + '\n' + json.dumps(candidate) + '\n')
    emitter = S.build_report(tmp_path, now=NOW)['emitter']
    one = emitter['windows']['1h']['filters']['fomo']['graduated']
    assert one['outcomes'] == {'claimed': 1, 'emitted': 1, 'cooldown': 1, 'no_print': 1}
    assert one['emission_rows'] == 2 and one['emission_tickets'] == 1
    assert emitter['windows']['24h']['filters']['fomo']['graduated']['outcomes']['no_print'] == 2
    (folder / '2026-10-27.jsonl').write_text('{broken\n')
    bad = S.build_report(tmp_path, now=NOW)['emitter']
    assert bad['emission_source'] == 'unknown' and bad['errors']
    assert bad['windows']['1h']['filters']['fomo']['graduated']['emission_rows'] is None


def test_provider_graduation_and_local_mapping_are_separate(tmp_path):
    clock(tmp_path)
    poll(tmp_path, feed='fomo', filt='graduated', rows=[coin('mapped', venue=None), coin('other', venue=None)])
    folder = tmp_path / 'data/desk/pumpswap'
    folder.mkdir(parents=True)
    conn = sqlite3.connect(folder / 'tape.db')
    conn.execute('CREATE TABLE pools(mint TEXT,quote_mint TEXT,canonical INTEGER,received_at REAL)')
    conn.executemany('INSERT INTO pools VALUES(?,?,?,?)', [('mapped', S.WSOL, 1, NOW - 5),
                     ('other', 'wrong quote', 1, NOW - 5), ('other', S.WSOL, 0, NOW - 5)])
    conn.commit()
    conn.close()
    row = metric(S.build_report(tmp_path, now=NOW), feed='fomo', filt='graduated')
    assert row['locally_mapped'] == 1 and row['locally_mapped_share'] == .5
    assert row['provider_graduated'] == {'yes': 0, 'no': 0, 'unknown': 2}


def test_first_sighting_window_uses_corrected_clock_not_host(tmp_path):
    clock(tmp_path, 25200)
    poll(tmp_path, offset=25200, rows=[coin('s')])
    assert metric(S.build_report(tmp_path, now=NOW))['new_first_sightings'] == 1


def test_empty_emission_directory_is_not_evidence_of_zero_emissions(tmp_path):
    (tmp_path / 'data/desk/candidate_emissions_intake').mkdir(parents=True)
    emitter = S.build_report(tmp_path, now=NOW)['emitter']
    assert emitter['emission_source'] == 'not running'
    assert emitter['windows']['1h'] is None


@pytest.mark.parametrize('network,schema,map_source,completeness,known', [
    ('solana', 'desk-candidate/pumpswap-v1', 'intake_canonical_pda', 'partial', True),
    ('solana', 'desk-candidate/pumpswap-v1', 'intake_canonical_pda', 'complete', True),
    ('56', 'desk-candidate/pumpswap-v1', 'intake_canonical_pda', 'partial', False),
    ('solana', 'desk-candidate/v1', 'intake_canonical_pda', 'partial', False),
    ('solana', 'desk-candidate/pumpswap-v1', 'unproven', 'partial', False),
])
def test_intake_mapping_is_independent_of_mint_metadata(tmp_path, network, schema, map_source, completeness, known):
    clock(tmp_path)
    poll(tmp_path, rows=[coin('mapped', venue=None)])
    folder = tmp_path / 'data/desk/candidate_emissions_intake'
    folder.mkdir()
    candidate = dict(schema=schema, network_id=network, mint='mapped', ticket_id='t',
                     emitted_at=datetime.fromtimestamp(NOW - 5, timezone.utc).isoformat(),
                     intake={'feed': 'pumpfun', 'filter': 'live'},
                     snapshot={'fields': {'pool_state': {'completeness': completeness, 'value': {
                         'map_source': map_source, 'supply_atomic': None, 'base_decimals': None,
                         'metadata_known_at': None, 'metadata_slot': None, 'metadata_source': None}}}})
    (folder / '2026-10-27.jsonl').write_text(json.dumps(candidate) + '\n')
    report = S.build_report(tmp_path, now=NOW)
    assert report['mapping']['source'] == ('available' if known else 'not running')
    assert metric(report)['locally_mapped'] == (1 if known else None)
