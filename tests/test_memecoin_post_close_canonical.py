"""Post-close observations require canonical identity and chain ordering."""
import sqlite3

import pytest

from test_memecoin_evm_post_close_marks import (
    CHAIN, CLOSED_AT, MINT, SIG, _ledger, _stores,
)
from sniper.services.memecoin_evm_post_close_marks import evm_post_close_marks


def _canonical(tape_path):
    c = sqlite3.connect(tape_path)
    c.execute('CREATE TABLE IF NOT EXISTS block_times (chain_id TEXT, block_no INTEGER, '
              'block_ts TEXT, fetched_at TEXT, block_hash TEXT, PRIMARY KEY(chain_id,block_no))')
    for i, at in c.execute('SELECT id, observed_at FROM pool_ticks').fetchall():
        block = 100 + i
        c.execute('UPDATE pool_ticks SET block_no=?, log_index=?, block_hash=? WHERE id=?',
                  (block, i, f'hash{block}', i))
        c.execute('INSERT OR REPLACE INTO block_times VALUES(?,?,?,?,?)',
                  (CHAIN, block, at, at, f'hash{block}'))
    c.commit()
    return c


@pytest.mark.parametrize('defect', ['orphan', 'late_old', 'missing_hash', 'future'])
def test_post_close_ignores_noncanonical_or_out_of_order_price(tmp_path, defect):
    ledger, _ = _ledger()
    ticks = [(SIG, CLOSED_AT, 10**30),
             ('good', '2026-09-23T20:46:49+00:00', 10**30),
             ('bad', '2026-09-23T20:46:50+00:00', 5*10**29)]
    tape_path, bind_path = _stores(tmp_path, ticks=ticks)
    c = _canonical(tape_path)
    if defect == 'orphan':
        c.execute("UPDATE block_times SET block_hash='different' WHERE block_no=103")
    elif defect == 'late_old':
        c.execute('UPDATE pool_ticks SET block_no=99 WHERE id=3')
        c.execute("INSERT INTO block_times VALUES(?,99,?,?,?)",
                  (CHAIN, '2026-09-23T20:46:40+00:00', CLOSED_AT, 'hash103'))
    elif defect == 'missing_hash':
        c.execute('UPDATE pool_ticks SET block_hash=NULL WHERE id=3')
    else:
        c.execute("UPDATE block_times SET block_ts='2026-09-23T20:47:30+00:00' WHERE block_no=103")
    c.commit(); c.close()
    result = evm_post_close_marks(ledger, now_iso='2026-09-23T20:47:00+00:00',
                                  tape_path=tape_path, bind_path=bind_path)
    expected = ledger.execute('SELECT price FROM memecoin_fills').fetchone()[0]
    assert result[(CHAIN, MINT)]['price'] == pytest.approx(expected)


def test_post_close_stamp_is_block_time_not_late_receipt(tmp_path):
    ledger, _ = _ledger()
    tape_path, bind_path = _stores(tmp_path, ticks=[
        (SIG, CLOSED_AT, 10**30),
        ('next', '2026-09-23T20:46:59+00:00', 5*10**29)])
    c = _canonical(tape_path)
    c.execute("UPDATE block_times SET block_ts='2026-09-23T20:46:49+00:00' WHERE block_no=102")
    c.commit(); c.close()
    result = evm_post_close_marks(ledger, now_iso='2026-09-23T20:47:00+00:00',
                                  tape_path=tape_path, bind_path=bind_path)
    assert result[(CHAIN, MINT)]['observed_at'] == '2026-09-23T20:46:49+00:00'


def test_unknown_canonical_identity_has_no_post_close_price(tmp_path):
    ledger, _ = _ledger()
    tape_path, bind_path = _stores(tmp_path, ticks=[(SIG, CLOSED_AT, 10**30)])
    c = _canonical(tape_path)
    c.execute('UPDATE block_times SET block_hash=NULL')
    c.commit(); c.close()
    assert evm_post_close_marks(ledger, now_iso='2026-09-23T20:47:00+00:00',
                                tape_path=tape_path, bind_path=bind_path) == {}


def test_late_inserted_earlier_log_in_same_block_is_not_latest(tmp_path):
    ledger, _ = _ledger()
    tape_path, bind_path = _stores(tmp_path, ticks=[
        (SIG, CLOSED_AT, 10**30), ('later', CLOSED_AT, 10**30),
        ('earlier', CLOSED_AT, 5*10**29)])
    c = _canonical(tape_path)
    c.execute("UPDATE pool_ticks SET block_no=101,block_hash='hash101'")
    c.execute('UPDATE pool_ticks SET log_index=0 WHERE id=3')
    c.commit(); c.close()
    result = evm_post_close_marks(ledger, now_iso='2026-09-23T20:47:00+00:00',
                                  tape_path=tape_path, bind_path=bind_path)
    assert result[(CHAIN, MINT)]['price'] == pytest.approx(
        ledger.execute('SELECT price FROM memecoin_fills').fetchone()[0])


def test_exit_anchor_uses_closing_signature_not_last_inserted_fill():
    from sniper.services.memecoin_evm_post_close_marks import _exit_fill
    ledger, pid = _ledger()
    expected = _exit_fill(ledger, pid, SIG)
    ledger.execute("INSERT INTO memecoin_fills(position_id,kind,price,qty,notional,tx_sig,"
                   "network_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                   (pid, 'derisk', 99, 1, 99, 'older-signature', CHAIN,
                    '2026-09-23T20:44:00+00:00'))
    assert _exit_fill(ledger, pid, SIG) == expected


def test_nonfinite_exit_price_is_unavailable():
    from sniper.services.memecoin_evm_post_close_marks import _exit_fill
    ledger, pid = _ledger()
    ledger.execute('UPDATE memecoin_fills SET price=?', (float('inf'),))
    assert _exit_fill(ledger, pid, SIG) == (None, None)


def test_repeated_close_uses_newest_available_anchor(tmp_path, monkeypatch):
    from sniper.services import memecoin_evm_post_close_marks as post
    from sniper.persistence import db_memecoin as m
    ledger, _ = _ledger()
    tape, bind = _stores(tmp_path, ticks=[])
    monkeypatch.setattr(m, 'recently_closed_real_positions', lambda *a, **kw: [
        {'id': 2, 'network_id': CHAIN, 'mint': MINT},
        {'id': 1, 'network_id': CHAIN, 'mint': MINT}])
    monkeypatch.setattr(post, '_position_mark', lambda c, b, t, pos, *args: {
        'price': pos['id'], 'observed_at': CLOSED_AT, 'source': post.SOURCE})
    result = post.evm_post_close_marks(ledger, now_iso=CLOSED_AT, tape_path=tape, bind_path=bind)
    assert result[(CHAIN, MINT)]['price'] == 2
