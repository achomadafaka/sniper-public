"""Local collector provenance. Receipts bind content; only the Desk store authenticates their origin.

New admissions must run verify_source against an explicit trusted intake root. A stored, trusted receipt may
be checked after pruning; source_proof_refusal alone never authenticates caller-supplied JSON.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path

from sniper.services import memecoin_desk_contract as K

VERSION = "desk-intake-source-v1"
MAX_AGE_S = 120.0
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_CORE = ("id", "poll_id", "feed", "filter", "network_id", "mint", "provider_rank", "rank_kind",
         "observed_at", "host_received_at", "clock_offset_s", "source_ref", "poll_feed", "poll_filter",
         "poll_received_at", "poll_status", "poll_error")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _identity(candidate, policy_hash):
    return {"network_id": candidate["network_id"], "mint": candidate["mint"], "pool": candidate["pool"],
            "snapshot_hash": candidate["snapshot"]["hash"], "policy_hash": policy_hash,
            "candidate_hash": _hash(candidate), "intake_hash": _hash(candidate["intake"])}


def _source_path(intake_dir, claim):
    day, ref = claim["intake_row_ref"].split(":", 1)  # source_ref is opaque and may contain further colons
    if date.fromisoformat(day).isoformat() != day or not ref:
        raise ValueError("day")
    root = Path(intake_dir).resolve()
    path = root / claim["feed"] / (day + ".db")
    if not path.resolve().is_relative_to(root):
        raise ValueError("path")
    return path, ref, day


def _read(path, ref):
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1)
    conn.row_factory = sqlite3.Row
    try:
        indexes = conn.execute('SELECT name,"unique",partial FROM pragma_index_list(?)', ("intake_rows",)).fetchall()
        unique_ref = any(unique and not partial and
            [r[0] for r in conn.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno", (name,))] == ["source_ref"]
            for name, unique, partial in indexes)
        parent_pk = conn.execute("SELECT name,type,pk FROM pragma_table_info(?) WHERE pk>0 ORDER BY pk", ("polls",)).fetchall()
        if not unique_ref or [tuple(r) for r in parent_pk] != [("id", "INTEGER", 1)]:
            raise ValueError("intake_source_schema")
        # Verified UNIQUE source_ref and polls INTEGER PRIMARY KEY bound this read to one row.
        row = conn.execute(
            "SELECT r.id,r.poll_id,r.feed,r.filter,r.network_id,r.mint,r.provider_rank,r.rank_kind,"
            "r.observed_at,r.host_received_at,r.clock_offset_s,r.source_ref,p.feed AS poll_feed,"
            "p.filter AS poll_filter,p.received_at AS poll_received_at,p.status AS poll_status,p.error AS poll_error,"
            "CASE WHEN LENGTH(r.raw_json)<=65536 THEN r.raw_json END AS raw_json FROM intake_rows r "
            "LEFT JOIN polls p ON p.id=r.poll_id WHERE r.source_ref=?", (ref,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _row_refusal(row, candidate, day):
    claim = candidate["intake"]
    if row is None:
        return "intake_source_row_missing"
    if (row["poll_status"] != "ok" or row["poll_error"] is not None
            or row["poll_feed"] != row["feed"] or row["poll_filter"] != row["filter"]
            or row["poll_received_at"] != row["host_received_at"]):
        return "intake_source_poll"
    for key in ("feed", "filter", "provider_rank", "rank_kind", "observed_at", "host_received_at", "clock_offset_s"):
        if row[key] != claim[key]:
            return "intake_source_mismatch"
    if row["network_id"] != candidate["network_id"] or row["mint"] != candidate["mint"]:
        return "intake_source_mismatch"
    observed, host, offset = (row[k] for k in ("observed_at", "host_received_at", "clock_offset_s"))
    if (not all(_finite(v) for v in (observed, host, offset)) or min(observed, host) < 0
            or not math.isclose(observed, host - offset, rel_tol=0, abs_tol=1e-6)):
        return "intake_source_clock"
    try:
        if datetime.fromtimestamp(host, timezone.utc).date().isoformat() != day:
            return "intake_source_clock"
    except (ValueError, OverflowError, OSError):
        return "intake_source_clock"
    emitted = candidate["snapshot"]["emission_at"]
    state = candidate["snapshot"]["fields"]["pool_state"]["value"]
    printed = state.get("reserve_known_at") if isinstance(state, dict) else None
    if not _finite(printed) or not host < printed <= emitted:
        return "intake_source_print_order"
    if not 0 <= (emitted - offset) - observed <= MAX_AGE_S:
        return "intake_source_stale"
    return None


def _metadata(row, known_at):
    out = {"source": row["feed"], "known_at": known_at,
           **{k: row[k] for k in ("observed_at", "host_received_at", "clock_offset_s")}}
    if row["feed"] != "pumpfun":
        return out
    try:
        raw = json.loads(row["raw_json"])
    except (ValueError, TypeError, RecursionError):
        return out
    if not isinstance(raw, dict):
        return out
    created = raw.get("created_timestamp")
    if _finite(created) and created > 0:
        out["created_timestamp"] = created
    creator = raw.get("creator")
    if K._key(creator, 32):
        out["creator"] = creator
    return out


def verify_source(candidate, *, intake_dir, policy_hash, now_fn=time.time):
    """Verify one original emission with one indexed read-only lookup. No provider or current-age fallback."""
    proof = {"version": VERSION, "status": "unknown", "reason": None, "known_at": None,
             "source_row_hash": None, "source_metadata": None}
    try:
        candidate = K.validate_candidate(candidate)
        if (candidate.get("venue") != "pumpswap" or candidate["network_id"] != "solana"
                or "intake" not in candidate):
            raise ValueError("intake")
        if not isinstance(policy_hash, str) or not _HEX.fullmatch(policy_hash):
            proof["reason"] = "intake_source_policy"
        else:
            proof.update(_identity(candidate, policy_hash))
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        proof["reason"] = "intake_source_candidate_invalid"
    row = None
    if proof["reason"] is None:
        try:
            path, ref, day = _source_path(intake_dir, candidate["intake"])
        except (ValueError, TypeError, OSError):
            proof["reason"] = "intake_source_path"
        else:
            try:
                if not path.is_file():
                    proof["reason"] = "intake_source_missing"
                else:
                    row = _read(path, ref)
                    proof["reason"] = _row_refusal(row, candidate, day)
            except (sqlite3.Error, OSError, ValueError, OverflowError) as exc:
                proof["reason"] = "intake_source_schema" if str(exc) == "intake_source_schema" else "intake_source_unreadable"
    try:
        known = now_fn()  # actual local verification completion, never the original listing or intended landing
    except Exception:
        known = None
    if _finite(known) and known >= 0:
        proof["known_at"] = known
    if proof["reason"] is None:
        if not _finite(known) or known < candidate["snapshot"]["emission_at"]:
            proof["reason"] = "intake_source_clock"
        else:
            proof.update(status="pass", source_row_hash=_hash({k: row[k] for k in _CORE}),
                         source_metadata=_metadata(row, known))
    proof["proof_hash"] = _hash(proof)
    return proof


def source_proof_refusal(proof, candidate, *, policy_hash, as_of):
    """Content/time binding only; caller must first retrieve this receipt from its trusted Desk store."""
    if not isinstance(proof, dict) or proof.get("version") != VERSION:
        return "intake_source_proof_unknown"
    if not isinstance(policy_hash, str) or not _HEX.fullmatch(policy_hash):
        return "intake_source_policy"
    try:
        candidate = K.validate_candidate(candidate)
        if proof.get("proof_hash") != _hash({k: v for k, v in proof.items() if k != "proof_hash"}):
            return "intake_source_proof_hash"
        if any(proof.get(k) != v for k, v in _identity(candidate, policy_hash).items()):
            return "intake_source_proof_binding"
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return "intake_source_proof_binding"
    if proof.get("status") != "pass" or proof.get("reason") is not None:
        return proof.get("reason") or "intake_source_proof_unknown"
    if not isinstance(proof.get("source_row_hash"), str) or not _HEX.fullmatch(proof["source_row_hash"]):
        return "intake_source_proof_hash"
    known = proof.get("known_at")
    if (not _finite(known) or not _finite(as_of)
            or not candidate["snapshot"]["emission_at"] <= known <= as_of):
        return "intake_source_proof_clock"
    return None
