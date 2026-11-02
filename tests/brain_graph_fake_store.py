"""In-memory BrainStore that carries migration 032's CHECKs and triggers, for offline tests.

Import it as `from brain_graph_fake_store import FakeStore` (tests/ is on sys.path the way
`_web_csrf` is). `import tests...` resolves to a stray site-packages package on this host.

Every rule the database enforces is mirrored here and raises BrainRefused, the error RestStore
raises for a database refusal. A rule that exists only in the fake would let a test pass against
behaviour the real database refuses, so keep the two in step with supabase_032_brain_graph.sql."""
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from bot.brain_graph import AUTHORS, ENTITY_TYPES, KINDS, READ_STATUSES, STATUSES, VERIFIERS
from bot.brain_graph.store import BrainRefused, VersionConflict

_DAYS = {"measurement": 7, "settled": 60, "task": 3}
_VERIFIED = ("verified", "verified_partial", "refuted")
_EP_KINDS = ("handoff_line", "operator_order", "crew_report", "command_output", "commit", "doc",
             "transcript_excerpt", "legacy_memory", "handover", "selftest")
_RELS = ("supports", "contradicts", "supersedes", "depends_on", "derived_from", "same_as", "part_of",
         "candidate")
_FACT_IMMUTABLE = ("statement", "kind", "author", "recorded_at", "entity_ids", "valid_from", "polarity",
                   "author_session")


def _refs(evidence):
    return sorted((str(e.get("type")), str(e.get("ref"))) for e in (evidence or []))


