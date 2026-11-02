"""Office T15 step 2 probe driver: one test per chain link (sheet, blind judges, aggregate, Risk, Trader, exit, grade,
auditor). No model calls: judges are injected fakes."""
import json
from types import SimpleNamespace

import pytest

from office.test_sheet_tool import Reads, candidate, hard, CALL_NOW            # noqa: F401  (sheet stubs)
from office.test_pilot_seats import policy, safe, MINT, NOW, FX

from sniper.office import ledger as L
from sniper.office import probe as P
from sniper.office import store as S
from sniper.office import trader as T


def env(outcome="success", decision="ENTER", served="claude-opus-5-5", text=None):
    return SimpleNamespace(outcome=outcome, served_model=served, attempt_id="a1", error_reason=None,
                           text=text if text is not None else json.dumps({"decision": decision, "memo": "m"}))


SERVED = {"judge_claude": "claude-opus-5-5", "judge_gpt": "gpt-6-luna"}


def receipt(**sheet_over):
    return {"sheet_hash": "h" * 64, "sheet": {"safety": safe(), **sheet_over},
            "request": {"network_id": "solana", "mint": MINT, "pool": "P"}}


SHEET_KW = dict(candidate=candidate(), desk_dir="/d", policy={"rail_bounds": {}, "hard_filter": {}, "v": 1},
                clock_sample={"offset_s": 0.25, "sampled_at": CALL_NOW - 10}, hard_filter_row=hard(),
                now=CALL_NOW, buyer_ledger=[], strategy_epoch=P.PROBE_EPOCH)


# ---- sheet
def test_sheet_link_freezes_only_a_known_receipt(tmp_path, monkeypatch):
    Reads(monkeypatch)
    conn = S.open_store(tmp_path / "office.db")
    kw = dict(candidate=candidate(), desk_dir="/d", policy={"rail_bounds": {}, "hard_filter": {}, "v": 1},
              clock_sample={"offset_s": 0.25, "sampled_at": CALL_NOW - 10}, hard_filter_row=hard(), now=CALL_NOW,
              buyer_ledger=[], strategy_epoch=P.PROBE_EPOCH)
    r, bh = P.sheet_link(conn, "d1", **kw)
    assert r["state"] == "known" and len(bh) == 64
    assert P.sheet_link(conn, "d1", **kw)[1] == bh                                  # retry: same frozen bundle
    with pytest.raises(P.ProbeStop, match="sheet_refused"):
        P.sheet_link(conn, "d2", **dict(kw, hard_filter_row=hard(verdict="FAIL")))


# ---- blind judges
def test_each_judge_sees_the_same_view_and_never_another_memo():
    seen = []

    def call(slot, user, schema):
        seen.append((slot, user))
        return env(served=SERVED[slot], text=json.dumps({"decision": "ENTER", "memo": "SECRET-" + slot}))
    view = P.judge_view("d1", "b" * 64, receipt())
    out = P.judges_link(view, call)
    assert [s for s, _ in seen] == list(P.JUDGE_SLOTS)
    assert seen[0][1] == seen[1][1] == view and "SECRET" not in view and "memo" not in json.loads(view)
    assert all(out[s]["served_model"] for s in P.JUDGE_SLOTS)
    assert json.loads(view)["label"] == "non-evaluation"


def test_failed_unserved_or_offschema_judge_is_unknown_not_skip():
    answers = {"judge_claude": env(outcome="timeout"), "judge_gpt": env(served=None)}
    out = P.judges_link("v", lambda s, u, sc: answers[s])
    assert out["judge_claude"]["decision"] is None and out["judge_claude"]["reason"].startswith("call_timeout")
    assert out["judge_gpt"]["reason"] == "served_model_unknown"
    out = P.judges_link("v", lambda s, u, sc: env(served=SERVED[s], text='{"decision":"BUY","memo":"x"}'))
    assert all(r["reason"] == "answer_off_schema" for r in out.values())


