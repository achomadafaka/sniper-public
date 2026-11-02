"""Task 11 memory core (Lambda): known-time retrieval, supersession as-of, sealing. In-memory backend; the brain (office/*) adapter implements the same two methods."""
import pytest

from sniper.office import memory as M


class Mem:
    """Backend protocol: entries() -> list[dict]; add(dict); rank(query, entries) -> ordered entries."""
    def __init__(self):
        self.rows = []

    def entries(self, section=None, cut=None):
        return [dict(r) for r in self.rows if (section is None or r["section"] == section) and (cut is None or r["known_at"] <= cut)]

    def get(self, ids):
        return {r["entry_id"]: dict(r) for r in self.rows if r["entry_id"] in set(ids)}

    def add(self, row):
        row = dict(row, entry_id="m%d" % (len(self.rows) + 1))
        self.rows.append(row)
        return row["entry_id"]

    def rank(self, query, entries):
        return sorted(entries, key=lambda e: (-sum(w in e["text"] for w in query.split()), e["entry_id"]))


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def lesson(text, trades=("t1",), coin="C1", creator="W1", minute=100, **kw):
    return {"section": "office/lessons", "author": "pm", "text": text, "source_trade_ids": list(trades), "sources": [{"coin": coin, "creator": creator, "minute": minute}], **kw}


CAND = {"coin": "C9", "creator": "W9", "minute": 500}


def test_a_later_lesson_is_excluded_from_an_earlier_decision():
    b, clk = Mem(), Clock(1000.0)
    M.write(b, clk, lesson("early rug pattern"))
    clk.t = 2000.0
    M.write(b, clk, lesson("late rug pattern"))
    got = M.retrieve(b, "rug", decision_cut=1500.0, candidate=CAND, viewer_role="System")
    assert [e["text"] for e in got] == ["early rug pattern"]


def test_author_cannot_backdate_known_at_and_an_unknown_clock_refuses():
    b = Mem()
    with pytest.raises(M.Refused):
        M.write(b, Clock(1000.0), dict(lesson("x"), known_at=1.0))
    with pytest.raises(M.Refused):
        M.write(b, lambda: None, lesson("x"))
    with pytest.raises(M.Refused):
        M.write(b, lambda: float("nan"), lesson("x"))


def test_lessons_from_the_same_coin_creator_or_minute_window_are_excluded():
    b, clk = Mem(), Clock(1000.0)
    M.write(b, clk, lesson("same coin rug", coin="C9"))
    M.write(b, clk, lesson("same creator rug", creator="W9"))
    M.write(b, clk, lesson("same minute rug", minute=500))
    M.write(b, clk, lesson("unrelated rug"))
    got = [e["text"] for e in M.retrieve(b, "rug", decision_cut=2000.0, candidate=CAND, viewer_role="System")]
    assert got == ["unrelated rug"]


def test_supersession_is_applied_as_of_the_cut_not_now():
    b, clk = Mem(), Clock(1000.0)
    old = M.write(b, clk, lesson("v1 avoid fresh pools"))
    clk.t = 3000.0
    M.write(b, clk, lesson("v2 allow fresh pools", supersedes=old))
    assert [e["text"] for e in M.retrieve(b, "pools", decision_cut=2000.0, candidate=CAND, viewer_role="System")] == ["v1 avoid fresh pools"]       # the replacement did not exist yet
    assert [e["text"] for e in M.retrieve(b, "pools", decision_cut=4000.0, candidate=CAND, viewer_role="System")] == ["v2 allow fresh pools"]


def test_filter_before_rank_returns_a_full_k_when_enough_visible_entries_exist():
    b, clk = Mem(), Clock(1000.0)
    for i in range(5):
        M.write(b, clk, lesson("rug %d" % i))
    clk.t = 9000.0
    for i in range(20):
        M.write(b, clk, lesson("rug rug rug future %d" % i))                       # would out-rank the visible ones and starve a top-k-then-filter implementation
    got = M.retrieve(b, "rug", decision_cut=2000.0, candidate=CAND, k=5, viewer_role="System")
    assert len(got) == 5 and all("future" not in e["text"] for e in got)


def pitch(text, decision_id, author):
    return dict(lesson(text), section="office/notes", kind="pitch", author=author, decision_id=decision_id)


