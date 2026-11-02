"""Machine evidence checks. A throwaway git repo makes on-main / local-only / branch-only / tag-only
commits, template knobs and ALTER-added columns deterministic; one test runs on this repo."""
import json
import subprocess
from pathlib import Path

import pytest

from bot.brain_graph.evidence import check, check_all

REPO = Path(__file__).resolve().parents[1]


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def _commit(repo, rel, text, msg):
    p = Path(repo) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _git(repo, "add", rel)
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    on_main = _commit(r, "sniper/settings/knobs.py", "\n".join([
        'MEMECOIN_REAL_TP_PCT = os.getenv("MEMECOIN_REAL_TP_PCT", "25")',
        'url = os.getenv("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_%s" % CHAIN)',
        'g = os.getenv(f"MEMECOIN_X_GRACE_{chain}")',
        'CREATE TABLE IF NOT EXISTS pool_ticks (chain_id TEXT, block INTEGER)',
        'ALTER TABLE pool_ticks ADD COLUMN block_hash TEXT',
    ]), "base")
    _commit(r, "rust/sidecar/src/main.rs", 'let p = std::env::var("SNIPER_TPU_SIDECAR_PORT");', "rust")
    _commit(r, "tests/test_k.py", 'assert "ZZQT_ONLY_NAMED_IN_A_TEST" and "MEMECOIN_REAL_ONLY_IN_TEST"', "test")
    (r / "index").mkdir()
    (r / "index" / "schema_registry.json").write_text(json.dumps(
        {"tables": {"memecoin_positions": {"columns": [{"name": "opened_recorded_at"}]}}}))
    _git(r, "update-ref", "refs/remotes/origin/main", "HEAD")           # what origin has
    local = _commit(r, "docs/new_audit.md", "x", "local, not pushed")
    _git(r, "checkout", "-q", "-b", "side")
    side = _commit(r, "side.txt", "x", "on a side branch only")
    _git(r, "checkout", "-q", "main")
    _git(r, "checkout", "-q", "-b", "tmp")
    tagged = _commit(r, "tagged.txt", "x", "tag only")
    _git(r, "tag", "archive/old", tagged)
    _git(r, "checkout", "-q", "main")
    _git(r, "branch", "-q", "-D", "tmp")
    return {"repo": r, "on_main": on_main, "local": local, "side": side, "tagged": tagged}


def _v(item, r, **kw):
    return check(item, r["repo"], **kw)[0]


def test_sha_states(repo):
    assert _v({"type": "sha", "ref": repo["on_main"][:10]}, repo) == "ok"
    assert _v({"type": "sha", "ref": repo["local"][:10]}, repo) == "pending_push"      # HEAD, not pushed
    assert _v({"type": "sha", "ref": repo["side"][:10]}, repo) == "pending_push"       # a builder's branch
    assert _v({"type": "sha", "ref": repo["tagged"][:10]}, repo) == "unchecked"           # never merged
    assert _v({"type": "sha", "ref": "0123456789abcdef0123"}, repo) == "broken"
    assert _v({"type": "sha", "ref": "0123456789abcdef0123"}, repo, fetched=False) == "unchecked"
    assert _v({"type": "sha", "ref": "not-a-sha"}, repo) == "broken"


def test_paths(repo):
    assert _v({"type": "path", "ref": "sniper/settings/knobs.py"}, repo) == "ok"
    assert _v({"type": "path", "ref": "sniper/settings/knobs.py:3"}, repo) == "ok"          # file:line
    assert _v({"type": "doc", "ref": "docs/new_audit.md"}, repo) == "pending_push"
    assert _v({"type": "path", "ref": "no/such/file.py"}, repo) == "broken"
    assert _v({"type": "path", "ref": "no/such/file.py"}, repo, fetched=False) == "unchecked"
    assert _v({"type": "path", "ref": "data/runner.pid"}, repo) == "unchecked"              # runtime, absent
    (repo["repo"] / "data").mkdir()
    (repo["repo"] / "data" / "runner.pid").write_text("1")
    assert _v({"type": "path", "ref": "data/runner.pid"}, repo) == "ok"


