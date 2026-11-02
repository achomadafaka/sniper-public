"""The brain's write and verify paths. Every write passes the gate; nothing is deleted.

write_fact order:
  1. gate and evidence checks (no I/O to Supabase);
  2. entity LOOKUP only (existing ids), then contradiction candidates on them;
  3. needs_judgement / noop return here, having created nothing;
  4. only now are missing entities created. A new entity needs a section, because views reach
     facts through their entities' sections, and an action key found in the statement adds a
     `tool` entity so two facts about the same action share an entity;
  5. one RPC commits the new fact, judged lifecycle changes, and links atomically. Stale
     candidates retry once, then remain judge_pending. A stable fact UUID makes replay safe.
Outbox: appends and the flush's hand-off hold one file lock. The flush moves the queue to an
`.inflight` file and releases the lock before any network call, so another session can keep
appending. A crash leaves .inflight behind, and the next flush replays it."""
from __future__ import annotations

import fcntl
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from bot.brain_graph import AUTHORS, READ_STATUSES, VERDICTS, VERIFIERS
from bot.brain_graph.contradict import action_keys, candidates, polarity
from bot.brain_graph.evidence import check_all
from bot.brain_graph.gate import redact, validate_draft, validate_episode_body
from bot.brain_graph.store import BrainRefused, BrainUnavailable, VersionConflict

PART = 1800


def _wanted_entities(draft):
    ents = [dict(e) for e in draft.get("entities") or []]
    seen = {(e["type"], e["name"].strip().lower()) for e in ents}
    for key in sorted(action_keys(draft.get("statement") or "")):
        if ("tool", key) not in seen:
            ents.append({"type": "tool", "name": key})
            seen.add(("tool", key))
    return ents


def _lookup(store, ents):
    found, missing = [], []
    for e in ents:
        hit = store.find_entity(e["type"], e["name"])
        if hit:
            found.append(hit["id"])
        else:
            missing.append(e)
    return list(dict.fromkeys(found)), missing


def _create(store, missing, default_section):
    ids, cache = [], {}
    for e in missing:
        slug = e.get("section") or default_section
        if slug not in cache:
            secs = store.sections([slug])
            if not secs:
                raise BrainRefused(f"no section {slug!r}")
            cache[slug] = secs[0]["id"]
        ids.append(store.insert("brain_entities", {"type": e["type"], "name": e["name"].strip(),
                                                   "aliases": list(e.get("aliases") or []),
                                                   "section_id": cache[slug]})["id"])
    return ids


def _commit(store, row, actions):
    warnings = []
    for attempt in range(3):
        try:
            return store.write_fact(row, actions), warnings
        except VersionConflict:
            if attempt == 2:
                raise
            for action in actions:
                if action["verdict"] not in ("supersedes", "contradicts"):
                    continue
                fresh = store.get("brain_facts", action["id"]) if attempt == 0 else None
                if fresh and fresh.get("valid_to") is None and fresh["status"] in READ_STATUSES:
                    action["version"] = fresh["version"]
                else:
                    action["verdict"] = "candidate"
                    warnings.append(f"{action['id']} moved while judging; linked as a candidate, fact left judge_pending")


def normalize_operator_order(draft):
    """Anchor explicit/new canonical order writes; this never authenticates prose.

    Historical imports are not text-classified. Existing legacy rows need their
    author's explicit relink/rewrite; retrying an old UUID does not migrate it.
    """
    explicit = draft.get("operator_order", False)
    if type(explicit) is not bool:
        raise BrainRefused("operator_order must be boolean")
    if draft.get("kind") not in ("decision", "settled"):
        if explicit:
            raise BrainRefused("operator orders must be decision or settled facts")
        return draft
    entities = list(draft.get("entities") or [])
    linked = any(e.get("type") == "actor" and str(e.get("name", "")).lower() == "operator" for e in entities)
    canonical = (draft.get("author_session") != "import:v1" and re.match(
        r"(?i)^\s*operator(?:\s+order\b|\s+20\d{2}-\d{2}-\d{2}\b)", draft.get("statement") or ""))
    try:
        detail = json.loads(draft.get("detail") or "{}")
    except (ValueError, TypeError):
        detail = {"previous_detail": draft.get("detail")}
    if not isinstance(detail, dict):
        detail = {"previous_detail": detail}
    if not (explicit or linked or canonical or draft.get("author") == "operator" or "order_provenance" in detail):
        return draft
    if not linked:
        entities.append({"type": "actor", "name": "operator"})
    result = dict(draft, entities=entities)
    # Only a separate authenticated host ledger can establish order authority.
    detail["order_provenance"] = {"relayed_by": draft.get("author"), "authenticated": False}
    result["detail"] = json.dumps(detail, sort_keys=True)
    return result


