"""Frozen cohort-D X attention contract; local tape only."""
import hashlib
import json
import math
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from sniper.services import memecoin_desk_list_claims as L

_PATH = Path(__file__).with_name('desk_context_definitions') / 'heat_v2.json'
_BYTES = _PATH.read_bytes()
DEFINITION = json.loads(_BYTES)
KNOWN_KEYS = frozenset(DEFINITION['known_keys'])
UNKNOWN_KEYS = frozenset(DEFINITION['unknown_keys'])
REASONS = frozenset(DEFINITION['reasons'])
FIELD_REASONS = frozenset(DEFINITION['field_reasons'])
WINDOW_KNOWN_KEYS = frozenset(DEFINITION['window_known_keys'])
WINDOW_UNKNOWN_KEYS = frozenset(DEFINITION['window_unknown_keys'])
READ_BUDGET_S = DEFINITION['read_budget_s']


class _Budget(Exception):
    pass


def definition():
    return {'version': DEFINITION['version'], 'definition_sha256': hashlib.sha256(json.dumps(DEFINITION, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()}


def _unknown(reason, as_of):
    return dict(state='unknown',reason=reason,definition_sha256=definition()['definition_sha256'],as_of=as_of)


def _corrected(host, offset):
    return host-offset if L._number(host) and L._number(offset) else None


def _covered(intervals, start, end):
    total, right = 0., start
    for lo,hi in sorted(intervals):
        lo,hi=max(lo,start),min(hi,end)
        if hi>max(lo,right):
            total+=hi-max(lo,right);right=hi
    return min(end-start,total)


def _coverage(events, as_of):
    sessions, intervals, latest, epoch_min = {}, [], None, None
    for host,kind,detail,offset,session in sorted(events,key=lambda x:x[0]):
        at=_corrected(host,offset)
        if kind=='epoch' and at is None:
            raise ValueError('clock_unknown')
        if at is None or at>as_of or not session:
            continue
        if kind=='epoch':
            sessions[session]=[False,None]
            epoch_min=at if epoch_min is None else min(at,epoch_min)
            continue
        if session not in sessions:
            continue
        connected,last=sessions[session]
        if kind=='connect':
            sessions[session]=[True,at]
        elif kind=='heartbeat':
            active=json.loads(detail or '{}').get('connected') is True
            if active:
                latest=at if latest is None else max(latest,at)
            if active and connected and last is not None and 0<=at-last<=DEFINITION['coverage_gap_limit_s']:
                intervals.append((last,at))
            sessions[session]=[active,at if active else None]
        elif kind in ('disconnect','auth_error','connect_error'):
            if connected and last is not None and 0<=at-last<=DEFINITION['coverage_gap_limit_s']:
                intervals.append((last,at))
            sessions[session]=[False,None]
    return intervals,latest,epoch_min


def heat_for(candidate, *, desk_dir, as_of):
    """A bounded local read; gaps/unknown metadata never turn into observed zero."""
    started=time.monotonic();deadline=started+READ_BUDGET_S;handles=[];read_n=0
    def check():
        if time.monotonic()>=deadline:
            raise _Budget
    def open_ro(path):
        check();conn=sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True,timeout=0)
        conn.set_progress_handler(lambda:time.monotonic()>=deadline,1000)
        conn.execute('BEGIN');handles.append(conn);return conn
    def rows(conn,sql,args=()):
        nonlocal read_n
        check();out=conn.execute(sql,args).fetchmany(DEFINITION['max_rows']-read_n+1)
        read_n+=len(out)
        if read_n>DEFINITION['max_rows']:
            raise _Budget
        return out
    try:
        check()
        if not L._number(as_of):
            return _unknown('clock_unknown',None)
        desk=Path(desk_dir).resolve();out_dir=desk/'x'
        if not (out_dir/'producer.heartbeat').is_file():
            return _unknown('no_x_tape',as_of)
        beat=json.loads((out_dir/'producer.heartbeat').read_text())
        if not isinstance(beat.get('session_id'),str):
            return _unknown('no_coverage_proof',as_of)
        mint=candidate.get('mint')
        if not isinstance(mint,str) or not mint:
            return _unknown('ticker_unknown',as_of)
        try:
            source=L.source_row(candidate,desk)
            symbol=L.symbol_of(json.loads(source['raw_json']),source['feed'])
        except Exception:
            symbol=None
        definitions=_PATH.parent
        for filename,key in [('promoters','promoter_list_sha256'),('common_words','common_word_list_sha256')]:
            if hashlib.sha256(json.dumps(json.loads((definitions/(filename+'.json')).read_bytes()), sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()!=DEFINITION[key]:
                return _unknown('x_tape_unreadable',as_of)
        promoters=set(h.lower().lstrip('@') for h in json.loads((definitions/'promoters.json').read_bytes())['handles'])
        words=json.loads((definitions/'common_words.json').read_bytes())['words']
        ambiguity=None if symbol is None else (True if len(symbol)<=3 or symbol.isdigit() or symbol in words else False)
        map_complete=True
        if ambiguity is False:
            for feed in ('fomo','pumpfun','dexscreener'):
                path=desk/'intake'/feed/'first_sightings.db'
                if not path.exists():
                    map_complete=False;continue
                db=open_ro(path)
                for network,other,raw in rows(db,'SELECT network_id,mint,raw_json FROM first_sightings '
                        'WHERE observed_at>? AND observed_at<?',(as_of-86400,as_of)):
                    check()
                    try:
                        other_symbol=L.symbol_of(json.loads(raw),feed)
                    except Exception:
                        other_symbol=None
                    if other_symbol is None:
                        map_complete=False
                    elif (network,other)!=(candidate.get('network_id'),mint) and other_symbol==symbol:
                        ambiguity=True
            if ambiguity is False and not map_complete:
                ambiguity=None
        day=datetime.fromtimestamp(as_of,timezone.utc).date()
        oldest=(day-timedelta(days=DEFINITION['max_history_days'])).isoformat()
        paths=sorted((p for p in out_dir.glob('????-??-??.db') if oldest<=p.stem<=day.isoformat()),reverse=True)
        if not paths:
            return _unknown('no_x_tape',as_of)
        databases=[];events=[];recent=[];links={};uncertain_links=set();lower=as_of-max(DEFINITION['windows_s'].values())
        for path in paths:
            db=open_ro(path)
            cols={r[1] for r in rows(db,'PRAGMA table_info(posts)')}
            databases.append((db,'clock_offset_s' in cols))
            if 'clock_offset_s' not in cols:
                recent+=rows(db,'SELECT event,tweet_id,received_at,payload_json,NULL FROM posts')
                continue
            events+=rows(db,'SELECT at,kind,detail,clock_offset_s,session_id FROM connection')
            recent+=rows(db,'SELECT event,tweet_id,received_at,payload_json,clock_offset_s FROM posts '
                'WHERE clock_offset_s IS NULL OR (received_at-clock_offset_s>? AND received_at-clock_offset_s<=?)',
                (lower,as_of))
            for tid,host,offset in rows(db,'SELECT tweet_id,received_at,clock_offset_s FROM token_meta WHERE mint=? '
                    "AND COALESCE(chain,'solana')='solana' "
                    'AND (clock_offset_s IS NULL OR received_at-clock_offset_s<=?)',(mint,as_of)):
                at=_corrected(host,offset)
                if at is None: uncertain_links.add(tid)
                elif tid not in links or at<links[tid]: links[tid]=at
        intervals,latest,epoch=_coverage(events,as_of)
        if epoch is None:
            return _unknown('no_coverage_proof',as_of)
        if latest is None or not 0<=as_of-latest<=DEFINITION['freshness_s']:
            return _unknown('tape_stale',as_of)
        ticker=re.compile(r'(?<!\w)\$?'+re.escape(symbol)+r'(?!\w)',re.I) if symbol else None
        mint_pattern=re.compile(r'(?<![A-Za-z0-9])'+re.escape(mint)+r'(?![A-Za-z0-9])')
        def matches(tid,payload):
            text=payload.get('text','')
            return tid in links or tid in uncertain_links or (isinstance(text,str) and (mint_pattern.search(text) or (ticker and ticker.search(text))))
        wanted={tid for tid,at in links.items() if lower<at<=as_of}|uncertain_links;backfill=set();scan_loss=bool(uncertain_links)
        for event,tid,host,raw,offset in recent:
            check()
            try:
                payload=json.loads(raw)
                payload=payload.get('tweet',payload) if event=='tweet_deleted' else payload
                if tid and matches(tid,payload):
                    (backfill if event=='initialTweets' else wanted).add(tid)
            except Exception:
                scan_loss=True
        matched={};uncertain=set()
        for db,clock_column in databases:
            ids=sorted(wanted)
            for i in range(0,len(ids),400):
                part=ids[i:i+400]
                offset_sql='clock_offset_s' if clock_column else 'NULL'
                query='SELECT event,tweet_id,received_at,payload_json,'+offset_sql+' FROM posts WHERE tweet_id IN ('+','.join('?'*len(part))+')'
                for event,tid,host,raw,offset in rows(db,query,part):
                    check()
                    if event=='initialTweets':
                        continue
                    payload=json.loads(raw)
                    payload=payload.get('tweet',payload) if event=='tweet_deleted' else payload
                    if not matches(tid,payload):
                        continue
                    at=_corrected(host,offset)
                    text=payload.get('text','')
                    text_match=isinstance(text,str) and (mint_pattern.search(text) or (ticker and ticker.search(text)))
                    if at is not None and not text_match:
                        if tid not in links:
                            continue  # Unknown mint-link time cannot date unrelated text.
                        at=max(at,links[tid])
                    if at is None:
                        uncertain.add(tid);continue
                    if at<=as_of and (tid not in matched or at<matched[tid][0]):
                        matched[tid]=(at,payload)
        for tid in uncertain:
            matched.pop(tid,None);scan_loss=True
        windows={}
        for name,length in DEFINITION['windows_s'].items():
            covered=round(_covered(intervals,as_of-length,as_of),6);gap=round(length-covered,6)
            if as_of-length<epoch or covered<length*DEFINITION['minimum_coverage_fraction']:
                windows[name]=dict(state='unknown',reason='no_coverage_proof');continue
            posts=[payload for at,payload in matched.values() if as_of-length<at<=as_of]
            authors=[p.get('author') if isinstance(p.get('author'),dict) else {} for p in posts]
            author_ids={str(a['id']) for a in authors if type(a.get('id')) in (str,int) and a.get('id') not in ('',None)}
            unknown_n=sum(type(a.get('id')) not in (str,int) or a.get('id') in ('',None) for a in authors)
            followers=[a.get('followersCount') for a in authors]
            followers_ok=bool(followers) and all(type(v) is int and v>=0 for v in followers)
            missing={}
            if not followers_ok:
                missing['top_author_followers']='followers_missing' if posts else 'no_mentions'
            promoter_n=None
            if not promoters:
                missing['promoter_mentions']='promoter_list_empty'
            elif any(not isinstance(a.get('handle'),str) for a in authors):
                missing['promoter_mentions']='author_missing'
            else:
                promoter_n=sum(a['handle'].lower().lstrip('@') in promoters for a in authors)
            windows[name]=dict(state='known',mentions=len(posts),accounts=len(author_ids),
                top_author_followers=max(followers) if followers_ok else None,promoter_mentions=promoter_n,
                author_unknown_n=unknown_n,covered_s=covered,gap_s=gap,scan_loss=scan_loss,
                lower_bound=gap>0 or unknown_n>0 or scan_loss or symbol is None or (bool(posts) and not followers_ok),
                field_unknown=missing)
        if not any(w['state']=='known' for w in windows.values()):
            return _unknown('no_coverage_proof',as_of)
        counted=[at for at,_ in matched.values() if lower<at<=as_of]
        missing={}
        if not counted:
            missing['first_mention_age_s']='no_mentions'
        if ambiguity is None:
            missing['ticker_ambiguous']='ticker_unknown' if symbol is None else 'symbol_map_incomplete'
        check()
        return dict(state='known',reason=None,definition_sha256=definition()['definition_sha256'],as_of=as_of,
            match_basis='both' if symbol else 'mint',ticker_ambiguous=ambiguity,windows=windows,
            first_mention_age_s=int(as_of-min(counted)) if counted else None,backfill_n=len(backfill),
            promoter_list_sha256=DEFINITION['promoter_list_sha256'],common_word_list_sha256=DEFINITION['common_word_list_sha256'],
            field_unknown=missing)
    except _Budget:
        return _unknown('read_budget',as_of)
    except ValueError as exc:
        return _unknown('clock_unknown' if str(exc)=='clock_unknown' else 'x_tape_unreadable',as_of)
    except Exception:
        return _unknown('read_budget' if time.monotonic()>=deadline else 'x_tape_unreadable',as_of)
    finally:
        for handle in handles:
            handle.close()
