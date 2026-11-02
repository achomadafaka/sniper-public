"""Atomic off-path cache publication and immutable in-memory admission lookup.

Only the background reader touches disk. lookup never opens, stats, locks, waits,
starts a thread or reads an environment variable. A missing/dead worker is unknown.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from pathlib import Path

from sniper.services.memecoin_desk_dump_group import POLICY_HASH, VERSION, checked_fact

MAX_BYTES = 16 * 1024 * 1024
RETIRED_AGE_OUT_S = 86400.0 + 300.0       # a retired venue stops mattering once the 24 h flag memory (plus margin) holds none of its episodes


def publish_cache(path, scores, *, coverage, cutoff, published_at, ttl_s=3):
    """Single worker only. Score keys are pool addresses; each score binds mint/pool.
    Partial history permits POSITIVE membership blocks, never a clean certificate.
    coverage: {curve:{complete:bool,...},ps:{complete:bool,...}}; old string partial
    values are tolerated as incomplete for offline fixtures, never clean.
    """
    if not 0 < ttl_s <= 5 or not 0 < cutoff <= published_at:
        raise ValueError('dump_group_cache_clock')
    if isinstance(coverage.get('ps'),dict) and 'loss_fraction_24h' in coverage['ps']:
        x=coverage['ps']['loss_fraction_24h']                   # a non-finite measurement must not make the whole snapshot unserialisable: it is published as unknown
        if type(x) not in (int,float) or not math.isfinite(x):
            coverage=dict(coverage,ps=dict(coverage['ps'],loss_fraction_24h=None))
    data=dict(version=VERSION,policy_hash=POLICY_HASH,published_at=published_at,cutoff=cutoff,
              expires_at=published_at+ttl_s,coverage=coverage,scores={})
    # The full intervals live once in the snapshot header, not once per pool.
    summary={v:dict(complete=isinstance(c,dict) and c.get('complete') is True,sound=isinstance(c,dict) and c.get('sound') is True,
                    history_start=c.get('history_start') if isinstance(c,dict) else None,
                    holes=len(c.get('holes',())) if isinstance(c,dict) and isinstance(c.get('holes',()),(list,tuple)) else None)
             for v,c in coverage.items() if v in ('curve','ps','pumpswap')}
    def venue_ok(v):
        c=coverage.get(v)
        if not isinstance(c,dict):
            return False
        if c.get('complete') is True:
            return True
        since=c.get('retired_since')       # retired AND aged out: the remaining venues' history alone is the whole flag memory
        return (v=='curve' and c.get('retired') is True and isinstance(since,(int,float)) and not isinstance(since,bool)
                and since==since and since not in (float('inf'),float('-inf')) and cutoff-since>=RETIRED_AGE_OUT_S)
    complete=all(venue_ok(v) for v in ('curve','ps'))
    curve_ok=venue_ok('curve')
    ps_cov=coverage.get('ps') if isinstance(coverage.get('ps'),dict) else {}
    loss=ps_cov.get('loss_fraction_24h')
    loss_ok=type(loss) in (int,float) and math.isfinite(loss) and 0<=loss<=1
    curve_state='complete' if isinstance(coverage.get('curve'),dict) and coverage['curve'].get('complete') is True else 'aged_out'
    def ps_partial(score):
        """Amendment 31: a PumpSwap coin passes with a MEASURED partial PS venue when the venue is sound (everything `complete` needs except zero holes), its loss fraction is measured, the curve
        venue is complete or aged out, and no hole of unknown scope or proven for the coin's own pool overlaps [created - 60 s, asof]. -> (ok, detail)."""
        w=score.get('ps_window')
        if score.get('venue')!='ps' or not curve_ok:
            return False,'partial_history'
        if ps_cov.get('sound') is not True or not loss_ok:
            return False,'partial_history'
        if not isinstance(w,dict) or w.get('created_known') is not True or type(w.get('holes_blocking')) is not int or type(w.get('holes_any')) is not int:
            return False,'partial_history'
        return (w['holes_blocking']==0),('ps_window_hole' if w['holes_blocking'] else 'partial_history')
    for pool,score in scores.items():
        item=dict(score,version=VERSION,policy_hash=POLICY_HASH,known_at=published_at,
                  cutoff=cutoff,expires_at=published_at+ttl_s,coverage=summary)
        if item.get('pool')!=pool:
            raise ValueError('dump_group_cache_identity')
        if item.get('status')=='kill' and any(item.get(k) is not True for k in
                                               ('positive_proven','target_complete','exclusions_valid')):
            item.update(status='unknown',reason='dump_group_unknown',detail='positive_unproven',would_block=True)
        if item.get('status')=='pass':
            w=item.pop('ps_window',None)
            if complete and item.get('venue')=='ps' and ps_cov.get('complete') is True:
                item.update(dump_group_coverage='complete',curve_venue=curve_state)
            elif complete and item.get('venue')!='ps':
                pass                                              # a curve coin with both venues complete: unchanged
            else:
                ok,detail=ps_partial(dict(item,ps_window=w))
                if ok:
                    item.update(dump_group_coverage='ps_partial_measured',ps_loss_fraction_24h=float(loss),ps_window_holes=w['holes_any'],
                                ps_window_blocking_holes=w['holes_blocking'],curve_venue=curve_state)
                else:
                    item.update(status='unknown',reason='dump_group_unknown',detail=detail)
        else:
            item.pop('ps_window',None)
        data['scores'][pool]=item
    raw=json.dumps(data,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    if len(raw)>MAX_BYTES:
        raise ValueError('dump_group_cache_oversize')
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.cache-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(raw)
        os.replace(name,path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class SnapshotCache:
    def __init__(self,path,*,enabled=True):
        self.path=Path(path)
        self.enabled=enabled
        self._snapshot=None
        self._error='cache_missing'
        self._stop=threading.Event()
        self._thread=None

    def reload(self):
        """Background only. Parse completely before one atomic reference publication."""
        try:
            with self.path.open('rb') as f:
                raw=f.read(MAX_BYTES+1)
            if len(raw)>MAX_BYTES:
                raise ValueError('oversize')
            obj=json.loads(raw)
            if (not isinstance(obj,dict) or obj.get('version')!=VERSION or obj.get('policy_hash')!=POLICY_HASH
                    or not isinstance(obj.get('scores'),dict)):
                raise ValueError('shape')
            self._snapshot=obj
            self._error=None
        except (OSError,ValueError,TypeError,RecursionError) as exc:
            self._snapshot=None
            self._error='cache_unreadable:'+type(exc).__name__

    def start(self):
        if self._thread is not None or not self.enabled:
            return self
        def run():
            while not self._stop.is_set():
                self.reload()
                self._stop.wait(.5)
        self._thread=threading.Thread(target=run,name='desk-dump-cache',daemon=True)
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(1)

    def lookup(self,*,mint,pool,now):
        def unknown(detail):
            return dict(status='unknown',reason='disabled' if not self.enabled else 'dump_group_unknown',detail=detail,
                        version=VERSION,policy_hash=POLICY_HASH,mint=mint,pool=pool,
                        known_at=now,cutoff=now,expires_at=now+3)
        if not self.enabled:
            return unknown('disabled')
        snapshot=self._snapshot
        if snapshot is None:
            return unknown(self._error or 'cache_missing')
        fact=snapshot['scores'].get(pool)
        if not isinstance(fact,dict):
            return unknown('coin_missing')
        checked=checked_fact(fact,mint=mint,pool=pool,now=now)
        if checked.get('reason') in ('cache_identity','cache_timestamp','cache_stale','cache_shape'):
            return unknown(checked['reason'])
        return checked