def write_fact(store, draft, *, repo, judgements=None, interactive=True, secret_values=frozenset(),
               outbox=None, fetched=True, write_id=None, import_status=None, valid_to=None,
               judge_pending=False, candidate_ids=()):
    if import_status not in (None, "evidence_broken", "retracted", "superseded"):
        raise ValueError("invalid import_status")
    importing = draft.get("author_session") == "import:v1"
    if (import_status or valid_to is not None or judge_pending or candidate_ids) and not importing:
        return {"status": "refused", "errors": ["only the importer sets a status on write"]}
    closed = import_status in ("retracted", "superseded")
    if closed and not (valid_to or draft.get("valid_from")):
        return {"status": "refused", "errors": ["closed import needs its source date"]}
    draft = normalize_operator_order(draft)
    errors = validate_draft(draft, secret_values=set(secret_values))
    evidence = check_all(draft.get("evidence"), repo, fetched=fetched)
    errors += [f"evidence {e.get('type')} {e.get('ref')}: {e['check_reason']}"
               for e in evidence if e["check"] == "broken" and not import_status]
    if errors:
        return {"status": "refused", "errors": errors}
    judgements = dict(judgements or {})
    author = draft["author"]
    write_id = str(uuid.UUID(write_id)) if write_id else str(uuid.uuid4())
    try:
        existing = store.get("brain_facts", write_id)
        if existing:
            if (existing["kind"], existing["statement"], existing["author"]) != (
                    draft["kind"], draft["statement"].strip(), author):
                raise BrainRefused("operation_identity_conflict")
            return {"status": "written", "fact": existing}
        ents = _wanted_entities(draft)
        found_ids, missing = _lookup(store, ents)
        unsectioned = [f"{e['type']}:{e['name']}" for e in missing if not (e.get("section") or draft.get("section"))]
        if unsectioned:
            return {"status": "refused", "errors": [
                "new entities need a section (--section <slug>): " + ", ".join(unsectioned)]}
        cands = candidates(store, draft["statement"], draft["kind"], found_ids) if found_ids and not closed else []
        unjudged = [c for c in cands if judgements.get(c["id"]) not in VERDICTS]
        if unjudged and interactive:
            return {"status": "needs_judgement",
                    "candidates": [{"id": c["id"], "kind": c["kind"], "statement": c["statement"],
                                    "status": c["status"]} for c in unjudged]}
        if any(judgements.get(c["id"]) == "duplicate" for c in cands):
            return {"status": "noop", "duplicate_of": [c["id"] for c in cands
                                                       if judgements.get(c["id"]) == "duplicate"]}
        entity_ids = found_ids + _create(store, missing, draft.get("section"))
        actions = [{"id": c["id"], "version": c["version"],
                    "verdict": judgements.get(c["id"]) if judgements.get(c["id"]) in VERDICTS else "candidate"}
                   for c in cands]
        warnings = []
        # A judged target can close while a write is offline. Keep that relationship pending
        # instead of dropping its supplied judgement when the live candidate query excludes it.
        seen = {c["id"] for c in cands}
        for ident, verdict in ({} if closed else judgements).items():
            if ident not in seen and verdict in VERDICTS and verdict != "unrelated":
                old = store.get("brain_facts", ident)
                if old is None:
                    raise BrainRefused(f"no judged fact {ident}")
                if (importing and verdict == "refines" and old.get("valid_to") is None
                        and old["status"] in READ_STATUSES
                        and set(draft.get("episode_ids") or []).intersection(old.get("episode_ids") or [])):
                    actions.append({"id": ident, "version": old["version"], "verdict": "refines"})
                    continue
                actions.append({"id": ident, "version": old["version"], "verdict": "candidate"})
                warnings.append(f"{ident} left the live candidates; fact left judge_pending")
        for ident in candidate_ids:
            if not closed and ident not in {a["id"] for a in actions}:
                old = store.get("brain_facts", ident)
                if old is None:
                    raise BrainRefused(f"no import candidate {ident}")
                actions.append({"id": ident, "version": old["version"], "verdict": "candidate"})
        row = {"id": write_id, "kind": draft["kind"], "statement": draft["statement"].strip(),
               "detail": draft.get("detail"), "entity_ids": entity_ids,
               "polarity": polarity(draft["statement"]), "evidence": evidence,
               "author": author, "author_session": draft.get("author_session"),
               "episode_ids": draft.get("episode_ids") or []}
        for k in ("valid_from", "verify_by"):
            if draft.get(k):
                row[k] = draft[k]
        if import_status:
            row["status"] = import_status
        if judge_pending:
            row["judge_pending"] = True
        if closed:
            row["valid_to"] = valid_to or row["valid_from"]
        fact, moved = _commit(store, row, actions)
        warnings.extend(moved)
        out = {"status": "written", "fact": fact}
        if warnings:
            out["warnings"] = warnings
        return out
    except BrainUnavailable:
        if outbox is None:
            raise
        _queue(Path(outbox), draft, judgements, write_id=write_id,
               options={"import_status": import_status, "valid_to": valid_to,
                        "judge_pending": judge_pending, "candidate_ids": list(candidate_ids)})
        return {"status": "queued"}