# ---- aggregate
@pytest.mark.parametrize("a,b,want", [("ENTER", "ENTER", ("ENTER", None)),
                                      ("ENTER", "WAIT", ("SKIP", "judges_not_unanimous_enter")),
                                      ("ENTER", None, ("UNKNOWN", "judge_unknown:judge_gpt"))])
def test_aggregate_enters_only_on_unanimous_known_enter(a, b, want):
    assert P.aggregate({"judge_claude": {"decision": a}, "judge_gpt": {"decision": b}}) == want
    assert P.aggregate({"judge_claude": {"decision": "ENTER"}}) == ("UNKNOWN", "judge_set_incomplete")


# ---- Risk
def test_risk_reads_safety_from_the_sheet_and_vetoes_unknown():
    _, r = P.risk_link(policy(), "ENTER", "d1", "b" * 64, receipt())
    assert r.decision == "allow" and r.bundle_hash == "b" * 64 and r.clip_usd == 25.0
    _, r = P.risk_link(policy(), "ENTER", "d1", "b" * 64, receipt(safety=safe(sells_exist_60s=None)))
    assert (r.decision, r.reason) == ("veto", "sells_60s_unknown")
    _, r = P.risk_link(policy(), "SKIP", "d1", "b" * 64, receipt())
    assert r.reason == "not_enter"


# ---- Trader
@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "_canonical_pool", lambda mint: "P")
    conn = L.open_ledger(tmp_path / "ledger.db")
    T.bootstrap_pilot(conn, epoch_id=P.PROBE_EPOCH, book_id=P.PROBE_BOOK, clip_usd=policy().clip_usd,
                      rule_version="none", capital_usd=1000.0, actor="operator", now=NOW - 10)
    yield conn
    conn.close()


def test_trader_submits_once_on_the_probe_book_and_records_vetoes(book):
    bundle, r = P.risk_link(policy(), "ENTER", "d1", "b" * 64, receipt())
    rec = P.trader_link(book, policy(), bundle, r, now=NOW, sol_usd=FX, sol_at=NOW)
    assert rec["outcome"] == "submitted" and rec["order_id"] == "probe:d1" and rec["book_id"] == P.PROBE_BOOK
    assert P.trader_link(book, policy(), bundle, r, now=NOW + 1, sol_usd=FX, sol_at=NOW) == rec      # idempotent
    bundle, r = P.risk_link(policy(), "SKIP", "d2", "c" * 64, receipt())
    assert P.trader_link(book, policy(), bundle, r, now=NOW)["outcome"] == "vetoed"


# ---- grade
def test_grade_link_scores_a_closed_order_on_the_probe_epoch():
    order = {"order_id": "probe:d1", "state": "closed", "filled_at": NOW, "closed_at": NOW + 60,
             "spend_usd": 25.0, "exit_usd": 30.0, "costs_usd": 0.5}
    g = P.grade_link(order, look_at=NOW + 120, bound_usd_per_fill=0.1)
    assert g["entered"] is True and g["y"] == pytest.approx(30.0 - 25.0 - 0.5 - 0.2)
    assert (g["epoch_id"], g["label"]) == (P.PROBE_EPOCH, "non-evaluation")
    open_order = dict(order, state="open", closed_at=None)
    assert P.grade_link(open_order, look_at=NOW + 120, bound_usd_per_fill=0.1)["y_censored"] is True


# ---- real judge_call and probe book
def test_judge_call_refuses_before_any_transport_without_the_go():
    touched = []
    transports = {"claude": SimpleNamespace(client=SimpleNamespace(call=lambda *a, **k: touched.append(1)))}
    call = P.judge_call_over({"seats": {"judge_claude": {"provider": "claude", "model": "claude-opus-5-5"}}},
                             {"judge_claude": {"text": "p", "sha256": "0" * 64}}, transports, lambda: NOW,
                             decision_id="d1", deadline_s=30, allow_model_calls=False)
    with pytest.raises(P.ProbeStop, match="model_calls_not_allowed"):
        call("judge_claude", "v", P.JUDGE_SCHEMA)
    assert touched == []


