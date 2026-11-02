"""Follow already-proven graph counterparties; discovery now never becomes historical warning."""
import json
import math
import sqlite3
import time
from pathlib import Path

from . import wallet_watch as W
from .chain_data import SIGNATURE
from .wallet_family import PROGRAMS


def _funding_proof(chain,edge,now):
    row=chain.execute('SELECT state,result,shown_at FROM fetches WHERE fetch_id=?',(edge['proof_ref'],)).fetchone()
    if not row or row[0]!='known' or type(row[2]) not in (int,float) or not 0<row[2]<=now or len(row[1])>8192:return None
    fact=json.loads(row[1])
    if not isinstance(fact,dict):return None
    sig=fact.get('funding_signature');amount=fact.get('amount_atomic')
    if not (fact.get('state')=='known' and fact.get('network_id')=='solana' and fact.get('kind')=='verified_funding' and fact.get('verified') is True
        and fact.get('funder')==edge['dst'] and fact.get('recipient')==edge['src'] and fact.get('funded_at')==edge['observed_at']
        and isinstance(sig,str) and SIGNATURE.fullmatch(sig) and isinstance(amount,str) and amount.isascii() and amount.isdigit() and len(amount)<=20 and 0<int(amount)<2**64
        and fact.get('method') in ('confirmed_system_transfer','confirmed_seeded_wsol_close')):return None
    if edge['proof_kind']=='research_stream':
        if fact.get('provenance')!='research_stream' or fact.get('commitment')!='confirmed' or fact.get('tool')!='watch_stream_funding':return None
    elif fact.get('provenance')!='research_rpc' or fact.get('tool')!='watch_verified_funding' or not isinstance(fact.get('asof'),dict) or fact['asof'].get('kind')!='finalized_transaction' or fact['asof'].get('block_time_s')!=fact['funded_at']:return None
    return sig


def expand_existing_links(net,desk_dir,now,limit=100):
    """One fair work page. Unadded links remain in the graph for later calls and restarts."""
    if type(now) not in (int,float) or not math.isfinite(now) or now<=0 or type(limit) is not int or limit<1:raise ValueError('bad_link_backfill_arguments')
    counts=dict(added=0,examined=0,blocked=0);conn=chain=None;deadline=time.monotonic()+1
    infrastructure=W._known_infrastructure()
    if infrastructure is None:return dict(state='unknown',reason='link_infrastructure_unknown',**counts)
    infrastructure=infrastructure|PROGRAMS
    try:
        conn=net._conn()
        conn.execute('CREATE TABLE IF NOT EXISTS watch_link_backfill_attempts(edge_id INTEGER PRIMARY KEY,turn INTEGER NOT NULL,last_try REAL,state TEXT,reason TEXT)')
        conn.execute('CREATE INDEX IF NOT EXISTS watch_link_backfill_turn ON watch_link_backfill_attempts(turn)')
        conn.commit();conn.set_progress_handler(lambda:time.monotonic()>deadline,1000)
        predicate="""w.removed_at IS NULL AND e.network_id='solana' AND e.source='host' AND e.type='funded_by' AND e.proof_state='verified'
            AND ((e.proof_kind='research_stream' AND e.method_version='wallet-watch-stream-v1') OR (e.proof_kind='chain_fetch' AND e.method_version='wallet-watch-receipt-v1'))
            AND e.known_at>0 AND e.known_at<=? AND e.observed_at>0 AND e.observed_at<=?"""
        rows=conn.execute("""WITH candidates AS (
            SELECT e.*,w.address AS anchor,e.src AS target FROM watch_list w CROSS JOIN edges e INDEXED BY edges_verified_fanout ON e.dst=w.address WHERE """+predicate+"""
            UNION ALL SELECT e.*,w.address AS anchor,e.dst AS target FROM watch_list w CROSS JOIN edges e INDEXED BY edges_src ON e.src=w.address WHERE """+predicate+""")
            SELECT e.* FROM candidates e LEFT JOIN watch_link_backfill_attempts tried ON tried.edge_id=e.edge_id
            WHERE NOT EXISTS(SELECT 1 FROM watch_list listed WHERE listed.address=e.target)
            ORDER BY COALESCE(tried.turn,0),e.edge_id LIMIT ?""",(now,now,now,now,limit)).fetchall()
        if net.chain_db is None:return dict(state='unknown',reason='link_proof_store_unknown',**counts)
        chain=sqlite3.connect(Path(net.chain_db).resolve().as_uri()+'?mode=ro',uri=True,timeout=0)
        chain.set_progress_handler(lambda:time.monotonic()>deadline,1000)
        for edge in rows:
            if time.monotonic()>deadline:return dict(state='partial',reason='link_backfill_work_budget',**counts)
            target,anchor=edge['target'],edge['anchor'];reason=None;counts['examined']+=1
            labels={r[0].lower() for r in conn.execute("SELECT label FROM labels WHERE network_id='solana' AND address IN (?,?)",(target,anchor))}
            if {target,anchor}&infrastructure or labels&{'infrastructure','exchange','known_infrastructure'}:reason='link_infrastructure'
            else:
                try:sig=_funding_proof(chain,edge,now)
                except (ValueError,TypeError,KeyError):sig=None
                if sig is None:reason='link_receipt_unproven'
                else:
                    result=W.add(net,desk_dir,address=target,operator_id='linked:'+anchor,campaign_limit=-1,evidence='Verified funding counterparty '+sig+'; observed link, not common control; joined now.',source='host',actor='wallet_link_backfill',now=now)
                    if result.get('state')=='added':counts['added']+=1
                    else:reason=result.get('reason','link_watch_unknown')
            counts['blocked']+=reason is not None
            with conn:
                old=conn.execute('SELECT 1 FROM watch_link_backfill_attempts WHERE edge_id=?',(edge['edge_id'],)).fetchone()
                why=net._refusal(conn,now,rows=0 if old else 1)
                if why:return dict(state='partial',reason=why,**counts)
                conn.execute('INSERT INTO watch_link_backfill_attempts VALUES(?,(SELECT COALESCE(MAX(turn),0)+1 FROM watch_link_backfill_attempts),?,?,?) ON CONFLICT(edge_id) DO UPDATE SET turn=excluded.turn,last_try=excluded.last_try,state=excluded.state,reason=excluded.reason',(edge['edge_id'],now,'deferred' if reason else 'added',reason))
                if not old:net._count(conn,now,rows=1)
        return dict(state='partial',reason='observed_links_only',**counts)
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):
        if conn is not None:conn.rollback()
        return dict(state='unknown',reason='link_backfill_read_budget' if time.monotonic()>deadline else 'link_backfill_store_unknown',**counts)
    finally:
        if conn is not None:conn.close()
        if chain is not None:chain.close()
