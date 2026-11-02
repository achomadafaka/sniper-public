import os
import time
import pytest
from sniper.services import memecoin_desk_keep_extend as KE
from test_desk_ps_keep_window import make_worker
POOL='8'*43

@pytest.mark.parametrize('bad',['missing','stale','malformed'])
def test_expired_membership_ends_even_when_extension_file_unavailable(make_worker,bad):
    w=make_worker('1260');now=time.time()
    KE.write(str(w.ps_ext_path),{POOL:now+10},now);w.keep_extend_tick(now)
    if bad=='missing':os.unlink(w.ps_ext_path)
    elif bad=='stale':KE.write(str(w.ps_ext_path),{},now-KE.FRESH_S-1)
    else:w.ps_ext_path.write_text('{broken')
    w.keep_extend_tick(now+11)
    assert POOL not in w.ps_extended
    assert w.files.conn('ps_pool_selection',now).execute('SELECT end_at FROM ps_pool_selection WHERE pool=?',(POOL,)).fetchone()[0] is not None

def test_extension_drop_does_not_close_feed_owned_selection(make_worker):
    w=make_worker('1260');now=time.time()
    c=w.files.conn('ps_pool_selection',now)
    c.execute('INSERT INTO ps_pool_selection VALUES(?,?,?,?,?)',(POOL,w.ps_coverage_session,now-5,None,1))
    w.ps_selected={POOL}
    KE.write(str(w.ps_ext_path),{POOL:now+100},now);w.keep_extend_tick(now)
    KE.write(str(w.ps_ext_path),{},now+1);w.keep_extend_tick(now+1)
    assert c.execute('SELECT end_at FROM ps_pool_selection WHERE pool=? AND start_at=?',(POOL,now-5)).fetchone()[0] is None

def test_extension_coverage_joins_real_capture_session(make_worker,monkeypatch):
    from sniper.services import memecoin_desk_ps_losses as PSL
    w=make_worker('1260');now=time.time()
    KE.write(str(w.ps_ext_path),{POOL:now+100},now);w.keep_extend_tick(now)
    c=w.files.conn('ps_capture_intervals',now)
    c.execute('INSERT INTO ps_capture_intervals VALUES(?,?,?,?)',(w.ps_coverage_session,now-1,now+10,PSL.PROTOCOL))
    for conn in w.files._open.values():conn.commit()
    monkeypatch.setattr(PSL,'losses',lambda *a,**k:[])
    assert PSL.pool_coverage(w.base,POOL,now,now+5)==[(now,now+5)]

def test_pressure_rollback_preserves_later_nonextension_anchor(make_worker,monkeypatch):
    from test_desk_ps_keep_window import anchor
    w=make_worker('1260');now=time.time()
    c=anchor(w,monkeypatch,now-30,pool=POOL)
    KE.write(str(w.ps_ext_path),{POOL:now+3700},now);w.keep_extend_tick(now)
    w.keep_window(c,POOL,now+10,'second',12)
    ordinary_end=now+10+w.ps_keep_window_s-60
    w.keep_extend_shed(.81*float(w.cfg['ps_hard_gb']),500.,now+11)
    assert c.execute('SELECT keep_until FROM ps_keep WHERE pool=?',(POOL,)).fetchone()[0]>=ordinary_end
