"""WO-7 + WO-13 — evidence-engine corrections and one-shot gate cadence.

Fixes the incomplete scoring skeleton BEFORE any holdout is read:
- position_days from held-pool tape coverage (never None); closed_overlapping only counts closes whose
  lifetime overlaps eligible tape;
- fill calibration is conservative + MONOTONE with a sample floor; reserve midpoint is never executable;
- acceptance pairs by immutable EPISODE ID and fails closed on length/identity/ordering mismatch
  (never a silent positional zip);
- a canonical JSON policy manifest + SHA-256 carrying every constant/grid/cutoff/fill-model-version/
  degenerate rule; previous-V5-SHA vs candidate-V5-SHA;
- rejected revisions are immutable + permanently rejected; a retry needs a NEW prereg + non-overlapping
  evidence;
- the first preregistered question (material LP after a prior partial -> CUT or suppress) declares both
  arms and freezes the selection on TRAINING before holdout;
- the floor reporter never opens holdout or mutates policy.
"""
import sqlite3

import pytest

from sniper.services import memecoin_v5_replay_gate as g


# --- acceptance pairs by episode id, fails closed (no silent zip truncation) -----------------------

def test_acceptance_pairs_by_episode_id_deterministically():
    base = {"epB": 12.0, "epA": 10.0, "epC": 11.0}
    cand = {"epC": 13.0, "epA": 12.0, "epB": 14.0}
    ids, b, c = g.pair_by_episode(base, cand)
    assert ids == ("epA", "epB", "epC")          # deterministic sorted-by-id order
    assert b == [10.0, 12.0, 11.0] and c == [12.0, 14.0, 13.0]


def test_acceptance_fails_closed_on_episode_identity_mismatch():
    with pytest.raises(g.EvidencePairingError):
        g.pair_by_episode({"epA": 1.0, "epB": 2.0}, {"epA": 1.0, "epZ": 2.0})   # id sets differ
    with pytest.raises(g.EvidencePairingError):
        g.pair_by_episode({"epA": 1.0, "epB": 2.0}, {"epA": 1.0})               # length mismatch
    with pytest.raises(g.EvidencePairingError):
        g.pair_by_episode({}, {})                                              # empty -> fail closed


def test_acceptance_verdict_uses_episode_keyed_inputs():
    fm = g.FillModel(impact_cap_pct=6.0, haircut_frac=0.05, calibrated=True,
                     slippage_curve=((100.0, 0.05),))
    base = {f"ep{i}": 10.0 for i in range(40)}
    cand = {f"ep{i}": 12.0 for i in range(40)}
    v = g.acceptance_verdict(base, cand, fill_model=fm, floor_metrics=g._FLOORS_OK_FIXTURE
                             if hasattr(g, "_FLOORS_OK_FIXTURE") else
                             {"position_days": 14, "closed_overlapping": 20, "decoded_lp_removals": 10},
                             catastrophic_baseline=0.1, catastrophic_candidate=0.08)
    assert v.n == 40 and v.arm_eligible is True
    # a mismatched candidate id set fails the verdict closed (not a truncated pairing)
    bad = g.acceptance_verdict(base, {**cand, "epX": 1.0}, fill_model=fm,
                               floor_metrics={"position_days": 14, "closed_overlapping": 20,
                                              "decoded_lp_removals": 10},
                               catastrophic_baseline=0.1, catastrophic_candidate=0.08)
    assert bad.arm_eligible is False and any("pairing" in r for r in bad.reasons)


# --- conservative + monotone fill calibration ------------------------------------------------------

def test_fill_calibration_is_monotone_nondecreasing_in_size():
    # raw closes are noisy/non-monotone; the fit curve must be conservative + monotone in notional
    closes = [{"notional": n, "realized_impact_frac": f} for n, f in
              [(100, 0.02), (200, 0.01), (300, 0.05), (400, 0.03), (500, 0.06)] * 5]
    fm = g.calibrate_fill_model(closes, impact_cap_pct=20.0, haircut_frac=0.1, min_closes=20)
    assert fm.calibrated is True
    impacts = [fm._impact_frac(n) for n in (100, 200, 300, 400, 500)]
    assert impacts == sorted(impacts)            # non-decreasing: a bigger bag never cheaper


# --- canonical policy manifest + SHA-256 -----------------------------------------------------------

