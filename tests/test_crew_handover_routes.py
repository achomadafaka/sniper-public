"""Host-only staged registration, exact generation CAS and replay-safe rollback."""
import hashlib
import uuid

import pytest
from test_crew_session_bridge import cb, local  # noqa: F401


def stage(bridge, **changes):
    old=bridge.register('Delta',None,str(uuid.uuid4()))
    candidate={'thread':str(uuid.uuid4()),'pipe':None,'from_mode':old['from_mode']}
    args=dict(role='Delta',expected=old,candidate=candidate,handover_id=str(uuid.uuid4()),
              note_hash='a'*64,permissions_hash='b'*64,expires_at=120.,now=1.,owner_token=str(uuid.uuid4()))
    args.update(changes)
    claim=bridge.stage_handover(**args)
    return args,claim


def ready(bridge,args,claim):
    return bridge.handover_ready(args['handover_id'],claim['capability'],thread=args['candidate']['thread'],
        note_hash=args['note_hash'],permissions_hash=args['permissions_hash'],watermark=0,now=2.,owner_token=args['owner_token'])


def test_candidate_is_staged_not_registered_and_two_acks_are_distinct(local):
    _,bridge=local;args,claim=stage(bridge)
    assert bridge.context('Delta')==args['expected']
    with pytest.raises(cb.BridgeError,match='staged_candidate'):
        bridge.register('Delta',None,args['candidate']['thread'],replace_thread=args['expected']['thread'])
    ready(bridge,args,claim)
    route=bridge.activate_handover(args['handover_id'],claim['capability'],owner_token=args['owner_token'],now=3.)
    assert route['thread']==args['candidate']['thread'] and route['generation']==2
    assert bridge.activate_handover(args['handover_id'],claim['capability'],owner_token=args['owner_token'],now=4.)==route
    assert bridge.handover_status(args['handover_id'])['activation_ack'] is False
    bridge.ack_handover(args['handover_id'],claim['capability'],thread=route['thread'],watermark=0,now=4.,owner_token=args['owner_token'])
    assert bridge.handover_status(args['handover_id'])['activation_ack'] is True


@pytest.mark.parametrize('mutate',['aba','pipe','mode'])
def test_stale_capability_cannot_overwrite_manual_binding_or_rollback(local,mutate):
    _,bridge=local;args,claim=stage(bridge);ready(bridge,args,claim)
    old=args['expected']
    if mutate=='aba':
        other=bridge.register('Delta',None,str(uuid.uuid4()),replace_thread=old['thread'])
        bridge.register('Delta',None,old['thread'],replace_thread=other['thread'])
    else:
        routes=bridge.routes();routes['Delta']=dict(old,**({'pipe':'/tmp/changed.sock'} if mutate=='pipe' else {'from_mode':'bypass'}))
        cb.write_json(bridge.state_dir/'routes.json',{'routes':routes})
    current=bridge.context('Delta')
    with pytest.raises(cb.BridgeError,match='registration_changed'):
        bridge.activate_handover(args['handover_id'],claim['capability'],owner_token=args['owner_token'],now=3.)
    assert bridge.context('Delta')==current
    with pytest.raises(cb.BridgeError):
        bridge.rollback_handover(args['handover_id'],claim['capability'],owner_token=args['owner_token'],now=4.)
    assert bridge.context('Delta')==current


def test_rollback_bumps_generation_and_never_steals_a_newer_route(local):
    _,bridge=local;args,claim=stage(bridge);ready(bridge,args,claim)
    route=bridge.activate_handover(args['handover_id'],claim['capability'],owner_token=args['owner_token'],now=3.)
    restored=bridge.rollback_handover(args['handover_id'],claim['capability'],owner_token=args['owner_token'],now=4.)
    assert restored['thread']==args['expected']['thread'] and restored['generation']==route['generation']+1
    assert bridge.rollback_handover(args['handover_id'],claim['capability'],owner_token=args['owner_token'],now=5.)==restored
    with pytest.raises(cb.BridgeError):bridge.activate_handover(args['handover_id'],claim['capability'],owner_token=args['owner_token'],now=6.)


@pytest.mark.parametrize('bad', ['token','thread','note','permissions','expiry'])
def test_readiness_requires_exact_capability_identity_and_freshness(local,bad):
    _,bridge=local;args,claim=stage(bridge)
    params=dict(thread=args['candidate']['thread'],note_hash=args['note_hash'],permissions_hash=args['permissions_hash'],watermark=0,now=2.,owner_token=args['owner_token'])
    token=claim['capability']
    if bad=='token':token='f'*64
    elif bad=='thread':params['thread']=str(uuid.uuid4())
    elif bad=='note':params['note_hash']='c'*64
    elif bad=='permissions':params['permissions_hash']='c'*64
    else:params['now']=121.
    with pytest.raises(cb.BridgeError):bridge.handover_ready(args['handover_id'],token,**params)
    assert bridge.context('Delta')==args['expected']


