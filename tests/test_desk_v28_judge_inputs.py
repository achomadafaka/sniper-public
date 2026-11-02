"""V28 freezes the exact prompt, canonical cohort identity and partial seen inputs."""
import base64
import copy
import hashlib
import json
import zlib
from contextlib import nullcontext

import pytest

from scripts import memecoin_desk_judge_worker as W
from sniper.persistence import db_memecoin_desk as D
from sniper.services import memecoin_desk_judge as J
from sniper.services import memecoin_desk_intake as I
from sniper.services import memecoin_desk_ps_facts as F
from test_desk_v28_policy_binding import DIGEST, SYSTEM, v28_policy
from test_desk_ps_inputs_integration import Clock, block, forbidden, iso, refresh, ready as ready_v27
from test_desk_ps_model_facts import NOW, tape
from test_memecoin_desk_contract import rehash
from test_memecoin_desk_judge import databases, model_returning  # noqa: F401

RELEASE = "a" * 40


def ready(db, tmp):
    c, _ = tape(tmp, coverage=0)
    c["snapshot"]["fields"]["pool_state"]["value"].update(virtual_quote_known_at=NOW - 5,
        virtual_quote_slot=7, virtual_quote_source="pumpswap_grpc:buy_event_virtual_quote")
    listing = NOW - 30
    source = I.open_day(tmp / "intake", "pumpfun", listing)
    I.record_poll(source, "pumpfun", "active", listing,
        [{"network_id": "solana", "chain_raw": "solana", "mint": c["mint"], "venue": "pumpswap", "raw": {}}], offset_s=0)
    source.close()
    c["intake"] = {"feed": "pumpfun", "filter": "active", "provider_rank": 1, "rank_kind": "list_position",
        "observed_at": listing, "host_received_at": listing, "clock_offset_s": 0,
        "intake_row_ref": iso(listing)[:10] + ":pumpfun:1:0"}
    rehash(c)
    cid = J.desk.provision_cohort(*db, producer="U", policy=v28_policy(),
        prompt_version="desk-judge-v28", created_at=iso(NOW - 3600))
    ticket = J.desk.ingest_candidate(db[0], c, cohort_id=cid, now_iso=iso(NOW),
        intake_dir=tmp / "intake", source_now_fn=lambda: NOW)["ticket_id"]
    D.record_stage(db[0], ticket_id=ticket, stage="hard_filter", seat="code", verdict="PASS",
        reason="fixture", inputs={"evidence": {"known_at": iso(NOW)}}, output={}, ts=iso(NOW))
    return ticket, cid


def inputs(ticket, candidate, meta, ts, *, tmp):
    out = block(ticket, candidate, meta, ts, tmp=tmp)
    out["ps_facts"] = F.facts_for(candidate, desk_dir=tmp, as_of=ts, receipt_cut=meta["receipt_cut"],
        cut_slot=meta["cut_slot"], ours=frozenset(("ours",)), version="pumpswap-facts-v2")
    return out


def skip(sheet):
    return json.dumps({"k": sheet["opportunity_id"], "v": "S", "e": "insufficient_data", "r": None,
                       "c": None, "enter_ttl_s": None, "next_check_s": None})


def run(db, tmp, *, calls=None, clock=None, call_model=None, **kw):
    calls, clock = ([] if calls is None else calls), clock or Clock()
    return J.run_once(db[0], lambda: nullcontext(db[1]),
        call_model=call_model or model_returning(skip, calls=calls, clock=clock, echo=J.GPT_MODEL),
        system_prompt=kw.pop("system_prompt", SYSTEM), worker="judge-999999", now_fn=clock,
        calls_per_hour=40, asof_fn=refresh, structure_fn=forbidden,
        ps_inputs_fn=kw.pop("ps_inputs_fn", lambda *args: inputs(*args, tmp=tmp)),
        release_sha=RELEASE, expected_prompt_version="desk-judge-v28", **kw)


def saved(db):
    return json.loads(db[0].execute("SELECT result_json FROM desk_judge_calls").fetchone()[0])


def canonical_hash(db, cid):
    return db[1].execute("SELECT policy_hash FROM memecoin_desk_cohorts WHERE cohort_id=?", (cid,)).fetchone()[0]


