"""Draft preparation has no authority, no persistence and no economic calculation."""
import importlib
import hashlib
import json
import pytest
from .test_eval_report import world,filled_cohort,seed_safety,T,report
from sniper.office import eval_report as R,store as S


def test_prepare_then_look_binds_exact_canonical_record_and_keeps_inputs_readonly(world):
    P=importlib.import_module('scripts.office_eval_seal_prepare')
    filled_cohort(world);now=T+14*86400+100;seed_safety(world,now);world.o.commit()
    before=['\n'.join(c.iterdump()) for c in (world.o,world.l,world.d)]
    out=P.prepare(world.office,world.ledger,desk_path=world.desk,now=now,limits=R.DEFAULT_LIMITS)
    raw=out['record_bytes'];record=json.loads(raw)
    assert out['state']=='draft_unapproved' and raw==S.canonical_json(record)
    assert out['sha256']==hashlib.sha256(raw).hexdigest()
    result=report(world,now=now,mode='look',look_record=raw,look_sha256=out['sha256'])
    assert result['economics']['state']=='known'
    assert result['look_record_sha256']==out['sha256']
    assert out['record_bytes']==P.prepare(world.office,world.ledger,desk_path=world.desk,now=now,limits=R.DEFAULT_LIMITS)['record_bytes']
    assert before==['\n'.join(c.iterdump()) for c in (world.o,world.l,world.d)]


def test_prepared_fx_and_limits_must_match_replay(world):
    P=importlib.import_module('scripts.office_eval_seal_prepare')
    filled_cohort(world);now=T+14*86400+100;seed_safety(world,now);world.o.commit()
    draft=P.prepare(world.office,world.ledger,desk_path=world.desk,now=now,limits=R.DEFAULT_LIMITS,sol_usd=150.,sol_at=now)
    for kw in ({'sol_usd':151.,'sol_at':now},{'sol_usd':150.,'sol_at':now,'limits':{'max_queries':511}}):
        out=report(world,now=now,mode='look',look_record=draft['record_bytes'],look_sha256=draft['sha256'],**kw)
        assert out['economics']['state']=='withheld'
