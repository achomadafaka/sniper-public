"""Host read-only view of followed-wallet activity for seats. Fresh read-only handle per call, closed after, time-bound; nothing here reaches the runner.
Decisions and fills of ours stay out: this is what the wallets did, as we received it. A wallet's id is our source_user_id, not a chain address."""
from contextlib import closing
from datetime import datetime,timezone
import sqlite3
import time

from sniper.persistence.db_core import get_readonly_connection

LIMIT_MAX=100
BUDGET_S=.1
RECENT_IDS=5000
COLUMNS='e.source_user_id,e.source_handle,e.mint,e.ticker,e.side,e.their_size_usd,e.their_market_cap,e.network_id,e.signal_source,e.delivery_lane,e.created_at,e.received_at'


def _open(path):
    conn=get_readonly_connection(path)
    conn.execute('PRAGMA busy_timeout=100')   # the shared handle waits 30 s on a lock; this budget must hold
    deadline=time.monotonic()+BUDGET_S
    conn.set_progress_handler(lambda:int(time.monotonic()>=deadline),1000)
    return conn


def _iso(since):
    return datetime.fromtimestamp(since,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def _wallets(conn,ids):
    if not ids:return {}
    rows=conn.execute('SELECT source_user_id,handle,label,paused,excluded,real_approved,real_chains FROM memecoin_wallets WHERE source_user_id IN (%s)'%','.join('?'*len(ids)),list(ids)).fetchall()
    return {r['source_user_id']:dict(handle=r['handle'],label=r['label'],armed=bool(r['real_approved']),chains=r['real_chains'],paused=bool(r['paused']),excluded=bool(r['excluded'])) for r in rows}


def read(path,*,wallet=None,label=None,mint=None,since=None,limit=50):
    """Exactly one selector, or none for the newest events plus the biggest wallets by size in them. `since` is Unix seconds. An unknown wallet is refused, not empty; a failed read is unknown, never empty."""
    chosen=[v for v in (wallet,label,mint) if v is not None]
    if len(chosen)>1 or any(type(v) is not str or not v.strip() or len(v)>128 for v in chosen):return dict(state='refused',reason='one_of_wallet_label_mint')
    if type(limit) is not int or not 1<=limit<=LIMIT_MAX:return dict(state='refused',reason='limit_1_to_100')
    if since is not None and (type(since) not in (int,float) or not 0<=since<=253402300799):return dict(state='refused',reason='since_unix_seconds')
    cut=_iso(since) if since is not None else ''
    try:
        with closing(_open(path)) as conn:
            if mint is not None:
                rows=conn.execute('SELECT '+COLUMNS+' FROM memecoin_events e INDEXED BY idx_mc_events_mint WHERE e.mint=? AND e.created_at>=? ORDER BY e.created_at DESC LIMIT ?',(mint,cut,limit+1)).fetchall()
            elif wallet is not None or label is not None:
                ids=[r[0] for r in conn.execute('SELECT source_user_id FROM memecoin_wallets WHERE source_user_id=? OR lower(handle)=lower(?) OR lower(label)=lower(?) LIMIT 8',((wallet or label),)*3)]
                if not ids:return dict(state='refused',reason='wallet_not_followed')
                rows=[r for i in ids for r in conn.execute('SELECT '+COLUMNS+' FROM memecoin_events e INDEXED BY idx_mc_events_user_time WHERE e.source_user_id=? AND e.created_at>=? ORDER BY e.created_at DESC LIMIT ?',(i,cut,limit+1))]
                rows.sort(key=lambda r:r['created_at'] or '',reverse=True)
            else:
                top=conn.execute('SELECT max(id) FROM memecoin_events').fetchone()[0] or 0
                rows=conn.execute('SELECT '+COLUMNS+' FROM memecoin_events e WHERE e.id>? AND e.created_at>=? ORDER BY e.id DESC',(top-RECENT_IDS,cut)).fetchall()
            biggest=None
            if wallet is None and label is None and mint is None:
                size={}
                for r in rows:
                    if r['side']=='buy' and type(r['their_size_usd']) in (int,float):size[r['source_user_id']]=size.get(r['source_user_id'],0.)+r['their_size_usd']
                biggest=sorted(size.items(),key=lambda kv:kv[1],reverse=True)[:10]
            keep=rows[:limit]
            info=_wallets(conn,{r['source_user_id'] for r in keep}|{k for k,_ in biggest or ()})
    except (OSError,sqlite3.Error):return dict(state='unknown',reason='activity_unreadable')
    out=dict(state='known',truncated=len(rows)>limit,note='followed wallets only; absence means none of ours in retained events, not that nobody bought; a wallet id is not a chain address; size and market cap are the source\'s claim',
             items=[dict(dict(r),wallet=info.get(r['source_user_id'],dict(handle=None))) for r in keep])
    if biggest is not None:out.update(biggest_by_buy_usd=[dict(wallet_id=k,buy_usd=v,**info.get(k,{})) for k,v in biggest],biggest_window_events=RECENT_IDS)
    return out


def touched(path,mint,*,limit=8):
    """Facts for a feed ticket: who we follow touched this mint. Unknown stays unknown."""
    got=read(path,mint=mint,limit=50)
    if got['state']!='known':return dict(state=got['state'],reason=got.get('reason'))
    who={}
    for r in got['items']:
        w=who.setdefault(r['source_user_id'],dict(handle=r['wallet'].get('handle'),armed=r['wallet'].get('armed'),buys=0,sells=0,last_at=r['created_at'],chain=r['network_id']))
        w['buys' if r['side']=='buy' else 'sells']+=1
    return dict(state='known',count=len(who),truncated=got['truncated'],wallets=list(who.values())[:limit])
