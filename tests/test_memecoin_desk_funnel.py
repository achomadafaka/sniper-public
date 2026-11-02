"""Desk funnel end to end through the real entrypoints on the ranker-gated JUDGE worker: U emission ->
cluster writer -> assembler -> ranker --once -> JUDGE worker main (courier + reconcile) -> intent drain.
Fixture and the first nine checks are Codex's merged-flow harness (2e7d7ee29); the courier page checks
are Lambda's (db0f97595). Fake provider/model/buy/alerts, temporary DBs and test clocks only."""
import json
import sqlite3
import sys
import time as real_time
import test_memecoin_desk_ranker as TR
from datetime import datetime,timezone
from contextlib import nullcontext
from types import SimpleNamespace
import pytest
import test_memecoin_desk_assembler as TA
from test_memecoin_desk_pipeline import databases
from test_memecoin_desk_judge_witness import answer
from test_memecoin_desk_judge_worker import W
from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk as desk,memecoin_desk_cluster as cluster,memecoin_manual_intents as intents
from sniper.services.memecoin_cluster_admission import ClusterDecision

@pytest.fixture
def funnel(databases,tmp_path,monkeypatch):
    # Isolated pytest processes only: the clocks advance through real entrypoints, no live readers.
    rank_at = TR.T0 + 60
    clock = [rank_at]
    def sleep(seconds):
        clock[0] += seconds
    fake_time = SimpleNamespace(time=lambda:clock[0], monotonic=real_time.monotonic, sleep=sleep)
    real_datetime = datetime
    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            value = real_datetime.fromtimestamp(clock[0], timezone.utc)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    monkeypatch.setattr(sys.modules[__name__],"datetime",FrozenDateTime)
    monkeypatch.setattr(TA,"time",fake_time)
    monkeypatch.setattr(TA.A,"time",fake_time)
    monkeypatch.setattr(TR.W,"time",fake_time)
    monkeypatch.setattr(W,"datetime",FrozenDateTime)
    monkeypatch.setattr(TA,"POLICY",{**TA.POLICY,"model_billing_basis":"subscription_flat",
                                    "select_version":"desk-judge-select-v1"})
    w=TA.world.__wrapped__(databases,tmp_path,monkeypatch)
    c=w["cand"]
    # The ranker binds its emission-price read to the actual trigger swap identity.
    with sqlite3.connect(w["desk"]/"swaps"/(w["day"]+".db")) as conn:
        conn.execute("UPDATE swaps SET signature=?,slot=? WHERE signature='s'",(c["trigger_signature"],c["band_entry_slot"]))
    def factory(allowance):
        meter,read=cluster.metered(lambda:None,allowance)
        class Service:
            def decide(self,mint,**kw):
                read()
                now=datetime.now(timezone.utc).isoformat()
                return ClusterDecision("d",True,"allowed","p",now,now,{})
        return meter,Service()
    stats=cluster.run_pass(factory,w["desk"]/"candidate_emissions",w["desk"]/"cluster_checks",
        per_min=6,calls_per_candidate=40,calls_per_min=300,calls_per_day=20000,
        now_fn=lambda:datetime.now(timezone.utc).isoformat(),epoch_fn=lambda:clock[0])
    assert stats["decided"]==1
    monkeypatch.setenv("MEMECOIN_DESK_JUDGE_ENABLED","1")
    monkeypatch.setenv("MEMECOIN_DESK_RANKER_ENABLED","1")
    monkeypatch.setattr(W,"LATENCY_LOG",tmp_path/"latency.jsonl")
    monkeypatch.setattr("sniper.services.memecoin_alerts.send_alert",lambda *a,**k:None)
    pb=tmp_path/"playbook.md";pb.write_text("prompt_version: `desk-judge-v12`\n---\nOffline enum prompt\n")
    calls=[]
    w.update(ledger=databases[1],calls=calls,pb=pb,clock=clock,rank_at=rank_at)
    # The as-of refresh needs a healthy U tape: a fresh universe heartbeat on the fake clock at every judge start
    # (the swaps day file above already holds the trigger swap). The refresh stays ON; no global test switch.
    real_main=W.main
    def main_with_heartbeat(*a,**kw):
        (w["desk"]/"universe.heartbeat").write_text(json.dumps({"ts":clock[0],"connected":True,"sol_usd":180.0,"sol_age_s":1.0}))
        return real_main(*a,**kw)
    monkeypatch.setattr(W,"main",main_with_heartbeat)
    def model(system,user,**kw):
        assert kw.get("persist_session") is False, kw            # the judge run path opts out of session persistence
        sheet=json.loads(user);calls.append(sheet)
        return answer(sheet)
    monkeypatch.setattr(W,"sdk_call_model",model)
    return w

