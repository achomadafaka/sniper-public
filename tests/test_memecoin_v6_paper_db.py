"""Task 3 — isolated V6 live-paper ledger: schema isolation, immutable dedup, atomic filled/unfilled
action application, reconstruction, and censoring. Exercises the real Task 1 engine end-to-end so the
ledger is verified against genuine V6Decision/observation objects, not mocks.
"""
from dataclasses import replace
from datetime import datetime, timedelta
import re
import sqlite3

import pytest

from sniper.persistence import db_memecoin_v6_paper as db
from sniper.services.memecoin_v6_engine import (
    EvidenceIdentity, V6Action, V6Decision, V6Observation, V6PositionState, fingerprint,
    make_observation,
    policy_document, policy_hash, step,
)
from sniper.services.memecoin_v6_position_policy import V6State
from sniper.services.memecoin_v6_events import V6MarketEvent, event_identity

NOW = "2026-07-26T12:00:00+00:00"
LATER = "2026-07-26T12:00:05+00:00"

APP_TABLES = {
    "v6_paper_signal_attempts", "v6_paper_entries", "v6_paper_positions",
    "v6_paper_observations", "v6_paper_decisions", "v6_paper_action_intents",
    "v6_paper_fills", "v6_paper_cursors", "v6_paper_feed_health",
    "v6_paper_latency_samples",
    "v6_paper_market_events",
    "v6_paper_exit_followup_schedules", "v6_paper_exit_followup_results",
    "v6_paper_admission_queue", "v6_paper_entry_quote_attempts",
    "v6_paper_admission_rejections",
    "v6_paper_solana_arrivals",
}


def _conn(tmp_path):
    return db.connect(str(tmp_path / "v6.sqlite"))


def _sol_arrival(signature="sig-1", *, pool="VaultA", slot=123):
    return {
        "pool": pool, "signature": signature, "slot": slot, "mint": "MintA",
        "binding": {"pool": pool, "coin_vault": pool, "mint": "MintA"},
        "received_at": NOW,
    }


def test_solana_arrival_queue_is_immutable_ordered_and_restart_safe(tmp_path):
    path = str(tmp_path / "v6.sqlite")
    conn = db.connect(path)
    assert db.enqueue_solana_arrivals(conn, (_sol_arrival("sig-2", slot=124),)) == 1
    assert db.enqueue_solana_arrivals(conn, (_sol_arrival("sig-1", slot=123),)) == 1
    assert db.enqueue_solana_arrivals(conn, (_sol_arrival("sig-1", slot=123),)) == 0
    with pytest.raises(db.ImmutableConflict):
        db.enqueue_solana_arrivals(conn, (_sol_arrival("sig-1", slot=999),))
    conn.close()

    reopened = db.connect(path)
    pending = db.due_solana_arrivals(reopened, NOW, limit=10)
    assert [(row.slot, row.signature) for row in pending] == [(123, "sig-1")]


def test_solana_arrival_retry_and_resolution_are_durable(tmp_path):
    conn = _conn(tmp_path)
    arrival = _sol_arrival()
    db.enqueue_solana_arrivals(conn, (arrival, _sol_arrival("sig-2", slot=124)))
    assert db.retry_solana_arrival(
        conn, "VaultA", "sig-1", error="SolanaTransactionUnavailable",
        attempted_at=NOW, next_attempt_at=LATER)
    # A later signature in the same pool cannot pass an unresolved earlier frontier.
    assert db.due_solana_arrivals(conn, NOW, limit=10) == ()
    due = db.due_solana_arrivals(conn, LATER, limit=10)
    assert due[0].attempt_count == 1 and due[0].last_error == "SolanaTransactionUnavailable"
    assert db.pending_solana_pools(conn) == ("VaultA",)
    assert db.resolve_solana_arrival(
        conn, "VaultA", "sig-1", outcome="no_relevant_event", resolved_at=LATER)
    assert [row.signature for row in db.due_solana_arrivals(conn, LATER, limit=10)] == ["sig-2"]
    assert db.resolve_solana_arrival(
        conn, "VaultA", "sig-1", outcome="no_relevant_event", resolved_at=LATER) is False


def _entry(entry_id="e1", token_qty=1_000_000, **over):
    quote_payload = {
        "provider": "jupiter", "inAmount": 25_000_000, "outAmount": token_qty,
        "route": "raydium:poolX",
    }
    base = {
        "entry_id": entry_id,
        "origin_event_id": f"evt-{entry_id}",
        "network_id": "solana",
        "mint": "MINT" + entry_id,
        "ticker": "PEPE",
        "source_wallet_id": "w1",
        "source_wallet_handle": "@w1",
        "signal_source": "fomo",
        "notional_usd_micros": 25_000_000,
        "entry_price": "0.000042",
        "quote_in_asset": "USDC", "quote_in_atomic": 25_000_000, "quote_in_decimals": 6,
        "quote_out_asset": "MINT", "quote_out_atomic": token_qty, "quote_out_decimals": 6,
        "token_qty_atomic": token_qty,
        "route_target": "raydium", "pool_or_vault": "poolX",
        "quote_id": "q1", "quote_provider": "jupiter", "quote_age_s": 1.5,
        "quote_payload": quote_payload,
        "quote_fingerprint": fingerprint(quote_payload),
        "route_binding": {"provider": "jupiter", "pool_or_vault": "poolX"},
        "costs": {"known_fees_usd_micros": 0, "unknown_components": []},
        "net_pnl_computable": True,
        "entry_mcap": None, "entry_liquidity": None,
        "signal_ts": NOW, "quote_ts": NOW, "fill_ts": NOW,
        "policy_hash": policy_hash(), "policy_document": policy_document(),
    }
    base.update(over)
    return base


def _exit_fill(req, out=9_800_000, fp="rqfp"):
    quote_payload = {
        "provider": "jupiter", "inAmount": req, "outAmount": out,
        "route": "raydium:poolX",
    }
    return {
        "token_qty_atomic": req,
        "quote_in_asset": "MINT", "quote_in_atomic": req, "quote_in_decimals": 6,
        "quote_out_asset": "USDC", "quote_out_atomic": out, "quote_out_decimals": 6,
        "quote_fingerprint": fingerprint(quote_payload) if fp == "rqfp" else fp,
        "quote_provider": "jupiter",
        "quote_payload": quote_payload,
        "route_binding": {"provider": "jupiter", "pool_or_vault": "poolX"},
        "costs": {"known_fees_usd_micros": 1000, "unknown_components": []},
        "net_pnl_computable": True,
        "quote_age_s": 2.0, "unit_price": "0.0245",
        "observed_at": NOW,
    }


def _hand_decision(state, fraction_ppm, action, token, *, input_fingerprint=None):
    """Build a V6Decision consistent with the ledger's fraction math, for a chosen fraction the
    seed engine would not itself emit (e.g. 40%)."""
    remaining = state.remaining_qty_atomic
    requested = (remaining * fraction_ppm) // 1_000_000
    filled = replace(state, remaining_qty_atomic=remaining - requested,
                     pending_action_token=None, last_action_outcome="filled")
    unfilled = replace(state, pending_action_token=token, last_action_outcome="unfilled")
    return V6Decision(
        action=action, fraction_ppm=fraction_ppm, action_token=token,
        state_if_filled=filled, state_if_unfilled=unfilled,
        reason_codes=("test",), thought="t",
        input_fingerprint=input_fingerprint or "fp-" + token,
        policy_hash=policy_hash(),
    )


def _complete_payload(**changes):
    payload = {
        "executable_price": 0.9, "chain_vol_p90": 0.18, "route_degraded": False,
        "source_wallet_exited": False, "wallet_tier_adjustment": 0, "is_break": False,
        "held_s": 4 * 3600.0, "arrival_rate": 0.0, "buy_share": 0.0,
        "realized_vol": 0.01, "prior_low": 0.95, "price": 0.9, "net": 1.0,
    }
    payload.update(changes)
    return payload


def _persist_hand_decision(conn, state, fraction_ppm, action, token):
    obs = make_observation(
        observation_id="obs-" + token, observed_at=NOW, network_id="solana",
        payload=_complete_payload(),
    )
    db.append_observation(conn, state.position_id, obs, block_identity="slot:" + token,
                          capture_version="v1")
    decision = _hand_decision(
        state, fraction_ppm, action, token, input_fingerprint=obs.input_fingerprint,
    )
    db.append_decision(conn, state.position_id, decision)
    return decision


