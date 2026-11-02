import pytest
from test_memecoin_desk_intake_book import sight,book,printed,MINT,NOW


def test_durable_claim_and_deferral_ticket_time_are_atomic(tmp_path,monkeypatch):
    sight(tmp_path,NOW-30)
    b=book(tmp_path);b.reload(NOW);b.fx_deferred(MINT,NOW-2)
    original=b._outcome
    def crash(*args):
        if args[2]=='claimed':raise RuntimeError('crash after claim commit')
        return original(*args)
    monkeypatch.setattr(b,'_outcome',crash)
    with pytest.raises(RuntimeError):printed(b,NOW)
    assert b.db.execute('SELECT COUNT(*) FROM claims').fetchone()[0]==1
    assert b.db.execute('SELECT first_ticket_at FROM fx_deferrals').fetchone()[0]==NOW


def test_failed_deferral_update_rolls_back_claim(tmp_path):
    import sqlite3
    sight(tmp_path,NOW-30)
    b=book(tmp_path);b.reload(NOW);b.fx_deferred(MINT,NOW-2)
    b.db.execute("CREATE TEMP TRIGGER refuse_fx_update BEFORE UPDATE ON fx_deferrals BEGIN SELECT RAISE(ABORT,'fixture update failed'); END")
    with pytest.raises(sqlite3.IntegrityError):printed(b,NOW)
    assert b.db.execute('SELECT COUNT(*) FROM claims').fetchone()[0]==0
    assert b.db.execute('SELECT first_ticket_at FROM fx_deferrals').fetchone()[0] is None
