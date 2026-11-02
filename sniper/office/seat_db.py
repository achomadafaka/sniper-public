"""db_read, host side. A seat's SELECT runs in a private in-memory database holding copied rows up to the cut, never on a connection that can reach the desk. Rows are data.

Views plus an authorizer are not a boundary: SQLite reports a CTE name where the view name goes, so WITH x AS (SELECT * FROM main.t) read anything (Zeta).

Only tables named here are visible. Paper positions, grades, closed orders and look records are left out on purpose: a persistent seat
could otherwise read interim results (Gamma D2). A table with no usable time column is not listable.

The cut must be now. desk_ticket_heads.state and last_seen_at are current values, so an earlier cut would show a state from after it."""
import glob
import json
import os
import re
import sqlite3
import time
from datetime import datetime,timezone
try:from . import seat_intel as INTEL_SPEC
except ImportError:   # run as a script or loaded by path (the db_read child): the spec module sits beside this file
    import importlib.util as _u
    _spec=_u.spec_from_file_location('seat_intel',os.path.join(os.path.dirname(os.path.abspath(__file__)),'seat_intel.py'))
    INTEL_SPEC=_u.module_from_spec(_spec);_spec.loader.exec_module(INTEL_SPEC)

MAX_ROWS=200
MAX_OUTPUT=12000   # below the tool's 16 KiB, so a result is never cut mid-JSON
MAX_VALUE=1<<20
MAX_COLUMNS=64   # both the column count and each name's length
COPY_ROWS=20000
CHILD_MEMORY=768*1024**2
CHILD_FILE=64*1024**2
COPY_BYTES=16*1024**2   # per table; bounds host memory
DEADLINE_S=2.   # the seat's statement
COPY_DEADLINE_S=6.   # copying the allowed tables: 1.5 s measured on the live files, so 2 s would flap under load; the child still has the tool's 15 s timeout
# table: (time column, 'iso' or 'epoch', columns). Named columns, so a new one stays out until reviewed. *_json blobs, position_id and intent_id are left out.
ALLOWED={
    'desk_tickets':('ts','iso',('id','ticket_id','opportunity_id','cohort_id','producer','network_id','mint','pool','stage','seat','verdict','reason','model','prompt_version',
                                'latency_ms','gate_state','gate_evaluated_at','response_at','trigger_at','wait_state','cost_usd','ts')),
    'desk_ticket_heads':('created_at','iso',('ticket_id','opportunity_id','cohort_id','producer','network_id','mint','pool','emitted_at','created_at','last_seen_at','state')),
    'desk_candidate_emissions':('emitted_at','iso',('opportunity_id','ticket_id','producer','cohort_id','network_id','mint','pool','emitted_at','received_at','is_primary','reason','gate_state')),
    'desk_intake_source_receipts':('known_at','epoch',('opportunity_id','cohort_id','known_at')),
}

