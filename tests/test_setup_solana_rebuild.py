"""The Solana pre-signal rebuild (setup grading A2/A2.7): the instrument, its stops and its pause."""
from __future__ import annotations

import importlib.util
import pathlib
import sqlite3
import urllib.error

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "research" / "setup_solana_rebuild.py"
_spec = importlib.util.spec_from_file_location("setup_solana_rebuild", _PATH)
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

MINT = "Coin1111111111111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
COIN_VAULT, QUOTE_VAULT, OWNER = "CoinVault111", "QuoteVault111", "PoolAuth111"
TRADER, SOURCE = "Trader111", "Source111"
T = 1_790_000_000.0
AMM = {"coin_vault": COIN_VAULT, "quote_vault": QUOTE_VAULT, "quote_mint": WSOL, "curve": False}


def _bal(index, mint, owner, amount):
    return {"accountIndex": index, "mint": mint, "owner": owner, "uiTokenAmount": {"amount": str(amount)}}


def _tx(sig, ts, coin_d, quote_d, *, signer=TRADER, created=False):
    """A parsed transaction touching the pool's two vaults: + means into the pool."""
    keys = [{"pubkey": signer, "signer": True}, {"pubkey": COIN_VAULT, "signer": False},
            {"pubkey": QUOTE_VAULT, "signer": False}]
    pre = [] if created else [_bal(1, MINT, OWNER, 1_000_000)]
    pre += [_bal(2, WSOL, OWNER, 5_000_000)]
    post = [_bal(1, MINT, OWNER, 1_000_000 + coin_d), _bal(2, WSOL, OWNER, 5_000_000 + quote_d)]
    return {"blockTime": ts, "slot": int(ts), "transaction": {"signatures": [sig], "message": {
        "accountKeys": keys}}, "meta": {"preTokenBalances": pre, "postTokenBalances": post,
                                        "preBalances": [0, 0, 0], "postBalances": [0, 0, 0], "fee": 0}}


def _side(**kw):
    kw.setdefault("source_sig", None)
    return R.PoolSide(mint=MINT, binding=AMM, t=T, **kw)


# ------------------------------------------------------------------ the pool-side instrument

def test_a_swap_is_priced_from_the_pool_and_liquidity_is_excluded():
    side = _side()
    side.add([_tx("buy", T - 10, -1_000, 2_000),           # coin out, quote in: a buy at 2.0
              _tx("lp", T - 20, 500, 1_000)])               # both in: a deposit, not a trade
    assert side.swaps == [(T - 10, 2.0, 2000.0)]
    assert side.n["liquidity"] == 1 and side.n["txs"] == 2


def test_the_source_trade_is_excluded_by_signature():
    side = _side(source_sig="src")
    side.add([_tx("src", T - 2, -1_000, 9_000), _tx("other", T - 5, -1_000, 2_000)])
    assert [s[1] for s in side.swaps] == [2.0] and side.n["source_excluded"] == 1


def test_the_source_wallet_is_excluded_only_in_the_last_minute():
    side = _side(source_wallets={SOURCE})
    side.add([_tx("late", T - 30, -1_000, 3_000, signer=SOURCE),
              _tx("early", T - 300, -1_000, 2_000, signer=SOURCE)])
    assert [s[1] for s in side.swaps] == [2.0] and side.n["source_excluded"] == 1


def test_the_vaults_creation_dates_the_pool():
    side = _side()
    side.add([_tx("b", T - 100, -1_000, 2_000), _tx("init", T - 400, 1_000, 2_000, created=True)])
    assert side.created_at == T - 400


# ------------------------------------------------------------------ A1 features and categories

def _swaps(*pairs):
    """(seconds before t, price) newest first, unit volume."""
    return [(T - ago, price, 1.0) for ago, price in pairs]


def test_fewer_than_five_swaps_is_c0():
    out = R.features(_swaps((10, 1.0), (20, 1.0)), T, T - 3600, None, True)
    assert out["category"] == "C0" and out["c0_reason"] == "fewer_than_5_swaps"


