"""Office v2 paper orders. Host-side, paper ledger only, no funded import. A seat supplies a ticket id and a reason; everything priced or identified comes from the host.

Any one Judge may enter (operator, 2026-10-30); the Head can halt. Source is assigned here, never stated by a seat. A self order needs a sheet for a coin with no ticket, and no such
sheet exists, so it is refused by name: candidate_register still records the coin, which is the self-sourced denominator."""
from dataclasses import dataclass,field
import math
import hashlib
import json
import sqlite3

from . import eval_orders as EO, ledger as L, probe as P, store as S, trader as T
from .tools import sheet as SH
from . import self_sheet as SS

MAX_SHEET_AGE_S=120.   # Beta's number for Gamma: a seat's call fetches its sheet at admission, so this bounds a cached or slow one
DECISION_S=30.


@dataclass
class Context:
    """The Trader context eval_orders and trader expect. Built only by the host, from the socket-bound identity."""
    actor_id:str
    book_id:str
    epoch_id:str
    decision_id:str
    deadline_at:float
    cut:dict=field(default_factory=dict)
    role:str='trader'
    generation:int=1


def ensure_schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS office_v2_feed_filters(seq INTEGER PRIMARY KEY AUTOINCREMENT,epoch_id TEXT NOT NULL,actor_id TEXT NOT NULL,at REAL NOT NULL,rules_json TEXT NOT NULL,reason TEXT NOT NULL,after_case TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS office_v2_feed_seen(epoch_id TEXT NOT NULL,actor_id TEXT NOT NULL,ticket_id TEXT NOT NULL,first_at REAL NOT NULL,PRIMARY KEY(epoch_id,actor_id,ticket_id))')
    conn.execute('CREATE TABLE IF NOT EXISTS office_v2_feed_hidden(epoch_id TEXT NOT NULL,actor_id TEXT NOT NULL,ticket_id TEXT NOT NULL,filter_seq INTEGER NOT NULL,rule_json TEXT NOT NULL,at REAL NOT NULL,PRIMARY KEY(epoch_id,actor_id,ticket_id,filter_seq))')
    conn.execute('CREATE TABLE IF NOT EXISTS office_v2_candidates(epoch_id TEXT NOT NULL,network_id TEXT NOT NULL,mint TEXT NOT NULL,pool TEXT,actor_id TEXT NOT NULL,known_at REAL NOT NULL,'
                 'PRIMARY KEY(epoch_id,network_id,mint))')
    conn.execute('CREATE TABLE IF NOT EXISTS office_v2_book_snapshots(snapshot_id TEXT PRIMARY KEY,epoch_id TEXT NOT NULL,actor_id TEXT NOT NULL,as_of REAL NOT NULL,equity_state TEXT,equity_usd REAL,'
                 'unpriced_count INTEGER,daily_realized_usd REAL,positions INTEGER,daily_loss_halted INTEGER)')
    conn.execute('CREATE TABLE IF NOT EXISTS office_v2_halts(seq INTEGER PRIMARY KEY AUTOINCREMENT,epoch_id TEXT NOT NULL,action TEXT NOT NULL,actor_id TEXT NOT NULL,reason TEXT NOT NULL,at REAL NOT NULL,'
                 "book_state TEXT,CHECK(action IN ('halt','resume')))")
    conn.execute('CREATE INDEX IF NOT EXISTS office_v2_halts_epoch ON office_v2_halts(epoch_id,seq)')
    conn.execute('CREATE TABLE IF NOT EXISTS office_v2_order_labels(order_id TEXT NOT NULL,decision_hash TEXT NOT NULL,epoch_id TEXT NOT NULL,actor_id TEXT NOT NULL,incarnation INTEGER NOT NULL,source TEXT NOT NULL,'
                 "sheet_age_s REAL NOT NULL,at REAL NOT NULL,labels_json TEXT NOT NULL,PRIMARY KEY(order_id,decision_hash),CHECK(source IN ('feed','self')))")


FILTER_OPS={'gt':lambda a,b:a>b,'lt':lambda a,b:a<b,'gte':lambda a,b:a>=b,'lte':lambda a,b:a<=b,'eq':lambda a,b:a==b,'ne':lambda a,b:a!=b}


def valid_rules(rules):
    if not isinstance(rules,list) or len(rules)>20:return False
    return all(isinstance(r,dict) and set(r)=={'path','op','value'} and isinstance(r['path'],str) and 0<len(r['path'])<=80 and r['op'] in FILTER_OPS
               and (type(r['value']) in (str,bool) or type(r['value']) in (int,float) and math.isfinite(r['value'])) for r in rules)


def rule_hides(rule,checks):
    """Missing data never hides a ticket: an absent field, or one of another type, leaves it visible."""
    value=checks
    for part in rule['path'].split('.'):
        value=value.get(part) if isinstance(value,dict) else None
    numeric=lambda v:type(v) in (int,float) and math.isfinite(v)
    if value is None or not (numeric(value) and numeric(rule['value']) or type(value) is type(rule['value']) and type(value) in (str,bool)):return False
    return FILTER_OPS[rule['op']](value,rule['value'])


def register(conn,*,epoch_id,actor_id,network_id,mint,pool,now,registration_clock=None):
    """First registration wins. Legacy known_at is host time; its original corrected clock binding is an append-only receipt."""
    if any(type(v) is not str or not v or len(v)>256 for v in (network_id,mint)) or (pool is not None and (type(pool) is not str or len(pool)>256)):
        return dict(state='refused',reason='candidate_invalid')
    with S.write_transaction(conn):
        inserted=conn.execute('INSERT OR IGNORE INTO office_v2_candidates VALUES(?,?,?,?,?,?)',(epoch_id,network_id,mint,pool,actor_id,now)).rowcount
        if inserted:
            binding=SS.clock_binding(epoch=epoch_id,network=network_id,mint=mint,pool=pool,actor=actor_id,host=now,sample=registration_clock)
            if registration_clock is not None:
                if binding is None:binding=dict(kind='candidate_registration_clock/v1',state='unknown',reason='registration_clock_unvalidated',cache_key=SS.registration_key(epoch_id,network_id,mint),epoch_id=epoch_id,network_id=network_id,mint=mint,registered_by=actor_id,known_at_host=now)
                S.append(conn,'receipts',binding)
        row=conn.execute('SELECT known_at,actor_id,pool FROM office_v2_candidates WHERE epoch_id=? AND network_id=? AND mint=?',(epoch_id,network_id,mint)).fetchone()
    clock=SS.registration_time(conn,epoch_id,network_id,mint,row)
    return dict(state='registered',known_at=row[0],first_actor=row[1],clock_state=clock['state'],registration_clock_receipt=clock.get('clock_receipt'),corrected_known_at=clock.get('known_at'))


def entry_halt(conn,*,epoch_id):
    """The one halt state, for the order gate and for what a seat is shown. never_set is no receipt at all: entries are open, but nobody has resumed anything."""
    row=conn.execute('SELECT action,actor_id,at,reason FROM office_v2_halts WHERE epoch_id=? ORDER BY seq DESC LIMIT 1',(epoch_id,)).fetchone()
    if row is None:return dict(state='never_set',entries_halted=False,epoch_id=epoch_id,receipt=None)
    return dict(state='halted' if row[0]=='halt' else 'resumed',entries_halted=row[0]=='halt',epoch_id=epoch_id,receipt=dict(action=row[0],actor_id=row[1],at=row[2],reason=row[3]))


def halted(conn,*,epoch_id):
    return entry_halt(conn,epoch_id=epoch_id)['entries_halted']


def set_halt(conn,*,epoch_id,actor_id,role,action,reason,now,book_state=None):
    """Clearing is the operator's or the Head's logged resume, never a Judge's."""
    if role not in ('head','operator') or action not in ('halt','resume') or type(reason) is not str or not reason.strip():
        return dict(state='refused',reason='halt_not_allowed')
    with S.write_transaction(conn):
        conn.execute('INSERT INTO office_v2_halts(epoch_id,action,actor_id,reason,at,book_state) VALUES(?,?,?,?,?,?)',(epoch_id,action,actor_id,reason[:500],now,book_state))
    return dict(state=action)


def source_of(tickets_known_at,*,cut):
    """`tickets_known_at`: when each feed ticket for this coin became known. A ticket after the cut does not make the order feed."""
    return 'feed' if any(L._num(t) and t<=cut for t in tickets_known_at) else 'self'


REVERSIBLE_REFUSALS=frozenset({'epoch_clock_regressed','epoch_equity_unknown','epoch_mark_stale','fx_unknown'})   # a book state, not a verdict on the coin; no order was made


def order_id_for(epoch_id,book_id,network_id,mint):
    """One entry per coin per epoch, so the same id across incarnations: a retry after a swap finds the first order, it cannot make a second."""
    return hashlib.sha256(S.canonical_json([epoch_id,book_id,network_id,mint])).hexdigest()


ORDER_ROLES=('judge','judge_gpt')   # two roles because a role held by two seats cannot receive mail


def enter(store_conn,ledger_conn,policy,*,epoch_id,book_id,actor_id,role,incarnation,candidate,feed_known_at,reason,labels,now,sol_usd=None,sol_at=None,ledger_epoch_id=None,stratum=None):
    """-> {state, reason, order_id, source}. state submitted, refused or unknown; unknown never means no order was considered."""
    if role not in ORDER_ROLES:return dict(state='refused',reason='role_cannot_order')
    if type(reason) is not str or not reason.strip() or len(reason)>160:return dict(state='refused',reason='reason_invalid')
    try:
        seat=store_conn.execute('SELECT epoch_id,incarnation,sealed FROM office_v2_roster WHERE actor_id=?',(actor_id,)).fetchone()
        if seat is None or seat[0]!=epoch_id or seat[2]:return dict(state='refused',reason='seat_not_in_epoch')
        if seat[1]!=incarnation:return dict(state='refused',reason='stale_incarnation')
        if halted(store_conn,epoch_id=epoch_id):return dict(state='refused',reason='book_halted')
        request=candidate.get('request') if isinstance(candidate,dict) else None
        network_id=(candidate or {}).get('network_id') or (request or {}).get('network_id')
        mint=candidate.get('mint') if isinstance(candidate,dict) else None
        if type(network_id) is not str or type(mint) is not str or not mint:return dict(state='refused',reason='candidate_invalid')
        source=source_of(feed_known_at,cut=now)
        if source=='self':
            registered=store_conn.execute('SELECT 1 FROM office_v2_candidates WHERE epoch_id=? AND network_id=? AND mint=?',(epoch_id,network_id,mint)).fetchone()
            return dict(state='refused',reason='self_sheet_unavailable' if registered else 'candidate_not_registered',source='self')
        order_id=order_id_for(epoch_id,book_id,network_id,mint)
        # Receipts are immutable, so a reversible refusal moves to the coin's next attempt id. The chain has one open end: two writers meet on the same key, so never two orders.
        attempt=0
        while (prior:=T.lookup_decision(ledger_conn,action_key=order_id,decision_id=order_id,book_id=book_id)) is not None:
            if prior['outcome']!='refused' or prior['reason'] not in REVERSIBLE_REFUSALS:
                return dict(state='duplicate',reason=prior['reason'],order_id=prior['order_id'],source=source,outcome=prior['outcome'])
            attempt+=1;order_id=hashlib.sha256(S.canonical_json([epoch_id,book_id,network_id,mint,attempt])).hexdigest()
        context=Context(actor_id=actor_id,book_id=book_id,epoch_id=ledger_epoch_id or epoch_id,decision_id=order_id,deadline_at=now+DECISION_S,cut=dict(knowledge_cut=now))
        refused=EO._scope(ledger_conn,context,now)
        if refused:return dict(state='refused',reason=refused)
        with S.write_transaction(store_conn):
            receipt=EO._receipt(store_conn,context,candidate,now)
            age=now-receipt['clock_sample']['corrected_utc']
            if not 0<=age<=MAX_SHEET_AGE_S:return dict(state='refused',reason='sheet_stale',source=source,sheet_age_s=age)
            bundle_hash=SH.freeze_bundle(store_conn,order_id,receipt['cache_key'],definition_bundle_hash=receipt['definition_bundle_hash'],strategy_epoch=receipt['strategy_epoch'])
        bundle,risk=P.risk_link(policy,'ENTER',order_id,bundle_hash,receipt)
        body=receipt['sheet']
        metadata={'ticker':body.get('ticker',body.get('symbol')),'decimals':(body.get('pool_state') or {}).get('base_decimals'),'metadata_known_at':receipt['clock_sample']['corrected_utc'],'sheet_expires_at':now-age+MAX_SHEET_AGE_S}
        if stratum is not None:metadata['stratum']=stratum        # written by the ledger inside the order's own insert transaction
        # One label per attempt, tied to its decision. The order's own label is the one whose hash equals decision_receipts.judgements_hash, so an abandoned attempt can never pass as the submitter.
        labels=dict(labels or {},attempt_n=attempt,attempt_chain=order_id_for(epoch_id,book_id,network_id,mint))
        decision=dict(actor_id=actor_id,incarnation=incarnation,reason=reason,source=source,labels=labels,at=now,sheet_age_s=age,receipt=receipt['row_hash'],bundle=bundle_hash)
        decision_hash=SH._sha(decision)
        # Written first: the store and the ledger are two databases, so this order means a crash leaves a label with no order, never an order with no source.
        with S.write_transaction(store_conn):
            store_conn.execute('INSERT OR IGNORE INTO office_v2_order_labels VALUES(?,?,?,?,?,?,?,?,?)',(order_id,decision_hash,epoch_id,actor_id,incarnation,source,age,now,json.dumps(labels,sort_keys=True,default=str)))
        result=T.execute_decision(ledger_conn,policy,context=context,action_key=order_id,bundle=bundle,risk_result=risk,now=now,sol_usd=sol_usd,sol_at=sol_at,order_metadata=metadata,
                                  decision_evidence={'judgements_hash':decision_hash,'decision_label':'ENTER'})
        return dict(state='submitted' if result['outcome']=='submitted' else 'refused',reason=result['reason'],order_id=result['order_id'],source=source,sheet_age_s=age,bundle_hash=bundle_hash)
    except sqlite3.Error:return dict(state='unknown',reason='database_unknown')
    except (ValueError,TypeError,KeyError,AttributeError) as exc:
        return dict(state='unknown',reason='order_proof_unknown:'+type(exc).__name__)


def order_label(store_conn,ledger_conn,order_id):
    """The label of the decision that actually submitted. None means label_missing: its source is unknown, never feed."""
    receipt=ledger_conn.execute('SELECT judgements_hash FROM decision_receipts WHERE order_id=?',(order_id,)).fetchone()
    if receipt is None:return None
    row=store_conn.execute('SELECT actor_id,incarnation,source,sheet_age_s,labels_json FROM office_v2_order_labels WHERE order_id=? AND decision_hash=?',(order_id,receipt[0])).fetchone()
    return None if row is None else dict(actor_id=row[0],incarnation=row[1],source=row[2],sheet_age_s=row[3],labels=json.loads(row[4]))