# ---- Step 1: schema + isolation --------------------------------------------------------------

def test_schema_has_exact_app_tables(tmp_path):
    conn = _conn(tmp_path)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert names == APP_TABLES


def test_schema_v9_uses_compact_decisions_exact_atomics_and_open_coin_index(tmp_path):
    conn = _conn(tmp_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
    columns = {row[1] for row in conn.execute("PRAGMA table_info(v6_paper_decisions)")}
    assert {"state_before_hash", "state_after_hash", "state_delta_json",
            "semantic_transition"} <= columns
    assert "state_before_json" not in columns
    assert "state_if_filled_json" not in columns
    assert "state_if_unfilled_json" not in columns
    for table, names in {
        "v6_paper_entries": {"quote_in_atomic", "quote_out_atomic", "token_qty_atomic"},
        "v6_paper_positions": {"entry_qty_atomic", "remaining_qty_atomic"},
        "v6_paper_fills": {"token_qty_atomic", "quote_in_atomic", "quote_out_atomic"},
    }.items():
        declared = {row[1]: row[2] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert all(declared[name] == "TEXT" for name in names)
    index_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_v6_positions_open_coin'",
    ).fetchone()[0]
    assert "network_id, mint" in index_sql
    assert "status='open'" in index_sql


def test_entry_and_reconstruction_preserve_atomics_above_sqlite_int64(tmp_path):
    conn = _conn(tmp_path)
    huge = 10 ** 30 + 123456789
    position = db.record_entry_fill(conn, _entry(token_qty=huge))

    raw = conn.execute(
        "SELECT token_qty_atomic,typeof(token_qty_atomic) FROM v6_paper_entries"
    ).fetchone()
    assert tuple(raw) == (str(huge), "text")
    assert position.entry_qty_atomic == huge
    assert position.remaining_qty_atomic == huge
    assert db.reconstruct_position(conn, position.entry_id).remaining_qty_atomic == huge

    decision = _persist_hand_decision(
        conn, position.engine_state, 400_000, V6Action.DERISK, "huge-action")
    requested = huge * 400_000 // 1_000_000
    after = db.apply_action_fill(conn, decision, _exit_fill(requested))
    assert after.remaining_qty_atomic == huge - requested
    assert db.reconstruct_position(conn, position.entry_id).remaining_qty_atomic == huge - requested


def test_connect_migrates_v2_integer_atomics_without_changing_rows(tmp_path):
    path = str(tmp_path / "v6.sqlite")
    conn = db.connect(path)
    expected = db.record_entry_fill(conn, _entry(token_qty=123456789))
    conn.close()

    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute("BEGIN IMMEDIATE")
    columns = db._ATOMIC_COLUMNS
    for table, atomics in columns.items():
        ddl = raw.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
        old = table + "__v2"
        ddl = re.sub(rf"^CREATE TABLE\s+{table}", f"CREATE TABLE {old}", ddl, count=1)
        for name in atomics:
            ddl = re.sub(rf"(\b{name}\s+)TEXT\b", rf"\1INTEGER", ddl, count=1)
        raw.execute(ddl)
        names = [row[1] for row in raw.execute(f"PRAGMA table_info({table})")]
        joined = ",".join(names)
        raw.execute(f"INSERT INTO {old} ({joined}) SELECT {joined} FROM {table}")
    for table in ("v6_paper_fills", "v6_paper_positions", "v6_paper_entries"):
        raw.execute(f"DROP TABLE {table}")
    for table in ("v6_paper_entries", "v6_paper_positions", "v6_paper_fills"):
        raw.execute(f"ALTER TABLE {table}__v2 RENAME TO {table}")
    raw.execute("PRAGMA user_version=2")
    raw.execute("COMMIT")
    raw.close()

    migrated = db.connect(path)
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == 10
    assert db.open_positions(migrated) == [expected]
    assert migrated.execute(
        "SELECT typeof(token_qty_atomic) FROM v6_paper_entries").fetchone()[0] == "text"


def test_market_event_batch_is_immutable_deduped_and_reconstructable(tmp_path):
    conn = _conn(tmp_path)
    event = V6MarketEvent(
        network_id="4663", pool="0x" + "ab" * 32, block_height=123,
        block_hash="0xblock", tx_id="0xtx", event_index=4,
        source_event_at=NOW, received_at=LATER, kind="v3_swap",
        payload={"amount0": "10", "amount1": "-9", "tick": 1},
    )

    assert db.append_market_events(conn, (event, event)) == 1
    assert db.append_market_events(conn, (event,)) == 0
    assert db.append_market_events(conn, (replace(
        event, received_at="2026-07-26T12:00:10+00:00"),)) == 0
    assert db.market_events(conn, "4663", event.pool, after=NOW) == (event,)
    row = conn.execute(
        "SELECT event_identity FROM v6_paper_market_events").fetchone()
    assert row[0] == event_identity(event)

    changed = replace(event, payload={"amount0": "11", "amount1": "-9", "tick": 1})
    # A changed payload creates a changed semantic identity; exact chain coordinates remain unique.
    with pytest.raises(db.ImmutableConflict):
        db.append_market_events(conn, (changed,))


def test_append_transition_is_atomic_and_skips_repeated_semantic_noop(tmp_path):
    conn = _conn(tmp_path)
    position = db.record_entry_fill(conn, _entry())
    observation = make_observation(
        observation_id="obs-semantic-1", observed_at=NOW, network_id="solana",
        payload=_complete_payload(), evidence=EvidenceIdentity(market="slot:123"),
    )

    first = step(position.engine_state, observation)
    assert first.semantic_transition is True
    assert db.append_transition(conn, position.entry_id, observation, first) is True

    current = db.open_positions(conn)[0]
    second = step(current.engine_state, observation)
    assert second.semantic_transition is False
    assert db.append_transition(conn, position.entry_id, observation, second) is False
    assert conn.execute("SELECT COUNT(*) FROM v6_paper_observations").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM v6_paper_decisions").fetchone()[0] == 1
    assert db.reconstruct_position(conn, position.entry_id).engine_state == current.engine_state


def test_market_evidence_can_evolve_a_to_b_to_a_without_immutable_conflict(tmp_path):
    conn = _conn(tmp_path)
    position = db.record_entry_fill(conn, _entry())
    observations = (
        make_observation(
            observation_id="obs-A-1", observed_at="2026-07-26T12:00:00+00:00",
            network_id="solana", payload=_complete_payload(),
            evidence=EvidenceIdentity(market="market-A", quote="quote-A"),
        ),
        make_observation(
            observation_id="obs-B-2", observed_at="2026-07-26T12:00:01+00:00",
            network_id="solana", payload=_complete_payload(price=0.91),
            evidence=EvidenceIdentity(market="market-B", quote="quote-B"),
        ),
        make_observation(
            observation_id="obs-A-3", observed_at="2026-07-26T12:00:02+00:00",
            network_id="solana", payload=_complete_payload(),
            evidence=EvidenceIdentity(market="market-A", quote="quote-A"),
        ),
    )

    for observation in observations:
        current = db.open_positions(conn)[0]
        assert db.append_transition(
            conn, position.entry_id, observation, step(current.engine_state, observation)
        ) is True

    assert [row[0] for row in conn.execute(
        "SELECT observation_id FROM v6_paper_observations ORDER BY observed_at"
    )] == ["obs-A-1", "obs-B-2", "obs-A-3"]


def test_append_transition_rolls_back_observation_when_decision_is_invalid(tmp_path):
    conn = _conn(tmp_path)
    position = db.record_entry_fill(conn, _entry())
    observation = make_observation(
        observation_id="obs-rollback", observed_at=NOW, network_id="solana",
        payload=_complete_payload(), evidence=EvidenceIdentity(market="slot:rollback"),
    )
    invalid = replace(step(position.engine_state, observation), policy_hash="0" * 64)

    with pytest.raises(ValueError, match="policy_hash"):
        db.append_transition(conn, position.entry_id, observation, invalid)

    assert conn.execute("SELECT COUNT(*) FROM v6_paper_observations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM v6_paper_decisions").fetchone()[0] == 0


def test_action_intent_retry_is_bounded_and_fill_reconstructs(tmp_path):
    assert [db.retry_delay_s(i) for i in range(8)] == [0, 1, 2, 5, 10, 30, 30, 30]
    conn = _conn(tmp_path)
    position = db.record_entry_fill(conn, _entry())
    observation = make_observation(
        observation_id="obs-intent", observed_at=NOW, network_id="solana",
        payload=_complete_payload(
            executable_price=0.8, price=0.8, prior_low=0.9, held_s=10.0,
            is_break=True, net=-1.0,
        ),
        evidence=EvidenceIdentity(market="slot:intent"),
    )
    decision = step(position.engine_state, observation)
    assert decision.action is V6Action.DERISK
    assert db.append_transition(conn, position.entry_id, observation, decision) is True
    intent = db.active_intent(conn, position.entry_id)
    assert intent is not None and intent.token == decision.action_intent_token
    assert db.unapplied_decisions(conn) == []

    assert db.claim_retry(conn, intent.token, NOW) is True
    assert db.claim_retry(conn, intent.token, NOW) is False
    assert db.record_intent_unfilled(conn, intent.token, "quote_unavailable", NOW) is True
    assert db.claim_retry(conn, intent.token, "2026-07-26T12:00:00.500+00:00") is False
    assert db.claim_retry(conn, intent.token, "2026-07-26T12:00:01+00:00") is True

    requested = (position.remaining_qty_atomic * decision.fraction_ppm) // 1_000_000
    assert db.complete_intent_fill(conn, intent.token, _exit_fill(requested)) is True
    assert db.complete_intent_fill(conn, intent.token, _exit_fill(requested)) is False
    assert db.active_intent(conn, position.entry_id) is None
    applied = conn.execute(
        "SELECT applied_outcome,applied_at FROM v6_paper_decisions WHERE action_token=?",
        (intent.token,),
    ).fetchone()
    assert tuple(applied) == ("filled", NOW)
    reconstructed = db.reconstruct_position(conn, position.entry_id)
    assert reconstructed.remaining_qty_atomic == position.remaining_qty_atomic - requested


def test_v9_migration_backfills_filled_decision_truth(tmp_path):
    path = str(tmp_path / "v6.sqlite")
    conn = db.connect(path)
    position = db.record_entry_fill(conn, _entry())
    observation = make_observation(
        observation_id="obs-v9-fill", observed_at=NOW, network_id="solana",
        payload=_complete_payload(
            executable_price=0.8, price=0.8, prior_low=0.9, is_break=True, net=-1.0),
        evidence=EvidenceIdentity(market="slot:v9-fill"),
    )
    decision = step(position.engine_state, observation)
    db.append_transition(conn, position.entry_id, observation, decision)
    intent = db.active_intent(conn, position.entry_id)
    requested = (position.remaining_qty_atomic * decision.fraction_ppm) // 1_000_000
    db.complete_intent_fill(conn, intent.token, _exit_fill(requested))
    conn.execute(
        "UPDATE v6_paper_decisions SET applied_outcome='unfilled',applied_at=NULL "
        "WHERE action_token=?", (intent.token,))
    conn.execute("DROP TABLE v6_paper_solana_arrivals")
    conn.execute("PRAGMA user_version=8")
    conn.close()

    migrated = db.connect(path)

    row = migrated.execute(
        "SELECT applied_outcome,applied_at FROM v6_paper_decisions WHERE action_token=?",
        (intent.token,),
    ).fetchone()
    assert tuple(row) == ("filled", NOW)


def test_recovery_hold_persists_intent_cancellation_before_retry(tmp_path):
    conn = _conn(tmp_path)
    position = db.record_entry_fill(conn, _entry())
    broken = make_observation(
        observation_id="obs-break-intent", observed_at=NOW, network_id="solana",
        payload=_complete_payload(
            executable_price=0.8, price=0.8, prior_low=0.9, held_s=20_000.0,
            is_break=True, net=-1.0, arrival_rate=0.0, buy_share=0.0,
            realized_vol=0.01,
        ),
        evidence=EvidenceIdentity(market="slot:break", quote="quote:break"),
    )
    break_decision = step(position.engine_state, broken)
    db.append_transition(conn, position.entry_id, broken, break_decision)
    token = db.active_intent(conn, position.entry_id).token

    current = db.open_positions(conn)[0]
    recovery = make_observation(
        observation_id="obs-recovery-intent", observed_at=LATER, network_id="solana",
        payload=_complete_payload(
            executable_price=0.95, price=0.95, prior_low=0.9, held_s=20_001.0,
            is_break=False, net=1.0, arrival_rate=0.0, buy_share=0.0,
            realized_vol=0.01, quote_status="healthy",
        ),
        evidence=EvidenceIdentity(market="slot:recovery", quote="quote:recovery"),
    )
    recovery_decision = step(current.engine_state, recovery)

    assert db.append_transition(conn, position.entry_id, recovery, recovery_decision) is True
    assert db.active_intent(conn, position.entry_id) is None
    stored = conn.execute(
        "SELECT status,reason FROM v6_paper_action_intents WHERE action_token=?", (token,),
    ).fetchone()
    assert tuple(stored) == ("superseded", "trigger_invalidated")


def test_action_fill_schedules_exact_post_exit_followups(tmp_path):
    conn = _conn(tmp_path)
    position = db.record_entry_fill(conn, _entry())
    decision = _persist_hand_decision(
        conn, position.engine_state, 250_000, V6Action.DERISK, "followup-action")
    db.apply_action_fill(conn, decision, _exit_fill(250_000))

    rows = conn.execute(
        "SELECT action_token,horizon_s,due_at,token_qty_atomic FROM "
        "v6_paper_exit_followup_schedules ORDER BY horizon_s"
    ).fetchall()
    assert [row["horizon_s"] for row in rows] == [30, 120, 300, 900, 3600, 10800, 43200]
    assert {row["action_token"] for row in rows} == {"followup-action"}
    assert {row["token_qty_atomic"] for row in rows} == {"250000"}
    start = datetime.fromisoformat(NOW)
    assert [datetime.fromisoformat(row["due_at"]) - start for row in rows] == [
        timedelta(seconds=value) for value in (30, 120, 300, 900, 3600, 10800, 43200)
    ]


def test_due_followup_and_result_are_restart_safe_and_immutable(tmp_path):
    path = str(tmp_path / "v6.sqlite")
    conn = db.connect(path)
    position = db.record_entry_fill(conn, _entry())
    decision = _persist_hand_decision(
        conn, position.engine_state, 250_000, V6Action.DERISK, "followup-restart")
    db.apply_action_fill(conn, decision, _exit_fill(250_000))

    assert db.due_exit_followup(conn, "2026-07-26T12:00:29+00:00") is None
    due = db.due_exit_followup(conn, "2026-07-26T12:00:30+00:00")
    assert due.action_token == "followup-restart"
    assert due.horizon_s == 30 and due.token_qty_atomic == 250_000
    conn.close()

    restarted = db.connect(path)
    assert db.due_exit_followup(restarted, "2026-07-26T12:00:30+00:00") == due
    fill = _exit_fill(250_000, out=10_000_000)
    assert db.record_exit_followup_result(restarted, due.followup_id, fill, LATER) is True
    assert db.record_exit_followup_result(restarted, due.followup_id, fill, LATER) is False
    with pytest.raises(db.ImmutableConflict):
        db.record_exit_followup_result(
            restarted, due.followup_id, _exit_fill(250_000, out=10_000_001), LATER)
    assert db.due_exit_followup(restarted, "2026-07-26T12:00:30+00:00") is None


def test_ddl_carries_no_credential_or_funded_columns(tmp_path):
    conn = _conn(tmp_path)
    ddl = "\n".join(r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
    for forbidden in ("private_key", "signed_payload", "tx_sig", "reservation"):
        assert forbidden not in ddl.lower()


def test_connect_existing_is_readonly_and_requires_file(tmp_path):
    path = str(tmp_path / "ro.sqlite")
    with pytest.raises((FileNotFoundError, Exception)):
        db.connect_existing(path)                      # absent -> fails
    db.connect(path).close()                           # create it
    ro = db.connect_existing(path)
    with pytest.raises(Exception):
        ro.execute("INSERT INTO v6_paper_cursors(name, value, corrected_at) VALUES ('x','1',?)",
                   (NOW,))


# ---- Step 4/5: immutable dedup ---------------------------------------------------------------

def test_signal_attempt_idempotent_and_conflict(tmp_path):
    conn = _conn(tmp_path)
    kw = dict(origin_event_id="evt1", network_id="solana", mint="M", outcome="quote_stale",
              attempted_at=NOW)
    db.record_signal_attempt(conn, **kw)
    db.record_signal_attempt(conn, **kw)               # exact redelivery ok
    with pytest.raises(db.ImmutableConflict):
        db.record_signal_attempt(conn, **{**kw, "outcome": "capacity_full"})


def _admission(origin, source_position_id, *, received_at=NOW, mint="MintQueue"):
    return {
        "origin_event_id": origin, "source_position_id": source_position_id,
        "network_id": "solana", "mint": mint, "received_at": received_at,
        "signal": {"origin_event_id": origin, "source_event_id": source_position_id,
                   "network_id": "solana", "mint": mint},
    }


def test_admission_queue_deduplicates_origin_and_rejects_conflicting_identity(tmp_path):
    conn = _conn(tmp_path)
    assert db.enqueue_admission(conn, _admission("evt-q1", 10), NOW) is True
    assert db.enqueue_admission(conn, _admission("evt-q1", 10), NOW) is False
    with pytest.raises(db.ImmutableConflict):
        db.enqueue_admission(conn, _admission("evt-q1", 11, mint="Other"), NOW)
    assert conn.execute("SELECT COUNT(*) FROM v6_paper_admission_queue").fetchone()[0] == 1


def test_admission_claims_fill_free_capacity_oldest_first(tmp_path):
    conn = _conn(tmp_path)
    db.enqueue_admission(conn, _admission("evt-q1", 10, mint="Mint1"), NOW)
    db.enqueue_admission(conn, _admission("evt-q2", 11, mint="Mint2"), NOW)
    db.enqueue_admission(conn, _admission("evt-q3", 12, mint="Mint3"), NOW)

    claims = db.claim_admissions(
        conn, NOW, max_claims=4, max_open=4, open_count=1, lease_s=15)
    assert [claim.origin_event_id for claim in claims] == ["evt-q1", "evt-q2", "evt-q3"]
    assert len({claim.claim_token for claim in claims}) == 3
    assert db.claim_admissions(
        conn, NOW, max_claims=4, max_open=4, open_count=1, lease_s=15) == ()


def test_admission_terminalizes_candidate_for_an_existing_open_coin(tmp_path):
    conn = _conn(tmp_path)
    db.record_entry_fill(conn, _entry("existing", mint="MintQueue"))
    db.enqueue_admission(conn, _admission("evt-new", 10, mint="MintQueue"), NOW)

    assert db.claim_admissions(
        conn, NOW, max_claims=4, max_open=4, open_count=1, lease_s=15) == ()
    row = conn.execute(
        "SELECT state,last_outcome,last_detail FROM v6_paper_admission_queue",
    ).fetchone()
    assert tuple(row) == (
        "terminal_unfilled", "duplicate_open_coin", "duplicate_open_coin",
    )


def test_admission_terminalizes_a_closed_coin_in_the_same_policy_cohort(tmp_path):
    conn = _conn(tmp_path)
    existing = _entry("closed", mint="MintLifecycle")
    db.record_entry_fill(conn, existing)
    conn.execute(
        "UPDATE v6_paper_positions SET status='closed',closed_at=? WHERE position_id=?",
        (NOW, existing["entry_id"]),
    )
    db.enqueue_admission(conn, _admission("evt-repeat", 10, mint="MintLifecycle"), NOW)

    assert db.claim_admissions(
        conn, NOW, max_claims=4, max_open=4, open_count=0, lease_s=15,
        cohort_policy_hash=existing["policy_hash"],
    ) == ()
    row = conn.execute(
        "SELECT state,last_outcome,last_detail FROM v6_paper_admission_queue",
    ).fetchone()
    assert tuple(row) == (
        "terminal_unfilled", "duplicate_policy_coin", "duplicate_policy_coin",
    )


def test_open_coin_terminalizes_a_retry_without_waiting_for_retry_due_time(tmp_path):
    conn = _conn(tmp_path)
    db.enqueue_admission(conn, _admission("evt-retry-duplicate", 10, mint="MintRetry"), NOW)
    claim = db.claim_admissions(
        conn, NOW, max_claims=1, max_open=4, open_count=0, lease_s=15)[0]
    assert db.record_entry_quote_result(
        conn, claim, outcome="quote_unavailable", completed_at=NOW,
    ) == "retry_wait"
    db.record_entry_fill(conn, _entry("existing-retry", mint="MintRetry"))

    assert db.claim_admissions(
        conn, NOW, max_claims=1, max_open=4, open_count=1, lease_s=15) == ()
    assert conn.execute(
        "SELECT state FROM v6_paper_admission_queue",
    ).fetchone()[0] == "terminal_unfilled"


def test_admission_claims_only_one_candidate_per_coin_at_a_time(tmp_path):
    conn = _conn(tmp_path)
    db.enqueue_admission(conn, _admission("evt-first", 10, mint="MintSame"), NOW)
    db.enqueue_admission(conn, _admission("evt-second", 11, mint="MintSame"), NOW)

    claims = db.claim_admissions(
        conn, NOW, max_claims=4, max_open=4, open_count=0, lease_s=15)

    assert [claim.origin_event_id for claim in claims] == ["evt-first"]
    assert conn.execute(
        "SELECT state FROM v6_paper_admission_queue WHERE origin_event_id='evt-second'",
    ).fetchone()[0] == "queued"


def test_admission_fill_race_loser_becomes_terminal_without_second_position(tmp_path):
    conn = _conn(tmp_path)
    db.enqueue_admission(conn, _admission("evt-race", 10, mint="MintRace"), NOW)
    claim = db.claim_admissions(
        conn, NOW, max_claims=1, max_open=4, open_count=0, lease_s=15)[0]
    db.record_entry_fill(conn, _entry("winner", mint="MintRace"))

    position = db.finalize_admission_fill(
        conn, claim, _entry("loser", origin_event_id="evt-race", mint="MintRace"),
        completed_at=NOW, provider="jupiter",
    )

    assert position is None
    assert len(db.open_positions(conn)) == 1
    queue = conn.execute(
        "SELECT state,last_outcome,last_detail FROM v6_paper_admission_queue",
    ).fetchone()
    assert tuple(queue) == (
        "terminal_unfilled", "duplicate_open_coin", "duplicate_open_coin",
    )
    assert conn.execute(
        "SELECT outcome FROM v6_paper_entry_quote_attempts",
    ).fetchone()[0] == "duplicate_open_coin"


def test_admission_quote_failure_retries_then_becomes_terminal(tmp_path):
    conn = _conn(tmp_path)
    db.enqueue_admission(conn, _admission("evt-retry", 10), NOW)
    first = db.claim_admissions(
        conn, NOW, max_claims=1, max_open=4, open_count=0, lease_s=15)[0]
    state = db.record_entry_quote_result(
        conn, first, outcome="quote_unavailable", completed_at=NOW,
        detail="rpc_transport", provider="solana:jupiter")
    assert state == "retry_wait"
    row = conn.execute(
        "SELECT attempt_count,next_attempt_at,last_detail FROM v6_paper_admission_queue"
    ).fetchone()
    assert tuple(row) == (1, "2026-07-26T12:00:02+00:00", "rpc_transport")

    second = db.claim_admissions(
        conn, "2026-07-26T12:00:02+00:00", max_claims=1, max_open=4,
        open_count=0, lease_s=15)[0]
    assert db.record_entry_quote_result(
        conn, second, outcome="quote_unavailable",
        completed_at="2026-07-26T12:00:02+00:00", detail="rpc_transport") == "retry_wait"
    third = db.claim_admissions(
        conn, "2026-07-26T12:00:07+00:00", max_claims=1, max_open=4,
        open_count=0, lease_s=15)[0]
    assert db.record_entry_quote_result(
        conn, third, outcome="quote_unavailable",
        completed_at="2026-07-26T12:00:07+00:00", detail="rpc_transport") \
        == "terminal_unfilled"
    assert conn.execute(
        "SELECT COUNT(*) FROM v6_paper_entry_quote_attempts WHERE origin_event_id='evt-retry'"
    ).fetchone()[0] == 3


def test_obsolete_admission_claim_cannot_change_recovered_row(tmp_path):
    conn = _conn(tmp_path)
    db.enqueue_admission(conn, _admission("evt-lease", 10), NOW)
    old = db.claim_admissions(
        conn, NOW, max_claims=1, max_open=4, open_count=0, lease_s=1)[0]
    assert db.recover_admission_leases(conn, "2026-07-26T12:00:02+00:00") == 1
    replacement = db.claim_admissions(
        conn, "2026-07-26T12:00:04+00:00", max_claims=1, max_open=4,
        open_count=0, lease_s=15)[0]
    assert replacement.claim_token != old.claim_token
    with pytest.raises(db.StateConflict, match="obsolete admission claim"):
        db.record_entry_quote_result(
            conn, old, outcome="filled", completed_at="2026-07-26T12:00:04+00:00",
            detail="ok", entry_id="v6-late")


def test_lease_recovery_reconciles_a_legacy_partially_committed_fill(tmp_path):
    conn = _conn(tmp_path)
    entry = _entry()
    db.enqueue_admission(
        conn,
        _admission(
            entry["origin_event_id"], 10, mint=entry["mint"],
            received_at="2026-07-26T11:59:00+00:00",
        ),
        NOW,
    )
    db.claim_admissions(conn, NOW, max_claims=1, max_open=4, open_count=0, lease_s=1)
    db.record_signal_attempt(
        conn, origin_event_id=entry["origin_event_id"], network_id=entry["network_id"],
        mint=entry["mint"], outcome="filled", attempted_at=NOW, entry_id=entry["entry_id"],
    )
    db.record_entry_fill(conn, entry)

    assert db.recover_admission_leases(conn, "2026-07-26T12:00:02+00:00") == 1
    queue = conn.execute(
        "SELECT state,last_outcome,last_detail,entry_id FROM v6_paper_admission_queue"
    ).fetchone()
    assert tuple(queue) == ("filled", "filled", "reconciled_existing_entry", entry["entry_id"])
    assert conn.execute("SELECT COUNT(*) FROM v6_paper_entry_quote_attempts").fetchone()[0] == 0


def test_lease_recovery_preserves_an_existing_terminal_signal_summary(tmp_path):
    conn = _conn(tmp_path)
    db.enqueue_admission(conn, _admission("evt-terminal", 10), NOW)
    db.claim_admissions(conn, NOW, max_claims=1, max_open=4, open_count=0, lease_s=1)
    db.record_signal_attempt(
        conn, origin_event_id="evt-terminal", network_id="solana", mint="MintQueue",
        outcome="quote_stale", attempted_at=NOW,
        detail={"code": "admission_freshness_expired"},
    )

    assert db.recover_admission_leases(conn, "2026-07-26T12:00:02+00:00") == 1
    queue = conn.execute(
        "SELECT state,last_outcome,last_detail,entry_id FROM v6_paper_admission_queue"
    ).fetchone()
    assert tuple(queue) == (
        "terminal_unfilled", "quote_stale", "reconciled_terminal_summary", None,
    )
    assert conn.execute("SELECT COUNT(*) FROM v6_paper_entry_quote_attempts").fetchone()[0] == 0


def test_v5_database_migrates_admission_tables_through_v9(tmp_path):
    path = str(tmp_path / "v5.sqlite")
    conn = db.connect(path)
    conn.execute("PRAGMA user_version=5")
    conn.close()
    migrated = db.connect(path)
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == 10
    assert {row[1] for row in migrated.execute(
        "PRAGMA table_info(v6_paper_admission_queue)")} >= {
            "origin_event_id", "source_position_id", "state", "claim_token",
            "lease_expires_at", "next_attempt_at", "last_detail"}
    assert {row[1] for row in migrated.execute(
        "PRAGMA table_info(v6_paper_admission_rejections)")} >= {
            "source_position_id", "reason_code", "detail_json", "content_fingerprint"}


def test_v6_database_migrates_immutable_admission_rejections_to_v9(tmp_path):
    path = str(tmp_path / "v6.sqlite")
    conn = db.connect(path)
    conn.execute("DROP TABLE v6_paper_admission_rejections")
    conn.execute("PRAGMA user_version=6")
    conn.close()

    migrated = db.connect(path)

    assert migrated.execute("PRAGMA user_version").fetchone()[0] == 10
    assert migrated.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='v6_paper_admission_rejections'"
    ).fetchone() is not None


def test_admission_rejection_is_immutable_and_idempotent(tmp_path):
    conn = _conn(tmp_path)
    rejection = {
        "source_position_id": 91, "origin_event_id": None, "network_id": "ethereum",
        "mint": "MintBad", "reason_code": "unsupported_network",
        "detail": {"execution_mode": "paper"},
    }

    assert db.record_admission_rejection(conn, rejection, NOW) is True
    assert db.record_admission_rejection(conn, rejection, NOW) is False
    with pytest.raises(db.ImmutableConflict):
        db.record_admission_rejection(
            conn, {**rejection, "reason_code": "missing_event_provenance"}, NOW)


def test_filled_signal_attempt_remains_idempotent_after_entry_link(tmp_path):
    conn = _conn(tmp_path)
    kw = dict(origin_event_id="evt-e1", network_id="solana", mint="MINTe1",
              outcome="filled", attempted_at=NOW)
    db.record_signal_attempt(conn, **kw)
    db.record_entry_fill(conn, _entry())
    db.record_signal_attempt(conn, **kw)
    db.record_signal_attempt(conn, **kw, entry_id="e1")


def test_entry_cannot_link_a_nonfilled_signal_attempt(tmp_path):
    conn = _conn(tmp_path)
    db.record_signal_attempt(
        conn, origin_event_id="evt-e1", network_id="solana", mint="MINTe1",
        outcome="quote_stale", attempted_at=NOW,
    )
    with pytest.raises(db.StateConflict, match="signal attempt"):
        db.record_entry_fill(conn, _entry())


def test_entry_fill_creates_rows_links_attempt_and_dedups(tmp_path):
    conn = _conn(tmp_path)
    db.record_signal_attempt(conn, origin_event_id="evt-e1", network_id="solana", mint="MINTe1",
                             outcome="filled", attempted_at=NOW)
    pos = db.record_entry_fill(conn, _entry())
    assert pos.remaining_qty_atomic == 1_000_000
    assert pos.status == "open"
    assert conn.execute("SELECT COUNT(*) FROM v6_paper_entries").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM v6_paper_positions").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM v6_paper_fills WHERE leg='entry'").fetchone()[0] == 1
    # attempt linked to the entry
    assert conn.execute(
        "SELECT entry_id FROM v6_paper_signal_attempts WHERE origin_event_id='evt-e1'"
    ).fetchone()[0] == "e1"
    # exact redelivery returns the existing position, creates nothing new
    assert db.record_entry_fill(conn, _entry()) == pos
    assert conn.execute("SELECT COUNT(*) FROM v6_paper_positions").fetchone()[0] == 1
    # same identity, changed immutable content -> conflict
    with pytest.raises(db.ImmutableConflict):
        db.record_entry_fill(conn, _entry(entry_price="0.999"))
    # no decision until observation + decision appended
    assert db.position_timeline(conn, "e1")["decisions"] == []


