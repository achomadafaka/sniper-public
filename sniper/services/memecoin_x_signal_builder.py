"""Crash-safe deterministic signal builder for the isolated memecoin X database."""
from __future__ import annotations

from sniper.analysis.memecoin_x.signals import events_for_observation
from sniper.persistence.db_memecoin_x import (
    enqueue_signal_notification,
    finish_signal_run,
    last_completed_observation_id,
    persist_signal_event,
    start_signal_run,
)


def run_once(conn, *, run_id: str, observed_at: str, limit=1000) -> dict:
    limit = int(limit)
    if not 1 <= limit <= 10000:
        raise ValueError("limit must be in 1..10000")
    after = last_completed_observation_id(conn)
    if not start_signal_run(
            conn, run_id=run_id, started_at=observed_at,
            after_observation_id=after):
        raise ValueError(f"signal run already exists: {run_id}")
    rows = conn.execute("""
        SELECT id FROM memecoin_x_card_observations
        WHERE id > ? ORDER BY id LIMIT ?
    """, (after, limit)).fetchall()
    through = after
    created = 0
    try:
        for row in rows:
            observation_id = int(row["id"])
            for event in events_for_observation(conn, observation_id=observation_id):
                existed = conn.execute(
                    "SELECT 1 FROM memecoin_x_signal_events WHERE logical_key=?",
                    (event["logical_key"],)).fetchone() is not None
                event_id = persist_signal_event(
                    conn, event, created_at=observed_at, commit=False)
                created += int(not existed)
                enqueue_signal_notification(
                    conn, logical_key=f"dashboard:{event['logical_key']}",
                    signal_event_id=event_id, channel="dashboard",
                    created_at=observed_at, commit=False)
                if event["event_type"] in {"fast_consensus", "tier2_scout"}:
                    enqueue_signal_notification(
                        conn, logical_key=f"telegram:{event['logical_key']}",
                        signal_event_id=event_id, channel="telegram",
                        created_at=observed_at, commit=False)
            through = observation_id
        finish_signal_run(
            conn, run_id=run_id, finished_at=observed_at, status="ok",
            through_observation_id=through, events_created=created)
    except Exception as exc:
        conn.rollback()
        finish_signal_run(
            conn, run_id=run_id, finished_at=observed_at, status="error",
            through_observation_id=after, events_created=0, error_detail=str(exc))
        raise
    return {
        "run_id": run_id,
        "status": "ok",
        "after_observation_id": after,
        "through_observation_id": through,
        "observations_processed": len(rows),
        "events_created": created,
    }
