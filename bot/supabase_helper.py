#!/usr/bin/env python3
"""Supabase shared memory helper — no MCP needed, uses REST API directly.

Two tables:
  memories      — persistent key/value store shared between local Claude and VPS bot
                  active by default; superseded/retired entries are history
  session_logs  — per-session work summaries for cross-instance handoff

Typical VPS bot workflow:
    from bot.supabase_helper import read_memories, upsert_memory, write_session_log

    # On startup — get context from local Claude
    memories = read_memories()

    # After completing significant work — tell local Claude what happened
    write_session_log(
        summary="Fixed paper trader threshold bug, restarted services",
        key_decisions="Changed RESOLUTION_WIN_THRESHOLD 0.90 → 0.97",
        files_changed="paper_trader.py, paper_settings.py",
    )

    # Update a shared fact (safe to call repeatedly — idempotent)
    upsert_memory("project", "Proof Bar Status", "Current proof bar metrics", content)

Memory types (enforced by DB check constraint):
    user        — facts about Pablo / operator preferences
    feedback    — instructions for Claude's behavior
    project     — live project state (proof bar, services, thresholds)
    reference   — static reference docs, glossaries, ADRs

CLI usage:
    python3 bot/supabase_helper.py read              # short summary of all memories
    python3 bot/supabase_helper.py read-full         # full content of all memories
    python3 bot/supabase_helper.py read-logs [N]     # last N session logs (default 5)
    python3 bot/supabase_helper.py search <query>    # ranked full-text search (falls back to substring)
    python3 bot/supabase_helper.py semantic <query>  # vector semantic search (bge-small-en; falls back to FTS)
    python3 bot/supabase_helper.py upsert <type> <name> <content>
    python3 bot/supabase_helper.py delete <name>     # delete memory by exact name
    python3 bot/supabase_helper.py write-log <summary>
    python3 bot/supabase_helper.py sync-state        # auto-sync live DB stats to Supabase
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any

# Ensure project root is importable when invoked as `python3 bot/supabase_helper.py`
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sniper.markets.content_normalization import normalize_memory_content

logger = logging.getLogger(__name__)

# Backward-compatible alias for older tests/callers.
_normalize_memory_content = normalize_memory_content

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")

VALID_TYPES = {"user", "feedback", "project", "reference", "knowledge"}
# Lifecycle vocabulary. The DB CHECK constraint
# (migrations/supabase_022_memories_status_widen.sql) and
# scripts/knowledge_sync.VALID_STATUSES must stay aligned with this set.
VALID_STATUSES = {
    "active", "superseded", "retired",
    "draft", "archived", "rejected", "needs_review",
}
_MAX_RETRIES = 3
_RETRY_DELAY = 1.5  # seconds, multiplied by attempt number


# ---------------------------------------------------------------------------
# Internal HTTP helpers
# ---------------------------------------------------------------------------

def _headers(extra: dict | None = None) -> dict:
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip",
    }
    if extra:
        h.update(extra)
    return h


def _request(method: str, path: str, body: Any = None, extra_headers: dict | None = None, timeout: float = 10) -> Any:
    """Execute a Supabase REST request with retry on transient failures."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_ANON_KEY must be set in environment / .env"
        )
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = _headers(extra_headers)

    last_err: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if raw and resp.headers.get("Content-Encoding", "").strip().lower() == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            body_text = raw.decode(errors="replace")
            if raw and (e.headers or {}).get("Content-Encoding", "").strip().lower() == "gzip":
                try:
                    body_text = gzip.decompress(raw).decode(errors="replace")
                except (OSError, EOFError, zlib.error) as decode_error:
                    body_text = f"invalid gzip: {decode_error}"
            last_err = RuntimeError(f"HTTP {e.code} {method} {path}: {body_text[:300]}")
            if e.code in (400, 401, 402, 403, 404, 409, 422):
                raise last_err  # Non-retryable client errors
            logger.warning("Supabase transient error (attempt %d/%d): %s", attempt + 1, _MAX_RETRIES, last_err)
        except Exception as e:
            last_err = e
            logger.warning("Supabase request error (attempt %d/%d): %s", attempt + 1, _MAX_RETRIES, e)
        if attempt < _MAX_RETRIES - 1:
            time.sleep(_RETRY_DELAY * (attempt + 1))
    raise last_err  # type: ignore[misc]


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _current_git_sha() -> str | None:
    """Return the current git commit SHA when this checkout is available."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return None
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def _memory_status_schema_missing(exc: Exception) -> bool:
    msg = str(exc)
    return (
        "valid_as_of_commit" in msg
        or "superseded_by" in msg
        or "status" in msg and ("column" in msg.lower() or "schema cache" in msg.lower())
    )


# ---------------------------------------------------------------------------
# memories table
# ---------------------------------------------------------------------------

def read_memories(type_filter: str | None = None, active_only: bool = True) -> list[dict]:
    """Return memories, optionally filtered by type. Ordered by updated_at desc.

    By default callers only see active brain entries. Pass active_only=False for
    audits, migrations, and history views.
    """
    path = "memories?select=*&order=updated_at.desc"
    if type_filter:
        if type_filter not in VALID_TYPES:
            raise ValueError(f"type must be one of {VALID_TYPES}, got {type_filter!r}")
        path += f"&type=eq.{type_filter}"
    if active_only:
        path += "&status=eq.active"
    try:
        return _request("GET", path) or []
    except Exception as exc:
        if active_only and _memory_status_schema_missing(exc):
            logger.warning("memories.status not available; retrying read without active filter")
            return read_memories(type_filter=type_filter, active_only=False)
        raise


def get_memory(name: str, type_filter: str | None = None, active_only: bool = True) -> dict | None:
    """Fetch a single active memory by exact name. Returns None if not found."""
    path = f"memories?select=*&name=eq.{urllib.parse.quote(name)}&order=updated_at.desc&limit=1"
    if type_filter:
        path += f"&type=eq.{type_filter}"
    if active_only:
        path += "&status=eq.active"
    try:
        rows = _request("GET", path)
    except Exception as exc:
        if active_only and _memory_status_schema_missing(exc):
            logger.warning("memories.status not available; retrying get without active filter")
            return get_memory(name, type_filter=type_filter, active_only=False)
        raise
    if not rows:
        return None
    row = rows[0]
    row["content"] = normalize_memory_content(str(row.get("type") or ""), str(row.get("name") or ""), str(row.get("content") or ""))
    return row


def upsert_memory(
    type: str,
    name: str,
    description: str,
    content: str,
    source: str = "vps",
    status: str = "active",
    superseded_by: str | None = None,
    valid_as_of_commit: str | None = None,
    research_metadata: dict | None = None,
) -> dict:
    """Create or update a memory by (type, name).

    Uses a read-then-write strategy: checks for an existing memory with the same
    name and type, then PATCHes if found or POSTs if new. Fully idempotent.
    """
    if type not in VALID_TYPES:
        raise ValueError(f"type must be one of {VALID_TYPES}, got {type!r}")
    if source not in ("vps", "local", "agent"):
        raise ValueError(f"source must be 'vps' | 'local' | 'agent', got {source!r}")
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")

    from sniper.research.wallet_execution.publication import memory_claim_guard
    memory_claim_guard(name, content, metadata=research_metadata)
    content = normalize_memory_content(type, name, content)
    existing = get_memory(name, type_filter=type, active_only=False)
    payload = {
        "type": type,
        "name": name,
        "description": description,
        "content": content,
        "source": source,
        "status": status,
        "superseded_by": superseded_by,
        "valid_as_of_commit": valid_as_of_commit or _current_git_sha(),
        "updated_at": _now_iso(),
    }

    if existing:
        row_id = existing["id"]
        try:
            result = _request(
                "PATCH",
                f"memories?id=eq.{row_id}",
                body=payload,
                extra_headers={"Prefer": "return=representation"},
            )
        except Exception as exc:
            if not _memory_status_schema_missing(exc):
                raise
            legacy_payload = {k: v for k, v in payload.items() if k not in {"status", "superseded_by", "valid_as_of_commit"}}
            result = _request(
                "PATCH",
                f"memories?id=eq.{row_id}",
                body=legacy_payload,
                extra_headers={"Prefer": "return=representation"},
            )
            payload = legacy_payload
        return result[0] if result else {**existing, **payload}
    else:
        try:
            result = _request(
                "POST",
                "memories",
                body=payload,
                extra_headers={"Prefer": "return=representation"},
            )
        except Exception as exc:
            if not _memory_status_schema_missing(exc):
                raise
            legacy_payload = {k: v for k, v in payload.items() if k not in {"status", "superseded_by", "valid_as_of_commit"}}
            result = _request(
                "POST",
                "memories",
                body=legacy_payload,
                extra_headers={"Prefer": "return=representation"},
            )
            payload = legacy_payload
        return result[0] if result else payload


def write_memory(
    type: str,
    name: str,
    description: str,
    content: str,
    source: str = "vps",
    status: str = "active",
    superseded_by: str | None = None,
    valid_as_of_commit: str | None = None,
) -> dict:
    """Alias for upsert_memory — kept for backwards compatibility."""
    return upsert_memory(
        type,
        name,
        description,
        content,
        source=source,
        status=status,
        superseded_by=superseded_by,
        valid_as_of_commit=valid_as_of_commit,
    )


# ---------------------------------------------------------------------------
# KSG-aware upsert (Phase 3 columns)
# ---------------------------------------------------------------------------

# The KSG-extension migration (`supabase_020_memories_ksg_extension.sql`)
# adds these columns to `public.memories`. `upsert_memory` (above) does
# NOT write them — the legacy entrypoint is preserved for non-KSG callers.
_KSG_COLUMNS: tuple[str, ...] = (
    "slug",
    "entry_type",
    "summary",
    "tags",
    "source_branch",
    "source_pr",
    "source_files",
    "supersedes_ids",
    "valid_to_commit",
    "confidence",
    "reviewed_by",
    "reviewed_at",
    "metadata",
)


def _ksg_schema_missing(exc: Exception) -> bool:
    """True when a PATCH/POST failed because the Phase 3 KSG columns
    don't exist on the target instance yet (e.g. migration unapplied).
    PostgREST surfaces these as `column "<name>" does not exist` or
    `schema cache` errors mentioning one of the KSG columns."""
    msg = str(exc).lower()
    if "column" not in msg and "schema cache" not in msg:
        return False
    return any(col in msg for col in _KSG_COLUMNS)


def upsert_ksg_entry(
    *,
    type: str,
    name: str,
    description: str,
    content: str,
    source: str = "agent",
    status: str = "active",
    superseded_by: str | None = None,
    valid_as_of_commit: str | None = None,
    # KSG-extension fields. All optional; only the non-None ones are
    # serialized into the request body.
    slug: str | None = None,
    entry_type: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
    source_branch: str | None = None,
    source_pr: int | None = None,
    source_files: list[str] | None = None,
    supersedes_ids: list[str] | None = None,
    valid_to_commit: str | None = None,
    confidence: str | None = None,
    reviewed_by: str | None = None,
    reviewed_at: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Upsert a memory row preserving KSG metadata columns.

    The legacy `upsert_memory()` strips KSG fields silently — it accepts
    only `type/name/description/content/source/status/superseded_by/
    valid_as_of_commit`. `knowledge_sync.py` was routing entries through
    it, dropping `entry_type/tags/source_branch/source_pr/source_files/
    confidence/supersedes_ids` etc. KSG validation looked stricter than
    persistence; rows landed in Supabase missing fields the migration
    + spec required (Codex deep-review finding #4).

    This function is the KSG-aware write path: it preserves every
    Phase 3 column the migration added. If the Phase 3 schema is not
    yet applied on the target instance, the write falls back to the
    legacy column set (so partial deployments don't hard-fail) and the
    return payload reflects the actual written shape.

    Behavior contract:
      * Validates `type`, `source`, `status` against the same
        vocabularies as `upsert_memory`.
      * Read-then-PATCH/POST. Idempotent on (type, name).
      * Excludes None-valued KSG fields from the payload — preserves
        prior values on PATCH; defaults on POST.
      * Falls back to a legacy-shape payload when PostgREST reports
        a missing KSG column.
    """
    if type not in VALID_TYPES:
        raise ValueError(f"type must be one of {VALID_TYPES}, got {type!r}")
    if source not in ("vps", "local", "agent"):
        raise ValueError(f"source must be 'vps' | 'local' | 'agent', got {source!r}")
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")

    from sniper.research.wallet_execution.publication import memory_claim_guard
    memory_claim_guard(name, content, metadata=metadata, entry_type=entry_type)
    content = normalize_memory_content(type, name, content)
    existing = get_memory(name, type_filter=type, active_only=False)

    payload: dict[str, Any] = {
        "type": type,
        "name": name,
        "description": description,
        "content": content,
        "source": source,
        "status": status,
        "superseded_by": superseded_by,
        "valid_as_of_commit": valid_as_of_commit or _current_git_sha(),
        "updated_at": _now_iso(),
    }

    # KSG fields — only include the explicitly-provided ones to avoid
    # overwriting prior values with nulls.
    ksg_fields: dict[str, Any] = {
        "slug": slug,
        "entry_type": entry_type,
        "summary": summary,
        "tags": tags,
        "source_branch": source_branch,
        "source_pr": source_pr,
        "source_files": source_files,
        "supersedes_ids": supersedes_ids,
        "valid_to_commit": valid_to_commit,
        "confidence": confidence,
        "reviewed_by": reviewed_by,
        "reviewed_at": reviewed_at,
        "metadata": metadata,
    }
    for k, v in ksg_fields.items():
        if v is not None:
            payload[k] = v

    def _legacy_fallback_payload() -> dict[str, Any]:
        """Strip everything Phase 3 adds and what
        _memory_status_schema_missing covers — used when the target
        instance is older than spec §5.1."""
        keep = {
            "type", "name", "description", "content",
            "source", "updated_at",
        }
        return {k: v for k, v in payload.items() if k in keep}

    if existing:
        row_id = existing["id"]
        try:
            result = _request(
                "PATCH",
                f"memories?id=eq.{row_id}",
                body=payload,
                extra_headers={"Prefer": "return=representation"},
            )
        except Exception as exc:
            if _ksg_schema_missing(exc):
                # Drop KSG fields, retry with legacy + status payload.
                retry_payload = {
                    k: v for k, v in payload.items()
                    if k not in _KSG_COLUMNS
                }
                try:
                    result = _request(
                        "PATCH",
                        f"memories?id=eq.{row_id}",
                        body=retry_payload,
                        extra_headers={"Prefer": "return=representation"},
                    )
                    payload = retry_payload
                except Exception as exc2:
                    if not _memory_status_schema_missing(exc2):
                        raise
                    legacy = _legacy_fallback_payload()
                    result = _request(
                        "PATCH",
                        f"memories?id=eq.{row_id}",
                        body=legacy,
                        extra_headers={"Prefer": "return=representation"},
                    )
                    payload = legacy
            elif _memory_status_schema_missing(exc):
                legacy = _legacy_fallback_payload()
                result = _request(
                    "PATCH",
                    f"memories?id=eq.{row_id}",
                    body=legacy,
                    extra_headers={"Prefer": "return=representation"},
                )
                payload = legacy
            else:
                raise
        return result[0] if result else {**existing, **payload}

    try:
        result = _request(
            "POST",
            "memories",
            body=payload,
            extra_headers={"Prefer": "return=representation"},
        )
    except Exception as exc:
        if _ksg_schema_missing(exc):
            retry_payload = {
                k: v for k, v in payload.items()
                if k not in _KSG_COLUMNS
            }
            try:
                result = _request(
                    "POST",
                    "memories",
                    body=retry_payload,
                    extra_headers={"Prefer": "return=representation"},
                )
                payload = retry_payload
            except Exception as exc2:
                if not _memory_status_schema_missing(exc2):
                    raise
                legacy = _legacy_fallback_payload()
                result = _request(
                    "POST",
                    "memories",
                    body=legacy,
                    extra_headers={"Prefer": "return=representation"},
                )
                payload = legacy
        elif _memory_status_schema_missing(exc):
            legacy = _legacy_fallback_payload()
            result = _request(
                "POST",
                "memories",
                body=legacy,
                extra_headers={"Prefer": "return=representation"},
            )
            payload = legacy
        else:
            raise
    return result[0] if result else payload


