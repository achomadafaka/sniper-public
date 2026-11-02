"""Office ledger (plan d1d90258c Task 3, as amended by Beta round 1 and accepted by Alpha): an isolated paper book with a
frozen policy, PumpSwap fill math only, typed equity. The quote functions are stubbed: the PumpSwap math itself is
memecoin_desk_ps_paper's and has its own tests."""
import ast
import json
import dataclasses
import sqlite3
from pathlib import Path

import pytest

from sniper.office import ledger as L

ROOT = Path(__file__).resolve().parents[2]
MINT = "Mint1111111111111111111111111111111111111pump"
NOW = 1_800_000_000.0
FX = 150.0                                      # USD per SOL
CLOCK = {"offset_s": 0.0, "sampled_at": NOW - 10}  # a zero-offset sample; tests of the domain use others


def policy(**over):
    base = dict(enabled=True, chains=("solana",), clip_usd=30.0, rail_bounds={}, max_concurrent=3,
                daily_loss_usd=100.0, fill_model_version="office-fill-v1", sl_pct=30.0, tp_pct=100.0,
                trail_pct=0.0, catastrophe_pct=60.0, max_hold_s=3600.0, cap_min_headroom_pct=0.0,
                fx_max_age_s=120.0, entry_timeout_s=60.0)
    base.update(over)
    return L.OfficeBookPolicy(**base)


def pods_rule(version="rule-v1", **over):
    """A frozen pods rule loose enough that only the book policy binds (tests/office/test_pods.py tests the pod caps)."""
    return dict({"version": version, "score": {"metric": "realized_net_usd", "window_periods": 7, "min_closed": 5},
                 "step_pct": 10.0, "floor_pct": 10.0, "min_transfer_usd": 25.0, "max_concurrent_total": 999,
                 "daily_loss_usd_total": 1e9}, **over)


def setup(conn, book="A", usd=1000.0, kind="evaluated", epoch="E1", clip=30.0, rule="rule-v1"):
    """An epoch (frozen clip + reallocation rule), one book in it, and its audited initial capital."""
    if not conn.execute("SELECT 1 FROM ledger_epochs WHERE epoch_id=?", (epoch,)).fetchone():
        L.open_epoch(conn, epoch_id=epoch, clip_usd=clip, rule_version=rule, actor="operator", note="test", now=NOW - 2,
                     pods_rule=pods_rule(rule))
    L.open_book(conn, book_id=book, epoch_id=epoch, kind=kind, actor="operator", note="test", now=NOW - 2)
    L.fund(conn, book_id=book, usd=usd, kind="initial", actor="operator", note="test capital", now=NOW - 1)