def test_policy_manifest_is_canonical_and_hashes_all_fields():
    man = g.policy_manifest(baseline_policy_sha="v5base", candidate_policy_sha="v5cand",
                            evidence_cutoff="2026-07-25T00:00:00+00:00")
    for key in ("candidate_grid", "evidence_floors", "evidence_cutoff", "fill_model_version",
                "degenerate_rules", "baseline_policy_sha", "candidate_policy_sha", "policy_revision",
                "registered_parameters"):
        assert key in man
    sha1 = g.policy_sha256(man)
    sha2 = g.policy_sha256(g.policy_manifest(baseline_policy_sha="v5base", candidate_policy_sha="v5cand",
                                             evidence_cutoff="2026-07-25T00:00:00+00:00"))
    assert sha1 == sha2 and len(sha1) == 64      # canonical + deterministic
    # a changed constant changes the hash
    assert g.policy_sha256({**man, "evidence_cutoff": "2026-08-01"}) != sha1


def test_baseline_is_previous_v5_not_an_old_engine():
    man = g.policy_manifest(baseline_policy_sha="prev-v5", candidate_policy_sha="cand-v5",
                            evidence_cutoff="2026-07-25")
    assert man["baseline_policy_sha"] == "prev-v5"
    assert man["engine_version"] == "v5" and "v2" not in man.get("engine_version", "")


def _complete_manifest():
    from sniper.settings import memecoin as cfg
    manifest = g.policy_manifest(
        baseline_policy_sha="prev-v5-abc", candidate_policy_sha="candidate-v5-def",
        evidence_cutoff="2026-07-25T00:00:00+00:00",
        selected_parameters={"partial_lp_escalation": "suppress"},
        registered_parameters=g.registered_policy_parameters(cfg))
    assert "partial_lp_escalation_unregistered" not in manifest["degenerate_rules"]
    return manifest


def test_registered_policy_snapshot_captures_every_runtime_decision_input():
    from sniper.settings import memecoin as cfg
    params = g.registered_policy_parameters(cfg)
    assert set(params) >= {
        "band_anchor_trail_pct", "band_edge_duration_s", "partial_sizing",
        "unsellable_sweeps", "lp_severity_bounds", "route_gone_streak",
        "no_progress_window_s", "structure_persistence_window_s", "feature_freshness_s",
        "min_structure_points", "pivot_confirm_events",
    }


def test_policy_registry_persists_one_holdout_read_and_permanent_rejection(tmp_path):
    path = tmp_path / "registry.jsonl"
    reg = g.PolicyRegistry(path)
    sha = reg.preregister(
        _complete_manifest(), evidence_window=("2026-07-01", "2026-07-14"),
        registered_at="2026-07-25T00:00:00+00:00")
    assert reg.can_read_holdout(sha) is True
    reg.record_holdout(
        sha, verdict="REJECTED", recorded_at="2026-07-25T01:00:00+00:00",
        result={"paired_bootstrap_lb": -1.0})

    reopened = g.PolicyRegistry(path)
    assert reopened.can_read_holdout(sha) is False
    assert reopened.is_rejected(sha) is True
    with pytest.raises(g.RevisionImmutableError):
        reopened.record_holdout(
            sha, verdict="ACCEPTED", recorded_at="2026-07-26T00:00:00+00:00", result={})
    with pytest.raises(g.EvidenceWindowReuseError):
        reopened.preregister(
            {**_complete_manifest(), "candidate_policy_sha": "new-candidate"},
            evidence_window=("2026-07-10", "2026-07-20"),
            registered_at="2026-07-26T00:00:00+00:00")


def test_policy_registry_returns_only_an_accepted_immutable_revision(tmp_path):
    reg = g.PolicyRegistry(tmp_path / "registry.jsonl")
    manifest = _complete_manifest()
    sha = reg.preregister(
        manifest, evidence_window=("2026-07-01", "2026-07-14"),
        registered_at="2026-07-25T00:00:00+00:00")

    assert reg.accepted_manifest(sha) is None
    reg.record_holdout(
        sha, verdict="ACCEPTED", recorded_at="2026-07-25T01:00:00+00:00",
        result={"arm_eligible": True})

    assert reg.accepted_manifest(sha) == manifest
    assert reg.accepted_manifest("0" * 64) is None


def test_policy_registry_cannot_label_a_failed_gate_accepted(tmp_path):
    reg = g.PolicyRegistry(tmp_path / "registry.jsonl")
    sha = reg.preregister(
        _complete_manifest(), evidence_window=("2026-07-01", "2026-07-14"),
        registered_at="2026-07-25T00:00:00+00:00")
    with pytest.raises(g.InvalidPolicyRevision, match="arm_eligible"):
        reg.record_holdout(
            sha, verdict="ACCEPTED", recorded_at="2026-07-25T01:00:00+00:00",
            result={"arm_eligible": False})


