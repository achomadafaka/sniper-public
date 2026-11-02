"""Retention for every Office v2 writer. Bounded deletes, run hourly by the seat host. Spec section 12."""
from pathlib import Path
import re
import sqlite3
import subprocess

from . import quota_measurement as Q,search_broker as S

DAY=86400
EVENT_DAYS=14
LOG_DAYS=14
SESSION_DAYS=14
BATCH=5000
DEFAULT_CAP=5*1024**3


def _as_office(*argv,timeout=120):
    return subprocess.run(['sudo','-n','-u','office',*argv],capture_output=True,text=True,timeout=timeout,cwd='/')


def prune_events(conn,*,now):
    """The (corrected_at) index bounds this. NULL times stay until the epoch's export: unknown is not old."""
    return conn.execute('DELETE FROM office_v2_events WHERE seq IN (SELECT seq FROM office_v2_events WHERE corrected_at<? ORDER BY corrected_at LIMIT ?)',
                        (now-EVENT_DAYS*DAY,BATCH)).rowcount


def prune_logs(directory,*,now):
    """Whole days, by the date in the name. A name that does not parse is left alone."""
    import calendar,time
    removed=0
    for path in Path(directory).glob('*.jsonl'):
        match=re.search(r'-(\d{8})\.jsonl$',path.name)
        if not match:continue
        try:at=calendar.timegm(time.strptime(match.group(1),'%Y%m%d'))
        except ValueError:continue
        if at<now-LOG_DAYS*DAY:
            path.unlink();removed+=1
    return removed


def prune_sessions(state_dir,*,current=None):
    """Old session files, never the current one. A missing directory is fine before the first session; any other failure is reported."""
    root=str(Path(state_dir)/'.claude/projects')
    keep=['!','-name',current+'.jsonl'] if current else []
    try:
        # sudo itself can exit 1, so absence is a line printed by the command that ran, never a return code.
        present=_as_office('sh','-c','if [ -d "$1" ]; then echo present; else echo absent; fi','sh',root)
        answer=present.stdout.strip() if present.returncode==0 else None
        if answer=='absent':return True
        if answer!='present':return False
        return _as_office('find',root,'-type','f','-mtime','+%d'%SESSION_DAYS,*keep,'-delete').returncode==0
    except (OSError,subprocess.SubprocessError):return False


def session_exists(state_dir,session_id,*,family='claude'):
    """False only on a clean search that found nothing. Anything else raises: a failed read must not throw away a live session."""
    tree,name=('.codex/sessions','*'+session_id+'.jsonl') if family=='codex' else ('.claude/projects',session_id+'.jsonl')
    done=_as_office('find',str(Path(state_dir)/tree),'-name',name)
    if done.returncode:raise OSError('session_search_failed')
    return bool(done.stdout.strip())


def workspace_bytes(path):
    """None when it cannot be measured."""
    try:
        done=_as_office('du','-sb',str(path))
        used=int(done.stdout.split()[0]) if done.returncode==0 else None
        return used if used is not None and used>=0 else None
    except (OSError,subprocess.SubprocessError,IndexError,ValueError):return None


def over_cap(cfg):
    """Admission check between wakes, not a disk quota: a running wake can grow past the cap until it ends. Over cap or unmeasurable means no next wake."""
    cap=cfg.get('workspace_cap_bytes',DEFAULT_CAP)
    for path in (cfg['workspace'],cfg['state_dir']):
        used=workspace_bytes(path)
        if used is None:return 'workspace_size_unknown'
        if used>cap:return 'workspace_over_cap'
    return None


def prune_orphan_labels(store,ledger_path,*,now):
    """Attempts no receipt ever claimed. Off the order path, and only past a week, so a crash's retry still finds its own."""
    old=store.execute('SELECT order_id,decision_hash FROM office_v2_order_labels WHERE at<? LIMIT ?',(now-7*DAY,BATCH)).fetchall()
    ledger=sqlite3.connect(Path(ledger_path).resolve().as_uri()+'?mode=ro',uri=True,timeout=1.)
    try:
        # Only this batch's order ids, by the receipts' order id: never the whole table into memory.
        kept=set()
        ids=sorted({r[0] for r in old})
        for start in range(0,len(ids),500):
            chunk=ids[start:start+500]
            kept.update(r[0] for r in ledger.execute('SELECT judgements_hash FROM decision_receipts WHERE order_id IN (%s)'%','.join('?'*len(chunk)),chunk))
    finally:ledger.close()
    gone=[r for r in old if r[1] not in kept]
    with store:store.executemany('DELETE FROM office_v2_order_labels WHERE order_id=? AND decision_hash=?',gone)
    return len(gone)


