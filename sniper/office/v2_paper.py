"""The host paper service behind a seat's paper tools. Holds the ledger and the sheet path, so a seat supplies only a ticket id and a reason. Paper only; no funded write."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from . import seat_facts,wallet_activity
from . import ledger as L, operating_view as OV, runtime as RT, seat_events as E, store as S, v2_orders as O,self_sheet as SS
from .tools import sheet as SH


RESULTS_MAX=60


SEAT_RAILS=('sl_pct','tp_pct','trail_pct')   # the catastrophe rail, clip, concurrency and daily halt are not a seat's to change


def _rail_refusal(policy,rails):
    """A seat's rails must each sit inside the policy's own bound; a rail with no bound cannot be changed."""
    for name,value in rails.items():
        bound=policy.rail_bounds.get(name) if name in SEAT_RAILS else None
        if bound is None or type(value) not in (int,float) or not bound[0]<=value<=bound[1]:return 'rail_out_of_bounds:'+str(name)[:32]
    return None


def _epoch_seconds(value):
    """Desk stamps are ISO text; anything else is unknown, which cannot make an order feed."""
    try:return datetime.fromisoformat(value).timestamp()
    except (TypeError,ValueError):return None


WALLET_WINDOW_S=300


def largest_wallet(desk_dir,pool,now_host,window_s=WALLET_WINDOW_S):
    """Largest net buyer on the retained tape over the window, in SOL and against the pool's newest SOL reserve. Reported, never a gate. No rows is unknown, not zero."""
    try:
        conn=sqlite3.connect(Path(desk_dir,'pumpswap/tape.db').resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)
        with closing(conn):
            rows=conn.execute('SELECT actor,is_buy,quote_atomic,post_quote_reserve_atomic FROM swaps_ps INDEXED BY swaps_ps_pool_received WHERE pool=? AND received_at>=? AND received_at<=? ORDER BY received_at LIMIT 5000',(pool,now_host-window_s,now_host)).fetchall()
        net={}
        for actor,is_buy,quote,_ in rows:
            if actor and quote is not None and is_buy in (0,1):net[actor]=net.get(actor,0)+(int(quote) if is_buy else -int(quote))
        if not net:return dict(state='unknown',reason='no_tape_rows',window_s=window_s)
        actor,value=max(net.items(),key=lambda kv:kv[1])
        reserve=next((int(r[3]) for r in reversed(rows) if r[3] is not None),None)
        return dict(state='partial' if len(rows)>=5000 else 'known',window_s=window_s,rows=len(rows),actor=actor,net_sol=value/1e9,pct_of_pool_sol=value/reserve*100 if reserve else None)
    except (OSError,sqlite3.Error,ValueError,TypeError):return dict(state='unknown',reason='tape_unreadable',window_s=window_s)


FEED_FACTS=dict(top10_pct=('holders','top10_pct'),creator_pct=('holders','creator_pct'),cluster_status=('cluster','status'),cluster_reason=('cluster','reason'),
                dump_buyer_share=('dump_group','buyer_share'),sell_round_trip_loss_pct=('sell_probe','round_trip_loss_pct'))


