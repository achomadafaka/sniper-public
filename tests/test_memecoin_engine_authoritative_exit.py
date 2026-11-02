"""Exit-engine-v5 funded authority: envelope policy selection + real-runner adapter.

Flag-gated OFF (MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS default ""); the whole existing memecoin suite
must stay byte-identical with the flag off. These tests turn it on explicitly and verify: the
envelope policy (tp/derisk/trail/stop zeroed, catastrophe/unquotable/timecap/mirror unaffected
except mirror off), the contract records engine_authoritative, and the real_exit_sweep adapter maps
CUT/DERISK/TAKE_PROFIT onto the existing real_trail/derisk executable reasons — never a new route,
never widening a guard, DERISK firing at most once, and the N=5 canary bound.
"""
import json
import os
import sqlite3

import pytest

from sniper.analysis.exit_state_types import MarketState, Provenance, StateLabel
from sniper.persistence import db_exit_state as x
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_funded_policy as funded_policy
from sniper.services import memecoin_v5_replay_gate as gate
from sniper.services import memecoin_exit_market_state as mkstate
from sniper.services import memecoin_real_runner as R
from sniper.services.memecoin_exit_brain import (
    CUT, DEFER, DERISK, ENGINE_VERSION_V5, RIDE, TAKE_PROFIT,
    CandidateDecision,
)
from sniper.services.memecoin_funded_policy import (
    ENGINE_ENVELOPE_POLICY_ID,
    build_funded_contract,
    canonical_funded_contract,
    resolve_funded_policy,
)
from sniper.services.memecoin_market_state import EntryClassification
from sniper.settings import memecoin as cfg


def _accepted_policy_revision():
    manifest = gate.policy_manifest(
        baseline_policy_sha="previous-v5", candidate_policy_sha="current-v5",
        evidence_cutoff="2026-07-25T00:00:00+00:00",
        selected_parameters={"partial_lp_escalation": "suppress"},
        registered_parameters=gate.registered_policy_parameters(cfg))
    return gate.policy_sha256(manifest), manifest


# --- fixtures / helpers -----------------------------------------------------------------------

def _classification(*, network_id="solana", mint="MINT", coin_band="small"):
    return EntryClassification(
        classification_version=1, network_id=network_id, mint=mint, signal_source="global_feed",
        decision_at="2026-07-24T00:00:00+00:00", coin_band=coin_band, lifecycle="young",
        liquidity_class="balanced", momentum_phase="climbing", market_regime="hot_narrow",
        data_quality="partial", risk_scale=1.0, reason_codes=(), facts={},
    )


def _engine_contract_fields(*, network_id="solana", mint="MINT", coin_band="small"):
    policy = resolve_funded_policy(coin_band=coin_band, network_id=network_id)
    assert policy.policy_id == ENGINE_ENVELOPE_POLICY_ID
    contract = build_funded_contract(
        classification=_classification(network_id=network_id, mint=mint, coin_band=coin_band),
        base_clip_usd=10.0, approved_clip_usd=10.0, policy=policy, origin_event_id=None,
    )
    payload, digest = canonical_funded_contract(contract)
    return {"funded_contract_version": 1, "funded_contract_hash": digest,
            "funded_contract_json": payload}


def _c():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open_engine_position(c, mint, *, entry=1.0, opened="2026-07-09T00:00:00Z", uid="u1",
                          network_id="solana", coin_band="small"):
    pid = m.open_position(
        c, source_user_id=uid, source_handle="h", mint=mint, ticker="T", entry_price=entry,
        entry_notional=10.0, entry_fill_source="chain", entry_lag_s=-1.0, their_size_usd=0.0,
        opened_at=opened, strategy="alpha_b", execution_mode="real", network_id=network_id,
        **_engine_contract_fields(network_id=network_id, mint=mint, coin_band=coin_band),
    )
    m.insert_fill(c, position_id=pid, kind="entry", price=entry, qty=10.0, notional=10.0 * entry,
                  fill_source="chain", fraction=1.0, created_at=opened)
    c.commit()
    return pid


def _exit_state_factory(tmp_path, name="exit_state.db"):
    """Own-DB split (P0-3, 2026-07-24): the engine reads market state from its OWN exit-state file,
    never the funded runner's shared ticks DB."""
    path = str(tmp_path / name)

    def _connect():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        x.ensure_exit_state_tables(c)
        return c
    return _connect


_TRUSTED = {f: Provenance("pool_ticks", quality="trusted") for f in ("price", "flow", "vol", "depth")}
_REQ = ("price", "flow", "vol", "depth")


def _write_state(connect_fn, position_id, mint, *, chain_id="solana",
                 computed_at="2026-07-09T00:05:00+00:00", engine_version=None, feature_version=None,
                 **kw):
    """`mint`/`chain_id` must match the REAL position's identity (P0-1: the engine refuses a state
    whose identity doesn't match the position it's about to act on) and `engine_version`/
    `feature_version` default to the CURRENT feature-computation code's versions (P0-1: a stale
    version also falls back to envelope-only) — override either only to exercise that specific
    guard."""
    base = dict(chain_id=chain_id, mint=mint, pool="p", position_id=position_id,
                engine_version=engine_version if engine_version is not None else mkstate.ENGINE_VERSION,
                feature_version=feature_version if feature_version is not None else mkstate.FEATURE_VERSION,
                state=StateLabel.TRENDING, required_families=_REQ, provenance=_TRUSTED,
                computed_time=computed_at)   # mirrors compute_state(), which always sets this too
    base.update(kw)
    state = MarketState(**base)
    c = connect_fn()
    x.write_market_state(c, state, computed_at=computed_at)
    c.close()


@pytest.fixture(autouse=True)
def _engine_on(monkeypatch, tmp_path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / ".env").write_text("MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS=solana\n")
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(state_root))
    monkeypatch.setattr(funded_policy, "active_policy_revision", _accepted_policy_revision)
    monkeypatch.setattr(cfg, "MEMECOIN_ENGINE_MAX_POSITIONS", 5)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)


# --- policy / contract selection ----------------------------------------------------------------

