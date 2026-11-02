"""Bounded read-only display fields; never reprices historical fills with current FX."""
from copy import deepcopy
from decimal import Decimal,InvalidOperation
import json,math,sqlite3,time
from pathlib import Path
from .report import _ro

BOOKS={'office-v2-explore-1':'explore-ledger','office-v2-explore-gpt-1':'explore-gpt-ledger'}
FIELDS=('entry_unit_price_usd','exit_unit_price_usd','entry_mcap_usd','exit_mcap_usd')

def items(value,limit=200):return value[:limit] if isinstance(value,list) else []

def number(v):return type(v) in (int,float) and math.isfinite(v) and v>=0

def unit(usd,qty,decimals):
    if not number(usd) or type(decimals) is not int or not 0<=decimals<=18:return None
    if not isinstance(qty,str) or not qty.isascii() or not qty.isdigit() or len(qty)>80:return None
    try:
        q=Decimal(qty)
        if q<=0:return None
        value=float(Decimal(str(usd))*Decimal(10)**decimals/q)
        return value if number(value) and (value>0 or usd==0) else None
    except (InvalidOperation,ValueError,OverflowError):return None

def supply(store,row):
    hit=store.execute('SELECT row_hash,payload_json FROM decision_bundles WHERE decision_id=? LIMIT 1',(row['order_id'],)).fetchone()
    if not hit or hit['row_hash']!=row['decision_bundle_hash']:return None
    bundle=json.loads(hit['payload_json']);ref=bundle.get('receipt_row_hash')
    hit=store.execute('SELECT payload_json FROM receipts WHERE row_hash=? LIMIT 1',(ref,)).fetchone()
    if not hit:return None
    pool=json.loads(hit[0])['sheet']['pool_state'];at=pool.get('metadata_known_at')
    if type(pool.get('base_decimals')) is not int or type(row['decimals']) is not int or not 0<=row['decimals']<=18 or pool.get('base_decimals')!=row['decimals'] or not number(at) or not number(row['filled_at']) or at>row['filled_at']:return None
    qty=pool.get('supply_atomic')
    if not isinstance(qty,str) or not qty.isascii() or not qty.isdigit() or len(qty)>80 or int(qty)<=0:return None
    return Decimal(qty)/Decimal(10)**row['decimals'],qty,at

def enrich(root,payload):
    """Fresh payload copy; exact book/order/epoch identity and snapshot cut, at most 400 rows."""
    out=deepcopy(payload)
    if not isinstance(out,dict) or out.get('epoch_id')!='office-v2-explore-1':return out
    root=Path(root);deadline=time.monotonic()+.2;groups={k:[] for k in BOOKS}
    cut=out.get('now',out.get('as_of'))
    if isinstance(out.get('orders'),dict):groups['office-v2-explore-1']+= [(r,cut) for r in items(out['orders'].get('items')) if isinstance(r,dict)]
    for book in items(out.get('separate_books')):
        if isinstance(book,dict) and book.get('book_id') in groups and book.get('epoch_id')==book['book_id']:groups[book['book_id']]+=[(r,book.get('as_of',cut)) for r in items(book.get('orders')) if isinstance(r,dict)]
    for row in items(out.get('items'),10):
        if isinstance(row,dict) and row.get('book_id') in groups:groups[row['book_id']].append((row,cut))
    if not any(groups.values()):return out
    store=None;store_context=None
    try:
        try:store_context=_ro(root/'office.db');store=store_context.__enter__()
        except (OSError,sqlite3.Error):store=None
        for book,targets in groups.items():
            if not targets:continue
            for target,_ in targets:
                for key in FIELDS:target.setdefault(key,None)
                target.update(entry_mcap_reason='recorded entry supply unavailable',exit_mcap_reason='recorded entry supply unavailable',price_fields_reason='ledger unavailable')
            ids=list(dict.fromkeys(t.get('order_id') for t,_ in targets if isinstance(t.get('order_id'),str)))[:200]
            if not ids:continue
            try:
                if time.monotonic()>deadline:raise ValueError('display read budget')
                with _ro(root/'v2'/BOOKS[book]/'ledger.db') as conn:
                    rows=conn.execute('SELECT order_id,book_id,epoch_id,book_kind,network_id,mint,state,reason,requested_at,filled_at,closed_at,entry_unit_price_usd,exit_usd,tokens_atomic,decimals,decision_bundle_hash FROM orders WHERE book_id=? AND epoch_id=? AND order_id IN ('+','.join('?' for _ in ids)+') LIMIT 200',(book,book,*ids)).fetchall()
                by_id={r['order_id']:r for r in rows}
                for target,asof in targets:
                    if time.monotonic()>deadline:target['price_fields_reason']='display read budget';continue
                    row=by_id.get(target.get('order_id'));state=target.get('status',target.get('state'))
                    if target.get('book_id',book)!=book or not row or state in ('open','closed') and not number(row['filled_at']) or state=='closed' and not number(row['closed_at']) or row['book_kind']!='evaluated' or row['network_id']!='solana' or row['mint']!=target.get('mint') or state!=row['state'] or not number(asof) or not number(row['requested_at']) or any(number(row[k]) and row[k]>asof for k in ('requested_at','filled_at','closed_at')):
                        target['price_fields_reason']='order identity or time differs';continue
                    target.update(reason=row['reason'],entry_unit_price_usd=row['entry_unit_price_usd'] if number(row['entry_unit_price_usd']) else None,exit_usd=row['exit_usd'],tokens_atomic=row['tokens_atomic'],decimals=row['decimals'],price_fields_reason=None)
                    target['exit_unit_price_usd']=unit(row['exit_usd'],row['tokens_atomic'],row['decimals']) if state=='closed' else None
                    if state not in ('open','closed'):continue
                    try:recorded=supply(store,row) if store else None
                    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,AttributeError,InvalidOperation,OverflowError):recorded=None
                    if recorded is None:continue
                    tokens,raw_supply,supply_at=recorded
                    target.update(mcap_basis='recorded entry supply held constant; modelled',mcap_supply_atomic=raw_supply,mcap_supply_known_at=supply_at)
                    for side in ('entry','exit'):
                        price=target[side+'_unit_price_usd']
                        if number(price):
                            cap=float(Decimal(str(price))*tokens)
                            if number(cap) and (cap>0 or price==0):target[side+'_mcap_usd']=cap;target[side+'_mcap_reason']=None
                            else:target[side+'_mcap_reason']='market cap outside supported display range'
                        else:target[side+'_mcap_reason']=side+' price not recorded'
            except (OSError,sqlite3.Error,ValueError,TypeError,OverflowError):continue
    finally:
        if store is not None:store_context.__exit__(None,None,None)
    return out
