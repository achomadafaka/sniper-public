"""Crawler default adapter -> real U protobuf emission -> existing merged desk funnel.

SDK transport, assembler safety check, cluster decision, SOL price and funded buy are fake.
Curve math, all worker entrypoints, persistence and ledger reconciliation are real; DBs are temporary.
"""
import json
import sqlite3
import struct
from types import SimpleNamespace

import pytest
import claude_agent_sdk as sdk
import test_memecoin_desk_crawler as CF
import test_memecoin_desk_funnel as F
import test_memecoin_desk_universe_worker as UF
from test_memecoin_desk_pipeline import databases
from test_memecoin_desk_judge_worker import assistant, result
from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk_crawler as C
from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_safety as S
from sniper.services.memecoin_desk_asof import INITIAL_VIRTUAL_QUOTE

RAW = "UNTRUSTED_RAW_CRAWLER_INPUT"


def trade(mcap):
    vq, vt = UF.vq_for(mcap)
    return (UF.TRADE + UF.MINT + struct.pack("<QQ?", 10**9, 10**9, True) + UF.USER
            + struct.pack("<q", 1) + struct.pack("<QQQQ", vq, vt, vq-INITIAL_VIRTUAL_QUOTE, vt-S.PUMP_VIRTUAL_TOKEN_OFFSET)
            + UF.FEE + struct.pack("<QQ", 100, 1) + UF.CREATOR + struct.pack("<QQ", 30, 1))


@pytest.fixture
def flow(databases, tmp_path, monkeypatch):
    monkeypatch.setattr(UF, "SOL", 180.0)  # Match the inherited funnel's as-of heartbeat.
    monkeypatch.setattr(F.TA,"POLICY",{**F.TA.POLICY,"crawler_version":C.CRAWLER_VERSION})
    w=F.funnel.__wrapped__(databases,tmp_path,monkeypatch)
    # Replace the canned candidate with a real U emission; no ticket has been ingested yet.
    (w["desk"]/"candidate_emissions"/(w["day"]+".jsonl")).write_text("")
    (w["desk"]/"cluster_checks"/(w["day"]+".jsonl")).unlink()
    fake_time=SimpleNamespace(time=lambda:w["clock"][0],monotonic=F.real_time.monotonic,
                              sleep=lambda seconds:w["clock"].__setitem__(0,w["clock"][0]+seconds))
    monkeypatch.setattr(UF.W,"time",fake_time)
    w["clock"][0]=w["rank_at"]-120
    producer,_=UF.worker(w["desk"])
    producer.sol_usd=UF.SOL
    producer.sol_at=w["rank_at"]-20
    w["clock"][0]=w["rank_at"]-20
    producer.process(UF.frame_meta(10,int(w["clock"][0])))
    producer.process(UF.frame_tx(10,1,trade(20000)))
    producer.files.commit()
    mint=UF.U.b58encode(UF.MINT)
    x=w["desk"]/"x"
    x.mkdir()
    with sqlite3.connect(x/(w["day"]+".db")) as src:
        src.execute("CREATE TABLE posts(id INTEGER PRIMARY KEY,event TEXT,tweet_id TEXT,received_at REAL,handle TEXT,payload_json TEXT)")
        src.execute("CREATE TABLE token_meta(id INTEGER PRIMARY KEY,tweet_id TEXT,mint TEXT,chain TEXT,symbol TEXT,received_at REAL,payload_json TEXT)")
        src.execute("INSERT INTO token_meta(mint,chain,symbol,received_at,payload_json) VALUES(?,NULL,'SYM',?,?)",
            (mint,w["rank_at"]-11,json.dumps({"tokenMeta":{"name":RAW}})))
    monkeypatch.setenv("MEMECOIN_DESK_CRAWLER_ENABLED","1")
    monkeypatch.setenv("MEMECOIN_DESK_CRAWLER_CALLS_PER_HOUR","40")
    monkeypatch.setenv("MEMECOIN_DESK_CRAWLER_CALLS_PER_DAY","600")
    # Use crawler main's real default SDK adapter. The only injected service argument is its clock.
    real_pass=CF.W.crawl_pass
    monkeypatch.setattr(CF.W,"crawl_pass",lambda db,**kw:real_pass(db,clock=lambda:w["clock"][0],**kw))
    w.update(producer=producer,mint=mint,crawl_sdk=[],judge_calls=[])
    def judge_model(_system,user,persist_session=True):
        sheet=json.loads(user)
        assert RAW not in user
        w["judge_calls"].append(sheet)
        return F.answer(sheet)
    monkeypatch.setattr(F.W,"sdk_call_model",judge_model)
    yield w
    for conn in producer.files._open.values():
        conn.close()


def crawl(w,monkeypatch,intent="analysis",flags=None,*,is_error=False):
    flags={k:False for k in C.FLAGS} if flags is None else flags
    text=json.dumps({"k":w["mint"],"intent":intent,"flags":flags})
    async def query(prompt,options):
        w["crawl_sdk"].append(prompt)
        assert options.model==C.MODEL and options.tools==[] and options.mcp_servers=={}
        assert options.setting_sources==[] and options.max_turns==1
        assert RAW in prompt and prompt.count(C.FENCE_CLOSE)==1
        w["clock"][0]+=1
        yield assistant(model=C.MODEL)
        yield result(result=text,is_error=is_error)
    monkeypatch.setattr(sdk,"query",query)
    w["clock"][0]=w["rank_at"]-10
    assert CF.W.main([])==0
    with sqlite3.connect(w["desk"]/"crawler"/"crawls.db") as db:
        row=db.execute("SELECT intent,known_at,inputs_json,call_error FROM crawls ORDER BY id DESC LIMIT 1").fetchone()
    assert RAW not in row[2]
    return row


