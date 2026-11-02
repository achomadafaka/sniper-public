"""Host-side alert on coins the office holds: one wallet taking one mint from 3+ owners in a transaction, or a non-pool holder jumping 3%+ of supply. Research RPC only, shared pace, off the funded path. Latency over coverage: it reads a few newest transactions per coin, and says how many it skipped; a transfer that doesn't list the mint account isn't seen."""
import json
import sqlite3
import time

from . import chain_data as C,seat_events as E

MIN_SOURCES=3
JUMP_PCT=3.
SIGS_PER_COIN=20
TXS_PER_COIN=5
SEND_ROLES=('head','judge','judge_gpt','scout')


def detect(result,mint,supply_atomic,*,pools=frozenset()):
    """Alerts in one transaction, from token balance changes only. Amounts are atomic; unknown supply skips the jump rule, never reads as zero."""
    meta=C._dict(result.get('meta'))
    if meta.get('err') is not None:return []
    change={}
    post={}
    for side,rows in ((-1,meta.get('preTokenBalances') or []),(1,meta.get('postTokenBalances') or [])):
        for row in rows:
            row=C._dict(row);owner=C._address(row.get('owner'));amount=C._digits(C._dict(row.get('uiTokenAmount')).get('amount'))
            if row.get('mint')!=mint or owner is None or amount is None:continue
            change[owner]=change.get(owner,0)+side*int(amount)
            if side==1:post[owner]=post.get(owner,0)+int(amount)
    out=[]
    senders=[o for o,d in change.items() if d<0 and o not in pools]
    takers=[o for o,d in change.items() if d>0 and o not in pools]
    if len(senders)>=MIN_SOURCES and len(takers)==1:
        out.append(dict(kind='consolidation',wallet=takers[0],sources=len(senders),amount_atomic=str(change[takers[0]])))
    if supply_atomic:
        for owner in takers:
            pct=change[owner]*100./supply_atomic
            if pct>=JUMP_PCT:out.append(dict(kind='holder_jump',wallet=owner,pct_of_supply=round(pct,2)))
    return out


def ensure_schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS watch_alerts(signature TEXT,mint TEXT,kind TEXT,wallet TEXT,block_time_s INTEGER,detected_at REAL,detail TEXT,mailed_at REAL,PRIMARY KEY(signature,mint,kind,wallet))')
    conn.execute('CREATE TABLE IF NOT EXISTS watch_seen(signature TEXT,mint TEXT,at REAL,PRIMARY KEY(signature,mint))')
    conn.execute('CREATE TABLE IF NOT EXISTS watch_passes(at REAL,coins INTEGER,txs_read INTEGER,txs_skipped INTEGER,refused TEXT)')


def coins(ledger_path):
    """Open and pending orders only, by mint (Alpha 2026-10-30): no coins, no calls. Widening to sheets read waits for real credit and delivery numbers."""
    found={}
    conn=sqlite3.connect('file:%s?mode=ro'%ledger_path,uri=True,timeout=2)
    try:
        for mint,pool in conn.execute("SELECT mint,pool FROM orders WHERE state IN ('pending','open') AND network_id='solana'"):found[mint]=pool
    finally:conn.close()
    return found


