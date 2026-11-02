"""Task 18 org side: seats -> pod books, scores, and the scheduled frozen reallocation on a real ledger."""
import json
from pathlib import Path

import pytest

from sniper.office import ledger as L, org as O, pod_floor as F, pods as P

ROOT = Path(__file__).resolve().parents[2]
NOW = 1_800_000_000.0 - 1_800_000_000.0 % 86400 + 86400 + 3600          # 01:00 UTC on a day after the closes below
FX = 150.0
RULE = P.load_rule(ROOT / "config" / "office" / "pods.json")


def seats(n=3):
    org = json.loads((ROOT / "config/office/default_org.json").read_text())
    return O.expand({**org, "pods": n})


@pytest.fixture
def conn(tmp_path):
    return L.open_ledger(tmp_path / "ledger.db")


def book(conn, n=3, kind="evaluated", total=1500.0, epoch="E1"):
    return F.open_pod_books(conn, RULE, seats(n), epoch_id=epoch, clip_usd=25.0, total_usd=total, kind=kind, actor="crew", note="t", now=NOW - 1000)


def close(conn, pod, i, usd, epoch="E1", at=None):
    oid = "%s-%d" % (pod, i)
    conn.execute("INSERT INTO orders(order_id, book_id, epoch_id, book_kind, network_id, mint, pool, decision_bundle_hash, policy_json, policy_hash, "
                 "fill_model_version, state, requested_at, closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'closed', ?, ?)",
                 (oid, P.book_id(epoch, pod), epoch, "evaluated", "solana", oid, "P", "h", "{}", "h", "v", NOW - 5000, NOW - 3600 * 5 if at is None else at))
    conn.execute("INSERT INTO cashflows(order_id, kind, at, lamports, cost_lamports, cost_basis, cost_evidence, fx_usd, fx_at, usd) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (oid, "exit", NOW - 5000, "0", "0", "x", "x", FX, NOW - 5000, usd))
    conn.commit()


def scored(conn, wins):
    for pod, usd in wins.items():
        for i in range(5):
            close(conn, pod, i, usd / 5)


def run(conn, **kw):
    return F.reallocate(conn, RULE, epoch_id="E1", now=NOW, sol_usd=FX, sol_at=NOW - 5, **kw)


def test_open_books_one_per_pod_rule_frozen_capital_split_and_idempotent(conn):
    ids = book(conn)
    assert ids == {"pod1": "E1:pod1", "pod2": "E1:pod2", "pod3": "E1:pod3"}
    assert [L.capital(conn, b) for b in ids.values()] == [500.0, 500.0, 500.0]
    assert conn.execute("SELECT rule_sha256 FROM epoch_pods_rules WHERE epoch_id='E1'").fetchone()
    assert book(conn) == ids and conn.execute("SELECT COUNT(*) FROM capital_flows").fetchone()[0] == 3
    with pytest.raises(ValueError, match="pod_open_conflict"):
        book(conn, total=1800.0)
    with pytest.raises(ValueError, match="pod_open_conflict"):
        book(conn, kind="discovery")


def test_shared_seats_hold_no_book():
    assert F.pod_ids(seats(2)) == ["pod1", "pod2"] and all(s["pod"] is None for s in seats(2) if s["role"] in ("Head", "Risk"))


def test_scores_count_whole_days_before_today_only(conn):
    book(conn)
    close(conn, "pod1", 0, 10.0)
    close(conn, "pod1", 1, 5.0, at=NOW - 3600 * 24 * 8)                # older than the 7-day window
    close(conn, "pod1", 2, 99.0, at=NOW - 60)                          # today: not a whole day yet
    assert F.pod_scores(conn, RULE, epoch_id="E1", pods=["pod1", "pod2"], now=NOW) == {"pod1": (10.0, 1), "pod2": (0.0, 0)}


def test_best_pod_takes_the_step_once_and_a_replay_is_no_second_effect(conn):
    book(conn)
    scored(conn, {"pod1": 40.0, "pod2": -10.0, "pod3": 0.0})
    out = run(conn)
    assert [s for _, s in out] == ["booked"]
    before = conn.execute("SELECT COUNT(*) FROM capital_flows").fetchone()[0]
    assert [s for _, s in run(conn)] == ["replayed"]
    assert conn.execute("SELECT COUNT(*) FROM capital_flows").fetchone()[0] == before
    assert [L.capital(conn, "E1:" + p) for p in ("pod1", "pod2", "pod3")] == [650.0, 350.0, 500.0]


def test_unknown_inputs_book_nothing(conn):
    book(conn)
    scored(conn, {"pod1": 40.0, "pod2": -10.0, "pod3": 0.0})
    assert F.reallocate(conn, RULE, epoch_id="E1", now=NOW, sol_usd=None, sol_at=None) == []          # FX unknown
    assert F.reallocate(conn, RULE, epoch_id="E1", now=NOW, sol_usd=FX, sol_at=NOW - 500) == []         # FX stale
    n = conn.execute("SELECT COUNT(*) FROM capital_flows WHERE kind LIKE 'transfer%'").fetchone()[0]
    assert n == 0


def test_too_few_closes_book_nothing(conn):
    book(conn)
    for i in range(4):
        close(conn, "pod1", i, 10.0)
    assert run(conn) == []


def test_discovery_epochs_and_wrong_rule_are_not_touched(conn):
    book(conn, kind="discovery")
    scored(conn, {"pod1": 40.0, "pod2": -10.0, "pod3": 0.0})
    assert run(conn) == []
    with pytest.raises(ValueError, match="rule_version_not_epoch_rule"):
        F.reallocate(conn, {**RULE, "version": "other"}, epoch_id="E1", now=NOW, sol_usd=FX, sol_at=NOW)


def test_period_key_follows_the_passed_clock(conn):
    assert F.period_key(0) == "1970-01-01" and F.period_key(NOW) != F.period_key(NOW - 86400)


@pytest.mark.parametrize("action", ["open", "reallocate"])
@pytest.mark.parametrize("bad_rule", ["changed", "missing", "bad_hash"])
def test_hooks_require_the_exact_frozen_epoch_rule(conn, action, bad_rule):
    book(conn)
    scored(conn, {"pod1": 40.0, "pod2": -10.0, "pod3": 0.0})
    rule = dict(RULE)
    if bad_rule == "changed":
        rule["step_pct"] = 20.0
    elif bad_rule == "missing":
        conn.execute("DELETE FROM epoch_pods_rules WHERE epoch_id='E1'")
    else:
        conn.execute("UPDATE epoch_pods_rules SET rule_sha256=? WHERE epoch_id='E1'", ("0" * 64,))
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM capital_flows").fetchone()[0]
    with pytest.raises(ValueError, match="pods_rule_not_epoch_rule"):
        if action == "open":
            F.open_pod_books(conn, rule, seats(), epoch_id="E1", clip_usd=25.0, total_usd=1500.0,
                             kind="evaluated", actor="crew", note="retry", now=NOW)
        else:
            F.reallocate(conn, rule, epoch_id="E1", now=NOW, sol_usd=FX, sol_at=NOW)
    assert conn.execute("SELECT COUNT(*) FROM capital_flows").fetchone()[0] == before


def test_replay_when_the_plan_changed_after_the_booking_does_not_conflict(conn):
    book(conn)
    scored(conn, {"pod1": 40.0, "pod2": -10.0, "pod3": 0.0})
    first = run(conn)
    close(conn, "pod3", 9, -500.0, at=NOW - 3600 * 5)    # a late-booked close changes pod3's score and the free cash after the transfer
    close(conn, "pod2", 9, 900.0, at=NOW - 3600 * 5)
    again = run(conn)
    assert again == [(first[0][0], "replayed")]


def test_a_rerun_with_a_different_pod_set_refuses_grow_and_shrink(conn):
    book(conn, n=2, total=1000.0)
    with pytest.raises(ValueError, match="pod_open_conflict"):
        book(conn, n=3, total=1500.0)                      # per-book initial stays 500, but a third book is a different allocation
    book(conn, n=3, total=1500.0, epoch="E2")
    with pytest.raises(ValueError, match="pod_open_conflict"):
        book(conn, n=2, total=1000.0, epoch="E2")          # a subset leaves a pod's book outside the call
    assert conn.execute("SELECT COUNT(*) FROM books WHERE epoch_id='E1'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM books WHERE epoch_id='E2'").fetchone()[0] == 3


def test_a_rerun_with_a_changed_total_refuses_and_changes_nothing(conn):
    book(conn, n=3, total=1500.0)
    flows = conn.execute("SELECT COUNT(*), SUM(usd) FROM capital_flows").fetchone()
    with pytest.raises(ValueError, match="pod_open_conflict"):
        book(conn, n=3, total=1800.0)
    assert conn.execute("SELECT COUNT(*), SUM(usd) FROM capital_flows").fetchone() == flows