def test_old_coordinator_cannot_activate_or_rollback_after_refence(local):
    _,bridge=local;args,claim=stage(bridge);ready(bridge,args,claim)
    newer=str(uuid.uuid4())
    bridge.refence_handover(args['handover_id'],claim['capability'],expected_owner=args['owner_token'],new_owner=newer,now=3.)
    with pytest.raises(cb.BridgeError,match='owner_changed'):
        bridge.activate_handover(args['handover_id'],claim['capability'],owner_token=args['owner_token'],now=4.)
    current=bridge.activate_handover(args['handover_id'],claim['capability'],owner_token=newer,now=4.)
    with pytest.raises(cb.BridgeError,match='owner_changed'):
        bridge.rollback_handover(args['handover_id'],claim['capability'],owner_token=args['owner_token'],now=5.)
    assert bridge.context('Delta')==current


def test_crash_after_rollback_route_write_reconciles_without_another_generation(local,monkeypatch):
    _,bridge=local;args,claim=stage(bridge);ready(bridge,args,claim)
    bridge.activate_handover(args['handover_id'],claim['capability'],owner_token=args['owner_token'],now=3.)
    real=cb.write_json;owner=__import__('threading').get_ident()
    def fail(path,value):
        if (__import__('threading').get_ident()==owner and path.name==args['handover_id']+'.json'
                and value.get('state')=='rolled_back'):
            raise OSError('fixture disk failure')
        return real(path,value)
    monkeypatch.setattr(cb,'write_json',fail)
    with pytest.raises(OSError):bridge.rollback_handover(args['handover_id'],claim['capability'],owner_token=args['owner_token'],now=4.)
    monkeypatch.setattr(cb,'write_json',real)
    restored=bridge.context('Delta')
    assert bridge.rollback_handover(args['handover_id'],claim['capability'],owner_token=args['owner_token'],now=5.)==restored


def test_held_intake_preserves_ids_and_replays_old_consumption_tristate(local,monkeypatch):
    _,bridge=local
    bridge.store('before','Alpha','Delta','old instruction')
    args,claim=stage(bridge)
    monkeypatch.setattr(bridge,'codex',lambda *a:pytest.fail('held recipient was woken'))
    row=bridge.send('Alpha','Delta','new instruction',message_id='during')
    assert row['delivery_status']=='handover_held'
    page=bridge.handover_mail(args['handover_id'],claim['capability'],after=0,limit=10,now=2.,owner_token=args['owner_token'])
    assert page['complete'] is True
    assert [(r['id'],r['delivered_to_old'],r['reconcile_only']) for r in page['messages']]==[('before','unknown',True),('during',False,False)]
    bridge.handover_consumed(args['handover_id'],claim['capability'],message_id='before',thread=args['expected']['thread'],effect_ref='action-1',now=2.,owner_token=args['owner_token'])
    page=bridge.handover_mail(args['handover_id'],claim['capability'],after=0,limit=1,now=2.,owner_token=args['owner_token'])
    assert page['complete'] is False and page['messages'][0]['delivered_to_old'] is True
    assert page['messages'][0]['effect_ref']=='action-1' and page['messages'][0]['reconcile_only'] is True


def test_wrong_old_identity_cannot_claim_consumption(local):
    _,bridge=local;bridge.store('m','Alpha','Delta','instruction');args,claim=stage(bridge)
    with pytest.raises(cb.BridgeError):
        bridge.handover_consumed(args['handover_id'],claim['capability'],message_id='m',thread=args['candidate']['thread'],effect_ref=None,now=2.,owner_token=args['owner_token'])


def test_terminal_handover_requires_exact_route_and_postactivation_ack(local):
    _,bridge=local;args,claim=stage(bridge)
    with pytest.raises(cb.BridgeError):bridge.finish_handover(args['handover_id'],claim['capability'],outcome='retired',now=2.,owner_token=args['owner_token'])
    ready(bridge,args,claim);route=bridge.activate_handover(args['handover_id'],claim['capability'],now=3.,owner_token=args['owner_token'])
    with pytest.raises(cb.BridgeError):bridge.finish_handover(args['handover_id'],claim['capability'],outcome='retired',now=4.,owner_token=args['owner_token'])
    bridge.ack_handover(args['handover_id'],claim['capability'],thread=route['thread'],watermark=0,now=4.,owner_token=args['owner_token'])
    assert bridge.finish_handover(args['handover_id'],claim['capability'],outcome='retired',now=5.,owner_token=args['owner_token'])['state']=='retired'


@pytest.mark.parametrize('boundary',['intent','route'])
def test_activation_crash_recovers_exact_route_once(local,monkeypatch,boundary):
    _,bridge=local;args,claim=stage(bridge);ready(bridge,args,claim)
    actual=cb.write_json;crashed=[]
    def crash(path,value):
        result=actual(path,value)
        matches=(path.name==args['handover_id']+'.json' and value.get('state')=='activating') if boundary=='intent' else path.name=='routes.json'
        if matches and not crashed:
            crashed.append(True);raise KeyboardInterrupt('simulated bridge crash')
        return result
    monkeypatch.setattr(cb,'write_json',crash)
    with pytest.raises(KeyboardInterrupt):
        bridge.activate_handover(args['handover_id'],claim['capability'],now=3.,owner_token=args['owner_token'])
    monkeypatch.setattr(cb,'write_json',actual)
    route=bridge.activate_handover(args['handover_id'],claim['capability'],now=4.,owner_token=args['owner_token'])
    assert route['thread']==args['candidate']['thread'] and route['generation']==2
    assert bridge.handover_status(args['handover_id'])['state']=='active'
