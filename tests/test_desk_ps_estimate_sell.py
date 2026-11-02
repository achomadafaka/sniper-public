"""estimate_sell skips the loss proof, never the identity of the swap it values."""
import json,sqlite3
import pytest
from sniper.services import memecoin_desk_ps_asof as A,memecoin_desk_ps_paper as P,memecoin_desk_ps_tape as T

MINT='m';AT=1000.


def tip(status='mapped',mint=MINT,quote=T.WSOL_MINT,received=AT-5,payload='proved'):
    return (1,'sig',0,1,0,'1000000','1000000',20,5,5,received,1.,status,mint,quote,payload,0)


@pytest.fixture
def rig(monkeypatch):
    conn=sqlite3.connect(':memory:');conn.execute('CREATE TABLE ps_decode_losses(received_at REAL)')
    conn.execute('CREATE TABLE pools(pool TEXT,mint TEXT,quote_mint TEXT,canonical INTEGER,known_at REAL)');conn.execute('INSERT INTO pools VALUES(?,?,?,1,1.)',('P',MINT,T.WSOL_MINT))
    conn.execute('CREATE TABLE swaps_ps(signature TEXT,ordinal INTEGER,slot INTEGER,pool TEXT,mint TEXT,quote_mint TEXT,map_status TEXT,map_known_at REAL,UNIQUE(signature,ordinal))');conn.execute("INSERT INTO swaps_ps VALUES('buy',1,1,'P',?,?,'mapped',1.)",(MINT,T.WSOL_MINT))
    state=dict(tip=tip(),conn=conn)
    class Keep:
        def __getattr__(self,name):return getattr(conn,name)
        def close(self):pass
    monkeypatch.setattr(A,'_ro',lambda p:Keep())
    monkeypatch.setattr(T,'tape_path',lambda d:'x');monkeypatch.setattr(T,'canonical_pool',lambda m:'P');monkeypatch.setattr(T,'retained_from',lambda *a:0.)
    monkeypatch.setattr(T,'window_rows',lambda *a,**k:[state['tip']])
    monkeypatch.setattr(T,'_proof',lambda payload:5 if payload=='proved' else None)
    monkeypatch.setattr(T,'_proof_before',lambda *a,**k:dict(value=5,received_at=AT-30,signature='buy',slot=1,ordinal=1))
    return state


def read():
    return P.estimate_sell('d',MINT,1000,AT)


@pytest.mark.parametrize('bad',[tip(mint='other'),tip(quote='NOTSOL'),tip(status='mystery'),tip(status='unmapped')])
def test_a_swap_of_another_coin_or_quote_prices_nothing(rig,bad):
    rig['tip']=bad
    assert read()==(None,'estimate_identity_unknown')


def test_a_proved_swap_is_estimated_and_a_sell_tip_is_carried(rig):
    assert read()[0]['binding_kind']=='estimated'
    rig['tip']=tip(payload='sell')
    out=read()[0];assert out['binding_kind']=='carried' and out['proof']['carried_age_s']==30. and out['proof']['quote_source_signature']=='buy'
    rig['conn'].execute('DELETE FROM pools')
    rig['tip']=tip(status='unmapped',mint=None,quote=None)
    assert read()[0] is not None


def test_a_stale_tip_is_refused(rig):
    rig['tip']=tip(received=AT-61)
    assert read()==(None,'estimate_tip_stale')


def test_bounds_run_to_the_observation_and_both_ages_are_recorded(rig):
    rig['tip']=tip(received=AT-50)
    assert P.estimate_sell('d',MINT,1000,AT,observed_at=AT+11)==(None,'estimate_tip_stale')
    out=P.estimate_sell('d',MINT,1000,AT,observed_at=AT+9)[0]['proof']
    assert out['newest_swap_age_s']==50. and out['newest_swap_age_at_observation_s']==59.


def test_a_map_learned_after_the_swap_is_refused(rig):
    rig['conn'].execute('UPDATE pools SET known_at=1100.')
    assert read()==(None,'estimate_identity_unknown')


def test_a_tip_newer_than_the_cut_is_refused(rig):
    rig['tip']=tip(received=1100.)
    assert read()==(None,'estimate_tip_stale')


def test_a_carry_from_another_coin_is_refused(rig):
    rig['tip']=tip(payload='sell')
    rig['conn'].execute("UPDATE swaps_ps SET mint='other'")
    assert read()==(None,'estimate_identity_unknown')


def test_the_carried_source_is_the_exact_leg_not_another_in_its_transaction(rig):
    rig['tip']=tip(payload='sell')
    rig['conn'].execute("UPDATE swaps_ps SET mint='other'")
    rig['conn'].execute("INSERT INTO swaps_ps VALUES('buy',0,1,'P',?,?,'mapped',1.)",(MINT,T.WSOL_MINT))
    assert read()==(None,'estimate_identity_unknown')
