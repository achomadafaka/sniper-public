"""Office lessons loop: grade.read pages Gamma's grader; lesson.propose writes office memory; the next prompt of the
same role carries its lessons."""
import pytest

from sniper.office import lessons as LS, memory as M, memory_store as MS, runtime_steps as RS, store as S


class FakeLedger:
    def __init__(self, rows):self.rows = rows


def fake_grader(rows):
    def grade(conn, *, epoch_id, after=None, limit=None):
        out = [r for r in rows if after is None or tuple(r["cursor"]) > tuple(after)]
        return out[:limit]
    return grade


ROWS = [{"order_id": "o%d" % i, "state": "graded", "cursor": [100.0 + i, "o%d" % i]} for i in range(3)]


def test_grade_page_is_bounded_and_carries_a_cursor(monkeypatch):
    monkeypatch.setattr(LS, "grade_closed", fake_grader(ROWS))
    page = LS.grade_page(None, epoch_id="e", evaluated=False, viewer_role="postmortem", cursor="", limit=2)
    assert [r["order_id"] for r in page["rows"]] == ["o0", "o1"] and page["next"] == "101.0|o1"
    rest = LS.grade_page(None, epoch_id="e", evaluated=False, viewer_role="postmortem", cursor=page["next"], limit=2)
    assert [r["order_id"] for r in rest["rows"]] == ["o2"] and rest["next"] is None


def test_evaluated_grades_are_hidden_from_quarantined_roles(monkeypatch):
    monkeypatch.setattr(LS, "grade_closed", fake_grader(ROWS))
    assert LS.grade_page(None, epoch_id="e", evaluated=True, viewer_role="head", cursor="")["state"] == "refused"
    assert LS.grade_page(None, epoch_id="e", evaluated=True, viewer_role="postmortem", cursor="")["state"] == "known"


def test_malformed_cursor_is_refused_not_restarted(monkeypatch):
    monkeypatch.setattr(LS, "grade_closed", fake_grader(ROWS))
    with pytest.raises(ValueError):
        LS.grade_page(None, epoch_id="e", evaluated=False, viewer_role="postmortem", cursor="nope")


def test_lesson_reaches_the_next_prompt_of_the_same_role_only(tmp_path):
    mem = MS.OfficeMemory(S.open_store(tmp_path / "office.db"))
    LS.propose(mem, lambda: 50.0, author="ag_pm", for_role="judge", text="Skip coins whose top10 holders exceed 40%.",
               order_ids=["o1"], evaluated=False)
    assert [i["text"] for i in LS.for_role(mem, role="judge", cut=60.0)["items"]] == ["Skip coins whose top10 holders exceed 40%."]
    assert LS.for_role(mem, role="scout", cut=60.0)["items"] == []
    assert LS.for_role(mem, role="judge", cut=40.0)["items"] == []          # causal: not known before its write


def test_quarantined_role_gets_no_live_lessons(tmp_path):
    mem = MS.OfficeMemory(S.open_store(tmp_path / "office.db"))
    assert LS.for_role(mem, role="head", cut=60.0) == {"state": "quarantined", "items": []}


def test_bad_lesson_target_is_refused(tmp_path):
    mem = MS.OfficeMemory(S.open_store(tmp_path / "office.db"))
    with pytest.raises(ValueError):
        LS.propose(mem, lambda: 50.0, author="a", for_role="postmortem", text="x", order_ids=[], evaluated=False)


def test_postmortem_is_a_seat_and_its_tools_are_declared():
    assert RS.slot_of({"role": "Postmortem"}) == "postmortem"
    assert "lesson.propose" in RS.WRITE_TOOLS and {"grade.read", "lesson.propose"} <= set(RS._REQUEST_ARGS)


def test_evaluation_derived_lessons_stay_shadow(tmp_path):
    mem = MS.OfficeMemory(S.open_store(tmp_path / "office.db"))
    LS.propose(mem, lambda: 50.0, author="ag_pm", for_role="judge", text="graded lesson", order_ids=["o1"], evaluated=True)
    LS.propose(mem, lambda: 51.0, author="ag_pm", for_role="judge", text="unknown epoch", order_ids=["o2"], evaluated=None)
    assert LS.for_role(mem, role="judge", cut=60.0)["items"] == []


def test_lessons_live_epoch_injects_grade_derived_lessons(tmp_path):
    mem = MS.OfficeMemory(S.open_store(tmp_path / "office.db"))
    LS.propose(mem, lambda: 50.0, author="ag_pm", for_role="judge", text="graded lesson", order_ids=["o1"], evaluated=True)
    assert [i["text"] for i in LS.for_role(mem, role="judge", cut=60.0, live=True)["items"]] == ["graded lesson"]
    assert LS.for_role(mem, role="judge", cut=60.0, live="yes")["items"] == []                # only a literal True
    assert LS.for_role(mem, role="head", cut=60.0, live=True)["state"] == "quarantined"