def delete_memory(name: str, type_filter: str | None = None) -> int:
    """Delete memory by exact name. Returns number of rows deleted."""
    path = f"memories?name=eq.{urllib.parse.quote(name)}"
    if type_filter:
        path += f"&type=eq.{type_filter}"
    result = _request("DELETE", path, extra_headers={"Prefer": "return=representation"})
    return len(result) if result else 0


def search_memories(query: str, active_only: bool = True) -> list[dict]:
    """Case-insensitive substring search across active name and content fields.

    This is a raw-substring primitive. Live callers rely on it as a structured-key
    PREFIX lookup (e.g. ``search_memories("country:")``, ``"flag:"``, ``"session/"``),
    so its semantics must NOT change. For free-text research retrieval use
    ``search_memories_fts()`` instead.
    """
    q = urllib.parse.quote(query, safe='')
    path = f"memories?select=*&or=(name.ilike.*{q}*,content.ilike.*{q}*)&order=updated_at.desc"
    if active_only:
        path += "&status=eq.active"
    try:
        return _request("GET", path) or []
    except Exception as exc:
        if active_only and _memory_status_schema_missing(exc):
            logger.warning("memories.status not available; retrying search without active filter")
            return search_memories(query, active_only=False)
        raise


def search_memories_fts(query: str, active_only: bool = True, limit: int = 50, timeout: float = 10) -> list[dict]:
    """Ranked full-text search over memories — the search to use for research.

    Backed by the Postgres ``search_memories_fts`` RPC (migration 028): a weighted
    tsvector (name^A, summary/description^B, content^C) queried with
    ``websearch_to_tsquery`` and ranked by ``ts_rank``. Unlike the ilike-substring
    ``search_memories()``, this stems words and ANDs multiple terms, so a query
    like "memecoin exit latency" matches documents that never contain that exact
    substring.

    Falls back to substring ``search_memories()`` when the RPC is unavailable
    (e.g. an environment where migration 028 has not been applied), so callers
    always get a best-effort result instead of an exception.
    """
    body = {"q": query, "active_only": active_only, "max_results": int(limit)}
    try:
        rows = _request("POST", "rpc/search_memories_fts", body=body, timeout=timeout)
        return rows or []
    except Exception as exc:
        logger.warning(
            "search_memories_fts RPC unavailable (%s); falling back to substring search", exc
        )
        return search_memories(query, active_only=active_only)


