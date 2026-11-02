"""Office Task 10 persistence (Lambda): hire / fire / update_agent / change_org / epochs on the Office store. The ONLY writer of `agents` and `epochs` (Codex's Supervisor reads agents, never writes).
Every change is ONE write transaction: the agent row, an append-only `org_changes` row and the epoch (the previous open epoch is closed, the new one is linked to the org change seq) land together or not at all.
hire is idempotent on a key (agent_id = ag_ + sha256(key)[:16], PK existence, so a crash/restart never makes two agents and never regenerates a persona). Persona generation is an INJECTED callable (a model call routed through
the Task 2 scheduler by the caller; it runs OUTSIDE the write lock). A persona may carry no performance claims (numbers/percent/win-rate/profit): only the stored career record states performance.
generation increments on any prompt/persona/model change and on archive; accept_answer rejects an answer whose generation is not the current one or whose agent is archived. Risk and Postmortem can be restaffed, never removed.
The Head can apply persona/prompt/org changes but never limits['head_forbidden_changes']; crew-made changes of those kinds are allowed and open an epoch too. Unknown kinds and unknown clocks write nothing."""
import hashlib
import json
import math
import re
import time

from sniper.office import org as O
from sniper.office import store as S

_PERF = re.compile(r"[$%]|\d|\b(win\s*rate|profit|p\s*&\s*l|pnl|returns?|roi|record)\b", re.I)


class OrgRefused(Exception):
    pass


class BadPersona(ValueError):
    pass


ACTORS = frozenset({"head", "crew"})                  # the Head is the only Office actor allowed to change the org; "crew" is the trusted crew path. Anything else (an agent name, model text) is REFUSED, never implicitly crew.


def _actor(by):
    if by not in ACTORS:
        raise OrgRefused("unknown_actor: %r" % (by,))
    return by


def _now(clock):
    t = clock()
    if t is None or isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t):
        raise OrgRefused("clock unknown or non-finite: refusing to write")
    return float(t)


def _agent(conn, aid):
    r = conn.execute("SELECT payload_json FROM agents WHERE id=?", (aid,)).fetchone()
    return None if r is None else json.loads(r[0])


def _put_agent(conn, a, new=False):
    body = S.canonical_json(a).decode("utf-8")
    if new:
        conn.execute("INSERT INTO agents(id, payload_json) VALUES(?,?)", (a["agent_id"], body))
    else:
        conn.execute("UPDATE agents SET payload_json=? WHERE id=?", (body, a["agent_id"]))


