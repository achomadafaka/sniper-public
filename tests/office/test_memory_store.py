"""Task 11 adapter: the memory core over the Office store (memory_entries + memory_snapshots + FTS5). entry_id = row_hash; known_at is the trusted writer clock; filters run BEFORE ranking in SQL."""
import json
import re

import pytest

from sniper.office import memory as M
from sniper.office import memory_store as MS
from sniper.office import store as S

CAND = {"coin": "C9", "creator": "W9", "minute": 500}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def mem(tmp_path):
    conn = S.open_store(tmp_path / "office.db")
    yield MS.OfficeMemory(conn), conn
    conn.close()


def lesson(text, ns="office/lessons", coin="C1", **kw):
    return {"section": ns, "kind": "lesson", "author": "pm1", "text": text, "source_trade_ids": ["t1"], "sources": [{"coin": coin, "creator": "W1", "minute": 100}], "version": 1, **kw}


def get(m, q, cut, role="System", **kw):
    return M.retrieve(m, q, decision_cut=cut, candidate=CAND, viewer_role=role, **kw)


def test_write_stamps_known_at_returns_the_row_hash_as_identity_and_roundtrips(mem):
    m, conn = mem
    eid = M.write(m, Clock(1000.0), lesson("rug pattern in fresh pools"))
    assert len(eid) == 64 and S.latest(conn, "memory_entries", "row_hash", eid)["row_hash"] == eid
    e = m.get([eid])[eid]
    assert e["text"] == "rug pattern in fresh pools" and e["section"] == "office/lessons" and e["known_at"] == 1000.0 and e["entry_id"] == eid
    assert S.verify_chain(conn, "memory_entries") == (True, None)


def test_domain_validation_refuses_bad_entries_and_writes_nothing(mem):
    m, conn = mem
    bad = [lesson("x", ns="crew/lessons"), dict(lesson("x"), kind="gossip"), dict(lesson("x"), text=""), dict(lesson("x"), text="y" * 8001), dict(lesson("x"), author=""),
           dict(lesson("x"), kind="pitch"), dict(lesson("x"), evaluation_derived="yes"), dict(lesson("x"), version=0), dict(lesson("x"), source_trade_ids="t1"),
           dict(lesson("x"), supersedes="0" * 64)]
    for b in bad:
        with pytest.raises(M.Refused):
            M.write(m, Clock(1000.0), b)
    assert conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0] == 0


def test_the_caller_cannot_supply_known_at_or_entry_id_and_an_unknown_clock_refuses(mem):
    m, conn = mem
    for extra in ({"known_at": 1.0}, {"entry_id": "a" * 64}):
        with pytest.raises(M.Refused):
            M.write(m, Clock(1000.0), dict(lesson("x"), **extra))
    with pytest.raises(M.Refused):
        M.write(m, lambda: None, lesson("x"))
    assert conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0] == 0


def test_retrieval_applies_the_cut_in_sql_and_filters_before_ranking_with_a_full_k(mem):
    m, conn = mem
    clk = Clock(1000.0)
    for i in range(5):
        M.write(m, clk, lesson("rug note %d" % i, coin="C%d" % (20 + i)))
    clk.t = 9000.0
    for i in range(20):
        M.write(m, clk, lesson("rug rug rug future %d" % i, coin="C%d" % (40 + i)))             # would starve a rank-then-filter retrieval
    got = get(m, "rug", 2000.0, k=5)
    assert len(got) == 5 and all("future" not in e["text"] for e in got)


def test_same_source_exclusion_supersession_as_of_the_cut_and_pitch_barrier_work_through_the_store(mem):
    m, conn = mem
    clk = Clock(1000.0)
    old = M.write(m, clk, lesson("v1 avoid fresh pools"))
    M.write(m, clk, lesson("same coin pools", coin="C9"))
    clk.t = 3000.0
    M.write(m, clk, lesson("v2 allow fresh pools", supersedes=old))
    assert [e["text"] for e in get(m, "pools", 2000.0)] == ["v1 avoid fresh pools"]
    assert [e["text"] for e in get(m, "pools", 4000.0)] == ["v2 allow fresh pools"]
    M.write(m, clk, {"section": "office/notes", "kind": "pitch", "author": "judgeA", "text": "judge A pitch ENTER", "decision_id": "d1", "source_trade_ids": [], "sources": [], "version": 1})
    assert get(m, "pitch", 4000.0, viewer="judgeB") == []
    assert [e["text"] for e in get(m, "pitch", 4000.0, viewer="judgeB", released_decisions=frozenset({"d1"}))] == ["judge A pitch ENTER"]


def test_a_query_with_fts_syntax_characters_is_data_not_syntax(mem):
    m, _ = mem
    M.write(m, Clock(1000.0), lesson("rug OR pool AND NOT near(x) \"quoted\" -dash"))
    assert len(get(m, 'rug" OR NOT (', 2000.0)) == 1                                           # tokenised and quoted: no FTS5 syntax error, no injection
    assert get(m, "", 2000.0) == [] or isinstance(get(m, "", 2000.0), list)