def test_resolve_funded_policy_selects_engine_envelope_only_on_listed_chains():
    on = resolve_funded_policy(coin_band="small", network_id="solana")
    off = resolve_funded_policy(coin_band="small", network_id="4663")
    assert on.policy_id == ENGINE_ENVELOPE_POLICY_ID
    assert off.policy_id != ENGINE_ENVELOPE_POLICY_ID
    assert (on.tp_pct, on.derisk_pct, on.trail_pct, on.stop_pct) == (0.0, 0.0, 0.0, 0.0)
    assert on.mirror_on_sell is False
    # envelope rails stay live and identical to a normal policy's
    assert on.catastrophe_pct == cfg.MEMECOIN_REAL_CATASTROPHE_PCT
    assert on.max_hold_s == cfg.MEMECOIN_REAL_MAX_HOLD_S
    assert on.unquotable_exit_s == cfg.MEMECOIN_REAL_UNQUOTABLE_EXIT_S


def test_engine_version_is_frozen_into_the_funded_policy_at_reservation(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_ENGINE_VERSION", "v5")
    policy = resolve_funded_policy(coin_band="small", network_id="solana")
    assert policy.policy_id == ENGINE_ENVELOPE_POLICY_ID
    assert policy.decision_engine_version == "v5"


def test_flag_off_default_never_selects_engine_envelope(monkeypatch):
    from pathlib import Path
    Path(os.environ["SNIPER_STATE_ROOT"], ".env").write_text(
        "MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS=\n")
    p = resolve_funded_policy(coin_band="small", network_id="solana")
    assert p.policy_id != ENGINE_ENVELOPE_POLICY_ID


def test_engine_flag_rereads_state_root_without_mutating_process_env(monkeypatch):
    from pathlib import Path
    state_env = Path(os.environ["SNIPER_STATE_ROOT"], ".env")
    monkeypatch.setenv("MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS", "4663")
    assert cfg.engine_authoritative_chains() == {"solana"}
    assert os.environ["MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS"] == "4663"

    state_env.write_text("MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS=\n")
    assert cfg.engine_authoritative_chains() == set()
    assert os.environ["MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS"] == "4663"


def test_engine_flag_missing_or_invalid_state_root_fails_off(monkeypatch, tmp_path):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path / "missing"))
    assert cfg.engine_authoritative_chains() == set()
    monkeypatch.setenv("SNIPER_STATE_ROOT", "relative-root")
    assert cfg.engine_authoritative_chains() == set()


def test_engine_flag_malformed_or_unreadable_file_fails_off(tmp_path):
    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / ".env").write_text("MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS\n")
    assert cfg.engine_authoritative_chains(env={"SNIPER_STATE_ROOT": str(malformed)}) == set()

    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    (unreadable / ".env").mkdir()  # opening a directory as text fails on every privilege level
    assert cfg.engine_authoritative_chains(env={"SNIPER_STATE_ROOT": str(unreadable)}) == set()

    for i, value in enumerate(('"solana', 'solana"', "'solana", "solana'")):
        broken_quotes = tmp_path / f"broken-quotes-{i}"
        broken_quotes.mkdir()
        (broken_quotes / ".env").write_text(
            f"MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS={value}\n")
        assert cfg.engine_authoritative_chains(
            env={"SNIPER_STATE_ROOT": str(broken_quotes)}) == set()


def test_contract_json_records_engine_authoritative_flag():
    fields = _engine_contract_fields()
    contract = json.loads(fields["funded_contract_json"])
    assert contract["engine_authoritative"] is True
    normal_policy = resolve_funded_policy(coin_band="small", network_id="4663")
    normal = build_funded_contract(
        classification=_classification(network_id="4663"), base_clip_usd=10.0,
        approved_clip_usd=10.0, policy=normal_policy, origin_event_id=None)
    assert normal["engine_authoritative"] is False


def test_runtime_authority_requires_frozen_policy_to_match_active_revision(monkeypatch):
    policy = resolve_funded_policy(coin_band="small", network_id="solana")
    positions = [{"id": 1, "network_id": "solana", "mint": "MINT"}]
    assert R._engine_authoritative_ids(positions, {1: policy}, limit=5) == {1}

    monkeypatch.setattr(funded_policy, "active_policy_revision", lambda: None)
    assert R._engine_authoritative_ids(positions, {1: policy}, limit=5) == set()


def test_runtime_authority_rejects_live_parameter_drift(monkeypatch):
    policy = resolve_funded_policy(coin_band="small", network_id="solana")
    positions = [{"id": 1, "network_id": "solana", "mint": "MINT"}]
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_UNSELLABLE_SWEEPS",
                        cfg.MEMECOIN_REAL_UNSELLABLE_SWEEPS + 1)
    assert R._engine_authoritative_ids(positions, {1: policy}, limit=5) == set()


# --- pure mapping property test (mirrors the 600-case pattern in test_exit_engine_v5.py) --------

def test_engine_decision_mapping_never_widens_or_invents_a_route():
    import random
    rnd = random.Random(20260724)
    actions = [RIDE, DERISK, TAKE_PROFIT, CUT, DEFER]
    for _ in range(600):
        action = rnd.choice(actions)
        fraction = rnd.choice([None, 0.1, 0.5, 0.9]) if action in (DERISK, TAKE_PROFIT) else None
        exit_reason = "real_trail" if action == CUT else None
        decision = CandidateDecision(action=action, exit_reason=exit_reason,
                                     proposed_fraction=fraction)
        reason, mapped_fraction = R._map_engine_decision(decision)
        assert reason in (None, "real_trail", "derisk")     # never a new exit route
        if action == CUT:
            assert reason == "real_trail" and mapped_fraction is None       # full exit, not a partial
        elif action in (DERISK, TAKE_PROFIT):
            assert reason == "derisk"
            if mapped_fraction is not None:
                assert 0.0 < mapped_fraction <= 1.0                          # never a size increase
        else:
            assert reason is None and mapped_fraction is None                # no action ever
    assert R._map_engine_decision(None) == (None, None)


# --- real_exit_sweep adapter ---------------------------------------------------------------------

def test_engine_cut_exits_via_real_trail_debounce_exempt(tmp_path, monkeypatch):
    c = _c()
    pid = _open_engine_position(c, "MintCut")
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    # LP pull -> CUT outranks everything (v3 prereg §2.1), even a strong uptrend read.
    _write_state(connect, pid, "MintCut", depth_pull_flag=True, higher_low_state=True,
                 net_flow_quote="999")
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (calls.append((pos["mint"], reason)),
                                                     {"status": "closed"})[1],
          "preview_exit_fn": lambda pos: {"expected_usdc": 9.0}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:05:00Z")
    assert calls == [("MintCut", "real_trail")]
    audit = c.execute("SELECT allowed, detail FROM memecoin_exit_trigger_audit "
                      "WHERE position_id=? AND reason='real_trail'", (pid,)).fetchall()
    assert audit and audit[0]["allowed"] == 1
    assert json.loads(audit[0]["detail"])["mode"] == "engine"