def test_a_pitch_is_invisible_to_peers_until_the_trusted_barrier_releases_the_decision():
    b, clk = Mem(), Clock(1000.0)
    M.write(b, clk, pitch("judge A pitch ENTER", "d1", "judgeA"))
    assert M.retrieve(b, "pitch", decision_cut=2000.0, candidate=CAND, viewer="judgeB", viewer_role="System") == []
    assert [e["text"] for e in M.retrieve(b, "pitch", decision_cut=2000.0, candidate=CAND, viewer="judgeA", viewer_role="System")] == ["judge A pitch ENTER"]         # the author always sees their own
    assert M.retrieve(b, "pitch", decision_cut=2000.0, candidate=CAND, viewer="judgeB", released_decisions=frozenset({"d2"}), viewer_role="System") == []            # another decision's release does not help
    got = M.retrieve(b, "pitch", decision_cut=2000.0, candidate=CAND, viewer="judgeB", released_decisions=frozenset({"d1"}), viewer_role="System")
    assert [e["text"] for e in got] == ["judge A pitch ENTER"]


def test_an_entry_flag_cannot_release_a_pitch():
    b, clk = Mem(), Clock(1000.0)
    M.write(b, clk, dict(pitch("A pitch", "d1", "judgeA"), committed=True))                    # the old per-entry boolean no longer matters
    assert M.retrieve(b, "pitch", decision_cut=2000.0, candidate=CAND, viewer="judgeB", viewer_role="System") == []


def test_sealed_snapshot_pins_membership_hashes_and_supersession_and_ignores_later_writes():
    b, clk = Mem(), Clock(1000.0)
    old = M.write(b, clk, lesson("v1"))
    clk.t = 1500.0
    M.write(b, clk, lesson("v2", supersedes=old))
    snap = M.seal_snapshot(b, epoch_id="e1", cut=2000.0)
    assert snap["members"] and all(len(m["content_sha256"]) == 64 for m in snap["members"]) and snap["supersessions"] == [{"old": old, "new": snap["members"][-1]["entry_id"], "old_sha256": snap["supersessions"][0]["old_sha256"]}] and len(snap["supersessions"][0]["old_sha256"]) == 64
    clk.t = 3000.0
    M.write(b, clk, lesson("v3 evaluation derived"))
    again = M.seal_snapshot(b, epoch_id="e1", cut=2000.0)
    assert again["snapshot_id"] == snap["snapshot_id"]                              # later writes cannot change a sealed snapshot
    got = M.retrieve(b, "v", decision_cut=9999.0, candidate=CAND, snapshot=snap, trusted_snapshot_id=snap["snapshot_id"], viewer_role="System")
    assert [e["text"] for e in got] == ["v2"]                                       # reads only the sealed membership, even with a later cut
    b.rows[0]["text"] = "TAMPERED"
    with pytest.raises(M.Refused):
        M.retrieve(b, "v", decision_cut=9999.0, candidate=CAND, snapshot=snap, trusted_snapshot_id=snap["snapshot_id"], viewer_role="System")      # a deleted/edited member fails its content hash


def test_deleted_member_of_a_sealed_snapshot_is_detected():
    b, clk = Mem(), Clock(1000.0)
    M.write(b, clk, lesson("keep me"))
    snap = M.seal_snapshot(b, epoch_id="e1", cut=2000.0)
    b.rows.clear()
    with pytest.raises(M.Refused):
        M.retrieve(b, "keep", decision_cut=2000.0, candidate=CAND, snapshot=snap, trusted_snapshot_id=snap["snapshot_id"], viewer_role="System")


def test_a_snapshot_never_serves_an_entry_known_after_the_decision_cut():
    b, clk = Mem(), Clock(100.0)
    M.write(b, clk, lesson("known at 100"))
    snap = M.seal_snapshot(b, epoch_id="e1", cut=200.0)
    assert M.retrieve(b, "known", decision_cut=50.0, candidate=CAND, snapshot=snap, trusted_snapshot_id=snap["snapshot_id"], viewer_role="System") == []        # decision at t=50 cannot see t=100
    assert len(M.retrieve(b, "known", decision_cut=150.0, candidate=CAND, snapshot=snap, trusted_snapshot_id=snap["snapshot_id"], viewer_role="System")) == 1