def test_active_policy_requires_pointer_to_accepted_registry_revision(tmp_path):
    state = tmp_path / "state"
    data = state / "data"
    data.mkdir(parents=True)
    env = {"SNIPER_STATE_ROOT": str(state)}
    assert g.active_policy_revision(env=env) is None

    reg = g.PolicyRegistry(data / g.POLICY_REGISTRY_FILENAME)
    manifest = _complete_manifest()
    sha = reg.preregister(
        manifest, evidence_window=("2026-07-01", "2026-07-14"),
        registered_at="2026-07-25T00:00:00+00:00")
    (data / g.ACTIVE_POLICY_FILENAME).write_text(
        '{"policy_sha256":"' + sha + '"}\n', encoding="utf-8")
    assert g.active_policy_revision(env=env) is None

    reg.record_holdout(
        sha, verdict="ACCEPTED", recorded_at="2026-07-25T01:00:00+00:00",
        result={"arm_eligible": True})
    assert g.active_policy_revision(env=env) == (sha, manifest)


def test_active_policy_pointer_fails_closed_on_extra_or_malformed_fields(tmp_path):
    state = tmp_path / "state"
    data = state / "data"
    data.mkdir(parents=True)
    env = {"SNIPER_STATE_ROOT": str(state)}
    pointer = data / g.ACTIVE_POLICY_FILENAME
    for payload in ("not-json", '{}', '{"policy_sha256":"abc","enabled":true}'):
        pointer.write_text(payload, encoding="utf-8")
        assert g.active_policy_revision(env=env) is None


def test_policy_registry_refuses_placeholder_or_unselected_manifest(tmp_path):
    reg = g.PolicyRegistry(tmp_path / "registry.jsonl")
    placeholder = g.policy_manifest(
        baseline_policy_sha="PENDING_PREV_V5_REPLAY",
        candidate_policy_sha="candidate", evidence_cutoff="SEALED_NOT_SET")
    with pytest.raises(g.InvalidPolicyRevision):
        reg.preregister(placeholder, evidence_window=("a", "b"), registered_at="t")


# --- first preregistered question: partial-LP escalation, both arms, frozen on training ------------

def test_partial_lp_escalation_declares_both_arms():
    assert set(g.PARTIAL_LP_ESCALATION_ARMS) == {"escalate_to_cut", "suppress"}


def test_partial_lp_selection_is_frozen_on_training_before_holdout():
    # both arms scored on TRAINING; the winner is frozen and returned with its training evidence — the
    # function must never touch holdout (it takes only the training slice).
    train_scores = {"escalate_to_cut": 5.0, "suppress": 3.0}
    sel = g.select_partial_lp_arm(train_scores)
    assert sel["selected_arm"] == "escalate_to_cut" and sel["frozen"] is True
    assert sel["basis"] == "training"


# --- floor reporter: honest counts, never opens holdout, emits EVIDENCE_BLOCKED -------------------

def _ledger_and_tape():
    m = sqlite3.connect(":memory:"); m.row_factory = sqlite3.Row
    m.execute("CREATE TABLE memecoin_positions(id INTEGER, execution_mode TEXT, is_open INTEGER, "
              "network_id TEXT, mint TEXT, opened_at TEXT, closed_at TEXT, quote_route_target TEXT)")
    t = sqlite3.connect(":memory:"); t.row_factory = sqlite3.Row
    t.execute("CREATE TABLE pool_ticks(chain_id TEXT, pool TEXT, kind TEXT, block_no INTEGER, "
              "tx_sig TEXT, log_index INTEGER, observed_at TEXT)")
    t.execute("CREATE TABLE block_times(chain_id TEXT, block_no INTEGER, block_ts TEXT)")
    t.execute("CREATE TABLE pool_meta(chain_id TEXT, pool TEXT, token0 TEXT, token1 TEXT, "
              "quote_side INTEGER, token0_decimals INTEGER, token1_decimals INTEGER)")
    return m, t


def test_floor_status_reports_all_required_fields_and_blocks():
    m, t = _ledger_and_tape()
    status = g.chain_floor_status(m, t, "4663")
    for key in ("chain", "metrics", "floors_met", "reasons"):
        assert key in status
    for metric in ("position_days", "closed_overlapping", "decoded_lp_removals"):
        assert metric in status["metrics"]
    assert status["floors_met"] is False          # empty tape -> blocked, never a fabricated pass


