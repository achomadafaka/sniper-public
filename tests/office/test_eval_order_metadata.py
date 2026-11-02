"""Display evidence is nullable and cannot replace fill accounting or admission."""
from pathlib import Path
import sqlite3

import pytest

from sniper.office import ledger as L
from office.test_ledger import book, policy, NOW, FX, MINT, manage


@pytest.mark.parametrize('decimals',[0,6,18,None])
def test_verified_metadata_is_persisted_and_fill_price_uses_whole_tokens(book,decimals):
    conn,quotes,_=book
    result=L.submit(conn,policy(),book_id='A',order_id='metadata',network_id='solana',mint=MINT,pool='P',
        decision_bundle_hash='b'*64,now=NOW,sol_usd=FX,sol_at=NOW,
        metadata={'ticker':'TEST','decimals':decimals,'metadata_known_at':NOW-1})
    assert result.state=='pending'
    row=conn.execute("SELECT * FROM orders WHERE order_id='metadata'").fetchone()
    assert row['ticker']=='TEST' and row['decimals']==decimals and row['metadata_known_at']==NOW-1
    assert row['entry_unit_price_usd'] is None
    manage(conn,policy(),NOW+L.ENTRY_DELAY_S+1)
    row=conn.execute("SELECT * FROM orders WHERE order_id='metadata'").fetchone()
    assert row['state']=='open'
    if decimals is None:assert row['entry_unit_price_usd'] is None
    else:
        expected=row['spend_usd']*10**decimals/int(row['tokens_atomic'])
        assert row['entry_unit_price_usd']==pytest.approx(expected)


def test_old_callers_get_unknown_metadata_and_replay_does_not_replace_it(book):
    conn,quotes,_=book
    kwargs=dict(book_id='A',order_id='legacy',network_id='solana',mint=MINT,pool='P',
                decision_bundle_hash='b'*64,now=NOW,sol_usd=FX,sol_at=NOW)
    assert L.submit(conn,policy(),**kwargs).state=='pending'
    assert L.submit(conn,policy(),**kwargs,metadata={'ticker':'FORGED','decimals':9,'metadata_known_at':NOW}).state=='pending'
    row=conn.execute("SELECT * FROM orders WHERE order_id='legacy'").fetchone()
    assert all(row[k] is None for k in ('ticker','decimals','metadata_known_at','entry_unit_price_usd'))


def test_invalid_metadata_does_not_become_zero_or_change_order_amount(book):
    conn,quotes,_=book
    got=L.submit(conn,policy(),book_id='A',order_id='unknown',network_id='solana',mint=MINT,pool='P',
        decision_bundle_hash='b'*64,now=NOW,sol_usd=FX,sol_at=NOW,
        metadata={'ticker':False,'decimals':True,'metadata_known_at':NOW+1})
    assert got.state=='pending'
    row=conn.execute("SELECT * FROM orders WHERE order_id='unknown'").fetchone()
    assert row['ticker'] is None and row['decimals'] is None and row['metadata_known_at'] is None
    manage(conn,policy(),NOW+L.ENTRY_DELAY_S+1)
    row=conn.execute("SELECT * FROM orders WHERE order_id='unknown'").fetchone()
    assert row['spend_usd']==30. and row['entry_unit_price_usd'] is None


def test_existing_ledger_migrates_additively_and_reopens_idempotently(tmp_path):
    path=tmp_path/'legacy.db'
    conn=sqlite3.connect(path)
    old=L.SCHEMA.replace(',\n    ticker TEXT, decimals INTEGER, metadata_known_at REAL, entry_unit_price_usd REAL, exit_by TEXT,\n    epoch_halted_at REAL, post_halt INTEGER','')
    conn.executescript(old)
    assert 'decimals' not in {r[1] for r in conn.execute('PRAGMA table_info(orders)')}
    conn.close()
    for _ in range(2):
        conn=L.open_ledger(path)
        assert {'ticker','decimals','entry_unit_price_usd','metadata_known_at','exit_by'} <= {
            r[1] for r in conn.execute('PRAGMA table_info(orders)')}
        conn.close()