def test_judge_call_refuses_a_changed_prompt_and_routes_through_pilot_models(monkeypatch):
    import hashlib
    seen = []
    monkeypatch.setattr("sniper.office.pilot_models.call",
                        lambda m, slot, system, user, **kw: seen.append((slot, system, user, sorted(kw["transports"]))) or env())
    manifest = {"seats": {"judge_gpt": {"provider": "codex", "model": "gpt-6-luna"}}}
    good = {"judge_gpt": {"text": "p", "sha256": hashlib.sha256(b"p").hexdigest()}}
    call = P.judge_call_over(manifest, good, {"codex": SimpleNamespace()}, lambda: NOW, decision_id="d1", deadline_s=30,
                             allow_model_calls=True)
    assert call("judge_gpt", "v", P.JUDGE_SCHEMA).served_model and seen == [("judge_gpt", "p", "v", ["codex"])]
    bad = P.judge_call_over(manifest, {"judge_gpt": {"text": "p2", "sha256": good["judge_gpt"]["sha256"]}},
                            {"codex": SimpleNamespace()}, lambda: NOW, decision_id="d1", deadline_s=30, allow_model_calls=True)
    with pytest.raises(P.ProbeStop, match="prompt_pin_mismatch"):
        bad("judge_gpt", "v", P.JUDGE_SCHEMA)


def test_probe_book_bootstraps_on_the_probe_epoch_from_the_pilot_file(tmp_path):
    from office.test_pilot_seats import ROOT
    conn = L.open_ledger(tmp_path / "ledger.db")
    pol = P.bootstrap_probe_book(conn, ROOT / "config/office/book_policy_pilot.json", now=NOW)
    assert pol.clip_usd == 25.0
    assert [tuple(r) for r in conn.execute("SELECT book_id, epoch_id, kind FROM books")] == [
        (P.PROBE_BOOK, P.PROBE_EPOCH, "discovery")]
    P.bootstrap_probe_book(conn, ROOT / "config/office/book_policy_pilot.json", now=NOW + 5)      # idempotent


# ---- unknown stops before Risk/Trader in run(); oversized memo is off-schema
def test_front_stops_on_unknown_without_a_trader_receipt(tmp_path, monkeypatch, book):
    Reads(monkeypatch)
    conn = S.open_store(tmp_path / "office.db")
    steps = []
    with pytest.raises(P.ProbeStop, match="aggregate_unknown"):
        P.front(decision_id="d8", store_conn=conn, ledger_conn=book, policy=policy(), sheet_kwargs=SHEET_KW,
                judge_call=lambda s, u, sc: env(outcome="timeout"), now=NOW, sol_usd=FX, sol_at=NOW,
                report=lambda step, payload: steps.append(step))
    assert steps == ["sheet", "judge:judge_claude", "judge:judge_gpt", "judges", "aggregate"]
    assert book.execute("SELECT COUNT(*) FROM decision_receipts").fetchone()[0] == 0