def emit_and_bind(w):
    w["clock"][0]=w["rank_at"]-1
    w["producer"].process(UF.frame_meta(11,int(w["clock"][0])))
    w["producer"].process(UF.frame_tx(11,2,trade(40000)))
    w["producer"].files.commit()
    path=w["desk"]/"candidate_emissions"/(w["day"]+".jsonl")
    rows=[json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows)==1
    w["cand"]=rows[0]
    assert K.render_judge_sheet({**rows[0],"opportunity_id":rows[0]["ticket_id"]})
    w["clock"][0]+=0.1
    def factory(allowance):
        meter,read=F.cluster.metered(lambda:None,allowance)
        class Service:
            def decide(self,mint,**kw):
                read()
                now=F.datetime.now(F.timezone.utc).isoformat()
                return F.ClusterDecision("d",True,"allowed","p",now,now,{})
        return meter,Service()
    stats=F.cluster.run_pass(factory,w["desk"]/"candidate_emissions",w["desk"]/"cluster_checks",
        per_min=6,calls_per_candidate=40,calls_per_min=300,calls_per_day=20000,
        now_fn=lambda:F.datetime.now(F.timezone.utc).isoformat(),epoch_fn=lambda:w["clock"][0])
    assert stats["decided"]==1
    return rows[0]


def test_crawl_default_sdk_u_snapshot_judge_and_courier_stay_connected(flow,monkeypatch):
    w=flow
    row=crawl(w,monkeypatch)
    assert row[0]=="analysis" and row[1]<w["rank_at"]-1 and row[3] is None
    candidate=emit_and_bind(w)
    assert candidate["snapshot"]["fields"]["crawler_intent"]["value"]=="analysis"
    F.run_workers(w)
    assert len(w["crawl_sdk"])==1 and len(w["judge_calls"])==1
    assert w["judge_calls"][0]["crawler_intent"]=="analysis"
    buys=[]
    def buy(conn,mint,*a,**kw):
        buys.append((mint,kw))
        return {"status":"refused","reason":"offline"}
    F.intents.drain(w["ledger"],buy,now_fn=lambda:F.datetime.now(F.timezone.utc).isoformat())
    assert len(buys)==1 and buys[0][0]==w["mint"]
    assert buys[0][1]["size_usd"]==25 and buys[0][1]["desk_ticket_id"]==candidate["ticket_id"]
    assert F.judge(w)==0
    assert store.get_ticket(w["conn"],candidate["ticket_id"])["state"]=="refused"
    policy=store.get_cohort(w["conn"],w["cohort"])["policy"]
    assert (policy["max_open"],policy["daily_halt_usd"],policy["cumulative_kill_usd"])==(5,200,-200)
    assert policy["crawler_version"]==C.CRAWLER_VERSION


@pytest.mark.parametrize("intent,flags",[("warning",None),("analysis",{**{k:False for k in C.FLAGS},"rug_history_claim":True})])
def test_prior_crawler_warning_reaches_the_judge_as_a_fact_hf2(flow,monkeypatch,intent,flags):
    w=flow
    crawl(w,monkeypatch,intent,flags)
    candidate=emit_and_bind(w)
    F.run_workers(w)
    head=store.get_ticket(w["conn"],candidate["ticket_id"])
    assert any(s["stage"]=="hard_filter" and s["verdict"]=="PASS" for s in head["stages"]),head      # not a kill any more
    assert len(w["judge_calls"])==1
    sheet=w["judge_calls"][0]
    assert sheet["crawler_intent"]=="warning" or sheet["crawler_flags"]["rug_history_claim"] is True


def test_sdk_failure_remains_missing_through_the_merged_desk(flow,monkeypatch):
    w=flow
    row=crawl(w,monkeypatch,is_error=True)
    assert row[0] is None and row[3]=="model_result_error"
    candidate=emit_and_bind(w)
    field=candidate["snapshot"]["fields"]["crawler_intent"]
    assert field["value"] is None and field["completeness"]=="missing"
    F.run_workers(w)
    assert len(w["judge_calls"])==1 and w["judge_calls"][0]["crawler_intent"] is None


def test_post_emission_crawl_cannot_rewrite_the_frozen_judge_sheet(flow,monkeypatch):
    w=flow
    crawl(w,monkeypatch)
    candidate=emit_and_bind(w)
    frozen=json.dumps(candidate,sort_keys=True)
    path=w["desk"]/"crawler"/"crawls.db"
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO crawls(network_id,mint,known_at,input_hash,inputs_json,intent,flags_json,model,call_error,prompt_version) "
            "VALUES('solana',?,?,'late','[]','warning',?,?,NULL,?)",
            (w["mint"],w["rank_at"]+1,json.dumps({k:False for k in C.FLAGS}),C.MODEL,C.CRAWLER_VERSION))
    F.run_workers(w)
    assert json.dumps(w["cand"],sort_keys=True)==frozen
    assert len(w["judge_calls"])==1 and w["judge_calls"][0]["crawler_intent"]=="analysis"


@pytest.mark.parametrize("explicit_reconcile",[False,True])
def test_crawler_candidate_fill_and_close_reconcile(flow,monkeypatch,explicit_reconcile):
    crawl(flow,monkeypatch)
    emit_and_bind(flow)
    F.test_confirmed_position_close_reaches_the_ticket(flow,explicit_reconcile)
