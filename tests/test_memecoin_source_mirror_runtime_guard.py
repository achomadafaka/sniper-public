import json
import sqlite3
from pathlib import Path

import pytest

from scripts import memecoin_runtime_verify as verify
from sniper.services import memecoin_funded_policy as fp
from sniper.services.memecoin_market_state import EntryClassification


ROOT = Path(__file__).resolve().parents[1]
UID = "pump:ExactCaseOwner"


@pytest.fixture(autouse=True)
def _isolated_database(monkeypatch):
    monkeypatch.delenv("DB_PATH", raising=False)


def _state(tmp_path, selector=""):
    state = tmp_path / "state"
    state.mkdir()
    (state / ".env").write_text(
        f"MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS={selector}\nMEMECOIN_REAL_ENABLED=0\n")
    (state / ".fomo.env").write_text("")
    with sqlite3.connect(state / "sniper_tracker.db") as conn:
        conn.execute("CREATE TABLE memecoin_positions (id INTEGER, source_user_id TEXT, network_id TEXT, "
                     "mint TEXT, execution_mode TEXT, is_open INTEGER, funded_contract_version INTEGER, "
                     "funded_contract_hash TEXT, funded_contract_json TEXT)")
        conn.execute("CREATE TABLE memecoin_manual_exit_intents "
                     "(action TEXT, state TEXT, request_json TEXT)")
    return state


def _old_candidate(tmp_path):
    candidate = tmp_path / "old"
    (candidate / "sniper" / "services").mkdir(parents=True)
    (candidate / "sniper" / "__init__.py").write_text("")
    (candidate / "sniper" / "services" / "__init__.py").write_text("")
    (candidate / "sniper" / "services" / "memecoin_funded_policy.py").write_text(
        "SOURCE_MIRROR_TP100_POLICY_ID = 'source-mirror-tp100-v1'\n")
    return candidate


def _open_profile(state, *, opened=True):
    classification = EntryClassification(
        classification_version=1, network_id="solana", mint="MINT", signal_source="onchain",
        decision_at="2026-10-20T12:00:00+00:00", coin_band="small", lifecycle="young",
        liquidity_class="balanced", momentum_phase="climbing", market_regime="unknown",
        data_quality="complete", risk_scale=1.0, reason_codes=(), facts={})
    contract = fp.build_funded_contract(
        classification=classification, base_clip_usd=50, approved_clip_usd=50,
        policy=fp.source_mirror_tp100_policy(coin_band="small"), origin_event_id="buy",
        source_mirror_tp100={"policy_id": fp.SOURCE_MIRROR_TP100_POLICY_ID,
                             "source_user_id": UID, "network_id": "solana"})
    encoded, digest = fp.canonical_funded_contract(contract)
    with sqlite3.connect(state / "sniper_tracker.db") as conn:
        conn.execute("INSERT INTO memecoin_positions VALUES (1,?,?,?,'real',?,1,?,?)",
                     (UID, "solana", "MINT", int(opened), digest, encoded))


def test_ordinary_book_does_not_require_new_capability_or_start_candidate(tmp_path, monkeypatch):
    state = _state(tmp_path)
    monkeypatch.delenv("MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS", raising=False)
    monkeypatch.setattr(verify.subprocess, "run", lambda *_a, **_k: pytest.fail("unneeded candidate probe"))
    assert verify.source_mirror_capability_errors(_old_candidate(tmp_path), state) == []


