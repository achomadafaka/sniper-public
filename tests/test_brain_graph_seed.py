"""The seed of sections, role views and actors (plan Task 11) over the FakeStore."""
import sys
from pathlib import Path

from brain_graph_fake_store import FakeStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import brain_seed  # noqa: E402


def test_seed_is_idempotent_and_covers_every_role():
    s = FakeStore()
    first, second = brain_seed.seed(s), brain_seed.seed(s)
    assert first == {"sections_created": len(brain_seed.SECTIONS), "views_created": 8, "actors_created": 9, "views_updated": 0}
    assert second == {"sections_created": 0, "views_created": 0, "actors_created": 0, "views_updated": 0}
    slugs = {x["slug"] for x in s.sections()}
    for role in ("alpha", "beta", "gamma", "lambda", "codex", "delta", "epsilon", "zeta"):
        spec = s.view(f"role/{role}")["query_spec"]
        assert set(spec["sections"]) <= slugs and "brain/doctrine" in spec["sections"] and spec["role_text"]
    assert all(s.find_entity("actor", a) for a in brain_seed.ACTORS)


def test_a_half_applied_seed_completes_on_rerun():
    s = FakeStore()
    s.insert("brain_sections", {"slug": "memecoin", "title": "Memecoin"})     # a run that died early
    got = brain_seed.seed(s)
    assert got["sections_created"] == len(brain_seed.SECTIONS) - 1
    by = {x["slug"]: x for x in s.sections()}
    assert by["memecoin/solana"]["parent_id"] == by["memecoin"]["id"]


def test_the_dry_run_writes_nothing():
    s = FakeStore()
    got = brain_seed.seed(brain_seed.DryRun(s))
    assert got["sections_created"] == len(brain_seed.SECTIONS) and s.sections() == []
    assert s.view("role/alpha") is None and s.find_entity("actor", "operator") is None