def judge(w):
    return W.main(["--desk-db",str(w["tmp"]/"desk.db"),"--ledger-db",str(w["tmp"]/"ledger.db"),
        "--playbook",str(w["pb"]),"--calls-per-hour","15"],env_root=w["tmp"])

def rank(w, rank_at=None):
    rank_at=w["rank_at"] if rank_at is None else rank_at
    w["clock"][0]=rank_at+2
    assert TR.W.main(["--once"])==0
    return rank_at

def run_workers(w):
    assert TA.run(w)==0
    rank(w)
    assert judge(w)==0

def test_actual_judge_worker_hands_enter_to_the_intent_worker(funnel):
    w=funnel;run_workers(w)
    assert len(w["calls"])==1
    stages={s["stage"]:s["verdict"] for s in store.get_ticket(w["conn"],w["cand"]["ticket_id"])["stages"]}
    assert stages["hard_filter"]=="PASS" and stages["vet"]=="ENTER"
    buys=[]
    def buy(conn,mint,*a,**kw):
        buys.append((mint,kw));return {"status":"refused","reason":"offline witness"}
    intents.drain(w["ledger"],buy,now_fn=lambda:datetime.now(timezone.utc).isoformat())
    assert len(buys)==1,{"pending_intents":intents.pending_count(w["ledger"]),"outbox":store.list_outbox(w["conn"])}

def test_explicit_courier_control_reaches_buy_once_and_replay_never_duplicates(funnel):
    w=funnel;run_workers(w)
    now=datetime.now(timezone.utc).isoformat()
    desk.dispatch_outbox(w["conn"],w["ledger"],now_iso=now)
    desk.dispatch_outbox(w["conn"],w["ledger"],now_iso=now)
    assert intents.pending_count(w["ledger"])==1
    buys=[]
    def buy(conn,mint,*a,**kw):
        buys.append((mint,kw));return {"status":"refused","reason":"offline witness"}
    intents.drain(w["ledger"],buy,now_fn=lambda:datetime.now(timezone.utc).isoformat())
    intents.drain(w["ledger"],buy,now_fn=lambda:datetime.now(timezone.utc).isoformat())
    assert len(buys)==1
    mint,kw=buys[0]
    assert mint==w["cand"]["mint"] and kw["network_id"]=="solana"
    assert kw["signal_source"]=="desk" and kw["desk_ticket_id"]==w["cand"]["ticket_id"]
    assert kw["size_usd"]==25 and kw["desk_rails"]=={"tp_pct":60.0,"sl_pct":20.0,"trail_pct":15.0}
    assert kw["desk_metadata"]["cohort_id"]==w["cohort"]
    desk.reconcile_intents(w["conn"],w["ledger"],now_iso=datetime.now(timezone.utc).isoformat())
    assert store.get_ticket(w["conn"],w["cand"]["ticket_id"])["state"]=="refused"
    assert desk.reconcile_intents(w["conn"],w["ledger"],now_iso=datetime.now(timezone.utc).isoformat())==[]

def test_unbound_cluster_row_is_unknown_not_yet_ready_for_the_model_hf2(funnel):
    w=funnel
    path=next((w["desk"]/"cluster_checks").glob("*.jsonl"))
    row=json.loads(path.read_text());row["snapshot_hash"]="0"*64
    path.write_text(json.dumps(row)+"\n")
    run_workers(w)
    assert TA.stage(w,w["cand"]["ticket_id"])[0]["verdict"]=="PASS"          # the filter never waits for or kills on cluster
    assert len(w["calls"])==1
    cluster=w["calls"][0]["evidence"]["cluster"]
    assert (cluster["state"],cluster["check"],cluster["reason"])==("unknown","unknown","not_yet_ready")   # forged row never binds