def test_direct_entry_finalization_rolls_back_signal_when_entry_insert_fails(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "CREATE TRIGGER reject_direct_entry BEFORE INSERT ON v6_paper_positions "
        "BEGIN SELECT RAISE(ABORT,'injected direct entry failure'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected direct entry failure"):
        db.finalize_direct_entry_fill(conn, _entry(), attempted_at=NOW)

    assert conn.execute("SELECT COUNT(*) FROM v6_paper_signal_attempts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM v6_paper_entries").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM v6_paper_positions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM v6_paper_fills").fetchone()[0] == 0


def test_entry_fill_persists_canonical_quote_route_and_cost_evidence(tmp_path):
    conn = _conn(tmp_path)
    entry = _entry()
    db.record_entry_fill(conn, entry)
    stored_entry = conn.execute(
        "SELECT quote_fingerprint, quote_payload_json, route_binding_json, costs_json, "
        "net_pnl_computable FROM v6_paper_entries WHERE entry_id='e1'"
    ).fetchone()
    stored_fill = conn.execute(
        "SELECT route_quote_fingerprint, quote_payload_json, route_binding_json, costs_json, "
        "net_pnl_computable FROM v6_paper_fills WHERE position_id='e1' AND leg='entry'"
    ).fetchone()
    assert stored_entry["quote_fingerprint"] == entry["quote_fingerprint"]
    assert stored_entry["quote_payload_json"] == db.canonical_json(entry["quote_payload"])
    assert stored_entry["route_binding_json"] == db.canonical_json(entry["route_binding"])
    assert stored_entry["costs_json"] == db.canonical_json(entry["costs"])
    assert stored_entry["net_pnl_computable"] == 1
    assert stored_fill["route_quote_fingerprint"] == entry["quote_fingerprint"]