# Other databases beside the desk db, at fixed names under its directory: a seat never names a path. Same rule as above: named columns, public chain data only,
# never our own decisions or quotes (runner_signals golden_*, entry_*, candidate_* and confirmed_* stay out).
# table: (source, time column or None, 'iso'|'epoch', columns, predicate with one ? for the cut, used when there is no time column, asof label). 'now' tables hold values as
# last written; 'row' tables (the outcome table) are append-only and each row is dated by its own time column, so a row at or before the cut is causal for that cut.
# Every one carries its last write; a table not written for STALE_S is labelled stale.
SOURCES={'cluster':'desk/cluster.db','runner':'runner_signals.db','opportunity':'memecoin_opportunity.db','outcomes':'office/outcomes.db','replay':'office/pass_replay_v2.db'}
STALE_S=7*86400.
EXTERNAL={
    'cluster_funder_cache':('cluster','resolved_at','iso',('owner','funder','status','resolved_at','funder_source'),None,'now'),
    'cluster_snapshots':('cluster','snapshot_at','iso',('snapshot_id','mint','snapshot_at'),None,'now'),
    'cluster_snapshot_holders':('cluster',None,'iso',('snapshot_id','owner','balance_atomic','exclusion'),
                                'snapshot_id IN (SELECT snapshot_id FROM cluster_snapshots WHERE snapshot_at<=?)','now'),
    'runner_signals':('runner','created_at','iso',('mint','ticker','created_at','creator','dev_hold_share','dev_prior_mints','holder_top1_share','holder_top5_share',
                                                   'funder_top_share','funder_clustered_share'),None,'now'),
    'op_creator_outcomes':('opportunity','last_outcome_at','iso',('network_id','venue_id','creator_id','scored_outcomes','sum_return_bps','last_outcome_at'),None,'now'),
    # numeric only: no ticker or free text. outcome_coins is dated by the sheet, outcome_horizons by its maturity.
    'outcome_coins':('outcomes','sheet_at','epoch',('network_id','mint','sheet_at','creator','creator_source','price_source'),None,'row'),
    # who made each mint, from every source that names one; two sources that disagree are two rows (not resolved here). A mint with no row has an unknown creator.
    'mint_creators':('outcomes','source_at','epoch',('network_id','mint','creator','source','source_at'),None,'row'),
    'outcome_horizons':('outcomes','matured_at','epoch',('network_id','mint','horizon_s','matured_at','status','p0','p0_age_s','p_end','last_print_age_s','n_prints','ret','peak_ret',
                                                         'trough_ret','max_drawdown','collapsed','collapse_s','gap_check','tape_decoder_version','loss_complete_n','loss_unknown_n','loss_share_complete','loss_share_all',
                                                         'bracket_n','bracket_endpoint_displacement_max'),None,'row'),
    # PASS replay. DESCRIPTIVE, IN-SAMPLE, MODELLED. In-sample: the PASS set and the rail values were chosen after the crew had seen these coins; fills are modelled (entry = last print at or before PASS time + delay, within 120 s; cost = sell_probe round trip frozen at PASS + 0.76 percent fee; no landing, no size effect). Variants are hypotheses, not policy. status ok = a covered modelled exit: a rail fired inside the covered span, or the max hold elapsed inside it. In the current file all 183 base_d1 ok rows ended by a rail and none by max hold, because coverage rarely reaches 3600 s; the keep-extended cohort can produce max_hold exits. A row with no rail observed before coverage ended is tape_ended_capture_stopped; what it did after coverage is unknown. ok is therefore conditional on a rail firing (or the hold completing) inside coverage, and any mean over ok rows is biased. Always print n and the count of tape_ended_capture_stopped, censored_pool_loss, no_print_at_t0 (no entry price under the 120 s rule; not never-captured), immature beside any figure, at coin level and ticket level (a coin can have many tickets). Rows are clustered by hour and creator; effective n is far below row count. Pair age is mostly null. A rail rule that does best here is a hypothesis; trying many variants and picking one is a multiple-comparison pick. No return summary or ranking is published by the crew; seats may query and must label their own figures with the limits of this paragraph. Dated by matured_at (entry + 3600 s + 60 s) so a row never shows a result whose hold has not elapsed at the cut. features_json stays out.
    'pass_replay':('replay','matured_at','epoch',('ticket_id','variant','network_id','mint','pool','pass_at','entry_delay_s','entry_at','matured_at','coverage_end','asof','sl_pct','tp_pct','trail_pct',
                                                  'catastrophe_pct','mark_blind_s','status','entry_price','rail','exit_at','exit_price','hold_s','gross_ret','peak_ret','trough_ret',
                                                  'last_print_age_s','probe_round_trip_loss_pct','fee_frac','net_ret','pair_age_s','mcap_usd','liq_usd','top10_pct','creator_pct',
                                                  'gap_check','tape_decoder_version'),None,'row'),
    'replay_meta':('replay','built_at','epoch',('built_at','version','rule','since','variants'),None,'now'),
}

# Intel tables (seat_intel.py): same machinery, plus expressions over the source row, text cut to INTEL_SPEC.TEXT_BYTES with a `<field>_truncated` marker, and a source note with retention in coverage.
SOURCES.update(INTEL_SPEC.SOURCES)
NOTES_MAX=4   # source notes in one answer: the response cap is 12 KB
SRC_TABLE={};EXPR={};TEXT={};NOTES={};STALE_AFTER={};CLASS={}
for _t,_v in INTEL_SPEC.INTEL.items():
    EXTERNAL[_t]=(_v['source'],_v['time'],'epoch',_v['cols']+tuple(c+'_truncated' for c in _v['text'])+('truncated_fields',),None,'row')
    SRC_TABLE[_t]=_v['src'];EXPR[_t]=_v['expr'];TEXT[_t]=_v['text'];NOTES[_t]=_v['note'];STALE_AFTER[_t]=_v['stale_after_s'];CLASS[_t]=_v['klass']