def _path(row,marks,points=24):
    """At most `points` marks, % against spend, seconds from fill; a null mark stays a gap. Always keeps the worst and the last."""
    try:spend=int(row['spend_lamports'] or 0)
    except (TypeError,ValueError):spend=0
    priced=[m for m in marks if m['lamports'] is not None]
    if not spend or not row['filled_at'] or not marks:return []
    keep=marks[::max(1,len(marks)//points)][:points]+[marks[-1]]+([min(priced,key=lambda m:int(m['lamports'])),priced[-1]] if priced else [])
    unique={m['at']:m for m in keep}
    return [dict(t_s=round(m['at']-row['filled_at']),pct=None if m['lamports'] is None else round((int(m['lamports'])/spend-1)*100,1),kind=m['binding_kind']) for _,m in sorted(unique.items())]   # pct None is a real gap


def _sheet_facts(store,order_id):
    """The sheet's facts at the order, from the frozen bundle. Unknown stays unknown."""
    try:
        bundle=store.execute('SELECT payload_json FROM decision_bundles WHERE decision_id=? ORDER BY seq DESC LIMIT 1',(order_id,)).fetchone()
        hit=store.execute('SELECT payload_json FROM receipts WHERE row_hash=?',(json.loads(bundle[0])['receipt_row_hash'],)).fetchone() if bundle else None   # the frozen receipt, not the newest for this key
        sheet=json.loads(hit[0])['sheet'] if hit else None
        if not isinstance(sheet,dict):return dict(state='unknown')
        return dict(state='known',safety=sheet.get('safety'),**{k:sheet.get(k) for k in ('mcap_usd','age_s','flow_60s','return_60s_pct','venue','missing') if k in sheet})
    except (sqlite3.Error,ValueError,TypeError,KeyError):return dict(state='unknown')


class PaperService:
    def __init__(self,*,epoch_id,book_id,store_path,ledger_path,desk_path,desk_dir,clock_file,policy,segment_of,clock=time.time,sheet=None,pool_state_reader=None,cohort_of=lambda:None,policy_sha256=None,ledger_epoch_id=None,policy_file=None,self_sheet=None):
        self.policy_sha256,self.policy_file=policy_sha256,policy_file
        self.pool_state_reader,self.cohort_of=pool_state_reader,cohort_of
        self.release_sha=None;self.funded_db=None;self.results_ledgers=None
        self.epoch_id,self.book_id,self.store_path,self.ledger_path=epoch_id,book_id,store_path,ledger_path
        self.ledger_epoch_id=ledger_epoch_id or epoch_id   # a seat's own book can live in its own ledger epoch; roster, mail and halts stay on epoch_id
        self.desk_path,self.desk_dir,self.clock_file,self.policy,self.segment_of,self.clock=desk_path,desk_dir,clock_file,policy,segment_of,clock
        self.sheet=sheet or self._sheet
        self.coin_sheet=self_sheet or SS.build

    def _store(self):
        return S.open_store(Path(self.store_path))   # its receipt path requires the store's own pragmas

    def _role(self,conn,actor_id,incarnation=None):
        """The role of a seat in this epoch, now. A sealed, foreign-epoch or stale-incarnation seat has none. `incarnation` None is only for enter, where O.enter makes the same check."""
        row=conn.execute('SELECT role,epoch_id,sealed,incarnation FROM office_v2_roster WHERE actor_id=?',(actor_id,)).fetchone()
        if row is None or row[1]!=self.epoch_id or row[2] or (incarnation is not None and row[3]!=incarnation):return None
        return row[0]

    def _sheet(self,store_conn,ticket_id,*,now_host):
        """The near-pass tool's own path, so a seat's order uses the sheet the Office already reviewed."""
        return self._tools(store_conn)['desk.near_pass_sheet'].handler({'ticket_id':ticket_id},None).result

    def _tools(self,store_conn):
        return RT.near_pass_tools(store_conn,self.desk_path,self.desk_dir,self.clock_file,pool_state_reader=self.pool_state_reader,cohort_of=self.cohort_of)

    def feed(self):
        """Tickets a Judge can order on, as the office's near-pass list gives them."""
        with closing(self._store()) as store:
            return self._tools(store)['desk.near_pass_list'].handler({},None).result

    def candidate_lists(self):
        from .seat_candidates import read,FILE_NAME
        return read(Path(self.store_path).parent/FILE_NAME,self.clock())

    def feed_liquidity(self):
        """Liquidity change for the listed tickets, its own read: the feed list runs close to its output limit. Reported only; nothing orders on it."""
        from .feed_liquidity import report
        return report(self.feed(),get_ticket=self._ticket,desk_dir=self.desk_dir,now=self.clock())

    def _checks(self,ticket_id):
        """The hard filter's own checks for a ticket; {} when unreadable, which hides nothing."""
        try:
            conn=sqlite3.connect(Path(self.desk_path).resolve().as_uri()+'?mode=ro',uri=True,timeout=1.)
            with closing(conn):
                row=conn.execute("SELECT substr(inputs_json,1,65536) FROM desk_tickets WHERE ticket_id=? AND stage='hard_filter' ORDER BY id DESC LIMIT 1",(ticket_id,)).fetchone()
            return json.loads(row[0])['evidence']['checks'] if row else {}
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):return {}

    def _ticket_evidence(self,ticket_id):
        try:
            conn=sqlite3.connect(Path(self.desk_path).resolve().as_uri()+'?mode=ro',uri=True,timeout=1.)
            with closing(conn):
                row=conn.execute("SELECT substr(inputs_json,1,65536) FROM desk_tickets WHERE ticket_id=? AND stage='hard_filter' ORDER BY id DESC LIMIT 1",(ticket_id,)).fetchone()
            return json.loads(row[0])['evidence'] if row else None
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):return None

    def _ticket_pool(self,ticket_id):
        return (self._ticket_evidence(ticket_id) or {}).get('pool')

    def coin_facts(self,mint,pool):
        """Run-up and other books' closes, plain, for a feed ticket or a sheet read. Reported, never a gate."""
        now=self.clock()
        return dict(run_up=seat_facts.run_up(self.desk_dir,pool,now) if pool else dict(state='unknown',reason='pool_unknown'),
                    closes_on_this_coin=seat_facts.closes_on_coin(self.results_ledgers or (self.ledger_path,),mint,now) if mint else dict(state='unknown',reason='mint_unknown'))

    def ticket_facts(self,ticket_id):
        evidence=self._ticket_evidence(ticket_id) or {}
        return self.coin_facts(evidence.get('mint'),evidence.get('pool'))

    def position_triggers(self,*,actor_id,seen):
        """New triggers on this seat's open positions, once per position and trigger. Wakes cannot catch a one-slot crash: a wake takes longer than the slot."""
        out=[]
        with closing(self._store()) as store,closing(L.open_ledger(Path(self.ledger_path))) as ledger:
            for row in ledger.execute("SELECT * FROM orders WHERE book_id=? AND state='open' AND exit_requested_at IS NULL",(self.book_id,)).fetchall():
                label=O.order_label(store,ledger,row['order_id'])
                if label is None or label['actor_id']!=actor_id:continue
                marks=[dict(r) for r in ledger.execute('SELECT lamports,binding_kind FROM marks WHERE order_id=? ORDER BY id DESC LIMIT 5',(row['order_id'],))][::-1]
                for name,facts in seat_facts.triggers(dict(row),marks,self.desk_dir,self.clock()).items():
                    if row['order_id']+':'+name not in seen:out.append(dict(order_id=row['order_id'],mint=row['mint'],trigger=name,**facts))
        return out

    def set_feed_filter(self,*,actor_id,incarnation,rules,reason,after_case=None):
        if type(reason) is not str or not reason.strip():return dict(state='refused',reason='reason_required')
        if not O.valid_rules(rules):return dict(state='refused',reason='rules_invalid',hint='up to 20 of {path,op,value}; op gt lt gte lte eq ne; path is dotted under the ticket checks, e.g. holders.top10_pct')
        with closing(self._store()) as store:
            if self._role(store,actor_id,incarnation) is None:return dict(state='refused',reason='seat_not_in_epoch')
            # The change log comes first: selection changes per seat, one segment line, no epoch reset. No line, no filter.
            if self.release_sha is not None:
                from . import v2_explore as X
                try:X.record_change(store,epoch_id=self.epoch_id,at=self.clock(),release_sha=self.release_sha,content_hash=hashlib.sha256(json.dumps([actor_id,rules],sort_keys=True).encode()).hexdigest(),kind='config',note='seat feed filter '+actor_id+(' after case '+str(after_case)[:64] if after_case else ''))
                except ValueError:return dict(state='refused',reason='filter_change_unlogged')
            with S.write_transaction(store):
                store.execute('INSERT INTO office_v2_feed_filters(epoch_id,actor_id,at,rules_json,reason,after_case) VALUES(?,?,?,?,?,?)',(self.epoch_id,actor_id,self.clock(),json.dumps(rules),reason[:500],None if after_case is None else str(after_case)[:128]))
        return dict(state='known',rules=rules)

    def _hidden(self,actor_id,ticket_ids):
        """{ticket_id: rule} under this seat's newest filter. Every hidden ticket is logged, so the sample it dropped stays visible."""
        out={}
        with closing(self._store()) as store:
            row=store.execute('SELECT seq,rules_json FROM office_v2_feed_filters WHERE epoch_id=? AND actor_id=? ORDER BY seq DESC LIMIT 1',(self.epoch_id,actor_id)).fetchone()
            if row is None or not json.loads(row[1]):return out
            rules=json.loads(row[1])
            for ticket in ticket_ids:
                checks=self._checks(ticket)
                rule=next((r for r in rules if O.rule_hides(r,checks)),None)
                if rule is not None:out[ticket]=rule
            if out:
                with S.write_transaction(store):
                    store.executemany('INSERT OR IGNORE INTO office_v2_feed_hidden VALUES(?,?,?,?,?,?)',[(self.epoch_id,actor_id,t,row[0],json.dumps(r),self.clock()) for t,r in out.items()])
        return out

    def curated_feed(self,*,actor_id,include_hidden=False):
        """The feed through this seat's own filter, with each ticket's holder, cluster and dump facts plainly on it."""
        out=self.feed()
        items=out.get('items')
        if not isinstance(items,list):return out
        with closing(self._store()) as store,S.write_transaction(store):   # only a real listing counts as seen; a wake probe or an order's label does not
            store.executemany('INSERT OR IGNORE INTO office_v2_feed_seen VALUES(?,?,?,?)',[(self.epoch_id,actor_id,i.get('ticket_id'),self.clock()) for i in items])
        hidden=self._hidden(actor_id,[i.get('ticket_id') for i in items])
        kept=[]
        for item in items:
            checks=self._checks(item.get('ticket_id'))
            item['facts']={k:checks.get(a,{}).get(b) if isinstance(checks.get(a),dict) else None for k,(a,b) in FEED_FACTS.items()}
            pool=self._ticket_pool(item.get('ticket_id'))
            mint=(self._ticket_evidence(item.get('ticket_id')) or {}).get('mint')
            item['facts']['followed_wallets']=wallet_activity.touched(self.funded_db,mint) if self.funded_db and mint else dict(state='unknown',reason='not_available')
            evidence=self._ticket_evidence(item.get('ticket_id')) or {}
            item['facts'].update(self.coin_facts(evidence.get('mint'),evidence.get('pool')))
            item['facts']['largest_wallet']=largest_wallet(self.desk_dir,pool,self.clock()) if pool else dict(state='unknown',reason='pool_unknown')
            rule=hidden.get(item.get('ticket_id'))
            if rule is None:kept.append(item)
            elif include_hidden:kept.append(dict(item,hidden_by=rule))
        return dict(out,items=kept,hidden_count=len(hidden))

    def visible_tickets(self,*,actor_id,ticket_ids):
        hidden=self._hidden(actor_id,ticket_ids)
        return [t for t in ticket_ids if t not in hidden]

    def read_sheet(self,*,ticket_id):
        """The same build as admission: what a seat reads is what its order is checked against."""
        with closing(self._store()) as store:
            got=self.sheet(store,ticket_id,now_host=self.clock())
        if got.get('state')=='known':return got
        # A seat reads what the ticket holds with the rest typed unknown, never nothing. Only the read: enter() builds its own sheet and still needs state known.
        try:ticket=self._ticket(ticket_id)
        except sqlite3.Error:return got
        if ticket is None:return got
        from .partial_sheet import partial_sheet
        view=partial_sheet(ticket,got,now_host=self.clock())
        if isinstance(view.get('sheet'),dict):   # the failed sheet still shows cluster and holders; the LP read needs no sheet, the cap and cache bound it
            from . import safety_evidence as SE
            from sniper.services import memecoin_desk_contract as K
            stage=next((s for s in ticket.get('stages',[]) if isinstance(s,dict) and s.get('stage')=='hard_filter'),{})
            checks=((stage.get('inputs') or {}).get('evidence') or {}).get('checks')
            cand=ticket.get('candidate') if isinstance(ticket.get('candidate'),dict) else {}
            _,_,view['sheet']['safety_evidence']=SE.build(desk_dir=self.desk_dir,mint=ticket.get('mint',cand.get('mint')),pool=ticket.get('pool',cand.get('pool')),as_of=self.clock(),soft=K.soft_facts(checks),reader=self.pool_state_reader,checks=checks)
        return dict(view,ps_pending_gaps=got['ps_pending_gaps']) if 'ps_pending_gaps' in got else view

    def _ticket(self,ticket_id):
        from sniper.persistence import db_memecoin_desk as desk
        conn=sqlite3.connect(Path(self.desk_path).resolve().as_uri()+'?mode=ro',uri=True,timeout=1.);conn.row_factory=sqlite3.Row
        with closing(conn):return desk.get_ticket(conn,ticket_id)

    def _policy_current(self):
        """The last check before an order: the policy this service holds must still be the file's. An override takes effect only at a rebuild, so no order runs on one policy while the record says another."""
        if self.policy_file is None:return True
        from . import eval_transition as X
        try:return X.book_policy_identity(json.loads(Path(self.policy_file).read_text())['policy'])['sha256']==self.policy_sha256
        except (OSError,ValueError,KeyError,TypeError):return False

    def enter(self,*,actor_id,incarnation,ticket_id,reason,labels,rails=None):
        if not self._policy_current():return dict(state='refused',reason='book_policy_restart_needed')
        policy=self.policy
        if rails:
            if os.environ.get('OFFICE_SEAT_STRATEGY','1')=='0':return dict(state='refused',reason='seat_strategy_off')
            bad=_rail_refusal(policy,rails)
            if bad:return dict(state='refused',reason=bad)
            policy=replace(policy,**rails);labels=dict(labels,rail_override=dict(rails))   # frozen on the order by _submit
        from scripts.office_paper_watcher import read_fx
        from sniper.services.memecoin_desk_intake import clock_sample
        host=self.clock()
        offset,sampled_at=clock_sample(self.clock_file,now=host)
        if offset is None:return dict(state='unknown',reason='clock_unknown')
        now=host-offset
        try:ticket=self._ticket(ticket_id)
        except sqlite3.Error:return dict(state='unknown',reason='desk_unavailable')
        if ticket is None:return dict(state='refused',reason='ticket_unknown')
        with closing(self._store()) as store,closing(L.open_ledger(Path(self.ledger_path))) as ledger:
            role=self._role(store,actor_id)
            if role not in O.ORDER_ROLES:return dict(state='refused',reason='role_cannot_order')
            got=self.sheet(store,ticket_id,now_host=host)
            partial=None
            if got.get('state')!='known':   # a provider timeout stays unknown: only the sheet's own refusal is a refusal
                from . import partial_order as PO
                part=self._partial(ticket,got,offset=offset,sampled_at=sampled_at,host=host,store=store) if PO.enabled() else None
                if part is None or part.get('state')!='ok':   # the known-only gate, unchanged, when the partial path does not apply (knob off, class not allowed, evidence missing)
                    return dict(state='refused' if got.get('state')=='refused' else 'unknown',reason='sheet_'+str(got.get('reason'))[:120])
                partial=part
            # The sheet took time and its receipt carries a fresh clock, so the decision cut comes after it, from the host's clock.
            host=self.clock()
            offset,_=clock_sample(self.clock_file,now=host)
            if offset is None:return dict(state='unknown',reason='clock_unknown')
            now=host-offset
            if partial is None:
                hit=S.latest(store,'receipts','cache_key',got['cache_key'])
                if hit is None:return dict(state='unknown',reason='receipt_missing')
                request=json.loads(hit['payload_json'])['request']
                candidate=dict(network_id=request['network_id'],receipt_row_hash=hit['row_hash'],cache_key=got['cache_key'],sheet_hash=got['sheet_hash'],mint=request['mint'],pool=request['pool'],
                               feed=request.get('feed','pass'),sheet=got['sheet'],lenses=got['lenses'])
                known_at=[_epoch_seconds(s.get('ts')) for s in ticket.get('stages',[]) if s.get('stage')=='hard_filter']
                vq=(got['sheet'].get('pool_state') or {}).get('virtual_quote_source');extra={}
                stratum=dict(sheet_state='known',sheet_class=None,sheet_ticket_age_s=None,sheet_unknown_n=None,sheet_view_hash=None,sells_basis='fresh_flow',sells_n=None,probe_age_s=None,probe_loss_pct=None)
            else:
                candidate,known_at,vq=partial['candidate'],partial['known_at'],None
                o=partial['order'];stratum=o;extra=dict(sheet_state='partial',sheet_class=o['sheet_class'],sells_basis=o['sells_basis'],ticket_age_s=o['sheet_ticket_age_s'],sheet_view_hash=o['sheet_view_hash'],partial_change='partial-sheet-orders-v1')
            labels=dict(labels,largest_wallet=largest_wallet(self.desk_dir,candidate['pool'],host),hidden_from_seat=ticket_id in self._hidden(actor_id,[ticket_id]))   # both sheet kinds; logged, not shown to the seat
            fx,fx_at=read_fx(self.desk_dir,now_host=host,offset=offset)
            if fx is None:return dict(state='unknown',reason='fx_unknown')   # no quote is not a veto, and never a default price
            result=O.enter(store,ledger,policy,epoch_id=self.epoch_id,ledger_epoch_id=self.ledger_epoch_id,book_id=self.book_id,actor_id=actor_id,role=role,incarnation=incarnation,candidate=candidate,feed_known_at=known_at,
                           reason=reason,labels=dict(labels,segment_id=self.segment_of(store),book_policy_sha256=self.policy_sha256,virtual_quote_source=vq,**extra),now=now,sol_usd=fx,sol_at=fx_at,stratum=stratum)
            return result

    def _partial(self,ticket,got,*,offset,sampled_at,host,store):
        """The partial receipt for a ticket whose full sheet failed, or None. Reads the cohort from the desk store (read-only); any failure is no partial path, never a guess."""
        from . import partial_order as PO
        from sniper.persistence import db_memecoin_desk as desk
        try:
            conn=sqlite3.connect(Path(self.desk_path).resolve().as_uri()+'?mode=ro',uri=True,timeout=1.);conn.row_factory=sqlite3.Row
            with closing(conn):cohort=desk.get_cohort(conn,ticket.get('cohort_id'))
            return PO.candidate(store,ticket,cohort,got,offset=offset,sampled_at=sampled_at,host=host,desk_dir=self.desk_dir)
        except (sqlite3.Error,ValueError,TypeError,KeyError,OSError):return None

    def request_exit(self,*,actor_id,incarnation,order_id,reason):
        """A Judge's discretionary exit of an open order of this book. It runs through the same delayed exit as a rail."""
        if os.environ.get('OFFICE_SEAT_STRATEGY','1')=='0':return dict(state='refused',reason='seat_strategy_off')
        if type(order_id) is not str or not order_id or type(reason) is not str or not reason.strip() or len(reason)>160:return dict(state='refused',reason='exit_request_invalid')
        from sniper.services.memecoin_desk_intake import clock_sample
        host=self.clock()
        offset,_=clock_sample(self.clock_file,now=host)
        if offset is None:return dict(state='unknown',reason='clock_unknown')
        with closing(self._store()) as store:
            if self._role(store,actor_id,incarnation) not in O.ORDER_ROLES:return dict(state='refused',reason='role_cannot_exit')
        try:
            with closing(L.open_ledger(Path(self.ledger_path))) as ledger,L._write(ledger):
                row=ledger.execute('SELECT epoch_id,book_id,state,exit_requested_at FROM orders WHERE order_id=?',(order_id,)).fetchone()
                if row is None or row['epoch_id']!=self.ledger_epoch_id or row['book_id']!=self.book_id:return dict(state='refused',reason='order_scope_mismatch')
                if row['state']!='open':return dict(state='refused',reason='not_open')
                if row['exit_requested_at'] is not None:return dict(state='refused',reason='exit_already_requested')
                result=L.request_exit(ledger,order_id=order_id,reason=reason,requested_by=actor_id,now=host-offset,exit_by='judge')
                return dict(state='submitted',order_id=order_id) if result.ok else dict(state='refused',reason=result.reason)
        except (OSError,sqlite3.Error):return dict(state='unknown',reason='ledger_unreadable')

    def _coin_context(self,store,*,actor_id,incarnation,network_id,mint,pool,host):
        if self._role(store,actor_id,incarnation) is None:return dict(state='refused',reason='seat_not_in_epoch')
        if not SS.logged(store,self.epoch_id):return dict(state='refused',reason='ticketless_change_unlogged')
        if any(type(v) is not str or not v.strip() or len(v)>256 for v in (network_id,mint)) or (pool is not None and (type(pool) is not str or not pool or len(pool)>256)):
            return dict(state='refused',reason='candidate_invalid')
        if network_id!='solana':return dict(state='refused',reason='chain_not_supported')
        from sniper.services.memecoin_desk_intake import clock_sample
        offset,_=clock_sample(self.clock_file,now=host)
        if not L._num(offset) or not L._num(host) or host-offset<0:return dict(state='unknown',reason='clock_unknown')
        feed=SS.feed_lookup(self.desk_path,network_id,mint,cut_host=host)
        if feed['state']!='known':return feed
        if feed['source']=='feed':return dict(state='refused',reason='feed_ticket_required',source='feed',ticket_id=feed['ticket_id'])
        reg=store.execute('SELECT known_at,actor_id,pool FROM office_v2_candidates WHERE epoch_id=? AND network_id=? AND mint=?',(self.epoch_id,network_id,mint)).fetchone()
        if reg is None:return dict(state='refused',reason='candidate_not_registered')
        registration=SS.registration_time(store,self.epoch_id,network_id,mint,reg)
        if registration['state']!='known':return registration
        if not 0<=registration['known_at']<=host-offset:return dict(state='refused',reason='registration_not_prior',source='self')
        if reg[2] is not None and pool is not None and reg[2]!=pool:return dict(state='refused',reason='registered_pool_mismatch')
        return dict(state='known',offset=offset,pool=reg[2] if reg[2] is not None else pool,registration=registration)


    def read_self_sheet(self,*,actor_id,incarnation,network_id,mint,pool=None):
        try:
            with closing(self._store()) as store:
                host=self.clock()
                context=self._coin_context(store,actor_id=actor_id,incarnation=incarnation,network_id=network_id,mint=mint,pool=pool,host=host)
                if context['state']!='known':return context
                return self.coin_sheet(network_id=network_id,mint=mint,pool=context['pool'],now=host-context['offset'],registration=context['registration'])
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):return dict(state='unknown',reason='self_sheet_unknown')


    def book_view(self,*,actor_id,incarnation):
        """Own book's open positions, typed equity and today's PnL against the halt. Nothing closed: results stay out of a seat's reach. Head and Judge only."""
        from scripts.office_paper_watcher import read_fx
        from sniper.services.memecoin_desk_intake import clock_sample
        with closing(self._store()) as store:
            if self._role(store,actor_id,incarnation) not in ('head',)+O.ORDER_ROLES:return dict(state='refused',reason='role_cannot_view_book')
            try:entry_halt=O.entry_halt(store,epoch_id=self.epoch_id)
            except sqlite3.Error:entry_halt=dict(state='unknown',entries_halted=None,epoch_id=self.epoch_id,receipt=None)
        host=self.clock()
        offset,_=clock_sample(self.clock_file,now=host)
        if offset is None:return dict(state='unknown',reason='clock_unknown')
        fx,fx_at=read_fx(self.desk_dir,now_host=host,offset=offset)
        with closing(L.open_ledger(Path(self.ledger_path))) as ledger:
            out=OV.view(ledger,book_id=self.book_id,now=host-offset,sol_usd=fx,sol_at=fx_at,daily_loss_usd=self.policy.daily_loss_usd,max_concurrent=self.policy.max_concurrent)
        # What was shown, not just its hash: the epoch record promises a reading conditioned on it. Written before the seat sees it.
        with closing(self._store()) as store,S.write_transaction(store):
            store.execute('INSERT OR IGNORE INTO office_v2_book_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)',(out['equity_snapshot_id'],self.epoch_id,actor_id,out['as_of'],out['equity']['state'],out['equity']['usd'],
                          out['equity']['unpriced_count'],out['daily_realized_usd'],len(out['positions']),int(out['daily_loss_halted'])))
        return dict(out,entry_halt=entry_halt)

    def postmortems(self,*,actor_id,seen,limit=3):
        """Closes of this seat's own orders it has not been shown: its reason and plan, the entry sheet's facts, the graded result and the price path. Host data, read-only."""
        from .call_grader import grade_call
        out=[]
        with closing(self._store()) as store,closing(L.open_ledger(Path(self.ledger_path))) as ledger:
            rows=ledger.execute("SELECT * FROM orders WHERE book_id=? AND state IN ('closed','censored') ORDER BY requested_at",(self.book_id,)).fetchall()
            for row in rows:
                if row['order_id'] in seen or len(out)>=limit:continue
                label=O.order_label(store,ledger,row['order_id'])
                if label is None or label['actor_id']!=actor_id:continue
                marks=[dict(r) for r in ledger.execute('SELECT at,lamports,binding_kind FROM marks WHERE order_id=? ORDER BY at,id',(row['order_id'],))]
                flows=[dict(r) for r in ledger.execute('SELECT kind,usd FROM cashflows WHERE order_id=?',(row['order_id'],))]
                out.append(dict(order_id=row['order_id'],mint=row['mint'],pool=row['pool'],state=row['state'],reason=row['reason'],source=label['source'],
                                entry_reason=label['labels'].get('entry_reason','not recorded before this release'),plan=label['labels'].get('plan','not recorded before this release'),
                                exit_trigger_kind=row['exit_trigger_kind'],exit_fill_kind=row['exit_fill_kind'],exit_requested_after_s=row['exit_requested_at']-row['filled_at'] if row['exit_requested_at'] and row['filled_at'] else None,
                                grade=grade_call(dict(row),flows,marks),entry_sheet=_sheet_facts(store,row['order_id']),largest_wallet_at_order=label['labels'].get('largest_wallet'),price_path=_path(row,marks),note='modelled paper; one trade is not a rule'))
        return out

    def order_audit(self,*,actor_id,incarnation,order_id):
        """The retained record of one of this seat's own orders. No re-fetch; what was not retained says so."""
        if not isinstance(order_id,str) or not 0<len(order_id)<=128:return dict(state='refused',reason='order_id_invalid')
        try:
            with closing(self._store()) as store,closing(L.open_ledger(Path(self.ledger_path))) as ledger:
                if self._role(store,actor_id,incarnation) is None:return dict(state='refused',reason='seat_not_in_epoch')
                row=ledger.execute('SELECT * FROM orders WHERE order_id=? AND book_id=?',(order_id,self.book_id)).fetchone()
                if row is None:return dict(state='refused',reason='order_not_in_this_book')
                label=O.order_label(store,ledger,order_id)
                if label is None or label['actor_id']!=actor_id:return dict(state='refused',reason='not_your_order')
                ordinal=ledger.execute('SELECT count(*) FROM orders WHERE book_id=? AND rowid<=(SELECT rowid FROM orders WHERE order_id=?)',(self.book_id,order_id)).fetchone()[0]   # insertion order: a later order at the same time cannot move it
                bundle=store.execute('SELECT payload_json FROM decision_bundles WHERE decision_id=? ORDER BY seq DESC LIMIT 1',(order_id,)).fetchone()
                bound=json.loads(bundle[0]) if bundle else {}
                hit=store.execute('SELECT payload_json FROM receipts WHERE row_hash=?',(bound.get('receipt_row_hash'),)).fetchone() if bound else None
                receipt=json.loads(hit[0]) if hit else {}
                sheet=receipt.get('sheet') or {}
                return dict(state='known',order_id=order_id,entry_ordinal_in_book=ordinal,policy=json.loads(row['policy_json']),policy_hash=row['policy_hash'],fill_model_version=row['fill_model_version'],
                            requested_at=row['requested_at'],filled_at=row['filled_at'],source=label['source'],sheet_age_s=label['sheet_age_s'],
                            entry_reason=label['labels'].get('entry_reason','not recorded before this release'),plan=label['labels'].get('plan','not recorded before this release'),rail_override=label['labels'].get('rail_override'),
                            bundle_hash=row['decision_bundle_hash'],sheet_hash=receipt.get('sheet_hash','unavailable'),receipt_row_hash=bound.get('receipt_row_hash','unavailable'),
                            sheet_as_of=receipt.get('clock_sample',{}).get('corrected_utc','unavailable'),coverage=(sheet.get('ps_facts') or {}).get('coverage','unavailable'),note='retained record; earlier reads of this sheet are not kept here')
        except (OSError,sqlite3.Error,ValueError,TypeError):return dict(state='unknown',reason='audit_unreadable')

    def results_view(self,*,actor_id,incarnation):
        """Every closed and censored order of the epoch, any seat's, newest first: learning needs the losers too. Open-order PnL stays with book_view. What was shown is hashed and stored before the seat sees it. results_asof is the read time."""
        with closing(self._store()) as store:
            if self._role(store,actor_id,incarnation) is None:return dict(state='refused',reason='seat_not_in_epoch')
        try:
            rows=[]
            for path in self.results_ledgers or (self.ledger_path,):   # both books: learning needs the other Judge's losers too
                with closing(L.open_ledger(Path(path))) as ledger:
                    rows+=[dict(r) for r in ledger.execute("SELECT order_id,epoch_id,mint,state,reason,exit_reason,requested_at,filled_at,closed_at,spend_usd,exit_usd,costs_usd,exit_fill_kind FROM orders "
                                                           "WHERE epoch_id IN (?,?,'office-v2-explore-gpt-1') AND state IN ('closed','censored') ORDER BY requested_at DESC LIMIT ?",(self.epoch_id,self.ledger_epoch_id,RESULTS_MAX+1))]
            rows.sort(key=lambda r:r['requested_at'],reverse=True)
        except (OSError,sqlite3.Error):return dict(state='unknown',reason='ledger_unreadable')   # never an empty list: that reads as no results
        items=[dict(r,net_usd=r['exit_usd']-r['spend_usd']-(r['costs_usd'] or 0.) if r['state']=='closed' and r['exit_usd'] is not None and r['spend_usd'] is not None else None) for r in rows[:RESULTS_MAX]]
        at=self.clock()
        out=dict(state='known',results_asof=at,truncated=len(rows)>RESULTS_MAX,items=items,note='modelled paper fills; no landing proof; small samples do not show skill; exit_fill_kind estimated_terminal is priced on an estimate, upper-biased, not outcome evidence')
        digest=hashlib.sha256(json.dumps(items,sort_keys=True,default=str).encode()).hexdigest()
        with closing(self._store()) as store,S.write_transaction(store):
            # One row per distinct result set per seat, counted: rereads can't grow it, and each new set is one row.
            store.execute('CREATE TABLE IF NOT EXISTS office_v2_results_reads(epoch_id TEXT,actor_id TEXT,sha256 TEXT,first_at REAL,last_at REAL,items_n INTEGER,reads INTEGER,order_ids_json TEXT,PRIMARY KEY(epoch_id,actor_id,sha256))')
            store.execute('INSERT INTO office_v2_results_reads VALUES(?,?,?,?,?,?,1,?) ON CONFLICT(epoch_id,actor_id,sha256) DO UPDATE SET last_at=excluded.last_at,reads=reads+1',(self.epoch_id,actor_id,digest,at,at,len(items),json.dumps([i['order_id'] for i in items])))
        return dict(out,results_sha256=digest)

    def register(self,*,actor_id,incarnation,network_id,mint,pool=None):
        with closing(self._store()) as store:
            if self._role(store,actor_id,incarnation) is None:return dict(state='refused',reason='seat_not_in_epoch')
            host=self.clock()
            from sniper.services.memecoin_desk_intake import clock_sample
            offset,at=clock_sample(self.clock_file,now=host)
            return O.register(store,epoch_id=self.epoch_id,actor_id=actor_id,network_id=network_id,mint=mint,pool=pool,now=host,registration_clock=dict(offset_s=offset,sampled_at_host=at))

    def set_halt(self,*,actor_id,incarnation,action,reason):
        with closing(self._store()) as store:
            return O.set_halt(store,epoch_id=self.epoch_id,actor_id=actor_id,role=self._role(store,actor_id,incarnation),action=action,reason=reason,now=self.clock())
