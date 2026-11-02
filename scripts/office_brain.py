#!/usr/bin/env python3
"""Office seat brain access, one call per process, so the service key never enters the seat host's environment. Prints JSON.

read: facts only from allowlisted sections. Every section a fact touches must be allowed, so a mixed fact stays out.
note: an episode with source_ref office:<actor>:<id>, which crew reads exclude. Facts are never written: the write gate needs evidence a seat cannot vouch for."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
# Measured on 8 queries: bot-research alone returned almost nothing, so the whole memecoin tree. Left out: crew (orders, tasks), research/studies (epoch results),
# infra, and live sizing. Kinds exclude decisions and tasks, which carry operator orders and crew work.
READ_SECTIONS=('memecoin','research/methods')
DENY_SECTIONS=('memecoin/sizing',)
READ_KINDS=('finding','trap','rule','observation')
# Backstop for a misfiled fact. Epoch results belong under research/studies or crew (Gamma, v2 prereg section 5).
HIDDEN=re.compile(r'office-(paper-eval|v2-(proof|paper|explore))-[0-9]+|look record|sealed look',re.I)
MAX_FACTS=10


def section_allowed(slug):
    def under(names):return any(slug==s or slug.startswith(s+'/') for s in names)
    return under(READ_SECTIONS) and not under(DENY_SECTIONS)


def _after(value,now):
    """True when a stamp is past the cut. A stamp that doesn't parse is treated as past: unknown stays out."""
    if value is None:return False
    try:return datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()>now
    except (ValueError,OSError,OverflowError):return True


def filter_facts(rows,section_of,*,now=None,epoch_entities=frozenset()):
    """Unknown section or no entity at all reads as not allowed. A fact linked to an epoch: entity carries Office results (Gamma's writer rule); a fact stamped after the cut doesn't exist yet."""
    now=time.time() if now is None else now
    out=[]
    for row in rows:
        ids=row.get('entity_ids') or []
        if set(ids)&epoch_entities or _after(row.get('recorded_at'),now) or _after(row.get('valid_from'),now):continue
        if HIDDEN.search(json.dumps([row.get('statement'),row.get('detail'),row.get('evidence')],default=str)):continue
        if ids and row.get('kind') in READ_KINDS and all(section_allowed(section_of.get(i,'')) for i in ids):
            out.append(dict(id=row.get('id'),kind=row.get('kind'),status=row.get('status'),statement=row.get('statement')))
    return out


def read(store,q):
    from bot.brain_graph import search
    rows=search.ask(store,q,limit=MAX_FACTS*3,embed=False)
    sections={s['id']:s['slug'] for s in store.sections()}
    ids=sorted({e for r in rows for e in r.get('entity_ids') or []})
    from bot.brain_graph.store import _pg_in
    section_of,epoch_entities={},set()
    for start in range(0,len(ids),100):
        for e in store.rows_page('brain_entities',select='id,section_id,name',filters=(('id','in',_pg_in(ids[start:start+100])),),limit=100):
            section_of[e['id']]=sections.get(e.get('section_id'),'')
            if str(e.get('name') or '').lower().startswith('epoch:'):epoch_entities.add(e['id'])
    return dict(state='known',facts=filter_facts(rows,section_of,epoch_entities=frozenset(epoch_entities))[:MAX_FACTS],sections=list(READ_SECTIONS))


def note(store,actor,text,secrets):
    from bot.brain_graph import api
    if not text.strip() or len(text)>4000:return dict(state='refused',reason='text_invalid')
    ref='office:%s:%s'%(actor,uuid.uuid4().hex)
    api.add_episode(store,'handoff_line',text,author='cron',source_ref=ref,secret_values=secrets)
    return dict(state='recorded',source_ref=ref)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--env-file',required=True);parser.add_argument('op',choices=('read','note'))
    parser.add_argument('--actor',required=True);parser.add_argument('text');args=parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(args.env_file)
    from bot.brain_graph.gate import secret_values_from_env
    from bot.brain_graph.store import BrainRefused,BrainUnavailable,RestStore
    try:
        store=RestStore()
        print(json.dumps(read(store,args.text) if args.op=='read' else note(store,args.actor,args.text,secret_values_from_env(args.env_file)),default=str))
    except (BrainRefused,BrainUnavailable) as exc:
        print(json.dumps(dict(state='unknown',reason=type(exc).__name__)))


if __name__=='__main__':main()
