"""Observed Solana first sightings + retained WSOL PumpSwap flow, never universe-wide launch counts.

Separate live-v2 daily files. No network/model calls. Initial cursor tails at most 5000 source rows;
flow reads at most 5000 recent receipt rows. Counts/flow are lower bounds, including retention and
bootstrap omissions. First-sighting and block clocks are corrected UTC; receipt/heartbeat clocks
are host time and always supplied separately. Creator identity and retired band entries are unknown.
"""
from contextlib import closing
import collections
import json
import math
import re
import sqlite3
import time
from datetime import datetime,timezone
from pathlib import Path
from sniper.services import memecoin_desk_theme_rules as R
from sniper.services.memecoin_desk_ps_tape import WSOL_MINT

VERSION='narrative-live-v2'
COVERAGE='observed_first_sightings_ps_lower_bound'
BATCH=5000
FLOW_LIMIT=5000
WINDOWS=(300,900,3600)
DDL='''
CREATE TABLE IF NOT EXISTS tags(mint TEXT PRIMARY KEY,theme TEXT NOT NULL,matched TEXT,rules_version TEXT NOT NULL,
 launch_received_at REAL NOT NULL,known_at REAL NOT NULL,name TEXT,symbol TEXT,source_rowid INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS tags_received ON tags(launch_received_at);
CREATE TABLE IF NOT EXISTS theme_agg(at REAL NOT NULL,theme TEXT NOT NULL,window_s INTEGER NOT NULL,
 launches INTEGER,band_entries INTEGER,buy_usd REAL,buyers INTEGER,slope REAL,coverage TEXT NOT NULL,
 rules_version TEXT NOT NULL,PRIMARY KEY(at,theme,window_s));
CREATE TABLE IF NOT EXISTS token_hourly(hour_start REAL NOT NULL,kind TEXT NOT NULL,rank INTEGER NOT NULL,
 token TEXT NOT NULL,launches INTEGER NOT NULL,creators INTEGER,prev_hour_launches INTEGER,
 known_at REAL NOT NULL,rules_version TEXT NOT NULL,PRIMARY KEY(hour_start,kind,rank));
CREATE TABLE IF NOT EXISTS cursors(day TEXT PRIMARY KEY,last_rowid INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS source_state(at REAL PRIMARY KEY,protocol TEXT NOT NULL,launch_state TEXT NOT NULL,
 flow_state TEXT NOT NULL,flow_rows INTEGER,source_tip_rowid INTEGER,cursor_rowid INTEGER,
 launch_latest_at REAL,bootstrap_limit INTEGER NOT NULL,coverage TEXT NOT NULL,flow_fx_usd REAL);
'''
STOPWORDS=frozenset('the and for you your are was this that with from not but all can our out his her its coin coins token tokens official real new just get got has have had will one two who what why how when'.split())


def number(x):return type(x) in (int,float) and math.isfinite(x)
def day(at):return datetime.fromtimestamp(at,timezone.utc).strftime('%Y-%m-%d')


def ro(path):
    c=sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True,timeout=.25)
    until=time.monotonic()+.5;c.set_progress_handler(lambda:time.monotonic()>until,1000)
    return c


def heartbeat(path,host_now,age):
    try:
        data=json.loads(Path(path).read_text())
        return data if number(data.get('ts')) and 0<=host_now-data['ts']<=age else None
    except (OSError,ValueError,TypeError,AttributeError):return None


def ingest(db,desk,now,host_now,batch_size):
    beat=heartbeat(desk/'narrative_guess/worker.heartbeat.json',host_now,60)
    if beat is None or beat.get('error') is not None:return 0,'heartbeat_unavailable',None,None,None
    try:
        with closing(ro(desk/'narrative_guess/narrative_guess.db')) as src:
            tip=src.execute('SELECT COALESCE(MAX(rowid),0) FROM launches').fetchone()[0]
            cur=db.execute("SELECT last_rowid FROM cursors WHERE day='guesser'").fetchone()
            last=cur[0] if cur else max(0,tip-batch_size)
            rows=src.execute('SELECT rowid,network_id,mint,name,symbol,first_seen_known_at FROM launches WHERE rowid>? ORDER BY rowid LIMIT ?', (last,batch_size)).fetchall()
            latest=src.execute('SELECT first_seen_known_at FROM launches ORDER BY rowid DESC LIMIT 1').fetchone()
    except (sqlite3.Error,OSError):return 0,'source_unreadable',None,None,None
    n=0;state='ok'
    for rid,network,mint,name,symbol,at in rows:
        if not number(at):state='invalid_row';break
        if at>now:break
        if network=='solana':
            theme,matched=R.tag(symbol,name) if symbol or name else ('unknown',None)
            n+=db.execute('INSERT OR IGNORE INTO tags VALUES(?,?,?,?,?,?,?,?,?)',
                (mint,theme,matched,R.RULES_VERSION,at,now,name,symbol,rid)).rowcount
        last=rid
    db.execute("INSERT INTO cursors VALUES('guesser',?) ON CONFLICT(day) DO UPDATE SET last_rowid=excluded.last_rowid",(last,))
    return n,state,tip,last,latest[0] if latest and number(latest[0]) and latest[0]<=now else None


