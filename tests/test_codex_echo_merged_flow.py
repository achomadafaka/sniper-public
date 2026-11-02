"""Actual crawler/JUDGE SDK adapters through U, the desk and the intent ledger.

Only SDK transport, assembler safety, cluster decision, SOL price and buy are fake.
This is an offline integration check; it does not measure real model quality.
"""
import importlib.util
import json
import sqlite3
from pathlib import Path

import claude_agent_sdk as sdk
import pytest
import test_codex_crawler_merged_flow as H
from test_codex_crawler_merged_flow import flow  # noqa: F401
from test_memecoin_desk_pipeline import databases  # noqa: F401
from test_memecoin_desk_judge_worker import assistant, result
from sniper.persistence import db_memecoin as ledger
from sniper.persistence import db_memecoin_desk as store

C, F, CW, JW = H.C, H.F, H.CF.W, H.F.W
REAL_JUDGE_SDK = JW.sdk_call_model
MODEL = C.MODEL


def transport(w, monkeypatch, echo, text, *, stage):
    calls = []

    async def query(prompt, options):
        assert options.model == MODEL
        assert options.tools == [] and options.mcp_servers == {}
        assert options.setting_sources == [] and options.max_turns == 1
        if stage == "judge":
            sheet = json.loads(prompt)
            assert H.RAW not in prompt
            payload = F.answer(sheet)["text"] if text == "answer" else text
            calls.append(sheet)
        else:
            assert H.RAW in prompt and prompt.count(C.FENCE_CLOSE) == 1
            payload = text
            calls.append(prompt)
        w["clock"][0] += 0.1
        if echo is not None:
            yield assistant(model=echo)
        yield result(result=payload)

    monkeypatch.setattr(sdk, "query", query)
    return calls


def crawl(w, monkeypatch, echo=MODEL, *, bad_text=False):
    text = ["bad response"] if bad_text else json.dumps({
        "k": w["mint"], "intent": "analysis", "flags": {k: False for k in C.FLAGS}})
    calls = transport(w, monkeypatch, echo, text, stage="crawler")
    w["clock"][0] = w["rank_at"] - 10
    assert CW.main([]) == 0
    assert len(calls) == 1
    with sqlite3.connect(w["desk"] / "crawler" / "crawls.db") as db:
        row = db.execute("SELECT intent,model,call_error FROM crawls ORDER BY id DESC LIMIT 1").fetchone()
    H.emit_and_bind(w)
    return row


def judge(w, monkeypatch, echo=MODEL, text="answer"):
    # Restore the production adapter replaced by the inherited offline fixture.
    monkeypatch.setattr(JW, "sdk_call_model", REAL_JUDGE_SDK)
    return transport(w, monkeypatch, echo, text, stage="judge")


def saved_call(w):
    row = w["conn"].execute("SELECT result_json FROM desk_judge_calls").fetchone()
    assert row and row[0]
    return json.loads(row[0])


def known_cost(w):
    cost = ledger.get_desk_cohort_cost(w["ledger"], w["cohort"], "desk-judge:" + w["cand"]["ticket_id"])
    assert cost["kind"] == "model" and cost["usd"] == 0, cost


@pytest.mark.parametrize("echo", [None, MODEL, MODEL + "-20261015"], ids=["omitted", "exact", "snapshot"])
def test_both_default_sdk_adapters_accept_the_shared_echo_rule_through_courier(flow, monkeypatch, echo):
    w = flow
    assert crawl(w, monkeypatch, echo) == ("analysis", echo, None)
    calls = judge(w, monkeypatch, echo)
    F.run_workers(w)
    assert len(calls) == 1 and calls[0]["crawler_intent"] == "analysis"
    assert saved_call(w)["model_echo"] == echo
    known_cost(w)
    buys = []
    F.intents.drain(w["ledger"], lambda conn, mint, *a, **kw: buys.append((mint, kw)) or
                    {"status": "refused", "reason": "offline"},
                    now_fn=lambda: F.datetime.now(F.timezone.utc).isoformat())
    assert len(buys) == 1 and buys[0][0] == w["mint"]
    assert buys[0][1]["size_usd"] == 25
    assert buys[0][1]["desk_ticket_id"] == w["cand"]["ticket_id"]
    assert F.judge(w) == 0
    assert store.get_ticket(w["conn"], w["cand"]["ticket_id"])["state"] == "refused"
    policy = store.get_cohort(w["conn"], w["cohort"])["policy"]
    assert (policy["max_open"], policy["daily_halt_usd"], policy["cumulative_kill_usd"]) == (5, 200, -200)


