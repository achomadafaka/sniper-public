"""New PumpSwap facts require a new frozen cohort; legacy inputs stay byte-identical."""
import copy

import pytest

from scripts import memecoin_desk_provision_cohort as P
from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_context_facts as C
from test_desk_intake_feed_policy import frozen, FEEDS
from test_desk_context_facts import build, MINT, NOW
from test_memecoin_desk_intake_filter import RULE, intake_candidate, ev, pol
from test_memecoin_desk_contract import BOUNDS, AT
from test_desk_ps_model_facts import tape, facts


def enabled():
    return P.frozen_policy(judge_p_admit=1, round_trip_loss_pct_max=8, hook_allowlist=[],
                           rail_bounds=BOUNDS, venue="pumpswap", intake={**RULE, "feeds": FEEDS}, ps_inputs=True)


def test_new_input_semantics_are_opt_in_frozen_and_prompt_bound():
    old = frozen(FEEDS)
    new = enabled()
    assert "ps_inputs" not in old and "context" not in old and "structure" not in old
    assert new["ps_inputs"] == "pumpswap-facts-v1"
    assert new["hard_filter"]["intake_source_version"] == "desk-intake-source-v1"
    assert new["context"]["pumpswap"] is True and new["structure"]["sheet"] == "facts-v2"
    assert P.cohort_id(old) != P.cohort_id(new)
    K.validate_ps_inputs(new, "desk-judge-v27")
    for version in ("desk-judge-v26", "desk-judge-v28"):
        with pytest.raises(ValueError, match="ps_inputs"):
            K.validate_ps_inputs(new, version)
    with pytest.raises(ValueError, match="ps_inputs"):
        K.validate_ps_inputs(old, "desk-judge-v27")


@pytest.mark.parametrize("change", [{"venue": "pumpfun"}, {"intake": None}])
def test_new_inputs_require_intake_pumpswap(change):
    args = dict(judge_p_admit=1, round_trip_loss_pct_max=8, hook_allowlist=[], rail_bounds=BOUNDS,
                venue="pumpswap", intake=RULE, ps_inputs=True)
    args.update(change)
    with pytest.raises(ValueError, match="ps_inputs"):
        P.frozen_policy(**args)


def test_pumpswap_context_uses_general_sources_and_retires_curve_aggregates(tmp_path):
    root = build(tmp_path)
    policy = enabled()
    fn = C.judge_context_fn(root, lambda _: {"policy": policy})
    out = C.validate(fn({}, {"venue": "pumpswap", "mint": MINT}, NOW))
    assert out["trending"] == {"state": "unknown", "reason": "curve_retired"}
    assert out["narrative"]["theme"] == "animal_dog"
    assert out["narrative"]["theme_15m"] == {"state": "unknown", "reason": "curve_retired"}
    assert out["dex"]["boosted"] is True and out["heat"]["crawler_intent"] == "promotion"
    assert C.judge_context_fn(root, lambda _: {"policy": frozen(FEEDS)})({}, {"venue": "pumpswap"}, NOW) is None
    assert C.judge_context_fn(root, lambda _: {"policy": {"context": {}}})({}, {"venue": "pumpswap"}, NOW)["dex"]["reason"] == "not_applicable"


def test_sheet_projects_only_bound_facts_and_keeps_original_snapshot(tmp_path):
    c, _ = tape(tmp_path)
    original = copy.deepcopy(c)
    f = facts(c, tmp_path)
    sheet = K.render_judge_sheet(c, ps_facts=f)
    assert c == original
    assert sheet["flow_60s"] == f["flow_60s"]
    assert sheet["structure"] == f["structure"] and sheet["ps_facts"] == f
    assert sheet["age_s"] is None and "flow_60s" not in sheet["missing"]
    for key, value in (("mint", "other"), ("snapshot_hash", "0"*64), ("as_of", NOW+1)):
        wrong = copy.deepcopy(f)
        wrong[key] = value
        with pytest.raises(ValueError, match="ps_facts"):
            K.render_judge_sheet(c, ps_facts=wrong)


def test_new_hard_filter_requires_source_receipt_without_changing_old_cohort():
    c = intake_candidate()
    assert K.hard_filter(c, evidence=ev(c), policy=pol(), evaluated_at=AT)["action"] == "PASS"
    p = {**pol(), "intake_source_version": "desk-intake-source-v1"}
    assert K.hard_filter(c, evidence=ev(c), policy=p, evaluated_at=AT)["reason"] == "unknown:intake_source:missing"
    e = {**ev(c), "intake_source": {"status": "pass"}}
    assert K.hard_filter(c, evidence=e, policy=p, evaluated_at=AT)["action"] == "SKIP"


def test_safety_projection_requires_bound_hard_filter_and_keeps_unknowns():
    c = intake_candidate()
    evidence = ev(c)
    safety = K.safety_from_evidence(c, evidence, policy=pol(), as_of=K._utc(AT))
    assert safety["authority_mint"] == safety["authority_freeze"] == "closed"
    assert safety["sell_probe"] == "ok"
    assert safety["lp_locked_or_burned"] is None
    sheet = K.render_judge_sheet(c, safety=safety)
    assert sheet["safety"] == safety and "safety" not in sheet["missing"]
    evidence["snapshot_hash"] = "0"*64
    with pytest.raises(ValueError, match="safety"):
        K.safety_from_evidence(c, evidence, policy=pol(), as_of=K._utc(AT))