# ---- Step 6/7: atomic filled + unfilled ------------------------------------------------------

def test_apply_action_fill_reduces_inventory_and_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    pos = db.record_entry_fill(conn, _entry())
    dec = _persist_hand_decision(
        conn, pos.engine_state, 250_000, V6Action.DERISK, "tok-derisk-1")
    filled = db.apply_action_fill(conn, dec, _exit_fill(250_000))
    assert filled.remaining_qty_atomic == 750_000
    assert filled.status == "open"
    assert db.apply_action_fill(conn, dec, _exit_fill(250_000)) == filled   # idempotent
    assert db.fill_count(conn, "tok-derisk-1") == 1                          # exactly one fill


def test_apply_action_fill_rejects_mismatched_fill_quantity(tmp_path):
    conn = _conn(tmp_path)
    pos = db.record_entry_fill(conn, _entry())
    dec = _persist_hand_decision(conn, pos.engine_state, 250_000, V6Action.DERISK, "tok-x")
    with pytest.raises(ValueError):
        db.apply_action_fill(conn, dec, _exit_fill(249_999))   # fill != requested


def test_record_action_unfilled_preserves_inventory_and_state(tmp_path):
    conn = _conn(tmp_path)
    pos = db.record_entry_fill(conn, _entry())
    first = _persist_hand_decision(conn, pos.engine_state, 250_000, V6Action.DERISK, "tok-a")
    filled = db.apply_action_fill(
        conn, first, _exit_fill(250_000))
    nxt = _persist_hand_decision(
        conn, filled.engine_state, 500_000, V6Action.DERISK, "tok-b")
    unfilled = db.record_action_unfilled(conn, nxt, "quote_unavailable", NOW)
    assert unfilled.remaining_qty_atomic == 750_000
    assert unfilled.engine_state == nxt.state_if_unfilled
    assert db.fill_count(conn, "tok-b") == 0
    # idempotent unfilled
    assert db.record_action_unfilled(conn, nxt, "quote_unavailable", NOW) == unfilled