def test_floor_report_emits_code_complete_evidence_blocked_and_never_reads_holdout():
    m, t = _ledger_and_tape()
    report = g.floor_report(m, t, chains=("solana", "4663"))
    assert report["verdict"] == "CODE_COMPLETE_EVIDENCE_BLOCKED"
    assert report["holdout_read"] is False and report["policy_mutated"] is False
    assert set(report["chains"]) == {"solana", "4663"}


def test_floor_report_reads_each_chain_from_its_own_tape_connection():
    m, evm = _ledger_and_tape()
    _unused, sol = _ledger_and_tape()
    sol.execute("INSERT INTO pool_ticks VALUES('solana','SOLPOOL','sol_burn',1,'s',0,'t')")
    evm.execute("INSERT INTO pool_ticks VALUES('4663','EVMPOOL','v3_burn',2,'e',0,'t')")

    report = g.floor_report(
        m, {"solana": sol, "4663": evm}, chains=("solana", "4663"),
        calibration_samples={"solana": 0, "4663": 20})

    assert report["chains"]["solana"]["metrics"]["decoded_lp_removals"] == 1
    assert report["chains"]["4663"]["metrics"]["decoded_lp_removals"] == 1
    assert "fill_model_uncalibrated" in report["chains"]["solana"]["reasons"]
    assert "fill_model_uncalibrated" not in report["chains"]["4663"]["reasons"]


def test_position_days_include_open_positions_but_closed_overlap_does_not():
    m, t = _ledger_and_tape()
    t.execute("INSERT INTO pool_meta VALUES('4663','pool','quote','0xmint',0,18,18)")
    t.executemany("INSERT INTO pool_ticks VALUES('4663','pool','v3_swap',?,'s'||?,0,'t')",
                  [(1, 1), (2, 2)])
    t.executemany("INSERT INTO block_times VALUES('4663',?,?)", [
        (1, "2026-07-01T00:00:00+00:00"), (2, "2026-07-03T00:00:00+00:00")])
    m.execute("INSERT INTO memecoin_positions VALUES(1,'real',0,'4663','0xmint',"
              "'2026-07-01T00:00:00+00:00','2026-07-02T00:00:00+00:00',NULL)")
    m.execute("INSERT INTO memecoin_positions VALUES(2,'real',1,'4663','0xmint',"
              "'2026-07-02T00:00:00+00:00',NULL,NULL)")

    status = g.chain_floor_status(m, t, "4663", fill_model_calibrated=True)

    assert status["metrics"]["closed_overlapping"] == 1
    assert status["metrics"]["position_days"] == 2.0


def test_real_close_samples_pair_exact_quote_to_confirmed_fill_per_chain():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE memecoin_positions(id INTEGER, execution_mode TEXT, network_id TEXT, "
              "exit_reason TEXT)")
    c.execute("CREATE TABLE memecoin_fills(id INTEGER, position_id INTEGER, kind TEXT, notional REAL, "
              "fraction REAL, created_at TEXT, tx_sig TEXT)")
    c.execute("CREATE TABLE memecoin_exit_trigger_audit(position_id INTEGER, reason TEXT, "
              "preview_usdc REAL, allowed INTEGER, checked_at TEXT)")
    c.executemany("INSERT INTO memecoin_positions VALUES(?,?,?,?)", [
        (1, "real", "4663", "real_trail"), (2, "real", "4663", "real_trail"),
        (3, "real", "solana", "real_trail")])
    c.executemany("INSERT INTO memecoin_fills VALUES(?,?,?,?,?,?,?)", [
        (10, 1, "bracket_exit", 90.0, 1.0, "t1", "sig1"),
        (11, 2, "derisk", 36.0, 0.4, "t2", "sig2"),
        (12, 3, "bracket_exit", 80.0, 1.0, "t3", "sig3"),
        (13, 1, "bracket_exit", 1.0, 1.0, "t4", None)])
    c.executemany("INSERT INTO memecoin_exit_trigger_audit VALUES(?,?,?,?,?)", [
        (1, "real_trail", 100.0, 1, "t1"),
        (2, "derisk", 100.0, 1, "t2"),
        (3, "real_trail", 100.0, 1, "t3"),
        (1, "real_trail", 1.0, 1, "wrong-time")])

    samples = g.real_close_samples(c, "4663")

    assert [s["fill_id"] for s in samples] == [10, 11]
    assert samples[0]["notional"] == 100.0
    assert samples[0]["realized_impact_frac"] == pytest.approx(0.10)
    assert samples[1]["notional"] == 40.0
    assert samples[1]["realized_impact_frac"] == pytest.approx(0.10)