def run_pass(chain,ledger_path,*,deadline_s=25.):
    """One pass. A provider refusal ends it, recorded, so the shared stop holds."""
    now=chain.clock();deadline=chain.monotonic()+deadline_s;dials=[]
    conn=chain._conn();ensure_schema(conn)
    read=skipped=0;refused=None
    found=coins(ledger_path)
    try:
        for mint,pool in found.items():
            sigs,_c,_a=chain._paced_call('getSignaturesForAddress',[mint,dict(limit=SIGS_PER_COIN,commitment='confirmed')],deadline,dials,ttl=1)
            if not isinstance(sigs,list):continue
            fresh=[s for s in (C._signature(C._dict(r).get('signature')) for r in sigs) if s and not conn.execute('SELECT 1 FROM watch_seen WHERE signature=? AND mint=?',(s,mint)).fetchone()]
            skipped+=max(0,len(fresh)-TXS_PER_COIN)
            for signature in fresh[TXS_PER_COIN:]:conn.execute('INSERT OR IGNORE INTO watch_seen VALUES(?,?,?)',(signature,mint,now))
            if not fresh:continue
            supply,_c,_a=chain._paced_call('getTokenSupply',[mint],deadline,dials,ttl=3600)
            supply=C._digits(C._dict(C._dict(supply).get('value')).get('amount'))
            for signature in fresh[:TXS_PER_COIN]:
                result,_c,_a=chain._paced_call('getTransaction',[signature,dict(encoding='jsonParsed',maxSupportedTransactionVersion=1,commitment='confirmed')],deadline,dials,ttl=C.FINAL_TTL)
                if not isinstance(result,dict):continue   # not visible yet: a later pass reads it, it stays unseen
                read+=1
                conn.execute('INSERT OR IGNORE INTO watch_seen VALUES(?,?,?)',(signature,mint,now))
                for alert in detect(result,mint,int(supply) if supply else None,pools=frozenset({pool}) if pool else frozenset()):
                    conn.execute('INSERT OR IGNORE INTO watch_alerts VALUES(?,?,?,?,?,?,?,NULL)',(signature,mint,alert['kind'],alert['wallet'],C._int(result.get('blockTime')),chain.clock(),json.dumps(alert)))
    except C._Refused as exc:
        refused=exc.reason
    conn.execute('INSERT INTO watch_passes VALUES(?,?,?,?,?)',(now,len(found),read,skipped,refused))
    conn.close()


def deliver(chain,office):
    """Mails every alert not yet mailed, so a crash or failed mail retries next pass; a crash between the two can repeat a mail, never lose one. Sender is the host's 'watch', never the operator; text is numbers and addresses only."""
    conn=chain._conn();ensure_schema(conn)
    try:
        actors=[r[0] for r in office.execute('SELECT actor_id FROM office_v2_roster WHERE sealed=0 AND role IN (%s)'%','.join('?'*len(SEND_ROLES)),SEND_ROLES)]
        for mint,signature,kind,wallet,block_time,detail in conn.execute('SELECT mint,signature,kind,wallet,block_time_s,detail FROM watch_alerts WHERE mailed_at IS NULL').fetchall():
            text='WATCH candidate %s mint=%s tx=%s block_time_s=%s detail=%s. Balance changes in one transaction: candidate evidence, not proof of linked wallets or intent.'%(kind,mint,signature,block_time,detail)
            for actor in actors:E.seat_dm(office,from_actor='watch',from_generation=0,from_incarnation=0,to_actor_id=actor,text=text,now=chain.clock())
            conn.execute('UPDATE watch_alerts SET mailed_at=? WHERE signature=? AND mint=? AND kind=? AND wallet=?',(chain.clock(),signature,mint,kind,wallet))
    finally:conn.close()


def latency(chain,start,end):
    """Detection delay against chain time, seconds: p50, p90, n. detected_at to mailed_at and mail to read come from the rows and the mail store."""
    try:conn=sqlite3.connect(chain.path.as_uri()+'?mode=ro',uri=True,timeout=2)
    except sqlite3.Error:return dict(state='unknown',reason='chain_db_missing')
    try:
        g=sorted(r[0] for r in conn.execute('SELECT detected_at-block_time_s FROM watch_alerts WHERE detected_at>=? AND detected_at<? AND block_time_s IS NOT NULL',(start,end)))
    except sqlite3.Error:return dict(state='unknown',reason='chain_db_unreadable')
    finally:conn.close()
    return dict(state='known',n=len(g),p50=g[len(g)//2] if g else None,p90=g[int(len(g)*.9)] if g else None)


def loop(chain,office_path,ledger_path,*,log,wait=time.sleep):
    while True:
        try:
            office=sqlite3.connect(office_path,timeout=5,isolation_level=None)
            try:
                run_pass(chain,ledger_path);deliver(chain,office)
            finally:office.close()
        except Exception:log.exception('watch_alert_failed')
        wait(5)