def test_front_then_back_waits_for_the_watcher_exit_then_grades(tmp_path, monkeypatch, book):
    Reads(monkeypatch)
    conn = S.open_store(tmp_path / "office.db")
    steps = []
    report = lambda step, payload: steps.append(step)
    rec = P.front(decision_id="d7", store_conn=conn, ledger_conn=book, policy=policy(),
                  sheet_kwargs=dict(SHEET_KW), judge_call=lambda s, u, sc: env(served=SERVED[s]), now=NOW,
                  sol_usd=FX, sol_at=NOW, report=report)
    assert steps == ["sheet", "judge:judge_claude", "judge:judge_gpt", "judges", "aggregate", "risk", "trader"]
    # Reads stubs safety to authority_mint only, so Risk vetoes: no order, graded as no_order
    assert rec["outcome"] == "vetoed" and P.back(book, rec, look_at=NOW, bound_usd_per_fill=0.1, report=report) == "no_order"
    bundle, r = P.risk_link(policy(), "ENTER", "d6", "b" * 64, receipt())
    rec = P.trader_link(book, policy(), bundle, r, now=NOW, sol_usd=FX, sol_at=NOW)
    assert P.back(book, rec, look_at=NOW, bound_usd_per_fill=0.1, report=report) == "waiting_exit"
    book.execute("UPDATE orders SET state='closed', filled_at=?, closed_at=?, spend_usd=25, exit_usd=26 "
                 "WHERE order_id='probe:d6'", (NOW, NOW + 30))
    assert P.back(book, rec, look_at=NOW + 60, bound_usd_per_fill=0.1, report=report) == "graded"
    assert steps[-2:] == ["exit", "grade"]


def test_oversized_memo_is_off_schema():
    out = P.judges_link("v", lambda s, u, sc: env(served=SERVED[s], text=json.dumps({"decision": "ENTER", "memo": "x" * 16385})))
    assert all(r["reason"] == "answer_off_schema" for r in out.values())


# ---- Zeta FAIL e35b88fab witnesses (verbatim from ~/.cache/codex/zeta-probe-e35b88fab)
def test_zeta_unknown_never_becomes_skip():
    rows=P.judges_link('v',lambda s,u,sc:env(outcome='timeout') if s=='judge_gpt' else env())
    decision,_=P.aggregate(rows)
    assert decision != 'SKIP', 'unknown judgement became a known SKIP'


def test_zeta_offschema_extra_fields_cannot_enter():
    out=P.judges_link('v',lambda s,u,sc:env(text=json.dumps({'decision':'ENTER','memo':'m','extra':True})))
    assert all(row['decision'] is None for row in out.values())


def test_zeta_claude_echo_cannot_satisfy_gpt_slot():
    out=P.judges_link('v',lambda s,u,sc:env(served='claude-opus-5-5'))
    assert out['judge_gpt']['decision'] is None


def test_zeta_unknown_cannot_poison_later_known_retry(book):
    unknown=P.judges_link('v',lambda s,u,sc:env(outcome='timeout'))
    decision,_=P.aggregate(unknown)
    if decision in P.DECISIONS:
        bundle,r=P.risk_link(policy(),decision,'retry','b'*64,receipt())
        P.trader_link(book,policy(),bundle,r,now=NOW,sol_usd=FX,sol_at=NOW)
    bundle,r=P.risk_link(policy(),'ENTER','retry','b'*64,receipt())
    rec=P.trader_link(book,policy(),bundle,r,now=NOW,sol_usd=FX,sol_at=NOW)
    assert rec['outcome']=='submitted', rec


def test_retried_judge_call_reuses_one_request_id_per_decision_and_slot():
    import hashlib
    rids = []
    client = SimpleNamespace(call=lambda adapter, req, **kw: rids.append(kw["request_id"]) or env(served="gpt-6-luna"))
    transports = {"codex": SimpleNamespace(client=client, adapter=None)}
    prompts = {"judge_gpt": {"text": "p", "sha256": hashlib.sha256(b"p").hexdigest()}}
    manifest = {"seats": {"judge_gpt": {"provider": "codex", "model": "gpt-6-luna"}}}
    mk = lambda d: P.judge_call_over(manifest, prompts, transports, lambda: NOW, decision_id=d, deadline_s=30,
                                     allow_model_calls=True)
    for call in (mk("d1"), mk("d1"), mk("d2")):
        try:
            call("judge_gpt", "v", P.JUDGE_SCHEMA)
        except Exception:
            pass                                           # pilot_models envelope parsing is not under test here
    assert len(rids) == 3 and rids[0] == rids[1] != rids[2]
