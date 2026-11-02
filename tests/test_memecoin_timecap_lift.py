"""The 4h timecap lift and the frozen silence threshold.

Two properties carry this change, and both fail silently if they break:

  - A LIFTED TIMECAP IS A DIFFERENT POLICY, so it must get a different `policy_id`. Writing 14400
    under an existing id puts two contents under one identity and every historical comparison keyed
    on policy_id silently mixes cohorts.
  - THE NEW FIELD MUST NOT BREAK ROLLBACK. `_validated_policy_data` rejects unknown keys and
    requires an exact `policy_version`, so a policy-object change would make the pinned prior
    release raise on contracts written by this one — leaving open funded positions whose exit sweep
    cannot read its own rails, across all three funded readers.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_funded_policy as fp
from sniper.services.memecoin_market_state import EntryClassification
from sniper.settings import memecoin as cfg


@pytest.fixture
def lift_on(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_TIMECAP_LIFT_ENABLED", True)


@pytest.fixture
def lift_off(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_TIMECAP_LIFT_ENABLED", False)


def _follow(**kw):
    return fp._policy(policy_id="follow-30-50-v1", coin_band="mid", tp=25.0, derisk=0.0,
                      trail=0.0, stop=35.0, trail_arm=0.0, trail_requires_derisk=False,
                      mirror_on_sell=False, **kw)


# --------------------------------------------------------------- default-inert

def test_with_the_lift_off_every_family_is_byte_identical(lift_off):
    p = _follow()
    assert p.policy_id == "follow-30-50-v1"
    assert p.max_hold_s == float(cfg.MEMECOIN_REAL_MAX_HOLD_S)


@pytest.mark.parametrize("old,new", sorted(fp.TIMECAP_LIFT_POLICY_IDS.items()))
def test_every_lifted_family_gets_a_new_id_and_the_4h_cap(lift_on, old, new):
    p = fp._policy(policy_id=old, coin_band="mid", tp=25.0, derisk=0.0, trail=0.0, stop=35.0,
                   trail_arm=0.0, trail_requires_derisk=False, mirror_on_sell=False)
    assert p.policy_id == new
    assert p.max_hold_s == fp.TIMECAP_LIFT_MAX_HOLD_S == 14400.0


def test_no_family_maps_onto_an_id_another_family_already_uses():
    """A collision would put two DIFFERENT policies under one name — the failure this rename
    exists to prevent, reintroduced by the rename itself."""
    olds, news = set(fp.TIMECAP_LIFT_POLICY_IDS), set(fp.TIMECAP_LIFT_POLICY_IDS.values())
    assert len(news) == len(fp.TIMECAP_LIFT_POLICY_IDS), "two families map to one new id"
    assert not (olds & news), "a new id collides with a shipped one"


def test_the_lift_changes_only_the_cap_and_never_the_shape(lift_on, lift_off):
    """TP/stop/trail/derisk belong to the ladder review, not to this change."""
    import contextlib
    off = _follow()
    with contextlib.ExitStack():
        pass
    # rebuild with the lift on
    import sniper.settings.memecoin as c
    c.MEMECOIN_TIMECAP_LIFT_ENABLED = True
    try:
        on = _follow()
    finally:
        c.MEMECOIN_TIMECAP_LIFT_ENABLED = False
    for field in ("tp_pct", "stop_pct", "trail_pct", "trail_arm_pct", "derisk_pct",
                  "derisk_mode", "derisk_fraction", "catastrophe_pct", "unquotable_exit_s",
                  "stop_debounce_sweeps", "mirror_on_sell", "coin_band", "policy_version"):
        assert getattr(on, field) == getattr(off, field), f"{field} moved"
    assert (on.policy_id, on.max_hold_s) != (off.policy_id, off.max_hold_s)


def test_the_engine_envelope_is_not_lifted(lift_on):
    """The v5 engine owns its own sell decision under the v3 prereg; its backstop is not ours."""
    assert fp.ENGINE_ENVELOPE_POLICY_ID not in fp.TIMECAP_LIFT_POLICY_IDS


# --------------------------------------------------------------- rollback safety

def test_a_lifted_policy_still_parses_under_the_strict_validator(lift_on):
    p = _follow()
    parsed = fp._validated_policy_data(p.to_dict())
    assert parsed.policy_id == "follow-30-50-v2" and parsed.max_hold_s == 14400.0
    assert parsed.policy_version == fp.FUNDED_POLICY_VERSION, "the SCHEMA version must not move"


def test_the_policy_schema_version_is_not_used_to_mark_a_content_change():
    """`FUNDED_POLICY_VERSION` is one module constant stamped on EVERY family and checked for exact
    equality on read. Bumping it to mark new content is one field carrying two meanings, and the
    pinned prior release rejects the result. Identity lives in `policy_id`."""
    assert fp.FUNDED_POLICY_VERSION == 1


PINNED = pathlib.Path(".runtime/memecoin/releases")


@pytest.mark.skipif(not PINNED.is_dir(), reason="no pinned releases on this host")
def test_the_pinned_release_can_still_parse_what_this_build_writes(lift_on):
    """THE PROPERTY THE WHOLE DESIGN IS SHAPED BY. If this fails, a rollback leaves open funded
    positions whose exit sweep cannot read its own rails."""
    releases = sorted(d for d in PINNED.iterdir() if (d / "sniper").is_dir())
    if not releases:
        pytest.skip("no usable pinned release")
    policy = json.dumps(_follow().to_dict())
    script = (
        "import json,sys\n"
        "from sniper.services.memecoin_funded_policy import _validated_policy_data\n"
        "_validated_policy_data(json.loads(sys.stdin.read()))\n"
        "print('parsed')\n")
    for release in releases[-1:]:
        out = subprocess.run([sys.executable, "-c", script], input=policy,
                             capture_output=True, text=True, cwd=str(release))
        assert out.returncode == 0, f"{release.name} cannot parse a lifted contract: {out.stderr}"


# --------------------------------------------------------------- the frozen threshold

def _classification():
    return EntryClassification(
        mint="MINT", network_id="solana", coin_band="mid", lifecycle="young",
        liquidity_class="deep", momentum_phase="unknown", market_regime="normal",
        data_quality="partial", risk_scale=1.0, reason_codes=[], facts={},
        signal_source="global_feed", decision_at="2026-08-26T18:00:00+00:00",
        classification_version=1)


def _contract(**kw):
    return fp.build_funded_contract(
        classification=_classification(), base_clip_usd=40.0, approved_clip_usd=40.0,
        policy=_follow(), origin_event_id="e1", **kw)


def _position(contract):
    canonical, digest = m.canonical_funded_json(contract)
    return {"mint": "MINT", "network_id": "solana", "funded_contract_version": 1,
            "funded_contract_hash": digest, "funded_contract_json": canonical}


def test_a_new_contract_freezes_the_threshold_in_source_not_in_policy(lift_on):
    c = _contract()
    assert c["source"]["voldeath_exit_s"] == float(cfg.MEMECOIN_VOLDEATH_EXIT_S)
    assert "voldeath_exit_s" not in c["policy"], "in policy it would break the pinned reader"
    fp._validated_policy_data(c["policy"])       # still strictly valid


def test_with_the_lift_off_no_contract_carries_the_key(lift_off):
    assert "voldeath_exit_s" not in _contract()["source"]


def test_the_frozen_threshold_reads_back_authenticated(lift_on):
    assert fp.frozen_voldeath_exit_s(_position(_contract(voldeath_exit_s=45.0))) == 45.0


def test_a_contract_without_the_key_reads_legacy_never_malformed(lift_off):
    """Absent must NEVER be an error: the deploy reconciliation gate has to stay green over every
    existing row."""
    pos = _position(_contract())
    assert fp.frozen_voldeath_exit_s(pos) is None
    assert fp.frozen_policy_from_position(pos) is not None, "the strict parse still succeeds"


def test_a_legacy_position_with_no_contract_at_all_reads_none():
    assert fp.frozen_voldeath_exit_s({"mint": "M", "network_id": "solana"}) is None


def test_tampered_bytes_do_not_yield_a_threshold(lift_on):
    pos = _position(_contract(voldeath_exit_s=45.0))
    pos["funded_contract_json"] = pos["funded_contract_json"].replace('"voldeath_exit_s":45.0',
                                                                      '"voldeath_exit_s":5.0')
    assert fp.frozen_voldeath_exit_s(pos) is None, "an unauthenticated value must not be honoured"


@pytest.mark.parametrize("bad", [0, -1, "60", True, None])
def test_an_unusable_frozen_value_falls_back_rather_than_firing_early(lift_on, bad):
    c = _contract()
    c["source"]["voldeath_exit_s"] = bad
    assert fp.frozen_voldeath_exit_s(_position(c)) is None


def test_only_the_threshold_is_frozen_never_whether_the_rail_runs(lift_on):
    """An operator must be able to stop a rail on open positions. Freezing `armed` would make a
    position opened under the rail impossible to disarm."""
    assert "voldeath_armed" not in json.dumps(_contract())


# --------------------------------------------------------------- precedence

def test_the_cohort_cutoff_is_checked_before_the_frozen_value():
    """Alpha, 2026-08-26: a contract carrying the frozen threshold whose position predates the
    armed cutoff must still be cohort-excluded. Cutoff first, frozen value second."""
    from sniper.services.memecoin_volume_death import armed_exit_decision

    class Poisoned:
        def execute(self, *a, **k):
            raise AssertionError("cohort exclusion must return before any DB read")

    fire, silence, why = armed_exit_decision(
        Poisoned(), {"id": 1, "mint": "M", "network_id": "solana",
                     "opened_at": "2026-08-26T13:00:00+00:00"},
        now_iso="2026-08-26T18:00:00+00:00", held_s=9999.0, exit_s=45.0,
        min_opened_at="2026-08-26T14:05:51+00:00")
    assert (fire, why) == (False, "cohort_excluded")


def test_a_non_finite_threshold_cannot_reach_a_contract_at_all(lift_on):
    """Defence in depth: the canonical writer refuses non-finite numbers, so NaN never gets as far
    as the reader. Pinned so a looser writer would fail here rather than in production."""
    c = _contract()
    c["source"]["voldeath_exit_s"] = float("nan")
    with pytest.raises(ValueError):
        m.canonical_funded_json(c)


def test_the_runner_prefers_the_frozen_threshold_over_the_env(monkeypatch):
    """Behaviour, not source text: the frozen threshold wins over a different env value, and the env
    value is only the fallback for a legacy contract. The runner's armed volume-death call takes its
    exit_s from voldeath_exit_s (pinned by name, the one wiring check left)."""
    monkeypatch.setattr(cfg, "MEMECOIN_TIMECAP_LIFT_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_VOLDEATH_EXIT_S", 90.0)
    frozen_pos = _position(_contract())                   # freezes 90 s into the contract
    monkeypatch.setattr(cfg, "MEMECOIN_VOLDEATH_EXIT_S", 30.0)   # the env moves after the open
    assert fp.voldeath_exit_s(frozen_pos, cfg.MEMECOIN_VOLDEATH_EXIT_S) == 90.0
    monkeypatch.setattr(cfg, "MEMECOIN_TIMECAP_LIFT_ENABLED", False)
    legacy_pos = _position(_contract())                   # a pre-lift contract carries none
    assert fp.voldeath_exit_s(legacy_pos, 30.0) == 30.0
    assert fp.voldeath_exit_s({"mint": "M"}, 45.0) == 45.0                   # no contract at all
    body = pathlib.Path(fp.__file__).parent.joinpath("memecoin_real_runner.py").read_text()
    block = body[body.index("ARMED VOLUME-DEATH (2026-08-26, operator)"):][:2400]
    assert 'exit_s=voldeath_exit_s(pos, getattr(cfg, "MEMECOIN_VOLDEATH_EXIT_S", 60.0))' in block


# --------------------------------------------------------------- mixed-ledger reconciliation

def _insert_position(conn, contract, *, opened_at, is_open=1, attempt_id=None):
    canonical, digest = m.canonical_funded_json(contract)
    conn.execute(
        "INSERT INTO memecoin_positions (mint, network_id, execution_mode, is_open, opened_at, "
        "entry_price, attempt_id, funded_contract_version, funded_contract_hash, "
        "funded_contract_json) VALUES ('MINT','solana','real',?,?,1.0,?,1,?,?)",
        (is_open, opened_at, attempt_id, digest, canonical))


def test_a_ledger_mixing_lifted_and_unlifted_contracts_reconciles_green(monkeypatch):
    """THE DEPLOY GATE MUST STAY GREEN OVER EVERY EXISTING ROW. A contract that predates the lift
    carries neither the new policy_id nor the frozen threshold, and absent must read as legacy
    rather than malformed — otherwise activation blocks on the history it is meant to leave alone.
    """
    import sqlite3
    from sniper.services.memecoin_funded_reconcile import (
        reconcile_severity, validate_funded_contract_reconciliation)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m._ensure_memecoin_tables_in_transaction(conn)
    cutover = "2026-08-01T00:00:00+00:00"

    def _attempt(attempt_id, contract, started):
        canonical, digest = m.canonical_funded_json(contract)
        conn.execute(
            "INSERT INTO memecoin_entry_attempts (attempt_id, mint, network_id, "
            "decision_started_at, funded_contract_version, funded_contract_hash, "
            "funded_contract_json) VALUES (?, 'MINT', 'solana', ?, 1, ?, ?)",
            (attempt_id, started, digest, canonical))

    monkeypatch.setattr(cfg, "MEMECOIN_TIMECAP_LIFT_ENABLED", False)
    legacy = _contract()
    _attempt("a-old", legacy, "2026-08-20T12:00:00+00:00")
    _insert_position(conn, legacy, opened_at="2026-08-20T12:00:00+00:00", is_open=0,
                     attempt_id="a-old")
    monkeypatch.setattr(cfg, "MEMECOIN_TIMECAP_LIFT_ENABLED", True)
    lifted = _contract()
    _attempt("a-new", lifted, "2026-08-26T18:00:00+00:00")
    _insert_position(conn, lifted, opened_at="2026-08-26T18:00:00+00:00", is_open=1,
                     attempt_id="a-new")

    blocking, warnings = reconcile_severity(conn, cutover_at=cutover)
    assert blocking == [], f"a mixed ledger must not block a deploy: {blocking}"
    assert validate_funded_contract_reconciliation(conn, cutover_at=cutover) == []


def test_both_cohorts_parse_their_own_rails_after_the_lift(monkeypatch):
    """The point of freezing: an open pre-lift position keeps 780s while a post-lift one gets
    14400, with the env now saying 'lifted' for both."""
    monkeypatch.setattr(cfg, "MEMECOIN_TIMECAP_LIFT_ENABLED", False)
    old = _position(_contract())
    monkeypatch.setattr(cfg, "MEMECOIN_TIMECAP_LIFT_ENABLED", True)
    new = _position(_contract())

    old_policy = fp.frozen_policy_from_position(old)
    new_policy = fp.frozen_policy_from_position(new)
    assert old_policy.policy_id == "follow-30-50-v1"
    assert old_policy.max_hold_s == float(cfg.MEMECOIN_REAL_MAX_HOLD_S)
    assert new_policy.policy_id == "follow-30-50-v2" and new_policy.max_hold_s == 14400.0
    assert fp.frozen_voldeath_exit_s(old) is None
    assert fp.frozen_voldeath_exit_s(new) == float(cfg.MEMECOIN_VOLDEATH_EXIT_S)


# --------------------------------------------------------------- the activation script

DEPLOY = pathlib.Path("scripts/deploy_memecoin_runtime.sh")


def test_the_activation_flag_is_wired_and_cannot_share_another_activation():
    body = DEPLOY.read_text()
    assert "--activate-timecap-lift" in body
    assert "timecap-lift activation cannot share another activation" in body, \
        "one restart must not carry two policy changes, or neither cohort can be attributed"


def test_the_activation_stops_the_runner_before_it_rechecks_pending_reservations():
    """Order is the safety property: a reservation can be created between a check and a stop."""
    body = DEPLOY.read_text()
    block = body[body.index('if [ "$ACTIVATE_TIMECAP_LIFT" -eq 1 ]; then'):]
    block = block[:block.index("\nfi\n")]
    assert block.index("ROLLOVER_OLD_RUNNER_STOPPED=1") < block.index("pending_reservations")
    assert 'trap pre_switch_rollover_rollback EXIT' in block, "no rollback path armed"
    assert block.index("trap pre_switch_rollover_rollback") < block.index("kill \"$OLD_PID\"")


def test_the_activation_revalidates_the_release_after_writing_the_env():
    body = DEPLOY.read_text()
    block = body[body.index('if [ "$ACTIVATE_TIMECAP_LIFT" -eq 1 ]; then'):]
    block = block[:block.index("\nfi\n")]
    assert block.index("ROLLOVER_ENV_CHANGED=1") < block.index("verify_candidate_release")
    assert "validate_funded_contracts" in block


def test_the_activation_writes_no_second_cutoff():
    """The frozen contract IS the cohort boundary. A parallel cutoff would be a second copy of it,
    free to drift. The armed-rail cutoff must also be left exactly as it is."""
    body = DEPLOY.read_text()
    block = body[body.index('if [ "$ACTIVATE_TIMECAP_LIFT" -eq 1 ]; then'):]
    block = block[:block.index("\nfi\n")]
    assert "MEMECOIN_VOLDEATH_ARM_MIN_OPENED_AT" not in block
    assert "MIN_RESERVED_AT" not in block
    assert block.count("MEMECOIN_TIMECAP_LIFT_ENABLED=1") == 2   # the replace and the append


# ---------------------------------------- the out_of_scope band (2026-08-26, live incident)

def test_an_out_of_scope_contract_parses_and_yields_its_rails():
    """REGRESSION, found live: coin_band='out_of_scope' is a value the WRITER legitimately writes
    (big-mcap coins admitted under follow-30-50 per doctrine), but the validator rejected it, so
    frozen_policy_from_position raised and the position rode with NO rails — no TP, no stop, no
    timecap. All three closed out_of_scope positions in history closed `manual`: the operator was
    the stop loss. Position 44703 sat 18 minutes past its frozen 780s cap before the operator
    reported "TP and SL aren't working"."""
    import json
    from sniper.services import memecoin_funded_policy as fp
    policy = fp._policy(
        policy_id="follow-30-50-v1", coin_band="out_of_scope",
        tp=25.0, derisk=0.0, trail=0.0, stop=50.0, trail_arm=0.0,
        trail_requires_derisk=False)
    assert policy.coin_band == "out_of_scope"
    data = json.loads(json.dumps(policy.to_dict()))
    parsed = fp._validated_policy_data(data)
    assert parsed.tp_pct == 25.0 and parsed.stop_pct == 50.0 and parsed.max_hold_s > 0


# ------------------------------------------------- the gate must run the sweep's own parse

def test_the_deploy_gate_catches_a_contract_the_exit_sweep_cannot_parse(monkeypatch):
    """REGRESSION 2026-08-26 (position 44703). Byte integrity and semantic parseability are
    DIFFERENT PROPERTIES. Four `out_of_scope` contracts were authentic, consistent across every
    table, and unparseable by `frozen_policy_from_position` — so the sweep logged and continued and
    those positions rode with NO take-profit, NO stop and NO timecap, past both gates. All four
    closed `manual`: the operator was the stop loss.

    Simulated here by narrowing the accepted band set back to what it was, which is exactly the
    shape of the original defect."""
    import sqlite3
    from sniper.services import memecoin_funded_reconcile as rec

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m._ensure_memecoin_tables_in_transaction(conn)
    cutover = "2026-08-01T00:00:00+00:00"
    contract = _contract()
    canonical, digest = m.canonical_funded_json(contract)
    conn.execute(
        "INSERT INTO memecoin_entry_attempts (attempt_id, mint, network_id, decision_started_at, "
        "funded_contract_version, funded_contract_hash, funded_contract_json) "
        "VALUES ('a1','MINT','solana','2026-08-26T18:00:00+00:00',1,?,?)", (digest, canonical))
    _insert_position(conn, contract, opened_at="2026-08-26T18:00:00+00:00", is_open=1,
                     attempt_id="a1")

    assert rec.reconcile_severity(
        conn, cutover_at=cutover, policy_parse=fp.frozen_policy_from_position)[0] == [], \
        "a healthy ledger must pass the semantic check too"

    def unparseable(pos):
        raise ValueError("funded contract policy coin band is invalid")

    blocking, _warnings = rec.reconcile_severity(
        conn, cutover_at=cutover, policy_parse=unparseable)
    assert any("exit sweep cannot parse" in b for b in blocking), \
        "an OPEN position the sweep cannot read must BLOCK, not sail through"

    # and the check is OPT-IN, so a dependency-light caller keeps today's byte-only behaviour
    assert rec.reconcile_severity(conn, cutover_at=cutover)[0] == []


def test_an_unparseable_contract_on_a_CLOSED_position_is_debt_not_a_block(monkeypatch):
    """Severity split held (2026-07-21): a closed row guards no live risk, and blocking on it
    bricked armed startup for 26 minutes once already."""
    import sqlite3
    from sniper.services import memecoin_funded_reconcile as rec

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m._ensure_memecoin_tables_in_transaction(conn)
    contract = _contract()
    canonical, digest = m.canonical_funded_json(contract)
    conn.execute(
        "INSERT INTO memecoin_entry_attempts (attempt_id, mint, network_id, decision_started_at, "
        "funded_contract_version, funded_contract_hash, funded_contract_json) "
        "VALUES ('a1','MINT','solana','2026-08-26T18:00:00+00:00',1,?,?)", (digest, canonical))
    _insert_position(conn, contract, opened_at="2026-08-26T18:00:00+00:00", is_open=0,
                     attempt_id="a1")

    def unparseable(pos):
        raise ValueError("coin band is invalid")

    blocking, warnings = rec.reconcile_severity(
        conn, cutover_at="2026-08-01T00:00:00+00:00", policy_parse=unparseable)
    assert blocking == [], "a closed row must not block a restart"
    assert any("exit sweep cannot parse" in w for w in warnings), "but it is still audit debt"


def test_out_of_scope_is_accepted_because_the_writer_writes_it():
    """The validator's job is the WRITER'S ACTUAL DOMAIN, not the subset that feels canonical.
    Live contracts carry exactly five bands; the reader must accept all five."""
    for band in ("nano", "micro", "small", "mid", "out_of_scope"):
        policy = fp._follow3050_policy(coin_band=band)
        assert fp._validated_policy_data(policy.to_dict()).coin_band == band


def test_both_money_gates_pass_the_sweeps_own_parse():
    """The injection only helps if the callers that decide money actually use it. Armed startup
    guards live risk continuously; the deploy validator is the attended gate."""
    runner = pathlib.Path("scripts/memecoin_runner_loop.py").read_text()
    assert "policy_parse=frozen_policy_from_position" in runner, "armed startup must pass it"
    validator = pathlib.Path("scripts/validate_memecoin_funded_contracts.py").read_text()
    assert "policy_parse=frozen_policy_from_position" in validator


def test_the_validator_says_so_when_it_cannot_run_the_parse():
    """A gate that silently drops its most important check is the failure class this closes.
    Unreachable in a real release — which always carries the policy stack — so it must be loud if
    it ever fires."""
    body = pathlib.Path("scripts/validate_memecoin_funded_contracts.py").read_text()
    assert "the exit-sweep parse did NOT run" in body
    assert "file=sys.stderr" in body[body.index("policy stack unavailable"):][:400]


# ------------------------------------------------- the rename must not change any OTHER behaviour

def test_copy_arm_suppression_survives_the_rename():
    """REGRESSION 2026-08-26, found reviewing an unrelated change; the rename was mine.
    `memecoin_risk.py` hard-coded ("tp-only-v1", "follow-30-50-v1") to decide which policies own
    their own rails. The lift renames both, so the tuple would have stopped matching and a
    mirror-on-sell copy arm would have silently replaced the exact rails that check protects — the
    behaviour its own comment says it exists to avoid. A version rename must change the NAME and
    nothing else."""
    for old, new in fp.TIMECAP_LIFT_POLICY_IDS.items():
        assert fp.owns_its_rails(old) == fp.owns_its_rails(new), \
            f"{old} -> {new} changed whether a copy arm may override it"


def test_self_railed_families_are_matched_by_family_not_by_version():
    assert fp.owns_its_rails("follow-30-50-v1") and fp.owns_its_rails("follow-30-50-v99")
    assert fp.owns_its_rails("tp-only-v2") and fp.owns_its_rails(fp.ENGINE_ENVELOPE_POLICY_ID)
    assert not fp.owns_its_rails("funded-nano-v3"), "band policies may take a copy arm"
    assert not fp.owns_its_rails("") and not fp.owns_its_rails(None)


def test_no_versioned_policy_id_is_hard_coded_outside_the_rename_map():
    """The defect class: an identity string pinned somewhere a rename cannot reach. The map itself
    and the constructors are the only legitimate homes; a research constant may pin an era."""
    import subprocess
    allowed = {
        "sniper/services/memecoin_funded_policy.py",     # the map and the constructors
        "sniper/analysis/memecoin_exit_rail_replay.py",  # CURRENT_ERA pins the era it studied
    }
    hits = subprocess.run(
        ["grep", "-rn", "-E", r'"(follow-30-50|tp-only|funded-(nano|micro|small|mid))-v[0-9]+"',
         "--include=*.py", "sniper/", "scripts/"],
        capture_output=True, text=True).stdout.strip().splitlines()
    def _is_comment(hit):
        return hit.split(":", 2)[-1].lstrip().startswith("#")

    stray = [h for h in hits
             if h.split(":", 1)[0] not in allowed and not _is_comment(h)]
    assert not stray, "a versioned policy id is pinned where the rename cannot reach it:\n" + \
        "\n".join(stray)