def test_prompt_digest_mismatch_refuses_before_claim_model_and_ledger(databases, tmp_path):
    ticket, _ = ready(databases, tmp_path / "tape")
    with pytest.raises(ValueError, match="judge_playbook_mismatch"):
        run(databases, tmp_path / "tape", system_prompt=SYSTEM + "changed", call_model=forbidden)
    assert databases[0].execute("SELECT COUNT(*) FROM desk_judge_calls").fetchone()[0] == 0
    assert not any(s["stage"] == "vet" for s in D.get_ticket(databases[0], ticket)["stages"])


def test_serialized_model_input_preserves_zero_coverage_seen_totals_and_counters(databases, tmp_path):
    ticket, cid = ready(databases, tmp_path / "tape")
    calls = []
    run(databases, tmp_path / "tape", calls=calls)
    assert len(calls) == 1
    f = calls[0]["ps_facts"]
    assert f["version"] == "pumpswap-facts-v2"
    assert f["flow_60s"]["covered_s"] == 0 and f["flow_60s"]["partial"] is True
    assert (f["flow_60s"]["buy_usd"], f["flow_60s"]["sell_usd"], f["flow_60s"]["lower_bound"]) == (80, 20, True)
    assert f["flow_60s"]["loss_classes_exclusive"] is True
    assert all(k in f["flow_60s"] for k in ("unknown_loss_receipts", "global_loss_receipts", "own_pool_losses"))
    assert len(f["structure"]["chart_coverage"]) == len(f["structure"]["chart"]) == 24
    assert all(len(bar) == 8 for bar in f["structure"]["chart"])
    assert calls[0]["structure"] == f["structure"] and calls[0]["flow_60s"] == f["flow_60s"]
    result = saved(databases)
    exact = zlib.decompress(base64.b64decode(result["sheet_zb64"])).decode()
    assert json.loads(exact) == calls[0]
    assert hashlib.sha256(exact.encode()).hexdigest() == result["sheet_sha256"]
    assert result["system_sha256"] == DIGEST and result["release_sha"] == RELEASE
    assert result["prompt_version"] == "desk-judge-v28" and result["policy_hash"] == canonical_hash(databases, cid)
    vet = next(s for s in D.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet")
    assert vet["inputs"]["sheet"] == calls[0]


@pytest.mark.parametrize("early", [False, True])
def test_trusted_v28_identity_wins_over_untrusted_result_and_skip_context(databases, tmp_path, monkeypatch, early):
    _, cid = ready(databases, tmp_path / "tape")
    spoof = {"prompt_version": "spoof", "policy_hash": "0" * 64, "system_sha256": "0" * 64,
             "release_sha": "0" * 40, "sheet_sha256": "0" * 64}
    if early:
        monkeypatch.setattr(J, "_skip_context", lambda *a, **kw: copy.deepcopy(spoof))
        run(databases, tmp_path / "tape", admit_fn=lambda _: False)
    else:
        base = model_returning(skip, calls=[], echo=J.GPT_MODEL)
        def model(*a, **kw):
            return {**base(*a, **kw), **spoof}
        run(databases, tmp_path / "tape", call_model=model)
    result = saved(databases)
    assert (result["prompt_version"], result["policy_hash"], result["system_sha256"], result["release_sha"]) == (
        "desk-judge-v28", canonical_hash(databases, cid), DIGEST, RELEASE)
    if not early:
        assert result["sheet_sha256"] != spoof["sheet_sha256"]


def test_version_mismatch_is_typed_before_claim_and_never_legacy_falls_back(databases, tmp_path):
    ticket, _ = ready(databases, tmp_path / "tape")
    def wrong(*args):
        return block(*args, tmp=tmp_path / "tape")  # default v1
    out = run(databases, tmp_path / "tape", ps_inputs_fn=wrong, call_model=forbidden)
    assert out[0]["reason"] == "ps_inputs_version_mismatch"
    assert databases[0].execute("SELECT COUNT(*) FROM desk_judge_calls").fetchone()[0] == 0
    t = D.get_ticket(databases[0], ticket)
    c, meta = refresh(t["candidate"], NOW + 1)
    b = block(t, c, meta, NOW + 1, tmp=tmp_path / "tape")
    with pytest.raises(ValueError, match="ps_inputs_version_mismatch"):
        J.desk.record_decision(databases[0], object(), ticket, None, bounds=v28_policy()["rail_bounds"],
            elapsed_ms=0, call_started_at=iso(NOW), response_at=iso(NOW), now_iso=iso(NOW), **b)


def test_v27_also_refuses_v2_before_claim_and_record(databases, tmp_path):
    ticket, _ = ready_v27(databases, tmp_path / "tape")
    out = J.run_once(databases[0], forbidden, call_model=forbidden, system_prompt="fixture", worker="judge-test",
        now_fn=Clock(), calls_per_hour=40, asof_fn=refresh,
        ps_inputs_fn=lambda *args: inputs(*args, tmp=tmp_path / "tape"))
    assert out[0]["reason"] == "ps_inputs_version_mismatch"
    assert databases[0].execute("SELECT COUNT(*) FROM desk_judge_calls").fetchone()[0] == 0
    t = D.get_ticket(databases[0], ticket)
    c, meta = refresh(t["candidate"], NOW + 1)
    with pytest.raises(ValueError, match="ps_inputs_version_mismatch"):
        J.desk.record_decision(databases[0], object(), ticket, None, bounds=v28_policy()["rail_bounds"],
            elapsed_ms=0, call_started_at=iso(NOW), response_at=iso(NOW), now_iso=iso(NOW),
            **inputs(t, c, meta, NOW + 1, tmp=tmp_path / "tape"))


@pytest.mark.parametrize("flow,expected", [
    ({"sell_n": 1, "partial": True, "covered_s": 0}, True),
    ({"sell_n": 0, "partial": True, "covered_s": 0}, None),
    ({"sell_n": 0, "partial": True, "covered_s": 9}, None),
    ({"sell_n": 0, "partial": False, "covered_s": 60}, False),
    (None, None),
])
def test_worker_selects_frozen_version_and_partial_zero_never_proves_no_sells(databases, tmp_path, monkeypatch, flow, expected):
    ticket, _ = ready(databases, tmp_path / "tape")
    t = D.get_ticket(databases[0], ticket)
    c, meta = refresh(t["candidate"], NOW + 1)
    safety = block(t, c, meta, NOW + 1, tmp=tmp_path / "tape")["safety"]
    monkeypatch.setattr(J.contract, "safety_from_evidence", lambda *a, **kw: copy.deepcopy(safety))
    seen = []
    def facts(*a, **kw):
        seen.append(kw)
        return {"version": "pumpswap-facts-v2", "flow_60s": flow}
    monkeypatch.setattr(F, "facts_for", facts)
    out = W.make_ps_inputs_fn(databases[0], tmp_path / "tape", lambda _: [])(t, c, meta, NOW + 1)
    assert seen[0]["version"] == "pumpswap-facts-v2"
    assert out["safety"]["sells_exist_60s"] is expected


def test_saved_v28_recovery_keeps_original_identity_without_new_prompt_or_input_reads(databases, tmp_path):
    _, cid = ready(databases, tmp_path / "tape")
    clock, calls = Clock(), []
    def crash(*a, **kw):
        raise RuntimeError("saved before record")
    with pytest.raises(RuntimeError, match="saved before record"):
        run(databases, tmp_path / "tape", clock=clock, calls=calls, record_fn=crash)
    before = saved(databases)
    assert before["prompt_version"] == "desk-judge-v28" and before["policy_hash"] == canonical_hash(databases, cid)
    (tmp_path / "tape" / "pumpswap" / "tape.db").unlink()
    J.recover(databases[0], lambda: nullcontext(databases[1]), now_fn=clock)
    assert saved(databases) == before and len(calls) == 1


def test_bare_v28_claim_recovery_does_not_invent_prompt_or_policy_identity(databases, tmp_path):
    ticket, _ = ready(databases, tmp_path / "tape")
    assert D.claim_judge_call(databases[0], ticket_id=ticket, call_id="lost-v28", worker="judge-999999", claimed_at=iso(NOW))
    J.recover(databases[0], lambda: nullcontext(databases[1]), now_fn=Clock())
    assert saved(databases) == {"call_error": "lost"}