def semantic_search_memories(query: str, active_only: bool = True, limit: int = 10, timeout: float = 10) -> list[dict]:
    """Vector semantic search over memories — catches conceptual matches FTS misses.

    Embeds the query with the local bge-small-en model (bot.embeddings) and calls
    the `match_memories` RPC (migration 029, cosine kNN over pgvector). Rows carry a
    `similarity` field (1 = identical direction). Degrades gracefully: if the
    embedding model is unavailable (dep/model missing) or the RPC fails, it falls
    back to `search_memories_fts()` so callers never break — semantic is a bonus
    layer over FTS, not a hard dependency.
    """
    try:
        from bot.embeddings import embed_query, to_pgvector
        emb = to_pgvector(embed_query(query))
    except Exception as exc:
        logger.warning("semantic embed unavailable (%s); falling back to FTS", exc)
        return search_memories_fts(query, active_only=active_only, limit=limit, timeout=timeout)

    body = {"query_embedding": emb, "match_count": int(limit), "active_only": active_only}
    try:
        rows = _request("POST", "rpc/match_memories", body=body, timeout=timeout)
        return rows or []
    except Exception as exc:
        logger.warning("match_memories RPC failed (%s); falling back to FTS", exc)
        return search_memories_fts(query, active_only=active_only, limit=limit, timeout=timeout)