def _cut(kind,now):
    return now if kind=='epoch' else datetime.fromtimestamp(now,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f+00:00')


def _authorizer(action,a,b,db,view):
    # What a seat's statement runs against holds only copied rows, so reads are free. This keeps it to reading: no attach, pragma or write.
    if action in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_RECURSIVE,sqlite3.SQLITE_READ):return sqlite3.SQLITE_OK
    if action==sqlite3.SQLITE_FUNCTION and b not in ('load_extension','readfile','writefile','fts3_tokenizer'):return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _cut_text(value):
    """-> (value cut to TEXT_BYTES bytes on a character boundary, 1 if it was cut else 0). Non-text passes through unmarked (0)."""
    if not isinstance(value,str):return value,0
    raw=value.encode('utf-8','replace')
    if len(raw)<=INTEL_SPEC.TEXT_BYTES:return value,0
    return raw[:INTEL_SPEC.TEXT_BYTES].decode('utf-8','ignore'),1


def _fill(conn,sources,table,where,params,cols,deadline_state):
    """sources: connections, newest first (a day-partitioned table has one per day file). Intel tables read their source table through expressions; EVERY string column is cut to TEXT_BYTES
    (the declared text columns get a <field>_truncated marker, and truncated_fields lists each field cut in the row, so a long handle or id is never passed unmarked)."""
    text=TEXT.get(table,());expr=EXPR.get(table,{});src_table=SRC_TABLE.get(table,table);intel=table in NOTES
    base=[c for c in cols if not (c.endswith('_truncated') and c[:-len('_truncated')] in text) and not (intel and c=='truncated_fields')]
    names=','.join('"%s"'%c for c in cols)
    conn.execute('CREATE TABLE %s(%s)'%(table,names))
    select=','.join('%s AS "%s"'%(expr[c],c) if c in expr else '"%s"'%c for c in base)
    used=0;n=0;state='complete';failed=[]
    for source in sources:
        try:cursor=source.execute('SELECT %s FROM %s WHERE %s ORDER BY rowid DESC LIMIT ?'%(select,src_table,where),params+(COPY_ROWS+1,))
        except sqlite3.OperationalError:
            if len(sources)==1:raise      # a single source that cannot answer is unavailable (the caller says so)
            failed.append(_dbname(source));continue
        for row in cursor:
            row=list(row);cut=[];marks={}
            if intel:
                for k,c in enumerate(base):
                    row[k],m=_cut_text(row[k])
                    if m:cut.append(c);marks[c]=1
            used+=sum(len(v) for v in row if isinstance(v,(str,bytes)))
            if n==COPY_ROWS or used>COPY_BYTES:
                state='newest_only';break
            extra=[marks.get(c,0) for c in text]+([','.join(cut) or None] if intel else [])
            conn.execute('INSERT INTO %s VALUES(%s)'%(table,','.join('?'*len(cols))),row+extra);n+=1
        if state!='complete':break
    if failed:
        if not n and len(failed)==len(sources):raise sqlite3.OperationalError('no partition could be read')
        return dict(rows=n,state='partial_source',failed_files=failed,copy_state=state)
    return dict(rows=n,state=state)


def _dbname(conn):
    try:return os.path.basename(conn.execute('PRAGMA database_list').fetchone()[2])
    except sqlite3.Error:return '?'


def _stale(last,now):
    try:return now-datetime.fromisoformat(last.replace('Z','+00:00')).timestamp()>STALE_S
    except (AttributeError,ValueError):return None


