"""Idempotent queueing for optional frozen memecoin X LLM jobs."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re

from sniper.analysis.memecoin_x.llm_contract import (
    canonical_input_hash,
    freeze_digest_evidence,
    freeze_event_evidence,
    freeze_on_request_evidence,
)
from sniper.persistence.db_memecoin_x import create_llm_job, llm_digest_watermark


_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_VERSION_KEYS = frozenset({
    "prompt_version", "model_version", "output_schema_version", "generation",
})


def _aware(value):
    parsed = datetime.fromisoformat(str(value))
    if parsed.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _versions(versions):
    if not isinstance(versions, dict) or not _VERSION_KEYS.issubset(versions):
        raise ValueError("missing required LLM version fields")
    values = {key: versions[key] for key in _VERSION_KEYS}
    if any(not str(values[key]).strip() for key in (
            "prompt_version", "model_version", "output_schema_version")):
        raise ValueError("blank LLM version field")
    if not isinstance(values["generation"], dict):
        raise ValueError("generation version must be an object")
    return values


def _version_key(provider, model, versions):
    payload = json.dumps(
        {"provider": provider, "model": model, **versions}, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _create(conn, *, logical_key, mode, snapshot, provider, model, versions,
            network_id=None, mint=None):
    values = _versions(versions)
    job = {
        "logical_key": logical_key,
        "mode": mode,
        "as_of": snapshot["as_of"],
        "network_id": network_id,
        "mint": mint,
        "evidence_ids": [row["evidence_id"] for row in snapshot["evidence"]],
        "input": snapshot,
        "input_hash": canonical_input_hash(snapshot),
        "prompt_version": values["prompt_version"],
        "provider": str(provider),
        "model": str(model),
        "model_version": values["model_version"],
        "output_schema_version": values["output_schema_version"],
        "generation": values["generation"],
    }
    return create_llm_job(conn, job, created_at=snapshot["as_of"])


def enqueue_event_job(
        conn, *, signal_event_id, as_of, provider, model, versions) -> int | None:
    _aware(as_of)
    row = conn.execute(
        "SELECT event_type,network_id,mint FROM memecoin_x_signal_events WHERE id=?",
        (int(signal_event_id),)).fetchone()
    if row is None:
        raise ValueError(f"signal event not found: {signal_event_id}")
    if row["event_type"] not in {"fast_consensus", "tier2_scout"}:
        return None
    values = _versions(versions)
    snapshot = freeze_event_evidence(conn, signal_event_id=signal_event_id, as_of=as_of)
    logical = f"event:{int(signal_event_id)}:{_version_key(provider, model, values)}"
    return _create(
        conn, logical_key=logical, mode="event", snapshot=snapshot,
        provider=provider, model=model, versions=values,
        network_id=row["network_id"], mint=row["mint"])


def enqueue_digest_job(conn, *, as_of, provider, model, versions) -> int | None:
    cutoff = _aware(as_of)
    after = llm_digest_watermark(conn)
    candidate_ids = []
    for row in conn.execute(
            "SELECT id,triggered_at FROM memecoin_x_signal_events WHERE id>? ORDER BY id",
            (after,)):
        if _aware(row["triggered_at"]) <= cutoff:
            candidate_ids.append(int(row["id"]))
    if not candidate_ids:
        return None
    through = max(candidate_ids)
    values = _versions(versions)
    snapshot = freeze_digest_evidence(
        conn, after_event_id=after, through_event_id=through, as_of=as_of)
    if not snapshot["signal_events"]:
        return None
    logical = f"digest:{after}:{through}:{_version_key(provider, model, values)}"
    return _create(
        conn, logical_key=logical, mode="digest", snapshot=snapshot,
        provider=provider, model=model, versions=values)


def enqueue_on_request_job(
        conn, *, request_id, as_of, window, network_id=None, mint=None,
        provider, model, versions) -> int:
    if not _REQUEST_ID.fullmatch(str(request_id or "")):
        raise ValueError("invalid operator request ID")
    _aware(as_of)
    values = _versions(versions)
    snapshot = freeze_on_request_evidence(
        conn, network_id=network_id, mint=mint, as_of=as_of, window=window)
    logical = f"on_request:{request_id}"
    input_hash = canonical_input_hash(snapshot)
    existing = conn.execute(
        "SELECT id,input_hash,provider,model,prompt_version,model_version,"
        "output_schema_version,generation_json FROM memecoin_x_llm_jobs WHERE logical_key=?",
        (logical,)).fetchone()
    if existing is not None:
        generation = json.loads(existing["generation_json"])
        same = (
            existing["input_hash"] == input_hash
            and existing["provider"] == str(provider)
            and existing["model"] == str(model)
            and existing["prompt_version"] == values["prompt_version"]
            and existing["model_version"] == values["model_version"]
            and existing["output_schema_version"] == values["output_schema_version"]
            and generation == values["generation"]
        )
        if not same:
            raise ValueError("duplicate request ID has conflicting frozen inputs")
        return int(existing["id"])
    return _create(
        conn, logical_key=logical, mode="on_request", snapshot=snapshot,
        provider=provider, model=model, versions=values,
        network_id=network_id, mint=mint)