def _slugify(name: str) -> str:
    """Deterministic KSG slug from a memory name: strip one prefix segment
    (`finding/…`, `research:…`), kebab-case the rest, cap length."""
    s = name
    for sep in ("/", ":"):
        if sep in s:
            s = s.split(sep, 1)[1]
            break
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:120] or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:120]


def save_finding(
    name: str,
    description: str,
    content: str,
    tags: list[str] | None = None,
    type: str = "knowledge",
    status: str = "active",
    superseded_by: str | None = None,
    valid_as_of_commit: str | None = None,
    entry_type: str = "finding",
    source_files: list[str] | None = None,
    confidence: str | None = None,
    source_branch: str | None = None,
    source_pr: int | None = None,
    research_metadata: dict | None = None,
) -> dict:
    """Agent-produced finding upsert — KSG-aware.

    Routes through `upsert_ksg_entry()` (NOT the legacy `upsert_memory()`, which
    silently strips entry_type/slug/tags) so agent findings land KSG-complete:
    entry_type='finding', a derived slug, tags as a real column, and
    valid_as_of_commit stamped. `source='agent'` + a `finding/` name prefix keep
    agent writes from colliding with human-curated entries.

    Tags are ALSO kept inlined in content for `search_memories()` back-compat;
    the canonical home is now the `tags` column.

    `source_branch` / `source_pr` are forwarded because the KSG requires one of them
    (branch-close design §"Provenance"). `upsert_ksg_entry()` has always accepted both;
    this helper simply did not pass them, so every finding written through it landed
    with NULL provenance and no way to trace it back to the change that produced it.
    """
    if not name.startswith("finding/"):
        name = f"finding/{name}"
    if tags:
        content = f"**Tags:** {', '.join(tags)}\n\n" + content
    return upsert_ksg_entry(
        type=type,
        name=name,
        description=description,
        content=content,
        source="agent",
        status=status,
        superseded_by=superseded_by,
        valid_as_of_commit=valid_as_of_commit,
        entry_type=entry_type,
        slug=_slugify(name),
        summary=description,
        tags=tags or None,
        source_files=source_files,
        confidence=confidence,
        source_branch=source_branch,
        source_pr=source_pr,
        metadata=research_metadata,
    )


