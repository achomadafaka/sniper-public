"""Task 10 persistence (Lambda): hire / fire / update / change_org / epochs on the real Office store. Persona generation is an injected callable (no model call here)."""
import hashlib
import json
from pathlib import Path

import pytest

from sniper.office import org as O
from sniper.office import roster as R
from sniper.office import store as S

C = Path(__file__).resolve().parents[2] / "config" / "office"


def sha(n):
    return hashlib.sha256((C / n).read_bytes()).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    from sniper.office import launcher_policy
    monkeypatch.setattr(launcher_policy, 'readonly_mount', lambda fd:True)  # Synthetic protocol fixture, not mount evidence.
    conn = S.open_store(tmp_path / "office.db")
    lim = O.load_limits(C / "limits.json", sha("limits.json"))
    t = [1000.0]
    clock = lambda: t[0]                                     # noqa: E731
    yield conn, lim, clock, t
    conn.close()


def persona(role, pod):
    return {"name": "Mara Voss", "personality": "dry, exacting, hates round numbers", "backstory": "ten years on a prop desk reading flow"}


def hire(env, key="k1", role="Scout", pod="pod1", model="claude-sonnet", provider="claude_sdk", family="claude", fn=persona, **kw):
    conn, lim, clock, _ = env
    return R.hire(conn, lim, clock=clock, role=role, pod=pod, provider=provider, model=model, family=family, idempotency_key=key, persona_fn=fn, policy_hash="p" * 64, prompt_hash="q" * 64, **kw)


def agent(conn, aid):
    return json.loads(conn.execute("SELECT payload_json FROM agents WHERE id=?", (aid,)).fetchone()[0])


def test_hire_writes_the_agent_an_org_change_and_opens_an_epoch_in_one_step(env):
    conn = env[0]
    aid = hire(env)
    a = agent(conn, aid)
    assert a["state"] == "active" and a["generation"] == 1 and a["role"] == "Scout" and a["family"] == "claude" and a["persona"]["name"] == "Mara Voss"
    n = conn.execute("SELECT COUNT(*) FROM org_changes").fetchone()[0]
    ep = json.loads(conn.execute("SELECT payload_json FROM epochs").fetchone()[0])
    assert n == 1 and ep["org_change_seq"] == 1 and a["hired_epoch"] == ep["epoch_id"] and S.verify_chain(conn, "org_changes") == (True, None)


def test_hire_is_idempotent_after_a_crash_and_does_not_regenerate_the_persona(env):
    conn = env[0]
    calls = []
    first = hire(env, fn=lambda r, p: calls.append(1) or persona(r, p))
    second = hire(env, fn=lambda r, p: calls.append(1) or persona(r, p))
    assert first == second and len(calls) == 1
    assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1 and conn.execute("SELECT COUNT(*) FROM org_changes").fetchone()[0] == 1


def test_a_persona_with_performance_claims_is_refused_and_nothing_is_written(env):
    conn = env[0]
    bad = lambda r, p: {"name": "X", "personality": "p", "backstory": "closed 400% in 3 months, 80% win rate"}   # noqa: E731
    with pytest.raises(R.BadPersona):
        hire(env, fn=bad)
    assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0 and conn.execute("SELECT COUNT(*) FROM org_changes").fetchone()[0] == 0


def test_hire_over_the_org_ceiling_is_refused(env):
    conn, lim, clock, _ = env
    lim = dict(lim, org_size_ceiling=2)
    hire((conn, lim, clock, None), key="a")
    hire((conn, lim, clock, None), key="b")
    with pytest.raises(R.OrgRefused, match="org_size_over_ceiling"):
        hire((conn, lim, clock, None), key="c")


def test_the_last_risk_or_postmortem_cannot_be_fired_but_can_be_restaffed_first(env):
    conn, lim, clock, _ = env
    r1 = hire(env, key="r1", role="Risk", pod=None)
    with pytest.raises(R.OrgRefused, match="missing_fixed_seat:Risk"):
        R.fire(conn, lim, clock=clock, agent_id=r1, reason="underperforming", by="head")
    hire(env, key="r2", role="Risk", pod=None)
    R.fire(conn, lim, clock=clock, agent_id=r1, reason="restaffed", by="head")
    a = agent(conn, r1)
    assert a["state"] == "archived" and a["generation"] == 2 and a["archived_epoch"]


