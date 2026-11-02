"""Explicit decimal and causal cuts for new PumpSwap facts; no live tape."""
import sqlite3

import pytest

from sniper.services import memecoin_desk_ps_tape as PT
from test_desk_ps_tape import MINT, NOW, POOL, ROWS, SOL, ps_tape


def test_validated_nine_decimals_change_whole_token_price_not_atomic_flow(tmp_path):
    ps_tape(tmp_path, ROWS)
    six = PT.load_ps_swaps(tmp_path, MINT, NOW - 20, NOW, SOL, base_decimals=6)
    nine = PT.load_ps_swaps(tmp_path, MINT, NOW - 20, NOW, SOL, base_decimals=9)
    assert nine[0]["price_usd"] == pytest.approx(six[0]["price_usd"] * 1000)
    assert nine[0]["token_atomic"] == six[0]["token_atomic"]
    assert nine[0]["usd"] == six[0]["usd"]


def test_unknown_decimals_preserve_flow_but_never_invent_absolute_price(tmp_path):
    ps_tape(tmp_path, ROWS)
    rows = PT.load_ps_swaps(tmp_path, MINT, NOW - 20, NOW, SOL, base_decimals=None)
    assert len(rows) == 4 and rows[0]["usd"] == 50
    assert all(r["price_usd"] is None and r["price_sol"] is None for r in rows)


@pytest.mark.parametrize("decimals", [-1, 19, True, 6.0, "6"])
def test_invalid_decimals_refuse(tmp_path, decimals):
    ps_tape(tmp_path, ROWS)
    with pytest.raises(PT.TapeUnreadable, match="decimals"):
        PT.load_ps_swaps(tmp_path, MINT, NOW - 20, NOW, SOL, base_decimals=decimals)


def test_future_slot_cannot_price_the_last_sell(tmp_path):
    rows = ROWS[:2] + [(-4, 2.1, 0, 20, "seller", 180, POOL, PT.WSOL_MINT),
                       (-3, 9.0, 1, 10, "future", 200, POOL, PT.WSOL_MINT)]
    ps_tape(tmp_path, rows)
    kept = PT.load_ps_swaps(tmp_path, MINT, NOW - 20, NOW, SOL, cut_slot=190)
    assert [r["slot"] for r in kept] == [170, 175, 180]
    assert kept[-1]["price_usd"] is None


def test_pre_window_virtual_quote_seed_cannot_cross_the_slot_cut(tmp_path):
    ps_tape(tmp_path, [(-12, 2.0, 1, 50, "future", 999, POOL, PT.WSOL_MINT),
                       (-4, 2.2, 1, 60, "past", 10, POOL, PT.WSOL_MINT)], proof=500_000_000)
    rows = PT.load_ps_swaps(tmp_path, MINT, NOW - 5, NOW, SOL, cut_slot=20)
    assert len(rows) == 1 and rows[0]["price_usd"] is None
    with sqlite3.connect(PT.tape_path(tmp_path)) as c:
        c.execute("UPDATE swaps_ps SET slot=1 WHERE signature='ps0'")
    assert PT.load_ps_swaps(tmp_path, MINT, NOW - 5, NOW, SOL, cut_slot=20)[0]["price_usd"] is not None


def test_bounded_reader_refuses_overflow_instead_of_truncating_flow(tmp_path):
    ps_tape(tmp_path, ROWS)
    with pytest.raises(PT.TapeUnreadable, match="row_limit"):
        PT.load_ps_swaps(tmp_path, MINT, NOW - 20, NOW, SOL, max_rows=3)