class FakeStore:
    def __init__(self):
        self.rows = {t: [] for t in ("brain_sections", "brain_episodes", "brain_entities", "brain_facts",
                                     "brain_links", "brain_views", "brain_health")}
        self.clock = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    def _now(self):
        self.clock += timedelta(seconds=1)
        return self.clock.isoformat()

    # ------------------------------------------------------------------ inserts
    def insert(self, table, row):
        row = dict(row)
        row.setdefault("id", str(uuid.uuid4()))
        getattr(self, "_check_" + table[len("brain_"):], lambda r: None)(row)
        self.rows[table].append(row)
        return dict(row)

    def _check_sections(self, row):
        if any(s["slug"] == row["slug"] for s in self.rows["brain_sections"]):
            raise BrainRefused("duplicate section slug")

    def _check_entities(self, row):
        if row.get("type") not in ENTITY_TYPES:
            raise BrainRefused(f"entity type {row.get('type')!r} violates the type CHECK")
        name = (row.get("name") or "").strip()
        if not 1 <= len(name) <= 300:
            raise BrainRefused("entity name length")
        if not row.get("section_id") or not any(s["id"] == row["section_id"] for s in self.rows["brain_sections"]):
            raise BrainRefused("section_id is NOT NULL and must reference a section")
        lc = row["name"].lower()
        aliases = sorted({a.strip().lower() for a in row.get("aliases") or [] if a.strip()} - {lc})
        for e in self.rows["brain_entities"]:
            if e["type"] != row["type"]:
                continue
            if e["name_lc"] == lc:
                raise BrainRefused("duplicate (type, name_lc)")
            if e["name_lc"] in aliases or set(e["aliases"]) & (set(aliases) | {lc}):
                raise BrainRefused(f"alias_conflict with entity {e['id']}")
        row.update(name_lc=lc, aliases=aliases)
        row.setdefault("summary", None)

    def _check_facts(self, row):
        if row.get("kind") not in KINDS:
            raise BrainRefused("kind CHECK")
        if not 8 <= len(row.get("statement") or "") <= 600:
            raise BrainRefused("statement length CHECK")
        if len(row.get("detail") or "") > 4096:
            raise BrainRefused("detail length CHECK")
        if not row.get("entity_ids"):
            raise BrainRefused("entity_ids cardinality CHECK")
        if row.get("author") not in AUTHORS:
            raise BrainRefused("author CHECK")
        if row["kind"] != "observation" and not row.get("evidence"):
            raise BrainRefused("evidence CHECK")
        if row["kind"] == "observation" and not row.get("episode_ids"):
            raise BrainRefused("observation episode CHECK")
        now = self._now()
        row.update(recorded_at=now, version=1)
        row.setdefault("status", "unverified")
        row.setdefault("judge_pending", False)
        row.setdefault("valid_from", now)
        row.setdefault("valid_to", None)
        row.setdefault("expired_at", None)
        row.setdefault("episode_ids", [])
        row.setdefault("polarity", 0)
        row.setdefault("detail", None)
        row.setdefault("author_session", None)
        row.setdefault("verified_by", None)
        row.setdefault("verified_at", None)
        row.setdefault("verify_note", None)
        if row["status"] not in STATUSES:
            raise BrainRefused("status CHECK")
        if row.get("verify_by") is None:
            row["verify_by"] = ("infinity" if row["kind"] == "observation" else
                                (self.clock + timedelta(days=_DAYS.get(row["kind"], 30))).isoformat())
        self._check_fact_row(row)

    def _check_fact_row(self, r):
        if r["verified_by"] is not None and r["verified_by"] not in VERIFIERS:
            raise BrainRefused("verified_by CHECK")
        if r["verified_by"] is not None and r["verified_by"] == r["author"]:
            raise BrainRefused("CHECK verified_by <> author")
        if r["status"] in _VERIFIED and r["verified_by"] is None:
            raise BrainRefused("CHECK verified status needs verified_by")
        if (r["valid_to"] is not None and datetime.fromisoformat(r["valid_to"].replace("Z", "+00:00"))
                < datetime.fromisoformat(r["valid_from"].replace("Z", "+00:00"))):
            raise BrainRefused("CHECK valid_to >= valid_from")

    def _check_episodes(self, row):
        if row.get("kind") not in _EP_KINDS:
            raise BrainRefused("episode kind CHECK")
        if len(row.get("body") or "") > 1800:
            raise BrainRefused("episode part over 1800 chars")
        if row.get("author") not in AUTHORS:
            raise BrainRefused("episode author CHECK")
        row.setdefault("part_no", 1)
        row.setdefault("parent_id", None)
        row.setdefault("occurred_at", None)                  # NULL = unknown, never "now"
        row["recorded_at"] = self._now()

    def _check_links(self, row):
        if row.get("rel") not in _RELS or row.get("author") not in AUTHORS:
            raise BrainRefused("link CHECK")
        if any((l["from_id"], l["to_id"], l["rel"]) == (row["from_id"], row["to_id"], row["rel"])
               for l in self.rows["brain_links"]):
            raise BrainRefused("duplicate link")

    # ------------------------------------------------------------------ fact updates (the trigger)
    def patch_fact(self, id, version, patch):
        for r in self.rows["brain_facts"]:
            if r["id"] != id:
                continue
            if r["version"] != version:
                raise VersionConflict(id)
            new = dict(r, **patch)
            for k in _FACT_IMMUTABLE:
                if new.get(k) != r.get(k):
                    raise BrainRefused(f"{k} is immutable; supersede instead")
            for k in ("valid_to", "expired_at"):
                if r[k] is not None and new[k] != r[k]:
                    raise BrainRefused("valid_to is write-once (a closed fact cannot be revived)")
            if _refs(new.get("evidence")) != _refs(r.get("evidence")):
                if new["status"] in _VERIFIED:
                    new["status"] = "unverified"
                new["verified_by"] = None
                new["verified_at"] = None
            if new["status"] in _VERIFIED and new["status"] != r["status"]:
                if r["status"] == "disputed":
                    raise BrainRefused("resolve the dispute first")
                if new["judge_pending"]:
                    raise BrainRefused("judge_pending candidates must be judged before verification")
            if new["status"] not in STATUSES:
                raise BrainRefused("status CHECK")
            self._check_fact_row(new)
            new["version"] = r["version"] + 1
            r.clear()
            r.update(new)
            return dict(r)
        raise BrainRefused(f"no fact {id}")

    def write_fact(self, row, actions):
        """Mirror 035's single transaction, including rollback on a rejected graph write."""
        existing = self.get("brain_facts", row["id"])
        if existing:
            if any(existing[k] != row[k] for k in ("kind", "statement", "author", "entity_ids")):
                raise BrainRefused("operation_identity_conflict")
            return existing
        before = {table: deepcopy(self.rows.get(table, []))
                  for table in ('brain_facts', 'brain_links', 'brain_judgements')}
        try:
            for action in actions:
                if action["verdict"] in ("supersedes", "contradicts"):
                    old = self.get("brain_facts", action["id"])
                    if (not old or old["version"] != action["version"] or not self._live(old)):
                        raise VersionConflict("version_conflict")
            starts = row.get("valid_from") or self._now()
            for action in actions:
                if action["verdict"] == "supersedes":
                    old = self.get("brain_facts", action["id"])
                    if starts < old["valid_from"]:
                        raise BrainRefused("supersede_backdated")
            new = self.insert("brain_facts", dict(row, valid_from=starts,
                status="disputed" if any(a["verdict"] == "contradicts" for a in actions) else row.get("status", "unverified"),
                expired_at=self._now() if row.get("valid_to") else None,
                judge_pending=bool(row.get("judge_pending")) or any(a["verdict"] == "candidate" for a in actions)))
            for action in actions:
                verdict = action["verdict"]
                if verdict in ("supersedes", "contradicts"):
                    patch = ({"status": "superseded", "valid_to": starts, "expired_at": new["recorded_at"]}
                             if verdict == "supersedes" else {"status": "disputed"})
                    self.patch_fact(action["id"], action["version"], patch)
                if verdict != "candidate":
                    self.record_judgement(new["id"], action["id"], verdict, new["author"])
                if verdict != "unrelated":
                    self.insert("brain_links", {"from_id": new["id"], "from_kind": "fact",
                        "to_id": action["id"], "to_kind": "fact",
                        "rel": "derived_from" if verdict == "refines" else verdict, "author": new["author"]})
            return new
        except BaseException:
            self.rows.update(before)
            raise

    def supersede(self, old_id, old_version, new_row):
        """Mirrors brain_supersede: only the RPC's fields reach the new row."""
        old = self.get("brain_facts", old_id)
        if old is None or old["version"] != old_version or old["valid_to"] is not None:
            raise VersionConflict("version_conflict")
        starts = new_row.get("valid_from") or self._now()
        if str(starts) < str(old["valid_from"]):
            raise BrainRefused("supersede_backdated")
        self.patch_fact(old_id, old_version, {"valid_to": starts, "expired_at": self._now(),
                                              "status": "superseded"})
        keep = ("kind", "statement", "detail", "entity_ids", "polarity", "evidence", "author",
                "author_session", "episode_ids", "verify_by", "judge_pending", "status")
        new = self.insert("brain_facts", dict({k: new_row.get(k) for k in keep if new_row.get(k) is not None},
                                              valid_from=starts))
        self.insert("brain_links", {"from_id": new["id"], "from_kind": "fact", "to_id": old_id,
                                    "to_kind": "fact", "rel": "supersedes", "author": new["author"]})
        return new

    # ------------------------------------------------------------------ reads
    def resolve_fact_id(self, prefix):
        p = str(prefix).strip().lower()
        hits = [r["id"] for r in self.rows["brain_facts"] if r["id"].lower().startswith(p)]
        return hits[0] if len(hits) == 1 else None

    def get(self, table, id):
        return next((dict(r) for r in self.rows[table] if r["id"] == id), None)

    def find_entity(self, type, name):
        lc = name.strip().lower()
        return next((dict(r) for r in self.rows["brain_entities"] if r["type"] == type and
                     (r["name_lc"] == lc or lc in r["aliases"])), None)

    def _live(self, f):
        return f["status"] in READ_STATUSES and f["valid_to"] is None

    def find_facts(self, entity_ids, kinds=None, live_only=True):
        out = [f for f in self.rows["brain_facts"]
               if set(f["entity_ids"]) & set(entity_ids) and (not kinds or f["kind"] in kinds)
               and (not live_only or self._live(f))]
        return [dict(f) for f in sorted(out, key=lambda f: f["recorded_at"])]

    def search_facts(self, q, limit=20):
        import re
        words = [w.lower() for w in re.split(r"[^A-Za-z0-9]+", q) if w]
        hits = [f for f in self.rows["brain_facts"] if self._live(f)
                and any(w in (f["statement"] + " " + (f.get("detail") or "")).lower() for w in words)]
        return [dict(f) for f in hits[:limit]]

    def match_facts(self, embedding, limit=20):
        return []

    def facts_by_shas(self, tokens, per_limit=20, page=500):
        out = {}
        for t in dict.fromkeys(x.lower() for x in tokens):
            facts = self.facts_by_sha(t, limit=page + 1)
            out[t] = {"facts": facts[:per_limit], "complete": len(facts) <= min(per_limit, page)}
        return out

    def facts_by_sha(self, token, limit=20):
        refs = {token} | {token[:n] for n in (7, 8, 9, 10, 12) if n < len(token)}
        ents = {e["id"] for e in self.rows["brain_entities"] if e["type"] == "commit" and e["name_lc"].startswith(token)}
        out = [f for f in self.rows["brain_facts"] if self._live(f) and (
            any(e.get("type") == "sha" and e.get("ref") in refs for e in f.get("evidence") or []) or set(f["entity_ids"]) & ents)]
        return [dict(f) for f in out[:limit]]

    def _in_sections(self, section_ids):
        ents = {e["id"] for e in self.rows["brain_entities"] if e["section_id"] in section_ids}
        return [f for f in self.rows["brain_facts"] if self._live(f) and f["kind"] != "observation"
                and set(f["entity_ids"]) & ents]

    def facts_in_sections(self, section_ids, kinds=None, *, queue_only=False, exclude_author=None,
                          oldest_first=False, limit=500):
        out = [f for f in self._in_sections(section_ids) if (not kinds or f["kind"] in kinds)
               and (not queue_only or f["status"] == "unverified" or f["judge_pending"])
               and (exclude_author is None or f["author"] != exclude_author)]
        out.sort(key=lambda f: f["recorded_at"], reverse=not oldest_first)
        return [dict(f) for f in out[:max(1, limit)]]

    def queue_count(self, section_ids, exclude_author=None):
        return len([f for f in self._in_sections(section_ids)
                    if (f["status"] == "unverified" or f["judge_pending"])
                    and (exclude_author is None or f["author"] != exclude_author)])

    def unverified_queue(self, role, section_ids, limit):
        return self.facts_in_sections(section_ids, None, queue_only=True, exclude_author=role,
                                      oldest_first=True, limit=limit)

    def sections(self, slugs=None):
        return [dict(s) for s in sorted(self.rows["brain_sections"], key=lambda s: s["slug"])
                if not slugs or s["slug"] in slugs]

    def view(self, name):
        return next((dict(v) for v in self.rows["brain_views"] if v["name"] == name), None)

    def patch_view(self, ident, patch):
        if set(patch) != {'query_spec'}:
            raise ValueError('only query_spec may be patched')
        row = next(r for r in self.rows['brain_views'] if r['name'] == ident)
        row.update(patch)
        return dict(row)

    def recent_episodes(self, author, limit):
        eps = [e for e in self.rows["brain_episodes"] if e["author"] == author]
        return [dict(e) for e in reversed(eps)][:limit]

    def episodes_since(self, kinds, since_iso, limit):
        eps = [e for e in self.rows["brain_episodes"]
               if e["kind"] in kinds and e.get("author") != "import"
               and (e.get("recorded_at") or "") >= since_iso]
        return [dict(e) for e in sorted(eps, key=lambda e: e.get("recorded_at") or "", reverse=True)][:limit]

    def rows_page(self, table, *, select='*', filters=(), after_id=None, limit=500):
        rows = [dict(r) for r in self.rows[table]]
        for col, op, val in filters:
            if op == 'eq':
                rows = [r for r in rows if r.get(col) == val]
            elif op == 'neq':
                rows = [r for r in rows if r.get(col) != val]
            elif op == 'like':
                rows = [r for r in rows if str(r.get(col) or '').startswith(val.rstrip('*'))]
            elif op == 'in':
                values = [v.strip('"') for v in val.strip('()').split(',')]
                rows = [r for r in rows if r.get(col) in values]
            elif op == 'is' and val == 'null':
                rows = [r for r in rows if r.get(col) is None]
            else:
                raise AssertionError((col, op, val))
        rows = sorted((r for r in rows if after_id is None or r['id'] > after_id), key=lambda r: r['id'])
        rows = rows[:max(1, min(int(limit), 500))]
        return rows if select == '*' else [{k: r.get(k) for k in select.split(',')} for r in rows]

    from bot.brain_graph.store import RestStore as _Paging
    _all = _Paging._all
    episode_index = _Paging.episode_index
    episode_refs = _Paging.episode_refs
    fact_index = _Paging.fact_index
    live_facts_page = _Paging.live_facts_page
    links_among = _Paging.links_among

    def insert_many(self, table, rows, *, ignore_duplicates=False):
        from copy import deepcopy
        before = deepcopy(self.rows)
        try:
            return [self.insert(table, r) for r in rows
                    if not (ignore_duplicates and r.get('id') and self.get(table, r['id']))]
        except BaseException:
            self.rows = before
            raise

    def patch_row(self, table, id, patch):
        if table not in ('brain_entities', 'brain_sections'):
            raise BrainRefused(f'patch_row does not write {table}')
        for row in self.rows[table]:
            if row['id'] == id:
                row.update(patch)
                return dict(row)
        raise BrainRefused(f'no {table} row {id}')

    def insert_health(self, metrics):
        return self.insert('brain_health', {'metrics': metrics, 'run_at': self._now()})

    def judged_pairs(self, fact_ids):
        ids = set(fact_ids)
        return {(j['pair_lo'], j['pair_hi']) for j in self.rows.setdefault('brain_judgements', [])
                if j['pair_lo'] in ids or j['pair_hi'] in ids}

    def record_judgement(self, a, b, verdict, author):
        from bot.brain_graph import AUTHORS, VERDICTS
        if a == b or verdict not in VERDICTS or author not in AUTHORS:
            raise BrainRefused('judgement CHECK')
        lo, hi = sorted((a, b))
        rows = self.rows.setdefault('brain_judgements', [])
        if not any((j['pair_lo'], j['pair_hi']) == (lo, hi) for j in rows):
            rows.append(dict(pair_lo=lo, pair_hi=hi, verdict=verdict, author=author, created_at=self._now()))

    def queue_candidate(self, newer_id, older_id, author='cron'):
        newer, older = self.get('brain_facts', newer_id), self.get('brain_facts', older_id)
        if (not newer or not older or not self._live(newer) or not self._live(older)
                or tuple(sorted((newer_id, older_id))) in self.judged_pairs([newer_id])):
            return False
        changed = False
        if not any(l['from_id'] == newer_id and l['to_id'] == older_id and l['rel'] == 'candidate'
                   for l in self.rows['brain_links']):
            changed = True
            self.insert('brain_links', dict(from_id=newer_id, from_kind='fact', to_id=older_id,
                to_kind='fact', rel='candidate', author=author))
        if not newer.get('judge_pending'):
            changed = True
            self.patch_fact(newer_id, newer['version'], {'judge_pending': True})
        return changed
