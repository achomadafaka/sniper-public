"""Storage for the brain graph. RestStore speaks PostgREST through bot.supabase_helper._request.

Errors, by what the caller should do next:
    BrainUnavailable    the brain could not answer (transport, timeout, HTTP 402/408/429/5xx). Reads fall
                      back to the snapshot and writes go to the outbox.
  BrainMisconfigured  (a BrainUnavailable) the service key is not loaded. Migration 032 denies
                      anon, and PostgREST answers an anon SELECT with [] and no error, so an anon
                      client would read an EMPTY brain. RestStore refuses to start instead.
  VersionConflict     the row moved since it was read: re-read and retry.
  BrainRefused        the database refused the request (a CHECK, a trigger, a missing row). Never
                      queued, because retrying a refused write cannot make it valid.

Section and entity reads go through RPCs (migration 032). A section's entity list can hold
thousands of ids and does not fit a URL, and the candidate stage needs EVERY fact on an entity,
oldest first, not a newest-first page."""
from __future__ import annotations

import os
import re
import urllib.parse


class BrainUnavailable(RuntimeError):
    pass


class BrainMisconfigured(BrainUnavailable):
    pass


class VersionConflict(RuntimeError):
    pass


class BrainRefused(RuntimeError):
    pass


_HTTP = re.compile(r"^HTTP (\d{3}) ")


def _q(v) -> str:
    return urllib.parse.quote(str(v), safe="")


def pg_array(values) -> str:
    """A Postgres array literal with every element quoted, so , { } " and spaces survive."""
    return "{" + ",".join('"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'
                          for v in values) + "}"


FACT_COLUMNS = ("id,kind,statement,detail,entity_ids,polarity,status,judge_pending,valid_from,valid_to,"
                "recorded_at,expired_at,verify_by,evidence,author,author_session,verified_by,verified_at,"
                "verify_note,episode_ids,version")
ENTITY_COLUMNS = "id,type,name,name_lc,aliases,summary,section_id,created_at,updated_at"
EPISODE_COLUMNS = "id,kind,body,parent_id,part_no,author,session_id,occurred_at,recorded_at,source_ref"
# Normal reads retain every functional field; vector workers supply their own select.
_READ_COLUMNS = {'brain_facts': FACT_COLUMNS, 'brain_entities': ENTITY_COLUMNS,
                 'brain_episodes': EPISODE_COLUMNS}


def _pg_in(values) -> str:
    return "(" + ",".join('"' + str(v).replace('"', '\\"') + '"' for v in values) + ")"