def _copy(conn,source,deadline,desk_path=None,opened=None,wanted=None,failed_open=None):
    """Newest rows (by rowid, which is arrival order and needs no scan) of each allowed table, at or before the cut, named columns only, into a private in-memory database. The source is never reachable by the seat's SQL.
    A table in another database that cannot be opened reads `unavailable`, never an empty table."""
    coverage={}
    for table,(column,kind,cols) in ALLOWED.items():
        coverage[table]=_fill(conn,[source],table,'%s IS NOT NULL AND %s<=?'%(column,column),(_cut(kind,deadline['now']),),cols,deadline)
    for table,(name,column,kind,cols,pred,asof) in EXTERNAL.items():
        srcs=opened.get(name) if opened is not None else None
        if table in NOTES and wanted is not None and table not in wanted:   # an intel table the statement does not name is not copied (they are big) and its note is not sent
            conn.execute('CREATE TABLE %s(%s)'%(table,','.join('"%s"'%c for c in cols)))
            coverage[table]=dict(state='not_requested',origin_class=CLASS[table]);continue
        if not srcs:
            conn.execute('CREATE TABLE %s(%s)'%(table,','.join('"%s"'%c for c in cols)))
            coverage[table]=dict(rows=0,state='unavailable',source=name,asof=asof,last_write=None,stale=None,**({'note':NOTES[table],'origin_class':CLASS[table]} if table in NOTES else {}),**({'failed_files':sorted(failed_open[name])} if (failed_open or {}).get(name) else {}));continue
        where=pred if column is None else '%s IS NOT NULL AND %s<=?'%(column,column)
        try:got=_fill(conn,srcs,table,where,(_cut(kind,deadline['now']),),cols,deadline)
        except sqlite3.OperationalError:   # a file without the table (half-built or drifted) or the shared deadline: this table only, never every table
            conn.execute('DROP TABLE IF EXISTS %s'%table);conn.execute('CREATE TABLE %s(%s)'%(table,','.join('"%s"'%c for c in cols)))
            coverage[table]=dict(rows=0,state='unavailable',source=name,asof=asof,last_write=None,stale=None,**({'note':NOTES[table],'origin_class':CLASS[table]} if table in NOTES else {}));continue
        last=conn.execute('SELECT MAX("%s") FROM %s'%(column,table)).fetchone()[0] if column else None
        entry=dict(got,source=name,asof=asof,last_write=last,stale=_stale(last,deadline['now']) if kind=='iso' and last else (deadline['now']-last>STALE_S if last else None))
        if table in NOTES and got.get('state')=='partial_source':entry['state']='partial_source'
        if table in NOTES and (failed_open or {}).get(name):entry['state']='partial_source';entry['failed_files']=sorted(set(entry.get('failed_files',[]))|set(failed_open[name]))     # a day file that exists but would not open
        if table in NOTES:entry.update(_intel_coverage(table,srcs,column,deadline['now'],last,with_note=wanted is None or table in sorted(wanted)[:NOTES_MAX]))
        if table in NOTES:
            lo,hi=conn.execute('SELECT MIN("%s"),MAX("%s") FROM %s'%(column,column,table)).fetchone()
            span=dict(min_receipt=lo,max_receipt=hi,state=got['state'])
            if got['state']=='newest_only' and 'omitted' not in entry.get('note',''):
                span['warning']='the answer was computed from the newest %d rows only; any count or rate from it covers this span, not the whole day or retention; rows_per_day shows the stored days'%got['rows']
            entry['copy_span']=span
            if entry.get('coverage_errors'):entry['state']='partial_source'
        coverage[table]=entry
    return coverage