def test_actual_judge_worker_reconciles_terminal_intent_feedback(funnel):
    w=funnel;run_workers(w)
    desk.dispatch_outbox(w["conn"],w["ledger"],now_iso=datetime.now(timezone.utc).isoformat())
    done=intents.drain(w["ledger"],lambda *a,**kw:{"status":"refused","reason":"offline witness"},
        now_fn=lambda:datetime.now(timezone.utc).isoformat())
    assert len(done)==1 and done[0][1]=="refused"
    assert W.main(["--desk-db",str(w["tmp"]/"desk.db"),"--ledger-db",str(w["tmp"]/"ledger.db"),
        "--playbook",str(w["pb"]),"--calls-per-hour","15"],env_root=w["tmp"])==0
    h=store.get_ticket(w["conn"],w["cand"]["ticket_id"])
    assert h["state"]=="refused" and any(s["stage"]=="fill" and s["reason"]=="intent_refused" for s in h["stages"]),h


@pytest.mark.parametrize("explicit_reconcile",[False,True])
def test_confirmed_position_close_reaches_the_ticket(funnel,explicit_reconcile):
    from sniper.persistence import db_memecoin as ledger
    w=funnel;run_workers(w)
    request=(store.list_outbox(w["conn"]) or store.list_intents(w["conn"]))[0]
    now=datetime.now(timezone.utc).isoformat()
    desk.dispatch_outbox(w["conn"],w["ledger"],now_iso=now)
    pid=ledger.open_position(w["ledger"],source_user_id=None,source_handle="offline-fixture",
        mint=w["cand"]["mint"],ticker=None,entry_price=0.0001,entry_notional=25,entry_fill_source="chain",
        entry_lag_s=0,their_size_usd=0,opened_at=now,execution_mode="real",network_id="solana",
        signal_source="desk",funded_contract_json=json.dumps({"source":{"desk":request["payload"]["desk_metadata"]}}))
    intents.resolve(w["ledger"],request["intent_id"],state="filled",now_iso=now,
        position_id=pid,result_status="filled",resolved_size_usd=25)
    desk.reconcile_intents(w["conn"],w["ledger"],now_iso=now)
    assert store.get_ticket(w["conn"],w["cand"]["ticket_id"])["state"]=="filled"
    closed=datetime.now(timezone.utc).isoformat()
    ledger.close_position(w["ledger"],pid,closed,2.0,"trail",recorded_at=closed)
    assert ledger.get_position(w["ledger"],pid)["is_open"]==0
    if explicit_reconcile:
        desk.reconcile_intents(w["conn"],w["ledger"],now_iso=datetime.now(timezone.utc).isoformat())
    else:
        assert W.main(["--desk-db",str(w["tmp"]/"desk.db"),"--ledger-db",str(w["tmp"]/"ledger.db"),
            "--playbook",str(w["pb"]),"--calls-per-hour","15"],env_root=w["tmp"])==0
    h=store.get_ticket(w["conn"],w["cand"]["ticket_id"])
    assert h["state"]=="closed" and [s["stage"] for s in h["stages"]].count("close")==1,h


def test_pending_ranker_waits_then_the_real_ranker_enables_one_call(funnel):
    w=funnel
    assert TA.run(w)==0
    assert judge(w)==0
    assert w["calls"]==[] and intents.pending_count(w["ledger"])==0
    assert w["conn"].execute("SELECT COUNT(*) FROM desk_judge_calls").fetchone()[0]==0
    rank(w)
    assert judge(w)==0 and judge(w)==0
    assert len(w["calls"])==1 and intents.pending_count(w["ledger"])==1
    c=store.get_cohort(w["conn"],w["cohort"])["policy"]
    assert (c["clip_usd"],c["max_open"],c["daily_halt_usd"],c["cumulative_kill_usd"])==(25,5,200,-200)