class RestStore:
    def __init__(self, request=None, timeout: float = 3.0):
        if request is None:
            import bot.supabase_helper as sb
            service = os.getenv("SUPABASE_SERVICE_KEY") or ""
            if not service or sb.SUPABASE_KEY != service:
                raise BrainMisconfigured(
                    "SUPABASE_SERVICE_KEY is not loaded (load .env before importing "
                    "bot.supabase_helper); an anon key reads an empty brain")
            request = sb._request
        self._request = request
        self._timeout = timeout

    def _call(self, method, path, body=None, prefer=None):
        headers = {"Prefer": prefer} if prefer else None
        try:
            return self._request(method, path, body, headers, timeout=self._timeout)
        except RuntimeError as exc:
            msg = str(exc)
            m = _HTTP.match(msg)
            if not m:
                raise BrainUnavailable(msg[:200]) from None
            code = int(m.group(1))
            if code in (402, 408, 429) or code >= 500:
                raise BrainUnavailable(msg[:200]) from None
            if "version_conflict" in msg:
                raise VersionConflict(msg[:200]) from None
            raise BrainRefused(msg[:300]) from None
        except Exception as exc:
            raise BrainUnavailable(type(exc).__name__) from None

    def _rpc(self, name, args):
        return self._call("POST", f"rpc/{name}", args) or []

    def insert(self, table, row):
        return self._call("POST", table, row, prefer="return=representation")[0]

    def get(self, table, id):
        rows = self._call("GET", f"{table}?select={_READ_COLUMNS.get(table, '*')}"
                                 f"&id=eq.{_q(id)}&limit=1") or []
        return rows[0] if rows else None

    def resolve_fact_id(self, prefix):
        """A full fact id from the 8+ hex-char prefix the boot prints (Lambda 2026-09-29: retract of a
        short id failed with 'invalid input syntax for type uuid'). Ambiguous or unknown -> None."""
        p = str(prefix).strip().lower()
        if len(p) == 36:
            return p
        if not re.fullmatch(r"[0-9a-f-]{8,35}", p):
            return None
        hexs = p.replace("-", "")
        lo = (hexs + "0" * 32)[:32]
        hi = (hexs + "f" * 32)[:32]
        fmt = lambda h: f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
        rows = self._call("GET", f"brain_facts?select=id&id=gte.{fmt(lo)}&id=lte.{fmt(hi)}&limit=2") or []
        return rows[0]["id"] if len(rows) == 1 else None

    def find_entity(self, type, name):
        """Exact name (case-insensitive through the generated name_lc column) or exact alias.
        Never a pattern and never cosine (R1): ilike treats `_` as a wildcard, and every knob has one."""
        lc = name.strip().lower()
        rows = self._call("GET", f"brain_entities?select={ENTITY_COLUMNS}"
                                 f"&type=eq.{_q(type)}&name_lc=eq.{_q(lc)}&limit=1") or []
        if rows:
            return rows[0]
        rows = self._call("GET", f"brain_entities?select={ENTITY_COLUMNS}"
                                 f"&type=eq.{_q(type)}&aliases=cs.{_q(pg_array([lc]))}&limit=1") or []
        return rows[0] if rows else None

    def find_facts(self, entity_ids, kinds=None, live_only=True):
        """Every fact on any of the entities, OLDEST first, no cap."""
        if not entity_ids:
            return []
        args = {"p_entity_ids": list(entity_ids),
                "p_kinds": list(kinds) if kinds else None,
                "p_live_only": bool(live_only)}
        facts, after = [], None
        while True:
            path = f"rpc/brain_facts_for_entities?select={FACT_COLUMNS}&order=id.asc&limit=500"
            if after is not None:
                path += f"&id=gt.{_q(after)}"
            rows = self._call("POST", path, args) or []
            # A server cap can be smaller than our page; only empty means done.
            if not rows:
                return sorted(facts, key=lambda f: (f.get("recorded_at") or "", f["id"]))
            if after is not None and rows[-1]["id"] <= after:
                raise BrainRefused("paging did not advance")
            facts.extend(rows)
            after = rows[-1]["id"]

    def patch_fact(self, id, version, patch):
        rows = self._call("PATCH", f"brain_facts?id=eq.{_q(id)}&version=eq.{int(version)}", patch,
                          prefer="return=representation") or []
        if rows:
            return rows[0]
        if self.get("brain_facts", id) is None:
            raise BrainRefused(f"no fact {id}")
        raise VersionConflict(f"fact {id} is not at version {version}")

    def write_fact(self, row, actions):
        return self._rpc("brain_write_fact", {"p_new": row, "p_actions": actions})[0]

    def supersede(self, old_id, old_version, new_row):
        rows = self._rpc("brain_supersede", {"p_old_id": old_id, "p_old_version": int(old_version),
                                             "p_new": new_row})
        return rows[0]

    def search_facts(self, q, limit=20):
        # Split on anything that is not a letter or digit and KEEP the parts: Postgres' English parser
        # splits "MEMECOIN_REAL_CHASE_MAX_PCT" or "scripts/brain_cli.py" the same way, so the parts
        # match. Keeping only isalnum() words dropped exactly the exact-key queries (review C3).
        words = " ".join(w for w in re.split(r"[^A-Za-z0-9]+", q) if w)
        if not words:
            return []
        from bot.brain_graph import READ_STATUSES
        return self._call("GET", f"brain_facts?select={FACT_COLUMNS}"
                                 f"&search_vector=wfts(english).{_q(words)}"
                                 f"&status=in.({','.join(READ_STATUSES)})&valid_to=is.null"
                                 f"&limit={int(limit)}") or []

    def match_facts(self, embedding, limit=20):
        if embedding is None:
            return []
        from bot.embeddings import to_pgvector
        hits = self._rpc("brain_match_facts", {"query_embedding": to_pgvector(embedding),
                                               "match_count": int(limit)})
        if not hits:
            return []
        from bot.brain_graph import READ_STATUSES
        rows = {r["id"]: r for r in (self._call("GET", f"brain_facts?select={FACT_COLUMNS}&id=in." + _pg_in(h["id"] for h in hits)
                                             + f"&status=in.({','.join(READ_STATUSES)})&valid_to=is.null")
                                     or [])}
        return [rows[h["id"]] for h in hits if h["id"] in rows]

    def facts_by_sha(self, token, limit=20):
        """Live facts that name a commit: evidence {type: sha} whose ref equals the token or one of its 7-12 char prefixes
        (stored refs are 7-40 chars), plus facts on a commit entity whose name starts with the token (entity names are the
        full 40). A token SHORTER than a stored evidence ref only resolves through the entity."""
        import json
        from bot.brain_graph import READ_STATUSES
        refs = [token] + [token[:n] for n in (7, 8, 9, 10, 12) if n < len(token)]
        seen, out = set(), []
        live = f"&status=in.({','.join(READ_STATUSES)})&valid_to=is.null&limit={int(limit)}"
        for ref in dict.fromkeys(refs):
            for f in self._call("GET", f"brain_facts?select={FACT_COLUMNS}&evidence=cs."
                                       f"{_q(json.dumps([{'type': 'sha', 'ref': ref}]))}{live}") or []:
                if f["id"] not in seen:
                    seen.add(f["id"])
                    out.append(f)
        ents = self._call("GET", f"brain_entities?select=id&type=eq.commit&name_lc=like.{_q(token.lower() + '*')}&limit=5") or []
        more = (self._call("POST", f"rpc/brain_facts_for_entities?select={FACT_COLUMNS}&order=id.asc&limit={int(limit)}",
                           {"p_entity_ids": [e["id"] for e in ents], "p_kinds": None, "p_live_only": True}) or []) if ents else []
        for f in more:
            if f["id"] not in seen:
                seen.add(f["id"])
                out.append(f)
        return out[:limit]

    def facts_by_shas(self, tokens, per_limit=20, page=500):
        """Batch of facts_by_sha for a bounded read budget: {token: {"facts": [...], "complete": bool}}. Requests are
        ceil(n/8) evidence reads (one OR-ed containment each) + one commit-entity read (+ one capped facts read if matched),
        not ~7 per token. `complete` is False when a read hit its page cap or the token has more than per_limit facts: a capped
        answer is UNKNOWN beyond what it shows, never proof of absence. An absent token is {"facts": [], "complete": True}.
        Tokens must be 7-40 lowercase hex chars (ValueError otherwise); retracted and superseded facts are not returned."""
        import json
        import re
        from bot.brain_graph import READ_STATUSES
        if type(page) is not int or page<1:raise ValueError('facts_by_shas needs a positive integer page')
        page=min(page,1000)   # deploy/brain-local/docker-compose.yml: the gateway caps rows at 1000.
        toks = list(dict.fromkeys(str(t).lower() for t in tokens))
        if any(not re.fullmatch(r"[0-9a-f]{7,40}", t) for t in toks):
            raise ValueError("facts_by_shas needs 7-40 hex chars per token")
        refs = {t: {t} | {t[:n] for n in (7, 8, 9, 10, 12) if n < len(t)} for t in toks}
        found, truncated = {t: {} for t in toks}, set()
        live = f"&status=in.({','.join(READ_STATUSES)})&valid_to=is.null&limit={int(page)}"
        for i in range(0, len(toks), 8):
            chunk = toks[i:i + 8]
            conds = ",".join('evidence.cs."' + json.dumps([{"type": "sha", "ref": r}]).replace('"', '\\"') + '"'
                             for r in sorted(set().union(*(refs[t] for t in chunk))))
            rows = self._call("GET", f"brain_facts?select={FACT_COLUMNS}&or=({_q(conds)}){live}") or []
            if len(rows) >= page:
                truncated.update(chunk)
            for f in rows:
                have = {e.get("ref") for e in f.get("evidence") or [] if e.get("type") == "sha"}
                for t in chunk:
                    if refs[t] & have:
                        found[t][f["id"]] = f
        exact = [t for t in toks if len(t) == 40]
        part = [t for t in toks if len(t) < 40]
        names = (f"name_lc=in.({','.join(_q(t) for t in exact)})" if exact and not part else
                 "or=(" + _q(",".join([f"name_lc.in.({','.join(exact)})"] * bool(exact) + [f"name_lc.like.{t}*" for t in part])) + ")")
        ents = (self._call("GET", f"brain_entities?select=id,name_lc&type=eq.commit&{names}&limit={int(page)}") or []) if toks else []
        if len(ents) >= page:
            truncated.update(toks)
        if ents:
            rows = self._call("POST", f"rpc/brain_facts_for_entities?select={FACT_COLUMNS}&order=id.asc&limit={int(page)}",
                              {"p_entity_ids": [e["id"] for e in ents], "p_kinds": None, "p_live_only": True}) or []
            if len(rows) >= page:
                truncated.update(t for t in toks if any(e["name_lc"].startswith(t) for e in ents))
            for f in rows:
                for t in toks:
                    if set(f["entity_ids"]) & {e["id"] for e in ents if e["name_lc"].startswith(t)}:
                        found[t][f["id"]] = f
        out = {}
        for t in toks:
            facts = sorted(found[t].values(), key=lambda f: (f.get("recorded_at") or "", f["id"]), reverse=True)
            out[t] = {"facts": facts[:per_limit], "complete": t not in truncated and len(facts) <= per_limit}
        return out

    def facts_in_sections(self, section_ids, kinds=None, *, queue_only=False, exclude_author=None,
                          oldest_first=False, limit=500):
        if not section_ids:
            return []
        # No embedding or search_vector: the boot never prints them and they were ~80% of the bytes
        # (1.2 s of a 3 s boot budget, measured 2026-09-29).
        return self._call("POST", "rpc/brain_facts_in_sections?select=" + FACT_COLUMNS, {
            "p_section_ids": list(section_ids), "p_kinds": list(kinds) if kinds else None,
            "p_queue_only": bool(queue_only), "p_exclude_author": exclude_author,
            "p_oldest_first": bool(oldest_first), "p_limit": int(limit)}) or []

    def queue_count(self, section_ids, exclude_author=None) -> int:
        if not section_ids:
            return 0
        n = self._rpc("brain_queue_count", {"p_section_ids": list(section_ids),
                                            "p_exclude_author": exclude_author})
        return int(n if isinstance(n, int) else (n[0] if n else 0))

    def unverified_queue(self, role, section_ids, limit):
        """Unverified or judge_pending facts another role wrote, OLDEST first (the true oldest,
        chosen in the database, not the oldest of a newest-first page)."""
        return self.facts_in_sections(section_ids, None, queue_only=True, exclude_author=role,
                                      oldest_first=True, limit=limit)

    def sections(self, slugs=None):
        path = "brain_sections?order=slug.asc"
        if slugs:
            path += "&slug=in." + _q(_pg_in(slugs))
        return self._call("GET", path) or []

    def view(self, name):
        rows = self._call("GET", f"brain_views?name=eq.{_q(name)}&limit=1") or []
        return rows[0] if rows else None

    def patch_view(self, ident, patch):
        if set(patch) != {'query_spec'}:
            raise ValueError('only query_spec may be patched')
        rows = self._call('PATCH', 'brain_views?name=eq.' + _q(ident), patch, prefer='return=representation') or []
        if not rows:
            raise BrainRefused('view disappeared')
        return rows[0]

    def recent_episodes(self, author, limit):
        return self._call("GET", f"brain_episodes?select={EPISODE_COLUMNS}"
                                 f"&author=eq.{_q(author)}&order=recorded_at.desc"
                                 f"&limit={int(limit)}") or []

    def episodes_since(self, kinds, since_iso, limit):
        """Newest-first episodes of the given kinds recorded at/after since_iso (the boot's 48 h)."""
        return self._call("GET", "brain_episodes?select=id,kind,body,author,recorded_at,source_ref&author=neq.import&kind=in."
                                 + _q(_pg_in(kinds))
                                 + f"&recorded_at=gte.{_q(since_iso)}&order=recorded_at.desc"
                                 f"&limit={int(limit)}") or []

    def rows_page(self, table, *, select=None, filters=(), after_id=None, limit=500):
        limit = max(1, min(int(limit), 500))
        if select is None:
            select = _READ_COLUMNS.get(table, '*')
        path = f'{table}?select={_q(select)}&order=id.asc&limit={limit}'
        for column, op, value in filters:
            path += f'&{column}={op}.{_q(value)}'
        if after_id:
            path += '&id=gt.' + _q(after_id)
        return self._call('GET', path) or []

    def _all(self, table, **kw):
        limit = max(1, min(int(kw.pop('limit', 500)), 500))
        rows, after = [], None
        while True:
            page = self.rows_page(table, after_id=after, limit=limit, **kw)
            rows.extend(page)
            if len(page) < limit:
                return rows
            if page[-1]['id'] == after:
                raise BrainRefused('paging did not advance')
            after = page[-1]['id']

    def episode_index(self, author, prefix):
        out = {}
        for r in self._all('brain_episodes', filters=(('author', 'eq', author),
                                                     ('source_ref', 'like', prefix + '*'))):
            slot = out.setdefault(r['source_ref'], {'parent': None, 'parts': 0, 'rows': []})
            slot['parts'] += 1
            slot['rows'].append(r)
            if r['part_no'] == 1:
                slot['parent'] = r['id']
        return out

    def episode_refs(self, author, prefix):
        return set(self.episode_index(author, prefix))

    def fact_index(self, session):
        from bot.brain_graph.import_sources import normalize
        return {normalize(r['statement']): r for r in self._all(
            'brain_facts', filters=(('author_session', 'eq', session),))}

    def insert_many(self, table, rows, *, ignore_duplicates=False):
        if not rows:
            return []
        path = table + ('?on_conflict=id' if ignore_duplicates else '')
        prefer = 'return=representation' + (',resolution=ignore-duplicates' if ignore_duplicates else '')
        return self._call('POST', path, list(rows), prefer=prefer) or []

    def live_facts_page(self, after_id=None, limit=500):
        from bot.brain_graph import READ_STATUSES
        return self.rows_page('brain_facts', after_id=after_id, limit=limit,
            filters=(('valid_to', 'is', 'null'), ('kind', 'neq', 'observation'),
                     ('status', 'in', '(' + ','.join(READ_STATUSES) + ')')))

    def links_among(self, fact_ids):
        out, ids = {}, list(dict.fromkeys(fact_ids))
        for i in range(0, len(ids), 100):
            for col in ('from_id', 'to_id'):
                for row in self._all('brain_links', filters=((col, 'in', _pg_in(ids[i:i + 100])),)):
                    out[row['id']] = row
        return list(out.values())

    def patch_row(self, table, id, patch):
        if table not in ('brain_entities', 'brain_sections'):
            raise BrainRefused(f'patch_row does not write {table}')
        rows = self._call('PATCH', f'{table}?id=eq.{_q(id)}', patch, prefer='return=representation') or []
        if not rows:
            raise BrainRefused(f'no {table} row {id}')
        return rows[0]

    def insert_health(self, metrics):
        return self.insert('brain_health', {'metrics': metrics})

    def judged_pairs(self, fact_ids):
        out, ids = set(), list(dict.fromkeys(fact_ids))
        for i in range(0, len(ids), 100):
            for col in ('pair_lo', 'pair_hi'):
                after = None
                while True:
                    path = (f'brain_judgements?{col}=in.{_q(_pg_in(ids[i:i + 100]))}'
                            '&select=pair_lo,pair_hi&order=pair_lo.asc,pair_hi.asc&limit=500')
                    if after:
                        lo, hi = after
                        path += '&or=' + _q(f'(pair_lo.gt.{lo},and(pair_lo.eq.{lo},pair_hi.gt.{hi}))')
                    rows = self._call('GET', path) or []
                    out.update((r['pair_lo'], r['pair_hi']) for r in rows)
                    if len(rows) < 500:
                        break
                    next_after = (rows[-1]['pair_lo'], rows[-1]['pair_hi'])
                    if next_after == after:
                        raise BrainRefused('judgement paging did not advance')
                    after = next_after
        return out

    def record_judgement(self, a, b, verdict, author):
        from bot.brain_graph import AUTHORS, VERDICTS
        if a == b or verdict not in VERDICTS or author not in AUTHORS:
            raise BrainRefused('judgement CHECK')
        lo, hi = sorted((a, b))
        self._call('POST', 'brain_judgements?on_conflict=pair_lo,pair_hi',
                   {'pair_lo': lo, 'pair_hi': hi, 'verdict': verdict, 'author': author},
                   prefer='resolution=ignore-duplicates,return=minimal')

    def queue_candidate(self, newer_id, older_id, author='cron'):
        return bool(self._rpc('brain_queue_candidate',
            {'p_newer': newer_id, 'p_older': older_id, 'p_author': author}))
