"""Version-bound normalized telemetry; no transcript discovery or model calls."""
import fcntl
import hashlib
import os
from pathlib import Path

from .crew_session_bridge import BridgeError, private, private_dir, read_json, write_json
from .handover import canonical, number

IDENTITY=('host_id','session_id','incarnation','key','model','client_version')


def observe(sample, *, expected, now, warn_pct=60, handover_pct=75, stale_s=120):
    unknown=dict(state='unknown',fill_pct=None,trigger=False)
    if not all(number(x) for x in (warn_pct,handover_pct,stale_s)) or not 0<warn_pct<handover_pct<100 or stale_s==0:return unknown
    if not isinstance(sample,dict) or not isinstance(expected,dict):return unknown
    if not number(now) or any(not isinstance(expected.get(k),str) or not expected[k] or sample.get(k)!=expected[k] for k in IDENTITY):return unknown
    if (not number(sample.get('observed_at')) or not 0<=now-sample['observed_at']<=stale_s
            or sample.get('semantics_verified') is not True
            or any(not isinstance(sample.get(k),str) or not sample[k] for k in ('window_id','event_id','provenance'))):return unknown
    if sample.get('compaction') is True:return dict(state='compaction',fill_pct=None,trigger=True)
    window=sample.get('window_tokens');used=sample.get('input_tokens')
    if not number(window) or window==0 or not number(used):return unknown
    if sample.get('backend') in ('claude','office_nonoverlapping'):
        counts=[sample.get(k) for k in ('cache_read_tokens','cache_create_tokens')]
        if not all(number(x) for x in counts):return unknown
        used+=sum(counts)
    elif sample.get('backend')!='codex':return unknown
    if not 0<warn_pct<handover_pct<100 or not number(used) or used>window:return unknown
    pct=100*used/window
    state='handover' if pct>=handover_pct else 'warn' if pct>=warn_pct else 'normal'
    return dict(state=state,fill_pct=pct,trigger=state=='handover')


def _validate_latch(record):
    for key in ('latched','unknown_recovered'):
        values=record.get(key,[] if key=='unknown_recovered' else None)
        if (not isinstance(values,list) or len(values)>4096
                or any(not isinstance(v,str) or len(v)!=64 or any(c not in '0123456789abcdef' for c in v) for v in values)
                or len(set(values))!=len(values)):
            raise ValueError('latch_invalid')


class TriggerLatch:
    """Durable one-trigger-per-session/window latch; cap refuses rather than forgets."""
    def __init__(self,path):
        self.path=Path(path);private_dir(self.path.parent)

    def observe(self,sample,*,expected,now,**policy):
        result=observe(sample,expected=expected,now=now,**policy)
        if result['state']=='unknown':return result
        fd=os.open(self.path.with_suffix('.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            if self.path.exists():
                if private(self.path).st_size>1024**2:return dict(state='unknown',fill_pct=None,trigger=False)
                record=read_json(self.path)
            else:record={'latched':[]}
            _validate_latch(record)
            key=hashlib.sha256(canonical({k:sample[k] for k in (*IDENTITY,'window_id')})).hexdigest()
            if key in record['latched']:return dict(result,trigger=False,latched=True)
            if result['trigger']:
                if len(record['latched'])>=4096:return dict(state='unknown',fill_pct=None,trigger=False)
                record['latched'].append(key);write_json(self.path,record)
            return result
        except (BridgeError,OSError,ValueError,TypeError,KeyError):return dict(state='unknown',fill_pct=None,trigger=False)
        finally:os.close(fd)

    def unknown_usage(self,*,expected,budget_known,initializing):
        """Trusted host calls for its running binding, never for a wrong-thread sample.

        One recovery per host/role, across replacement sessions and SDK versions. No
        automatic reset: a host policy intervention is needed to permit another.
        The allowance is spent before launch; a crash can consume it without a
        swap. That deliberately parks instead of risking a duplicate recovery.
        """
        park=dict(state='unknown',action='park',trigger=False,needs_page=True)
        if (not isinstance(expected,dict) or any(not isinstance(expected.get(k),str) or not expected[k] for k in IDENTITY)
                or budget_known is not True or type(initializing) is not bool):return park
        if initializing:return dict(state='initializing',action='initializing',trigger=False,needs_page=False)
        fd=os.open(self.path.with_suffix('.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            if self.path.exists():
                if private(self.path).st_size>1024**2:return park
                record=read_json(self.path)
            else:record={'latched':[]}
            _validate_latch(record)
            recovered=record.setdefault('unknown_recovered',[])
            key=hashlib.sha256(canonical({k:expected[k] for k in ('host_id','key')})).hexdigest()
            if key in recovered or len(recovered)>=4096:return park
            recovered.append(key);write_json(self.path,record)
            return dict(state='unknown',action='checkpoint_and_swap',trigger=True,needs_page=True)
        except (BridgeError,OSError,ValueError,TypeError,KeyError):return park
        finally:os.close(fd)
