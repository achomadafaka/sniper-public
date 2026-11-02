import sqlite3
from office.test_office_v2_seat_db import D,NOW,desk,_intel_rows

def test_zeta_uppercase_intel_keeps_rows(desk,tmp_path):
    _intel_rows(tmp_path)
    a=D.read(desk,'SELECT count(*) FROM x_posts',now=NOW)
    b=D.read(desk,'SELECT count(*) FROM X_POSTS',now=NOW)
    assert b['rows']==a['rows'], (a,b)

def test_zeta_all_external_text_fields_bounded(desk,tmp_path):
    _intel_rows(tmp_path)
    f=tmp_path/D.SOURCES['x'].replace('????-??-??','2026-10-30')
    c=sqlite3.connect(f);c.execute('UPDATE posts SET handle=?',('h'*1500,));c.commit();c.close()
    out=D.read(desk,'SELECT handle FROM x_posts LIMIT 1',now=NOW)
    assert len(out['rows'][0][0].encode())<=1000

def test_open_failure_partition_is_not_silently_complete(desk,tmp_path,monkeypatch):
    import shutil
    _intel_rows(tmp_path)
    newer=tmp_path/D.SOURCES['x'].replace('????-??-??','2026-10-30')
    older=newer.with_name('2026-10-29.db');shutil.copyfile(newer,older)
    real=sqlite3.connect
    def broken(path,*a,**kw):
        if '2026-10-29.db' in str(path):raise sqlite3.OperationalError('cannot open partition')
        return real(path,*a,**kw)
    monkeypatch.setattr(sqlite3,'connect',broken)
    out=D.read(desk,'SELECT count(*) FROM x_posts',now=NOW)
    assert out['coverage']['x_posts']['state'] in ('partial_source','unavailable'),out

def test_compact_coverage_keeps_sql_failures():
    class BadCoverage:
        def execute(self,*a):raise sqlite3.OperationalError('coverage failed')
    out=D._intel_coverage('x_posts',[BadCoverage()],'received_at',NOW,None,with_note=False)
    assert out.get('coverage_errors'),out
