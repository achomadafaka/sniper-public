import json
import math
from pathlib import Path

import pytest

from sniper.office import pods as P

RULE_PATH = Path(__file__).resolve().parents[2] / "config" / "office" / "pods.json"


@pytest.fixture
def rule():
    return P.load_rule(RULE_PATH)


def _plan(rule, scores, capital=None, free=None, period="2026-10-29"):
    capital = capital or {p: 500.0 for p in scores}
    free = free or dict(capital)
    return P.plan_reallocation(rule, epoch_id="e1", period_key=period, scores=scores, capital=capital, free_cash=free)


def test_repo_rule_loads_and_bad_rules_refuse(rule, tmp_path):
    assert rule["version"] == "pods-rule-v1"
    for bad in ({}, {**rule, "step_pct": True}, {**rule, "step_pct": math.inf}, {**rule, "floor_pct": 60.0},
                {**rule, "score": {**rule["score"], "min_closed": 0}}, {**rule, "max_concurrent_total": 1.5}):
        p = tmp_path / "r.json"
        p.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            P.load_rule(p)


def test_best_takes_step_from_worst_with_a_period_effect_key(rule):
    out = _plan(rule, {"pod1": (40.0, 6), "pod2": (-10.0, 5), "pod3": (0.0, 9)})
    assert len(out) == 1
    t = out[0]
    assert (t["from_book"], t["to_book"], t["usd"]) == ("e1:pod2", "e1:pod1", 150.0)   # 10% of 1500
    assert t["epoch_id"] == "e1" and t["rule_version"] == "pods-rule-v1"
    again = _plan(rule, {"pod1": (40.0, 6), "pod2": (-10.0, 5), "pod3": (0.0, 9)})
    assert again == out
    assert _plan(rule, {"pod1": (40.0, 6), "pod2": (-10.0, 5), "pod3": (0.0, 9)}, period="2026-10-30")[0]["transfer_id"] != t["transfer_id"]


@pytest.mark.parametrize("scores", [
    {"pod1": (40.0, 6), "pod2": None},
    {"pod1": (40.0, 6), "pod2": (None, 6)},
    {"pod1": (40.0, 6), "pod2": (-5.0, 4)},            # below min_closed
    {"pod1": (40.0, 6), "pod2": (math.nan, 6)},
    {"pod1": (40.0, 6), "pod2": (True, 6)},
    {"pod1": (40.0, 6)},                               # one pod
    {"pod1": (0.0, 6), "pod2": (0.0, 9)},              # no spread
])
def test_unknown_or_flat_scores_move_nothing(rule, scores):
    assert _plan(rule, scores) == []


def test_missing_capital_or_free_cash_moves_nothing(rule):
    s = {"pod1": (40.0, 6), "pod2": (-10.0, 6)}
    assert _plan(rule, s, capital={"pod1": 500.0}) == []
    assert _plan(rule, s, free={"pod1": 500.0, "pod2": None}) == []
    # Delta: a pod present in capital but missing from scores is unknown, not ignored
    assert _plan(rule, s, capital={"pod1": 500.0, "pod2": 500.0, "pod3": 500.0}) == []
    assert _plan(rule, s, free={"pod1": 500.0, "pod2": 500.0, "pod3": 500.0}) == []


def test_floor_free_cash_and_minimum_bound_the_amount(rule):
    s = {"pod1": (40.0, 6), "pod2": (-10.0, 6)}
    # floor: worst keeps 10% of 1000 = 100, so at most 20 -> below min_transfer_usd 25 -> nothing
    assert _plan(rule, s, capital={"pod1": 880.0, "pod2": 120.0}) == []
    # free cash binds: open reserves stay covered
    assert _plan(rule, s, free={"pod1": 500.0, "pod2": 33.337})[0]["usd"] == 33.33


def test_ties_break_by_pod_name(rule):
    out = _plan(rule, {"pod2": (40.0, 6), "pod1": (40.0, 6), "pod3": (-1.0, 6), "pod4": (-1.0, 6)})
    assert (out[0]["to_book"], out[0]["from_book"]) == ("e1:pod1", "e1:pod3")


def test_initial_split_and_caps_follow_capital_never_clip(rule):
    cap = P.initial_capital(rule, ["pod2", "pod1", "pod3"], 1000.0)
    assert cap == {"pod1": 333.34, "pod2": 333.33, "pod3": 333.33} and round(sum(cap.values()), 2) == 1000.0
    caps = P.pod_caps(rule, {"pod1": 600.0, "pod2": 300.0, "pod3": 100.0})
    assert caps["pod1"] == {"max_concurrent": 3, "daily_loss_usd": 60.0, "model_share": 0.6, "pick_rank": 1}
    assert caps["pod3"]["max_concurrent"] == 1 and caps["pod3"]["pick_rank"] == 3
    assert "clip_usd" not in caps["pod1"]
    caps = P.pod_caps(rule, {"a": 700.0, "b": 100.0, "c": 100.0, "d": 100.0})       # Delta witness: was 4/1/1/1 = 7
    assert [caps[p]["max_concurrent"] for p in "abcd"] == [3, 1, 1, 1]
    with pytest.raises(ValueError):
        P.pod_caps(rule, {"pod1": 0.0, "pod2": None})