def test_apply_action_fill_rolls_back_on_injected_failure(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    pos = db.record_entry_fill(conn, _entry())
    dec = _persist_hand_decision(
        conn, pos.engine_state, 250_000, V6Action.DERISK, "tok-boom")

    def _boom(*a, **k):
        raise RuntimeError("injected after fill insert")

    monkeypatch.setattr(db, "_verify_post_fill", _boom)
    with pytest.raises(RuntimeError):
        db.apply_action_fill(conn, dec, _exit_fill(250_000))
    # everything rolled back
    assert db.fill_count(conn, "tok-boom") == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM v6_paper_action_intents WHERE action_token='tok-boom'"
    ).fetchone()[0] == 1
    reloaded = db.open_positions(conn)[0]
    assert reloaded.remaining_qty_atomic == 1_000_000
    assert reloaded.engine_state == pos.engine_state


# ---- Step 8: reconstruction, concurrency, censoring ------------------------------------------

def test_full_exit_closes_and_reconstruction_balances(tmp_path):
    conn = _conn(tmp_path)
    pos = db.record_entry_fill(conn, _entry())
    dec = _persist_hand_decision(
        conn, pos.engine_state, 1_000_000, V6Action.EXIT_FULL, "tok-full")
    closed = db.apply_action_fill(conn, dec, _exit_fill(1_000_000))
    assert closed.remaining_qty_atomic == 0
    assert closed.status == "closed"
    assert closed.terminal_reason
    recon = db.reconstruct_position(conn, "e1")
    # entry qty == remaining + applied exit qtys
    assert recon.remaining_qty_atomic == 0
    assert recon.entry_qty_atomic == recon.remaining_qty_atomic + recon.exited_qty_atomic