def test_sealed_snapshots_persist_once_load_by_trusted_id_and_serve_the_quarantined_roles(mem):
    m, conn = mem
    clk = Clock(1000.0)
    M.write(m, clk, lesson("plain lesson"))
    M.write(m, clk, dict(lesson("evaluation derived lesson"), evaluation_derived=True))
    sid = MS.seal(m, epoch_id="e1", cut=2000.0)
    assert MS.seal(m, epoch_id="e1", cut=2000.0) == sid and conn.execute("SELECT COUNT(*) FROM memory_snapshots").fetchone()[0] == 1       # idempotent
    clk.t = 3000.0
    M.write(m, clk, lesson("later lesson"))
    assert MS.seal(m, epoch_id="e1", cut=2000.0) == sid                                                                                      # later writes cannot change it
    got = MS.retrieve_sealed(m, "lesson", snapshot_id=sid, decision_cut=2000.0, candidate=CAND, viewer_role="Head")
    assert [e["text"] for e in got] == ["plain lesson"]
    with pytest.raises(M.Refused):
        MS.retrieve_sealed(m, "lesson", snapshot_id="0" * 64, decision_cut=2000.0, candidate=CAND, viewer_role="Head")                      # unknown snapshot id
    with pytest.raises(M.Refused):
        get(m, "lesson", 2000.0, role="Head")                                                                                              # live memory stays closed to the Head


def test_the_read_queries_use_indexes_never_a_table_scan(mem):
    m, conn = mem
    M.write(m, Clock(1000.0), lesson("rug"))
    seen = []
    conn.set_trace_callback(lambda s: seen.append(s) if "memory_entries" in s and s.lstrip().upper().startswith(("SELECT", "WITH")) else None)
    get(m, "rug", 2000.0)
    m.get(["0" * 64])
    m.entries(section="office/lessons", cut=2000.0)
    conn.set_trace_callback(None)
    plans = [" | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + s)) for s in dict.fromkeys(seen)]
    assert len(plans) >= 4
    for plan in plans:
        assert re.search(r"SCAN memory_entries(?!_)", plan) is None, plan                          # SCAN of the FTS virtual table is its own lookup; a table SCAN is the defect
    assert any("memory_entries_known_at_seq" in p and "known_at<" in p for p in plans), plans          # the decision cut is an index range


def corrupt(conn, sql, args=()):
    conn.execute("DROP TRIGGER memory_entries_no_update")                  # what an attacker with file access can do; the chain must still catch it
    conn.execute(sql, args)
    conn.commit()


def seeded(mem, n=3):
    m, conn = mem
    clk = Clock(1000.0)
    ids = [M.write(m, clk, lesson("rug note %d" % i, coin="C%d" % (20 + i))) for i in range(n)]
    return m, conn, ids


def test_a_payload_changed_with_the_old_row_hash_is_refused_everywhere_and_never_sealed(mem):
    m, conn, ids = seeded(mem)
    corrupt(conn, "UPDATE memory_entries SET payload_json = replace(payload_json, 'rug note 1', 'TAMPERED') WHERE seq = 2")
    with pytest.raises(M.Refused):
        m.entries(cut=2000.0)
    with pytest.raises(M.Refused):
        m.get([ids[1]])
    with pytest.raises(M.Refused):
        get(m, "rug", 2000.0)
    with pytest.raises(M.Refused):
        MS.seal(m, epoch_id="e1", cut=2000.0)                                                      # a corrupted row is never admitted under a fresh trusted seal
    assert m.get([ids[0]])[ids[0]]["text"] == "rug note 0"                                         # untouched rows stay readable: only selected rows are verified


def test_a_changed_row_hash_or_prev_hash_is_refused(mem):
    m, conn, ids = seeded(mem)
    corrupt(conn, "UPDATE memory_entries SET row_hash = ? WHERE seq = 1", ("f" * 64,))
    with pytest.raises(M.Refused):
        m.get(["f" * 64])
    corrupt_prev = "UPDATE memory_entries SET prev_hash = ? WHERE seq = 3"
    conn.execute(corrupt_prev, ("d" * 64,))
    conn.commit()
    with pytest.raises(M.Refused):
        m.get([ids[2]])                                                                            # prev_hash changed, row_hash kept: the row no longer hashes to its own row_hash


def test_a_rehashed_row_is_caught_by_the_link_to_its_predecessor(mem):
    import hashlib
    m, conn, ids = seeded(mem)
    row = conn.execute("SELECT payload_json FROM memory_entries WHERE seq = 2").fetchone()[0]
    bad_prev = "e" * 64
    good_hash = hashlib.sha256(row.encode("utf-8") + bad_prev.encode("ascii")).hexdigest()           # self-consistent row whose prev_hash is not row 1's hash
    corrupt(conn, "UPDATE memory_entries SET prev_hash = ?, row_hash = ? WHERE seq = 2", (bad_prev, good_hash))
    with pytest.raises(M.Refused):
        m.get([good_hash])


def test_a_corrupted_snapshot_row_is_refused_on_load(mem):
    m, conn, ids = seeded(mem)
    sid = MS.seal(m, epoch_id="e1", cut=2000.0)
    conn.execute("DROP TRIGGER memory_snapshots_no_update")
    conn.execute("UPDATE memory_snapshots SET payload_json = replace(payload_json, 'e1', 'e2')")
    conn.commit()
    with pytest.raises(M.Refused):
        m.get_snapshot(sid)
    with pytest.raises(M.Refused):
        MS.retrieve_sealed(m, "rug", snapshot_id=sid, decision_cut=2000.0, candidate=CAND, viewer_role="Head")