@pytest.mark.parametrize("echo", [MODEL + "-anything", MODEL + "-٢٠٢٦١٠١٥", {"model": MODEL}, [MODEL]],
                         ids=["garbage", "unicode-digits", "object", "array"])
def test_bad_judge_echo_persists_cost_and_raw_audit_without_an_intent(flow, monkeypatch, echo):
    w = flow
    crawl(w, monkeypatch)
    calls = judge(w, monkeypatch, echo)
    F.run_workers(w)
    assert len(calls) == 1 and F.intents.pending_count(w["ledger"]) == 0
    assert store.list_outbox(w["conn"]) == []
    head = store.get_ticket(w["conn"], w["cand"]["ticket_id"])
    assert any(s["stage"] == "vet" and s["verdict"] == "SKIP" for s in head["stages"])
    raw = saved_call(w)
    if isinstance(echo, str):
        assert raw["model_echo"] == echo
    else:
        assert isinstance(raw["model_echo"], str) and repr(echo) in raw["model_echo"]
    known_cost(w)


def test_malformed_judge_text_is_a_costed_typed_skip_through_default_adapter(flow, monkeypatch):
    w = flow
    crawl(w, monkeypatch)
    calls = judge(w, monkeypatch, text=["bad reply"])
    F.run_workers(w)
    assert len(calls) == 1 and F.intents.pending_count(w["ledger"]) == 0
    assert saved_call(w)["call_error"] == "model_result_error"
    known_cost(w)


@pytest.mark.parametrize("echo,bad_text", [({"model": MODEL}, False), (MODEL + "-٢٠٢٦١٠١٥", False),
                                         (MODEL, True)], ids=["bad-echo-type", "unicode-echo", "bad-text-type"])
def test_failed_crawler_reply_is_missing_and_keeps_the_independent_judge_path(flow, monkeypatch, echo, bad_text):
    w = flow
    row = crawl(w, monkeypatch, echo, bad_text=bad_text)
    assert row[0] is None and row[2]
    field = w["cand"]["snapshot"]["fields"]["crawler_intent"]
    assert field["value"] is None and field["completeness"] == "missing"
    if not isinstance(echo, str):
        assert isinstance(row[1], str) and repr(echo) in row[1]
    calls = judge(w, monkeypatch)
    F.run_workers(w)
    assert len(calls) == 1 and calls[0]["crawler_intent"] is None


def test_snapshot_echo_reaches_fill_and_close_reconciliation(flow, monkeypatch):
    crawl(flow, monkeypatch, MODEL + "-20261015")
    judge(flow, monkeypatch, MODEL + "-20261015")
    F.test_confirmed_position_close_reaches_the_ticket(flow, explicit_reconcile=False)
    known_cost(flow)


def test_surrogate_echo_is_a_bound_failed_crawl_and_costed_judge_skip(flow, monkeypatch):
    # Construct inside the test: an unpaired surrogate cannot be an xdist collection parameter.
    echo = MODEL + "\ud800"
    row = crawl(flow, monkeypatch, echo)
    assert row[0] is None and row[2] and "\\ud800" in row[1]
    calls = judge(flow, monkeypatch, echo)
    F.run_workers(flow)
    assert len(calls) == 1 and F.intents.pending_count(flow["ledger"]) == 0
    assert "\\ud800" in saved_call(flow)["model_echo"]
    known_cost(flow)


def test_scout_remains_default_off_with_no_sdk_calls_or_desk_intents(flow, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts" / "memecoin_desk_source_scout.py"
    spec = importlib.util.spec_from_file_location("codex_merged_scout", path)
    scout = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scout)
    monkeypatch.delenv("MEMECOIN_DESK_SCOUT_ENABLED", raising=False)
    calls = transport(flow, monkeypatch, MODEL, "kol", stage="scout")
    assert scout.main(["daily"]) == 0
    assert calls == [] and F.intents.pending_count(flow["ledger"]) == 0
    assert flow["conn"].execute("SELECT COUNT(*) FROM desk_tickets").fetchone()[0] == 0