def _active(conn):
    return [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM agents WHERE json_extract(payload_json,'$.state')='active'")]


def _check_persona(p):
    for k in ("name", "personality", "backstory"):
        if not isinstance(p.get(k), str) or not p[k].strip():
            raise BadPersona("persona.%s missing" % k)
    if _PERF.search(" ".join(p[k] for k in ("personality", "backstory"))):
        raise BadPersona("persona carries a performance claim; only the stored career record may")


def _record(conn, lim, now, *, kind, by, reason, extra, epoch_kind="discovery", epoch_extra=None):
    """Append the org change, close the open epoch, open the new one. Caller holds the write transaction. -> epoch_id.
    A material change inside an EVALUATED epoch BREAKS it: the epoch is marked broken, a BROKEN row is appended to the trial registry (alpha stays spent; rows are only ever appended) and the next epoch is a
    discovery epoch until the crew freezes a new nominee (prereg: a changed rule is a new epoch)."""
    nxt = (conn.execute("SELECT COALESCE(MAX(seq), 0) FROM org_changes").fetchone()[0]) + 1
    epoch_id = "e%d" % nxt
    S.append(conn, "org_changes", {"kind": kind, "by": by, "reason": reason, "at": now, "epoch_id": epoch_id, **extra})
    for (eid, pj) in conn.execute("SELECT id, payload_json FROM epochs WHERE json_extract(payload_json,'$.closed_at') IS NULL").fetchall():
        e = json.loads(pj)
        e["closed_at"] = now
        if e.get("kind") == "evaluated" and epoch_kind != "evaluated":
            e["status"] = "broken"
            S.append(conn, "trial_registry", {"k": e["k"], "hypothesis_id": e["hypothesis_id"], "alpha_k": e["alpha_k"], "status": "BROKEN", "nominee_config_hash": e["nominee_config_hash"],
                                              "epoch_id": e["epoch_id"], "broken_by_org_change_seq": nxt, "reason": reason})
        conn.execute("UPDATE epochs SET payload_json=? WHERE id=?", (S.canonical_json(e).decode("utf-8"), eid))
    body = {"epoch_id": epoch_id, "opened_at": now, "reason": reason, "org_change_seq": nxt, "closed_at": None, "kind": epoch_kind, **(epoch_extra or {})}
    conn.execute("INSERT INTO epochs(id, payload_json) VALUES(?,?)", (epoch_id, S.canonical_json(body).decode("utf-8")))
    return epoch_id


EVALUATED_EPOCHS_ENABLED = False
# Gamma, Delta and Zeta must ACK the derived-alpha and resolved-nominee slice before this gate may flip.
# Until then NO argument set opens an evaluated epoch (regression sweeps them). Registry statuses written here are RUNNING and BROKEN only: terminal statuses (FAILED, COMPLETED, NOMINATED, REPLICATION, with the look_id) are
# APPENDED by Task 15, never updates of a row, and nobody invents a status string.


def _open_evaluated(conn, lim, *, clock, hypothesis_id, by, reason, nominee, nominee_refs, config_root,
                    k=None, alpha_k=None, nominee_config_hash=None, launcher_policy=None, probe_write=None):
    """Internal verified freeze; the public gate stays disabled until three source ACKs."""
    from . import nominee as N, launcher_policy as LP
    from .readiness import verdict as V
    _now(clock)
    try:
        frozen = S.canonical_json({'nominee':nominee, 'refs':nominee_refs})
        if len(frozen) > 65536:
            raise OrgRefused('nominee_input_size')
        copied = json.loads(frozen)
        nominee, nominee_refs = copied['nominee'], copied['refs']
    except (TypeError, ValueError, RecursionError):
        raise OrgRefused('nominee_input_invalid') from None
    if conn.in_transaction:
        raise OrgRefused('evaluated_freeze_requires_owned_transaction')
    try:
        authority = LP.prepare(root=config_root, refs=nominee_refs, by=by, clock=clock,
                               launcher_policy=launcher_policy, probe_write=probe_write)
    except LP.Invalid as exc:
        raise OrgRefused(str(exc)) from None
    try:
        with S.write_transaction(conn):
            deadline = time.monotonic() + .5
            now = _now(clock)
            authority.recheck(now)
            authority.ensure_unused(conn)
            count, largest = conn.execute('SELECT count(*),max(length(payload_json)) FROM trial_registry').fetchone()
            if count > 4096 or (largest or 0) > 65536 or not S.verify_chain(conn, 'trial_registry')[0]:
                raise OrgRefused('trial_registry_corrupt_or_unbounded')
            spent = [json.loads(row[0]).get('k') for row in conn.execute('SELECT payload_json FROM trial_registry')]
            if any(type(value) is not int or not 1 <= value <= V.K_MAX for value in spent):
                raise OrgRefused('trial_registry_corrupt')
            derived_k = 1 + max(spent, default=0)
            if derived_k > V.K_MAX:
                raise OrgRefused('family_exhausted')
            derived_alpha = V.alpha_k(derived_k)
            if k is not None and (type(k) is not int or k != derived_k):
                raise OrgRefused('k_mismatch')
            if alpha_k is not None and (type(alpha_k) not in (int,float) or alpha_k != derived_alpha):
                raise OrgRefused('alpha_mismatch')
            for (pj,) in conn.execute("SELECT payload_json FROM epochs WHERE json_extract(payload_json,'$.closed_at') IS NULL"):
                if json.loads(pj).get("kind") == "evaluated":
                    raise OrgRefused("evaluated_epoch_open")
            try:
                resolved, refs = N.resolve(conn, nominee, nominee_refs, root=config_root, k=derived_k, opened_at=now, deadline=deadline, authority=authority)
            except N.Invalid as exc:
                raise OrgRefused('nominee_unresolved: '+str(exc)) from None
            digest = hashlib.sha256(S.canonical_json(resolved)).hexdigest()
            if nominee_config_hash is not None and nominee_config_hash != digest:
                raise OrgRefused('nominee_hash_mismatch')
            now = _now(clock)
            policy_receipt = authority.consume(conn, now)
            eid = _record(conn, lim, now, kind="nominee_freeze", by=by, reason=reason, extra={"op": "open_evaluated", "hypothesis_id": hypothesis_id, "k": derived_k},
                          epoch_kind="evaluated", epoch_extra={"hypothesis_id": hypothesis_id, "k": derived_k, "alpha_k": derived_alpha,
                              "nominee_config_hash": digest, "nominee": resolved, "nominee_refs": refs, "launcher_policy_receipt_hash":policy_receipt})
            S.append(conn, "trial_registry", {"k": derived_k, "hypothesis_id": hypothesis_id, "alpha_k": derived_alpha, "status": "RUNNING", "nominee_config_hash": digest, "epoch_id": eid})
    except LP.Invalid as exc:
        raise OrgRefused(str(exc)) from None
    finally:
        authority.close()
    return eid


def verify_nominee(conn, *, epoch_id, config_root, clock, launcher_policy=None, probe_write=None):
    """Recheck before every export/look. A mismatch durably spends the trial as BROKEN."""
    from . import nominee as N, launcher_policy as LP
    if conn.in_transaction:
        raise OrgRefused('nominee_verification_requires_owned_transaction')
    now = _now(clock)
    peek = conn.execute('SELECT payload_json FROM epochs WHERE id=?', (epoch_id,)).fetchone()
    peek = json.loads(peek[0]) if peek else {}
    try:
        authority = LP.prepare(root=config_root, refs=peek.get('nominee_refs'), by='crew', clock=clock,
                               launcher_policy=launcher_policy, probe_write=probe_write)
    except LP.Invalid as exc:
        raise OrgRefused(str(exc)) from None
    try:
        broken = False
        with S.write_transaction(conn):
            authority.recheck(_now(clock))
            authority.ensure_unused(conn)
            row = conn.execute('SELECT payload_json FROM epochs WHERE id=?', (epoch_id,)).fetchone()
            e = json.loads(row[0]) if row else {}
            try:
                if e.get('status') == 'broken' or e.get('kind') != 'evaluated':
                    raise N.Invalid('not_an_intact_evaluated_epoch')
                resolved, refs = N.resolve(conn, e['nominee'], e['nominee_refs'], root=config_root,
                                          k=e['k'], opened_at=e['opened_at'], deadline=time.monotonic()+.5, authority=authority)
                if hashlib.sha256(S.canonical_json(resolved)).hexdigest() != e['nominee_config_hash']:
                    raise N.Invalid('stored_nominee_hash_mismatch')
            except (N.Invalid, KeyError):
                if e.get('kind') == 'evaluated' and e.get('status') != 'broken':
                    e.update(status='broken', closed_at=now)
                    conn.execute('UPDATE epochs SET payload_json=? WHERE id=?', (S.canonical_json(e).decode(), epoch_id))
                    S.append(conn, 'trial_registry', {key:e[key] for key in ('k','hypothesis_id','alpha_k','nominee_config_hash','epoch_id')} | {'status':'BROKEN','reason':'nominee_changed'})
                broken = True
            if not broken:
                authority.consume(conn, _now(clock))
    except LP.Invalid as exc:
        raise OrgRefused(str(exc)) from None
    finally:
        authority.close()
    if broken:
        raise OrgRefused('nominee_changed')
    return e['nominee_config_hash']


def open_evaluated_epoch(conn, lim, *, clock, hypothesis_id, by, reason, launcher_check, host_guard_is_pilot,
                         k=None, alpha_k=None, nominee_config_hash=None, nominee=None, nominee_refs=None, config_root=None,
                         launcher_policy=None, probe_write=None):
    """Crew-only, fail-closed. Every gate must pass, in this order; nothing is written on a refusal: (1) `by` is a known actor and is 'crew'; (2) `launcher_check(by)` (the host's trusted-launcher identity check; a caller-set
    'crew' string is only a label) is True; (3) the limits say evaluated_epoch_allowed is EXACTLY True (missing or any other value refuses); (4) `host_guard_is_pilot` is EXACTLY False (the full, non-pilot host guard is active);
    (5) EVALUATED_EPOCHS_ENABLED (False until k/alpha_k/nominee-hash derivation exists).
    T15 must pass `launcher_check` and `host_guard_is_pilot` from the PINNED launcher config / host guard it reads itself, never from a value the Office process or a model supplies."""
    _actor(by)
    if by != "crew":
        raise OrgRefused("crew_only: the Office cannot open or freeze an evaluated epoch")
    if not launcher_check(by):
        raise OrgRefused("untrusted_launcher: 'crew' is not backed by the host's trusted launcher")
    allowed = lim.get("evaluated_epoch_allowed")
    if allowed is False:
        raise OrgRefused("pilot_discovery_only: this manifest allows no evaluated epoch (nothing counts as evidence)")
    if allowed is not True:
        raise OrgRefused("evaluated_epoch_not_allowed: the limits do not explicitly allow an evaluated epoch")
    if host_guard_is_pilot is not False:
        raise OrgRefused("pilot_discovery_only: the active host guard is not the full non-pilot manifest")
    if not EVALUATED_EPOCHS_ENABLED:
        raise OrgRefused("evaluated_epochs_disabled: derived alpha and resolved nominee require Gamma/Delta/Zeta ACKs")
    return _open_evaluated(conn, lim, clock=clock, hypothesis_id=hypothesis_id, k=k, alpha_k=alpha_k, nominee_config_hash=nominee_config_hash,
                           by=by, reason=reason, nominee=nominee, nominee_refs=nominee_refs, config_root=config_root,
                           launcher_policy=launcher_policy, probe_write=probe_write)


def hire(conn, lim, *, clock, role, pod, provider, model, family, idempotency_key, persona_fn, policy_hash, prompt_hash, lane=None, lens=None, by="head", reason="hire"):
    _actor(by)
    aid = "ag_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]
    if _agent(conn, aid) is not None:
        return aid
    _now(clock)                                                      # an unknown clock refuses BEFORE a model call is spent
    if len(_active(conn)) + 1 > lim["org_size_ceiling"]:
        raise OrgRefused("org_size_over_ceiling")                    # refused BEFORE the persona model call is spent; re-checked under the lock below
    persona = persona_fn(role, pod)                                  # model call: outside the write lock
    _check_persona(persona)
    with S.write_transaction(conn):
        now = _now(clock)                                            # knowledge/commit time is sampled AFTER the callback returned, under the write lock: times follow commit order
        if _agent(conn, aid) is not None:
            return aid
        if len(_active(conn)) + 1 > lim["org_size_ceiling"]:
            raise OrgRefused("org_size_over_ceiling")
        epoch_id = _record(conn, lim, now, kind="org", by=by, reason=reason, extra={"op": "hire", "agent_id": aid, "role": role, "pod": pod})
        _put_agent(conn, {"agent_id": aid, "role": role, "pod": pod, "provider": provider, "family": family, "model": model, "state": "active", "generation": 1,
                          "policy_hash": policy_hash, "prompt_hash": prompt_hash, "persona_hash": hashlib.sha256(S.canonical_json(persona)).hexdigest(), "persona": persona,
                          "lane": lane, "lens": lens, "hired_epoch": epoch_id, "archived_epoch": None, "created_at": now, "idempotency_key": idempotency_key}, new=True)
    return aid


def fire(conn, lim, *, clock, agent_id, reason, by):
    _actor(by)
    _now(clock)
    with S.write_transaction(conn):
        now = _now(clock)
        a = _agent(conn, agent_id)
        if a is None or a["state"] != "active":
            raise OrgRefused("not_active")
        rest = [x for x in _active(conn) if x["agent_id"] != agent_id]
        if a["role"] in lim["fixed_seats"] and not any(x["role"] == a["role"] for x in rest):
            raise OrgRefused("missing_fixed_seat:%s" % a["role"])         # restaff first: the seat is never left empty
        epoch_id = _record(conn, lim, now, kind="org", by=by, reason=reason, extra={"op": "fire", "agent_id": agent_id, "role": a["role"]})
        a.update(state="archived", generation=a["generation"] + 1, archived_epoch=epoch_id)
        _put_agent(conn, a)
    return epoch_id


_UPDATABLE = {"prompt_hash": "prompt", "persona": "persona", "model": "model"}


def update_agent(conn, lim, *, clock, agent_id, changes, by, reason):
    _actor(by)
    _now(clock)
    bad = set(changes) - set(_UPDATABLE)
    if bad:
        raise OrgRefused("not_updatable:%s" % ",".join(sorted(bad)))
    with S.write_transaction(conn):
        now = _now(clock)
        a = _agent(conn, agent_id)
        if a is None or a["state"] != "active":
            raise OrgRefused("not_active")
        kinds = sorted({_UPDATABLE[k] for k in changes})
        if "persona" in changes:
            _check_persona(changes["persona"])
            a["persona"] = changes["persona"]
            a["persona_hash"] = hashlib.sha256(S.canonical_json(changes["persona"])).hexdigest()
        for k in ("prompt_hash", "model"):
            if k in changes:
                a[k] = changes[k]
        a["generation"] += 1
        epoch_id = _record(conn, lim, now, kind=kinds[0], by=by, reason=reason, extra={"op": "update", "agent_id": agent_id, "fields": sorted(changes), "generation": a["generation"]})
        _put_agent(conn, a)
    return epoch_id


def change_org(conn, lim, *, clock, change, by, reason):
    _actor(by)
    _now(clock)
    opens = O.opens_epoch(lim, change)                                # UnknownChange for an unclassified kind
    if by == "head" and not O.head_may_apply(lim, change):
        raise OrgRefused("head_forbidden:%s" % change["kind"])
    with S.write_transaction(conn):
        now = _now(clock)
        if opens:
            return _record(conn, lim, now, kind=change["kind"], by=by, reason=reason, extra={"op": "change", "detail": change.get("detail")})
        S.append(conn, "org_changes", {"kind": change["kind"], "by": by, "reason": reason, "at": now, "epoch_id": None, "op": "change", "detail": change.get("detail")})
        return None


def accept_answer(conn, agent_id, generation):
    a = _agent(conn, agent_id)
    return a is not None and a["state"] == "active" and a["generation"] == generation


def seed_pilot_org(conn, lim, *, clock, manifest, by, launcher_check):
    """One classified crew-only bootstrap. Ordinary hires keep their epoch behavior."""
    _actor(by)
    if by!='crew' or not callable(launcher_check) or launcher_check(by) is not True:
        raise OrgRefused('pilot_seed_untrusted_launcher')
    if (lim.get('pilot') is not True or lim.get('evaluated_epoch_allowed') is not False
            or lim.get('org_size_ceiling')!=4 or not O.opens_epoch(lim,{'kind':'pilot_bootstrap'})
            or O.head_may_apply(lim,{'kind':'pilot_bootstrap'})):
        raise OrgRefused('pilot_bootstrap_not_classified_crew_material')
    raw=S.canonical_json(manifest)
    if len(raw)>65536:raise OrgRefused('pilot_seed_manifest_size')
    manifest=json.loads(raw)
    digest=hashlib.sha256(raw).hexdigest()
    epoch_id=manifest.get('epoch_id')
    seats=manifest.get('seats')
    if (epoch_id!='office-pilot-discovery-1' or manifest.get('kind')!='discovery'
            or manifest.get('pilot') is not True or manifest.get('evaluation_allowed') is not False
            or manifest.get('book_id')!='pilot-floor' or not isinstance(seats,list) or len(seats)!=4
            or {p.get('slot') for p in seats}!={'head','scout','judge_claude','judge_gpt'}):
        raise OrgRefused('pilot_seed_manifest_invalid')
    if conn.in_transaction:raise OrgRefused('pilot_seed_requires_owned_transaction')
    ids={p['slot']:'ag_'+hashlib.sha256(('office-pilot:'+manifest['files']['pilot_org.json']+':'+p['slot']).encode()).hexdigest()[:16] for p in seats}
    def agent_for(p, opened_at):
        key='office-pilot:'+manifest['files']['pilot_org.json']+':'+p['slot']
        agent={k:p[k] for k in ('role','pod','family','provider','model','prompt_hash','policy_hash','lane','lens','persona')}
        agent.update(agent_id=ids[p['slot']],state='active',generation=1,hired_epoch=epoch_id,archived_epoch=None,
            created_at=opened_at,idempotency_key=key,seed_manifest_sha256=digest,
            persona_hash=hashlib.sha256(S.canonical_json(p['persona'])).hexdigest())
        return agent
    def epoch_for(opened_at):
        return dict(epoch_id=epoch_id,opened_at=opened_at,closed_at=None,kind='discovery',pilot=True,evaluation_allowed=False,
                    book_id='pilot-floor',org_change_seq=1,reason='Pinned pilot bootstrap',seed_manifest_sha256=digest)
    with S.write_transaction(conn):
        now=_now(clock)
        if conn.execute('SELECT purpose FROM _store_info WHERE id=1').fetchone()[0]!='office':
            raise OrgRefused('pilot_seed_requires_office_store')
        if conn.execute('SELECT 1 FROM trial_registry LIMIT 1').fetchone():
            raise OrgRefused('pilot_seed_foreign_trial_registry')
        epochs=conn.execute('SELECT id,payload_json FROM epochs LIMIT 2').fetchall()
        changes=conn.execute('SELECT payload_json FROM org_changes ORDER BY seq LIMIT 2').fetchall()
        agents=conn.execute('SELECT id,payload_json FROM agents LIMIT 5').fetchall()
        if epochs or changes or agents:
            prior_change=json.loads(changes[0][0]) if len(changes)==1 else {}
            opened_at=prior_change.get('at')
            if (len(epochs)==1 and epochs[0][0]==epoch_id and len(changes)==1
                    and json.loads(epochs[0][1]).get('seed_manifest_sha256')==digest
                    and json.loads(changes[0][0]).get('seed_manifest_sha256')==digest
                    and len(agents)==4 and {p[0] for p in agents}==set(ids.values())
                    and S.verify_chain(conn,'org_changes')[0]
                    and json.loads(changes[0][0]).get('seed_manifest')==manifest
                    and S.canonical_json(json.loads(epochs[0][1]))==S.canonical_json(epoch_for(opened_at))
                    and all(S.canonical_json(json.loads(row[1]))==S.canonical_json(agent_for(p,opened_at))
                            for p in seats for row in agents if row[0]==ids[p['slot']])):
                return dict(epoch_id=epoch_id,book_id='pilot-floor',agent_ids=ids,seed_manifest_sha256=digest)
            raise OrgRefused('pilot_seed_existing_foreign_or_mismatched_state')
        for table in S.APPEND_ONLY_TABLES-S.PRIVATE_TABLES | S.MUTABLE_TABLES:
            if conn.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone():
                raise OrgRefused('pilot_seed_existing_foreign_state')
        for p in seats:_check_persona(p['persona'])
        change=dict(kind='pilot_bootstrap',op='pilot_bootstrap',by=by,reason='Pinned pilot bootstrap',at=now,
                    epoch_id=epoch_id,seed_manifest_sha256=digest,seed_manifest=manifest)
        S.append(conn,'org_changes',change)
        epoch=epoch_for(now)
        conn.execute('INSERT INTO epochs VALUES(?,?)',(epoch_id,S.canonical_json(epoch).decode()))
        for p in seats:
            _put_agent(conn,agent_for(p,now),new=True)
    return dict(epoch_id=epoch_id,book_id='pilot-floor',agent_ids=ids,seed_manifest_sha256=digest)