def test_loss_before_window_invalidates_seed_until_an_independent_proof(tmp_path):
    from sniper.services import memecoin_desk_ps_losses as L
    from test_desk_ps_loss_scope import receipt, batch
    ps_tape(tmp_path, [(-12, 2., 1, 50, 'seed', 1, POOL, PT.WSOL_MINT),
                       (-4, 2.2, 1, 60, 'past', 10, POOL, PT.WSOL_MINT),
                       (-2, 2.4, 1, 60, 'new', 11, POOL, PT.WSOL_MINT)], proof=500_000_000)
    with sqlite3.connect(PT.tape_path(tmp_path)) as c:
        for ddl in L.SCHEMAS.values(): c.execute(ddl)
        for ddls in L.INDEXES.values():
            for ddl in ddls: c.execute(ddl)
        c.execute('CREATE TABLE ps_decode_losses(signature TEXT,cause TEXT,slot INTEGER,received_at REAL, '
                  'PRIMARY KEY(signature,cause)) WITHOUT ROWID')
        c.execute('CREATE INDEX ps_decode_losses_received ON ps_decode_losses(received_at)')
        c.execute('CREATE INDEX gaps_ps_at ON gaps_ps(at)')
        receipt(c, at=NOW-6, pools=[POOL]); batch(c, at=NOW-6)
    rows = PT.load_ps_swaps(tmp_path, MINT, NOW-5, NOW, SOL, cut_slot=20, integrity=True)
    assert all(r['price_usd'] is None for r in rows)
    with sqlite3.connect(PT.tape_path(tmp_path)) as c:
        payload = c.execute("SELECT payload_json FROM swaps_ps WHERE signature='ps0'").fetchone()[0]
        c.execute("UPDATE swaps_ps SET payload_json=? WHERE signature='ps2'", (payload,))
    rows = PT.load_ps_swaps(tmp_path, MINT, NOW-5, NOW, SOL, cut_slot=20, integrity=True)
    assert rows[0]['price_usd'] is None and rows[1]['price_usd'] is not None
    assert rows[1]['virtual_quote_proof']['received_at'] == NOW-2


def test_equal_cut_slot_next_witness_cannot_price_retained_sell(tmp_path):
    ps_tape(tmp_path, ROWS)
    rows = PT.load_ps_swaps(tmp_path, MINT, NOW-20, NOW, SOL, cut_slot=181)
    assert [r['slot'] for r in rows] == [170,175,180]
    assert rows[-1]['price_sol'] is None


def test_seed_bridge_foreign_proof_keeps_original_frozen_read_cut(tmp_path):
    import json
    from sniper.services import memecoin_desk_ps_losses as L
    from test_desk_ps_loss_scope import receipt, batch
    from test_memecoin_desk_pumpswap import ACTOR
    ps_tape(tmp_path,[(-12,2.,1,50,'seed',1,POOL,PT.WSOL_MINT),
                      (-4,2.2,1,60,'past',10,POOL,PT.WSOL_MINT),
                      (-2,2.4,1,60,'new',11,POOL,PT.WSOL_MINT)],proof=500_000_000)
    with sqlite3.connect(PT.tape_path(tmp_path)) as c:
        for ddl in L.SCHEMAS.values():c.execute(ddl)
        for ddls in L.INDEXES.values():
            for ddl in ddls:c.execute(ddl)
        c.execute('CREATE TABLE ps_decode_losses(signature TEXT,cause TEXT,slot INTEGER,received_at REAL,'
                  'PRIMARY KEY(signature,cause)) WITHOUT ROWID')
        c.execute('CREATE INDEX ps_decode_losses_received ON ps_decode_losses(received_at)')
        c.execute('CREATE INDEX gaps_ps_at ON gaps_ps(at)')
        receipt(c,at=NOW-6,pools=[ACTOR],scope='unknown');batch(c,at=NOW-6)
        proof=L.pool_key_record([ACTOR],signature='one',cause='decode_loss',received_at=NOW-6,known_at=NOW-4,slot=10)
        c.execute('INSERT INTO ps_loss_scope VALUES(?,?,?,?,?,?,?)',
                  ('one','decode_loss',json.dumps(proof),1,proof['proof'],proof['decoder_version'],proof['known_at']))
    rows=PT.load_ps_swaps(tmp_path,MINT,NOW-5,NOW,SOL,cut_slot=20,integrity=True,loss_reads={})
    assert all(r['price_usd'] is not None for r in rows)