def test_closed_unshortlisted_is_a_no_call_cut_through_worker(funnel):
    w=funnel
    assert TA.run(w)==0
    later=w["rank_at"]+60
    with sqlite3.connect(w["desk"]/"swaps"/(w["day"]+".db")) as conn:
        conn.execute("INSERT INTO swaps SELECT (SELECT MAX(slot) FROM swaps)+1,'later',ordinal,mint,curve,is_buy,quote_atomic,"
            "token_atomic,actor,virtual_quote*3,virtual_token,block_time,?,real_token,fee_bps,creator_fee_bps FROM swaps WHERE actor='a'",
            (later-1,))
    rank(w,later)
    assert judge(w)==0 and judge(w)==0
    h=store.get_ticket(w["conn"],w["cand"]["ticket_id"])
    assert any(s["stage"]=="vet" and s["reason"]=="judge_ranker_cut" for s in h["stages"]),h
    assert w["calls"]==[] and intents.pending_count(w["ledger"])==0
    assert store.judge_calls_since(w["conn"],since_iso="2026-10-21T00:00:00+00:00")==0


def test_missing_ranker_is_a_versioned_no_call_coverage_hole(funnel):
    from sniper.services import memecoin_desk_judge as J
    w=funnel
    assert TA.run(w)==0
    w["clock"][0]+=J.missing_after_s()+1
    assert judge(w)==0 and judge(w)==0
    h=store.get_ticket(w["conn"],w["cand"]["ticket_id"])
    assert any(s["stage"]=="vet" and s["reason"]=="judge_ranker_missing" for s in h["stages"]),h
    results=w["conn"].execute("SELECT result_json FROM desk_judge_calls").fetchall()
    assert len(results)==1 and json.loads(results[0][0])["select_version"]=="desk-judge-select-v1"
    assert w["calls"]==[] and intents.pending_count(w["ledger"])==0
    assert store.judge_calls_since(w["conn"],since_iso="2026-10-21T00:00:00+00:00")==0


def test_one_fill_and_close_flow_through_drain_and_worker_feedback(funnel):
    from sniper.persistence import db_memecoin as ledger
    w=funnel
    run_workers(w)
    buys=[]
    def buy(conn,mint,*a,**kw):
        buys.append(kw)
        now=datetime.now(timezone.utc).isoformat()
        pid=ledger.open_position(conn,source_user_id=None,source_handle="offline-fixture",mint=mint,
            ticker=None,entry_price=0.0001,entry_notional=kw["size_usd"],entry_fill_source="chain",
            entry_lag_s=0,their_size_usd=0,opened_at=now,execution_mode="real",network_id=kw["network_id"],
            signal_source=kw["signal_source"],funded_contract_json=json.dumps({"source":{"desk":kw["desk_metadata"]}}))
        return {"status":"filled","position_id":pid,"resolved_size_usd":kw["size_usd"]}
    done=intents.drain(w["ledger"],buy,now_fn=lambda:datetime.now(timezone.utc).isoformat())
    assert len(done)==1 and done[0][1]=="filled",done
    assert intents.drain(w["ledger"],buy,now_fn=lambda:datetime.now(timezone.utc).isoformat())==[]
    assert judge(w)==0
    h=store.get_ticket(w["conn"],w["cand"]["ticket_id"])
    assert h["state"]=="filled" and len(buys)==1,h
    status=intents.read_status(w["ledger"],done[0][0])
    pid=status["position_id"]
    now=datetime.now(timezone.utc).isoformat()
    ledger.close_position(w["ledger"],pid,now,2.0,"trail",recorded_at=now)
    assert judge(w)==0 and judge(w)==0
    h=store.get_ticket(w["conn"],w["cand"]["ticket_id"])
    assert h["state"]=="closed" and len(buys)==1,h
    assert [s["stage"] for s in h["stages"]].count("fill")==1
    assert [s["stage"] for s in h["stages"]].count("close")==1