def test_hot_cut_consumes_prewarmed_preview_without_requoting(monkeypatch):
    c = _c()
    pid = _open_engine_position(c, "MintHotCut")
    monkeypatch.setattr(R, "_connect_exit_state", lambda: object())
    decision = CandidateDecision(action=CUT, exit_reason="real_trail", reason_codes=("lp_pull",))
    monkeypatch.setattr(
        R, "_engine_decision_for", lambda xc, pos, policy, now_iso: (decision, None))
    calls = []
    quote_calls = []

    def _unexpected_quote(_pos):
        quote_calls.append(1)
        return {"expected_usdc": 1.0, "amount_atomic": 10}

    rc = {
        "exit_fn": lambda conn, pos, reason, now: calls.append(reason) or {"status": "closed"},
        "preview_exit_fn": _unexpected_quote,
    }
    R.real_exit_sweep(
        c, {"solana": rc}, price_fn=lambda _mints: {},
        now_fn=lambda: "2026-07-09T00:05:00Z", position_ids={pid},
        preview_by_position={pid: {"expected_usdc": 9.0, "amount_atomic": 10}},
    )
    assert calls == ["real_trail"]
    assert quote_calls == []


def test_engine_derisk_fires_once_via_derisked_at(tmp_path, monkeypatch):
    c = _c()
    pid = _open_engine_position(c, "MintDerisk", coin_band="nano")
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    # Use the fully evidenced volatility DERISK. Time-DERISK is held fail-closed until WO-5 supplies
    # persisted executable no-progress truth; age alone must never sell a healthy runner.
    # computed_at at/before the FIRST sweep's now_fn below (both sweeps still read fresh either way).
    _write_state(connect, pid, "MintDerisk", net_flow_quote="1", lower_high_state=True,
                 realized_vol=1.0,
                 computed_at="2026-07-09T00:00:00+00:00")
    calls = []
    def _derisk_fn(conn, pos, fraction, now):
        calls.append(fraction)
        m.set_position_derisked(conn, pos["id"], now)   # a real executor's one-shot stamp
        return {"status": "derisked", "received": 1.0, "sig": "sig1"}
    rc = {"derisk_fn": _derisk_fn, "preview_exit_fn": lambda pos: {"expected_usdc": 12.0}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:00:00Z")
    assert len(calls) == 1 and 0.0 < calls[0] <= 1.0
    pos = m.get_position(c, pid)
    assert pos["derisked_at"]
    xc = connect()
    try:
        evidence = xc.execute(
            "SELECT state_id, proposed_fraction, executable_confirmed, outcome "
            "FROM memecoin_exit_candidate_decisions WHERE position_id=?", (pid,)).fetchone()
    finally:
        xc.close()
    assert evidence["state_id"] is not None
    assert evidence["proposed_fraction"] == calls[0]
    assert evidence["executable_confirmed"] == 1 and evidence["outcome"] == "derisked"
    # second sweep: derisked_at now set -> propose_v5's once-guard stops proposing DERISK again.
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:05:00Z")
    assert len(calls) == 1              # unchanged: no second fire


def test_take_profit_maps_onto_the_same_partial_derisk_executor(tmp_path, monkeypatch):
    """Wiring proof, independent of whether propose_v5 can currently REACH TAKE_PROFIT: monkeypatch
    the engine-decision read to return a canned TAKE_PROFIT (the plumbing between the engine and the
    executor, not the pure decision rules, which are already covered by test_exit_engine_v5.py)."""
    c = _c()
    pid = _open_engine_position(c, "MintTP")
    monkeypatch.setattr(R, "_connect_exit_state", lambda: object())   # never actually read
    decision = CandidateDecision(action=TAKE_PROFIT, proposed_fraction=0.5,
                                 reason_codes=("take_profit", "climax"))
    monkeypatch.setattr(
        R, "_engine_decision_for", lambda xc, pos, policy, now_iso: (decision, None))
    calls = []

    def _derisk_fn(conn, pos, fraction, now):
        calls.append(fraction)
        m.set_position_derisked(conn, pos["id"], now)
        return {"status": "derisked", "received": 1.0, "sig": "sig1"}
    rc = {"derisk_fn": _derisk_fn, "preview_exit_fn": lambda pos: {"expected_usdc": 12.0}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:00:30Z")
    assert calls == [0.5]
    assert m.get_position(c, pid)["derisked_at"]


def test_take_profit_fires_from_a_real_feature_read_via_the_envelope_reference_trail(tmp_path, monkeypatch):
    """v3 prereg §6: the envelope's EXECUTED trail is 0/off, but the real-runner adapter passes the
    position's coin-band v1 anchor (band_anchor_trail_pct — the SAME [inherited] value the
    non-envelope policy for that band already uses) as propose_v5's REFERENCE scale. nano/micro's
    anchor is MEMECOIN_REAL_PARTIAL_TRAIL_PCT (30 by default) — realized_vol=0.31 clears that
    blowoff threshold, so a real feature read now produces TAKE_PROFIT, not DEFER/RIDE."""
    c = _c()
    pid = _open_engine_position(c, "MintTPReachable", coin_band="nano")
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    _write_state(connect, pid, "MintTPReachable", higher_low_state=True, arrival_accel=2.0,
                 realized_vol=0.31, net_flow_quote="10", computed_at="2026-07-09T00:00:30+00:00")
    calls = []

    def _derisk_fn(conn, pos, fraction, now):
        calls.append(fraction)
        m.set_position_derisked(conn, pos["id"], now)
        return {"status": "derisked", "received": 1.0, "sig": "sig1"}
    rc = {"derisk_fn": _derisk_fn, "preview_exit_fn": lambda pos: {"expected_usdc": 12.0}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:00:30Z")
    assert len(calls) == 1 and 0.5 < calls[0] < 0.75   # v5 conviction-scaled (vol above blowoff)
    assert m.get_position(c, pid)["derisked_at"]


def test_lower_high_derisk_fires_from_a_real_feature_read_via_the_envelope_reference_trail(
        tmp_path, monkeypatch):
    """Same reference-scale fix, the DERISK §2.3 vol-expansion branch (distinct from the
    edge-duration branch covered elsewhere): lower_high_state + realized_vol at/above the mid
    band's anchor (35) now reaches a real propose_v5 DERISK instead of deferring."""
    c = _c()
    pid = _open_engine_position(c, "MintDerisklowerhigh", coin_band="mid")
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    _write_state(connect, pid, "MintDerisklowerhigh", lower_high_state=True, realized_vol=0.36,
                 net_flow_quote="1", computed_at="2026-07-09T00:00:30+00:00")
    calls = []

    def _derisk_fn(conn, pos, fraction, now):
        calls.append(fraction)
        m.set_position_derisked(conn, pos["id"], now)
        return {"status": "derisked", "received": 1.0, "sig": "sig1"}
    rc = {"derisk_fn": _derisk_fn, "preview_exit_fn": lambda pos: {"expected_usdc": 12.0}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:00:30Z")
    assert len(calls) == 1 and 0.5 < calls[0] < 0.75   # v5 conviction-scaled (vol above blowoff)
    assert m.get_position(c, pid)["derisked_at"]


def test_v5_funded_fallback_uses_scaled_fraction_and_records_actual_engine(
        tmp_path, monkeypatch):
    """Catches hot v5 falling back to hard-coded v3 during a WS gap."""
    c = _c()
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_ENGINE_VERSION", "v5")
    pid = _open_engine_position(c, "MintV5Fallback", coin_band="nano")
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    _write_state(connect, pid, "MintV5Fallback", lower_high_state=True, realized_vol=0.60,
                 net_flow_quote="-1", computed_at="2026-07-09T00:00:30+00:00")
    calls = []

    def _derisk_fn(conn, pos, fraction, now):
        calls.append(fraction)
        m.set_position_derisked(conn, pos["id"], now)
        return {"status": "derisked", "received": 1.0, "sig": "sig-v5"}

    rc = {"derisk_fn": _derisk_fn, "preview_exit_fn": lambda pos: {"expected_usdc": 12.0}}
    R.real_exit_sweep(
        c, {"solana": rc}, price_fn=lambda mints: {},
        now_fn=lambda: "2026-07-09T00:00:30Z")
    assert calls == [pytest.approx(7 / 12)]
    xc = connect()
    try:
        row = xc.execute(
            "SELECT engine_version,proposed_fraction,outcome,state_id "
            "FROM memecoin_exit_candidate_decisions WHERE position_id=?", (pid,)).fetchone()
    finally:
        xc.close()
    assert row["engine_version"] == ENGINE_VERSION_V5
    assert row["proposed_fraction"] == pytest.approx(7 / 12)
    assert row["outcome"] == "derisked" and row["state_id"] is not None


def test_engine_envelope_executed_trail_is_always_zero():
    """The reference scale is adapter-side only — the FROZEN policy's executed trail must never
    pick up the anchor value, for any band."""
    for band in ("nano", "micro", "small", "mid"):
        assert resolve_funded_policy(coin_band=band, network_id="solana").trail_pct == 0.0


def test_legacy_trail_never_fires_under_envelope_even_at_the_reference_giveback(tmp_path, monkeypatch):
    """The reference trail (30% for nano) is ONLY a vol-blowoff scale for the engine — it must never
    leak into the legacy executed-trail rail. A 30% giveback from peak (which WOULD trip a live
    30%-trail policy) must not fire 'real_trail' unless the engine itself decides CUT; a RIDE-shaped
    state (no CUT trigger) leaves the position untouched."""
    c = _c()
    pid = _open_engine_position(c, "MintNoLegacyTrail", coin_band="nano")
    c.execute("UPDATE memecoin_positions SET peak_price=?, executable_peak_price=? WHERE id=?",
              (1.30, 1.30, pid))
    c.commit()
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    _write_state(connect, pid, "MintNoLegacyTrail", higher_low_state=True, net_flow_quote="1")   # RIDE-shaped
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (calls.append(reason), {"status": "closed"})[1],
          "preview_exit_fn": lambda pos: {"expected_usdc": 9.1}}
    mid = 1.30 * (1 - 0.30)   # exactly a 30% giveback from peak — would trip a LIVE 30% trail
    pmap = {"MintNoLegacyTrail": {"price": mid, "liquidity": 1e9}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: pmap,
                      now_fn=lambda: "2026-07-09T00:05:00Z")
    assert calls == []
    assert m.get_position(c, pid)["is_open"] == 1


def test_non_capable_state_is_envelope_only(tmp_path, monkeypatch):
    c = _c()
    pid = _open_engine_position(c, "MintNoState")
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    # no materialized state written at all
    calls = []
    rc = {"exit_fn": lambda *a: pytest.fail("must not exit without a capable engine state"),
          "derisk_fn": lambda *a: pytest.fail("must not derisk without a capable engine state"),
          "preview_exit_fn": lambda pos: {"expected_usdc": 9.0}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:05:00Z")
    assert m.get_position(c, pid)["is_open"] == 1


def test_catastrophe_still_fires_under_engine_mode(tmp_path, monkeypatch):
    c = _c()
    pid = _open_engine_position(c, "MintCatastrophe")
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    # a RIDE-shaped state (engine would say "keep holding") — catastrophe must still win the tie.
    _write_state(connect, pid, "MintCatastrophe", higher_low_state=True, net_flow_quote="1")
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (calls.append(reason), {"status": "closed"})[1],
          "preview_exit_fn": lambda pos: {"expected_usdc": 1.0}}
    cat = cfg.MEMECOIN_REAL_CATASTROPHE_PCT
    pmap = {"MintCatastrophe": {"price": 1.0 * (1 - (cat + 5) / 100.0), "liquidity": 1e9}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: pmap,
                      now_fn=lambda: "2026-07-09T00:05:00Z")
    assert calls == ["catastrophe"]


def test_max_positions_bound_excludes_the_newest_position(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_ENGINE_MAX_POSITIONS", 1)
    c = _c()
    pid_old = _open_engine_position(c, "MintOld", opened="2026-07-09T00:00:00Z")
    pid_new = _open_engine_position(c, "MintNew", opened="2026-07-09T00:00:01Z")
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    # both LP-pulled -> both would CUT if the engine were allowed to drive them.
    _write_state(connect, pid_old, "MintOld", depth_pull_flag=True)
    _write_state(connect, pid_new, "MintNew", depth_pull_flag=True)
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (calls.append(pos["mint"]),
                                                     {"status": "closed"})[1],
          "preview_exit_fn": lambda pos: {"expected_usdc": 9.0}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:05:00Z")
    assert calls == ["MintOld"]                                  # only the bound-eligible (oldest id)
    assert m.get_position(c, pid_new)["is_open"] == 1             # excess position untouched by the engine


# --- P0-1 (Codex 2026-07-24 activation-blocker review): stale/wrong-identity state validation ---
# The "January-snapshot-sells-July-position" hazard — `db_exit_state.latest_state` selects strictly
# by position_id with no other check. Every case below writes a state that WOULD CUT immediately if
# trusted (depth_pull_flag=True), then asserts the engine never acts on it.

def test_stale_state_falls_back_to_envelope_only(tmp_path, monkeypatch):
    c = _c()
    pid = _open_engine_position(c, "MintStale")
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    _write_state(connect, pid, "MintStale", depth_pull_flag=True,
                 computed_at="2026-01-01T00:00:00+00:00")   # months before the sweep clock below
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (calls.append(reason), {"status": "closed"})[1],
          "preview_exit_fn": lambda pos: {"expected_usdc": 9.0}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:05:00Z")
    assert calls == []
    assert m.get_position(c, pid)["is_open"] == 1


def test_identity_mismatched_mint_falls_back_to_envelope_only(tmp_path, monkeypatch):
    """A recycled position_id (a DB reset / backup restore) carrying a DIFFERENT position's state
    must never be read as authoritative for the current one."""
    c = _c()
    pid = _open_engine_position(c, "MintReal")
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    _write_state(connect, pid, "SomeOtherMint", depth_pull_flag=True)
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (calls.append(reason), {"status": "closed"})[1],
          "preview_exit_fn": lambda pos: {"expected_usdc": 9.0}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:05:00Z")
    assert calls == []
    assert m.get_position(c, pid)["is_open"] == 1


def test_identity_mismatched_chain_falls_back_to_envelope_only(tmp_path, monkeypatch):
    c = _c()
    pid = _open_engine_position(c, "MintChainMismatch")
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    _write_state(connect, pid, "MintChainMismatch", chain_id="4663", depth_pull_flag=True)
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (calls.append(reason), {"status": "closed"})[1],
          "preview_exit_fn": lambda pos: {"expected_usdc": 9.0}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:05:00Z")
    assert calls == []
    assert m.get_position(c, pid)["is_open"] == 1


def test_stale_engine_version_falls_back_to_envelope_only(tmp_path, monkeypatch):
    c = _c()
    pid = _open_engine_position(c, "MintOldEngine")
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    _write_state(connect, pid, "MintOldEngine", depth_pull_flag=True, engine_version="exit-brain-v1")
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (calls.append(reason), {"status": "closed"})[1],
          "preview_exit_fn": lambda pos: {"expected_usdc": 9.0}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:05:00Z")
    assert calls == []
    assert m.get_position(c, pid)["is_open"] == 1


def test_stale_feature_version_falls_back_to_envelope_only(tmp_path, monkeypatch):
    c = _c()
    pid = _open_engine_position(c, "MintOldFeature")
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    _write_state(connect, pid, "MintOldFeature", depth_pull_flag=True, feature_version="v1")
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (calls.append(reason), {"status": "closed"})[1],
          "preview_exit_fn": lambda pos: {"expected_usdc": 9.0}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:05:00Z")
    assert calls == []
    assert m.get_position(c, pid)["is_open"] == 1


# --- material defect (b): the chain flag is ALSO a runtime gate, not just a reservation-time one ---

def test_clearing_the_chain_flag_demotes_existing_positions_to_envelope_only(tmp_path, monkeypatch):
    """An operator clearing MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS must demote every existing
    engine-envelope position on that chain to envelope-only rails at the very next sweep — the
    frozen contract's policy_id alone is not a runtime kill switch."""
    c = _c()
    pid = _open_engine_position(c, "MintDemote")
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    _write_state(connect, pid, "MintDemote", depth_pull_flag=True)   # would CUT if the engine ran
    from pathlib import Path
    Path(os.environ["SNIPER_STATE_ROOT"], ".env").write_text(
        "MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS=\n")                 # operator clears flag
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (calls.append(reason), {"status": "closed"})[1],
          "preview_exit_fn": lambda pos: {"expected_usdc": 9.0}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:05:00Z")
    assert calls == []                                    # engine never consulted -> envelope-only
    assert m.get_position(c, pid)["is_open"] == 1


# --- material defect (a): exact_quote_misses maintenance must run for envelope positions too ------

def test_exact_quote_misses_accrues_under_engine_envelope_positions(tmp_path, monkeypatch):
    """exact_policy is always False under engine-envelope-v1 (trail_pct/derisk_pct both zeroed), so
    the exact_quote_misses accounting used to be structurally unreachable for engine positions —
    propose_v5's CUT-unsellable branch could never fire. Being engine-authoritative must ALSO drive
    the exact-quote attempt/tracking, independent of exact_policy."""
    c = _c()
    pid = _open_engine_position(c, "MintUnsellable")
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    # no materialized state needed: engine_ids membership (policy_id + live chain) alone drives the
    # exact-quote gate, independent of whether a state was ever read.
    pmap = {"MintUnsellable": {"price": 1.5, "liquidity": 1e9}}
    rc = {"exit_fn": lambda *a: pytest.fail("no exit expected"),
          "derisk_fn": lambda *a: pytest.fail("no derisk expected"),
          "preview_exit_fn": lambda pos: {"expected_usdc": 0.0}}   # chart alive, bag will not quote
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: pmap,
                      now_fn=lambda: "2026-07-09T00:05:00Z")
    assert m.get_position(c, pid)["exact_quote_misses"] == 1


# --- WO-4: typed quote-failure sweep pre-pass (false-CUT protection) --------------------------------

def _fail_all_preview(pos):
    return {"expected_usdc": 0.0}      # chart alive, bag will not quote -> exact-quote failure


def test_wo4_provider_outage_advances_no_misses(tmp_path, monkeypatch):
    """(a) When EVERY exact-relevant engine position fails its exact quote in one sweep while charts
    still price them, that is a provider outage — the miss counter must NOT advance for any, so the
    outage cannot manufacture a false unsellable-CUT (the measured 4663 class)."""
    c = _c()
    pids = [_open_engine_position(c, mint) for mint in ("MintOutA", "MintOutB", "MintOutC")]
    monkeypatch.setattr(R, "_connect_exit_state", _exit_state_factory(tmp_path))
    pmap = {mint: {"price": 1.5, "liquidity": 1e9} for mint in ("MintOutA", "MintOutB", "MintOutC")}
    rc = {"exit_fn": lambda *a: pytest.fail("no exit expected"),
          "derisk_fn": lambda *a: pytest.fail("no derisk expected"),
          "preview_exit_fn": _fail_all_preview}
    sent = []
    monkeypatch.setattr(R.alerts, "alert_quote_infra_episode", lambda event: sent.append(event))
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: pmap,
                      now_fn=lambda: "2026-07-09T00:05:00Z")
    assert [m.get_position(c, pid)["exact_quote_misses"] or 0 for pid in pids] == [0, 0, 0]
    assert sent[0]["failure"] == "provider_wide_failure"


def test_wo4_single_position_streak_still_advances(tmp_path, monkeypatch):
    """(b) A lone position whose bag genuinely will not quote is not an outage — its miss streak must
    still advance every sweep toward the CUT (a late CUT is a rug loss)."""
    c = _c()
    pid = _open_engine_position(c, "MintStreak")
    monkeypatch.setattr(R, "_connect_exit_state", _exit_state_factory(tmp_path))
    pmap = {"MintStreak": {"price": 1.5, "liquidity": 1e9}}
    rc = {"exit_fn": lambda *a: pytest.fail("no exit expected"),
          "preview_exit_fn": _fail_all_preview}
    for _ in range(2):
        R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: pmap,
                          now_fn=lambda: "2026-07-09T00:05:00Z")
    assert m.get_position(c, pid)["exact_quote_misses"] == 2


def test_wo4_engine_position_advances_identically_when_not_an_outage(tmp_path, monkeypatch):
    """(c) Identity for the funded/engine cohort (the 4663 false-CUT cohort): when the provider is up
    (another position's exact quote succeeds this sweep), a failing engine position (pid in
    engine_ids) advances EXACTLY as before the pre-pass — one increment, driven by engine membership."""
    c = _c()
    ok_pid = _open_engine_position(c, "MintOk")
    fail_pid = _open_engine_position(c, "MintEngineFail")
    monkeypatch.setattr(R, "_connect_exit_state", _exit_state_factory(tmp_path))
    pmap = {"MintOk": {"price": 1.5, "liquidity": 1e9},
            "MintEngineFail": {"price": 1.5, "liquidity": 1e9}}
    rc = {"exit_fn": lambda *a: None, "derisk_fn": lambda *a: None,
          # provider is UP: MintOk quotes, MintEngineFail does not -> not an outage
          "preview_exit_fn": lambda pos: ({"expected_usdc": 12.0}
                                          if pos["mint"] == "MintOk" else {"expected_usdc": 0.0})}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: pmap,
                      now_fn=lambda: "2026-07-09T00:05:00Z")
    assert (m.get_position(c, ok_pid)["exact_quote_misses"] or 0) == 0
    assert m.get_position(c, fail_pid)["exact_quote_misses"] == 1


