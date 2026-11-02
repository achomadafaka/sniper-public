"""Paper-only status publication; all state is temporary."""
import json
from types import SimpleNamespace

import pytest

from scripts import memecoin_desk_paper_engine as engine
from sniper.services import memecoin_desk_paper as paper


@pytest.mark.parametrize('failed',[False,True])
def test_main_publishes_counter_even_when_pass_fails(tmp_path,monkeypatch,failed):
    from sniper.util import envfiles
    from sniper.services import memecoin_telegram
    monkeypatch.setattr(envfiles,'load_env_files',lambda *_:None)
    monkeypatch.setattr(memecoin_telegram,'send_telegram_audited',lambda *_:None)
    monkeypatch.setattr(paper,'RUN_ID','a'*32,raising=False)
    monkeypatch.setattr(paper,'MARK_LOG_ERRORS',{'mark_log_errors':3},raising=False)
    def once(*args,**kwargs):
        if failed:raise ValueError('fixture failure')
        return []
    monkeypatch.setattr(engine,'run_once',once)
    data=tmp_path/'data';data.mkdir()
    assert engine.main(['--desk-db',str(data/'desk.db')])==0
    status=json.loads((data/'desk/desk_paper_engine.status').read_text())
    assert status['run_id']=='a'*32 and status['mark_log_errors']==3
    assert status['started_at_host']<=status['ts_host']
    assert status['engine_error'] is failed


def test_old_producer_does_not_fabricate_zero(tmp_path):
    engine.publish_status(tmp_path,SimpleNamespace(),started_at=10,now=11,engine_error=False)
    value=json.loads((tmp_path/'desk_paper_engine.status').read_text())
    assert value['run_id'] is None and value['mark_log_errors'] is None


def test_status_write_failure_is_visible_without_changing_pass(tmp_path,monkeypatch,capsys):
    from sniper.util import envfiles
    from sniper.services import memecoin_telegram
    monkeypatch.setattr(envfiles,'load_env_files',lambda *_:None)
    monkeypatch.setattr(memecoin_telegram,'send_telegram_audited',lambda *_:None)
    monkeypatch.setattr(engine,'run_once',lambda *a,**k:[])
    def fail(*a,**k):raise OSError('fixture disk full')
    monkeypatch.setattr(engine,'publish_status',fail)
    data=tmp_path/'data';data.mkdir()
    assert engine.main(['--desk-db',str(data/'desk.db')])==0
    assert 'status_error' in capsys.readouterr().err
