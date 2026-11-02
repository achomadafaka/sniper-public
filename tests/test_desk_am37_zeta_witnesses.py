"""Zeta HOLD 418dbcabd witnesses (verbatim from ~/.cache/codex/zeta-am37-418dbcabd/tests/test_zeta_fx_denominator.py)."""
from test_memecoin_desk_intake_book import _ps_worker, facts, event, NOW, SIG, MINT


def test_consumed_listing_is_not_an_otherwise_eligible_ticket(tmp_path, monkeypatch):
    worker=_ps_worker(tmp_path,monkeypatch)
    # on_print leaves this legitimate state after consuming every sighting for a mint.
    worker.intake_book.listed={MINT:{}}
    worker.pool_metadata=lambda *a:None
    worker.sol_usd,worker.sol_at=100.,NOW+10
    worker.sol_prev=(None,None)
    mapping,_=facts()
    worker.intake_hook(event(),mapping,10,SIG,0,None,NOW+20,NOW)
    assert worker.stats['otherwise_eligible']==0
    assert worker.stats['fx_after_cut']==0


def test_migrated_claims_accepts_previous_release_writer(tmp_path):
    from test_memecoin_desk_intake_book import book
    b=book(tmp_path)
    b.db.execute('INSERT OR IGNORE INTO claims VALUES (?,?,?,?,?,?,?,?,?,?)',
                 ('id','pumpfun','active','solana','mint','ref',100.,'{}','hash','pending'))