def test_wo4_single_typed_infra_failure_persists_ops_streak_without_unsellable_miss(
        tmp_path, monkeypatch):
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    from sniper.execution.exit_preview import PreviewResult

    c = _c()
    pid = _open_engine_position(c, "MintInfra")
    monkeypatch.setattr(R, "_connect_exit_state", _exit_state_factory(tmp_path))
    pmap = {"MintInfra": {"price": 1.5, "liquidity": 1e9}}
    sent = []
    monkeypatch.setattr(R.alerts, "alert_quote_infra_episode", lambda event: sent.append(event))
    rc = {"exit_fn": lambda *a: pytest.fail("no exit expected"),
          "preview_exit_fn": lambda _pos: PreviewResult.failed(
              QuoteFailure.RATE_LIMIT, provider_id="solana:jupiter")}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda _mints: pmap,
                      now_fn=lambda: "2026-07-09T00:05:00Z")
    row = m.get_position(c, pid)
    assert (row["exact_quote_misses"] or 0) == 0
    assert row["exact_quote_infra_misses"] == 1
    assert sent == [{
        "kind": "outage", "provider": "solana:jupiter", "failure": "rate_limit",
        "affected": 1, "max_streak": 1, "milestone": 1,
    }]


def test_wo4_single_typed_token_failure_advances_unsellable_only(tmp_path, monkeypatch):
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    from sniper.execution.exit_preview import PreviewResult

    c = _c()
    pid = _open_engine_position(c, "MintNoRoute")
    monkeypatch.setattr(R, "_connect_exit_state", _exit_state_factory(tmp_path))
    pmap = {"MintNoRoute": {"price": 1.5, "liquidity": 1e9}}
    rc = {"exit_fn": lambda *a: pytest.fail("no exit expected"),
          "preview_exit_fn": lambda _pos: PreviewResult.failed(
              QuoteFailure.NO_ROUTE, provider_id="solana:jupiter")}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda _mints: pmap,
                      now_fn=lambda: "2026-07-09T00:05:00Z")
    row = m.get_position(c, pid)
    assert row["exact_quote_misses"] == 1
    assert (row["exact_quote_infra_misses"] or 0) == 0