# ---------------------------------------------------------------------------
# session_logs table
# ---------------------------------------------------------------------------

def write_session_log(
    summary: str,
    key_decisions: str | None = None,
    files_changed: str | None = None,
    source: str = "vps",
) -> None:
    """Append a session summary so the other Claude instance knows what happened.

    Call this after completing any significant work block — deployments, bug fixes,
    analysis runs, config changes, etc.
    """
    if source not in ("vps", "local", "agent"):
        raise ValueError(f"source must be 'vps' | 'local' | 'agent', got {source!r}")
    payload = {
        "source": source,
        "summary": summary,
        "key_decisions": key_decisions,
        "files_changed": files_changed,
    }
    _request("POST", "session_logs", body=payload, extra_headers={"Prefer": "return=minimal"})


def read_session_logs(limit: int = 10, source_filter: str | None = None) -> list[dict]:
    """Return recent session logs, newest first."""
    path = f"session_logs?select=*&order=created_at.desc&limit={limit}"
    if source_filter:
        path += f"&source=eq.{source_filter}"
    return _request("GET", path) or []


# ---------------------------------------------------------------------------
# sync-state: auto-populate project memories from live DB
# ---------------------------------------------------------------------------

def sync_project_state(db_path: str | None = None) -> None:
    """Read live stats from SQLite and upsert them as Supabase memories.

    Syncs 3 memories: VPS Services Status, Proof Bar Status, Wallet Analysis State.
    Call after any significant system change so the local Claude instance stays current.
    """
    import sqlite3
    import subprocess
    from datetime import datetime, timezone

    if db_path is None:
        db_path = os.getenv("DB_PATH", "sniper_tracker.db")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # 1. Services
    svc_result = subprocess.run(
        ["systemctl", "--user", "is-active",
         "sniper-web", "sniper-stream", "sniper-analyzer",
         "sniper-paper", "sniper-alerter"],
        capture_output=True, text=True,
    )
    statuses = dict(zip(
        ["web", "stream", "analyzer", "paper", "alerter"],
        svc_result.stdout.strip().split("\n"),
    ))
    svc_lines = "\n".join(f"- sniper-{k}: {v}" for k, v in statuses.items())
    upsert_memory(
        "project", "VPS Services Status",
        f"Live service status as of {today}",
        f"Services ({today}):\n{svc_lines}",
        source="vps",
    )
    print(f"  ✓ Services: {', '.join(f'{k}={v}' for k,v in statuses.items())}")

    # 2. Strategy performance — per-strategy breakdown from strategy_positions
    sp_rows = conn.execute("""
        SELECT
            sp.strategy_id,
            si.label as strategy_name,
            COUNT(*) as total,
            SUM(CASE WHEN sp.state = 'open' THEN 1 ELSE 0 END) as open_ct,
            SUM(CASE WHEN sp.state = 'closed' THEN 1 ELSE 0 END) as closed_ct,
            SUM(CASE WHEN sp.state = 'closed' AND sp.realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            ROUND(SUM(CASE WHEN sp.state = 'closed' THEN sp.realized_pnl ELSE 0 END), 2) as realized_pnl,
            ROUND(SUM(CASE WHEN sp.state = 'open' THEN sp.pnl_usdc ELSE 0 END), 2) as unrealized_pnl
        FROM strategy_positions sp
        LEFT JOIN strategies si ON sp.strategy_id = CAST(si.id AS TEXT)
        GROUP BY sp.strategy_id
        ORDER BY realized_pnl DESC
    """).fetchall()

    strat_lines = []
    total_open = 0
    total_closed = 0
    total_pnl = 0.0
    for row in sp_rows:
        name = row["strategy_name"] or f"strategy_{row['strategy_id']}"
        closed = row["closed_ct"] or 0
        wins = row["wins"] or 0
        wr = round(wins / closed * 100, 1) if closed else 0.0
        pnl = row["realized_pnl"] or 0.0
        upnl = row["unrealized_pnl"] or 0.0
        total_open += row["open_ct"] or 0
        total_closed += closed
        total_pnl += pnl
        strat_lines.append(
            f"- {name}: {row['open_ct']} open | {closed} closed | "
            f"WR {wr}% | PnL ${pnl:+.2f} realized, ${upnl:+.2f} unrealized"
        )

    perf_content = (
        f"Strategy performance ({today}):\n"
        f"- Total open positions: {total_open}\n"
        f"- Total closed positions: {total_closed}\n"
        f"- Total realized PnL: ${total_pnl:+.2f}\n\n"
        + "\n".join(strat_lines)
    )
    upsert_memory("project", "Strategy Performance", f"Live strategy performance as of {today}", perf_content, source="vps")
    print(f"  ✓ Strategy performance: {total_open} open, {total_closed} closed, ${total_pnl:+.2f} PnL")

    # 3. Wallet classifications
    clf_rows = conn.execute(
        "SELECT classification, COUNT(*) cnt FROM user_analysis GROUP BY classification ORDER BY cnt DESC"
    ).fetchall()
    clf_lines = "\n".join(f"- {r['classification']}: {r['cnt']}" for r in clf_rows)
    total_wallets = sum(r["cnt"] for r in clf_rows)
    h60 = conn.execute("SELECT COUNT(*) FROM user_analysis WHERE insider_score >= 60").fetchone()[0]
    upsert_memory(
        "project", "Wallet Analysis State",
        f"Wallet classification breakdown as of {today}",
        f"Wallets analyzed ({today}):\n{clf_lines}\n- Total: {total_wallets}\n- Insider>=60: {h60}",
        source="vps",
    )
    print(f"  ✓ Wallets: {total_wallets} total, {h60} insider>=60")

    # --- Cross-layer intelligence ---
    try:
        # Convergence: trade ideas matching insider signals (score >= 70)
        convergence = conn.execute("""
            SELECT ti.condition_id, ti.market_title, ti.direction,
                   ps.wallet, ps.position_insider_score
            FROM trade_ideas ti
            JOIN position_signals ps ON ti.condition_id = ps.condition_id
            WHERE ti.status = 'open' AND ps.is_open = 1
              AND ps.position_insider_score >= 70
            LIMIT 5
        """).fetchall()

        if convergence:
            lines = []
            for row in convergence:
                lines.append(f"- {row['market_title']}: news={row['direction']}, insider {row['wallet'][:10]}... score={row['position_insider_score']}")
            conv_content = (
                f"## Finding\n{len(convergence)} markets where news intel AND insider wallets agree.\n\n"
                f"## Evidence\n" + "\n".join(lines) + "\n\n"
                f"## Source\nAutomated cross-layer scan {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}\n\n"
                f"## Confidence\nhigh\n\n"
                f"## Applies To\nAll strategies — convergence signals are highest confidence"
            )
            upsert_memory(
                type="project",
                name="convergence:latest-scan",
                description=f"{len(convergence)} news+insider convergence signals detected",
                content=conv_content,
                source="vps",
            )
            logger.info("Cross-layer: %d convergence signals found", len(convergence))

        # Conflict: open positions vs opposing trade ideas
        conflicts = conn.execute("""
            SELECT sp.condition_id, sp.outcome, ti.direction, ti.market_title, ti.reasoning
            FROM strategy_positions sp
            JOIN trade_ideas ti ON sp.condition_id = ti.condition_id
            WHERE sp.state = 'open' AND ti.status = 'open'
              AND ((LOWER(sp.outcome) = 'yes' AND ti.direction = 'buy_no')
                OR (LOWER(sp.outcome) = 'no' AND ti.direction = 'buy_yes'))
            LIMIT 5
        """).fetchall()

        if conflicts:
            lines = []
            for row in conflicts:
                lines.append(f"- {row[3]}: holding {row[1]} but news says {row[2]}")
            conf_content = (
                f"## Finding\n{len(conflicts)} open positions CONFLICT with new trade ideas.\n\n"
                f"## Evidence\n" + "\n".join(lines) + "\n\n"
                f"## Source\nAutomated cross-layer scan {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}\n\n"
                f"## Confidence\nmedium\n\n"
                f"## Applies To\nRisk management — review conflicting positions"
            )
            upsert_memory(
                type="project",
                name="conflict:latest-scan",
                description=f"{len(conflicts)} position vs news conflicts detected",
                content=conf_content,
                source="vps",
            )
            logger.info("Cross-layer: %d conflicts found", len(conflicts))
    except Exception:
        logger.debug("Cross-layer intelligence scan failed (non-blocking)", exc_info=True)

    conn.close()
    print(f"\n✅ sync-state complete — memories upserted to Supabase")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_memory(m: dict, full: bool = False) -> None:
    status = m.get("status", "active")
    print(f"\n=== [{m['type']}/{status}] {m['name']} (src={m['source']}, updated={str(m.get('updated_at', ''))[:16]}) ===")
    if full:
        print(m["content"])
    else:
        snippet = m["content"][:120].replace("\n", " ")
        print(f"  {snippet}{'...' if len(m['content']) > 120 else ''}")