def test_pod_caps_never_exceed_the_rule_totals(rule):
    import random
    rnd = random.Random(18)
    for _ in range(2000):
        n = rnd.randint(1, 9)
        cap = {"p%d" % i: rnd.choice([0.0, 0.01, 1.0, 100.0, rnd.uniform(0, 1e4)]) for i in range(n)}
        cap["p0"] = max(cap["p0"], 1.0)
        caps = P.pod_caps(rule, cap)
        assert sum(c["max_concurrent"] for c in caps.values()) == rule["max_concurrent_total"]
        assert round(sum(c["daily_loss_usd"] for c in caps.values()), 2) == rule["daily_loss_usd_total"]
        if n > rule["max_concurrent_total"]:
            assert sorted(c["max_concurrent"] for c in caps.values())[0] == 0


def test_planned_transfer_books_in_the_real_ledger(rule, tmp_path):
    from sniper.office import ledger as L
    conn = L.open_ledger(tmp_path / "l.db")
    L.open_epoch(conn, epoch_id="e1", clip_usd=25.0, rule_version=rule["version"], actor="t", note="t", now=1.0)
    for pod, usd in P.initial_capital(rule, ["pod1", "pod2"], 1000.0).items():
        L.open_book(conn, book_id=P.book_id("e1", pod), epoch_id="e1", kind="evaluated", actor="t", note="t", now=2.0)
        L.fund(conn, book_id=P.book_id("e1", pod), usd=usd, kind="initial", actor="t", note="t", now=3.0)
    (t,) = _plan(rule, {"pod1": (40.0, 6), "pod2": (-10.0, 6)})
    kw = dict(t, actor="t", note="t", now=4.0, sol_usd=150.0, sol_at=4.0)
    assert L.transfer(conn, **kw) == "booked"
    assert L.transfer(conn, **kw) == "replayed"
    with pytest.raises(ValueError, match="transfer_id_conflict"):
        L.transfer(conn, **dict(kw, usd=t["usd"] - 1))
    assert L.capital(conn) == 1000.0



# --- the ledger binds pod caps to the frozen rule, not to caller policy (Delta gap, Alpha 2026-10-29) ---

@pytest.fixture
def quoted(monkeypatch):
    from sniper.office import ledger as L
    from .test_ledger import Quotes
    q = Quotes()
    monkeypatch.setattr(L, "_ps_buy", q.buy)
    monkeypatch.setattr(L, "_ps_sell", q.sell)
    monkeypatch.setattr(L, "_canonical_pool", lambda mint: "P")
    return L


def _sub(L, conn, book, n, **pol):
    from .test_ledger import MINT, NOW, FX, policy
    return L.submit(conn, policy(clip_usd=25.0, **pol), book_id=book, order_id="o%d" % n, network_id="solana",
                    mint=MINT + str(n), pool="P", decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)


def test_ledger_caps_come_from_the_frozen_rule_not_the_caller(rule, tmp_path, quoted):
    L = quoted
    conn = L.open_ledger(tmp_path / "l.db")
    L.open_epoch(conn, epoch_id="e1", clip_usd=25.0, rule_version=rule["version"], actor="t", note="t", now=1.0,
                 pods_rule=rule)
    for pod, usd in {"pod1": 900.0, "pod2": 100.0}.items():         # pod2 share 0.1 -> 1 slot, 10 USD daily loss
        L.open_book(conn, book_id=P.book_id("e1", pod), epoch_id="e1", kind="evaluated", actor="t", note="t", now=2.0)
        L.fund(conn, book_id=P.book_id("e1", pod), usd=usd, kind="initial", actor="t", note="t", now=3.0)
    assert _sub(L, conn, "e1:pod2", 1, max_concurrent=99, daily_loss_usd=1e9).state == "pending"
    r = _sub(L, conn, "e1:pod2", 2, max_concurrent=99, daily_loss_usd=1e9)
    assert (r.state, r.reason) == ("refused", "pod_max_concurrent")
    assert _sub(L, conn, "e1:pod1", 3, max_concurrent=1).state == "pending"
    assert _sub(L, conn, "e1:pod1", 4, max_concurrent=1).reason == "max_concurrent"   # the tighter cap still holds


