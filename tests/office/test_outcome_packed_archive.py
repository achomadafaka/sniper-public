"""Real zstd and real sqlite: a packed archive day reads like a plain one, and is never silently missed."""
import shutil
import sqlite3
import subprocess

import pytest

from scripts import office_outcome_table as W

pytestmark = pytest.mark.skipif(shutil.which('zstd') is None, reason='zstd missing')


def day_file(path, pool='P', mint='M', creator='C', at=5.):
    c = sqlite3.connect(path)
    c.execute('CREATE TABLE swaps_ps(pool TEXT,mint TEXT,coin_creator TEXT,received_at REAL,slot INTEGER DEFAULT 1)')
    c.execute('CREATE TABLE ps_keep(pool TEXT,keep_from REAL,keep_until REAL)')
    c.execute('INSERT INTO swaps_ps(pool,mint,coin_creator,received_at) VALUES(?,?,?,?)', (pool, mint, creator, at))
    c.execute('INSERT INTO ps_keep VALUES(?,1.,9.)', (pool,))
    c.commit(); c.close()


def pack(path):
    subprocess.run(['zstd', '-qf', '--rm', str(path)], check=True)


@pytest.fixture
def world(tmp_path):
    arch = tmp_path / 'arch'; arch.mkdir()
    day_file(arch / 'keep-2026-10-01.db', mint='old'); pack(arch / 'keep-2026-10-01.db')
    day_file(arch / 'keep-2026-10-30.db', mint='new'); pack(arch / 'keep-2026-10-30.db')
    day_file(arch / 'keep-2026-10-31.db', mint='plain')
    desk = tmp_path / 'desk'; (desk / 'launches').mkdir(parents=True)
    return arch, desk, tmp_path


def test_days_list_both_forms_and_refuse_a_day_in_both(world):
    arch, _, _ = world
    assert W.archive_days(arch) == [('2026-10-01', True), ('2026-10-30', True), ('2026-10-31', False)]
    day_file(arch / 'keep-2026-10-30.db')
    with pytest.raises(ValueError, match='archive_day_ambiguous'): W.archive_days(arch)


def test_creators_read_packed_days_once_and_plain_ones_always(world):
    arch, desk, tmp = world
    seen = set()
    first = W.creator_rows(desk, arch, tmp / 'absent.db', unavailable=[], packed_read=seen, scratch=tmp / 's')
    assert {r[1] for r in first if r[3] == 'ps_archive'} == {'old', 'new', 'plain'} and seen == {'2026-10-01', '2026-10-30'}
    again = W.creator_rows(desk, arch, tmp / 'absent.db', unavailable=[], packed_read=seen, scratch=tmp / 's')
    assert {r[1] for r in again if r[3] == 'ps_archive'} == {'plain'}


def test_a_broken_packed_day_is_named_and_not_marked_read(world):
    arch, desk, tmp = world
    (arch / 'keep-2026-10-01.db.zst').write_bytes(b'not zstd')
    seen, gone = set(), []
    W.creator_rows(desk, arch, tmp / 'absent.db', unavailable=gone, packed_read=seen, scratch=tmp / 's')
    assert 'keep-2026-10-01' in gone and '2026-10-01' not in seen


def test_pending_window_reads_packed_days_from_the_floor_and_names_what_it_skips(world):
    arch, _, tmp = world
    ps = W.PumpSwap(tmp / 'desk', arch, tmp / 'run', {'new': 'P'}, packed_floor='2026-10-29')
    built = ps.archive
    assert ps.packed_skipped == ['2026-10-01'] and len(built) == 2
    assert sqlite3.connect(built[0][0]).execute('SELECT mint FROM swaps_ps').fetchone()[0] == 'new'
    full = W.PumpSwap(tmp / 'desk', arch, tmp / 'run2', {'new': 'P'})
    assert len(full.archive) == 3 and full.packed_skipped == []    # no floor, as the pass replay runs: every day
