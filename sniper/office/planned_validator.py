"""Independent root-safe host facts; no candidate execution or success-JSON trust.

Installed root import graph must be pinned. This source is not wired into a service.
DB reads REQUIRE a read-only mount, so root cannot create/alter SQLite WAL/SHM files.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time

from .planned_host import protected_directory
from .planned_recovery import canonical, read_regular


def pinned_data(path, sha256, *, owner_uid=0, maximum=65536):
    raw=read_regular(path,expected_uid=owner_uid,maximum=maximum)
    if hashlib.sha256(raw).hexdigest()!=sha256:
        raise ValueError('validator_pin_mismatch')
    return raw


def root_document(path, sha256):
    path=Path(path)
    fd=protected_directory(path.parent)
    try:raw=pinned_data(path,sha256)
    finally:os.close(fd)
    doc=json.loads(raw)
    if type(doc)is not dict:
        raise ValueError('validator_root_document')
    return doc


def process_identity(pid, *, proc_root=Path('/proc')):
    """Stable rechecked observation, NOT proof of loaded Python modules or future liveness.

    Native proc reads also hold/poll a pidfd, so exit/reuse during observation refuses.
    Fake proc roots are solely an offline test binding, never caller-controlled policy.
    """
    if type(pid)is not int or pid<=0:
        raise ValueError('validator_pid')
    p=Path(proc_root)/str(pid);pidfd=None
    try:
        if Path(proc_root)==Path('/proc'):
            pidfd=os.pidfd_open(pid,0)
        def started():
            return int(read_regular(p/'stat',maximum=65536).decode().rsplit(')',1)[1].split()[19])
        def identity():
            status=read_regular(p/'status',maximum=65536).decode().splitlines()
            uids=[line.split()[1:] for line in status if line.startswith('Uid:')]
            if len(uids)!=1 or len(uids[0])!=4 or len(set(uids[0]))!=1:
                raise ValueError('validator_uid_unknown')
            argv=read_regular(p/'cmdline',maximum=65536).split(b'\0')
            if not argv or argv[-1]!=b'':raise ValueError('validator_process_unknown')
            return int(uids[0][0]),argv,os.readlink(p/'exe')
        birth=started();observed=identity()
        if identity()!=observed or started()!=birth or birth<=0:
            raise ValueError('validator_process_changed')
        if pidfd is not None:
            import select
            poll=select.poll();poll.register(pidfd,select.POLLIN|select.POLLHUP|select.POLLERR)
            if poll.poll(0):raise ValueError('validator_process_exited')
        uid,argv,exe=observed
        return dict(pid=pid,birth_ticks=birth,uid=uid,argv=[a.decode() for a in argv[:-1]],exe=exe)
    except (IndexError,UnicodeError,TypeError) as exc:
        raise ValueError('validator_process_unknown') from exc
    finally:
        if pidfd is not None:os.close(pidfd)


def require_process(observed, *, uid,birth_ticks,script,exe,expected_argv=None):
    """Exact approved invocation; a script string appearing only as data is not execution."""
    expected=[exe,script] if expected_argv is None else expected_argv
    if (type(expected)is not list or not expected or any(type(a)is not str for a in expected)
            or type(observed)is not dict or observed.get('uid')!=uid
            or observed.get('birth_ticks')!=birth_ticks or observed.get('exe')!=exe
            or observed.get('argv')!=expected):
        raise ValueError('validator_process_changed')
    entry=1
    while entry<len(expected) and expected[entry]in('-I','-B','-u'):entry+=1
    if entry>=len(expected) or expected[entry]!=script:
        raise ValueError('validator_invocation_not_script')


def stopped_units(observed, required):
    """Observed mapping must come from fixed OS service probes, never user result JSON."""
    for unit in required:
        row=observed.get(unit)
        if (type(row)is not dict or row.get('LoadState')!='loaded' or row.get('ActiveState')!='inactive'
                or row.get('MainPID')!='0' or row.get('ControlPID')!='0'
                or row.get('SubState')!='dead'):
            raise ValueError('validator_unit_not_stopped:'+unit)
    return True


def _number(value):
    return type(value)in(int,float) and math.isfinite(value) and value>=0


def _connection(path):
    path=Path(path)
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('validator_db_symlink')
    if not os.statvfs(path).f_flag & os.ST_RDONLY:
        raise ValueError('validator_readonly_mount_required')
    # Never immutable=1: that could silently ignore the actual committed WAL.
    conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.025,isolation_level=None)
    try:
        conn.row_factory=sqlite3.Row
        if hasattr(conn,'enable_load_extension'):
            conn.enable_load_extension(False)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,16384)
        conn.execute('PRAGMA busy_timeout=25')
        conn.execute('PRAGMA trusted_schema=OFF')
        conn.execute('PRAGMA query_only=ON')
        deadline=time.monotonic()+.025
        conn.set_progress_handler(lambda:int(time.monotonic()>deadline),100)
        conn.execute('BEGIN')
        return conn
    except BaseException:
        conn.close();raise


def _object(raw):
    value=json.loads(raw)
    if type(value)is not dict:raise ValueError('validator_json_shape_unknown')
    return value


def office_witness(store,ledger,*,now_corrected):
    """Bounded raw-state witness, independent of checkout helpers/imports.

    The root caller MUST compare hashes to its authenticated accepted context.
    This function reports facts; it does not establish target approval by itself.
    """
    if not _number(now_corrected):
        raise ValueError('validator_clock_unknown')
    conn=book_conn=None
    try:
        conn=_connection(store);book_conn=_connection(ledger)
        purpose=conn.execute('SELECT purpose FROM _store_info WHERE id=1').fetchone()
        if purpose is None or purpose[0]!='office':
            raise ValueError('validator_store_unknown')
        lease=conn.execute('SELECT expires_at FROM supervisor_lease WHERE id=1').fetchone()
        if lease is None or not _number(lease[0]) or lease[0]>now_corrected:
            raise ValueError('validator_lease_live_or_unknown')
        control=conn.execute('SELECT payload_json FROM office_epoch_control WHERE id=1').fetchone()
        if control is None:raise ValueError('validator_epoch_unknown')
        control=_object(control[0])
        actors={r[0]:_object(r[1]) for r in conn.execute('SELECT id,payload_json FROM agents LIMIT 5')}
        epochs={r[0]:_object(r[1]) for r in conn.execute('SELECT id,payload_json FROM epochs LIMIT 3')}
        if (control.get('active_epoch')!='office-pilot-discovery-2' or len(actors)!=4 or len(epochs)!=2
                or control['active_epoch'] not in epochs
                or any(type(a)is not dict or type(a.get('generation'))is not int
                       or a['generation']!=2 or a.get('state')!='active' for a in actors.values())):
            raise ValueError('validator_roster_or_epoch_changed')
        book=book_conn.execute("SELECT * FROM books WHERE book_id='pilot-floor'").fetchone()
        flows=book_conn.execute("SELECT * FROM capital_flows WHERE book_id='pilot-floor' ORDER BY id LIMIT 1001").fetchall()
        if book is None or book['kind']!='discovery' or not 0<len(flows)<=1000:
            raise ValueError('validator_capital_unknown')
        return dict(epoch=control['active_epoch'],generation=2,
            office_sha256=hashlib.sha256(canonical(dict(control=control,actors=actors,epochs=epochs))).hexdigest(),
            capital_sha256=hashlib.sha256(canonical(dict(book=dict(book),flows=[dict(r) for r in flows]))).hexdigest())
    finally:
        if conn is not None:conn.close()
        if book_conn is not None:book_conn.close()
