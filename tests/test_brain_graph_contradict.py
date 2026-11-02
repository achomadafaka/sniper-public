"""The contradiction candidate stage. The agents fixtures are VERBATIM memory text
(feedback_prefer_tailored_agents.md vs feedback-no-agents-no-worker-fleets.md): a synthetic fixture
("general-purpose subagent") passed while the real text carried no key at all (review finding 20)."""
from brain_graph_fake_store import FakeStore
from bot.brain_graph.contradict import action_keys, candidates, keys, polarity

EV = [{"type": "doc", "ref": "a", "check": "unchecked"}]
OLD_AGENTS = ("For lookups: use the dedicated tool (Read for known paths, Grep via Bash for exact symbols). "
              "Reserve general-purpose Agent for open-ended cross-codebase questions.")
OLD_DISPATCH = ('Multi-agent parallel dispatch is the right shape for "explore X" / "what is the system missing" '
                "/ cross-cutting reviews. Always send parallel calls in a single message.")
NO_AGENTS = ("Do not use the Agent tool or spawn parallel subagents in this project. Run pytest with `-n 6` "
             "at most (never bare `-n auto`) and prefer targeted `-k` selections; kill worker remnants after "
             'a timed-out run (`pkill -f "exec(eval"`). One foreground task at a time.')


def _setup():
    s = FakeStore()
    sec = s.insert("brain_sections", {"slug": "crew", "title": "Crew"})
    return s, sec


def _e(s, sec, name="git", type="tool"):
    return s.insert("brain_entities", {"type": type, "name": name, "section_id": sec["id"]})


def _f(s, e, statement, kind="rule"):
    return s.insert("brain_facts", {"kind": kind, "statement": statement, "entity_ids": [e["id"]],
                                    "evidence": EV, "author": "beta"})


def test_keys():
    k = keys("Never `git stash` in the shared checkout; see MEMECOIN_REAL_CHASE_MAX_PCT and `.env`.")
    assert {"git stash", "MEMECOIN_REAL_CHASE_MAX_PCT"} <= k and ".env" not in k
    assert "agents" not in keys("The agents on the crew merge through Alpha.")        # crew sense: not a key


def test_the_verbatim_agents_texts_share_a_key():
    assert action_keys(OLD_AGENTS) == action_keys(OLD_DISPATCH) == {"agent tool"}
    assert "agent tool" in action_keys(NO_AGENTS)


def test_polarity():
    assert polarity("Never run `pkill -f`.") == -1
    assert polarity(NO_AGENTS) == -1
    assert polarity(OLD_AGENTS) == 1
    assert polarity("Run `git stash` before switching.") == 1


def test_the_real_agents_reversal_is_a_candidate():
    s, sec = _setup()
    e = _e(s, sec, "agent tool")
    old = _f(s, e, OLD_AGENTS)
    old2 = _f(s, e, OLD_DISPATCH)
    _f(s, e, "Commit with explicit pathspecs.")
    got = {c["id"] for c in candidates(s, NO_AGENTS, "rule", [e["id"]])}
    assert got == {old["id"], old2["id"]}


def test_reversal_across_kinds_in_the_normative_group():
    s, sec = _setup()
    e = _e(s, sec)
    old = _f(s, e, "Use `git stash` to park the fix.", kind="rule")
    trap = _f(s, e, "`git stash` lost 11 files on 04-25.", kind="trap")
    _f(s, e, "Commit with explicit pathspecs.", kind="rule")
    got = {c["id"] for c in candidates(s, "Never `git stash` in the shared checkout.", "settled", [e["id"]])}
    assert got == {old["id"], trap["id"]}


def test_descriptive_facts_are_not_normative_candidates():
    s, sec = _setup()
    e = _e(s, sec)
    _f(s, e, "`git stash` was used 3 times in August.", kind="measurement")
    assert candidates(s, "Never `git stash`.", "rule", [e["id"]]) == []


def test_the_oldest_fact_is_still_a_candidate_behind_many_newer_ones():
    s, sec = _setup()
    e = _e(s, sec)
    oldest = _f(s, e, "Use `git stash` to park the fix.")
    for i in range(300):
        _f(s, e, f"Unrelated rule number {i} about pathspec commits.")
    assert [c["id"] for c in candidates(s, "Never `git stash` here.", "rule", [e["id"]])] == [oldest["id"]]


def test_no_key_falls_back_to_word_overlap():
    s, sec = _setup()
    e = _e(s, sec, "runner", "process")
    near = _f(s, e, "The runner restarts after a deploy and reloads the environment file.")
    _f(s, e, "Totally different words about quotes and routes.")
    got = candidates(s, "The runner reloads the environment file after every restart.", "rule", [e["id"]])
    assert [c["id"] for c in got] == [near["id"]]