def verify_fact(store, fact_id, verdict, *, role, output, note="", version=None,
                secret_values=frozenset()):
    if verdict not in ("verified", "verified_partial", "refuted"):
        raise BrainRefused("verdict must be verified, verified_partial or refuted")
    if role not in VERIFIERS:
        raise BrainRefused(f"the verifier must be one of {', '.join(VERIFIERS)}")
    if validate_episode_body((note or "") + "\n" + (output or ""), secret_values=set(secret_values)):
        raise BrainRefused("refused: verification contains a secret or keyed URL (value not shown)")
    fact = store.get("brain_facts", fact_id)
    if fact is None:
        raise BrainRefused(f"no fact {fact_id}")
    if fact.get("valid_to") is not None or fact["status"] in ("retracted", "superseded"):
        # A duplicate verdict retracts with valid_to set; verifying it would read closed as live (Gamma 09-30).
        raise BrainRefused(f"fact is closed ({fact['status']}, valid_to {fact.get('valid_to')}): nothing to verify")
    if fact["author"] == role:
        raise BrainRefused("a fact is verified by a different role than its author")
    if fact["status"] == "disputed":
        raise BrainRefused("resolve the dispute first: supersede one side or judge the pair")
    if fact.get("judge_pending"):
        raise BrainRefused("judge its contradiction candidates first (brain queue)")
    if verdict == "refuted" and not ((output or "").strip() or (note or "").strip()):
        raise BrainRefused("a refutation needs a reason and the evidence output")
    if verdict == "verified" and not (output or "").strip():
        verdict = "verified_partial"             # no re-run output: partial at most (spec 5.10)
    body = ((note or "") + ("\n--- output ---\n" + output[:3000] if output else "")).strip()
    return store.patch_fact(fact_id, version or fact["version"], {
        "status": verdict, "verified_by": role, "verified_at": datetime.now(timezone.utc).isoformat(),
        "verify_note": body or None})


def retract_fact(store, fact_id, *, role, reason, reason_kind=None, version=None, secret_values=frozenset()):
    """Withdraw a misfiled or obsolete fact. Finished tasks use work_records.complete_work. Any crew role may retract, the
    reason is required and kept in verify_note, and the row stays for history (status only)."""
    if reason_kind not in ("obsolete", "misfiled"):
        raise BrainRefused("retract requires reason_kind obsolete or misfiled; finished tasks use complete")
    if role not in VERIFIERS:
        raise BrainRefused(f"the retracting role must be one of {', '.join(VERIFIERS)}")
    if not (reason or "").strip():
        raise BrainRefused("a retraction needs a reason")
    if validate_episode_body(reason, secret_values=set(secret_values)):
        raise BrainRefused("refused: the reason contains a secret or keyed URL (value not shown)")
    fact = store.get("brain_facts", fact_id)
    if fact is None:
        raise BrainRefused(f"no fact {fact_id}")
    if fact["status"] in ("retracted", "superseded"):
        raise BrainRefused(f"fact is already {fact['status']}")
    # verified_by stays NULL: migration 032 CHECKs verified_by <> author, and a role closing its own
    # task is the common case (Lambda 2026-09-29). The retracting role is in verify_note.
    return store.patch_fact(fact_id, version or fact["version"], {
        "status": "retracted", "verified_at": datetime.now(timezone.utc).isoformat(),
        "verify_note": f"retracted by {role} [{reason_kind}]: {reason.strip()}"})