def _intel_coverage(table,srcs,column,now,last,with_note=True):
    """Retention and freshness for an intel table, from the SOURCE (not the 20,000-row copy): first and last receipt, age of the newest row against the table stale line, rows per UTC
    day for the last 7 days, and the source note. A reader must not take 'no row' for 'never happened': nothing before min_receipt is stored."""
    src_table=SRC_TABLE.get(table,table);mn=mx=None;per_day={};errors=[]
    for src in srcs:
        try:
            lo,hi=src.execute('SELECT MIN(%s),MAX(%s) FROM %s WHERE %s<=?'%(column,column,src_table,column),(now,)).fetchone()
            if lo is not None:mn=lo if mn is None else min(mn,lo)
            if hi is not None:mx=hi if mx is None else max(mx,hi)
            for day,n in src.execute('SELECT CAST(%s/86400 AS INTEGER)*86400,COUNT(*) FROM %s WHERE %s>? AND %s<=? GROUP BY 1'%(column,src_table,column,column),(now-7*86400,now)):
                per_day[day]=per_day.get(day,0)+n
        except sqlite3.Error:errors.append(_dbname(src))
    files=[]
    for src in srcs[:3]:
        try:
            f=src.execute('PRAGMA database_list').fetchone()[2];files.append(dict(file=os.path.basename(f),file_age_s=round(now-os.path.getmtime(f))))
        except (sqlite3.Error,OSError):pass
    age=None if mx is None else now-mx
    newest_file_age=min((f['file_age_s'] for f in files),default=None)
    horizon=None
    for src in srcs:
        m=re.match(r'(\d{4}-\d{2}-\d{2})\.db$',_dbname(src))
        if m:horizon=m.group(1) if horizon is None else min(horizon,m.group(1))
    if not with_note:   # a statement naming many intel tables: the compact entry (the response cap is 12 KB); name fewer tables to see rows_per_day, the files and the note
        out=dict(min_receipt=mn,max_receipt=mx,age_s=age,origin_class=CLASS[table],newest_file_age_s=newest_file_age,stale=None if age is None else age>STALE_AFTER[table],
                 note='omitted: name at most %d intel tables to see the note'%NOTES_MAX)
        if errors:out['coverage_errors']=errors        # compact may drop the note, never a failure flag
        return out
    out=dict(min_receipt=mn,max_receipt=mx,age_s=age,origin_class=CLASS[table],newest_file_age_s=newest_file_age,reader_horizon_start=horizon,files_opened=len(srcs),stale=None if age is None else age>STALE_AFTER[table],writer_last_alive='unknown (a stale table may mean a dead writer or no events; not read here)',
                rows_per_day={datetime.fromtimestamp(d,timezone.utc).strftime('%Y-%m-%d'):n for d,n in sorted(per_day.items())},source_files=files,
                note=NOTES[table] if with_note else 'omitted: the statement names more than %d intel tables; the db_read description carries the rules and a statement naming fewer tables shows this note'%NOTES_MAX)
    if errors:out['coverage_errors']=errors      # a partition that cannot be read: the answer is partial, never the surviving subset presented as the table
    return out


COVERAGE_BUDGET=5500   # bytes of the 12 KB answer that coverage may take; the rest is the seat's rows
HEAVY=('note','rows_per_day','source_files','writer_last_alive')


def _shrink(coverage):
    """Keep the answer inside the cap however many intel tables a statement names: drop the heavy descriptive keys of intel tables (last name first) until coverage fits. Failure state,
    failed_files, coverage_errors, ages and the stale flag are never dropped."""
    for table in sorted((t for t in coverage if t in NOTES),reverse=True):
        if len(json.dumps(coverage,default=str))<=COVERAGE_BUDGET:return
        entry=coverage[table]
        if any(k in entry for k in HEAVY):
            for k in HEAVY:entry.pop(k,None)
            entry['note']='omitted to fit the answer size; name fewer intel tables to see the note, rows_per_day and files'
            span=entry.get('copy_span')
            if isinstance(span,dict):span.pop('warning',None)


def _open_sources(path,deadline_check,failed=None):
    """Each other database read-only, as a list of connections newest first (a day-partitioned pattern opens up to DAY_FILES day files); one that is missing or unreadable is None (reported
    unavailable). A day file that exists but cannot be opened is recorded in `failed[name]` (file names), never skipped silently."""
    opened={}
    for name,rel in SOURCES.items():
        full=os.path.join(os.path.dirname(path),rel)
        files=sorted(glob.glob(full),reverse=True)[:INTEL_SPEC.DAY_FILES] if any(ch in rel for ch in '?*') else [full]
        conns=[]
        for f in files:
            try:
                c=sqlite3.connect('file:%s?mode=ro'%f,uri=True,timeout=.5)
                c.set_progress_handler(deadline_check,2000);conns.append(c)
            except sqlite3.Error:
                if failed is not None and os.path.exists(f):failed.setdefault(name,[]).append(os.path.basename(f))
        opened[name]=conns or None
    return opened