def test_no_complete_five_minutes_is_c0():
    out = R.features(_swaps(*[(i, 1.0) for i in range(10, 70, 10)]), T, T - 100, None, False)
    assert out["category"] == "C0" and out["c0_reason"] == "no_complete_5m"


def test_vertical_is_c3():
    rows = [(10, 1.6), (100, 1.2), (200, 1.0), (1000, 2.0), (2000, 2.0)]
    out = R.features(_swaps(*rows), T, T - 3600, None, True)
    assert out["category"] == "C3"


def test_a_deep_pullback_is_c4():
    rows = [(10, 0.5), (100, 0.5), (200, 0.5), (1000, 1.0), (2000, 1.0)]
    assert R.features(_swaps(*rows), T, T - 3600, None, True)["category"] == "C4"


def test_a_fresh_pool_is_c1_first():
    rows = [(10, 1.6), (100, 1.2), (200, 1.0), (300, 1.0), (400, 1.0)]
    assert R.features(_swaps(*rows), T, T - 3600, T - 500, True)["category"] == "C1"


def test_a_capped_pull_scores_only_the_windows_it_reached():
    """15 minutes reached, the hour not: C2, C4 and C5 cannot be scored, and C1 has no age."""
    rows = [(i, 1.0) for i in range(30, 900, 30)]                          # flat, 29 swaps
    out = R.features(_swaps(*rows), T, T - 1000, None, False)
    assert out["scoreable"] == ["C3", "C6"] and out["category"] == "C6"
    assert out["complete_15m"] and not out["complete_1h"]


# ------------------------------------------------------------------ paging

def test_the_pull_pages_newest_first_and_marks_a_cap():
    pages = [{"data": [{"blockTime": T - 1 - i} for i in range(1000)], "paginationToken": "x"}] * 3
    seen, params = [], []
    calls = iter(pages)

    def call(method, p, credits, max_credits=None):
        params.append(p[1])
        return next(calls)
    out = R.pull(call, "owner", T, 2000, seen.append, mint=MINT)
    assert out == (2, False, T - 1000 + 1.0) and len(seen) == 2
    assert params[0]["sortOrder"] == "desc" and params[0]["filters"]["tokenTransfer"] == {"mint": MINT}
    assert params[1]["paginationToken"] == "x"


def test_a_short_page_completes_the_window():
    out = R.pull(lambda m, p, c, max_credits=None: {"data": [{"blockTime": T - 5}]},
                 "pool", T, 10_000, lambda d: None)
    assert out == (1, True, T - 3600)


def test_page_credits_follow_helius_billing():
    assert [R.page_credits({"data": [0] * n}) for n in (0, 1, 100, 101, 1000)] == [10, 10, 10, 20, 100]


def test_the_owner_comes_from_the_curve_or_the_tape_before_any_call():
    tape = sqlite3.connect(":memory:")
    tape.execute("CREATE TABLE sol_pool_vaults (pool TEXT, program TEXT)")
    tape.execute("INSERT INTO sol_pool_vaults VALUES (?, ?)", (COIN_VAULT, OWNER))
    no_call = lambda *a, **k: pytest.fail("no RPC needed")
    assert R.owner_of(no_call, tape, AMM) == OWNER
    assert R.owner_of(no_call, tape, {**AMM, "curve": True}) == QUOTE_VAULT


# ------------------------------------------------------------------ the caller's stops and pause

class Ledger:
    def __init__(self, open_counts):
        self.open_counts = list(open_counts)

    def execute(self, *_a):
        n = self.open_counts.pop(0) if self.open_counts else 0
        return type("C", (), {"fetchone": lambda _s: (n,)})()


def _caller(post, *, budget=100, open_counts=()):
    slept = []                                    # the fake clock is the time slept
    c = R.Caller(post, Ledger(open_counts), budget, sleep=slept.append, clock=lambda: sum(slept))
    return c, slept


def test_the_budget_stops_before_the_call_that_would_cross_it():
    c, _ = _caller(lambda m, p: {"result": {}}, budget=25)
    c("m", [], 10)
    c("m", [], 10)
    with pytest.raises(R.Stop, match="budget"):
        c("m", [], 10)
    assert c.spent == 20 and c.calls == 2