def test_a_courier_refusal_pages_once_and_recovery_pages_once(tmp_path):
    # Gamma MED on a23a5d93e: a permanent intent_conflict stopped every later ENTER with no page
    state, pages = tmp_path / "desk_courier_page.json", []
    alert = lambda kind, text: pages.append((kind, text))
    bad = [{"step": "dispatch_outbox", "refused": "intent_conflict", "ticket_id": "desk:U:abc"}]
    for _ in range(3):                                         # three passes, one restart in between
        W.page_refusals(bad, state, alert)
    assert [k for k, _ in pages] == ["DESK-COURIER"] and "intent_conflict:desk:U:abc" in pages[0][1]
    W.page_refusals([{"step": "dispatch_outbox", "ticket_id": "t", "state": "sent"}], state, alert)
    W.page_refusals([], state, alert)
    assert len(pages) == 2 and "recovered" in pages[1][1]


def test_an_intent_conflict_through_the_worker_pages_once_with_the_row(funnel, monkeypatch):
    w = funnel
    pages = []
    monkeypatch.setattr("sniper.services.memecoin_alerts.send_alert", lambda kind, text, **kw: pages.append(text))
    monkeypatch.setattr(desk, "dispatch_outbox", lambda *a, **kw: (_ for _ in ()).throw(
        desk._at(ValueError("intent_conflict"), w["cand"]["ticket_id"])))
    run_workers(w)
    for _ in range(2):
        assert W.main(["--desk-db", str(w["tmp"] / "desk.db"), "--ledger-db", str(w["tmp"] / "ledger.db"),
                       "--playbook", str(w["pb"]), "--calls-per-hour", "15"], env_root=w["tmp"]) == 0
    assert len(pages) == 1 and "intent_conflict:" + w["cand"]["ticket_id"] in pages[0]


def test_dispatch_names_the_row_it_refused_on_not_the_oldest_pending(funnel, monkeypatch):
    # Codex/Gamma on dd39f54ba: an older pending row A (cohort_accounting_unknown) hid a B -> C change
    w = funnel
    run_workers(w)
    from sniper.persistence import db_memecoin_desk as _store
    monkeypatch.setattr(desk.intents, "read_status", lambda conn, intent_id: {"mint": "someone-else"})
    # the worker already sent the row; serve it as pending again so dispatch meets the conflict
    monkeypatch.setattr(desk.store, "list_outbox", lambda conn, limit=10: _store.list_intents(conn, limit=limit))
    with pytest.raises(ValueError, match="intent_conflict") as caught:
        desk.dispatch_outbox(w["conn"], w["ledger"], now_iso=datetime.now(timezone.utc).isoformat())
    assert caught.value.ticket_id == w["cand"]["ticket_id"]
    pages = []
    W.page_refusals([{"step": "dispatch_outbox", "refused": "intent_conflict", "ticket_id": "B"}],
                    w["tmp"] / "p.json", lambda k, t: pages.append(t))
    W.page_refusals([{"step": "dispatch_outbox", "refused": "intent_conflict", "ticket_id": "C"}],
                    w["tmp"] / "p.json", lambda k, t: pages.append(t))
    assert len(pages) == 2 and ":C" in pages[1]


def test_an_unwritable_page_state_never_kills_the_worker_or_repages(tmp_path):
    pages, memo = [], {}
    state = tmp_path / "missing-dir" / "desk_courier_page.json"
    bad = [{"step": "dispatch_outbox", "refused": "intent_conflict", "ticket_id": "A"}]
    for _ in range(3):
        W.page_refusals(bad, state, lambda k, t: pages.append(t), memo)
    assert len(pages) == 1 and not state.exists()


def test_a_stale_tape_makes_the_actual_judge_worker_make_no_call(funnel):
    # the as-of refresh stays ON in the real worker: the tape loses the emission trigger swap AFTER the ranker shortlisted
    # the ticket = stale_snapshot, no model call, a recorded SKIP
    w=funnel
    assert TA.run(w)==0
    rank(w)
    with sqlite3.connect(w["desk"]/"swaps"/(w["day"]+".db")) as conn:
        conn.execute("UPDATE swaps SET signature='gone' WHERE signature=(SELECT MIN(signature) FROM swaps)")
    assert judge(w)==0
    assert w["calls"]==[]
    stages={s["stage"]:s["verdict"] for s in store.get_ticket(w["conn"],w["cand"]["ticket_id"])["stages"]}
    assert stages["vet"]=="SKIP"