def test_two_positions_reconstruct_independently_after_restart(tmp_path):
    path = str(tmp_path / "v6.sqlite")
    conn = db.connect(path)
    a = db.record_entry_fill(conn, _entry("a", 1_000_000))
    b = db.record_entry_fill(conn, _entry("b", 2_000_000))
    da = _persist_hand_decision(conn, a.engine_state, 250_000, V6Action.DERISK, "a1")
    db.apply_action_fill(conn, da,
                         _exit_fill(250_000))
    db_obs = make_observation(
        observation_id="obs-b1", observed_at=NOW, network_id="solana",
        payload=_complete_payload(),
    )
    db.append_observation(conn, b.engine_state.position_id, db_obs, capture_version="v1")
    db_decision = _hand_decision(
        b.engine_state, 500_000, V6Action.DERISK, "b1",
        input_fingerprint=db_obs.input_fingerprint,
    )
    db.append_decision(conn, b.engine_state.position_id, db_decision)
    db.apply_action_fill(conn, db_decision,
                         _exit_fill(1_000_000))
    conn.close()

    restarted = db.connect(path)                    # simulate worker restart
    ra = db.reconstruct_position(restarted, "a")
    rb = db.reconstruct_position(restarted, "b")
    assert ra.remaining_qty_atomic == 750_000
    assert rb.remaining_qty_atomic == 1_000_000
    assert {p.entry_id for p in db.open_positions(restarted)} == {"a", "b"}


def test_censor_excludes_from_open_positions_without_fake_fill(tmp_path):
    conn = _conn(tmp_path)
    db.record_entry_fill(conn, _entry())
    fills_before = conn.execute("SELECT COUNT(*) FROM v6_paper_fills").fetchone()[0]
    censored = db.censor_position(conn, "e1", reason="broken experiment", at=NOW)
    assert censored.status == "censored_operator"
    assert conn.execute("SELECT COUNT(*) FROM v6_paper_fills").fetchone()[0] == fills_before
    assert db.open_positions(conn) == []             # excluded from engine outcomes


def test_censor_requires_reason_and_aware_timestamp(tmp_path):
    conn = _conn(tmp_path)
    db.record_entry_fill(conn, _entry())
    with pytest.raises(ValueError):
        db.censor_position(conn, "e1", reason=" short ", at=NOW)
    with pytest.raises(ValueError):
        db.censor_position(conn, "e1", reason="broken experiment", at="2026-07-26T12:00:00")


def test_censor_is_idempotent_only_for_the_same_trimmed_reason(tmp_path):
    conn = _conn(tmp_path)
    db.record_entry_fill(conn, _entry())
    first = db.censor_position(conn, "e1", reason="  broken experiment  ", at=NOW)
    replay = db.censor_position(conn, "e1", reason="broken experiment", at=LATER)
    assert replay == first
    row = conn.execute(
        "SELECT censor_reason, censored_at, updated_at FROM v6_paper_positions WHERE position_id='e1'"
    ).fetchone()
    assert tuple(row) == ("broken experiment", NOW, NOW)
    with pytest.raises(db.StateConflict):
        db.censor_position(conn, "e1", reason="different reason", at=LATER)


# ---- Real engine end-to-end + cursors --------------------------------------------------------