def read(path,sql,*,now):
    """Returns state known with rows, or state unknown with a reason. A statement that hits the deadline is partial, never an empty result.
    Results cover the newest rows copied per table; `coverage` says where a table was cut, so a count is never mistaken for the whole table."""
    if not isinstance(sql,str) or not sql.strip() or len(sql)>4000 or ';' in sql.rstrip().rstrip(';'):return dict(state='unknown',reason='statement_invalid')
    deadline=float('inf')
    try:
        source=sqlite3.connect('file:%s?mode=ro'%path,uri=True,timeout=.5)
    except sqlite3.Error as exc:
        return dict(state='unknown',reason=type(exc).__name__)
    conn=sqlite3.connect(':memory:')
    try:
        deadline=time.monotonic()+COPY_DEADLINE_S
        check=lambda:int(time.monotonic()>deadline)
        source.set_progress_handler(check,2000)
        failed_open={}
        opened=_open_sources(path,check,failed_open)
        wanted={t for t in NOTES if re.search(r'\b%s\b'%t,sql,re.I)}
        try:coverage=_copy(conn,source,dict(now=now),path,opened,wanted,failed_open)
        finally:
            source.close()
            for group in opened.values():
                for c in group or ():c.close()
        _shrink(coverage)
        conn.execute('PRAGMA temp_store=MEMORY')   # the sorter would otherwise spill to temp files, past the address-space limit and into tmpfs RAM on this host
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,MAX_VALUE)   # zeroblob and printf cannot allocate past this before the deadline fires
        conn.set_authorizer(_authorizer)
        deadline=time.monotonic()+DEADLINE_S
        conn.set_progress_handler(lambda:int(time.monotonic()>deadline),2000)
        cursor=conn.execute(sql)
        names=[d[0] for d in cursor.description or []]
        if len(names)>MAX_COLUMNS or any(len(n)>MAX_COLUMNS for n in names):return dict(state='unknown',reason='response_too_large',rows=[])
        rows,used,reason=[],0,None
        for row in cursor:   # one at a time, so the byte cap also bounds host memory
            used+=len(json.dumps(row,default=str))
            if used>MAX_OUTPUT:reason='output_bytes';break
            if len(rows)==MAX_ROWS:reason='row_limit';break
            rows.append(list(row))
        out=dict(state='partial' if reason else 'known',columns=names,rows=rows,limited=bool(reason),reason=reason,cut=now,coverage=coverage,
                 whole_table=all(v['state'] in ('complete','not_requested') for v in coverage.values()))   # an unavailable source is not complete
        # The cap is on the whole answer: column names and escaping count too, and the tool must never cut JSON mid-way.
        while len(json.dumps(out,default=str))>MAX_OUTPUT and out['rows']:
            out['rows'].pop();out.update(state='partial',limited=True,reason='output_bytes')
        if len(json.dumps(out,default=str))>MAX_OUTPUT:   # what filled it travels with the refusal: this was seen once under load and never reproduced
            return dict(state='unknown',reason='response_too_large',rows=[],coverage_bytes=len(json.dumps(coverage,default=str)),failing=[t for t,v in coverage.items() if v.get('coverage_errors') or v.get('failed_files')][:20])
        return out
    except sqlite3.Error as exc:
        # The progress handler's abort arrives as a plain DatabaseError on this Python, so the deadline is told apart by time, not class.
        if time.monotonic()>deadline:return dict(state='partial',reason='deadline',rows=[])
        return dict(state='unknown',reason=re.sub(r'\s+',' ',str(exc))[:200] or type(exc).__name__,rows=[])
    finally:conn.close()


def main(argv):
    """One read in a child with an address-space limit: a recursive query's temp structures are bounded by the kernel, not only by the deadline. Memory failure reads unknown."""
    import resource
    resource.setrlimit(resource.RLIMIT_AS,(CHILD_MEMORY,CHILD_MEMORY))
    resource.setrlimit(resource.RLIMIT_FSIZE,(CHILD_FILE,CHILD_FILE))
    try:out=read(argv[1],argv[2],now=float(argv[3]))
    except MemoryError:out=dict(state='unknown',reason='memory_limit',rows=[])
    print(json.dumps(out,default=str))


if __name__=='__main__':
    import sys
    main(sys.argv)