def test_a_snapshot_with_emptied_members_or_a_forged_id_is_refused_and_a_missing_trusted_id_is_refused():
    b, clk = Mem(), Clock(100.0)
    M.write(b, clk, lesson("keep"))
    snap = M.seal_snapshot(b, epoch_id="e1", cut=200.0)
    emptied = dict(snap, members=[])
    with pytest.raises(M.Refused):
        M.retrieve(b, "keep", decision_cut=200.0, candidate=CAND, snapshot=emptied, trusted_snapshot_id=snap["snapshot_id"], viewer_role="System")        # body no longer hashes to the id
    with pytest.raises(M.Refused):
        M.retrieve(b, "keep", decision_cut=200.0, candidate=CAND, snapshot=snap, trusted_snapshot_id="0" * 64, viewer_role="System")                       # id is not the trusted sealed one
    with pytest.raises(M.Refused):
        M.retrieve(b, "keep", decision_cut=200.0, candidate=CAND, snapshot=snap, viewer_role="System")                                                      # no trusted id: refuse, never trust the dict
    other = M.seal_snapshot(b, epoch_id="e2", cut=200.0)
    assert other["snapshot_id"] != snap["snapshot_id"]                                                                               # the id is bound to the epoch


def test_every_field_the_reader_consumes_is_pinned_so_tampering_a_sealed_pitch_is_refused():
    b, clk = Mem(), Clock(1000.0)
    M.write(b, clk, pitch("A pitch", "d1", "judgeA"))
    snap = M.seal_snapshot(b, epoch_id="e1", cut=2000.0)
    tid = snap["snapshot_id"]
    base = [dict(r) for r in b.rows]
    for field, new in (("decision_id", "d2"), ("kind", "note"), ("author", "judgeB"), ("sources", []), ("section", "office/lessons"), ("extra_consumed_field", 1)):
        b.rows[:] = [dict(r) for r in base]
        b.rows[0][field] = new
        with pytest.raises(M.Refused):
            M.retrieve(b, "pitch", decision_cut=2000.0, candidate=CAND, viewer="judgeB", released_decisions=frozenset({"d2"}), snapshot=snap, trusted_snapshot_id=tid, viewer_role="System")
    b.rows[:] = [dict(r) for r in base]
    assert M.retrieve(b, "pitch", decision_cut=2000.0, candidate=CAND, viewer="judgeB", released_decisions=frozenset({"d1"}), snapshot=snap, trusted_snapshot_id=tid, viewer_role="System")


def test_quarantined_roles_read_only_sealed_snapshots_and_never_evaluation_derived_entries():
    b, clk = Mem(), Clock(1000.0)
    M.write(b, clk, lesson("plain lesson"))
    M.write(b, clk, dict(lesson("evaluation derived lesson"), evaluation_derived=True))
    with pytest.raises(M.Refused):
        M.retrieve(b, "lesson", decision_cut=2000.0, candidate=CAND, viewer="quant1", viewer_role="Quant")                   # live memory is closed to the Quant
    snap = M.seal_snapshot(b, epoch_id="e1", cut=2000.0)
    got = M.retrieve(b, "lesson", decision_cut=2000.0, candidate=CAND, viewer="quant1", viewer_role="Quant", snapshot=snap, trusted_snapshot_id=snap["snapshot_id"])
    assert [e["text"] for e in got] == ["plain lesson"]                                                                        # the sealed view, minus evaluation-derived content
    # a non-quarantined role still sees both in live memory
    assert {e["text"] for e in M.retrieve(b, "lesson", decision_cut=2000.0, candidate=CAND, viewer="pm1", viewer_role="Postmortem")} == {"plain lesson", "evaluation derived lesson"}


def test_the_chooser_role_is_quarantined_too():
    b, clk = Mem(), Clock(1000.0)
    M.write(b, clk, lesson("x lesson"))
    with pytest.raises(M.Refused):
        M.retrieve(b, "lesson", decision_cut=2000.0, candidate=CAND, viewer="c1", viewer_role="Chooser")


def test_viewer_role_is_required_and_the_head_is_quarantined_like_the_quant():
    b, clk = Mem(), Clock(1000.0)
    M.write(b, clk, lesson("plain lesson"))
    M.write(b, clk, dict(lesson("evaluation derived lesson"), evaluation_derived=True))
    with pytest.raises(TypeError):
        M.retrieve(b, "lesson", decision_cut=2000.0, candidate=CAND)                              # no default identity
    for bad in (None, "", "Wizard", "head"):
        with pytest.raises(M.Refused):
            M.retrieve(b, "lesson", decision_cut=2000.0, candidate=CAND, viewer_role=bad)
    with pytest.raises(M.Refused):
        M.retrieve(b, "lesson", decision_cut=2000.0, candidate=CAND, viewer_role="Head")           # the adaptive chooser reads sealed snapshots only
    snap = M.seal_snapshot(b, epoch_id="e1", cut=2000.0)
    got = M.retrieve(b, "lesson", decision_cut=2000.0, candidate=CAND, viewer_role="Head", snapshot=snap, trusted_snapshot_id=snap["snapshot_id"])
    assert [e["text"] for e in got] == ["plain lesson"]