def test_evaluated_book_without_a_frozen_pods_rule_refuses(rule, tmp_path, quoted):
    L = quoted
    conn = L.open_ledger(tmp_path / "l.db")
    L.open_epoch(conn, epoch_id="e1", clip_usd=25.0, rule_version="x", actor="t", note="t", now=1.0)
    L.open_book(conn, book_id="e1:pod1", epoch_id="e1", kind="evaluated", actor="t", note="t", now=2.0)
    L.fund(conn, book_id="e1:pod1", usd=1000.0, kind="initial", actor="t", note="t", now=3.0)
    assert _sub(L, conn, "e1:pod1", 1).reason == "pods_rule_unknown"
    for bad in (dict(rule, version="other"), dict(rule, step_pct=None)):
        with pytest.raises(ValueError):
            L.open_epoch(conn, epoch_id="e2", clip_usd=25.0, rule_version=rule["version"], actor="t", note="t",
                         now=1.0, pods_rule=bad)


def test_office_totals_bind_on_their_own(rule, tmp_path, quoted):
    L = quoted
    conn = L.open_ledger(tmp_path / "l.db")
    tight = dict(rule, max_concurrent_total=2)
    L.open_epoch(conn, epoch_id="e1", clip_usd=25.0, rule_version=rule["version"], actor="t", note="t", now=1.0,
                 pods_rule=tight)
    for pod in ("pod1", "pod2"):
        L.open_book(conn, book_id=P.book_id("e1", pod), epoch_id="e1", kind="evaluated", actor="t", note="t", now=2.0)
        L.fund(conn, book_id=P.book_id("e1", pod), usd=500.0, kind="initial", actor="t", note="t", now=3.0)
    assert _sub(L, conn, "e1:pod1", 1).state == "pending"
    assert _sub(L, conn, "e1:pod2", 2).state == "pending"
    # a third book opened mid-epoch gets a share of the same 2 slots, never a third one
    L.open_book(conn, book_id="e1:pod3", epoch_id="e1", kind="evaluated", actor="t", note="t", now=4.0)
    L.fund(conn, book_id="e1:pod3", usd=5000.0, kind="initial", actor="t", note="t", now=5.0)
    assert _sub(L, conn, "e1:pod3", 3).reason == "office_max_concurrent"


def test_apportion_is_exact_where_float_shares_overshoot():
    # Gamma note: float shares could floor above the units and make `left` negative. No float witness found
    # (2M random vectors); exact rationals remove the class, and this pins the invariant on hostile weights.
    for w in ({"a": 0.1, "b": 0.2, "c": 0.7}, {"a": 1e-300, "b": 1.0, "c": 1e300}, {"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}):
        rank = sorted(w)
        for units in range(0, 12):
            out = P._apportion(units, w, rank, 1, False)
            assert sum(out.values()) == units and min(out.values()) >= 0


@pytest.mark.parametrize("missing", ["scores", "capital", "free_cash"])
def test_missing_pod_in_any_input_moves_nothing(rule, missing):
    inputs = dict(scores={"a": (40.0, 6), "b": (-10.0, 6), "c": (0.0, 6)},
                  capital={"a": 500.0, "b": 500.0, "c": 500.0},
                  free_cash={"a": 500.0, "b": 500.0, "c": 500.0})
    del inputs[missing]["c"]
    assert P.plan_reallocation(rule, epoch_id="e1", period_key="2026-10-29", **inputs) == []


@pytest.mark.parametrize("extra", ["capital", "free_cash"])
def test_unscored_extra_pod_in_any_balance_input_moves_nothing(rule, extra):
    inputs = dict(scores={"a": (40.0, 6), "b": (-10.0, 6)},
                  capital={"a": 500.0, "b": 500.0}, free_cash={"a": 500.0, "b": 500.0})
    inputs[extra]["c"] = 500.0
    assert P.plan_reallocation(rule, epoch_id="e1", period_key="2026-10-29", **inputs) == []


def test_office_daily_loss_binds_across_pods(tmp_path, quoted):
    from .test_ledger import FX, MINT, NOW, manage, opened, policy, pods_rule, setup
    L = quoted
    conn = L.open_ledger(tmp_path / "l.db")
    L.open_epoch(conn, epoch_id="E1", clip_usd=30.0, rule_version="rule-v1", actor="t", note="t", now=NOW - 3,
                 pods_rule=pods_rule(daily_loss_usd_total=20.0))       # 10 USD per equal pod, 20 for the office
    setup(conn, "A", 500.0)
    setup(conn, "B", 500.0)
    pol = policy()
    q = L._ps_buy.__self__                                              # the quoted fixture's stub
    opened(conn, q, pol, "o1")
    q.value[MINT] = 1                                                   # near-total loss on A's 30 USD clip
    L.request_exit(conn, order_id="o1", reason="test", requested_by="Trader", now=NOW + 20)
    manage(conn, pol, NOW + 22)

    def sub(book, oid, mint):
        return L.submit(conn, pol, book_id=book, order_id=oid, network_id="solana", mint=mint, pool="P",
                        decision_bundle_hash="b" * 64, now=NOW + 40, sol_usd=FX, sol_at=NOW + 40)
    assert sub("B", "b1", MINT + "2").reason == "office_daily_loss_halt"   # B lost nothing; the office total binds
