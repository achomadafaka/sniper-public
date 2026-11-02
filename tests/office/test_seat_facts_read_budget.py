"""run_up runs the real query: its own read limit reads as that, a broken tape as unreadable."""
import sqlite3

from sniper.office import seat_facts as F


def tape(tmp_path, rows=3):
    (tmp_path / 'pumpswap').mkdir()
    c = sqlite3.connect(tmp_path / 'pumpswap/tape.db')
    c.execute('CREATE TABLE swaps_ps(pool TEXT,received_at REAL,actor TEXT,is_buy INTEGER,quote_atomic TEXT,post_base_reserve_atomic TEXT,post_quote_reserve_atomic TEXT,payload_json TEXT)')
    c.execute('CREATE INDEX swaps_ps_pool_received ON swaps_ps(pool,received_at)')
    c.executemany('INSERT INTO swaps_ps VALUES(?,?,?,?,?,?,?,?)', [('P', float(i), 'a', 1, '1', '10', '10', '{}') for i in range(rows)])
    c.commit(); c.close()
    return tmp_path


def test_a_read_past_the_budget_says_so(tmp_path, monkeypatch):
    d = tape(tmp_path, 5000)
    monkeypatch.setattr(F, 'READ_BUDGET_S', -1.0)
    out = F.run_up(d, 'P', 1e6)
    assert out['state'] == 'unknown' and out['reason'] == 'read_budget_exceeded'


def test_a_missing_tape_stays_unreadable(tmp_path):
    assert F.run_up(tmp_path / 'absent', 'P', 1e6)['reason'] == 'tape_unreadable'


def test_a_normal_read_is_not_a_budget_refusal(tmp_path):
    assert F.run_up(tape(tmp_path), 'P', 1e6)['reason'] == 'no_priced_tape'
