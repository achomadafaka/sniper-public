"""Lossless cold payload packs. Active flow identities remain in the original SQLite table."""
import gzip
import fcntl
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
import zlib

RECORD_BYTES=256*1024
PACK_WORK_BYTES=8*1024*1024


def _publish(path,data):
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with tmp.open('xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def read_payload(ref,archive_dir):
    """Read one bounded member, verifying its manifest entry and original payload digest."""
    try:
        if archive_dir is None or not isinstance(ref,dict) or set(ref)!={'file','flow_id','signature','offset','length','payload_sha256'}:raise ValueError('archive_context_unknown')
        if type(ref['flow_id']) is not int or ref['flow_id']<=0 or not isinstance(ref['signature'],str) or not 0<len(ref['signature'])<=128:raise ValueError('archive_identity_invalid')
        if not isinstance(ref['payload_sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',ref['payload_sha256']):raise ValueError('archive_digest_invalid')
        name=ref['file'];offset=ref['offset'];length=ref['length']
        if not isinstance(name,str) or not re.fullmatch(r'[0-9a-f]{64}\.pack',name):raise ValueError('archive_name_invalid')
        if type(offset) is not int or offset<0 or type(length) is not int or not 0<length<=RECORD_BYTES+1024:raise ValueError('archive_bounds_invalid')
        directory=Path(archive_dir).resolve();path=(directory/name).resolve();manifest=(directory/(name+'.json')).resolve()
        if path.parent!=directory or manifest.parent!=directory:raise ValueError('archive_path_invalid')
        with manifest.open('rb') as f:raw=f.read(1024*1024+1)
        if len(raw)>1024*1024:raise ValueError('archive_manifest_oversize')
        doc=json.loads(raw)
        if not isinstance(doc,dict) or type(doc.get('version')) is not int or doc['version']!=1 or not isinstance(doc.get('entries'),list) or ref not in doc['entries']:raise ValueError('archive_manifest_mismatch')
        with path.open('rb') as f:f.seek(offset);packed=f.read(length)
        if len(packed)!=length:raise ValueError('archive_truncated')
        decoder=zlib.decompressobj(31);payload=decoder.decompress(packed,RECORD_BYTES+1)
        if len(payload)>RECORD_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:raise ValueError('archive_record_invalid')
        if hashlib.sha256(payload).hexdigest()!=ref['payload_sha256']:raise ValueError('archive_checksum_mismatch')
        text=payload.decode('utf-8')
        inner=json.loads(text)
        if not isinstance(inner,dict) or 'archive_payload_v1' in inner:raise ValueError('nested_or_invalid_archive_payload')
        return text
    except (OSError,KeyError,TypeError,UnicodeError,zlib.error,json.JSONDecodeError) as exc:
        raise ValueError('archive_payload_unknown') from exc



PLAN_BYTES=1024*1024
BATCH_BYTES=1024*1024


def _identity(path):
    stat=path.stat()
    return [stat.st_dev,stat.st_ino]


def _sync_directory(directory):
    fd=os.open(directory,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


@contextmanager
def _queue_lock(directory):
    directory.mkdir(parents=True,exist_ok=True)
    with (directory/'prepare.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:yield
        finally:fcntl.flock(lock,fcntl.LOCK_UN)


def _plan(path,directory):
    with (directory/'pending.json').open('rb') as f:raw=f.read(PLAN_BYTES+1)
    if len(raw)>PLAN_BYTES:raise ValueError('archive_plan_oversize')
    doc=json.loads(raw)
    if (not isinstance(doc,dict) or set(doc)!={'version','source','identity','entries'}
            or type(doc['version']) is not int or doc['version']!=1 or doc['source']!=str(path)
            or doc['identity']!=_identity(path) or not isinstance(doc['entries'],list)
            or not 1<=len(doc['entries'])<=500):raise ValueError('archive_plan_invalid')
    ids=set()
    for entry in doc['entries']:
        if (not isinstance(entry,dict) or set(entry)!={'file','flow_id','signature','offset','length','payload_sha256'}
                or type(entry['flow_id']) is not int or entry['flow_id']<=0 or entry['flow_id'] in ids
                or not isinstance(entry['signature'],str)):raise ValueError('archive_plan_identity_invalid')
        ids.add(entry['flow_id'])
    return doc


def archive_rows(tape,archive_dir,*,before,limit=500):
    """Read-only pack preparation; the capture producer alone replaces active payloads."""
    if type(before) not in (int,float) or not math.isfinite(before) or before<=0 or type(limit) is not int or not 1<=limit<=500:raise ValueError('bad_archive_arguments')
    path=Path(tape).resolve();directory=Path(archive_dir);reader=None
    try:
        with _queue_lock(directory):
            identity=_identity(path)
            if (directory/'pending.json').exists():
                plan=_plan(path,directory)
                return dict(state='partial',reason='archive_apply_pending',queued=len(plan['entries']),archived=0)
            reader=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=0)
            deadline=time.monotonic()+1;reader.set_progress_handler(lambda:time.monotonic()>deadline,1000)
            if ('payload_archived' not in {r[1] for r in reader.execute('PRAGMA table_info(wallet_flow_transactions)')}
                    or reader.execute("SELECT 1 FROM sqlite_master WHERE name='wallet_flow_archive_pending'").fetchone() is None):
                return dict(state='unknown',reason='archive_schema_pending',queued=0,archived=0)
            rows=[];size=0
            for row in reader.execute('SELECT flow_id,signature,payload_json FROM wallet_flow_transactions INDEXED BY wallet_flow_archive_pending WHERE payload_archived=0 AND received_at<? ORDER BY received_at,flow_id LIMIT ?',(before,limit)):
                payload=row[2].encode('utf-8')
                if len(payload)>RECORD_BYTES:raise ValueError('archive_source_oversize')
                if rows and size+len(payload)>PACK_WORK_BYTES:break
                rows.append(row);size+=len(payload)
            reader.close();reader=None  # Never pin the WAL during compression/verification/fsync.
            if not rows:return dict(state='known',queued=0,archived=0)
            blob=bytearray();entries=[]
            for flow_id,signature,text in rows:
                payload=text.encode();packed=gzip.compress(payload,compresslevel=1,mtime=0)
                entries.append(dict(flow_id=flow_id,signature=signature,offset=len(blob),length=len(packed),payload_sha256=hashlib.sha256(payload).hexdigest()))
                blob.extend(packed)
            basis=dict(version=1,source=str(path),identity=identity,packed_sha256=hashlib.sha256(blob).hexdigest(),entries=entries)
            name=hashlib.sha256(blob+json.dumps(basis,sort_keys=True).encode()).hexdigest()+'.pack'
            for entry in entries:entry['file']=name
            _publish(directory/name,blob);_publish(directory/(name+'.json'),json.dumps(basis,sort_keys=True).encode())
            _sync_directory(directory)
            for entry,row in zip(entries,rows):
                if read_payload(entry,directory)!=row[2]:raise ValueError('archive_verification_failed')
            if _identity(path)!=identity:raise ValueError('archive_source_replaced')
            plan=json.dumps(dict(version=1,source=str(path),identity=identity,entries=entries),sort_keys=True).encode()
            if len(plan)>PLAN_BYTES:raise ValueError('archive_plan_oversize')
            _publish(directory/'pending.json',plan);_sync_directory(directory)
            return dict(state='known',queued=len(rows),archived=0,pack=name,source_identity=identity)
    except (OSError,sqlite3.Error,ValueError,TypeError) as exc:
        return dict(state='unknown',reason=str(exc) if isinstance(exc,ValueError) else 'archive_io_or_store_unknown',queued=0,archived=0)
    finally:
        if reader is not None:reader.close()


def next_batch(tape,archive_dir,*,limit=20):
    """Background local I/O only. Committed exact SQL markers are the durable queue cursor."""
    if type(limit) is not int or not 1<=limit<=20:raise ValueError('bad_archive_batch_limit')
    path=Path(tape).resolve();directory=Path(archive_dir);reader=None
    try:
        if not (directory/'pending.json').exists():return dict(state='known',items=[],reason='archive_queue_empty')
        with _queue_lock(directory):
            plan=_plan(path,directory);selected=[];complete=True;size=0
            reader=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=0)
            deadline=time.monotonic()+.1;reader.set_progress_handler(lambda:time.monotonic()>deadline,1000)
            for entry in plan['entries']:
                row=reader.execute('SELECT signature,payload_json,payload_archived FROM wallet_flow_transactions WHERE flow_id=?',(entry['flow_id'],)).fetchone()
                marker=json.dumps({'archive_payload_v1':entry},separators=(',',':'))
                if row is None or row[0]!=entry['signature']:raise ValueError('archive_source_changed')
                if row[2]==1 and row[1]==marker:continue
                complete=False
                if row[2]!=0:raise ValueError('archive_marker_changed')
                raw=row[1].encode()
                if len(raw)>RECORD_BYTES or hashlib.sha256(raw).hexdigest()!=entry['payload_sha256']:raise ValueError('archive_source_changed')
                if len(selected)>=limit or size+len(raw)>BATCH_BYTES:break
                selected.append(dict(flow_id=entry['flow_id'],signature=entry['signature'],original=row[1],marker=marker,ref=entry));size+=len(raw)
            reader.close();reader=None
            # Pack I/O is isolated from the capture connection and from any RO SQLite snapshot.
            for item in selected:
                if read_payload(item['ref'],directory)!=item['original']:raise ValueError('archive_verification_failed')
                del item['ref']
            if _identity(path)!=plan['identity']:raise ValueError('archive_source_replaced')
            if complete:
                (directory/'pending.json').unlink();_sync_directory(directory)
            return dict(state='known',items=selected,source=str(path),identity=plan['identity'],reason='archive_queue_complete' if complete else 'archive_apply_ready')
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError) as exc:
        return dict(state='unknown',reason=str(exc) if isinstance(exc,ValueError) else 'archive_io_or_store_unknown',items=[])
    finally:
        if reader is not None:reader.close()


def apply_batch(conn,tape,batch,*,opened_identity=None):
    """Stage verified replacements on the producer connection; never commit its outer transaction."""
    path=Path(tape).resolve();savepoint=False
    try:
        if (batch.get('state')!='known' or batch.get('source')!=str(path)
                or list(opened_identity or ())!=batch.get('identity') or _identity(path)!=batch.get('identity')
                or not isinstance(batch.get('items'),list) or not 1<=len(batch['items'])<=20):raise ValueError('archive_apply_identity_unknown')
        database=next((r[2] for r in conn.execute('PRAGMA database_list') if r[1]=='main'),None)
        if database is None or Path(database).resolve()!=path:raise ValueError('archive_connection_mismatch')
        size=0;ids=set()
        for item in batch['items']:
            if (set(item)!={'flow_id','signature','original','marker'} or type(item['flow_id']) is not int
                    or item['flow_id']<=0 or item['flow_id'] in ids or not isinstance(item['signature'],str)
                    or not isinstance(item['original'],str) or not isinstance(item['marker'],str)):
                raise ValueError('archive_apply_item_invalid')
            raw=item['original'].encode();size+=len(raw);ids.add(item['flow_id'])
            if len(raw)>RECORD_BYTES or len(item['marker'].encode())>2048 or size>BATCH_BYTES:raise ValueError('archive_apply_oversize')
            marker=json.loads(item['marker'])
            if not isinstance(marker,dict):raise ValueError('archive_apply_marker_invalid')
            ref=marker.get('archive_payload_v1')
            if (set(marker)!={'archive_payload_v1'} or not isinstance(ref,dict) or ref.get('flow_id')!=item['flow_id']
                    or ref.get('signature')!=item['signature'] or ref.get('payload_sha256')!=hashlib.sha256(raw).hexdigest()):raise ValueError('archive_apply_marker_invalid')
        if not conn.in_transaction:conn.execute('BEGIN IMMEDIATE')
        conn.execute('SAVEPOINT archive_apply');savepoint=True
        delta=0;original_bytes=0;staged=0
        for item in batch['items']:
            row=conn.execute('SELECT signature,payload_json,payload_archived FROM wallet_flow_transactions WHERE flow_id=?',(item['flow_id'],)).fetchone()
            if row is not None and tuple(row)==(item['signature'],item['marker'],1):continue
            changed=conn.execute('UPDATE wallet_flow_transactions SET payload_json=?,payload_archived=1 WHERE flow_id=? AND signature=? AND payload_json=? AND payload_archived=0',
                                 (item['marker'],item['flow_id'],item['signature'],item['original'])).rowcount
            if changed!=1:raise ValueError('archive_source_changed')
            raw_bytes=len(item['original'].encode());original_bytes+=raw_bytes
            delta+=len(item['marker'].encode())-raw_bytes;staged+=1
        if staged and conn.execute("UPDATE wallet_flow_meta SET payload_bytes=payload_bytes+? WHERE id=1 AND typeof(payload_bytes)='integer' AND payload_bytes>=?",(delta,original_bytes)).rowcount!=1:raise ValueError('archive_accounting_unknown')
        conn.execute('RELEASE archive_apply');savepoint=False
        return dict(state='partial',reason='archive_commit_pending',staged=staged,payload_bytes_delta=delta)
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError) as exc:
        if savepoint:
            try:
                conn.execute('ROLLBACK TO archive_apply');conn.execute('RELEASE archive_apply')
            except sqlite3.Error:
                return dict(state='unknown',reason='archive_rollback_unknown',staged=0)
        return dict(state='unknown',reason=str(exc) if isinstance(exc,ValueError) else 'archive_io_or_store_unknown',staged=0)