def _load_env() -> None:
    """Load .env from the project root (parent of bot/)."""
    _here = os.path.dirname(os.path.abspath(__file__))
    for candidate in [os.path.join(_here, "..", ".env"), os.path.join(_here, ".env")]:
        path = os.path.abspath(candidate)
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        os.environ.setdefault(k.strip(), v.strip())
            break


if __name__ == "__main__":
    _load_env()
    # Re-read after _load_env() populates os.environ from .env file
    SUPABASE_URL = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")
    logging.basicConfig(level=logging.WARNING)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "read"

    if cmd == "read":
        memories = read_memories()
        print(f"Memories ({len(memories)} total):")
        for m in memories:
            _print_memory(m, full=False)

    elif cmd == "read-full":
        for m in read_memories():
            _print_memory(m, full=True)

    elif cmd == "read-logs":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        logs = read_session_logs(limit=limit)
        if not logs:
            print("No session logs yet.")
        else:
            print(f"Last {len(logs)} session log(s):")
            for log in logs:
                print(f"\n[{log['source']}] {log['created_at'][:16]}")
                print(f"  Summary: {log['summary']}")
                if log.get("key_decisions"):
                    print(f"  Decisions: {log['key_decisions']}")
                if log.get("files_changed"):
                    print(f"  Files: {log['files_changed']}")

    elif cmd == "search":
        if len(sys.argv) < 3:
            print("Usage: supabase_helper.py search <query>")
            sys.exit(1)
        query = sys.argv[2]
        # Free-text: ranked FTS first, then fall back to substring so a
        # structured-key prefix ("country:", "session/") still resolves.
        results = search_memories_fts(query)
        mode = "full-text"
        if not results:
            results = search_memories(query)
            mode = "substring"
        print(f"Found {len(results)} match(es) [{mode}]:")
        for m in results:
            _print_memory(m, full=False)

    elif cmd == "semantic":
        if len(sys.argv) < 3:
            print("Usage: supabase_helper.py semantic <query>")
            sys.exit(1)
        results = semantic_search_memories(sys.argv[2], limit=10)
        print(f"Found {len(results)} semantic match(es):")
        for m in results:
            sim = m.get("similarity")
            tag = f"[{sim:.3f}] " if isinstance(sim, (int, float)) else ""
            desc = (m.get("description") or "").replace("\n", " ")[:90]
            print(f"  {tag}[{m.get('type')}] {m.get('name')} — {desc}")

    elif cmd == "upsert":
        if len(sys.argv) < 5:
            print("Usage: supabase_helper.py upsert <type> <name> <content>")
            print(f"  type must be one of: {', '.join(sorted(VALID_TYPES))}")
            sys.exit(1)
        result = upsert_memory(
            type=sys.argv[2],
            name=sys.argv[3],
            description=sys.argv[3],
            content=sys.argv[4],
        )
        print(f"✅ Upserted: [{result.get('type')}] {result.get('name')}")

    elif cmd == "delete":
        if len(sys.argv) < 3:
            print("Usage: supabase_helper.py delete <name>")
            sys.exit(1)
        n = delete_memory(sys.argv[2])
        print(f"Deleted {n} memory row(s) named {sys.argv[2]!r}")

    elif cmd == "write-log":
        if len(sys.argv) < 3:
            print("Usage: supabase_helper.py write-log <summary>")
            sys.exit(1)
        write_session_log(summary=sys.argv[2])
        print("✅ Session log written.")

    elif cmd == "sync-state":
        db = sys.argv[2] if len(sys.argv) > 2 else "sniper_tracker.db"
        sync_project_state(db_path=db)

    else:
        print(f"Unknown command: {cmd!r}")
        print("Commands: read, read-full, read-logs [N], search <q>, semantic <q>,")
        print("          upsert <type> <name> <content>, delete <name>, write-log <summary>, sync-state [db_path]")
        sys.exit(1)