def test_safety_proof_is_bound_to_original_proof_and_fresh_pool_cut(tmp_path):
    c, _ = tape(tmp_path)
    f = facts(c, tmp_path)
    safe = {k: None for k in ("authority_mint", "authority_freeze", "cluster_hit", "lp_locked_or_burned",
                              "top10_pct", "creator_pct", "sell_probe", "sells_exist_60s")}
    proof = dict(network_id=c["network_id"], mint=c["mint"], pool=c["pool"],
                 emission_snapshot_hash="0"*64, known_at=NOW-1, probe_receipt_cut=f["receipt_cut"],
                 probe_cut_slot=f["cut_slot"])
    assert K.render_judge_sheet(c, ps_facts=f, safety=safe, safety_proof=proof)["safety_proof"] == proof
    for key, value in (("known_at", NOW+1), ("probe_cut_slot", f["cut_slot"]+1), ("mint", "foreign")):
        wrong = {**proof, key: value}
        with pytest.raises(ValueError, match="safety_proof"):
            K.render_judge_sheet(c, ps_facts=f, safety=safe, safety_proof=wrong)


@pytest.mark.parametrize("token_cap", [None, 123])
def test_v27_cli_preview_freezes_actual_inputs_and_writes_no_stores(tmp_path, monkeypatch, capsys, token_cap):
    import json
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_DESK_RAIL_BOUNDS", "tp_pct:15-150,sl_pct:10-40,trail_pct:8-30")
    if token_cap is None:
        monkeypatch.delenv("MEMECOIN_DESK_JUDGE_MAX_OUTPUT_TOKENS", raising=False)
    else:
        monkeypatch.setenv("MEMECOIN_DESK_JUDGE_MAX_OUTPUT_TOKENS", str(token_cap))
    args = ["--judge-p-admit", "1", "--round-trip-loss-pct-max", "8", "--intake", "--venue", "pumpswap",
            "--ps-inputs", "--intake-feeds", *FEEDS, "--playbook",
            str(P.ROOT / "docs/superpowers/specs/2026-10-27-desk-judge-playbook-v27.md"), "--intake-prereg",
            "docs/audits/2026-10-27-three-feed-desk-inputs-v27-preregistration.md@52de42b4b3f6b45af9a30d8a4f1a3f303495d7f0",
            "--desk-db", str(tmp_path / "desk.db"), "--ledger-db", str(tmp_path / "ledger.db")]
    assert P.main(args) == 0
    output = json.loads(capsys.readouterr().out.split("\nMEMECOIN_DESK_U_COHORT_ID=", 1)[0])
    assert output["prompt_version"] == "desk-judge-v27"
    assert output["policy"]["structure"]["sheet"] == "facts-v2"
    assert output["policy"]["hard_filter"]["intake"]["feeds"] == FEEDS
    assert output["policy"]["judge_calls_per_hour"] == 40
    assert output["policy"].get("judge_max_output_tokens") == token_cap
    assert not (tmp_path / "desk.db").exists() and not (tmp_path / "ledger.db").exists()
    assert P.main([*args, "--expected-cohort-id", "desk-cohort:U:" + "0"*64, "--confirm"]) == 2
    assert not (tmp_path / "desk.db").exists() and not (tmp_path / "ledger.db").exists()


def test_initial_print_can_use_later_block_proof_without_backdating_knowledge(tmp_path):
    from datetime import datetime, timezone
    from test_desk_intake_provenance_store import setup, admit, receipt
    from test_memecoin_desk_contract import evidence
    conn, c, hard = setup(tmp_path)
    admit(conn, c, tmp_path)
    proof = receipt(conn, c, hard)
    now = proof["known_at"] + 2
    at = datetime.fromtimestamp(now, timezone.utc).isoformat()
    e = evidence(c)
    state = c["snapshot"]["fields"]["pool_state"]["value"]
    e["known_at"] = at
    e["checks"]["token_safety"].update(supply_atomic=state["supply_atomic"], decimals=state["base_decimals"])
    e["intake_source"] = proof
    cut = dict(receipt_cut=c["snapshot"]["emission_at"], cut_slot=state["slot"]+1, block_known_at=now)
    e["market_cut"] = cut
    assert K.hard_filter(c, evidence=e, policy=hard, evaluated_at=at)["action"] == "PASS"
    for bad in (None, {**cut, "block_known_at": now+1}, {**cut, "cut_slot": state["slot"]},
                {**cut, "receipt_cut": cut["receipt_cut"]+1}):
        invalid = {**e, "market_cut": bad}
        assert K.hard_filter(c, evidence=invalid, policy=hard, evaluated_at=at)["reason"] == "unknown:evidence:market_cut"