def add_episode(store, kind, body, *, author, session_id=None, source_ref=None, occurred_at=None,
                secret_values=frozenset(), redact_secrets=False):
    """Append an episode in parts of at most 1,800 chars. occurred_at None means UNKNOWN. A secret
    refuses an interactive episode; the importer passes redact_secrets=True to keep the provenance."""
    if author not in AUTHORS:
        raise BrainRefused(f"author must be one of {', '.join(AUTHORS)}")
    if validate_episode_body(body, secret_values=set(secret_values)):
        if not redact_secrets:
            raise BrainRefused("refused: the episode body contains a secret or a keyed URL (value not shown)")
        body, _ = redact(body, set(secret_values))
    chunks = [body[i:i + PART] for i in range(0, max(len(body), 1), PART)]
    out, parent = [], None
    for n, chunk in enumerate(chunks, start=1):
        row = {"kind": kind, "body": chunk, "part_no": n, "author": author, "session_id": session_id,
               "source_ref": source_ref, "occurred_at": occurred_at}
        if parent:
            row["parent_id"] = parent
        ep = store.insert("brain_episodes", row)
        parent = parent or ep["id"]
        out.append(ep)
    return out


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def _queue(outbox, draft, judgements, *, write_id=None, options=None):
    outbox = Path(outbox)
    line = json.dumps({"draft": draft, "judgements": judgements or {},
                       "write_id": write_id or str(uuid.uuid4()), "options": options or {},
                       "queued_at": datetime.now(timezone.utc).isoformat()}) + "\n"
    with _locked(outbox):
        with open(outbox, "a") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        _sync_parent(outbox)


def _sync_parent(path):
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _checkpoint(path, items):
    """Persist replay identities before any remote commit or destructive queue handoff."""
    for item in items:
        item["write_id"] = str(uuid.UUID(item["write_id"])) if item.get("write_id") else str(uuid.uuid4())
    data = "".join(json.dumps(item) + "\n" for item in items)
    checkpoint = path.with_name(path.name + ".tmp")
    with open(checkpoint, "w") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(checkpoint, path)
    _sync_parent(path)
    return data


def flush_outbox(store, outbox, *, repo, secret_values, fetched=True):
    """Replay queued writes non-interactively (unjudged candidates land judge_pending). One flusher
    at a time; returns {"busy": True} if another is running."""
    outbox = Path(outbox)
    inflight = outbox.with_name(outbox.name + ".inflight")
    counts = {"written": 0, "kept": 0, "refused": 0}
    outbox.parent.mkdir(parents=True, exist_ok=True)
    with open(outbox.with_name(outbox.name + ".flush.lock"), "a") as fl:
        try:
            fcntl.flock(fl, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(counts, busy=True)
        try:
            with _locked(outbox):
                data = outbox.read_text() if outbox.exists() else ""
                if data.strip():
                    queued = [json.loads(line) for line in data.splitlines() if line.strip()]
                    data = _checkpoint(outbox, queued)
                    with open(inflight, "a") as fh:
                        fh.write(data)
                        fh.flush()
                        os.fsync(fh.fileno())
                    _sync_parent(inflight)
                if outbox.exists():
                    with open(outbox, "w") as fh:
                        fh.flush()
                        os.fsync(fh.fileno())
            if not inflight.exists():
                return counts
            items = [json.loads(line) for line in inflight.read_text().splitlines() if line.strip()]
            _checkpoint(inflight, items)
            kept = []
            for item in items:
                line = json.dumps(item)
                try:
                    r = write_fact(store, item["draft"], repo=repo, judgements=item.get("judgements"),
                                   interactive=False, secret_values=secret_values, fetched=fetched,
                                   write_id=item["write_id"], **item.get("options", {}))
                except BrainUnavailable:
                    kept.append(line)
                    counts["kept"] += 1
                    continue
                counts["written" if r["status"] in ("written", "noop") else "refused"] += 1
            if kept:
                with _locked(outbox):
                    with open(outbox, "a") as fh:
                        fh.write("".join(k + "\n" for k in kept))
                        fh.flush()
                        os.fsync(fh.fileno())
                    _sync_parent(outbox)
            inflight.unlink()
            _sync_parent(inflight)
            return counts
        finally:
            fcntl.flock(fl, fcntl.LOCK_UN)