def test_wo4_solana_exact_quote_group_preserves_no_route_advancement(tmp_path, monkeypatch):
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    from sniper.execution.exit_preview import ExitPreview, PreviewResult

    c = _c()
    ok_pid = _open_engine_position(c, "MintOk")
    fail_a = _open_engine_position(c, "MintNoRouteA")
    fail_b = _open_engine_position(c, "MintNoRouteB")
    monkeypatch.setattr(R, "_connect_exit_state", _exit_state_factory(tmp_path))

    def preview(pos):
        if pos["id"] == ok_pid:
            return PreviewResult.succeeded(
                ExitPreview(12.0, 1, "jupiter", "2026-08-05T00:00:00Z"),
                provider_id="solana:exact_quote", provider_group="solana:exact_quote",
            )
        return PreviewResult.failed(
            QuoteFailure.NO_ROUTE,
            provider_id="solana:jupiter", provider_group="solana:exact_quote",
        )

    rc = {"exit_fn": lambda *a: pytest.fail("no exit expected"),
          "preview_exit_fn": preview}
    prices = {mint: {"price": 1.5, "liquidity": 1e9}
              for mint in ("MintOk", "MintNoRouteA", "MintNoRouteB")}

    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda _mints: prices,
                      now_fn=lambda: "2026-07-09T00:05:00Z")

    assert (m.get_position(c, ok_pid)["exact_quote_misses"] or 0) == 0
    assert m.get_position(c, fail_a)["exact_quote_misses"] == 1
    assert m.get_position(c, fail_b)["exact_quote_misses"] == 1


