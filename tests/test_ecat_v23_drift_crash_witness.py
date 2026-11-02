from test_desk_v23_drift_requeue import entered, refuse, ON, LATER
from test_memecoin_desk_pipeline import databases, api
from sniper.persistence import db_memecoin_desk as store

def test_committed_drift_fill_recovers_requeue_after_crash(databases, monkeypatch):
    t,intent=entered(databases,ON)
    refuse(databases,intent)
    real=api()._drift_requeue
    monkeypatch.setattr(api(),"_drift_requeue",lambda *a,**k:None)
    api().reconcile_intents(*databases,now_iso=LATER)
    monkeypatch.setattr(api(),"_drift_requeue",real)
    api().reconcile_intents(*databases,now_iso=LATER)
    assert store.get_ticket(databases[0],t+":drift") is not None


def test_drift_candidate_without_filter_is_recovered(databases, monkeypatch):
    t,intent=entered(databases,ON)
    refuse(databases,intent)
    real=api().evaluate_candidate
    def fail(*args,**kwargs):
        raise RuntimeError("simulated failure after candidate commit")
    monkeypatch.setattr(api(),"evaluate_candidate",fail)
    api().reconcile_intents(*databases,now_iso=LATER)
    monkeypatch.setattr(api(),"evaluate_candidate",real)
    api().reconcile_intents(*databases,now_iso=LATER)
    head=store.get_ticket(databases[0],t+":drift")
    assert head is not None
    assert any(s["stage"]=="hard_filter" for s in head["stages"])


def test_unfinished_child_is_not_stranded_after_resume_window(databases,monkeypatch):
    from datetime import datetime,timedelta
    t,intent=entered(databases,ON);refuse(databases,intent)
    real=api().evaluate_candidate
    monkeypatch.setattr(api(),"evaluate_candidate",lambda *a,**k:(_ for _ in ()).throw(RuntimeError("crash")))
    api().reconcile_intents(*databases,now_iso=LATER)
    monkeypatch.setattr(api(),"evaluate_candidate",real)
    late=(datetime.fromisoformat(LATER)+timedelta(seconds=601)).isoformat()
    api().reconcile_intents(*databases,now_iso=late)
    head=store.get_ticket(databases[0],t+":drift")
    assert head["state"]!="open" or any(s["stage"]=="hard_filter" for s in head["stages"])