def test_real_engine_break_drives_a_ledger_derisk(tmp_path):
    conn = _conn(tmp_path)
    pos = db.record_entry_fill(conn, _entry())
    obs = make_observation(
        observation_id="o1", observed_at=NOW, network_id="solana",
        payload={
            "executable_price": 0.9, "chain_vol_p90": 0.18, "route_degraded": False,
            "source_wallet_exited": False, "wallet_tier_adjustment": 0, "is_break": True,
            "held_s": 4 * 3600.0, "arrival_rate": 0.0, "buy_share": 0.0, "realized_vol": None,
            "prior_low": 0.95, "price": 0.9, "net": -1.0,
        })
    db.append_observation(conn, "e1", obs, block_identity="slot:123", capture_version="v1")
    decision = step(pos.engine_state, obs)
    assert decision.action is V6Action.DERISK          # tier-1 break -> staged derisk
    db.append_decision(conn, "e1", decision)
    assert len(db.position_timeline(conn, "e1")["decisions"]) == 1
    requested = (pos.remaining_qty_atomic * decision.fraction_ppm) // 1_000_000
    out = db.apply_action_fill(conn, decision, _exit_fill(requested))
    assert out.remaining_qty_atomic == 1_000_000 - requested
    assert db.reconstruct_position(conn, "e1").remaining_qty_atomic == out.remaining_qty_atomic


def test_append_decision_requires_a_prior_observation(tmp_path):
    conn = _conn(tmp_path)
    pos = db.record_entry_fill(conn, _entry())
    dec = _hand_decision(pos.engine_state, 250_000, V6Action.DERISK, "tok-noobs")
    with pytest.raises(ValueError):
        db.append_decision(conn, "e1", dec)            # no observation with dec.input_fingerprint


def test_cursor_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    assert db.get_cursor(conn, "solana_tape") is None
    db.set_cursor(conn, "solana_tape", "slot:999", NOW)
    assert db.get_cursor(conn, "solana_tape") == "slot:999"
    db.set_cursor(conn, "solana_tape", "slot:1000", NOW)
    assert db.get_cursor(conn, "solana_tape") == "slot:1000"


# ---- Review regressions: causal state, immutable audit, and transition ordering ----------------

def test_hold_decision_advances_causal_engine_state(tmp_path):
    conn = _conn(tmp_path)
    pos = db.record_entry_fill(conn, _entry())
    obs = make_observation(
        observation_id="hold-1", observed_at=NOW, network_id="solana",
        payload=_complete_payload(realized_vol=0.07),
    )
    db.append_observation(conn, "e1", obs, capture_version="v1")
    decision = step(pos.engine_state, obs)
    assert decision.action is V6Action.HOLD
    db.append_decision(conn, "e1", decision)

    advanced = db.apply_hold_decision(conn, decision, at=NOW)

    assert advanced.engine_state.break_state.vol_hist == (0.07,)
    assert db.apply_hold_decision(conn, decision, at=NOW) == advanced


def test_action_fill_requires_a_persisted_decision(tmp_path):
    conn = _conn(tmp_path)
    pos = db.record_entry_fill(conn, _entry())
    decision = _hand_decision(
        pos.engine_state, 250_000, V6Action.DERISK, "not-persisted")

    with pytest.raises(ValueError, match="persisted decision"):
        db.apply_action_fill(conn, decision, _exit_fill(250_000))


def test_action_outcomes_persist_corrected_resolution_times(tmp_path):
    conn = _conn(tmp_path)
    pos = db.record_entry_fill(conn, _entry())
    decision = _persist_hand_decision(
        conn, pos.engine_state, 250_000, V6Action.DERISK, "timed-fill")

    db.apply_action_fill(conn, decision, _exit_fill(250_000))

    fill = conn.execute(
        "SELECT created_at FROM v6_paper_fills WHERE action_token='timed-fill'"
    ).fetchone()
    claim = conn.execute(
        "SELECT resolved_at FROM v6_paper_action_intents WHERE action_token='timed-fill'"
    ).fetchone()
    assert fill["created_at"] == NOW
    assert claim["resolved_at"] == NOW


def test_action_fill_persists_quote_route_and_cost_evidence(tmp_path):
    conn = _conn(tmp_path)
    pos = db.record_entry_fill(conn, _entry())
    decision = _persist_hand_decision(
        conn, pos.engine_state, 250_000, V6Action.DERISK, "evidence-fill")
    modeled_fill = _exit_fill(250_000)
    db.apply_action_fill(conn, decision, modeled_fill)
    stored = conn.execute(
        "SELECT quote_provider, route_quote_fingerprint, quote_payload_json, route_binding_json, "
        "costs_json, net_pnl_computable FROM v6_paper_fills "
        "WHERE action_token='evidence-fill'"
    ).fetchone()
    assert stored["quote_provider"] == "jupiter"
    assert stored["route_quote_fingerprint"] == modeled_fill["quote_fingerprint"]
    assert stored["quote_payload_json"] == db.canonical_json(modeled_fill["quote_payload"])
    assert stored["route_binding_json"] == db.canonical_json(modeled_fill["route_binding"])
    assert stored["costs_json"] == db.canonical_json(modeled_fill["costs"])
    assert stored["net_pnl_computable"] == 1


def test_append_observation_rejects_fingerprint_mismatch(tmp_path):
    conn = _conn(tmp_path)
    db.record_entry_fill(conn, _entry())
    observation = V6Observation(
        observation_id="forged", observed_at=NOW, network_id="solana",
        payload=_complete_payload(), input_fingerprint="not-the-payload-hash",
    )

    with pytest.raises(ValueError, match="fingerprint"):
        db.append_observation(conn, "e1", observation, capture_version="v1")


def test_decision_redelivery_checks_reasons_thought_and_successor_states(tmp_path):
    conn = _conn(tmp_path)
    pos = db.record_entry_fill(conn, _entry())
    decision = _persist_hand_decision(
        conn, pos.engine_state, 250_000, V6Action.DERISK, "immutable-decision")

    with pytest.raises(db.ImmutableConflict):
        db.append_decision(
            conn, "e1", replace(decision, reason_codes=("tampered",), thought="tampered"))


def test_entry_rejects_unsupported_network_and_policy_hash_mismatch(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="network_id"):
        db.record_entry_fill(conn, _entry(network_id="8453"))
    with pytest.raises(ValueError, match="policy_hash"):
        db.record_entry_fill(conn, _entry(policy_hash="0" * 64))


def test_default_path_honors_v6_paper_db_override(monkeypatch, tmp_path):
    override = str(tmp_path / "override.sqlite")
    monkeypatch.setattr("sniper.settings.memecoin_v6_paper.MEMECOIN_V6_PAPER_DB", override)
    assert db._default_path() == override


def test_hold_and_unfilled_redelivery_ignore_new_retry_timestamp(tmp_path):
    conn = _conn(tmp_path)
    pos = db.record_entry_fill(conn, _entry())
    hold_obs = make_observation(
        observation_id="hold-retry", observed_at=NOW, network_id="solana",
        payload=_complete_payload(realized_vol=0.07),
    )
    db.append_observation(conn, "e1", hold_obs, capture_version="v1")
    hold = step(pos.engine_state, hold_obs)
    db.append_decision(conn, "e1", hold)
    held = db.apply_hold_decision(conn, hold, at=NOW)
    assert db.apply_hold_decision(conn, hold, at=LATER) == held

    action = _persist_hand_decision(
        conn, held.engine_state, 250_000, V6Action.DERISK, "unfilled-retry")
    unfilled = db.record_action_unfilled(conn, action, "quote_unavailable", NOW)
    assert db.record_action_unfilled(
        conn, action, "quote_unavailable", LATER) == unfilled


def test_natural_entry_identity_conflict_raises_immutable_conflict(tmp_path):
    conn = _conn(tmp_path)
    db.record_entry_fill(conn, _entry("first"))
    duplicate_identity = _entry(
        "second", origin_event_id="evt-first", network_id="solana", mint="MINTfirst")
    with pytest.raises(db.ImmutableConflict):
        db.record_entry_fill(conn, duplicate_identity)