class Quotes:
    """Stub for memecoin_desk_ps_paper.buy/sell: tokens per lamport fixed, sell value settable per mint."""

    def __init__(self):
        self.value = {}                         # mint -> lamports for the full position, or a reason string
        self.calls = []

    def buy(self, desk_dir, mint, spend, at, *, evaluated_at, integrity=False, **kw):
        self.calls.append(("buy", mint, spend, at, integrity))
        if self.value.get(mint) == "no_pool":
            return None, "no_swaps_before_cut"
        return {"tokens_atomic": str(spend * 1000), "spend_lamports": str(spend), "cash_left_atomic": "0",
                "pool": "P", "proof": {"slot": 1}}, None

    def sell(self, desk_dir, mint, qty, at, *, evaluated_at, integrity=False, **kw):
        self.calls.append(("sell", mint, qty, at, integrity))
        v = self.value.get(mint)
        if isinstance(v, str):
            return None, v
        return {"lamports": str(v if v is not None else int(qty) // 1000), "pool": "P", "proof": {"slot": 2}}, None


@pytest.fixture
def book(tmp_path, monkeypatch):
    q = Quotes()
    monkeypatch.setattr(L, "_ps_buy", q.buy)
    monkeypatch.setattr(L, "_ps_sell", q.sell)
    monkeypatch.setattr(L, "_canonical_pool", lambda mint: "P")
    conn = L.open_ledger(tmp_path / "office" / "ledger.db")
    setup(conn, "A", 1000.0)
    yield conn, q, tmp_path
    conn.close()


def manage(conn, pol, now, fx=FX):
    # FX observed at NOW (or 100 s back): causal for every fill and exit time the tests use, and fresh enough
    return L.manage(conn, pol, desk_dir=Path("/nonexistent"), clock_sample=CLOCK, now=now, sol_usd=fx, sol_at=max(NOW, now - 100))


def opened(conn, q, pol, order_id="o1", mint=MINT, at=NOW):
    r = L.submit(conn, pol, book_id="A", order_id=order_id, network_id="solana", mint=mint, pool="P",
                 decision_bundle_hash="b" * 64, now=at, sol_usd=FX, sol_at=at)
    assert r.state == "pending", r
    manage(conn, pol, at + L.ENTRY_DELAY_S + 1)
    return conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()


# ---- review focus 1 and isolation

@pytest.mark.parametrize("mode", ["real", "paper", None])
def test_host_mode_real_or_unset_never_creates_funded_intent(book, monkeypatch, mode):
    conn, q, tmp = book
    if mode is None:
        monkeypatch.delenv("MEMECOIN_DESK_EXECUTION", raising=False)
    else:
        monkeypatch.setenv("MEMECOIN_DESK_EXECUTION", mode)
    funded = tmp / "sniper_tracker.db"
    f = sqlite3.connect(funded)
    f.execute("CREATE TABLE memecoin_manual_intents(id INTEGER PRIMARY KEY, mint TEXT)")
    f.commit()
    f.close()
    before = sorted(p.name for p in tmp.rglob("*") if p.is_file())
    opened(conn, q, policy())
    assert sqlite3.connect(funded).execute("SELECT COUNT(*) FROM memecoin_manual_intents").fetchone()[0] == 0
    after = sorted(p.name for p in tmp.rglob("*") if p.is_file())
    assert set(after) - set(before) <= {"ledger.db-wal", "ledger.db-shm"}          # nothing new outside the ledger


def test_office_isolated_from_desk_globals(book, monkeypatch):
    import sniper.settings.memecoin as cfg
    from sniper.services import memecoin_desk_paper as DP
    conn, q, tmp = book
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CLIP_USD", 999.0, raising=False)
    row = opened(conn, q, policy())
    assert row["state"] == "open" and float(row["spend_usd"]) == pytest.approx(30.0, rel=1e-6)
    desk = sqlite3.connect(":memory:")
    DP.ensure(desk)
    req = {"payload": {"network_id": "solana", "mint": MINT, "size_usd": 25.0, "rails": {}}}
    before = DP.book_refusal(desk, request=req, now_iso="2027-01-15T00:00:00+00:00")
    L.submit(conn, policy(clip_usd=1.0, max_concurrent=99), book_id="A", order_id="o2", network_id="solana", mint=MINT,
             pool="P", decision_bundle_hash="c" * 64, now=NOW + 5, sol_usd=FX, sol_at=NOW + 5)
    assert DP.book_refusal(desk, request=req, now_iso="2027-01-15T00:00:00+00:00") == before


def test_no_funded_imports():
    banned_names = {"execution_mode", "dispatch_outbox"}
    banned_modules = ("memecoin_manual_intents", "memecoin_real_runner", "memecoin_funded_policy", "sniper.execution",
                      "solana_real_seams", "sniper.settings.memecoin")
    for path in (ROOT / "sniper" / "office").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) or isinstance(node, ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                assert name not in banned_names, (path, name)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] + ([node.module] if getattr(node, "module", None) else [])
                for m in mods:
                    assert not any(b in m for b in banned_modules), (path, m)


# ---- submit

def test_submit_idempotent_on_order_id(book):
    conn, q, _ = book
    for _ in range(2):
        L.submit(conn, policy(), book_id="A", order_id="o1", network_id="solana", mint=MINT, pool="P",
                 decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


@pytest.mark.parametrize("over,net,reason", [
    (dict(enabled=False), "solana", "office_book_disabled"),
    ({}, "8453", "chain_not_supported"),
    (dict(chains=("solana", "8453")), "8453", "chain_not_supported"),     # PumpSwap math only, whatever the policy says
])
def test_submit_refusals_are_typed(book, over, net, reason):
    conn, q, _ = book
    r = L.submit(conn, policy(**over), book_id="A", order_id="o1", network_id=net, mint=MINT, pool="P",
                 decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    assert r.state == "refused" and r.reason == reason


def test_max_concurrent_and_daily_loss_refuse(book):
    conn, q, _ = book
    pol = policy(max_concurrent=1)
    opened(conn, q, pol, "o1")
    r = L.submit(conn, pol, book_id="A", order_id="o2", network_id="solana", mint=MINT + "2", pool="P",
                 decision_bundle_hash="b" * 64, now=NOW + 10, sol_usd=FX, sol_at=NOW + 10)
    assert (r.state, r.reason) == ("refused", "max_concurrent")
    q.value[MINT] = 1                                                          # a near-total loss on o1
    L.request_exit(conn, order_id="o1", reason="test", requested_by="Trader", now=NOW + 20)
    manage(conn, pol, NOW + 22)
    pol2 = policy(max_concurrent=5, daily_loss_usd=20.0)
    r = L.submit(conn, pol2, book_id="A", order_id="o3", network_id="solana", mint=MINT + "3", pool="P",
                 decision_bundle_hash="b" * 64, now=NOW + 40, sol_usd=FX, sol_at=NOW + 40)
    assert (r.state, r.reason) == ("refused", "daily_loss_halt")


# ---- fills

def test_missing_pool_is_censored_not_filled(book):
    conn, q, _ = book
    q.value[MINT] = "no_pool"
    L.submit(conn, policy(), book_id="A", order_id="o1", network_id="solana", mint=MINT, pool="P",
             decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    manage(conn, policy(), NOW + 5)
    row = conn.execute("SELECT * FROM orders WHERE order_id='o1'").fetchone()
    assert row["state"] == "pending"                                           # waits inside the entry timeout
    manage(conn, policy(), NOW + 61 + L.ENTRY_DELAY_S)
    row = conn.execute("SELECT * FROM orders WHERE order_id='o1'").fetchone()
    assert row["state"] == "censored" and row["reason"] == "no_swaps_before_cut" and row["tokens_atomic"] is None


def test_unknown_fx_never_fills_and_is_censored_at_timeout(book):
    conn, q, _ = book
    L.submit(conn, policy(), book_id="A", order_id="o1", network_id="solana", mint=MINT, pool="P",
             decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    L.manage(conn, policy(), desk_dir=Path("/x"), clock_sample=CLOCK, now=NOW + 5, sol_usd=None, sol_at=None)
    assert conn.execute("SELECT state FROM orders").fetchone()[0] == "pending" and q.calls == []
    L.manage(conn, policy(), desk_dir=Path("/x"), clock_sample=CLOCK, now=NOW + 70, sol_usd=FX, sol_at=NOW - 500)     # stale FX
    row = conn.execute("SELECT * FROM orders").fetchone()
    assert row["state"] == "censored" and row["reason"] == "fx_unknown"


def test_fill_uses_integrity_quotes_at_the_delayed_time_and_records_the_model(book):
    conn, q, _ = book
    row = opened(conn, q, policy())
    assert q.calls[0][0] == "buy" and q.calls[0][3] == NOW + L.ENTRY_DELAY_S and q.calls[0][4] is True
    assert row["fill_model_version"] == "office-fill-v1" and row["policy_hash"]
    assert int(row["spend_lamports"]) == int(30.0 / FX * 1e9)


# ---- exits

def test_request_exit_is_executed_by_manage_after_the_delay(book):
    conn, q, _ = book
    opened(conn, q, policy())
    r = L.request_exit(conn, order_id="o1", reason="thesis_broken", requested_by="Trader", now=NOW + 10)
    assert r.ok
    manage(conn, policy(), NOW + 10 + L.EXIT_DELAY_S / 2)
    assert conn.execute("SELECT state FROM orders").fetchone()[0] == "open"
    manage(conn, policy(), NOW + 12)
    row = conn.execute("SELECT * FROM orders").fetchone()
    assert row["state"] == "closed" and row["exit_reason"] == "thesis_broken" and row["exit_requested_by"] == "Trader"
    assert q.calls[-1][0] == "sell" and q.calls[-1][3] == NOW + 10 + L.EXIT_DELAY_S


@pytest.mark.parametrize("order_id,why", [("nope", "unknown_order"), ("o1", "not_open")])
def test_request_exit_on_closed_or_unknown_order_is_refused(book, order_id, why):
    conn, q, _ = book
    opened(conn, q, policy())
    L.request_exit(conn, order_id="o1", reason="x", requested_by="Trader", now=NOW + 10)
    manage(conn, policy(), NOW + 12)
    r = L.request_exit(conn, order_id=order_id, reason="x", requested_by="Trader", now=NOW + 20)
    assert not r.ok and r.reason == why


@pytest.mark.parametrize("value_ratio,reason", [(0.65, "stop"), (2.5, "tp"), (0.3, "catastrophe")])
def test_rails_fire_from_the_frozen_policy(book, value_ratio, reason):
    conn, q, _ = book
    row = opened(conn, q, policy())
    q.value[MINT] = int(int(row["spend_lamports"]) * value_ratio)
    manage(conn, policy(), NOW + 20)
    manage(conn, policy(), NOW + 25)
    row = conn.execute("SELECT * FROM orders").fetchone()
    assert row["state"] == "closed" and row["exit_reason"] == reason


def test_max_hold_closes(book):
    conn, q, _ = book
    opened(conn, q, policy(max_hold_s=100.0))
    manage(conn, policy(max_hold_s=100.0), NOW + 150)
    manage(conn, policy(max_hold_s=100.0), NOW + 155)
    assert conn.execute("SELECT exit_reason FROM orders").fetchone()[0] == "max_hold"


def test_policy_is_frozen_at_submit(book):
    conn, q, _ = book
    row = opened(conn, q, policy(sl_pct=30.0))
    q.value[MINT] = int(int(row["spend_lamports"]) * 0.8)                      # -20 %
    manage(conn, policy(sl_pct=10.0), NOW + 20)                               # a later, tighter policy is ignored
    assert conn.execute("SELECT state FROM orders").fetchone()[0] == "open"


# ---- marks and equity (review focus 5)

def test_mark_source_and_age_recorded(book):
    conn, q, _ = book
    opened(conn, q, policy())
    manage(conn, policy(), NOW + 20)
    m = conn.execute("SELECT * FROM marks ORDER BY at DESC LIMIT 1").fetchone()
    assert m["source"] == "ps_paper.sell" and m["at"] == NOW + 20 and m["lamports"] is not None


def test_unpriced_bag_makes_equity_partial(book):
    conn, q, _ = book
    opened(conn, q, policy(), "o1", MINT)
    opened(conn, q, policy(), "o2", MINT + "2")
    q.value[MINT + "2"] = "pool_state_stale"
    manage(conn, policy(), NOW + 20)
    e = L.equity(conn, epoch_id="E1", now=NOW + 21, mark_max_age_s=30.0, sol_usd=FX, sol_at=NOW + 20)
    assert e.state == "partial" and e.unpriced_count == 1 and e.known_liq_lower > 0
    assert e.fill_model_version == "office-fill-v1"


def test_all_unpriced_or_unknown_fx_is_unknown_never_zero(book):
    conn, q, _ = book
    opened(conn, q, policy())
    q.value[MINT] = "pool_state_stale"
    manage(conn, policy(), NOW + 20)
    e = L.equity(conn, epoch_id="E1", now=NOW + 21, mark_max_age_s=30.0, sol_usd=FX, sol_at=NOW + 20)
    assert e.state == "unknown" and e.known_liq_lower is None
    e = L.equity(conn, epoch_id="E1", now=NOW + 21, mark_max_age_s=30.0, sol_usd=None, sol_at=None)
    assert e.state == "unknown"


def test_stale_mark_counts_as_unpriced(book):
    conn, q, _ = book
    opened(conn, q, policy())
    manage(conn, policy(), NOW + 20)
    e = L.equity(conn, epoch_id="E1", now=NOW + 200, mark_max_age_s=30.0, sol_usd=FX, sol_at=NOW + 200)
    assert e.unpriced_count == 1 and e.state == "unknown"


def test_closed_book_equity_is_known_and_net_of_costs(book):
    conn, q, _ = book
    row = opened(conn, q, policy())
    q.value[MINT] = int(row["spend_lamports"])                                 # flat on price
    L.request_exit(conn, order_id="o1", reason="x", requested_by="Trader", now=NOW + 10)
    manage(conn, policy(), NOW + 12)
    e = L.equity(conn, epoch_id="E1", now=NOW + 13, mark_max_age_s=30.0, sol_usd=FX, sol_at=NOW + 13)
    assert e.state == "known" and e.unpriced_count == 0
    assert e.cash < 1000.0                                                    # flat price still loses the modeled fees


def test_policy_dataclass_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy().clip_usd = 1.0


# ---- Zeta / Gamma binding review of the plan (relayed by Alpha)

@pytest.mark.parametrize("change", [dict(decision_bundle_hash="z" * 64), dict(mint=MINT + "x"), dict(pool="Q")])
def test_same_order_id_with_a_different_payload_is_rejected(book, change):
    conn, q, _ = book
    args = dict(order_id="o1", network_id="solana", mint=MINT, pool="P", decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    assert L.submit(conn, policy(), book_id="A", **args).state == "pending"
    r = L.submit(conn, policy(), book_id="A", **{**args, **change, "now": NOW + 1})
    assert (r.state, r.reason) == ("refused", "order_id_conflict")
    r = L.submit(conn, policy(clip_usd=10.0), book_id="A", **{**args, "now": NOW + 1})       # same order, other frozen policy
    assert (r.state, r.reason) == ("refused", "order_id_conflict")
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_a_size_cut_is_refused_in_the_evaluated_book(book):
    conn, q, _ = book
    r = L.submit(conn, policy(), book_id="A", order_id="o1", network_id="solana", mint=MINT, pool="P",
                 decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW, size_usd=15.0)
    assert (r.state, r.reason) == ("refused", "size_cut_not_allowed")
    r = L.submit(conn, policy(), book_id="A", order_id="o2", network_id="solana", mint=MINT, pool="P",
                 decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW, size_usd=30.0)
    assert r.state == "pending"


def test_each_fill_and_exit_debits_its_cost_bound_once_with_evidence(book):
    conn, q, _ = book
    opened(conn, q, policy())
    L.request_exit(conn, order_id="o1", reason="x", requested_by="Trader", now=NOW + 10)
    manage(conn, policy(), NOW + 12)
    manage(conn, policy(), NOW + 13)                                           # a second pass debits nothing
    rows = conn.execute("SELECT kind, cost_lamports, cost_evidence FROM cashflows WHERE order_id='o1' ORDER BY id").fetchall()
    assert [r["kind"] for r in rows] == ["entry", "exit"]
    assert all(int(r["cost_lamports"]) > 0 and r["cost_evidence"] for r in rows)


def test_book_cash_is_conserved_and_a_crash_mid_exit_rolls_back(book, monkeypatch):
    conn, q, _ = book
    for i in range(3):
        opened(conn, q, policy(), "o%d" % i, MINT + str(i))
    L.request_exit(conn, order_id="o0", reason="x", requested_by="Trader", now=NOW + 10)
    manage(conn, policy(), NOW + 12)
    real = L._record_cashflow

    def boom(*a, **k):
        real(*a, **k)
        raise RuntimeError("crash after the cashflow insert")
    L.request_exit(conn, order_id="o1", reason="x", requested_by="Trader", now=NOW + 20)
    monkeypatch.setattr(L, "_record_cashflow", boom)
    with pytest.raises(RuntimeError):
        manage(conn, policy(), NOW + 22)
    monkeypatch.setattr(L, "_record_cashflow", real)
    assert conn.execute("SELECT state FROM orders WHERE order_id='o1'").fetchone()[0] == "open"
    assert conn.execute("SELECT COUNT(*) FROM cashflows WHERE order_id='o1'").fetchone()[0] == 1   # entry only
    manage(conn, policy(), NOW + 23)
    assert conn.execute("SELECT state FROM orders WHERE order_id='o1'").fetchone()[0] == "closed"
    e = L.equity(conn, epoch_id="E1", now=NOW + 24, mark_max_age_s=60.0, sol_usd=FX, sol_at=NOW + 24)
    flows = conn.execute("SELECT COALESCE(SUM(usd), 0) FROM cashflows").fetchone()[0]
    per_order = sum((r["exit_usd"] or 0.0) - r["spend_usd"] - r["costs_usd"]
                    for r in conn.execute("SELECT * FROM orders WHERE spend_usd IS NOT NULL"))
    assert e.cash == pytest.approx(1000.0 + flows) == pytest.approx(1000.0 + per_order)



# ---- Delta + Zeta HOLD on 94f443712 (witnesses adopted)

def test_future_mark_cannot_value_earlier_equity(book):
    conn, q, _ = book
    opened(conn, q, policy())
    conn.execute("INSERT INTO marks(order_id, at, lamports, source) VALUES('o1', ?, '200000000', 'ps_paper.sell')",
                 (NOW + 1000,))
    e = L.equity(conn, epoch_id="E1", now=NOW + 5, mark_max_age_s=60.0, sol_usd=FX, sol_at=NOW + 5)
    assert e.state == "unknown" and e.known_liq_lower is None


def test_a_late_watcher_never_books_a_past_exit_and_re_arms_with_a_fresh_clock(book):
    conn, q, _ = book
    opened(conn, q, policy())
    L.request_exit(conn, order_id="o1", reason="x", requested_by="Trader", now=NOW + 10)
    L.manage(conn, policy(), desk_dir=Path("/x"), clock_sample=CLOCK, now=NOW + 1000, sol_usd=300.0, sol_at=NOW + 1000)
    row = conn.execute("SELECT * FROM orders").fetchone()
    assert row["state"] == "open" and row["due_at"] == NOW + 1000 + L.EXIT_DELAY_S
    o = conn.execute("SELECT * FROM outages").fetchone()
    assert (o["kind"], o["from_at"], o["to_at"]) == ("exit", NOW + 10 + L.EXIT_DELAY_S, NOW + 1000)
    L.manage(conn, policy(), desk_dir=Path("/x"), clock_sample=CLOCK, now=NOW + 1002, sol_usd=300.0, sol_at=NOW + 1000)
    row = conn.execute("SELECT * FROM orders").fetchone()
    assert row["state"] == "closed" and row["closed_at"] == NOW + 1001 and q.calls[-1][3] == NOW + 1001


def test_exit_fx_must_be_known_at_the_exit_time_itself(book):
    conn, q, _ = book
    opened(conn, q, policy())
    L.request_exit(conn, order_id="o1", reason="x", requested_by="Trader", now=NOW + 10)
    L.manage(conn, policy(), desk_dir=Path("/x"), clock_sample=CLOCK, now=NOW + 12, sol_usd=300.0, sol_at=NOW + 12)   # FX after T+11
    row = conn.execute("SELECT * FROM orders").fetchone()
    assert row["state"] == "open" and row["due_at"] == NOW + 11 and row["proceeds_lamports"] is None   # the cut waits for causal FX


def test_a_late_entry_is_censored_as_an_outage(book):
    conn, q, _ = book
    L.submit(conn, policy(), book_id="A", order_id="o1", network_id="solana", mint=MINT, pool="P",
             decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    manage(conn, policy(), NOW + 30)
    row = conn.execute("SELECT * FROM orders").fetchone()
    assert (row["state"], row["reason"]) == ("censored", "watcher_outage") and q.calls == []


def test_pool_binding_is_enforced_at_submit_and_on_every_quote(book, monkeypatch):
    conn, q, _ = book
    r = L.submit(conn, policy(), book_id="A", order_id="o1", network_id="solana", mint=MINT, pool="OTHER",
                 decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    assert (r.state, r.reason) == ("refused", "pool_not_canonical")
    real = q.buy
    monkeypatch.setattr(L, "_ps_buy", lambda *a, **k: ({**real(*a, **k)[0], "pool": "OTHER"}, None))
    L.submit(conn, policy(), book_id="A", order_id="o2", network_id="solana", mint=MINT, pool="P",
             decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    manage(conn, policy(), NOW + 2)
    row = conn.execute("SELECT * FROM orders WHERE order_id='o2'").fetchone()
    assert (row["state"], row["reason"]) == ("censored", "quote_binding_mismatch")


@pytest.mark.parametrize("over,why", [
    (dict(rail_bounds={"sl_pct": [10, 40]}, sl_pct=90.0), "rail_out_of_bounds:sl_pct"),
    (dict(cap_min_headroom_pct=5.0), "unsupported:cap_min_headroom_pct"),
    (dict(clip_usd=float("nan")), "policy_invalid:clip_usd"),
    (dict(max_concurrent=True), "policy_invalid:max_concurrent"),
    (dict(rail_bounds={"bogus": [0, 1]}), "policy_invalid:rail_bounds"),
])
def test_frozen_guards_are_enforced_or_refused_never_ignored(book, over, why):
    conn, q, _ = book
    r = L.submit(conn, policy(**over), book_id="A", order_id="o1", network_id="solana", mint=MINT, pool="P",
                 decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    assert (r.state, r.reason) == ("refused", why)


def test_liquidation_value_is_net_of_the_modeled_exit_cost_once(book):
    conn, q, _ = book
    row = opened(conn, q, policy())
    manage(conn, policy(), NOW + 20)
    e = L.equity(conn, epoch_id="E1", now=NOW + 21, mark_max_age_s=30.0, sol_usd=FX, sol_at=NOW + 20)
    exit_cost = round(L.EXEC_COST_SOL["exit"] * 1e9)
    assert e.known_liq_lower == pytest.approx((int(row["spend_lamports"]) - exit_cost) / 1e9 * FX)
    flow = conn.execute("SELECT cost_basis FROM cashflows").fetchone()[0]
    assert flow == "measured_mean"


def test_capital_is_explicit_and_bounds_the_book(tmp_path, monkeypatch):
    q = Quotes()
    monkeypatch.setattr(L, "_ps_buy", q.buy)
    monkeypatch.setattr(L, "_canonical_pool", lambda mint: "P")
    conn = L.open_ledger(tmp_path / "ledger.db")
    args = dict(network_id="solana", mint=MINT, pool="P", decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    assert L.submit(conn, policy(), book_id="A", order_id="o1", **args).reason == "book_unknown"
    L.open_epoch(conn, epoch_id="E1", clip_usd=30.0, rule_version="rule-v1", actor="operator", note="t", now=NOW,
                 pods_rule=pods_rule())
    L.open_book(conn, book_id="A", epoch_id="E1", kind="evaluated", actor="operator", note="t", now=NOW)
    assert L.submit(conn, policy(), book_id="A", order_id="o1", **args).reason == "no_capital"
    L.fund(conn, book_id="A", usd=31.0, kind="initial", actor="operator", note="t", now=NOW)
    with pytest.raises(ValueError):
        L.fund(conn, book_id="A", usd=31.0, kind="initial", actor="operator", note="t", now=NOW)
    assert L.submit(conn, policy(), book_id="A", order_id="o1", **args).state == "pending"
    other = dict(args, mint=MINT + "2")
    assert L.submit(conn, policy(), book_id="A", order_id="o2", **other).reason == "insufficient_capital"
    L.fund(conn, book_id="A", usd=100.0, kind="deposit", actor="operator", note="t", now=NOW)
    assert L.submit(conn, policy(), book_id="A", order_id="o2", **other).state == "pending"



# ---- Zeta HOLD a58513e74: capital reserves each pending order's own clip + costs; no clamp

def test_pending_reserve_uses_each_orders_own_frozen_clip_and_costs(tmp_path, monkeypatch):
    q = Quotes()
    monkeypatch.setattr(L, "_ps_buy", q.buy)
    monkeypatch.setattr(L, "_canonical_pool", lambda mint: "P")
    conn = L.open_ledger(tmp_path / "ledger.db")
    setup(conn, "A", 110.0, kind="discovery")
    a = dict(network_id="solana", mint=MINT, pool="P", decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    assert L.submit(conn, policy(clip_usd=100.0, max_concurrent=9), book_id="A", order_id="big", **a).state == "pending"
    assert L.submit(conn, policy(clip_usd=10.0, max_concurrent=9), book_id="A", order_id="s1",
                    **dict(a, mint=MINT + "2")).reason == "insufficient_capital"


def test_exact_capital_cannot_fill_into_a_negative_cash(tmp_path, monkeypatch):
    q = Quotes()
    monkeypatch.setattr(L, "_ps_buy", q.buy)
    monkeypatch.setattr(L, "_canonical_pool", lambda mint: "P")
    conn = L.open_ledger(tmp_path / "ledger.db")
    setup(conn, "A", 30.0)
    a = dict(network_id="solana", mint=MINT, pool="P", decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    assert L.submit(conn, policy(), book_id="A", order_id="o1", **a).reason == "insufficient_capital"


def test_a_fill_rechecks_capital_at_its_own_fx(book):
    conn, q, _ = book
    L.submit(conn, policy(clip_usd=30.0), book_id="A", order_id="o1", network_id="solana", mint=MINT, pool="P",
             decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    conn.execute("INSERT INTO capital_flows(book_id, at, kind, usd, actor, note) VALUES('A', ?, 'withdrawal', -990, 'operator', 't')", (NOW,))
    manage(conn, policy(), NOW + 2)
    row = conn.execute("SELECT state, reason FROM orders").fetchone()
    assert (row["state"], row["reason"]) == ("censored", "insufficient_capital")


def test_a_zero_value_bag_is_a_negative_liability_not_zero(book):
    conn, q, _ = book
    opened(conn, q, policy())
    q.value[MINT] = 0
    manage(conn, policy(), NOW + 20)
    e = L.equity(conn, epoch_id="E1", now=NOW + 21, mark_max_age_s=30.0, sol_usd=FX, sol_at=NOW + 20)
    assert e.known_liq_lower == pytest.approx(-round(L.EXEC_COST_SOL["exit"] * 1e9) / 1e9 * FX)


def test_two_writers_cannot_both_pass_the_capital_check(tmp_path, monkeypatch):
    """submit is one BEGIN IMMEDIATE transaction: a second connection waits instead of reading a stale book."""
    monkeypatch.setattr(L, "_canonical_pool", lambda mint: "P")
    path = tmp_path / "ledger.db"
    a, b = L.open_ledger(path), L.open_ledger(path)
    setup(a, "A", 50.0)
    args = dict(network_id="solana", mint=MINT, pool="P", decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    a.execute("BEGIN IMMEDIATE")
    b.execute("PRAGMA busy_timeout=0")
    with pytest.raises(sqlite3.OperationalError):
        L.submit(b, policy(), book_id="A", order_id="o2", **args)                         # locked out while a holds the book
    a.execute("ROLLBACK")
    assert L.submit(a, policy(), book_id="A", order_id="o1", **args).state == "pending"
    assert L.submit(b, policy(), book_id="A", order_id="o2", **dict(args, mint=MINT + "2")).reason == "insufficient_capital"


def test_a_stale_selected_row_cannot_refinalize_after_another_writer(book, monkeypatch):
    """Delta: manage re-reads each order under its write lock. Another writer closes o1 between manage's listing and
    its turn on o1: manage must not exit it twice."""
    conn, q, tmp = book
    opened(conn, q, policy())
    other = L.open_ledger(tmp / "office" / "ledger.db")
    L.request_exit(conn, order_id="o1", reason="x", requested_by="Trader", now=NOW + 10)
    real = L._write
    first = []

    def racing(c):
        if not first:                                # the other writer finishes the exit right before our lock
            first.append(1)
            L.manage(other, policy(), desk_dir=Path("/x"), clock_sample=CLOCK, now=NOW + 12, sol_usd=FX, sol_at=NOW)
        return real(c)
    monkeypatch.setattr(L, "_write", racing)
    manage(conn, policy(), NOW + 12)
    assert conn.execute("SELECT COUNT(*) FROM cashflows WHERE order_id='o1' AND kind='exit'").fetchone()[0] == 1
    assert [c[0] for c in q.calls].count("sell") == 1


def test_request_exit_inside_a_caller_transaction_uses_a_savepoint(book):
    conn, q, _ = book
    opened(conn, q, policy())
    conn.execute("BEGIN IMMEDIATE")
    assert L.request_exit(conn, order_id="o1", reason="x", requested_by="Trader", now=NOW + 10).ok
    conn.execute("ROLLBACK")                         # the caller's rollback undoes it
    assert conn.execute("SELECT exit_requested_at FROM orders").fetchone()[0] is None


# ---- Task 18 pods: one paper book per pod (book_id), own capital and limits, audited reallocation

def _two_books(tmp_path, monkeypatch, a_usd=100.0, b_usd=100.0):
    q = Quotes()
    monkeypatch.setattr(L, "_ps_buy", q.buy)
    monkeypatch.setattr(L, "_ps_sell", q.sell)
    monkeypatch.setattr(L, "_canonical_pool", lambda mint: "P")
    conn = L.open_ledger(tmp_path / "ledger.db")
    setup(conn, "A", a_usd)
    setup(conn, "B", b_usd)
    return conn, q


def sub(conn, book, oid, pol=None, mint=MINT):
    return L.submit(conn, pol or policy(), book_id=book, order_id=oid, network_id="solana", mint=mint, pool="P",
                    decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)


def test_books_have_their_own_capital_and_concurrency(tmp_path, monkeypatch):
    conn, q = _two_books(tmp_path, monkeypatch, a_usd=40.0)
    assert sub(conn, "A", "a1").state == "pending"
    assert sub(conn, "A", "a2", mint=MINT + "2").reason == "insufficient_capital"   # A cannot borrow B's capital
    pol = policy(max_concurrent=1)
    assert sub(conn, "B", "b1", pol, MINT + "3").state == "pending"
    assert sub(conn, "B", "b2", pol, MINT + "4").reason == "max_concurrent"
    assert sub(conn, "C", "c1", mint=MINT + "5").reason == "book_unknown"


def test_a_book_daily_loss_halts_only_that_book(tmp_path, monkeypatch):
    conn, q = _two_books(tmp_path, monkeypatch)
    pol = policy(daily_loss_usd=20.0, max_concurrent=5)
    sub(conn, "A", "a1", pol)
    manage(conn, pol, NOW + 2)
    q.value[MINT] = 1
    L.request_exit(conn, order_id="a1", reason="x", requested_by="Trader", now=NOW + 10)
    manage(conn, pol, NOW + 12)
    assert sub(conn, "A", "a2", pol, MINT + "2").reason == "daily_loss_halt"
    assert sub(conn, "B", "b1", pol, MINT + "2").state == "pending"


def test_reallocation_is_paired_audited_and_conserves_the_office_total(tmp_path, monkeypatch):
    conn, q = _two_books(tmp_path, monkeypatch)
    sub(conn, "A", "a1")
    before = L.capital(conn)
    L.transfer(conn, transfer_id="t1", from_book="A", to_book="B", usd=60.0, epoch_id="E1", rule_version="rule-v1", actor="Head",
               note="rule v1 epoch 2", now=NOW + 1, sol_usd=FX, sol_at=NOW)
    assert L.capital(conn) == before and L.capital(conn, "A") == 40.0 and L.capital(conn, "B") == 160.0
    rows = conn.execute("SELECT book_id, kind, usd, actor, note FROM capital_flows WHERE kind LIKE 'transfer%' "
                        "ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("A", "transfer_out", -60.0, "Head", "rule v1 epoch 2"),
                                        ("B", "transfer_in", 60.0, "Head", "rule v1 epoch 2")]
    with pytest.raises(ValueError, match="insufficient_capital"):              # A's pending clip stays covered
        L.transfer(conn, transfer_id="t2", from_book="A", to_book="B", usd=20.0, epoch_id="E1", rule_version="rule-v1", actor="Head",
                   note="x", now=NOW + 2, sol_usd=FX, sol_at=NOW)
    for bad, why in ((dict(to_book="A"), "transfer_invalid"), (dict(usd=-1.0), "transfer_invalid"),
                     (dict(to_book="Z"), "book_unknown"), (dict(epoch_id=""), "transfer_invalid"),
                     (dict(rule_version=""), "transfer_invalid"),
                     (dict(rule_version="rule-v2"), "rule_version_not_epoch_rule"),
                     (dict(epoch_id="E9"), "transfer_across_epoch_or_kind")):
        with pytest.raises(ValueError, match=why):
            L.transfer(conn, **{**dict(transfer_id="tx", from_book="A", to_book="B", usd=1.0, epoch_id="E1", rule_version="rule-v1",
                                       actor="Head", note="x", now=NOW + 3, sol_usd=FX, sol_at=NOW), **bad})
    assert L.external_flows(conn) == 200.0 and L.external_flows(conn, "A") == 100.0


def test_office_equity_is_the_sum_of_the_book_equities(tmp_path, monkeypatch):
    conn, q = _two_books(tmp_path, monkeypatch)
    sub(conn, "A", "a1")
    sub(conn, "B", "b1", mint=MINT + "2")
    manage(conn, policy(), NOW + 2)
    manage(conn, policy(), NOW + 20)
    kw = dict(now=NOW + 21, mark_max_age_s=30.0, sol_usd=FX, sol_at=NOW + 20)
    a, b, whole = L.equity(conn, book_id="A", **kw), L.equity(conn, book_id="B", **kw), L.equity(conn, epoch_id="E1", **kw)
    assert whole.cash == pytest.approx(a.cash + b.cash)
    assert whole.known_liq_lower == pytest.approx(a.known_liq_lower + b.known_liq_lower)
    assert (a.state, b.state, whole.state) == ("known", "known", "known")


def test_same_order_id_in_another_book_is_a_conflict(tmp_path, monkeypatch):
    conn, q = _two_books(tmp_path, monkeypatch)
    sub(conn, "A", "o1")
    assert (sub(conn, "B", "o1").state, sub(conn, "B", "o1").reason) == ("refused", "order_id_conflict")



# ---- Gamma HOLD 6d18d31ac (prereg Am3)

def test_one_entry_per_coin_across_the_evaluated_books_of_an_epoch(tmp_path, monkeypatch):
    conn, q = _two_books(tmp_path, monkeypatch)
    assert sub(conn, "A", "a1").state == "pending"
    r = sub(conn, "B", "b1")                                                  # same coin, another pod, another order id
    assert (r.state, r.reason) == ("refused", "coin_already_entered")


def test_discovery_books_never_enter_the_evaluated_equity(tmp_path, monkeypatch):
    conn, q = _two_books(tmp_path, monkeypatch)
    setup(conn, "D", 100.0, kind="discovery")
    kw = dict(now=NOW + 21, mark_max_age_s=30.0, sol_usd=FX, sol_at=NOW + 20)
    before = L.equity(conn, epoch_id="E1", **kw)
    assert sub(conn, "D", "d1").state == "pending"                            # discovery may take the same coin later
    manage(conn, policy(), NOW + 2)
    q.value[MINT] = 1
    L.request_exit(conn, order_id="d1", reason="x", requested_by="Trader", now=NOW + 10)
    manage(conn, policy(), NOW + 12)
    assert L.equity(conn, epoch_id="E1", **kw).cash == before.cash             # the discovery loss is not in the verdict
    assert L.equity(conn, epoch_id="E1", kind="discovery", **kw).cash < 100.0
    assert sub(conn, "A", "a1").state == "pending"                            # evaluated books still free to take it
    with pytest.raises(ValueError, match="transfer_across_epoch_or_kind"):
        L.transfer(conn, transfer_id="t1", from_book="D", to_book="A", usd=1.0, epoch_id="E1", rule_version="rule-v1", actor="Head",
                   note="x", now=NOW + 13, sol_usd=FX, sol_at=NOW + 13)
    with pytest.raises(ValueError, match="equity_scope_required"):
        L.equity(conn, **kw)


def test_evaluated_books_trade_only_the_epoch_clip(tmp_path, monkeypatch):
    conn, q = _two_books(tmp_path, monkeypatch)
    r = sub(conn, "A", "a1", policy(clip_usd=20.0))
    assert (r.state, r.reason) == ("refused", "clip_not_epoch_clip")


def test_a_transfer_row_without_its_epoch_and_rule_cannot_exist(tmp_path, monkeypatch):
    conn, q = _two_books(tmp_path, monkeypatch)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO capital_flows(book_id, at, kind, usd, actor, note) VALUES('A', 1, 'transfer_in', 5, 'x', 'y')")


# ---- Delta T7 clock domain: corrected UTC in the ledger, host only at the PumpSwap helper call

def test_quotes_are_read_on_the_host_clock_and_the_sample_is_in_the_proof(book):
    conn, q, _ = book
    clock = {"offset_s": 100.0, "sampled_at": NOW + 90}                        # host = corrected + 100
    L.submit(conn, policy(), book_id="A", order_id="o1", network_id="solana", mint=MINT, pool="P",
             decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    L.manage(conn, policy(), desk_dir=Path("/x"), now=NOW + 2, clock_sample=clock, sol_usd=FX, sol_at=NOW)
    kind, mint, amount, at, integrity = q.calls[-1]
    assert (kind, at) == ("buy", NOW + L.ENTRY_DELAY_S + 100.0)
    row = conn.execute("SELECT * FROM orders").fetchone()
    assert row["filled_at"] == NOW + L.ENTRY_DELAY_S                            # the ledger keeps corrected UTC
    proof = json.loads(row["fill_proof"])["clock"]
    assert proof == {"offset_s": 100.0, "sampled_at": NOW + 90, "host_at": NOW + L.ENTRY_DELAY_S + 100.0,
                     "host_evaluated_at": NOW + 102.0}


@pytest.mark.parametrize("clock", [None, {}, {"offset_s": None, "sampled_at": NOW}, {"offset_s": 1.0, "sampled_at": NOW + 50},
                                   {"offset_s": 0.0, "sampled_at": NOW - 4000}])
def test_an_unknown_or_stale_offset_reads_no_tape(book, clock):
    conn, q, _ = book
    opened(conn, q, policy())
    calls = len(q.calls)
    L.request_exit(conn, order_id="o1", reason="x", requested_by="Trader", now=NOW + 10)
    L.manage(conn, policy(), desk_dir=Path("/x"), now=NOW + 12, clock_sample=clock, sol_usd=FX, sol_at=NOW)
    assert len(q.calls) == calls                                                # no quote at all
    row = conn.execute("SELECT state, due_at FROM orders").fetchone()
    assert row["state"] == "open" and row["due_at"] == NOW + 12 + L.EXIT_DELAY_S
    assert conn.execute("SELECT reason FROM marks ORDER BY id DESC LIMIT 1").fetchone()[0] == "clock_unknown"


def test_same_order_id_replay_returns_the_first_result_not_coin_already_entered(book):
    conn, q, _ = book
    a = dict(book_id="A", order_id="o1", network_id="solana", mint=MINT, pool="P", decision_bundle_hash="b" * 64,
             now=NOW, sol_usd=FX, sol_at=NOW)
    first = L.submit(conn, policy(), **a)
    assert L.submit(conn, policy(), **a) == first


def test_the_coin_rule_has_a_database_backstop(book):
    conn, q, _ = book
    opened(conn, q, policy())
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO orders(order_id, book_id, epoch_id, book_kind, network_id, mint, pool, decision_bundle_hash, "
                     "policy_json, policy_hash, fill_model_version, state, requested_at) "
                     "VALUES('x','A','E1','evaluated','solana',?,'P','h','{}','h','v','pending',1)", (MINT,))


def test_a_read_only_handle_serves_equity_and_refuses_writes(book):
    conn, q, tmp = book
    opened(conn, q, policy())
    manage(conn, policy(), NOW + 20)
    ro = L.open_readonly(tmp / "office" / "ledger.db")
    kw = dict(now=NOW + 21, mark_max_age_s=30.0, sol_usd=FX, sol_at=NOW + 20)
    assert L.equity(ro, epoch_id="E1", **kw) == L.equity(conn, epoch_id="E1", **kw)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM orders")


def test_a_transfer_replay_is_a_noop_and_a_reused_id_is_a_conflict(tmp_path, monkeypatch):
    """Delta: transfer_id is the stable effect key for the rule runner's retries."""
    conn, q = _two_books(tmp_path, monkeypatch)
    kw = dict(transfer_id="r1:e1:1", from_book="A", to_book="B", usd=10.0, epoch_id="E1", rule_version="rule-v1",
              actor="Head", note="x", now=NOW + 1, sol_usd=FX, sol_at=NOW)
    assert L.transfer(conn, **kw) == "booked"
    assert L.transfer(conn, **dict(kw, now=NOW + 2)) == "replayed"
    assert L.capital(conn, "A") == 90.0 and L.capital(conn, "B") == 110.0
    for change in (dict(usd=11.0), dict(to_book="A", from_book="B"), dict(rule_version="rule-v2")):
        with pytest.raises(ValueError, match="transfer_id_conflict"):
            L.transfer(conn, **{**kw, **change})
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO capital_flows(book_id, at, kind, usd, actor, note, epoch_id, rule_version) "
                     "VALUES('A', 1, 'transfer_in', 5, 'x', 'y', 'E1', 'rule-v1')")      # no transfer_id


def test_lookup_transfer_is_read_only_and_checks_both_legs(tmp_path, monkeypatch):
    """Codex T6 reconcile: missing / booked / partial / conflict, never a write."""
    conn, q = _two_books(tmp_path, monkeypatch)
    want = dict(from_book="A", to_book="B", usd=10.0, epoch_id="E1", rule_version="rule-v1")
    assert L.lookup_transfer(conn, "r1")["state"] == "missing"
    L.transfer(conn, transfer_id="r1", actor="Head", note="x", now=NOW + 1, sol_usd=FX, sol_at=NOW, **want)
    before = conn.total_changes
    assert L.lookup_transfer(conn, "r1", expect=want) == {"state": "booked", "legs": L.lookup_transfer(conn, "r1")["legs"],
                                                          "transfer": want}
    assert L.lookup_transfer(conn, "r1", expect=dict(want, usd=11.0))["state"] == "conflict"
    assert conn.total_changes == before
    conn.execute("DELETE FROM capital_flows WHERE transfer_id='r1' AND kind='transfer_out'")
    assert L.lookup_transfer(conn, "r1", expect=want)["state"] == "partial"
    with pytest.raises(ValueError, match="transfer_id_conflict"):          # a half-booked id is never re-booked
        L.transfer(conn, transfer_id="r1", actor="Head", note="x", now=NOW + 2, sol_usd=FX, sol_at=NOW, **want)
    ro = L.open_readonly(tmp_path / "ledger.db")
    assert L.lookup_transfer(ro, "r1")["state"] == "partial"


@pytest.mark.parametrize('leg', ['entry', 'exit', 'mark'])
@pytest.mark.parametrize('offset', [0., 100.])
def test_frontier_retry_keeps_requested_cut_and_counts_refusal(book, monkeypatch, leg, offset):
    conn,q,tmp=book;pol=policy()
    if leg=='entry':
        L.submit(conn,pol,book_id='A',order_id='o1',network_id='solana',mint=MINT,pool='P',
                 decision_bundle_hash='b'*64,now=NOW,sol_usd=FX,sol_at=NOW)
        at=NOW+L.ENTRY_DELAY_S
    else:
        opened(conn,q,pol)
        at=NOW+10
        if leg=='exit':
            conn.execute("UPDATE orders SET exit_requested_at=?,exit_reason='manual',due_at=? WHERE order_id='o1'",(at-1,at));conn.commit()
    calls=[]
    def quote(desk,mint,amount,cut,**kw):
        calls.append(cut)
        if len(calls)==1:return None,'frontier_not_committed'
        return (dict(pool='P',tokens_atomic='123',spend_lamports='200000000') if leg=='entry'
                else dict(pool='P',lamports='200000000')),None
    monkeypatch.setattr(L,'_ps_buy' if leg=='entry' else '_ps_sell',quote)
    result=L.manage(conn,pol,desk_dir=tmp,clock_sample=dict(offset_s=offset,sampled_at=NOW+offset-10),now=at,sol_usd=FX,sol_at=NOW,mark_min_interval_s=15)
    assert result.frontier_not_committed==1
    L.manage(conn,pol,desk_dir=tmp,clock_sample=dict(offset_s=offset,sampled_at=NOW+offset-10),now=at+1,sol_usd=FX,sol_at=NOW,mark_min_interval_s=15)
    assert calls==[at+offset,at+offset]


def test_expired_frontier_mark_stays_unpriced_without_quote_or_rail(book, monkeypatch):
    conn,q,tmp=book;pol=policy();opened(conn,q,pol)
    calls=[]
    def quote(*args,**kwargs):calls.append(args[3]);return None,'frontier_not_committed'
    monkeypatch.setattr(L,'_ps_sell',quote)
    manage(conn,pol,NOW+10)
    result=manage(conn,pol,NOW+10+L.TICK_GRACE_S+1)
    assert result.unpriced==1 and calls==[NOW+10]
    assert conn.execute('SELECT reason FROM marks ORDER BY id DESC LIMIT 1').fetchone()[0]=='frontier_retry_expired'
    assert conn.execute("SELECT exit_requested_at FROM orders WHERE order_id='o1'").fetchone()[0] is None


def test_refused_sell_tip_mark_replaces_known_equity_with_unknown(book):
    conn,q,tmp=book;pol=policy();opened(conn,q,pol)
    manage(conn,pol,NOW+10)
    assert L.equity(conn,now=NOW+10,mark_max_age_s=30,sol_usd=FX,sol_at=NOW,book_id='A').state=='known'
    q.value[MINT]='pool_tip_unproven'
    manage(conn,pol,NOW+11)
    equity=L.equity(conn,now=NOW+11,mark_max_age_s=30,sol_usd=FX,sol_at=NOW,book_id='A')
    assert equity.state=='unknown' and equity.unpriced_count==1
    assert conn.execute('SELECT lamports,reason FROM marks ORDER BY id DESC LIMIT 1').fetchone()[:]==(None,'pool_tip_unproven')


def test_refused_sell_tip_exit_rearms_without_crediting_profit(book):
    conn,q,tmp=book;pol=policy();opened(conn,q,pol)
    L.request_exit(conn,order_id='o1',reason='horizon',requested_by='Trader',now=NOW+10)
    at=conn.execute("SELECT due_at FROM orders WHERE order_id='o1'").fetchone()[0]
    q.value[MINT]='pool_tip_unproven'
    result=manage(conn,pol,at)
    row=conn.execute("SELECT * FROM orders WHERE order_id='o1'").fetchone()
    assert result.closed==0 and result.unpriced==1 and row['state']=='open'
    assert row['due_at']==at+L.EXIT_DELAY_S and row['proceeds_lamports'] is None and row['exit_proof'] is None


def test_new_mark_keeps_binding_and_full_quote_proof(book, monkeypatch):
    conn,q,tmp=book;pol=policy();opened(conn,q,pol)
    proof=dict(map_source='curve_derived',binding_check='unchecked',cut=dict(slot=42,at=NOW+10))
    def sell(*a,**kw):return dict(pool='P',lamports='200000000',binding_kind='curve_derived_unchecked',proof=proof),None
    monkeypatch.setattr(L,'_ps_sell',sell)
    manage(conn,pol,NOW+10)
    row=conn.execute('SELECT * FROM marks ORDER BY id DESC LIMIT 1').fetchone()
    assert row['binding_kind']=='curve_derived_unchecked'
    saved=json.loads(row['quote_proof'])
    assert saved['proof']==proof and saved['clock']['host_at']==NOW+10 and saved['pool']=='P'


def test_mark_trace_migration_keeps_old_rows_unknown(tmp_path):
    path=tmp_path/'legacy.db';conn=sqlite3.connect(path)
    conn.execute('CREATE TABLE marks(id INTEGER PRIMARY KEY,order_id TEXT NOT NULL,at REAL NOT NULL,lamports TEXT,source TEXT NOT NULL,reason TEXT)')
    conn.execute("INSERT INTO marks VALUES(1,'old',1,'2','legacy',NULL)");conn.commit();conn.close()
    conn=L.open_ledger(path)
    try:
        row=conn.execute('SELECT * FROM marks').fetchone()
        assert row['lamports']=='2' and row['binding_kind'] is None and row['quote_proof'] is None
    finally:conn.close()
    L.open_ledger(path).close()


@pytest.mark.parametrize('binding,through,offset,valid', [
    (None,20,0,False),('create_pool',9,0,False),('create_pool',10,0,True),
    ('create_pool',10,100,False),('create_pool',110,100,True)])
def test_strict_equity_requires_binding_and_committed_quote(book,binding,through,offset,valid):
    conn,q,tmp=book;pol=policy();opened(conn,q,pol);manage(conn,pol,NOW+10)
    quote=dict(binding_kind=binding,pool='P',lamports='200000000',proof=dict(committed_through=NOW+through),
               clock=dict(offset_s=offset,host_at=NOW+10+offset))
    conn.execute('UPDATE marks SET binding_kind=?,quote_proof=?',(binding,json.dumps(quote)))
    args=dict(now=NOW+10,mark_max_age_s=30,sol_usd=FX,sol_at=NOW,book_id='A')
    assert L.equity(conn,**args).state=='known'
    assert L.equity(conn,require_proven=True,**args).state==('known' if valid else 'unknown')


@pytest.mark.parametrize('raw',[None,'{','{}','null','[]'])
def test_strict_equity_missing_or_malformed_proof_is_unpriced(book,raw):
    conn,q,tmp=book;pol=policy();opened(conn,q,pol);manage(conn,pol,NOW+10)
    conn.execute("UPDATE marks SET binding_kind='create_pool',quote_proof=?",(raw,))
    assert L.equity(conn,now=NOW+10,mark_max_age_s=30,sol_usd=FX,sol_at=NOW,book_id='A',require_proven=True).state=='unknown'


def test_strict_equity_keeps_proven_bag_and_censors_legacy_bag(book):
    conn,q,tmp=book;pol=policy();opened(conn,q,pol)
    opened(conn,q,pol,order_id='o2',mint='Other111111111111111111111111111111111pump')
    manage(conn,pol,NOW+10)
    quote=dict(binding_kind='create_pool',pool='P',lamports='200000000',proof=dict(committed_through=NOW+10),clock=dict(offset_s=0,host_at=NOW+10))
    conn.execute("UPDATE marks SET binding_kind='create_pool',quote_proof=? WHERE order_id='o1'",(json.dumps(quote),))
    args=dict(now=NOW+10,mark_max_age_s=30,sol_usd=FX,sol_at=NOW,book_id='A')
    assert L.equity(conn,**args).state=='known'
    strict=L.equity(conn,require_proven=True,**args)
    assert strict.state=='partial' and strict.unpriced_count==1 and strict.known_liq_lower is not None


@pytest.mark.parametrize('delta,expected',[(1e-9,True),(1e-3,False)])
def test_proven_mark_clock_tolerance(delta,expected):
    mark=dict(at=10.,lamports='1',binding_kind='create_pool',quote_proof=json.dumps(dict(binding_kind='create_pool',pool='P',lamports='1',
        clock=dict(offset_s=0.,host_at=10.+delta),proof=dict(committed_through=20.))))
    assert L._proven_mark(mark,'P') is expected


def test_equity_reports_only_priced_binding_counts(book):
    conn,q,tmp=book;pol=policy();opened(conn,q,pol);manage(conn,pol,NOW+10)
    quote=dict(binding_kind='curve_derived_unchecked',pool='P',lamports='200000000',proof=dict(committed_through=NOW+10),clock=dict(offset_s=0.,host_at=NOW+10))
    conn.execute("UPDATE marks SET binding_kind='curve_derived_unchecked',quote_proof=?",(json.dumps(quote),))
    result=L.equity(conn,now=NOW+10,mark_max_age_s=30,sol_usd=FX,sol_at=NOW,book_id='A',require_proven=True)
    assert result.binding_kind_counts=={'curve_derived_unchecked':1}
    conn.execute('UPDATE marks SET binding_kind=NULL')
    assert L.equity(conn,now=NOW+10,mark_max_age_s=30,sol_usd=FX,sol_at=NOW,book_id='A',require_proven=True).binding_kind_counts=={}


@pytest.mark.parametrize('changed', ['lamports','pool'])
def test_strict_mark_must_match_valued_amount_and_order_pool(changed):
    quote=dict(binding_kind='create_pool',pool='P',lamports='1',clock=dict(offset_s=0.,host_at=10.),proof=dict(committed_through=10.))
    quote[changed]='different'
    mark=dict(at=10.,lamports='1',binding_kind='create_pool',quote_proof=json.dumps(quote))
    assert L._proven_mark(mark,'P') is False


# ---- equity is causal at its as-of (Zeta reporting cut, Alpha 2026-10-29)

def test_equity_never_counts_capital_cash_or_inventory_after_its_as_of(book):
    conn, q, tmp = book
    base = L.equity(conn, book_id="A", now=NOW + 30, mark_max_age_s=60, sol_usd=FX, sol_at=NOW)
    conn.execute("INSERT INTO capital_flows(book_id,at,kind,usd,actor,note) VALUES('A',?,'deposit',200,'t','future')",
                 (NOW + 60,))
    later = L.equity(conn, book_id="A", now=NOW + 30, mark_max_age_s=60, sol_usd=FX, sol_at=NOW)
    assert later.cash == base.cash                                  # a deposit dated after the as-of is not cash yet
    assert L.equity(conn, book_id="A", now=NOW + 61, mark_max_age_s=60, sol_usd=FX, sol_at=NOW + 61).cash == base.cash + 200


def test_equity_values_an_order_by_its_state_at_the_as_of_not_now(book):
    conn, q, tmp = book
    pol = policy()
    row = opened(conn, q, pol)
    filled = row["filled_at"]
    before = L.equity(conn, book_id="A", now=filled - 1, mark_max_age_s=60, sol_usd=FX, sol_at=filled - 1)
    assert before.state == "known" and before.unpriced_count == 0   # not yet filled at that as-of: no inventory, no spend
    at_fill = L.equity(conn, book_id="A", now=filled, mark_max_age_s=60, sol_usd=FX, sol_at=filled)
    assert at_fill.cash < before.cash                                # the entry cashflow counts once it happened
    conn.execute("UPDATE orders SET state='closed', closed_at=? WHERE order_id='o1'", (filled + 500,))
    mid = L.equity(conn, book_id="A", now=filled + 10, mark_max_age_s=60, sol_usd=FX, sol_at=filled + 10)
    assert mid.unpriced_count + (1 if mid.known_liq_lower else 0) >= 1   # still open inventory at that as-of


def test_commit_lag_past_the_grace_retries_a_later_cut_and_is_no_outage(book, monkeypatch):
    conn, q, _ = book
    pol = policy()
    L.submit(conn, pol, book_id="A", order_id="o1", network_id="solana", mint=MINT, pool="P",
             decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    due = NOW + L.ENTRY_DELAY_S
    cuts = []
    def lagging(desk, mint, amount, cut, **kw):
        cuts.append(cut)
        return None, "frontier_not_committed"
    monkeypatch.setattr(L, "_ps_buy", lagging)
    manage(conn, pol, due + .1)
    late = due + L.TICK_GRACE_S + .3
    manage(conn, pol, late)   # the first order's shape: lag 5.33 s
    row = conn.execute("SELECT * FROM orders").fetchone()
    assert (row["state"], row["reason"]) == ("pending", "frontier_retry_expired") and row["due_at"] == late + L.ENTRY_DELAY_S
    assert conn.execute("SELECT count(*) FROM outages").fetchone()[0] == 0
    monkeypatch.setattr(L, "_ps_buy", q.buy)
    manage(conn, pol, row["due_at"] + .1)
    row = conn.execute("SELECT * FROM orders").fetchone()
    assert row["state"] == "open" and row["filled_at"] == late + L.ENTRY_DELAY_S
    assert row["frontier_retries"] == 1 and row["reason"] is None   # survives the fill, unlike the reason


def _lagged_retry(conn, pol, monkeypatch):
    L.submit(conn, pol, book_id="A", order_id="o1", network_id="solana", mint=MINT, pool="P",
             decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    due = NOW + L.ENTRY_DELAY_S
    monkeypatch.setattr(L, "_ps_buy", lambda *a, **k: (None, "frontier_not_committed"))
    manage(conn, pol, due + .1)
    late = due + L.TICK_GRACE_S + .3
    manage(conn, pol, late)
    return late + L.ENTRY_DELAY_S


def test_a_retry_past_the_entry_timeout_censors_with_its_own_reason(book, monkeypatch):
    conn, q, _ = book
    pol = policy()
    _lagged_retry(conn, pol, monkeypatch)
    manage(conn, pol, NOW + pol.entry_timeout_s + 1)
    row = conn.execute("SELECT * FROM orders").fetchone()
    assert (row["state"], row["reason"]) == ("censored", "frontier_retry_expired") and row["filled_at"] is None
    assert conn.execute("SELECT count(*) FROM outages").fetchone()[0] == 0


def test_a_watcher_absent_at_the_retry_due_time_still_records_an_outage(book, monkeypatch):
    conn, q, _ = book
    pol = policy()
    due = _lagged_retry(conn, pol, monkeypatch)
    manage(conn, pol, due + L.TICK_GRACE_S + 1)
    row = conn.execute("SELECT * FROM orders").fetchone()
    assert (row["state"], row["reason"]) == ("censored", "watcher_outage")
    assert conn.execute("SELECT count(*) FROM outages").fetchone()[0] == 1


def test_a_retry_never_outlives_the_sheet(book, monkeypatch):
    conn, q, _ = book
    pol = policy()
    L.submit(conn, pol, book_id="A", order_id="o1", network_id="solana", mint=MINT, pool="P",
             decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    conn.execute("UPDATE orders SET sheet_expires_at=?", (NOW + 3,))
    due = NOW + L.ENTRY_DELAY_S
    monkeypatch.setattr(L, "_ps_buy", lambda *a, **k: (None, "frontier_not_committed"))
    manage(conn, pol, due + .1)
    manage(conn, pol, due + L.TICK_GRACE_S + .3)
    row = conn.execute("SELECT * FROM orders").fetchone()
    assert (row["state"], row["reason"]) == ("censored", "sheet_expired") and row["filled_at"] is None


def test_no_path_fills_past_the_sheet_expiry(book):
    conn, q, _ = book
    pol = policy()
    L.submit(conn, pol, book_id="A", order_id="o1", network_id="solana", mint=MINT, pool="P",
             decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    conn.execute("UPDATE orders SET sheet_expires_at=?", (NOW + L.ENTRY_DELAY_S - .5,))
    manage(conn, pol, NOW + L.ENTRY_DELAY_S + .1)
    row = conn.execute("SELECT * FROM orders").fetchone()
    assert (row["state"], row["reason"]) == ("censored", "sheet_expired") and q.calls == []


def test_submit_stores_the_expiry_in_its_own_transaction(book):
    conn, q, _ = book
    L.submit(conn, policy(), book_id="A", order_id="o1", network_id="solana", mint=MINT, pool="P",
             decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW, metadata=dict(sheet_expires_at=NOW+50.))
    assert conn.execute("SELECT sheet_expires_at FROM orders").fetchone()[0] == NOW+50.


def _committed_book(book, monkeypatch, cut):
    conn, q, _ = book
    pol = policy()
    opened(conn, q, pol)
    monkeypatch.setattr(L, "_committed_cut", lambda desk, clock: cut[0])
    return conn, q, pol


def test_a_coin_falling_during_tape_lag_fires_at_the_first_committed_cut(book, monkeypatch):
    t = NOW + 100
    cut = [t - 4]
    conn, q, pol = _committed_book(book, monkeypatch, cut)
    seen = []
    monkeypatch.setattr(L, "_ps_sell", lambda desk, mint, qty, at, **k: (seen.append(at), (dict(pool="P", lamports="1"), None))[1])
    manage(conn, pol, t)
    row = conn.execute("SELECT * FROM orders").fetchone()
    mark = conn.execute("SELECT at, lamports, mark_age_s FROM marks ORDER BY id DESC LIMIT 1").fetchone()
    assert mark["at"] == t - 4 and mark["mark_age_s"] == 4 and mark["lamports"] == "1"
    assert row["exit_requested_at"] == t and row["due_at"] == t + L.EXIT_DELAY_S and row["exit_reason"] == "catastrophe"


def test_a_cut_past_the_bound_prices_nothing_and_a_run_of_nulls_is_one_row(book, monkeypatch):
    t = NOW + 100
    cut = [t - L.MARK_MAX_AGE_S - 1]
    conn, q, pol = _committed_book(book, monkeypatch, cut)
    monkeypatch.setattr(L, "_ps_sell", lambda *a, **k: 1 / 0)
    before = conn.execute("SELECT count(*) FROM marks").fetchone()[0]
    manage(conn, pol, t);manage(conn, pol, t + 1)
    rows = conn.execute("SELECT reason, null_n, at, null_last_at FROM marks ORDER BY id DESC LIMIT 1").fetchone()
    assert conn.execute("SELECT count(*) FROM marks").fetchone()[0] == before + 1
    assert tuple(rows) == ("frontier_stale", 2, t, t + 1)
    assert conn.execute("SELECT exit_requested_at FROM orders").fetchone()[0] is None


def test_max_hold_still_fires_on_a_null_mark(book, monkeypatch):
    t = NOW + 1
    cut = [0.]
    conn, q, pol = _committed_book(book, monkeypatch, cut)
    late = NOW + pol.max_hold_s + 10
    manage(conn, pol, late)
    assert conn.execute("SELECT exit_reason FROM orders").fetchone()[0] == "max_hold"


def test_exit_commit_lag_past_the_grace_retries_later_and_is_no_outage(book, monkeypatch):
    monkeypatch.setenv("OFFICE_EXIT_ESTIMATE_FILL", "0")
    conn, q, pol = _committed_book(book, monkeypatch, [None])
    at = NOW + 50
    conn.execute("UPDATE orders SET exit_requested_at=?, exit_reason='manual', due_at=? WHERE order_id='o1'", (at - 1, at))
    monkeypatch.setattr(L, "_ps_sell", lambda *a, **k: (None, "frontier_not_committed"))
    for tick in range(0, int(L.EXIT_CUT_HOLD_S) + 2):
        manage(conn, pol, at + tick)
    late = at + int(L.EXIT_CUT_HOLD_S) + 1
    assert conn.execute("SELECT count(*) FROM outages").fetchone()[0] == 0
    assert conn.execute("SELECT due_at FROM orders").fetchone()[0] == late + L.EXIT_DELAY_S
    assert conn.execute("SELECT exit_frontier_retries FROM orders").fetchone()[0] == 1


def test_max_hold_fires_on_the_unreadable_heartbeat_path_too(book, monkeypatch):
    conn, q, pol = _committed_book(book, monkeypatch, [None])
    filled = conn.execute("SELECT filled_at FROM orders").fetchone()[0]
    monkeypatch.setattr(L, "_ps_sell", lambda *a, **k: (None, "frontier_not_committed"))
    manage(conn, pol, filled + pol.max_hold_s - 1)
    assert conn.execute("SELECT exit_reason FROM orders").fetchone()[0] is None
    manage(conn, pol, filled + pol.max_hold_s + 9)
    assert conn.execute("SELECT exit_reason FROM orders").fetchone()[0] == "max_hold"


def test_marks_name_their_path(book, monkeypatch):
    conn, q, pol = _committed_book(book, monkeypatch, [NOW + 90])
    manage(conn, pol, NOW + 100)
    assert conn.execute("SELECT mark_path FROM marks ORDER BY id DESC LIMIT 1").fetchone()[0] == "heartbeat"


def test_a_watcher_absent_at_an_exit_retry_due_time_still_writes_the_outage(book, monkeypatch):
    conn, q, pol = _committed_book(book, monkeypatch, [None])
    at = NOW + 50
    conn.execute("UPDATE orders SET exit_requested_at=?, exit_reason='manual', due_at=? WHERE order_id='o1'", (at - 1, at))
    monkeypatch.setattr(L, "_ps_sell", lambda *a, **k: (None, "frontier_not_committed"))
    manage(conn, pol, at + .1)
    manage(conn, pol, at + .1 + L.TICK_GRACE_S + 1)
    assert conn.execute("SELECT count(*) FROM outages WHERE kind='exit'").fetchone()[0] == 1


def test_a_strict_refusal_falls_to_a_labelled_estimate_and_its_rail_fires(book, monkeypatch):
    t = NOW + 100
    conn, q, pol = _committed_book(book, monkeypatch, [t - 2])
    monkeypatch.setattr(L, "_ps_sell", lambda *a, **k: (None, "pool_tip_unproven"))
    monkeypatch.setattr(L, "_ps_estimate", lambda *a, **k: (dict(pool="P", lamports="1", binding_kind="carried", proof=dict(carried_age_s=30.)), None))
    manage(conn, pol, t)
    mark = conn.execute("SELECT lamports, binding_kind, quote_proof FROM marks ORDER BY id DESC LIMIT 1").fetchone()
    assert mark["lamports"] == "1" and mark["binding_kind"] == "carried" and "pool_tip_unproven" in mark["quote_proof"]
    assert conn.execute("SELECT exit_reason FROM orders").fetchone()[0] == "catastrophe"


def test_no_estimate_stays_null_and_a_frontier_miss_never_estimates(book, monkeypatch):
    t = NOW + 100
    conn, q, pol = _committed_book(book, monkeypatch, [t - 2])
    monkeypatch.setattr(L, "_ps_sell", lambda *a, **k: (None, "ps_capture_unproven"))
    monkeypatch.setattr(L, "_ps_estimate", lambda *a, **k: (None, "estimate_tip_stale"))
    manage(conn, pol, t)
    assert conn.execute("SELECT reason, lamports FROM marks ORDER BY id DESC LIMIT 1").fetchone()[0] == "ps_capture_unproven;estimate_tip_stale"
    assert conn.execute("SELECT exit_requested_at FROM orders").fetchone()[0] is None


def test_an_exit_fill_never_uses_an_estimate(book, monkeypatch):
    conn, q, pol = _committed_book(book, monkeypatch, [None])
    at = NOW + 50
    conn.execute("UPDATE orders SET exit_requested_at=?, exit_reason='manual', due_at=? WHERE order_id='o1'", (at - 1, at))
    monkeypatch.setattr(L, "_ps_sell", lambda *a, **k: (None, "ps_capture_unproven"))
    monkeypatch.setattr(L, "_ps_estimate", lambda *a, **k: 1 / 0)
    manage(conn, pol, at + .1)
    assert conn.execute("SELECT state FROM orders").fetchone()[0] == "open"


def test_each_estimate_kind_has_its_own_kill_switch(book, monkeypatch):
    t = NOW + 100
    conn, q, pol = _committed_book(book, monkeypatch, [t - 2])
    monkeypatch.setattr(L, "_ps_sell", lambda *a, **k: (None, "pool_tip_unproven"))
    monkeypatch.setattr(L, "_ps_estimate", lambda *a, **k: (dict(pool="P", lamports="1", binding_kind="estimated", proof={}), None))
    monkeypatch.setenv("OFFICE_MARK_ESTIMATED", "0")
    manage(conn, pol, t)
    assert conn.execute("SELECT reason FROM marks ORDER BY id DESC LIMIT 1").fetchone()[0] == "pool_tip_unproven;estimate_switched_off"
    monkeypatch.delenv("OFFICE_MARK_ESTIMATED")
    monkeypatch.setenv("OFFICE_MARK_CARRIED", "0")
    manage(conn, pol, t + 1)
    assert conn.execute("SELECT binding_kind FROM marks ORDER BY id DESC LIMIT 1").fetchone()[0] == "estimated"


def test_the_ledger_entry_is_the_carrying_buy_and_a_stale_carry_censors():
    assert L._psp.entry_buy is not L._psp.buy and 'entry_carry_stale' not in L.TRANSIENT_FILL


def test_an_exit_keeps_its_cut_through_tape_lag_and_fills_when_it_proves(book, monkeypatch):
    conn, q, pol = _committed_book(book, monkeypatch, [None])
    at = NOW + 50
    conn.execute("UPDATE orders SET exit_requested_at=?, exit_reason='manual', due_at=? WHERE order_id='o1'", (at - 1, at))
    cuts = []
    refusals = ["frontier_not_committed"] * 8 + ["no_block_before_as_of"] * 4 + ["ps_capture_unproven"] * 20
    def sell(desk, mint, qty, cut, **kw):
        cuts.append(cut)
        return (None, refusals.pop(0)) if refusals else (dict(pool="P", lamports="200000000"), None)
    monkeypatch.setattr(L, "_ps_sell", sell)
    for tick in range(0, 33):                         # FX sample moves past the cut at tick 5
        fx_at = at - 1 if tick < 5 else at + tick
        L.manage(conn, pol, desk_dir=Path("/nonexistent"), clock_sample=CLOCK, now=at + tick, sol_usd=FX, sol_at=fx_at)
    row = conn.execute("SELECT state, closed_at, exit_frontier_retries FROM orders").fetchone()
    assert (row["state"], row["closed_at"], row["exit_frontier_retries"]) == ("closed", at, 0)
    assert len(set(cuts)) == 1 and conn.execute("SELECT count(*) FROM outages").fetchone()[0] == 0


def _held_past_the_bound(book, monkeypatch, estimate):
    conn, q, pol = _committed_book(book, monkeypatch, [None])
    at = NOW + 50
    conn.execute("UPDATE orders SET exit_requested_at=?, exit_reason='manual', due_at=? WHERE order_id='o1'", (at - 1, at))
    monkeypatch.setattr(L, "_ps_sell", lambda *a, **k: (None, "ps_capture_unproven"))
    seen = []
    def fake(desk, mint, qty, cut, *, observed_at):
        seen.append((cut, observed_at))
        return estimate
    monkeypatch.setattr(L, "_ps_estimate", fake)
    for tick in range(0, int(L.EXIT_CUT_HOLD_S) + 2):
        L.manage(conn, pol, desk_dir=Path("/nonexistent"), clock_sample=CLOCK, now=at + tick, sol_usd=FX, sol_at=at - 1 if tick < 5 else at + tick)
    return conn, at, seen


def test_a_failed_hold_closes_labelled_at_the_estimate_for_that_cut(book, monkeypatch):
    estimate = (dict(pool="P", lamports="150000000", binding_kind="carried", proof=dict(newest_swap_age_at_observation_s=3., carried_age_s=9.)), None)
    conn, at, seen = _held_past_the_bound(book, monkeypatch, estimate)
    row = conn.execute("SELECT * FROM orders").fetchone()
    assert (row["state"], row["closed_at"], row["proceeds_lamports"], row["exit_fill_kind"]) == ("closed", at + 61, "150000000", "estimated_terminal")
    proof = json.loads(row["exit_proof"])
    assert proof["binding_kind"] == "carried" and seen == [(at + 61, at + 61)] and proof["proof"]["carried_age_s"] == 9.
    assert proof["terminal"] == dict(held_cut=at, hold_expiry_at=at + 60, estimate_observed_at=at + 61, hold_retries=0, cut_rule="first_estimate_at_or_after_hold_expiry")
    flow = conn.execute("SELECT cost_lamports, usd FROM cashflows WHERE kind='exit'").fetchone()
    assert flow["cost_lamports"] == str(L.COST_LAMPORTS["exit"])     # the strict fill's cost
    before = dict(row)
    monkeypatch.setenv("OFFICE_EXIT_ESTIMATE_FILL", "0")             # a later flip never relabels
    manage(conn, policy(), at + 100)
    assert dict(conn.execute("SELECT * FROM orders").fetchone()) == before
    cash = L.equity(conn, now=at + 100, mark_max_age_s=30., sol_usd=FX, sol_at=at + 100, book_id="A", require_proven=True).cash
    assert cash == pytest.approx(L.capital(conn, "A") + conn.execute("SELECT SUM(usd) FROM cashflows").fetchone()[0])   # ruled (b): it counts in kill equity
    assert flow["usd"] > 0


def test_a_strict_close_is_labelled_strict(book, monkeypatch):
    conn, q, pol = _committed_book(book, monkeypatch, [None])
    at = NOW + 50
    conn.execute("UPDATE orders SET exit_requested_at=?, exit_reason='manual', due_at=? WHERE order_id='o1'", (at - 1, at))
    monkeypatch.setattr(L, "_ps_sell", lambda *a, **k: (dict(pool="P", lamports="200000000"), None))
    manage(conn, pol, at)
    row = conn.execute("SELECT state, exit_fill_kind, exit_trigger_kind FROM orders").fetchone()
    assert tuple(row) == ("closed", "strict", "not_rails")


def test_the_trigger_label_is_the_mark_that_fired_the_rail(book, monkeypatch):
    conn, q, pol = _committed_book(book, monkeypatch, [None])
    at = NOW + 50
    conn.execute("INSERT INTO marks(order_id,at,lamports,source,binding_kind) VALUES('o1',?,'1','fixture','carried')", (at - 2,))
    conn.execute("UPDATE orders SET exit_requested_at=?, exit_requested_by='rails', exit_reason='trail', due_at=? WHERE order_id='o1'", (at - 1, at))
    monkeypatch.setattr(L, "_ps_sell", lambda *a, **k: (dict(pool="P", lamports="200000000"), None))
    manage(conn, pol, at)
    assert tuple(conn.execute("SELECT exit_fill_kind, exit_trigger_kind FROM orders").fetchone()) == ("strict", "carried")


def test_no_estimate_keeps_trying_and_the_next_tick_closes_at_its_own_time(book, monkeypatch):
    conn, at, seen = _held_past_the_bound(book, monkeypatch, (None, "estimate_tip_stale"))
    assert tuple(conn.execute("SELECT state, due_at, exit_fill_kind, exit_frontier_retries FROM orders").fetchone()) == ("open", at, None, 0)
    monkeypatch.setattr(L, "_ps_estimate", lambda *a, **k: (dict(pool="P", lamports="1", binding_kind="estimated", proof={}), None))
    L.manage(conn, policy(), desk_dir=Path("/nonexistent"), clock_sample=CLOCK, now=at + 62, sol_usd=FX, sol_at=at + 61)
    row = conn.execute("SELECT state, closed_at, exit_proof FROM orders").fetchone()
    terminal = json.loads(row["exit_proof"])["terminal"]
    assert (row["state"], row["closed_at"], terminal["hold_expiry_at"], terminal["estimate_observed_at"]) == ("closed", at + 62, at + 60, at + 62)


def test_the_kill_knob_stays_open_on_a_fresh_cut(book, monkeypatch):
    monkeypatch.setenv("OFFICE_EXIT_ESTIMATE_FILL", "0")
    conn, at, seen = _held_past_the_bound(book, monkeypatch, (dict(pool="P", lamports="1", binding_kind="carried", proof={}), None))
    row = conn.execute("SELECT state, due_at, exit_fill_kind, exit_frontier_retries FROM orders").fetchone()
    assert (row["state"], row["exit_fill_kind"], row["exit_frontier_retries"]) == ("open", None, 1) and row["due_at"] > at
    assert seen == []


def test_a_terminal_fill_refuses_fx_stale_at_its_own_observation(book, monkeypatch):
    conn, at, seen = _held_past_the_bound(book, monkeypatch, (None, "estimate_tip_stale"))
    monkeypatch.setattr(L, "_ps_estimate", lambda *a, **k: (dict(pool="P", lamports="1", binding_kind="estimated", proof={}), None))
    pol = policy();late = at + 62
    conn.execute("UPDATE orders SET exit_cut_fx_at=?", (late - pol.fx_max_age_s - 1,));conn.commit()   # the kept sample is stale too
    L.manage(conn, pol, desk_dir=Path("/nonexistent"), clock_sample=CLOCK, now=late, sol_usd=FX, sol_at=late - pol.fx_max_age_s - 1)
    assert conn.execute("SELECT state FROM orders").fetchone()[0] == "open"
    L.manage(conn, pol, desk_dir=Path("/nonexistent"), clock_sample=CLOCK, now=late + 1, sol_usd=FX, sol_at=late)
    row = conn.execute("SELECT state, exit_fill_kind FROM orders").fetchone()
    assert tuple(row) == ("closed", "estimated_terminal") and row["exit_fill_kind"] in ("strict", "estimated_terminal")


def test_a_terminal_fill_stores_a_later_strict_check_once_and_never_changes_the_fill(book, monkeypatch):
    conn, at, seen = _held_past_the_bound(book, monkeypatch, (dict(pool="P", lamports="150000000", binding_kind="carried", proof={}), None))
    closed = at + 61
    monkeypatch.setattr(L, "_ps_sell", lambda *a, **k: (dict(pool="P", lamports="90000000"), None))
    manage(conn, policy(), closed + 5)
    assert conn.execute("SELECT exit_estimate_check FROM orders").fetchone()[0] is None      # no strict cut after the close yet
    manage(conn, policy(), closed + 20)
    row = conn.execute("SELECT proceeds_lamports, exit_estimate_check FROM orders").fetchone()
    assert row[0] == "150000000" and json.loads(row[1]) == dict(state="known", cut=closed + 10, lamports="90000000", after_close_s=10.)


def test_no_strict_check_by_the_bound_is_recorded_as_none(book, monkeypatch):
    conn, at, seen = _held_past_the_bound(book, monkeypatch, (dict(pool="P", lamports="1", binding_kind="carried", proof={}), None))
    monkeypatch.setattr(L, "_ps_sell", lambda *a, **k: (dict(pool="P", lamports="9"), None))   # a valid quote past the bound is no check
    manage(conn, policy(), at + 61 + L.ESTIMATE_CHECK_MAX_S + L.ESTIMATE_CHECK_LAG_S + 1)
    assert json.loads(conn.execute("SELECT exit_estimate_check FROM orders").fetchone()[0])["state"] == "none_within_bound"


def test_a_refusal_branch_reaches_the_null_mark_and_the_kill_knob_drops_the_live_clock(book, monkeypatch):
    conn, q, pol = _committed_book(book, monkeypatch, [None])
    at = NOW + 50
    conn.execute("UPDATE orders SET exit_requested_at=?, exit_reason='manual', due_at=? WHERE order_id='o1'", (at - 1, at))
    clocks = []
    def sell(desk, mint, qty, cut, *, live_now=None, detail=None, **kw):
        clocks.append(live_now);detail.append("heartbeat_ahead")
        return None, "ps_capture_unproven"
    monkeypatch.setattr(L, "_ps_sell", sell)
    monkeypatch.setattr(L.time, "time", lambda: at + 1)
    manage(conn, pol, at);manage(conn, pol, at + 1)
    row = conn.execute("SELECT reason, null_n, reason_branches FROM marks ORDER BY id DESC LIMIT 1").fetchone()
    assert (row[0], row[1], json.loads(row[2])) == ("ps_capture_unproven", 2, {"heartbeat_ahead": 2}) and clocks[0] is L.time.time
    monkeypatch.setenv("OFFICE_CAPTURE_LIVE_CLOCK", "0")
    manage(conn, pol, at + 2)
    assert clocks[-1] is None
    branches = json.loads(conn.execute("SELECT reason_branches FROM marks ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert branches == {"heartbeat_ahead": 3, "live_clock_off": 1}
    monkeypatch.delenv("OFFICE_CAPTURE_LIVE_CLOCK")
    monkeypatch.setattr(L.time, "time", lambda: at + 3 + L.LIVE_CLOCK_MAX_TICK_AGE_S + 1)   # a historical replay keeps its frozen time
    manage(conn, pol, at + 3)
    assert clocks[-1] is None


def _exit_pending(book, monkeypatch):
    conn, q, pol = _committed_book(book, monkeypatch, [None])
    at = NOW + 50
    conn.execute("UPDATE orders SET exit_requested_at=?, exit_reason='manual', due_at=? WHERE order_id='o1'", (at - 1, at))
    return conn, pol, at


def test_a_block_cut_refusal_takes_a_fresh_cut_at_once_with_no_outage(book, monkeypatch):
    conn, pol, at = _exit_pending(book, monkeypatch)
    cuts = []
    def sell(desk, mint, qty, cut, **kw):
        cuts.append(cut)
        return (None, "pool_block_cut_unproven") if len(cuts) == 1 else (dict(pool="P", lamports="200000000"), None)
    monkeypatch.setattr(L, "_ps_sell", sell)
    manage(conn, pol, at);manage(conn, pol, at + L.EXIT_DELAY_S)
    row = conn.execute("SELECT state, closed_at, exit_fill_kind FROM orders").fetchone()
    assert cuts == [at, at + L.EXIT_DELAY_S] and tuple(row) == ("closed", at + L.EXIT_DELAY_S, "strict")
    assert conn.execute("SELECT count(*) FROM outages").fetchone()[0] == 0


def test_block_cut_refusals_cannot_chase_past_the_request_bound(book, monkeypatch):
    conn, pol, at = _exit_pending(book, monkeypatch)
    monkeypatch.setattr(L, "_ps_sell", lambda *a, **k: (None, "pool_block_cut_unproven"))
    monkeypatch.setattr(L, "_ps_estimate", lambda *a, **k: (dict(pool="P", lamports="5", binding_kind="estimated", proof={}), None))
    end = int(L.EXIT_REQUEST_MAX_S) + 2
    for tick in range(end):
        L.manage(conn, pol, desk_dir=Path("/nonexistent"), clock_sample=CLOCK, now=at + tick, sol_usd=FX, sol_at=at + tick)
        if conn.execute("SELECT state FROM orders").fetchone()[0] == "closed":break
    row = conn.execute("SELECT state, closed_at, exit_fill_kind, exit_proof FROM orders").fetchone()
    assert (row["state"], row["exit_fill_kind"]) == ("closed", "estimated_terminal") and row["closed_at"] == at + tick
    assert tick == int(L.EXIT_REQUEST_MAX_S)
    assert json.loads(row["exit_proof"])["terminal"]["hold_expiry_at"] == at - 1 + L.EXIT_REQUEST_MAX_S
    assert conn.execute("SELECT count(*) FROM outages").fetchone()[0] == 0


def test_entry_refusals_are_counted_by_reason_and_branch_and_the_fill_is_the_later_cut(book, monkeypatch):
    conn, q, tmp = book;pol = policy()
    L.submit(conn, pol, book_id="A", order_id="o1", network_id="solana", mint=MINT, pool="P", decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    answers = [("ps_capture_unproven", "heartbeat_ahead"), ("pool_block_cut_unproven", None)]
    def buy(desk, mint, amount, cut, *, detail=None, **kw):
        if answers:
            why, branch = answers.pop(0)
            if branch:detail.append(branch)
            return None, why
        return dict(pool="P", tokens_atomic="123", spend_lamports="200000000"), None
    monkeypatch.setattr(L, "_ps_buy", buy);monkeypatch.setattr(L.time, "time", lambda: NOW + 2)
    for tick in (1, 2, 3):
        L.manage(conn, pol, desk_dir=tmp, clock_sample=CLOCK, now=NOW + tick, sol_usd=FX, sol_at=NOW)
    row = conn.execute("SELECT state, filled_at, entry_refusals FROM orders").fetchone()
    assert row["state"] == "open" and row["filled_at"] == NOW + 3
    assert json.loads(row["entry_refusals"]) == {"ps_capture_unproven": 1, "ps_capture_unproven:heartbeat_ahead": 1, "pool_block_cut_unproven": 1}


def test_past_the_request_bound_a_strict_close_beats_the_estimate(book, monkeypatch):
    conn, pol, at = _exit_pending(book, monkeypatch)
    answers = iter([(None, "pool_block_cut_unproven")] * int(L.EXIT_REQUEST_MAX_S))
    monkeypatch.setattr(L, "_ps_sell", lambda *a, **k: next(answers, (dict(pool="P", lamports="200000000"), None)))
    monkeypatch.setattr(L, "_ps_estimate", lambda *a, **k: 1 / 0)
    for tick in range(int(L.EXIT_REQUEST_MAX_S) + 2):
        L.manage(conn, pol, desk_dir=Path("/nonexistent"), clock_sample=CLOCK, now=at + tick, sol_usd=FX, sol_at=at + tick - 2)
        if conn.execute("SELECT state FROM orders").fetchone()[0] == "closed":break
    assert tuple(conn.execute("SELECT state, exit_fill_kind FROM orders").fetchone()) == ("closed", "strict")


def test_an_order_without_a_stratum_is_unstamped_null_never_known_and_a_carried_stratum_is_atomic_with_the_insert(book):
    conn, q, tmp = book
    row = opened(conn, q, policy())
    assert (row["sheet_state"], row["sells_basis"], row["sheet_class"]) == (None, None, None)                  # no default: unstamped reads as unknown, never as a known-sheet order
    strat = dict(sheet_state="partial", sheet_class="tape_loss_since_cut", sheet_ticket_age_s=4000.5, sheet_unknown_n=7, sheet_view_hash="h" * 64, sells_basis="sell_probe", sells_n=None,
                 probe_age_s=4100.0, probe_loss_pct=2.37)
    r = L.submit(conn, policy(), book_id="A", order_id="o2", network_id="solana", mint=MINT + "2", pool="P", decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW,
                 metadata=dict(ticker="T", decimals=6, metadata_known_at=NOW - 1, stratum=strat))
    assert r.state == "pending"
    got = conn.execute("SELECT sheet_state,sheet_class,sheet_ticket_age_s,sheet_unknown_n,sheet_view_hash,sells_basis,sells_n,probe_age_s,probe_loss_pct FROM orders WHERE order_id='o2'").fetchone()
    assert tuple(got) == ("partial", "tape_loss_since_cut", 4000.5, 7, "h" * 64, "sell_probe", None, 4100.0, 2.37)


@pytest.mark.parametrize("bad", [dict(sheet_state="fine"), dict(sells_basis="guess"), dict(sheet_ticket_age_s="x"), dict(sheet_class=5), dict(extra=1)])
def test_an_invalid_stratum_creates_no_order(book, bad):
    conn, q, tmp = book
    strat = dict(sheet_state="known", sheet_class=None, sheet_ticket_age_s=None, sheet_unknown_n=None, sheet_view_hash=None, sells_basis="fresh_flow", sells_n=None, probe_age_s=None, probe_loss_pct=None)
    strat.update(bad)
    with pytest.raises(ValueError, match="stratum_invalid"):
        L.submit(conn, policy(), book_id="A", order_id="o3", network_id="solana", mint=MINT + "3", pool="P", decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW,
                 metadata=dict(ticker="T", decimals=6, metadata_known_at=NOW - 1, stratum=strat))
    assert conn.execute("SELECT count(*) FROM orders WHERE order_id='o3'").fetchone()[0] == 0


def test_the_migration_backfills_orders_that_predate_the_columns_once_and_later_nulls_stay_null(tmp_path):
    import sqlite3
    path = tmp_path / "old" / "ledger.db"
    path.parent.mkdir()
    raw = sqlite3.connect(path)
    raw.executescript(L.SCHEMA)                                                    # an old ledger: the base schema, no stratum columns
    raw.execute("INSERT INTO orders(order_id,book_id,epoch_id,book_kind,network_id,mint,pool,decision_bundle_hash,policy_json,policy_hash,fill_model_version,state,requested_at,due_at) "
                "VALUES('old','A','E','evaluated','solana','m','p','h','{}','x','v','closed',1,2)")
    raw.commit(); raw.close()
    conn = L.open_ledger(path)
    assert tuple(conn.execute("SELECT sheet_state,sells_basis FROM orders WHERE order_id='old'").fetchone()) == ("known", "fresh_flow")      # pre-change by definition
    conn.execute("INSERT INTO orders(order_id,book_id,epoch_id,book_kind,network_id,mint,pool,decision_bundle_hash,policy_json,policy_hash,fill_model_version,state,requested_at,due_at) "
                 "VALUES('new','A','E','evaluated','solana','m2','p','h','{}','x','v','closed',1,2)")
    conn.close()
    conn = L.open_ledger(path)                                                     # a reopen must not backfill a row that was written NULL after the migration
    assert tuple(conn.execute("SELECT sheet_state,sells_basis FROM orders WHERE order_id='new'").fetchone()) == (None, None)
    conn.close()


def test_entry_with_an_unproven_tip_retries_and_only_exits_keep_their_path():
    assert 'pool_tip_unproven' in L.TRANSIENT_FILL and 'pool_tip_unproven' not in L.EXIT_CUT_HOLD