def test_knobs(repo):
    assert _v({"type": "knob", "ref": "MEMECOIN_REAL_TP_PCT"}, repo) == "ok"
    assert _v({"type": "knob", "ref": "MEMECOIN_ONCHAIN_EVM_TX_READ_URL_56"}, repo) == "ok"   # "..._%s" % CHAIN
    assert _v({"type": "knob", "ref": "MEMECOIN_X_GRACE_8453"}, repo) == "ok"                # f"..._{chain}"
    assert _v({"type": "knob", "ref": "SNIPER_TPU_SIDECAR_PORT"}, repo) == "ok"             # Rust reads env
    assert _v({"type": "knob", "ref": "MEMECOIN_REAL_TP_PCTX"}, repo) == "unchecked"         # family known
    assert _v({"type": "knob", "ref": "ZZZQ_TOTALLY_MADE_UP"}, repo) == "broken"
    assert _v({"type": "knob", "ref": "lower_case"}, repo) == "broken"
    assert _v({"type": "knob", "ref": "ZZQT_ONLY_NAMED_IN_A_TEST"}, repo) == "broken"      # tests do not define
    assert _v({"type": "knob", "ref": "MEMECOIN_REAL_ONLY_IN_TEST"}, repo) == "unchecked"  # family is real


def test_columns(repo):
    assert _v({"type": "column", "ref": "memecoin_positions.opened_recorded_at"}, repo) == "ok"
    assert _v({"type": "column", "ref": "memecoin_positions.no_such_col"}, repo) == "broken"
    assert _v({"type": "column", "ref": "pool_ticks.block"}, repo) == "ok"                  # CREATE TABLE
    assert _v({"type": "column", "ref": "pool_ticks.block_hash"}, repo) == "ok"             # ALTER TABLE ADD
    assert _v({"type": "column", "ref": "pool_ticks.not_found"}, repo) == "unchecked"        # never broken
    assert _v({"type": "column", "ref": "brain_facts.status"}, repo) == "unchecked"          # Supabase


def test_check_all_annotates_and_never_raises(repo):
    out = check_all([{"type": "query", "ref": "select 1"}, {"type": "knob", "ref": "MEMECOIN_REAL_TP_PCT"}],
                    repo["repo"])
    assert [o["check"] for o in out] == ["unchecked", "ok"] and all(o["check_reason"] for o in out)
    assert check({"type": "sha", "ref": "abcdef1"}, Path("/nonexistent/repo"))[0] in ("unchecked", "broken")


def test_this_repo_template_knob_is_not_broken():
    # review finding 13: `git grep -w` on the stripped base missed this real knob and refused true facts
    assert check({"type": "knob", "ref": "MEMECOIN_ONCHAIN_EVM_TX_READ_URL_56"}, REPO)[0] == "ok"


def test_nonarchive_tag_alone_remains_broken(repo):
    _git(repo["repo"], "tag", "-d", "archive/old")
    _git(repo["repo"], "tag", "v-not-archive", repo["tagged"])
    assert _v({"type": "sha", "ref": repo["tagged"]}, repo) == "broken"


def test_a_path_only_at_the_cited_sha_is_pending_push_not_broken():
    """Beta 2026-09-29: a file on an unmerged branch was refused although the fact cited the commit."""
    import subprocess
    from bot.brain_graph import evidence
    repo = REPO if 'REPO' in globals() else __import__('pathlib').Path(__file__).resolve().parents[1]
    parent = subprocess.run(["git", "-C", str(repo), "rev-parse", "926d6c3e^"], capture_output=True,
                            text=True).stdout.strip()
    gone = {"type": "path", "ref": "scripts/memecoin_gmgn_shadow_loop.py"}
    alone = evidence.check_all([gone], repo)
    assert alone[0]["check"] == "broken"
    both = evidence.check_all([gone, {"type": "sha", "ref": parent[:10]}], repo)
    assert both[0]["check"] == "pending_push" and "cited commit" in both[0]["check_reason"]
