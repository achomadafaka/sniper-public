"""Wallet graph -> brain projection (Alpha order 2026-09-29; spec
docs/superpowers/specs/2026-09-29-memecoin-wallet-link-graph-design.md, "Brain projection").

Each qualifying edge becomes ONE measurement fact on its two wallet entities (section memecoin),
with the method version and the coverage caveats in the statement. A recompute rewrites only a
changed edge (the old fact is superseded or retracted), an edge that no longer passes is retracted,
and a stale or empty graph run projects nothing, so a J7 outage cannot retract the graph.
"""
import json
import sqlite3
from pathlib import Path

from brain_graph_fake_store import FakeStore

from scripts.research import wallet_link_graph as g

REPO = Path(__file__).resolve().parents[1]
NOW = "2026-09-29T22:30:00+00:00"


def _store():
    s = FakeStore()
    s.insert("brain_sections", {"slug": "memecoin", "title": "memecoin"})
    return s


def _follows(src="L", dst="F", n=5, p=0.0005, flag=None, passed=5):
    return {"network_id": "solana", "edge_type": "follows", "src": src, "dst": dst, "src_user_id": src,
            "dst_user_id": dst, "n_coins": n, "n_consistent": n, "lag_p50_s": 42.0, "lag_p90_s": 300.0,
            "null_p99": 2.0, "null_p": p, "flag": flag, "first_seen": "2026-09-16T01:00:00+00:00",
            "last_seen": "2026-09-29T20:00:00+00:00", "coverage": dict(g.COVERAGE, j7_empty_days=["2026-09-21"]),
            "evidence": {"coins": [{"mint": "m%d" % i, "leader_at": "2026-09-20T00:00:00+00:00", "lag_s": 30.0}
                                   for i in range(n)],
                         "null": {"shuffles": 2000, "seeds_passed": passed,
                                  "per_seed": [{"seed": i, "p99": 2, "exceed": 0, "p": p} for i in range(5)]}}}


def _dumps(src="S", dst="B", hits=6, near=18, p=0.0064, bins=(0, 0, 0, 1, 3, 7, 6, 0, 1, 3)):
    e = _follows(src, dst, hits, p)
    e.update(edge_type="dumps_on", null_p99=None, n_consistent=hits)
    e["evidence"] = {"coins": [], "null": {"test": "binomial", "near": near, "hits": hits, "q": 0.1,
                                          "p": p, "bins_120s": list(bins)}}
    return e


def _run(store, graph, edges, *, events=31000, finished=NOW, names=None, aliases=None,
         window_from="2026-09-15T22:30:00+00:00"):
    g.write_edges(graph, edges, computed_at=finished)
    conn = g.graph_connection(graph)
    with conn:
        conn.execute("INSERT INTO memecoin_wallet_graph_runs (method_version, started_at, finished_at, "
                     "window_from, edges, helius_calls, note) VALUES (?,?,?,?,?,0,?)",
                     (g.METHOD_VERSION, finished, finished, window_from, len(edges),
                      "events=%d" % events))
    conn.close()
    return g.project(store, graph, now_iso=finished, names=names or {"L": "leader", "F": "fol"},
                     aliases=aliases or {}, repo=REPO, secret_values=frozenset())


def _live(store):
    return [f for f in store.rows["brain_facts"] if f["status"] not in ("retracted", "superseded")]


def test_an_edge_becomes_one_measurement_fact_on_both_wallets(tmp_path):
    s, graph = _store(), tmp_path / "graph.db"
    out = _run(s, graph, [_follows()], aliases={"L": ["So1anaLeaderAddr"]})
    assert out["written"] == 1
    (fact,) = _live(s)
    assert fact["kind"] == "measurement" and fact["author"] == "cron"
    st = fact["statement"]
    for part in ("fol follows leader on solana", "5 of 5", "worst-seed p 0.0005", "p <= 0.01 on 5/5 seeds",
                 "J7 roster only",
                 g.METHOD_VERSION, "not proof of coordination", "2026-09-21"):
        assert part in st, part
    names = {e["name"]: e for e in s.rows["brain_entities"]}
    assert set(names) == {"leader", "fol"} and all(e["type"] == "wallet" for e in names.values())
    assert names["leader"]["aliases"] == ["l", "so1analeaderaddr"]         # the DB lowercases aliases
    detail = json.loads(fact["detail"])
    assert detail["edge_key"] == "solana|follows|L|F" and len(fact["detail"]) <= 4096
    assert {e["type"] for e in fact["evidence"]} == {"query", "doc", "path"}