def flow(desk,now,host_now):
    beat=heartbeat(desk/'universe.heartbeat',host_now,10)
    if beat is None or beat.get('pumpswap_capture') is not True:return None,'capture_unavailable',None
    fx=beat.get('sol_usd');age=beat.get('sol_age_s')
    fx=fx if number(fx) and fx>0 and number(age) and 0<=age<=120 else None
    try:
        with closing(ro(desk/'pumpswap/tape.db')) as src:
            raw=src.execute('SELECT signature,ordinal,mint,quote_atomic,actor,block_time,is_buy,quote_mint FROM swaps_ps INDEXED BY swaps_ps_received '
                'WHERE received_at>? AND received_at<=? ORDER BY received_at DESC LIMIT ?',
                (host_now-3600,host_now,FLOW_LIMIT)).fetchall()
        rows=[];seen=set()
        for sig,ordinal,mint,q,actor,at,buy,quote in raw:
            if buy!=1 or quote!=WSOL_MINT or not number(at) or not now-3600<at<=now:continue
            if not isinstance(q,str) or not q.isdecimal() or not isinstance(actor,str) or not actor or not number(at):raise ValueError('invalid_flow_row')
            key=(sig,ordinal)
            if key in seen:continue
            seen.add(key);rows.append((mint,int(q),actor,at))
        return rows,'capped' if len(raw)==FLOW_LIMIT else 'ok',fx
    except (sqlite3.Error,OSError,ValueError,TypeError):return None,'source_unreadable',fx


def tags_in(directory,since,until):
    tags={}
    for d in sorted({day(since),day(until)}):
        path=Path(directory)/(d+'.db')
        if not path.exists():continue
        with closing(ro(path)) as c:
            for row in c.execute('SELECT mint,theme,matched,launch_received_at,name,symbol FROM tags WHERE launch_received_at>=? AND launch_received_at<=? AND known_at<=?',(since,until,until)):
                tags[row[0]]=row
    return tags


def tokens(tags,hour):
    words=collections.Counter();names=collections.Counter()
    for _,_,matched,at,name,symbol in tags.values():
        if not hour<=at<hour+3600:continue
        if matched:words[matched.strip()]+=1
        names.update({x for x in re.findall(r'[^\W\d_]{3,}',R.normalize(symbol,name)) if x not in STOPWORDS})
    return words,names


def run_pass(store,desk,now,state,*,host_now,batch_size=BATCH):
    desk=Path(desk);db=store.db(now)
    rows,flow_state,fx=flow(desk,now,host_now)
    tagged,launch_state,tip,cursor,latest=ingest(db,desk,now,host_now,batch_size);db.commit()
    tags=tags_in(store.out_dir,now-7200,now)
    counts={w:collections.Counter(r[1] for r in tags.values() if now-w<r[3]<=now) for w in WINDOWS}
    cov=COVERAGE if rows is not None else 'observed_first_sightings_flow_unknown'
    if launch_state!='ok':cov='first_sightings_unavailable'
    for w in WINDOWS:
        usd=collections.Counter();buyers=collections.defaultdict(set)
        for mint,q,actor,at in rows or []:
            if mint in tags and now-w<at<=now:
                theme=tags[mint][1];usd[theme]+=q;buyers[theme].add(actor)
        for theme in R.THEMES:
            slope=(counts[900][theme]/15)/(counts[3600][theme]/60) if counts[3600][theme] and launch_state=='ok' else None
            db.execute('INSERT OR IGNORE INTO theme_agg VALUES(?,?,?,?,?,?,?,?,?,?)',
                (now,theme,w,counts[w][theme] if launch_state=='ok' else None,None,
                 usd[theme]/1e9*fx if rows is not None and fx is not None else None,
                 len(buyers[theme]) if rows is not None else None,slope if w==900 else None,cov,VERSION))
    hour=int(now//3600)*3600-3600
    if launch_state=='ok' and state.get('hour')!=hour:
        history=tags_in(store.out_dir,hour-3600,now)
        for kind,current,previous in zip(('rule_word','name_token'),tokens(history,hour),tokens(history,hour-3600)):
            for rank,(token,n) in enumerate(sorted(current.items(),key=lambda kv:(-kv[1],kv[0]))[:30],1):
                db.execute('INSERT OR IGNORE INTO token_hourly VALUES(?,?,?,?,?,?,?,?,?)',(hour,kind,rank,token,n,None,previous.get(token,0),now,VERSION))
        state['hour']=hour
    db.execute('INSERT OR REPLACE INTO source_state VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (now,VERSION,launch_state,flow_state,len(rows) if rows is not None else None,tip,cursor,latest,batch_size,cov,fx))
    db.commit()
    return dict(protocol=VERSION,tagged=tagged,launch_state=launch_state,flow_state=flow_state,coverage=cov)