def test_wo4_provider_outage_sends_one_aggregate_alert_for_two_positions(tmp_path, monkeypatch):
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    from sniper.execution.exit_preview import PreviewResult

    c = _c()
    first = _open_engine_position(c, "MintInfraA")
    second = _open_engine_position(c, "MintInfraB")
    monkeypatch.setattr(R, "_connect_exit_state", _exit_state_factory(tmp_path))
    sent = []
    monkeypatch.setattr(R.alerts, "alert_quote_infra_episode", lambda event: sent.append(event))
    rc = {
        "exit_fn": lambda *a: pytest.fail("no exit expected"),
        "preview_exit_fn": lambda _pos: PreviewResult.failed(
            QuoteFailure.RPC_TRANSPORT, provider_id="solana:jupiter"),
    }
    prices = {"MintInfraA": {"price": 1.5, "liquidity": 1e9},
              "MintInfraB": {"price": 1.5, "liquidity": 1e9}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda _mints: prices,
                      now_fn=lambda: "2026-07-09T00:05:00Z")
    assert sent == [{
        "kind": "outage", "provider": "solana:jupiter", "failure": "rpc_transport",
        "affected": 2, "max_streak": 1, "milestone": 1,
    }]
    assert m.get_position(c, first)["exact_quote_misses"] in (None, 0)
    assert m.get_position(c, second)["exact_quote_misses"] in (None, 0)


