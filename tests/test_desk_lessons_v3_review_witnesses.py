"""Offline behavioral witnesses for the remaining lessons-v3 evidence gaps."""
import json
import sqlite3
from types import SimpleNamespace

from test_desk_lessons import DG, L, T0, W, row, swap, unit


def test_verdict_time_entry_rank_cannot_depend_on_the_later_fill_price():
    prior = [swap(T0 - 120 + i, 3e-8 + i * 1e-10, i=i) for i in range(121)]
    after = [swap(T0 + 2 + i, 3e-8, i=200 + i) for i in range(1000)]
    low = prior + [swap(T0 + 1.5, 2e-8, i=150)] + after
    high = prior + [swap(T0 + 1.5, 9e-8, i=150)] + after
    assert [r for r in low if r["received_at"] <= T0] == [r for r in high if r["received_at"] <= T0]

    decision = unit(t_ref=T0, t_fill=T0 + 1.5)
    a, _ = L.facts_for(decision, L.Tape(low), launch_at=None, fill_latency=1.5)
    b, _ = L.facts_for(decision, L.Tape(high), launch_at=None, fill_latency=1.5)

    assert a["entry_rank_prior_pct"] is not None
    assert a["entry_rank_prior_pct"] == b["entry_rank_prior_pct"]


def test_good_exit_baseline_cannot_borrow_five_priors_from_another_cohort(tmp_path, monkeypatch):
    from sniper.util import clock

    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_DESK_LESSONS_ENABLED", "1")
    monkeypatch.setattr(clock, "offset_s", lambda: 0.0)
    monkeypatch.setattr(W, "time", SimpleNamespace(time=lambda: T0 + 6000))
    out = tmp_path / "lessons.db"
    db = L.connect(out)
    for i in range(5):
        prior = {"exit_regret_R": 0.5, "identity": {"cohort_id": "cohort-a", "prompt_version": "p1"}}
        L.store(db, "pos:a%d" % i, "realized", "A%d" % i, i, None, T0 - 100 + i, T0 - 100 + i, prior, [])
    db.close()

    candidate = {"key": "pos:b", "kind": "realized", "mint": "B", "t_ref": T0,
                 "cohort": "cohort-b", "prompt_version": "p1"}
    facts = {"t0": T0, "exit_regret_R": 0.2, "censored": [],
             "identity": {"cohort_id": "cohort-b", "prompt_version": "p1"}}
    monkeypatch.setattr(W, "discover", lambda *args: ([candidate], 0))
    monkeypatch.setattr(W, "fill_latency", lambda *args: 1.5)
    monkeypatch.setattr(W, "compute", lambda *args: (dict(facts), []))

    assert W.main(["--out", str(out), "--ledger", str(tmp_path / "ledger.db"),
                   "--desk-db", str(tmp_path / "desk.db"), "--max", "1"]) == 0
    with sqlite3.connect(out) as db:
        stored = db.execute("SELECT facts_json, tags_json FROM lessons WHERE key='pos:b'").fetchone()
    assert stored is not None
    result, tags = json.loads(stored[0]), json.loads(stored[1])
    assert result["good_exit_band"]["n"] == 0
    assert result["good_exit_band"]["median_regret_R"] is None
    assert "good_exit" not in tags


def test_digest_marks_all_null_regret_dollars_unavailable_or_known_zero():
    rows = [row("realized", "M%d" % i, ["late_curve"], pnl=-1, R=-0.04, fill=0.95) for i in range(20)]
    for item in rows:
        item["facts"].update(entry_regret_pct=None, entry_vs_best_after_pct=None, exit_regret_R=None)
    digest = DG.digest(rows)
    assert digest["tags"]["late_curve"]["entry_regret_known_n"] == 0
    assert digest["tags"]["late_curve"]["exit_regret_known_n"] == 0

    tag_line = next(line for line in DG.render_markdown(digest) if line.startswith("| late_curve |"))
    regret_cells = tag_line.split("|")[-3:-1]
    for cell in regret_cells:
        assert any(marker in cell.lower() for marker in
                   ("unavailable", "unknown", "n/a", "none", "—", "known=0", "known n=0", "known 0", "n=0")), tag_line
