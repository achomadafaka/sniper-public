"""Office Task 11 (Lambda): memory core. Known-time retrieval, supersession as of the decision cut, and sealed snapshots. PURE over a two-method backend
(`entries(section=None, cut=None)`, `get(ids)`, `add(row) -> entry_id`, `rank(query, pool)`); sniper/office/memory_store.py implements it over the Office store (memory_entries + FTS5), with entry_id = the row_hash.
Rules: (1) known_at is stamped HERE from a trusted corrected clock at write time; an author-supplied known_at or an unknown/non-finite clock is REFUSED (no backdating).
(2) retrieve filters FIRST (known_at <= decision cut, supersession as of the cut, same-coin / same-creator / same-minute-window sources excluded, uncommitted pitches hidden from other viewers) and only then ranks and
takes k, so a filtered top-k can never come back short while visible entries exist. (3) A sealed snapshot stores exact membership with content sha256 and the supersession history; retrieval from a snapshot reads only that
membership and re-verifies every member's content hash: an edited or deleted member is REFUSED, a later write cannot change it. Evaluation-derived lessons are written after the seal and are therefore invisible to it."""
import hashlib
import json
import math


class Refused(Exception):
    pass


def _canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _content_sha(e):
    """Hash EVERY field of the stored entry (no whitelist): anything a reader consumes or returns (decision_id, kind, author, sources, section, text, ...) is pinned, so editing any of it after the seal is refused."""
    return hashlib.sha256(_canon(e)).hexdigest()


def write(backend, clock, entry):
    if "known_at" in entry or "entry_id" in entry:
        raise Refused("known_at and entry_id are stamped by the memory writer, never supplied")
    t = clock()
    if t is None or isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t):
        raise Refused("unknown or non-finite clock: refusing to stamp known_at")
    row = dict(entry, known_at=float(t))
    if row.get("supersedes") and row["supersedes"] not in backend.get([row["supersedes"]]):
        raise Refused("supersedes an entry that does not exist")
    return backend.add(row)                                           # the backend assigns the identity (the store's row_hash)


def _shares_source(e, cand):
    for s in e.get("sources", []):
        if s.get("coin") == cand.get("coin") or s.get("creator") == cand.get("creator") or s.get("minute") == cand.get("minute"):
            return True
    return False


def _visible(entries, cut):
    """known_at <= cut, then supersession as of the cut: an entry is hidden only by a replacement that was itself known by the cut."""
    known = [e for e in entries if e["known_at"] <= cut]
    replaced = {e["supersedes"] for e in known if e.get("supersedes")}
    return [e for e in known if e["entry_id"] not in replaced]


ROLES = frozenset({"Head", "Risk", "Trader", "Postmortem", "Desk ops", "Quant", "Scout", "Analyst", "Judge", "Chooser", "System"})
QUARANTINED_ROLES = frozenset({"Head", "Quant", "Chooser"})   # spec: evaluation results are invisible to the chooser and the Quant before the preregistered look; in this org the adaptive chooser IS the Head
# "System" is the trusted-boundary identity (sealer, auditor, grader); an agent turn must be given its real role by the supervisor. viewer_role is REQUIRED: None / unknown is refused, never a permissive default.


def _snapshot_id(snap):
    body = {k: snap[k] for k in ("epoch_id", "cut", "members", "supersessions")}
    return hashlib.sha256(_canon(body)).hexdigest()


def retrieve(backend, query, *, decision_cut, candidate, viewer_role, k=5, viewer=None, snapshot=None, trusted_snapshot_id=None, released_decisions=frozenset()):
    """snapshot mode needs `trusted_snapshot_id` (the sealed id recorded by the trusted sealer): the dict is never trusted, its id is RECOMPUTED from its body and compared to both. The decision cut still applies inside a snapshot
    (an entry known after the decision is never served), with supersession as of that cut. A peer's pitch is visible only to its author, or once the TRUSTED decision-scoped barrier released that decision_id (`released_decisions`):
    a per-entry flag cannot release it."""
    if viewer_role not in ROLES:
        raise Refused("viewer_role %r is not a known Office role" % (viewer_role,))
    quarantined = viewer_role in QUARANTINED_ROLES
    if quarantined and snapshot is None:
        raise Refused("role %s reads sealed snapshots only" % viewer_role)
    if snapshot is not None:
        if not trusted_snapshot_id or _snapshot_id(snapshot) != snapshot.get("snapshot_id") or snapshot["snapshot_id"] != trusted_snapshot_id:
            raise Refused("sealed snapshot id does not match its body / the trusted sealed id")
        by_id = backend.get([m["entry_id"] for m in snapshot["members"]] + [sp["old"] for sp in snapshot["supersessions"]])
        members = []
        for m in snapshot["members"]:
            e = by_id.get(m["entry_id"])
            if e is None or _content_sha(e) != m["content_sha256"]:
                raise Refused("sealed snapshot member %s missing or altered" % m["entry_id"])
            members.append(e)
        olds = []
        for sp in snapshot["supersessions"]:                              # the history is pinned too: an edited or deleted superseded entry is refused
            old = by_id.get(sp["old"])
            if old is None or _content_sha(old) != sp["old_sha256"]:
                raise Refused("sealed snapshot supersession history %s missing or altered" % sp["old"])
            olds.append(old)
        pool = _visible(members + olds, decision_cut)                     # the decision cut and supersession as of it, inside the sealed membership
    else:
        pool = _visible(backend.entries(cut=decision_cut), decision_cut)         # the known_at cut is applied by the backend through its index
    if quarantined:
        pool = [e for e in pool if not e.get("evaluation_derived")]
    pool = [e for e in pool if not _shares_source(e, candidate) and not (e.get("kind") == "pitch" and e.get("author") != viewer and e.get("decision_id") not in released_decisions)]
    return backend.rank(query, pool)[:k]


def seal_snapshot(backend, *, epoch_id, cut):
    allk = backend.entries(cut=cut)
    vis = _visible(allk, cut)
    members = [{"entry_id": e["entry_id"], "content_sha256": _content_sha(e)} for e in sorted(vis, key=lambda e: (e["known_at"], e["entry_id"]))]
    by_id = {e["entry_id"]: e for e in allk}
    sup = [{"old": e["supersedes"], "new": e["entry_id"], "old_sha256": _content_sha(by_id[e["supersedes"]])} for e in sorted(allk, key=lambda e: (e["known_at"], e["entry_id"])) if e.get("supersedes")]
    body = {"epoch_id": epoch_id, "cut": cut, "members": members, "supersessions": sup}
    return dict(body, snapshot_id=hashlib.sha256(_canon(body)).hexdigest())