def test_an_unchanged_rerun_writes_nothing(tmp_path):
    s, graph = _store(), tmp_path / "graph.db"
    _run(s, graph, [_follows()])
    out = _run(s, graph, [_follows()], finished="2026-09-29T23:30:00+00:00")
    assert out["written"] == 0 and len(s.rows["brain_facts"]) == 1


def test_a_changed_edge_replaces_its_fact(tmp_path):
    s, graph = _store(), tmp_path / "graph.db"
    _run(s, graph, [_follows(n=5)])
    old = _live(s)[0]
    _run(s, graph, [_follows(n=6)], finished="2026-09-29T23:30:00+00:00")
    (live,) = _live(s)
    assert live["id"] != old["id"] and "6 of 6" in live["statement"]
    assert s.get("brain_facts", old["id"])["status"] == "superseded"       # judged, not just closed


def test_an_old_fact_outside_the_candidates_is_still_closed(tmp_path, monkeypatch):
    """The contradiction pass may not offer the old fact (top 5 by word overlap); the projector then
    retracts it itself, so an edge never has two live facts."""
    from bot.brain_graph import api
    s, graph = _store(), tmp_path / "graph.db"
    _run(s, graph, [_follows(n=5)])
    old = _live(s)[0]
    monkeypatch.setattr(api, "candidates", lambda *a, **k: [])
    _run(s, graph, [_follows(n=6)], finished="2026-09-29T23:30:00+00:00")
    assert len(_live(s)) == 1 and s.get("brain_facts", old["id"])["status"] == "retracted"


def test_an_edge_that_no_longer_passes_is_retracted(tmp_path):
    s, graph = _store(), tmp_path / "graph.db"
    _run(s, graph, [_follows(), _follows("L", "G")], names={"L": "leader", "F": "fol", "G": "gee"})
    out = _run(s, graph, [_follows()], finished="2026-09-29T23:30:00+00:00")
    assert out["retracted"] == 1
    (live,) = _live(s)
    assert "fol follows" in live["statement"]
    gone = [f for f in s.rows["brain_facts"] if f["status"] == "retracted"]
    assert len(gone) == 1 and "no longer passes" in gone[0]["verify_note"]


def test_an_empty_graph_run_projects_nothing(tmp_path):
    """A J7 outage gives 0 events and 0 edges: that must not retract the graph."""
    s, graph = _store(), tmp_path / "graph.db"
    _run(s, graph, [_follows()])
    out = _run(s, graph, [], events=0, finished="2026-09-29T23:30:00+00:00")
    assert out["refused"] and len(_live(s)) == 1


def test_a_stale_graph_run_projects_nothing(tmp_path):
    """The graph cron died: its last run is hours old and its edges must not be read as current."""
    s, graph = _store(), tmp_path / "graph.db"
    _run(s, graph, [_follows()])
    g.write_edges(graph, [], computed_at=NOW)
    out = g.project(s, graph, now_iso="2026-09-30T06:00:00+00:00", names={}, aliases={}, repo=REPO,
                    secret_values=frozenset())
    assert "h ago" in out["refused"] and len(_live(s)) == 1              # last run is 7.5 h old


def test_boundary_and_dumps_caveats_are_in_the_statement(tmp_path):
    s, graph = _store(), tmp_path / "graph.db"
    _run(s, graph, [_follows(flag="boundary", passed=3, p=0.012), _dumps()],
         names={"L": "leader", "F": "fol", "S": "prevaile", "B": "0xsneakky"})
    st = {json.loads(f["detail"])["edge_type"]: f["statement"] for f in _live(s)}
    assert "BOUNDARY" in st["follows"] and "worst-seed p 0.012, p <= 0.01 on 3/5 seeds" in st["follows"]
    for part in ("prevaile sells soon after 0xsneakky buys", "6 of 18", "0-120 s: 7", "120-240 s: 6",
                 "not a 120 s dump"):
        assert part in st["dumps_on"], part