def test_wo4_literal_none_is_cached_and_not_quoted_twice(tmp_path, monkeypatch):
    c = _c()
    _open_engine_position(c, "MintNone")
    monkeypatch.setattr(R, "_connect_exit_state", _exit_state_factory(tmp_path))
    calls = []
    rc = {"exit_fn": lambda *a: None,
          "preview_exit_fn": lambda pos: calls.append(pos["id"]) or None}
    R.real_exit_sweep(
        c, {"solana": rc},
        price_fn=lambda _mints: {"MintNone": {"price": 1.5, "liquidity": 1e9}},
        now_fn=lambda: "2026-07-09T00:05:00Z")
    assert len(calls) == 1


def test_wo4_previews_independent_positions_concurrently(tmp_path, monkeypatch):
    import threading

    c = _c()
    pids = [_open_engine_position(c, mint) for mint in ("MintParallelA", "MintParallelB")]
    monkeypatch.setattr(R, "_connect_exit_state", _exit_state_factory(tmp_path))
    barrier = threading.Barrier(2)
    def preview(_pos):
        barrier.wait(timeout=1.0)
        return {"expected_usdc": 12.0}
    rc = {"exit_fn": lambda *a: None, "preview_exit_fn": preview}
    pmap = {mint: {"price": 1.5, "liquidity": 1e9}
            for mint in ("MintParallelA", "MintParallelB")}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda _mints: pmap,
                      now_fn=lambda: "2026-07-09T00:05:00Z")
    rows = [m.get_position(c, pid) for pid in pids]
    assert [(row["exact_quote_misses"] or 0) for row in rows] == [0, 0]
    assert [(row["exact_quote_infra_misses"] or 0) for row in rows] == [0, 0]


# --- material defect (c): durable decision logging for the production engine path -----------------