def test_a_page_is_checked_against_its_worst_case_and_billed_as_returned():
    c, _ = _caller(lambda m, p: {"result": {"data": [0] * 150}}, budget=100)
    c("m", [], R.page_credits, max_credits=100)
    assert c.spent == 20
    with pytest.raises(R.Stop, match="budget"):
        c("m", [], R.page_credits, max_credits=100)


def test_three_consecutive_429s_stop_after_5_and_10_second_backoffs():
    def post(m, p):
        raise urllib.error.HTTPError("u", 429, "rate", {}, None)
    c, slept = _caller(post)
    with pytest.raises(R.Stop, match="429"):
        c("m", [], 10)
    assert [s for s in slept if s != R.SPACING_S] == [5.0, 10.0] and c.spent == 0


def test_an_open_solana_position_pauses_and_the_pause_is_not_budgeted():
    c, slept = _caller(lambda m, p: {"result": {"ok": 1}}, open_counts=(1, 1, 0))
    assert c("m", [], 10) == {"ok": 1}
    assert slept.count(R.FLAT_POLL_S) == 2 and c.pauses == [60.0] and c.spent == 10


# ------------------------------------------------------------------ the real tape is never written

def test_the_entry_tx_fallback_resolves_into_a_scratch_store(tmp_path):
    """The production resolver, fed our own entry fill, writes into an in-memory tape only."""
    tape_path = tmp_path / "tape.db"
    from sniper.persistence.db_ticks import connect_sol_tape
    from sniper.services.memecoin_solana_tape import ensure_sol_cursor
    real = connect_sol_tape(str(tape_path))
    ensure_sol_cursor(real)
    real.commit()
    ro = sqlite3.connect(f"file:{tape_path}?mode=ro", uri=True)
    ledger = sqlite3.connect(":memory:")
    ledger.execute("CREATE TABLE memecoin_fills (id INTEGER PRIMARY KEY, position_id INTEGER, "
                   "kind TEXT, tx_sig TEXT)")
    ledger.execute("INSERT INTO memecoin_fills (position_id, kind, tx_sig) VALUES (5,'entry','ours')")
    entry = _tx("ours", T - 900, -1_000, 2_000, signer="UsWallet111")   # we bought from the pool
    calls = []

    def call(method, params, credits):
        calls.append((method, params[0], credits))
        return entry
    got = R.binding_for(ro, ledger, {"id": 5, "mint": MINT}, call)
    assert got == {**AMM, "source": "entry_tx"}
    assert calls == [("getTransaction", "ours", R.TX_CREDITS)]
    assert real.execute("SELECT COUNT(*) FROM sol_pool_vaults").fetchone()[0] == 0


def test_vault_moves_ignores_zero_delta_touches():
    """A2.8's set comparison counts only transactions that moved our vault."""
    touch = _tx("touch", T - 3, 0, 0)
    assert R.vault_moves([_tx("swap", T - 2, -1_000, 2_000), touch], COIN_VAULT) == {"swap"}


# ------------------------------------------------------------------ A2.9

def test_c7_needs_all_six_categories_scoreable():
    """Flat and complete: nothing fires, everything was scoreable -> C7."""
    rows = [(i, 1.0) for i in range(20, 3600, 120)]
    out = R.features(_swaps(*rows), T, T - 3600, None, True)
    assert len(out["scoreable"]) == 6 and out["category"] == "C7"


def test_a_partly_unscoreable_decision_with_nothing_firing_is_c0_not_c7():
    rows = [(i, 1.0 + i / 2000) for i in range(30, 900, 30)]               # drifting, 15 m reached
    out = R.features(_swaps(*rows), T, T - 1000, None, False)
    assert out["scoreable"] == ["C3", "C6"]
    assert out["category"] == "C0" and out["c0_reason"] == "partly_unscoreable"