def test_unapplied_decision_survives_restart_and_blocks_a_second_decision(tmp_path):
    path = str(tmp_path / "v6.sqlite")
    conn = db.connect(path)
    pos = db.record_entry_fill(conn, _entry())
    pending = _persist_hand_decision(
        conn, pos.engine_state, 250_000, V6Action.DERISK, "pending-action")
    conn.close()

    restarted = db.connect(path)
    recovered = db.unapplied_decisions(restarted)
    assert len(recovered) == 1
    assert recovered[0].position_id == "e1"
    assert recovered[0].decision == pending

    obs = make_observation(
        observation_id="second-before-resolution", observed_at=LATER, network_id="solana",
        payload=_complete_payload(),
    )
    db.append_observation(restarted, "e1", obs, capture_version="v1")
    second = _hand_decision(
        pos.engine_state, 250_000, V6Action.DERISK, "second-action",
        input_fingerprint=obs.input_fingerprint,
    )
    with pytest.raises(db.StateConflict, match="unapplied decision"):
        db.append_decision(restarted, "e1", second)

    db.record_action_unfilled(restarted, pending, "quote_unavailable", LATER)
    assert db.unapplied_decisions(restarted) == []


def test_reconstruction_detects_mutable_engine_state_corruption(tmp_path):
    conn = _conn(tmp_path)
    pos = db.record_entry_fill(conn, _entry())
    decision = _persist_hand_decision(
        conn, pos.engine_state, 250_000, V6Action.DERISK, "reconstruct-state")
    db.record_action_unfilled(conn, decision, "quote_unavailable", NOW)
    conn.execute(
        "UPDATE v6_paper_positions SET engine_state_json=? WHERE position_id='e1'",
        (db._state_to_json(pos.engine_state),),
    )

    with pytest.raises(db.StateConflict, match="engine state"):
        db.reconstruct_position(conn, "e1")


def test_v8_database_migrates_to_v9_with_the_durable_solana_arrival_queue(tmp_path):
    """Forward-port lock (2026-08-03). The live paper DB was migrated to schema 9 by an
    unmerged branch, so every commit on main refused to open it and V6 could only run from
    that branch. Main must migrate v8 -> v9 itself."""
    path = str(tmp_path / "v8.sqlite")
    conn = db.connect(path)
    conn.execute("DROP TABLE v6_paper_solana_arrivals")
    conn.execute("PRAGMA user_version=8")
    conn.commit()
    conn.close()

    migrated = db.connect(path)

    assert migrated.execute("PRAGMA user_version").fetchone()[0] == 10
    # FULL shape, not a subset — a partial assert let `last_error` and `resolved_at` go missing
    # without failing, which is the same blind spot the required-column contract exists to close.
    migrated_cols = {row[1] for row in migrated.execute(
        "PRAGMA table_info(v6_paper_solana_arrivals)")}
    assert migrated_cols == {
        "pool", "signature", "slot", "mint", "binding_json", "received_at", "state",
        "attempt_count", "last_error", "next_attempt_at", "resolved_at", "content_fingerprint"}
    # A migrated DB and a freshly created one must be byte-identical in shape.
    fresh = db.connect(str(tmp_path / "fresh.sqlite"))
    assert migrated_cols == {row[1] for row in fresh.execute(
        "PRAGMA table_info(v6_paper_solana_arrivals)")}
    indexes = {r[0] for r in migrated.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='v6_paper_solana_arrivals'")}
    assert {"idx_v6_solana_arrivals_due", "idx_v6_solana_arrivals_frontier"} <= indexes


def test_v9_migration_marks_decisions_that_already_have_a_fill_as_filled(tmp_path):
    """NULL applied_outcome is the "still outstanding" marker the open-position index and the
    pending-decision lookup key on, so a decision whose fill already landed kept reading as
    outstanding. The migration backfills those, and must leave unfilled decisions alone."""
    path = str(tmp_path / "v8.sqlite")
    conn = db.connect(path)
    # Two positions so the two decisions need no state threading: one decision gets a fill,
    # the other never does.
    filled_pos = db.record_entry_fill(conn, _entry(entry_id="e-filled", token_qty=1_000_000))
    unfilled_pos = db.record_entry_fill(conn, _entry(entry_id="e-unfilled", token_qty=1_000_000))
    filled = _persist_hand_decision(
        conn, filled_pos.engine_state, 400_000, V6Action.DERISK, "tok-filled")
    db.apply_action_fill(conn, filled, _exit_fill(400_000))
    _persist_hand_decision(
        conn, unfilled_pos.engine_state, 400_000, V6Action.DERISK, "tok-unfilled")
    # Regress to the pre-forward-port state: v8 left applied_outcome NULL.
    conn.execute("UPDATE v6_paper_decisions SET applied_outcome=NULL")
    conn.execute("PRAGMA user_version=8")
    conn.commit()
    conn.close()

    migrated = db.connect(path)

    outcomes = dict(migrated.execute(
        "SELECT action_token, applied_outcome FROM v6_paper_decisions"))
    assert outcomes["tok-filled"] == "filled"
    assert outcomes["tok-unfilled"] is None


def test_connect_rejects_a_schema9_db_whose_arrival_table_is_missing_columns(tmp_path):
    """The 2026-08-03 forward-port shipped the table but not its required-column contract, so a
    schema-9 DB carrying only pool/signature/slot/state/next_attempt_at opened clean and
    reported healthy — no mint, binding, timestamps, retry fields or fingerprint."""
    path = str(tmp_path / "v9.sqlite")
    conn = db.connect(path)
    conn.execute("DROP TABLE v6_paper_solana_arrivals")
    conn.execute("CREATE TABLE v6_paper_solana_arrivals ("
                 "pool TEXT NOT NULL, signature TEXT NOT NULL, slot INTEGER NOT NULL, "
                 "state TEXT NOT NULL, next_attempt_at TEXT NOT NULL, "
                 "PRIMARY KEY (pool, signature))")
    conn.commit()
    conn.close()

    with pytest.raises(db.StateConflict, match="v6_paper_solana_arrivals missing columns"):
        db.connect(path)


# --- radar provenance and measurement censoring --------------------------------

def test_v6_entries_carry_radar_provenance(tmp_path):
    conn = db.connect(str(tmp_path / "v6.sqlite"))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(v6_paper_entries)")}
    assert {
        "source_receipt_id", "adapter_version", "fill_model_version",
        "rule_version", "evaluator_version", "launch_mode",
    } <= columns


def test_measurement_censoring_is_distinct_from_operator_censoring(tmp_path):
    conn = db.connect(str(tmp_path / "v6.sqlite"))
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='v6_paper_positions'"
    ).fetchone()[0]
    # Our own failures must not be recordable as operator action.
    assert "censored_migration" in sql
    assert "censored_handoff_stale" in sql
    assert "censored_operator" in sql
    assert "censored_whatever" not in sql


def test_v9_database_migrates_to_v10_keeping_existing_positions(tmp_path):
    path = str(tmp_path / "v6.sqlite")
    conn = db.connect(path)
    db.record_entry_fill(conn, _entry(entry_id="existing-v9", token_qty=1_000_000))
    positions_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='v6_paper_positions'"
    ).fetchone()[0]
    columns = [row[1] for row in conn.execute("PRAGMA table_info(v6_paper_positions)")]
    conn.close()

    # Reconstruct the actual schema-9 table shape. Merely lowering user_version leaves
    # the widened v10 CHECK in place and never exercises the table-rebuild migration.
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA foreign_keys=OFF")
    old_sql = positions_sql.replace(
        "v6_paper_positions", "v6_paper_positions_v9", 1
    ).replace(
        ",\n                                                  'censored_migration',"
        "\n                                                  'censored_handoff_stale'",
        "",
    )
    raw.execute(old_sql)
    joined = ",".join(columns)
    raw.execute(
        f"INSERT INTO v6_paper_positions_v9 ({joined}) "
        f"SELECT {joined} FROM v6_paper_positions"
    )
    raw.execute("DROP TABLE v6_paper_positions")
    raw.execute("ALTER TABLE v6_paper_positions_v9 RENAME TO v6_paper_positions")
    raw.execute("PRAGMA user_version=9")
    raw.commit()
    raw.close()

    migrated = db.connect(path)
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == 10
    assert migrated.execute(
        "SELECT COUNT(*) FROM v6_paper_positions"
    ).fetchone()[0] == 1
    assert migrated.execute("SELECT COUNT(*) FROM v6_paper_fills").fetchone()[0] == 1
    assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
    columns = {row[1] for row in migrated.execute("PRAGMA table_info(v6_paper_entries)")}
    assert "source_receipt_id" in columns
    assert migrated.execute(
        "SELECT 1 FROM sqlite_master WHERE name='v6_paper_positions_migrated'"
    ).fetchone() is None