def test_acted_engine_decision_is_durably_logged(tmp_path, monkeypatch):
    """An ACTED engine decision (CUT/DERISK/TAKE_PROFIT) must be written through
    db_exit_state.write_candidate_decision with its reason codes — not just logged to stdout."""
    c = _c()
    pid = _open_engine_position(c, "MintAudited")
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    _write_state(connect, pid, "MintAudited", depth_pull_flag=True, higher_low_state=True,
                 net_flow_quote="999")
    rc = {"exit_fn": lambda conn, pos, reason, now: {"status": "closed"},
          "preview_exit_fn": lambda pos: {"expected_usdc": 9.0}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:05:00Z")
    xc = connect()
    try:
        row = xc.execute(
            "SELECT candidate_action, candidate_reason_codes_json, engine_version "
            "FROM memecoin_exit_candidate_decisions WHERE position_id=?", (pid,)).fetchone()
    finally:
        xc.close()
    assert row is not None
    assert row["candidate_action"] == CUT
    assert "lp_pull" in row["candidate_reason_codes_json"]
    assert row["engine_version"] == ENGINE_VERSION_V5


def test_every_evaluated_decision_is_durably_logged_including_ride(tmp_path, monkeypatch):
    """P1-B (Codex round 2, supersedes the earlier no-log-on-quiet test): the design contract
    requires a decision record EVERY evaluated tick, RIDE/DEFER included — fallback-rate and
    per-action RIDE reads must be possible from the table alone."""
    c = _c()
    pid = _open_engine_position(c, "MintQuietRide")
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    _write_state(connect, pid, "MintQuietRide", higher_low_state=True, net_flow_quote="1")   # RIDE
    rc = {"exit_fn": lambda *a: pytest.fail("no exit expected"),
          "preview_exit_fn": lambda pos: {"expected_usdc": 9.0}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:05:00Z")
    xc = connect()
    try:
        rows = [dict(r) for r in xc.execute(
            "SELECT * FROM memecoin_exit_candidate_decisions WHERE position_id=?", (pid,))]
    finally:
        xc.close()
    assert len(rows) >= 1                                   # quiet sweeps are evidence too
    assert rows[0]["candidate_action"] == "RIDE"


def test_derisk_claim_crash_leaves_pending_not_confirmed(tmp_path):
    """P0-A (Codex round 2): an executor exception between claim and submit must leave a PENDING
    row — derisked_at NULL, derisk_confirmed False, visible to stale-pending recovery — never a
    false-confirmed row excluded from every queue."""
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_real_runner import derisk_confirmed
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    (m.init_memecoin_tables(c) if hasattr(m, "init_memecoin_tables") else m.ensure_memecoin_tables(c))
    c.execute("INSERT INTO memecoin_positions (id, mint, is_open) VALUES (1,'C',1)"); c.commit()
    claim1 = m.claim_position_derisk(c, 1, "2026-07-10T01:00:00+00:00")
    assert claim1
    # executor dies here (no release, no settle) — the crash shape
    pos = dict(c.execute("SELECT * FROM memecoin_positions WHERE id=1").fetchone())
    assert pos["derisked_at"] is None                       # NOT settled
    assert pos["derisk_pending_at"] is not None             # visibly pending
    assert derisk_confirmed(pos) is False                   # trail can never arm on it
    stale = m.stale_derisk_pendings(c, "2026-07-10T02:00:00+00:00")
    assert [p["id"] for p in stale] == [1]                  # recovery sees it
    assert m.release_stale_derisk_pending(
        c, 1, claim1, "2026-07-10T01:00:00+00:00")
    claim2 = m.claim_position_derisk(c, 1, "2026-07-10T03:00:00+00:00")
    assert claim2 and claim2 != claim1                              # rail re-armed
    # settled path clears pending atomically
    m.set_position_derisked(c, 1, "2026-07-10T03:05:00+00:00")
    pos = dict(c.execute("SELECT * FROM memecoin_positions WHERE id=1").fetchone())
    assert pos["derisked_at"] and pos["derisk_pending_at"] is None
    assert m.claim_position_derisk(c, 1, "x") is None       # one-shot stands


def test_derisk_pending_blocks_second_claim():
    import sqlite3
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    (m.init_memecoin_tables(c) if hasattr(m, "init_memecoin_tables") else m.ensure_memecoin_tables(c))
    c.execute("INSERT INTO memecoin_positions (id, mint, is_open) VALUES (2,'D',1)"); c.commit()
    assert m.claim_position_derisk(c, 2, "t1")
    assert m.claim_position_derisk(c, 2, "t2") is None      # loser refused while pending


def test_stale_reaper_snapshot_cannot_release_replacement_derisk_claim():
    import sqlite3
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.execute("INSERT INTO memecoin_positions (id, mint, is_open) VALUES (3,'E',1)"); c.commit()
    first = m.claim_position_derisk(c, 3, "t1")
    assert m.release_stale_derisk_pending(c, 3, first, "t1")
    replacement = m.claim_position_derisk(c, 3, "t2")
    assert replacement and replacement != first

    # A second recovery worker still holding the old snapshot cannot clear the new owner.
    assert not m.release_stale_derisk_pending(c, 3, first, "t1")
    row = dict(c.execute("SELECT * FROM memecoin_positions WHERE id=3").fetchone())
    assert row["derisk_claim_id"] == replacement
    assert row["derisk_pending_at"] == "t2"


def test_delayed_executor_cannot_mutate_replacement_derisk_claim():
    import sqlite3
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.execute("INSERT INTO memecoin_positions (id, mint, is_open) VALUES (4,'F',1)"); c.commit()
    old = m.claim_position_derisk(c, 4, "t1")
    assert m.release_stale_derisk_pending(c, 4, old, "t1")
    replacement = m.claim_position_derisk(c, 4, "t2")

    assert not m.mark_position_derisk_submitted(c, 4, old, "t3", "SIG_OLD")
    assert not m.release_position_derisk_claim(c, 4, old)
    assert m.mark_position_derisk_submitted(c, 4, replacement, "t3", "SIG_NEW")
    assert not m.reset_position_derisk_submission(c, 4, old, "SIG_NEW", "t4")
    with pytest.raises(ValueError, match="derisk stamp matched 0 rows"):
        m.book_derisk_fill(
            c, position_id=4, at="t4", price=1.0, qty=1.0, notional=1.0,
            fraction=0.5, tx_sig="SIG_NEW", claim_id=old)

    row = dict(c.execute("SELECT * FROM memecoin_positions WHERE id=4").fetchone())
    assert row["derisk_claim_id"] == replacement and row["derisk_sig"] == "SIG_NEW"
    assert c.execute(
        "SELECT COUNT(*) FROM memecoin_fills WHERE position_id=4 AND kind='derisk'"
    ).fetchone()[0] == 0


def test_decision_row_carries_state_id_and_execution_outcome(tmp_path, monkeypatch):
    """P1-B remainder (2026-07-25): the decision row links the exact state snapshot it acted on
    (state_id) and is updated with what actually happened (executable_confirmed + outcome) —
    decision and result readable from one row."""
    c = _c()
    pid = _open_engine_position(c, "MintChain")
    connect = _exit_state_factory(tmp_path)
    monkeypatch.setattr(R, "_connect_exit_state", connect)
    _write_state(connect, pid, "MintChain", depth_pull_flag=True)           # LP pull -> CUT
    rc = {"exit_fn": lambda conn, pos, reason, now: {"status": "closed"},
          "preview_exit_fn": lambda pos: {"expected_usdc": 9.0}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:05:00Z")
    xc = connect()
    try:
        row = xc.execute("SELECT * FROM memecoin_exit_candidate_decisions WHERE position_id=? "
                         "AND candidate_action='CUT'", (pid,)).fetchone()
        snap = xc.execute("SELECT id FROM memecoin_exit_market_state WHERE position_id=?",
                          (pid,)).fetchone()
    finally:
        xc.close()
    assert row is not None
    assert row["state_id"] == snap["id"]                    # links the exact snapshot acted on
    assert row["proposed_fraction"] is None                  # CUT is a full exit, never partial
    assert row["executable_confirmed"] == 1
    assert row["outcome"] == "closed"


def test_locked_decision_db_cannot_delay_any_funded_exit(tmp_path, monkeypatch):
    """All funded hooks finish before the deferred audit flush contends on its own DB lock."""
    import threading

    c = _c()
    pids = [_open_engine_position(c, mint, uid=f"u{i}")
            for i, mint in enumerate(("MintLockA", "MintLockB"), 1)]
    path = str(tmp_path / "locked-exit-state.db")
    setup = x.connect_exit_state(path)
    setup.close()

    def connect_short():
        xc = sqlite3.connect(path)
        xc.row_factory = sqlite3.Row
        return xc

    monkeypatch.setenv("MEMECOIN_EXIT_STATE_DB", path)
    for pid, mint in zip(pids, ("MintLockA", "MintLockB")):
        _write_state(connect_short, pid, mint, depth_pull_flag=True)

    lock_ready = threading.Event()
    funded_done = threading.Event()
    released_by_funded = []

    def hold_write_lock():
        locker = sqlite3.connect(path)
        locker.execute("BEGIN IMMEDIATE")
        lock_ready.set()
        released_by_funded.append(funded_done.wait(0.35))
        locker.rollback()
        locker.close()

    thread = threading.Thread(target=hold_write_lock)
    thread.start()
    assert lock_ready.wait(1.0)
    calls = []

    def exit_fn(_conn, pos, reason, _now):
        calls.append((pos["id"], reason))
        if len(calls) == 2:
            funded_done.set()
        return {"status": "closed"}

    rc = {"exit_fn": exit_fn, "preview_exit_fn": lambda pos: {"expected_usdc": 9.0}}
    R.real_exit_sweep(
        c, {"solana": rc}, price_fn=lambda mints: {},
        now_fn=lambda: "2026-07-09T00:05:00Z")
    thread.join(timeout=1.0)

    assert len(calls) == 2
    assert released_by_funded == [True]
