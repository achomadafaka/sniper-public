"""Bounded, read-only CPMM research windows on the HOST receipt clock."""
import json
import sqlite3
import time
from pathlib import Path

from sniper.services.memecoin_desk_cpmm import VERSION
from sniper.services.memecoin_desk_cpmm_capture import finite

MAX_ROWS = 10000


def read_window(desk_dir,mint,lo,hi,*,pool=None,now=None):
    now=time.time() if now is None else now
    out={'state':'unknown','reason':None,'rows':[],'gaps':[],'pool':pool,'coverage':None,
         'clock':'host_received_at','version':VERSION,'price_basis':'event_effective_pre'}
    def unknown(reason):out['reason']=reason;return out
    if not all(finite(x) for x in (lo,hi,now)) or not 0<=lo<=hi<=now:return unknown('clock_invalid')
    path=Path(desk_dir)/'cpmm/tape.db'
    if not path.exists():return unknown('tape_missing')
    try:
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=0) as conn:
            version,start,through,slot,complete=conn.execute('SELECT version,start_at,through,slot,complete FROM head WHERE id=1').fetchone()
            if version!=VERSION:return unknown('schema_version')
            bindings=conn.execute('SELECT pool FROM bindings WHERE mint=? AND known_at<=? LIMIT 3',(mint,hi)).fetchall()
            pools={r[0] for r in bindings}
            if pool is None:
                if len(pools)>1:return unknown('pool_ambiguous')
                if not pools:return unknown('pool_unknown')
                pool=next(iter(pools));out['pool']=pool
            elif pool not in pools:return unknown('pool_unknown')
            if not finite(start) or lo<start:return unknown('capture_cold')
            if not finite(through) or through<hi:return unknown('capture_incomplete')
            if through>now or now-through>5:return unknown('capture_stale')
            out['coverage']={'from':start,'through':through,'slot':slot}
            gaps=conn.execute('SELECT cause,start_at,end_at,pool FROM gaps WHERE start_at<=? AND end_at>=? AND (pool IS NULL OR pool=?) LIMIT ?',
                              (hi,lo,pool,MAX_ROWS+1)).fetchall()
            rows=conn.execute('SELECT payload FROM swaps WHERE mint=? AND pool=? AND received_at>=? AND received_at<=? ORDER BY received_at,slot,json_extract(payload,"$.tx_index"),json_extract(payload,"$.outer_ix"),coalesce(json_extract(payload,"$.inner_ix"),-1),json_extract(payload,"$.ordinal"),event_id LIMIT ?',
                              (mint,pool,lo,hi,MAX_ROWS+1)).fetchall()
            if len(rows)>MAX_ROWS or len(gaps)>MAX_ROWS:return unknown('row_cap')
            out['rows']=[json.loads(r[0]) for r in rows]
            out['gaps']=[dict(zip(('cause','start_at','end_at','pool'),r)) for r in gaps]
            if gaps:return unknown('capture_gap')
            if complete!=1:return unknown('capture_fence_unproven')
            if any(any(r.get(k)!='proven' for k in ('binding_status','actor_status','order_status')) for r in out['rows']):
                return unknown('partial_facts')
            out.update(state='ok',reason=None);return out
    except (sqlite3.Error,OSError,ValueError,TypeError,KeyError):return unknown('tape_unreadable')
    finally:
        if 'conn' in locals():conn.close()
