from datetime import datetime,timedelta
from test_desk_watch_list import databases,ticket,coin,look,H,RESPONSE,AT,service,store,_end

def test_change_after_life_cannot_open_child(databases):
 t=ticket(databases,coin());look(databases,t,H,RESPONSE)
 late=(datetime.fromisoformat(AT)+timedelta(seconds=901)).isoformat()
 try:service.record_relook(databases[0],t,{'trigger':'new_high','change_score':4},now_iso=late)
 except ValueError:return
 assert service.relook_watches(databases[0],now_iso=late)==[]

def test_unseated_child_has_terminal_watch_row(databases):
 t=ticket(databases,coin());look(databases,t,H,RESPONSE)
 now=(datetime.fromisoformat(RESPONSE)+timedelta(seconds=61)).isoformat()
 [child]=service.relook_watches(databases[0],now_iso=now)
 late=(datetime.fromisoformat(AT)+timedelta(seconds=901)).isoformat()
 service.relook_watches(databases[0],now_iso=late)
 assert store.get_ticket(databases[0],child)['state']=='expired'
 assert _end(databases,child) is not None

def test_per_look_hash_binds_actual_sheet(databases):
 import hashlib,json
 t=ticket(databases,coin());look(databases,t,H,RESPONSE)
 v=next(s for s in store.get_ticket(databases[0],t)['stages'] if s['stage']=='vet')
 assert v['inputs']['watch']['sheet_sha'] != store.get_ticket(databases[0],t)['snapshot_hash']


def _at(s):
    return (datetime.fromisoformat(AT)+timedelta(seconds=s)).isoformat()


def test_a_look_answered_after_root_life_never_enters(databases):
    """Zeta f0ef698b0 P1: child opened at 890, JUDGE answers ENTER at 905 (life 900): SKIP watch_life, no entry."""
    from test_memecoin_desk_contract import verdict
    t=ticket(databases,coin());look(databases,t,H,RESPONSE)
    service.record_relook(databases[0],t,{'trigger':'new_high','change_score':4},now_iso=_at(889))
    [child]=service.relook_watches(databases[0],now_iso=_at(890))
    out=look(databases,child,verdict(),_at(905))
    assert out['action']=='SKIP' and out['reason']=='watch_life', out


def test_a_relook_that_never_resumes_still_ends_the_watch(databases):
    """Zeta P2: relook at 899, resume at 901 refuses the child: the root's own look is the watch's last one."""
    t=ticket(databases,coin());look(databases,t,H,RESPONSE)
    service.record_relook(databases[0],t,{'trigger':'new_high','change_score':4},now_iso=_at(899))
    assert service.relook_watches(databases[0],now_iso=_at(901))==[]
    end=_end(databases,t)
    assert end is not None and end['end_reason']=='watch_life_unresumed', end


def test_continued_parents_never_starve_the_end_row_limit(databases):
    """Zeta P1: root WATCH -> cadence child -> child DROP: record_watch_ends(limit=1) ends the child."""
    from test_memecoin_desk_contract import verdict
    t=ticket(databases,coin());look(databases,t,H,RESPONSE)
    now=(datetime.fromisoformat(RESPONSE)+timedelta(seconds=61)).isoformat()
    [child]=service.relook_watches(databases[0],now_iso=now)
    look(databases,child,verdict(v='S',r=None),now)
    service.record_watch_ends(databases[0],now_iso=now,limit=1)
    assert _end(databases,child) is not None


def test_cadence_relook_carries_prev_look_high(databases):
    """Zeta P2: the cadence path copies the parent's look_high like a change trigger does."""
    from test_desk_watch_list import _watching, vet
    t,_=_watching(databases)
    now=(datetime.fromisoformat(RESPONSE)+timedelta(seconds=61)).isoformat()
    [child]=service.relook_watches(databases[0],now_iso=now)
    look(databases,child,H,now)
    assert vet(databases,child)['inputs']['watch']['prev_look_high']==2.2e-4
