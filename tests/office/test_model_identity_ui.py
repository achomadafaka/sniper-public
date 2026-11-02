import json
from pathlib import Path
import re
import subprocess
import pytest
from sniper.office import store
from sniper.office.report import read_state


@pytest.mark.parametrize('basis,expected',[('requested_pinned','requested_pinned'),('echoed','echoed'),(None,'unknown'),('invented','unknown')])
def test_basis_is_typed_and_carried_to_provider_observations(tmp_path,basis,expected):
    c=store.open_store(tmp_path/'office.db')
    c.execute('INSERT INTO agents VALUES(?,?)',('judge',json.dumps(dict(generation=2,provider='codex',model='requested'))))
    c.execute('INSERT INTO turn_runs VALUES(?,?)',('t',json.dumps(dict(actor_id='judge',generation=2,state='completed',created_at=100,value=dict(served_model='gpt-6-luna',served_model_basis=basis)))))
    c.commit();c.close()
    s=read_state(tmp_path,now=200.,host_now=200.)
    assert s['agents']['items'][0]['served_model_basis']==expected
    assert s['providers']['items'][0]['served_model_basis']==expected


def test_sealed_basis_and_model_are_not_exposed(tmp_path):
    c=store.open_store(tmp_path/'office.db')
    c.execute('INSERT INTO agents VALUES(?,?)',('judge',json.dumps(dict(generation=2,provider='codex'))))
    c.execute('INSERT INTO turn_runs VALUES(?,?)',('t',json.dumps(dict(actor_id='judge',generation=2,created_at=100,evaluation_derived=True,value=dict(served_model='SECRET_MODEL',served_model_basis='requested_pinned')))))
    c.commit();c.close();s=read_state(tmp_path,now=200.,host_now=200.)
    assert s['agents']['items'][0]['served_model_basis'] is None
    assert 'SECRET_MODEL' not in json.dumps(s)