def test_each_wallet_keeps_its_top_ten_edges_per_type():
    edges = [_follows("HUB", "f%02d" % i, n=3 + i, p=0.001) for i in range(12)]
    kept = g.select_edges(edges, top=10)
    assert len(kept) == 10 and {e["dst"] for e in kept} == {"f%02d" % i for i in range(2, 12)}
    inbound = [_follows("l%02d" % i, "HUB", n=3 + i, p=0.001) for i in range(12)]
    assert len(g.select_edges(inbound, top=10)) == 10                   # the cap holds on the dst side


def test_the_projection_map_lives_in_the_graph_db(tmp_path):
    s, graph = _store(), tmp_path / "graph.db"
    _run(s, graph, [_follows()])
    rows = sqlite3.connect(graph).execute("SELECT edge_key, fact_id FROM memecoin_wallet_graph_brain").fetchall()
    assert rows == [("solana|follows|L|F", _live(s)[0]["id"])]


def test_sibling_edges_on_a_shared_wallet_are_judged_not_left_pending(tmp_path):
    """Gamma MED 2026-09-29: sibling statements share a wallet and score Jaccard >= 0.3, so every
    fact was left judge_pending. The projector judges its own candidates: the same edge supersedes,
    any other graph fact is unrelated."""
    s, graph = _store(), tmp_path / "graph.db"
    names = {"L": "leader", "F": "fol", "G": "gee", "H": "aitch"}
    _run(s, graph, [_follows(), _follows("L", "G"), _follows("H", "L")], names=names)
    assert len(_live(s)) == 3 and not any(f.get("judge_pending") for f in _live(s))
    _run(s, graph, [_follows(n=6), _follows("L", "G"), _follows("H", "L")], names=names,
         finished="2026-09-29T23:30:00+00:00")
    assert len(_live(s)) == 3 and not any(f.get("judge_pending") for f in _live(s))


def test_a_human_fact_on_the_same_wallet_is_left_to_the_crew(tmp_path):
    from bot.brain_graph import api
    s, graph = _store(), tmp_path / "graph.db"
    human = api.write_fact(s, {"kind": "measurement", "author": "gamma", "section": "memecoin",
                               "claim_scope": "relationship",
                               "statement": "Checked by hand: " + g.statement_for(_follows(), {"L": "leader", "F": "fol"})[:400],
                               "entities": [{"type": "wallet", "name": "leader"}, {"type": "wallet", "name": "fol"}],
                               "evidence": [{"type": "doc", "ref": g.SPEC_DOC}]}, repo=REPO)["fact"]
    _run(s, graph, [_follows()])
    mine = [f for f in _live(s) if f["author"] == "cron"][0]
    assert mine.get("judge_pending") and s.get("brain_facts", human["id"])["status"] == "unverified"


def test_a_run_shorter_than_the_method_window_projects_nothing(tmp_path):
    """Gamma LOW 1: a manual `run --days 1` has fewer edges; projecting it would retract the rest."""
    s, graph = _store(), tmp_path / "graph.db"
    _run(s, graph, [_follows(), _follows("L", "G")], names={"L": "leader", "F": "fol", "G": "gee"})
    out = _run(s, graph, [_follows()], finished="2026-09-29T23:30:00+00:00",
               window_from="2026-09-28T23:30:00+00:00")
    assert "window" in out["refused"] and len(_live(s)) == 2


def test_a_cap_drop_is_retracted_as_a_cap_drop(tmp_path, monkeypatch):
    """Gamma LOW 2: an edge dropped only by the per-wallet cap still passes; the reason must say so."""
    s, graph = _store(), tmp_path / "graph.db"
    names = {"L": "leader", "F": "fol", "G": "gee"}
    _run(s, graph, [_follows(), _follows("L", "G", p=0.001)], names=names)
    monkeypatch.setattr(g, "TOP_PER_TYPE", 1)
    out = _run(s, graph, [_follows(), _follows("L", "G", p=0.001)], names=names,
               finished="2026-09-29T23:30:00+00:00")
    assert out["retracted"] == 1
    (gone,) = [f for f in s.rows["brain_facts"] if f["status"] == "retracted"]
    assert "top-1 cap" in gone["verify_note"] and "no longer passes" not in gone["verify_note"]