def test_a_late_answer_from_a_fired_or_replaced_generation_is_rejected(env):
    conn, lim, clock, _ = env
    aid = hire(env)
    assert R.accept_answer(conn, aid, 1) is True
    R.update_agent(conn, lim, clock=clock, agent_id=aid, changes={"prompt_hash": "n" * 64}, by="head", reason="rewrite prompt")
    assert agent(conn, aid)["generation"] == 2 and R.accept_answer(conn, aid, 1) is False and R.accept_answer(conn, aid, 2) is True
    hire(env, key="x", role="Risk", pod=None)
    hire(env, key="y", role="Risk", pod=None)
    R.fire(conn, lim, clock=clock, agent_id=aid, reason="x", by="head")
    assert R.accept_answer(conn, aid, 2) is False                           # archived: no answer is accepted


def test_the_head_cannot_change_guards_or_tools_and_nothing_is_written(env):
    conn, lim, clock, _ = env
    before = conn.execute("SELECT COUNT(*) FROM org_changes").fetchone()[0]
    for kind in ("guards", "tools", "scoring", "live_authority"):
        with pytest.raises(R.OrgRefused, match="head_forbidden"):
            R.change_org(conn, lim, clock=clock, change={"kind": kind, "detail": "x"}, by="head", reason="r")
    assert conn.execute("SELECT COUNT(*) FROM org_changes").fetchone()[0] == before
    R.change_org(conn, lim, clock=clock, change={"kind": "scoring", "detail": "crew edit"}, by="crew", reason="crew-only change")   # the crew can; it opens an epoch too


def test_every_material_change_closes_the_previous_epoch_and_links_to_its_org_change(env):
    conn, lim, clock, t = env
    hire(env, key="a")
    t[0] += 10
    R.change_org(conn, lim, clock=clock, change={"kind": "persona", "detail": "d"}, by="head", reason="r")
    eps = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM epochs ORDER BY id")]
    assert len(eps) == 2 and sum(1 for e in eps if e["closed_at"] is None) == 1 and eps[0]["closed_at"] == 1010.0
    assert [e["org_change_seq"] for e in eps] == [1, 2]


def test_an_unknown_clock_or_change_kind_refuses_and_writes_nothing(env):
    conn, lim, _, _ = env
    with pytest.raises(R.OrgRefused, match="clock"):
        R.change_org(conn, lim, clock=lambda: None, change={"kind": "persona"}, by="head", reason="r")
    with pytest.raises(O.UnknownChange):
        R.change_org(conn, lim, clock=lambda: 5.0, change={"kind": "mystery"}, by="head", reason="r")
    assert conn.execute("SELECT COUNT(*) FROM org_changes").fetchone()[0] == 0