def _tape_with_binding():
    tape = sqlite3.connect(":memory:")
    tape.execute("CREATE TABLE sol_position_pools (position_id INTEGER, mint TEXT, pool TEXT, "
                 "venue_kind TEXT, resolution_tx TEXT, resolved_at TEXT)")
    tape.execute("CREATE TABLE sol_pool_vaults (pool TEXT, mint TEXT, coin_vault TEXT, quote_vault TEXT, "
                 "quote_mint TEXT, resolved_at TEXT, program TEXT)")
    tape.execute("INSERT INTO sol_position_pools VALUES (9, ?, ?, 'amm', 'x', 'now')", (MINT, COIN_VAULT))
    tape.execute("INSERT INTO sol_pool_vaults VALUES (?, ?, ?, ?, ?, 'now', ?)",
                 (COIN_VAULT, MINT, COIN_VAULT, QUOTE_VAULT, WSOL, OWNER))
    return tape


def _run(verified, delta_s):
    windows = []

    def call(method, params, credits, max_credits=None):
        windows.append(params[1]["filters"]["blockTime"])
        return {"data": []}
    created = "2026-09-01T00:00:00+00:00"
    row = (9, MINT, "2026-09-01T00:00:00+00:00", "uid", "act-1234", created)
    rec = R.run_one(call, None, _tape_with_binding(), row, verified, 10_000, delta_s=delta_s)
    return rec, windows, R.epoch(created)


def test_an_unexcludable_source_ends_every_window_delta_before_t():
    rec, windows, t = _run({}, 25.0)
    assert rec["source_excluded_by"] == "none" and rec["window_cut_s"] == 25.0
    assert windows == [{"gte": int(t - 25) - 3600, "lte": int(t - 25) - 1}]


def test_an_excluded_source_keeps_its_window():
    rec, windows, t = _run({"uid": {SOURCE}}, 25.0)
    assert rec["source_excluded_by"] == "wallet" and "window_cut_s" not in rec
    assert windows == [{"gte": int(t) - 3600, "lte": int(t) - 1}]


def test_delta_is_the_largest_excluded_source_lag_rounded_up_to_five_seconds():
    tape = _tape_with_binding()
    t = 1_790_000_000.0
    pop = [(1, MINT, "x", "u1", "o", "2026-09-01T00:00:12+00:00"),
           (9, MINT, "x", "uid", "o", R.datetime.fromtimestamp(t, R.timezone.utc).isoformat())]
    rows = [{"position_id": 1, "source_excluded_by": "signature", "t_basis": "source_tx",
             "t": R.epoch("2026-09-01T00:00:00+00:00")},
            {"position_id": 9, "source_excluded_by": "wallet", "pool": COIN_VAULT, "t": t}]

    def call(method, params, credits, max_credits=None):
        assert params[1]["filters"]["blockTime"] == {"gte": int(t) - 60, "lte": int(t) - 1}
        return {"data": [_tx("src", t - 17, -1, 1, signer=SOURCE), _tx("x", t - 40, -1, 1),
                         _tx("early", t - 55, -1, 1, signer=SOURCE)]}   # an earlier source trade
    res = R.source_lags(call, None, tape, rows, {"uid": {SOURCE}}, pop)
    assert sorted(lag for *_, lag in res["lags"]) == [12.0, 17.0]
    assert res["max_lag_s"] == 17.0 and res["delta_s"] == 20.0


def test_the_full_run_orders_test_first_then_train_newest_first():
    pop = [(1, "m", "2026-09-01", "u", "o", "c"), (2, "m", "2026-09-25", "u", "o", "c"),
           (3, "m", "2026-09-10", "u", "o", "c"), (4, "m", "2026-09-28", "u", "o", "c")]
    assert [r[0] for r in R.full_order(pop)] == [4, 2, 3, 1]


def test_reused_pilot_rows_apply_a29_and_drop_unexcludable_sources(tmp_path):
    import json
    path = tmp_path / "pilot.jsonl"
    rows = [{"position_id": 1, "source_excluded_by": "wallet", "category": "C7", "scoreable": ["C3"]},
            {"position_id": 2, "source_excluded_by": "signature", "category": "C7",
             "scoreable": ["C1", "C2", "C3", "C4", "C5", "C6"]},
            {"position_id": 3, "source_excluded_by": "none", "category": "C3", "scoreable": ["C3"]}]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    got = {r["position_id"]: r["category"] for r in R.reused(path)}
    assert got == {1: "C0", 2: "C7"}
