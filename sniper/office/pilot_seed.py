"""Host-only pilot manifest resolver. No provider calls or ledger writes."""
import hashlib
import json
import os
import stat
from pathlib import Path

from . import org as O, roster as R, store as S

FILES=('pilot_org.json','limits_pilot.json','pilot_models.json','book_policy_pilot.json')
# eval-1 keeps its 5-seat model manifest in its own file so the approved discovery pins never move
EVAL1_FILES=('pilot_org.json','limits_pilot.json','pilot_models_eval1.json','book_policy_pilot.json')
SLOTS=('head','scout','judge_claude','judge_gpt')
EVAL1_SLOTS=SLOTS+('postmortem',)


def _sha(value):
    return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)


def resolve_manifest(*, config_root, launcher_config, launcher_check, by='crew'):
    """Pins and actual prompt bytes come from the authenticated launcher, never a request."""
    if by!='crew' or not callable(launcher_config) or not callable(launcher_check) or launcher_check(by) is not True:
        raise R.OrgRefused('pilot_seed_untrusted_launcher')
    config=launcher_config(by)
    frozen=S.canonical_json(config)
    if len(frozen)>65536:raise ValueError('pilot_seed_launcher_size')
    config=json.loads(frozen)
    if (set(config)!={'schema','files','prompts'} or config['schema']!='office-pilot-seed-launcher-v1'
            or set(config['files'])!=set(FILES) or set(config['prompts'])!=set(SLOTS)
            or not all(_sha(v) for v in config['files'].values())):
        raise ValueError('pilot_seed_launcher_schema')
    docs={}
    for name in FILES:
        fd=os.open(Path(config_root)/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>65536:
                raise ValueError('pilot_seed_file_not_bounded_regular:'+name)
            raw=stream.read(65537)
        if len(raw)>65536 or hashlib.sha256(raw).hexdigest()!=config['files'][name]:
            raise ValueError('pilot_seed_file_pin_mismatch:'+name)
        docs[name]=json.loads(raw)
    org,limits,models,book=(docs[name] for name in FILES)
    seats=O.expand(org,limits)
    if (org.get('pilot') is not True or limits.get('pilot') is not True or limits.get('evaluated_epoch_allowed') is not False
            or limits.get('org_size_ceiling')!=4 or len(seats)!=4
            or O.validate(seats,limits,deterministic=org.get('deterministic_seats',()))
            or set(org.get('deterministic_seats',()))!={'Risk','Trader','Postmortem'}):
        raise ValueError('pilot_seed_org_invalid')
    bootstrap=book['bootstrap']
    if (book.get('evaluation') is not False or bootstrap.get('epoch_id')!='office-pilot-discovery-1'
            or bootstrap.get('book_id')!='pilot-floor' or type(bootstrap.get('capital_usd')) not in (int,float)
            or bootstrap['capital_usd']!=1000):
        raise ValueError('pilot_seed_book_contract_mismatch')
    resolved=[]
    for seat in seats:
        role,family=seat['role'],seat['family']
        slot={'Head':'head','Scout':'scout'}.get(role,'judge_'+family)
        if slot not in SLOTS or (role,family) not in {('Head','claude'),('Scout','claude'),('Judge','claude'),('Judge','gpt')}:
            raise ValueError('pilot_seed_seat_invalid')
        model=models['seats'][slot]
        if model.get('provider')!=('codex' if family=='gpt' else 'claude') or not isinstance(model.get('model'),str) or not model['model']:
            raise ValueError('pilot_seed_model_invalid')
        prompt=config['prompts'][slot]
        if (set(prompt)!={'text','sha256'} or not isinstance(prompt['text'],str) or not prompt['text']
                or not _sha(prompt['sha256']) or hashlib.sha256(prompt['text'].encode()).hexdigest()!=prompt['sha256']):
            raise ValueError('pilot_seed_prompt_pin_mismatch')
        resolved.append(dict(slot=slot,role=role,pod=seat['pod'],family=family,provider=model['provider'],model=model['model'],
            prompt_hash=prompt['sha256'],policy_hash=config['files']['book_policy_pilot.json'],lane=seat.get('lane'),lens=seat.get('lens'),
            persona=dict(name=family+' '+role,personality='Careful and direct.',backstory='Assigned to the discovery-only pilot.')))
    if {p['slot'] for p in resolved}!=set(SLOTS):raise ValueError('pilot_seed_seat_set_invalid')
    manifest=dict(schema='office-pilot-seed-v1',pilot=True,evaluation_allowed=False,kind='discovery',
        epoch_id=bootstrap['epoch_id'],book_id=bootstrap['book_id'],capital_usd=bootstrap['capital_usd'],
        files=config['files'],seats=sorted(resolved,key=lambda p:p['slot']))
    return manifest,limits


def seed_pilot_org(conn,*,config_root,launcher_config,launcher_check,clock,by='crew'):
    manifest,limits=resolve_manifest(config_root=config_root,launcher_config=launcher_config,launcher_check=launcher_check,by=by)
    return R.seed_pilot_org(conn,limits,clock=clock,manifest=manifest,by=by,launcher_check=launcher_check)


def approved_profile(config_root,config):
    """Only these two crew-approved prompt sets are selectable; no arbitrary path input."""
    from .host_auth import approved_path
    from .launcher_client import read_regular
    for profile,epoch_id,slots in (('pilot','office-pilot-discovery-1',SLOTS),('discovery2','office-pilot-discovery-2',SLOTS),
                                   ('office-paper-eval-1','office-paper-eval-1',EVAL1_SLOTS)):
        root=Path(config_root)/'prompts'/profile
        try:approval=json.loads(read_regular(approved_path(str(root/'sha256.json'))))
        except FileNotFoundError:continue
        if (approval.get('approval_status')!='approved_alpha' or approval.get('source_pins')!=config['files']
                or set(approval.get('files',{}))!={slot+'.md' for slot in slots} or set(config['prompts'])!=set(slots)):continue
        match=True
        for slot in slots:
            raw=read_regular(approved_path(str(root/(slot+'.md'))))
            prompt=config['prompts'][slot]
            if (hashlib.sha256(raw).hexdigest()!=approval['files'][slot+'.md']
                    or prompt!={'text':raw.decode(),'sha256':approval['files'][slot+'.md']}):match=False
        if match:return epoch_id
    raise ValueError('pilot_prompt_profile_unapproved')