def test_graph_facts_outside_the_map_are_still_judged_unrelated(tmp_path):
    """The map can be lost (a recreated graph DB): graph facts are known by their author_session."""
    s = _store()
    names = {"L": "leader", "F": "fol", "G": "gee"}
    _run(s, tmp_path / "old.db", [_follows()], names=names)
    _run(s, tmp_path / "new.db", [_follows("L", "G")], names=names)
    assert len(_live(s)) == 2 and not any(f.get("judge_pending") for f in _live(s))


def test_slot_edge_statements_name_their_own_sample(tmp_path):
    s, graph = _store(), tmp_path / "graph.db"
    e = _follows("A", "So1Companion", n=4, p=0.0007)
    e.update(edge_type="slot_follower", null_p99=None, lag_p50_s=None, lag_p90_s=None,
             coverage=dict(g.COVERAGE, sample="armed Solana wallets' own buys; companions are any wallet",
                           slot_reads={"ok": 120, "unread": 7, "not_found": 2, "truncated": 1}))
    e["evidence"] = {"coins": [], "null": {"test": "binomial", "n": 9, "k": 4, "q": 0.02, "p": 0.0007}}
    _run(s, graph, [e], names={"A": "prevaile"})
    (fact,) = _live(s)
    for part in ("So1Compa landed after prevaile inside prevaile's buy slot", "4 of 9", "base rate 0.02",
                 "armed-wallet buys only", "ok 120, unread 7, not_found 2, truncated 1"):
        assert part in fact["statement"], part
    assert "J7 roster only" not in fact["statement"]


def test_a_recreated_graph_db_supersedes_rather_than_duplicates(tmp_path):
    """Gamma LOW 2026-09-29: with an empty map, the old fact of the SAME edge came back as a candidate
    and was judged unrelated, leaving two live facts per edge and orphaning the first for good."""
    s = _store()
    names = {"L": "leader", "F": "fol", "G": "gee", "H": "aitch"}
    edges = [_follows(), _follows("L", "G"), _follows("H", "L")]
    _run(s, tmp_path / "a.db", edges, names=names)
    _run(s, tmp_path / "b.db", edges, names=names, finished="2026-09-29T23:30:00+00:00")
    statuses = sorted(f["status"] for f in s.rows["brain_facts"])
    assert len(_live(s)) == 3 and statuses.count("superseded") == 3
    assert not any(f.get("judge_pending") for f in _live(s))


def _slot(edge_type, src="A", dst="So1C", n=4):
    e = _follows(src, dst, n=n, p=0.0007)
    e.update(edge_type=edge_type, null_p99=None, lag_p50_s=None, lag_p90_s=None,
             coverage=dict(g.COVERAGE, slot_reads={"ok": 100}))
    e["evidence"] = {"coins": [], "null": {"test": "binomial", "n": 9, "k": n, "q": 0.02, "p": 0.0007}}
    return e


def test_a_slot_follower_fact_replaces_the_co_buy_fact_for_the_same_pair(tmp_path):
    """Alpha 2026-09-29: 38 of 40 co_buy_slot pairs were also slot_follower pairs, so the brain held
    two facts per pair. slot_follower wins; co_buy_slot is projected only where no follower edge
    exists, and a co_buy fact already written for a deduplicated pair is retracted as a duplicate."""
    s, graph = _store(), tmp_path / "graph.db"
    _run(s, graph, [_slot("co_buy_slot"), _slot("co_buy_slot", dst="So1D")], names={"A": "prevaile"})
    assert len(_live(s)) == 2
    out = _run(s, graph, [_slot("co_buy_slot"), _slot("slot_follower"), _slot("co_buy_slot", dst="So1D")],
               names={"A": "prevaile"}, finished="2026-09-29T23:30:00+00:00")
    kinds = sorted(json.loads(f["detail"])["edge_key"] for f in _live(s))
    assert kinds == ["solana|co_buy_slot|A|So1D", "solana|slot_follower|A|So1C"]
    assert out["retracted"] == 1
    (gone,) = [f for f in s.rows["brain_facts"] if f["status"] == "retracted"]
    assert "deduplicated" in gone["verify_note"] and "no longer passes" not in gone["verify_note"]