def registry(conn):
    return [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM trial_registry ORDER BY seq")]


def epochs(conn):
    return [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM epochs ORDER BY CAST(substr(id,2) AS INTEGER)")]


def open_eval(env, k=1):
    conn, lim, clock, _ = env
    from .epoch_fixture import open_epoch
    root = Path(conn.execute('PRAGMA database_list').fetchone()[2]).parent/'nominee'
    return open_epoch(conn, lim, clock, root, k=k)


def test_the_internal_open_registers_the_trial_running(env):
    conn, lim, clock, _ = env
    eid = open_eval(env)
    assert [e["kind"] for e in epochs(conn)] == ["evaluated"] and epochs(conn)[0]["epoch_id"] == eid
    assert registry(conn) == [{"k": 1, "hypothesis_id": "H1", "alpha_k": 0.025, "status": "RUNNING", "nominee_config_hash": epochs(conn)[0]["nominee_config_hash"], "epoch_id": eid}]


FULL = {"evaluated_epoch_allowed": True}


def public_open(conn, lim, clock, **over):
    kw = dict(clock=clock, hypothesis_id="H1", k=1, alpha_k=0.01, nominee_config_hash="n" * 64, by="crew", reason="x", launcher_check=lambda who: who == "crew", host_guard_is_pilot=False)
    kw.update(over)
    return R.open_evaluated_epoch(conn, lim, **kw)


def untouched(conn):
    return all(conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] == 0 for t in ("trial_registry", "epochs", "org_changes"))


def test_the_public_open_refuses_unconditionally_until_k_alpha_and_the_nominee_hash_are_derived(env):
    conn, lim, clock, _ = env
    full = dict(lim, **FULL)
    with pytest.raises(R.OrgRefused, match="evaluated_epochs_disabled"):
        public_open(conn, full, clock)                                      # every other gate passes: still refused (Alpha ruling; Gamma P1-a/b are the follow-up slice)
    assert untouched(conn) and R.EVALUATED_EPOCHS_ENABLED is False


def test_missing_limits_key_and_pilot_manifest_and_a_pilot_guard_all_refuse_fail_closed(env):
    conn, lim, clock, _ = env
    assert "evaluated_epoch_allowed" not in lim
    with pytest.raises(R.OrgRefused, match="evaluated_epoch_not_allowed"):
        public_open(conn, lim, clock)                                       # the key is MISSING: refuse (was fail-open)
    with pytest.raises(R.OrgRefused, match="pilot_discovery_only"):
        public_open(conn, dict(lim, evaluated_epoch_allowed=False), clock)
    for bad in (True, None, "no"):
        with pytest.raises(R.OrgRefused, match="pilot_discovery_only"):
            public_open(conn, dict(lim, **FULL), clock, host_guard_is_pilot=bad)         # only an explicit False (the full, non-pilot guard) passes this gate
    for allowed in ("true", 1, None):
        with pytest.raises(R.OrgRefused, match="evaluated_epoch_not_allowed"):
            public_open(conn, dict(lim, evaluated_epoch_allowed=allowed), clock)
    assert untouched(conn)


def test_a_caller_set_crew_string_does_not_pass_the_trusted_launcher_check_and_the_head_cannot_call_it(env):
    conn, lim, clock, _ = env
    full = dict(lim, **FULL)
    with pytest.raises(R.OrgRefused, match="untrusted_launcher"):
        public_open(conn, full, clock, launcher_check=lambda who: False)    # by == 'crew' is only a label; the host-side check says no
    with pytest.raises(R.OrgRefused, match="crew_only"):
        public_open(conn, full, clock, by="head")
    with pytest.raises(R.OrgRefused, match="unknown_actor"):
        public_open(conn, full, clock, by="alice")
    assert untouched(conn)


def test_a_material_change_inside_an_evaluated_epoch_breaks_it_alpha_stays_spent_and_the_next_epoch_is_discovery(env):
    conn, lim, clock, t = env
    first = open_eval(env)
    t[0] += 5
    R.change_org(conn, lim, clock=clock, change={"kind": "prompt", "detail": "tweak"}, by="head", reason="head tweak")
    eps = epochs(conn)
    assert eps[0]["status"] == "broken" and eps[0]["closed_at"] == 1005.0 and eps[1]["kind"] == "discovery"
    reg = registry(conn)
    assert [r["status"] for r in reg] == ["RUNNING", "BROKEN"] and reg[1]["k"] == 1 and reg[1]["epoch_id"] == first and reg[1]["broken_by_org_change_seq"] >= 1
    assert S.verify_chain(conn, "trial_registry") == (True, None)                                 # rows only ever appended: the spent alpha cannot be erased


def test_a_restaff_hire_or_fire_inside_an_evaluated_epoch_also_breaks_it_but_an_immaterial_change_does_not(env):
    conn, lim, clock, t = env
    r1 = hire(env, key="r1", role="Risk", pod=None)
    hire(env, key="r2", role="Risk", pod=None)
    open_eval(env)
    R.change_org(conn, lim, clock=clock, change={"kind": "desk_chat_message"}, by="head", reason="chatter")
    assert [r["status"] for r in registry(conn)] == ["RUNNING"] and epochs(conn)[-1]["kind"] == "evaluated"          # immaterial: nothing breaks
    R.fire(conn, lim, clock=clock, agent_id=r1, reason="restaff flag acted on", by="head")
    assert [r["status"] for r in registry(conn)] == ["RUNNING", "BROKEN"]


def test_a_discovery_epoch_change_breaks_nothing(env):
    conn, lim, clock, _ = env
    hire(env, key="a")
    R.change_org(conn, lim, clock=clock, change={"kind": "persona", "detail": "d"}, by="head", reason="r")
    assert registry(conn) == [] and {e["kind"] for e in epochs(conn)} == {"discovery"}


def test_hire_stamps_created_at_and_epoch_AFTER_the_persona_callback_returns(env):
    conn, lim, clock, t = env

    def slow_persona(role, pod):
        t[0] = 1010.0                                                        # the model answered 10 s later
        return persona(role, pod)

    aid = hire(env, fn=slow_persona)
    assert agent(conn, aid)["created_at"] == 1010.0
    assert json.loads(conn.execute("SELECT payload_json FROM epochs").fetchone()[0])["opened_at"] == 1010.0
    assert json.loads(conn.execute("SELECT payload_json FROM org_changes").fetchone()[0])["at"] == 1010.0


def test_an_unknown_clock_is_refused_before_the_persona_model_call_is_spent(env):
    conn, lim, _, _ = env
    calls = []
    with pytest.raises(R.OrgRefused, match="clock"):
        R.hire(conn, lim, clock=lambda: None, role="Scout", pod="pod1", provider="claude_sdk", model="m", family="claude", idempotency_key="k9",
               persona_fn=lambda r, p: calls.append(1) or persona(r, p), policy_hash="p" * 64, prompt_hash="q" * 64)
    assert calls == []


def test_only_head_and_crew_are_actors_anything_else_is_refused_not_implicitly_crew(env):
    conn, lim, clock, _ = env
    aid = hire(env)
    for who in ("alice", "", None, "judge1", "CREW", "Head"):
        with pytest.raises(R.OrgRefused, match="unknown_actor"):
            R.change_org(conn, lim, clock=clock, change={"kind": "live_authority", "detail": "unsafe"}, by=who, reason="model text")
        with pytest.raises(R.OrgRefused, match="unknown_actor"):
            R.update_agent(conn, lim, clock=clock, agent_id=aid, changes={"prompt_hash": "z" * 64}, by=who, reason="r")
        with pytest.raises(R.OrgRefused, match="unknown_actor"):
            R.fire(conn, lim, clock=clock, agent_id=aid, reason="r", by=who)
    with pytest.raises(R.OrgRefused, match="unknown_actor"):
        public_open(conn, dict(lim, **FULL), clock, by="alice")
    assert conn.execute("SELECT COUNT(*) FROM org_changes").fetchone()[0] == 1                 # only the hire


def test_org_change_times_follow_commit_order(env):
    conn, lim, clock, t = env
    hire(env, key="a")
    t[0] = 1005.0
    R.change_org(conn, lim, clock=clock, change={"kind": "persona", "detail": "d"}, by="head", reason="r")
    ats = [json.loads(r[0])["at"] for r in conn.execute("SELECT payload_json FROM org_changes ORDER BY seq")]
    assert ats == sorted(ats)


def test_a_pilot_manifest_refuses_an_evaluated_epoch_discovery_only_nothing_counts_as_evidence(tmp_path):
    conn = S.open_store(tmp_path / "pilot.db")
    lim = O.load_limits(C / "limits_pilot.json", sha("limits_pilot.json"))
    clock = lambda: 1000.0                                                  # noqa: E731
    with pytest.raises(R.OrgRefused, match="pilot_discovery_only"):
        public_open(conn, lim, clock)
    assert conn.execute("SELECT COUNT(*) FROM trial_registry").fetchone()[0] == 0 and conn.execute("SELECT COUNT(*) FROM epochs").fetchone()[0] == 0
    aid = R.hire(conn, lim, clock=clock, role="Judge", pod="pod1", provider="claude_sdk", model="m", family="claude", idempotency_key="j1", persona_fn=persona, policy_hash="p" * 64, prompt_hash="q" * 64)
    ep = json.loads(conn.execute("SELECT payload_json FROM epochs").fetchone()[0])
    assert ep["kind"] == "discovery" and aid                                 # every pilot epoch is a discovery epoch
    conn.close()


def test_the_pilot_org_ceiling_blocks_a_fifth_hire(tmp_path):
    conn = S.open_store(tmp_path / "pilot.db")
    lim = dict(O.load_limits(C / "limits_pilot.json", sha("limits_pilot.json")), org_size_ceiling=4)
    for i in range(4):
        R.hire(conn, lim, clock=lambda: 1000.0 + i, role="Judge", pod="pod1", provider="claude_sdk", model="m", family="claude", idempotency_key="k%d" % i, persona_fn=persona, policy_hash="p" * 64, prompt_hash="q" * 64)
    with pytest.raises(R.OrgRefused, match="org_size_over_ceiling"):
        R.hire(conn, lim, clock=lambda: 2000.0, role="Judge", pod="pod1", provider="claude_sdk", model="m", family="claude", idempotency_key="k5", persona_fn=persona, policy_hash="p" * 64, prompt_hash="q" * 64)
    conn.close()


def test_no_argument_set_can_open_an_evaluated_epoch_while_the_follow_up_slice_is_missing(env):
    import itertools
    conn, lim, clock, _ = env
    n = 0
    for by, allowed, pilot, launcher, k in itertools.product(("crew", "head", "alice"), (True, False, None, "missing"), (False, True, None), (True, False), (1, 2, 11)):
        l2 = {x: v for x, v in lim.items() if x != "evaluated_epoch_allowed"} if allowed == "missing" else dict(lim, evaluated_epoch_allowed=allowed)
        with pytest.raises(R.OrgRefused):
            public_open(conn, l2, clock, by=by, host_guard_is_pilot=pilot, launcher_check=lambda who, ok=launcher: ok, k=k)
        n += 1
    assert n == 216 and untouched(conn)


def test_the_pilot_fifth_agent_is_refused_before_any_model_call_or_write(tmp_path):
    conn = S.open_store(tmp_path / "pilot.db")
    lim = O.load_limits(C / "limits_pilot.json", sha("limits_pilot.json"))
    assert lim["org_size_ceiling"] == 4
    calls = []
    for i in range(4):
        R.hire(conn, lim, clock=lambda: 1000.0 + i, role="Judge", pod="pod1", provider="claude_sdk", model="m", family="claude", idempotency_key="k%d" % i, persona_fn=persona, policy_hash="p" * 64, prompt_hash="q" * 64)
    before = conn.execute("SELECT COUNT(*) FROM org_changes").fetchone()[0]
    with pytest.raises(R.OrgRefused, match="org_size_over_ceiling"):
        R.hire(conn, lim, clock=lambda: 2000.0, role="Judge", pod="pod1", provider="claude_sdk", model="m", family="claude", idempotency_key="k5", persona_fn=lambda r, p: calls.append(1) or persona(r, p), policy_hash="p" * 64, prompt_hash="q" * 64)
    assert calls == [] and conn.execute("SELECT COUNT(*) FROM org_changes").fetchone()[0] == before
    conn.close()


def test_no_non_test_module_under_sniper_reaches_the_internal_evaluated_open():
    import ast
    root = Path(__file__).resolve().parents[2] / "sniper"
    offenders = []
    for py in root.rglob("*.py"):
        if py.name == "roster.py" and py.parent.name == "office":
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == "_open_evaluated") or (isinstance(node, ast.Name) and node.id == "_open_evaluated") \
                    or (isinstance(node, ast.ImportFrom) and any(a.name == "_open_evaluated" for a in node.names)):
                offenders.append("%s:%s" % (py, getattr(node, "lineno", "?")))
    assert offenders == [], offenders                                                                  # only tests may seed an evaluated epoch; production code goes through the gated open_evaluated_epoch