def prune_snapshots(store,*,now):
    """Kept for the epoch's analysis window: until 30 days after it seals. An open epoch's rows are never touched."""
    return store.execute("DELETE FROM office_v2_book_snapshots WHERE rowid IN (SELECT s.rowid FROM office_v2_book_snapshots s JOIN epochs e ON e.id=s.epoch_id "
                         "WHERE json_extract(e.payload_json,'$.closed_at')<? LIMIT ?)",(now-30*DAY,BATCH)).rowcount


def prune_market(path,office,*,now):
    """Calls 30 days but never into an unexpired epoch, cache 1 day, fetches until 30 days after their epoch seals. Fetches carry no epoch id, so they go by when a seat saw them: before the newest epoch sealed over 30 days ago, and before the start of every epoch still open or sealed more recently. Any such epoch with no recorded start deletes no fetch."""
    closed=office.execute("SELECT max(json_extract(payload_json,'$.closed_at')) FROM epochs WHERE json_extract(payload_json,'$.closed_at')<?",(now-30*DAY,)).fetchone()[0]
    starts=[office.execute('SELECT min(at) FROM office_v2_changes WHERE epoch_id=?',(r[0],)).fetchone()[0]
            for r in office.execute("SELECT id FROM epochs WHERE json_extract(payload_json,'$.closed_at') IS NULL OR json_extract(payload_json,'$.closed_at')>=?",(now-30*DAY,)).fetchall()]
    cut=None if closed is None or any(x is None for x in starts) else min([closed]+starts)
    calls_cut=None if any(x is None for x in starts) else min([now-30*DAY]+starts)
    if cut is not None:   # the seats' own logs, on the same horizon as fetches; the inbox text they point at isn't pruned at all
        for table,column in (('office_v2_notifications','sent_at'),('office_v2_results_reads','last_at')):
            if office.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():
                office.execute('DELETE FROM %s WHERE rowid IN (SELECT rowid FROM %s WHERE %s<? LIMIT ?)'%(table,table,column),(cut,BATCH))
    chain=Path(path).with_name('chain.db')
    if chain.exists():
        other=sqlite3.connect(chain,timeout=2,isolation_level=None)
        try:
            if calls_cut is not None:other.execute('DELETE FROM calls WHERE rowid IN (SELECT rowid FROM calls WHERE at<? LIMIT ?)',(calls_cut,BATCH))
            other.execute('DELETE FROM cache WHERE rowid IN (SELECT rowid FROM cache WHERE expires<? LIMIT ?)',(now,BATCH))
            if cut is not None:
                other.execute('DELETE FROM fetches WHERE rowid IN (SELECT rowid FROM fetches WHERE shown_at<? LIMIT ?)',(cut,BATCH))
                # An alert goes by when seats were told; never one still waiting to be mailed.
                for table,column in (('watch_alerts','mailed_at'),('watch_seen','at'),('watch_passes','at')):
                    if other.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():
                        other.execute('DELETE FROM %s WHERE rowid IN (SELECT rowid FROM %s WHERE %s<? LIMIT ?)'%(table,table,column),(cut,BATCH))
        finally:other.close()
    if not Path(path).exists():return 0,0,0
    conn=sqlite3.connect(path,timeout=2,isolation_level=None)
    try:
        calls=conn.execute('DELETE FROM calls WHERE rowid IN (SELECT rowid FROM calls WHERE at<? LIMIT ?)',(calls_cut,BATCH)).rowcount if calls_cut is not None else 0
        cache=conn.execute('DELETE FROM cache WHERE rowid IN (SELECT rowid FROM cache WHERE at<? LIMIT ?)',(now-DAY,BATCH)).rowcount
        fetches=conn.execute('DELETE FROM fetches WHERE rowid IN (SELECT rowid FROM fetches WHERE shown_at<? LIMIT ?)',(cut,BATCH)).rowcount if cut is not None else 0
        return calls,cache,fetches
    finally:conn.close()


def run(cfg,office,quota,*,now,current=None):
    lines=[]
    try:lines.append('market_deleted=%d,%d,%d'%prune_market(Path(cfg['office_db']).parent/'v2/market.db',office,now=now))
    except (OSError,sqlite3.Error) as exc:lines.append('market_prune_failed=%s'%type(exc).__name__)
    if cfg.get('ledger_db'):lines.append('book_snapshots_deleted=%d'%prune_snapshots(office,now=now))
    try:S.from_config(cfg).prune(now);lines.append('search_pruned=True')
    except (OSError,ValueError,S.sqlite3.Error):lines.append('search_pruned=False')
    if cfg.get('ledger_db') and Path(cfg['ledger_db']).exists():lines.append('orphan_labels_deleted=%d'%prune_orphan_labels(office,cfg['ledger_db'],now=now))
    lines.append('events_deleted=%d'%prune_events(office,now=now))
    lines.append('logs_deleted=%d'%prune_logs(cfg['proxy_log_dir'],now=now))
    lines.append('sessions_ok=%s'%prune_sessions(cfg['state_dir'],current=current))
    lines.append('quota_deleted=%s'%Q.prune(quota,now=now))
    return lines
