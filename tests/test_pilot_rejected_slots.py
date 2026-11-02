"""Unsigned refusals release capacity without erasing immutable admission history."""
import pytest
from sniper.settings import memecoin as cfg
from test_codex_crowd_admission import B, I, M, NOW, db, enabled, request


def enqueue(c, wallet='W'):
    r=request(wallet);r.pop('ttl_s')
    return I.enqueue(c,now_iso=B.iso(NOW),**r)


def refuse(c, iid, **kw):
    I.resolve(c,iid,state=I.REFUSED,now_iso=B.iso(NOW+1),result_status='rejected',reason='cohort_accounting_unknown',**kw)


def test_unsigned_reject_releases_capacity_and_keeps_history(enabled,monkeypatch):
    monkeypatch.setattr(cfg,'MEMECOIN_LARGE_BUYER_MAX_FILLS',1)
    c=db();iid=enqueue(c);refuse(c,iid)
    assert I._pilot_slots(c,B.CROWD_COHORT_ID)==0
    assert enqueue(c,'replacement')
    assert c.execute('SELECT count(*) FROM memecoin_pilot_admissions').fetchone()[0]==2
    assert I.read_status(c,iid)['state']==I.REFUSED


@pytest.mark.parametrize('hazard',['queued','unresolved','missing_attempt','signed','submitted','reserved','position','submission_record'])
def test_uncertain_or_executed_work_still_consumes_capacity(enabled,hazard):
    c=db();iid=enqueue(c)
    if hazard=='queued':pass
    elif hazard=='unresolved':I.resolve(c,iid,state=I.UNRESOLVED,now_iso=B.iso(NOW+1),result_status='submitted_unknown')
    else:
        attempt='attempt' if hazard in ('missing_attempt','signed','submitted') else None
        refuse(c,iid,attempt_id=attempt)
        if hazard in ('signed','submitted'):
            c.execute('INSERT INTO memecoin_entry_attempts(attempt_id,origin_event_id,decision_started_at,terminal_status,terminal_reason,completed_at,signing_completed_at,transaction_submitted_at) VALUES(?,?,?,?,?,?,?,?)',('attempt','desk:lb:P:W',B.iso(NOW),'refused','rejected',B.iso(NOW+1),B.iso(NOW) if hazard=='signed' else None,B.iso(NOW) if hazard=='submitted' else None))
        if hazard in ('reserved','submission_record'):
            c.execute("INSERT INTO memecoin_pending_entries(mint,reserved_at,reserved_notional_usd,status,origin_event_id,desk_cohort_id,desk_opportunity_id) VALUES('mint',?,25,?,?,?,?)",(B.iso(NOW),'reserved' if hazard=='reserved' else 'failed','desk:lb:P:W',B.CROWD_COHORT_ID,'lb:P:W'))
            if hazard=='submission_record':
                pid=c.execute('SELECT id FROM memecoin_pending_entries').fetchone()[0]
                c.execute("INSERT INTO memecoin_submission_attempts(chain_id,action,owner_kind,owner_id,provider_id,state,created_at) VALUES('solana','entry','reservation',?,'rpc','not_sent',?)",(str(pid),B.iso(NOW)))
        if hazard=='position':
            c.execute("INSERT INTO memecoin_positions(mint,entry_price,entry_notional,opened_at,execution_mode,signal_source,origin_event_id,desk_cohort_id,desk_opportunity_id) VALUES('mint',1,25,?,'real','desk','desk:lb:P:W',?,'lb:P:W')",(B.iso(NOW),B.CROWD_COHORT_ID))
        c.commit()
    assert I._pilot_slots(c,B.CROWD_COHORT_ID)==1


def test_unsigned_failed_reservation_releases_capacity(enabled):
    c=db();iid=enqueue(c);refuse(c,iid,attempt_id='attempt')
    c.execute('INSERT INTO memecoin_entry_attempts(attempt_id,origin_event_id,decision_started_at,terminal_status,terminal_reason,completed_at) VALUES(?,?,?,?,?,?)',('attempt','desk:lb:P:W',B.iso(NOW),'refused','price cap',B.iso(NOW+1)))
    c.execute("INSERT INTO memecoin_pending_entries(mint,reserved_at,reserved_notional_usd,status,origin_event_id,attempt_id,desk_cohort_id,desk_opportunity_id) VALUES('mint',?,25,'failed','desk:lb:P:W','attempt',?,'lb:P:W')",(B.iso(NOW),B.CROWD_COHORT_ID));c.commit()
    assert I._pilot_slots(c,B.CROWD_COHORT_ID)==0


@pytest.mark.parametrize('field',['tx_sig','signed_payload_b64','signed_payload_sha256','broadcast_at','submission_bound_at','cleanup_tx_sig','cleanup_signed_at','cleanup_signed_payload_sha256'])
def test_failed_but_signed_reservation_never_releases_capacity(enabled,field):
    c=db();iid=enqueue(c);refuse(c,iid)
    c.execute("INSERT INTO memecoin_pending_entries(mint,reserved_at,reserved_notional_usd,status,origin_event_id,desk_cohort_id,desk_opportunity_id) VALUES('mint',?,25,'failed','desk:lb:P:W',?,'lb:P:W')",(B.iso(NOW),B.CROWD_COHORT_ID))
    c.execute(f'UPDATE memecoin_pending_entries SET {field}=?',('proof',));c.commit()
    assert I._pilot_slots(c,B.CROWD_COHORT_ID)==1


def test_ambiguous_refused_result_is_not_released(enabled):
    c=db();iid=enqueue(c)
    I.resolve(c,iid,state=I.REFUSED,now_iso=B.iso(NOW+1),result_status='error',reason='unknown outcome')
    assert I._pilot_slots(c,B.CROWD_COHORT_ID)==1
