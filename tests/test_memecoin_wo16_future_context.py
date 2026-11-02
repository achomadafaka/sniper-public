import json
import sqlite3

from sniper.persistence import db_exit_state as xs
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_holder_evidence as he


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _write(c, uid, state, observed_at, evidence_hash, *, remaining=None):
    m.upsert_holder_evidence(
        c, network_id="4663", mint="coin", source_user_id=uid, source_handle=uid,
        evidence_state=state, remaining_usd=remaining, observed_at=observed_at,
        source="fixture", upstream_event_id=evidence_hash, confirmation_state="explicit_post_trade",
        successful_poll_at=observed_at, invalidated_at=None, invalidation_reason=None,
        updated_at=observed_at, clock_domain="observed_real_utc", source_event_at=observed_at,
        source_timestamp_trust="claimed_untrusted", evidence_hash=evidence_hash)


def test_holder_history_is_append_only_idempotent_and_point_in_time():
    c = _conn()
    _write(c, "source", "positive", "2026-07-25T00:00:00+00:00", "h1", remaining=50)
    _write(c, "source", "zero", "2026-07-25T01:00:00+00:00", "h2", remaining=0)
    _write(c, "source", "zero", "2026-07-25T01:00:00+00:00", "h2", remaining=0)

    assert c.execute("SELECT COUNT(*) FROM memecoin_holder_evidence_history").fetchone()[0] == 2
    before = he.holder_evidence_at(
        c, network_id="4663", mint="coin", as_of="2026-07-25T00:30:00+00:00",
        max_age_s=3600)
    after = he.holder_evidence_at(
        c, network_id="4663", mint="coin", as_of="2026-07-25T01:30:00+00:00",
        max_age_s=3600)
    assert before[0]["evidence_state"] == "positive"
    assert after[0]["evidence_state"] == "zero"


def test_future_context_uses_only_prior_fresh_history_and_blocks_missing_sol_feed():
    c = _conn()
    _write(c, "source", "zero", "2026-07-25T00:00:00+00:00", "s", remaining=0)
    _write(c, "other", "positive", "2026-07-25T00:00:10+00:00", "o", remaining=25)
    _write(c, "future", "positive", "2026-07-25T00:02:00+00:00", "f", remaining=30)

    context = he.future_context_at(
        c, {"network_id": "4663", "mint": "coin", "source_user_id": "source"},
        decision_at="2026-07-25T00:01:00+00:00", max_age_s=120)

    assert context["source_wallet_exit_state"]["state"] == "zero"
    assert context["trusted_coholders"] == {"positive": 1, "zero": 0, "unknown": 0}
    assert context["sol_market_context"]["status"] == "blocked_missing_feed"
    assert context["observation_only"] is True


def test_future_context_lands_on_same_candidate_decision_row():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    xs.ensure_exit_state_tables(c)
    context = {"observation_only": True, "sol_market_context": {"status": "blocked_missing_feed"}}
    xs.write_candidate_decision(c, {
        "position_id": 7, "decided_at": "t", "engine_version": "v5",
        "feature_version": "f", "candidate_action": "RIDE", "future_context": context,
    })

    row = c.execute("SELECT future_context_json FROM memecoin_exit_candidate_decisions").fetchone()
    assert json.loads(row[0]) == context


def test_future_context_has_no_decision_rule_reader():
    import subprocess
    hits = subprocess.run(
        ["grep", "-rn", "holder_evidence_history\\|future_context_json",
         "sniper/services/memecoin_exit_brain.py", "sniper/analysis/exit_flex_v5.py",
         "sniper/analysis/exit_controller.py"], capture_output=True, text=True).stdout
    assert hits == ""