@pytest.mark.parametrize("reason", ["config", "open", "queued", "claimed", "submitted_unknown"])
def test_old_candidate_cannot_silently_run_new_policy_or_pending_partial(tmp_path, monkeypatch, reason):
    monkeypatch.delenv("MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS", raising=False)
    state = _state(tmp_path, f"{UID}:solana" if reason == "config" else "")
    if reason == "open":
        _open_profile(state)
    elif reason not in ("config", "open"):
        with sqlite3.connect(state / "sniper_tracker.db") as conn:
            conn.execute("INSERT INTO memecoin_manual_exit_intents VALUES ('source_partial',?,?)",
                         (reason, '{"source_sold_atomic":"1","source_pre_atomic":"4"}'))
    candidate = _old_candidate(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    assert verify.source_mirror_capability_errors(candidate, state)


def test_actual_candidate_profile_intent_and_recovery_capability_pass_without_state_write(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS", raising=False)
    state = _state(tmp_path, f"{UID}:solana")
    _open_profile(state)
    before = (state / "sniper_tracker.db").read_bytes()
    assert verify.source_mirror_capability_errors(ROOT, state) == []
    assert (state / "sniper_tracker.db").read_bytes() == before


def test_current_checkout_cannot_shadow_an_incapable_target(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS", raising=False)
    state = _state(tmp_path, f"{UID}:solana")
    candidate = tmp_path / "shadowed"
    candidate.mkdir()
    (candidate / "sniper").symlink_to(ROOT / "sniper", target_is_directory=True)
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    assert verify.source_mirror_capability_errors(candidate, state)


@pytest.mark.parametrize("terminal", ["done", "refused", "expired"])
def test_closed_profiles_and_terminal_source_requests_do_not_require_capability(tmp_path, monkeypatch, terminal):
    monkeypatch.delenv("MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS", raising=False)
    state = _state(tmp_path)
    _open_profile(state, opened=False)
    with sqlite3.connect(state / "sniper_tracker.db") as conn:
        conn.execute("INSERT INTO memecoin_manual_exit_intents VALUES ('source_partial',?,NULL)", (terminal,))
    monkeypatch.setattr(verify.subprocess, "run", lambda *_a, **_k: pytest.fail("unneeded candidate probe"))
    assert verify.source_mirror_capability_errors(_old_candidate(tmp_path), state) == []


def test_unreadable_state_or_malformed_active_configuration_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS", raising=False)
    state = _state(tmp_path, "missing-chain")
    assert verify.source_mirror_capability_errors(ROOT, state)
    (state / "sniper_tracker.db").write_bytes(b"invalid database")
    assert verify.source_mirror_capability_errors(ROOT, state)


def test_release_verification_blocks_incapable_candidate_before_context_construction(tmp_path, monkeypatch):
    from test_memecoin_runtime_verify import _repo, _manifest

    monkeypatch.delenv("MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS", raising=False)
    state = _state(tmp_path, f"{UID}:solana")
    (state / "data").mkdir()
    (state / "logs").mkdir()
    candidate, sha = _repo(tmp_path)
    result = verify.verify_release(candidate, state, _manifest(candidate, state, sha),
                                   config_validator=lambda *_a: pytest.fail("context construction ran"))
    assert not result.ok
    assert "source_mirror_candidate_incapable" in result.errors


def test_guard_reads_the_configured_ledger_instead_of_an_empty_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS", raising=False)
    state = _state(tmp_path)
    _open_profile(state)
    actual = tmp_path / "actual.db"
    (state / "sniper_tracker.db").rename(actual)
    (state / "sniper_tracker.db").touch()
    with (state / ".env").open("a") as handle:
        handle.write(f"DB_PATH={actual}\n")
    assert verify.source_mirror_capability_errors(_old_candidate(tmp_path), state)


@pytest.mark.parametrize("relative", ["sniper_tracker.db", "ledgers/custom.db"])
def test_relative_configured_ledger_uses_state_root_with_separate_candidate(tmp_path, monkeypatch, relative):
    monkeypatch.delenv("MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS", raising=False)
    state = _state(tmp_path)
    _open_profile(state)
    actual = state / relative
    actual.parent.mkdir(parents=True, exist_ok=True)
    if actual != state / "sniper_tracker.db":
        (state / "sniper_tracker.db").rename(actual)
    with (state / ".env").open("a") as handle:
        handle.write(f"DB_PATH='{relative}'\n")
    candidate = _old_candidate(tmp_path)
    decoy = candidate / relative
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.touch()
    before = actual.read_bytes()
    assert verify.source_mirror_capability_errors(candidate, state) == ["source_mirror_candidate_incapable"]
    assert actual.read_bytes() == before
    assert verify.source_mirror_capability_errors(ROOT, state) == []
    assert actual.read_bytes() == before
