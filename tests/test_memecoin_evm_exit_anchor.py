"""Anchor-checked EVM exit (spec docs/superpowers/specs/2026-10-01-memecoin-evm-anchor-checked-exit-design.md).

Driven through the PRODUCTION exit context (`build_real_evm_executor(...)["exit_fn"]` and
`["derisk_fn"]`, the callables the runner routes to; trap 0e30ad57), on chain "8453", whose quote token
is USD-kind (1 USDC == $1), so a quote's buyAmount reads directly as dollars.

The position: 10 tokens bought at $1.00 each, so the seeded anchor is $1.00 and X = 50% means a
whole-bag sell quote below $5.00 is held.
"""
import itertools
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from eth_account import Account

from sniper.execution.evm_real_seams import build_real_evm_executor
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_alerts as alerts
from sniper.services import memecoin_exit_anchor as A
from sniper.services import memecoin_manual_exit_intents as I
from sniper.services import memecoin_real_runner as R

DECISION_AT = "2026-10-01T12:00:00+00:00"
MINT = "0x" + "22" * 20
SPENDER = "0x" + "55" * 20


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def iso(self):
        return (datetime(2026, 10, 1, 12, tzinfo=timezone.utc) + timedelta(seconds=self.t)).isoformat()


@pytest.fixture
def env(monkeypatch):
    A.reset()
    clock = Clock()
    monkeypatch.setattr(A, "_clock", clock)
    monkeypatch.setenv("MEMECOIN_EVM_EXIT_ANCHOR_CHAINS", "8453")
    # THE DUST GUARD, EXPLICIT (Alpha 2026-10-06). It runs ON here, as in production, and an own
    # market mark that is down too is PRESENT, so a $1 quote on this $10 bag is a real collapse and
    # the anchor alone decides. 8453 has no own mark in production: the guard then HOLDs past the
    # anchor's bound, pinned in test_8453_with_no_own_mark_the_dust_guard_holds_past_the_anchor_bound.
    from sniper.services import memecoin_exit_dust_guard as G
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_DUST_GUARD", True)
    monkeypatch.setattr(G, "_PAGED", set())
    monkeypatch.setattr(G, "newest_own_mark", lambda *a, **k: (0.08, "test_own_mark_down_too"))
    pages = []
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: pages.append(a) or True)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    yield conn, clock, pages
    A.reset()


def _open_position(conn, uid="u1"):
    pid = m.open_position(
        conn, source_user_id=uid, source_handle="(real)", mint=MINT, ticker="TST",
        entry_price=1.0, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=-1.0,
        their_size_usd=0.0, opened_at=DECISION_AT, strategy="alpha_b", execution_mode="real",
        network_id="8453")
    m.insert_fill(conn, position_id=pid, kind="entry", price=1.0, qty=10.0, notional=10.0,
                  fill_source="chain", fraction=1.0, created_at=DECISION_AT, tx_sig="E")
    return m.get_position(conn, pid)


def _http(method, url, headers=None, body=None, timeout=10):
    if method != "POST":
        return 200, {}, None
    meth = json.loads(body).get("method")
    if meth == "eth_getTransactionCount":
        return 200, {"result": "0x1"}, None
    if meth == "eth_gasPrice":
        return 200, {"result": "0x3b9aca00"}, None
    if meth == "eth_getBalance":
        return 200, {"result": hex(10 ** 18)}, None
    if meth == "eth_call":
        return 200, {"result": "0x" + "f" * 64}, None
    if meth == "eth_getTransactionReceipt":
        return 200, {"result": {"status": "0x1"}}, None
    return 200, {"result": None}, None


def _executor(quotes_usd, proceeds_usd=15.0, during_quote=None):
    """`quotes_usd`: the whole-bag USD each successive sell quote pays (None = no route).
    `during_quote` runs inside each quote, as the ladder's own seconds pass."""
    quotes = iter(quotes_usd)
    usdc = itertools.cycle([0.0, proceeds_usd])

    def _quote(sell_token, buy_token, amount, *, slippage_bps=None):
        if during_quote is not None:
            during_quote()
        usd = next(quotes)
        if usd is None:
            return None
        return {"sellAmount": str(amount), "buyAmount": str(int(usd * 10 ** 6)),
                "otherAmountThreshold": str(int(usd * 0.9 * 10 ** 6)),
                "transaction": {"to": SPENDER, "data": "0x", "value": "0",
                                "gas": "300000", "gasPrice": "1000000000"},
                "issues": {"allowance": {"spender": SPENDER}}}
    from evm_receipt_fake import wrap
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    acct = Account.create()
    # The sell's own receipt pays the proceeds (every chain books exits from it, Alpha 2026-10-06).
    http = wrap(_http, owner=acct.address, mint=MINT, quote=CHAIN_QUOTE["8453"]["token"], coin=0,
                proceeds=int(proceeds_usd * 10 ** 18))
    return build_real_evm_executor(
        acct=acct, chain_id="8453", rpc_url="rpc", http=http, venue_quote_fn=_quote,
        usdc_fn=lambda: next(usdc), token_fn=lambda mint: 10.0, backend_label="auto_free")


def _audit(conn):
    return [dict(r) for r in conn.execute(
        "SELECT decision, value_usd, anchor_price, qty, held_s, exit_reason, rungs_json "
        "FROM memecoin_exit_anchor_audit ORDER BY id")]


# --- the executor: hold, recover, bound, override ------------------------------------------------

def test_a_low_sell_quote_is_held_at_once_and_recorded(env):
    conn, clock, _ = env
    pos = _open_position(conn)
    ex = _executor([1.0])
    res = ex["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)
    assert (res["status"], res["reason"]) == ("deferred", "held_anchor")
    assert m.get_position(conn, pos["id"])["is_open"]
    (row,) = _audit(conn)
    assert (row["decision"], row["value_usd"], row["anchor_price"], row["qty"]) == ("held", 1.0, 1.0, 10.0)
    assert row["exit_reason"] == "mirror_exit" and json.loads(row["rungs_json"])


def test_the_first_quote_back_above_x_sells_and_says_it_recovered(env):
    conn, clock, _ = env
    pos = _open_position(conn)
    ex = _executor([1.0, 15.0])
    assert ex["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)["reason"] == "held_anchor"
    clock.t += 1.5
    assert ex["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)["status"] == "closed"
    assert [r["decision"] for r in _audit(conn)] == ["held", "sold_recovered"]


def test_a_passing_quote_with_no_hold_writes_nothing(env):
    conn, clock, _ = env
    pos = _open_position(conn)
    assert _executor([15.0])["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)["status"] == "closed"
    assert _audit(conn) == []


def test_after_hold_s_and_two_held_attempts_the_next_sells_the_largest_answer_and_pages(env, caplog):
    conn, clock, pages = env
    pos = _open_position(conn)
    ex = _executor([1.0, 1.0, 1.0])
    for step in (0.0, 3.0):
        clock.t += step
        assert ex["exit_fn"](conn, pos, "b_stop_skip", DECISION_AT)["reason"] == "held_anchor"
    clock.t += 3.5                                                    # 6.5 s since the first hold
    with caplog.at_level(logging.WARNING):
        assert ex["exit_fn"](conn, pos, "b_stop_skip", DECISION_AT)["status"] == "closed"
    assert [r["decision"] for r in _audit(conn)] == ["held", "held", "sold_at_bound"]
    assert _audit(conn)[-1]["held_s"] == pytest.approx(6.5)
    assert any("sell ladder dust check fired" in r.message for r in caplog.records), \
        "the bound attempt must ask the ladder for the largest answer"
    assert len(pages) == 1 and pages[0][0] == "EXIT-ANCHOR-BOUND"


def test_8453_with_no_own_mark_the_dust_guard_holds_past_the_anchor_bound(env, monkeypatch):
    # Operator ruling b2ae12c0: no own market mark on 8453, so a dust candidate ($0.90 for a $10 bag)
    # HOLDs and pages once, and that overrides the anchor's "sell the largest answer after two held
    # attempts". The anchor's bound attempt sold nothing, so it is recorded as dust_held_at_bound and
    # never pages EXIT-ANCHOR-BOUND; its next episode holds again. Rollback: MEMECOIN_EXIT_DUST_GUARD=0.
    from sniper.services import memecoin_exit_dust_guard as G
    monkeypatch.setattr(G, "newest_own_mark", lambda *a, **k: None)
    conn, clock, pages = env
    pos = _open_position(conn)
    ex = _executor([0.9] * 9)
    seen = []
    for step in (0.0, 3.0, 3.5, 1.0, 3.0, 3.5, 1.0):                  # two full anchor episodes
        clock.t += step
        res = ex["exit_fn"](conn, pos, "b_stop_skip", DECISION_AT)
        seen.append(res["status"] if res["status"] != "deferred" else res["reason"])
    assert seen == ["held_anchor", "held_anchor", "dust_route_hold",
                    "held_anchor", "held_anchor", "dust_route_hold", "held_anchor"]
    assert m.get_position(conn, pos["id"])["is_open"]
    assert [p[0] for p in pages] == ["dust_route_hold"]               # one page, no bound page
    assert [r["decision"] for r in _audit(conn)] == [
        "held", "held", "dust_held_at_bound", "held", "held", "dust_held_at_bound", "held"]


def test_hold_s_alone_is_not_the_bound_two_held_attempts_are_needed(env):
    conn, clock, _ = env
    pos = _open_position(conn)
    ex = _executor([1.0, 1.0, 1.0])
    assert ex["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)["reason"] == "held_anchor"
    clock.t += 7.0
    assert ex["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)["reason"] == "held_anchor"
    clock.t += 1.0
    assert ex["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)["status"] == "closed"


def test_an_episode_with_no_attempt_for_a_minute_is_over(env):
    conn, clock, _ = env
    pos = _open_position(conn)
    ex = _executor([1.0, 1.0, 1.0])
    ex["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)
    clock.t += 3.0
    ex["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)
    clock.t += A.EPISODE_GAP_S + 1.0
    assert ex["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)["reason"] == "held_anchor"


def test_the_switch_off_sells_the_low_quote_as_today_and_records_nothing(env, monkeypatch):
    conn, clock, _ = env
    monkeypatch.setenv("MEMECOIN_EVM_EXIT_ANCHOR_CHAINS", "")
    pos = _open_position(conn)
    assert _executor([1.0])["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)["status"] == "closed"
    assert _audit(conn) == []


def test_a_partial_exit_scales_the_anchor_with_the_fraction(env):
    conn, clock, _ = env
    pos = _open_position(conn)
    ex = _executor([2.0, 3.0], proceeds_usd=3.0)       # half the bag: $2 is 0.4/token, $3 is 0.6
    res = ex["derisk_fn"](conn, pos, 0.5, DECISION_AT)
    assert (res["status"], res["reason"]) == ("deferred", "held_anchor")
    assert _audit(conn)[0]["qty"] == 5.0
    clock.t += 1.5
    assert ex["derisk_fn"](conn, pos, 0.5, DECISION_AT)["status"] != "deferred"
    assert [r["decision"] for r in _audit(conn)] == ["held", "sold_recovered"]


# --- the operator: manual close obeys the rule, survives the hold, and can override -------------

def _drain(conn, ex, clock):
    return I.drain(conn, lambda c, p, r, n: ex["exit_fn"](c, p, r, n),
                   lambda c, p, f, n: ex["derisk_fn"](c, p, f, n),
                   now_fn=clock.iso, position_fn=m.get_position)


def test_a_held_manual_close_is_retried_on_the_next_tick_not_dropped(env):
    """Gamma LOW 3 (spec §6): the intent survives `held_anchor` and the executor's bound ends it."""
    conn, clock, _ = env
    pos = _open_position(conn)
    ex = _executor([1.0, 1.0, 1.0])
    iid = I.enqueue(conn, position_id=pos["id"], action="close", now_iso=clock.iso())
    assert _drain(conn, ex, clock) == [(iid, I.QUEUED)]
    assert I.read_status(conn, iid)["attempts"] == 0          # a hold spends no stuck-retry budget
    assert _drain(conn, ex, clock) == []                       # not before the next tick
    clock.t += 1.5
    assert _drain(conn, ex, clock) == [(iid, I.QUEUED)]
    clock.t += 5.0
    assert _drain(conn, ex, clock) == [(iid, I.DONE)]
    assert I.read_status(conn, iid)["result_status"] == "closed"
    assert [r["decision"] for r in _audit(conn)] == ["held", "held", "sold_at_bound"]


def test_sell_at_any_price_sells_a_held_row_at_once(env):
    conn, clock, _ = env
    pos = _open_position(conn)
    ex = _executor([1.0, 1.0])
    assert ex["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)["reason"] == "held_anchor"
    iid = I.enqueue(conn, position_id=pos["id"], action="close_any_price", now_iso=clock.iso())
    assert _drain(conn, ex, clock) == [(iid, I.DONE)]
    assert [r["decision"] for r in _audit(conn)] == ["held", "sold_override"]


def test_an_override_that_never_reached_the_check_does_not_linger(env):
    conn, clock, _ = env
    pos = _open_position(conn)
    ex = _executor([None, None, None, 1.0])                  # the override's ladder finds no route
    I.enqueue(conn, position_id=pos["id"], action="close_any_price", now_iso=clock.iso())
    _drain(conn, ex, clock)
    m.release_position_close_claim(conn, pos["id"], m.get_position(conn, pos["id"]).get("close_claim_id"))
    clock.t += 1.0
    assert ex["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)["reason"] == "held_anchor"


def test_the_board_shows_a_held_row_and_not_a_sold_one(env):
    conn, clock, _ = env
    pos = _open_position(conn)
    ex = _executor([1.0, 15.0])
    ex["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)
    since = "2000-01-01T00:00:00+00:00"
    assert m.open_exit_anchor_holds(conn, since_iso=since)[pos["id"]]["value_usd"] == 1.0
    ex["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)
    assert m.open_exit_anchor_holds(conn, since_iso=since) == {}


# --- the sweep: the sticky anchor ----------------------------------------------------------------

def _tick(conn, clock, pos_id, unit):
    pos = m.get_position(conn, pos_id)
    m.set_position_last_executable(conn, pos_id, unit, clock.iso(), anchor=R._exit_anchor_tick(pos, unit))
    return m.get_position(conn, pos_id)["exit_anchor_price"]


def test_a_passing_preview_moves_the_anchor_and_a_low_one_does_not(env):
    conn, clock, _ = env
    pid = _open_position(conn)["id"]
    assert _tick(conn, clock, pid, 0.8) == 0.8                 # >= 50% of the $1.00 seed
    clock.t += 1.5
    assert _tick(conn, clock, pid, 0.3) == 0.8                 # below 0.40: never moves it
    clock.t += 1.5
    assert _tick(conn, clock, pid, 2.0) == 2.0


def test_a_low_level_that_persists_hold_s_over_two_ticks_becomes_the_anchor(env):
    conn, clock, _ = env
    pid = _open_position(conn)["id"]
    for step in (0.0, 2.0, 2.0):
        clock.t += step
        assert _tick(conn, clock, pid, 0.3) is None
    clock.t += 2.1                                              # 6.1 s, 4 ticks
    assert _tick(conn, clock, pid, 0.25) == 0.25


def test_a_passing_tick_ends_the_low_run(env):
    conn, clock, _ = env
    pid = _open_position(conn)["id"]
    assert _tick(conn, clock, pid, 0.3) is None
    clock.t += 1.0
    assert _tick(conn, clock, pid, 0.9) == 0.9
    clock.t += 5.5
    assert _tick(conn, clock, pid, 0.3) == 0.9                  # a new run, not 6.5 s of one


def test_the_anchor_rides_the_last_executable_update_and_the_switch_off_keeps_todays(env, monkeypatch):
    conn, clock, _ = env
    pid = _open_position(conn)["id"]
    seen = []
    conn.set_trace_callback(lambda sql: seen.append(sql) if sql.lstrip().startswith("UPDATE") else None)
    _tick(conn, clock, pid, 0.8)
    assert len(seen) == 1 and "last_executable_price" in seen[0] and "exit_anchor_price" in seen[0]
    monkeypatch.setenv("MEMECOIN_EVM_EXIT_ANCHOR_CHAINS", "")
    seen.clear()
    _tick(conn, clock, pid, 0.1)
    assert seen == ["UPDATE memecoin_positions SET last_executable_price=0.1, last_executable_at="
                    f"'{clock.iso()}' WHERE id={pid}"]


def test_no_anchor_and_no_entry_price_disables_the_rule():
    assert A.effective_anchor({"exit_anchor_price": None, "entry_price": 0}) is None
    assert A.effective_anchor({"exit_anchor_price": None, "entry_price": 1.5}) == 1.5
    assert A.effective_anchor({"exit_anchor_price": 0.7, "entry_price": 1.5}) == 0.7


def test_one_low_tick_never_re_bases_even_with_no_hold_time(env, monkeypatch):
    """§3.1: the re-base needs >= 2 low ticks as well as HOLD_S, so a lone dislocated tick never
    becomes the anchor however HOLD_S is set."""
    from sniper.settings import memecoin as cfg
    conn, clock, _ = env
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_EXIT_ANCHOR_HOLD_S", 0.0)
    pid = _open_position(conn)["id"]
    assert _tick(conn, clock, pid, 0.3) is None
    assert _tick(conn, clock, pid, 0.3) == 0.3


def test_the_close_route_queues_sell_at_any_price_only_on_an_explicit_true():
    from flask import Flask
    from _web_csrf import csrf_client
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint(
        {"conn": conn, "real_exit_intent_fn": I.make_enqueue_exit_fn(lambda: DECISION_AT)}))
    cl = csrf_client(app)
    pid = _open_position(conn)["id"]
    for body, action in (({"any_price": True}, "close_any_price"), ({"any_price": "yes"}, "close"), ({}, "close")):
        j = cl.post(f"/api/memecoin/positions/{pid}/close", json=body).get_json()
        assert I.read_status(conn, j["intent_id"])["action"] == action


# --- the sweep re-drives a held mirror sell; rails re-nominate on their own ---------------------

def _sweep_ctx(calls, expected_usdc):
    return {"exit_fn": lambda conn, pos, reason, now: calls.append(reason) or {"status": "deferred",
                                                                               "reason": "held_anchor"},
            "preview_exit_fn": lambda pos: {"expected_usdc": expected_usdc},
            "token_safety_fn": lambda mint, **kw: (True, "ok"), "usdc_fn": lambda: 300.0,
            "native_fn": lambda: 1.0, "token_fn": lambda mint: 10.0,
            "confirm_fn": lambda sig: "evm_confirm", "kill_path": None}


def _sweep(conn, calls, expected_usdc=9.0):
    # no display mid, so the sweep buys the exact preview and decides on it, as it does for every
    # exact-policy funded position
    R.real_exit_sweep(conn, {"8453": _sweep_ctx(calls, expected_usdc)},
                      price_fn=lambda mints: {}, now_fn=lambda: DECISION_AT)


def test_the_sweep_re_drives_a_held_mirror_sell_and_writes_the_anchor(env):
    conn, clock, _ = env
    pos = _open_position(conn)
    calls = []
    _sweep(conn, calls)
    assert calls == []                                     # flat: no rail, nothing held
    assert m.get_position(conn, pos["id"])["exit_anchor_price"] == pytest.approx(0.9)
    assert _executor([1.0])["exit_fn"](conn, pos, "mirror", DECISION_AT)["reason"] == "held_anchor"
    clock.t += 1.5
    _sweep(conn, calls)
    assert calls == ["mirror"]


def test_a_held_rail_exit_is_not_re_driven(env):
    conn, clock, _ = env
    pos = _open_position(conn)
    assert _executor([1.0])["exit_fn"](conn, pos, "vol_death", DECISION_AT)["reason"] == "held_anchor"
    calls = []
    clock.t += 1.5
    _sweep(conn, calls)
    assert calls == []


def test_a_mirror_re_drive_ends_with_its_sale_or_its_episode(env):
    conn, clock, _ = env
    pos = _open_position(conn)
    ex = _executor([1.0, 15.0])
    ex["exit_fn"](conn, pos, "mirror", DECISION_AT)
    assert A.redrive_reason(pos["id"]) == "mirror"
    clock.t += A.EPISODE_GAP_S + 1.0
    assert A.redrive_reason(pos["id"]) is None
    pos2 = _open_position(conn, uid="u2")
    ex2 = _executor([1.0, 15.0])
    ex2["exit_fn"](conn, pos2, "mirror", DECISION_AT)
    clock.t += 1.5
    assert ex2["exit_fn"](conn, pos2, "mirror", DECISION_AT)["status"] == "closed"
    assert A.redrive_reason(pos2["id"]) is None



# --- Gamma's review of e0faedf70 ---------------------------------------------------------------

def test_an_attempt_that_crosses_hold_s_mid_quote_is_held_and_the_next_sells_the_largest(env, caplog):
    """MEDIUM 1: only an attempt forced BEFORE its quote asked for the largest answer."""
    conn, clock, _ = env
    pos = _open_position(conn)
    mid_quote = {"advance": 0.0}

    def _ladder_seconds():
        clock.t += mid_quote["advance"]
    ex = _executor([1.0] * 4, during_quote=_ladder_seconds)
    ex["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)                  # held at 0
    clock.t += 3.0
    ex["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)                  # held at 3
    clock.t += 2.9
    mid_quote["advance"] = 0.5                                             # 5.9 -> 6.4 inside the quote
    assert ex["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)["reason"] == "held_anchor"
    mid_quote["advance"] = 0.0
    clock.t += 0.6
    with caplog.at_level(logging.WARNING):
        assert ex["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)["status"] == "closed"
    assert [r["decision"] for r in _audit(conn)] == ["held", "held", "held", "sold_at_bound"]
    assert any("sell ladder dust check fired" in r.message for r in caplog.records)


def test_held_attempts_inside_hold_s_stay_held_however_many(env):
    """MEDIUM 2: the bound needs HOLD_S as well as two held attempts."""
    conn, clock, _ = env
    pos = _open_position(conn)
    ex = _executor([1.0] * 4)
    for _ in range(4):
        assert ex["exit_fn"](conn, pos, "mirror_exit", DECISION_AT)["reason"] == "held_anchor"
        clock.t += 1.5                                                     # 0, 1.5, 3.0, 4.5 s
    assert [r["decision"] for r in _audit(conn)] == ["held"] * 4


def test_sell_at_any_price_survives_an_attempt_that_signed_nothing(env):
    """LOW 3: the override lasts until a sale lands (or the intent's TTL), not one attempt."""
    conn, clock, _ = env
    pos = _open_position(conn)
    ex = _executor([None, None, None, 1.0])                               # first ladder: no route
    iid = I.enqueue(conn, position_id=pos["id"], action="close_any_price", now_iso=clock.iso())
    assert _drain(conn, ex, clock) == [(iid, I.QUEUED)]
    assert A.forced(pos["id"], hold_s=6.0) is None                        # not left on for the rails
    clock.t += 1.5
    assert _drain(conn, ex, clock) == []                                  # the stuck schedule: 2 s
    clock.t += 0.5
    assert _drain(conn, ex, clock) == [(iid, I.DONE)]
    assert I.read_status(conn, iid)["result_status"] == "closed"
    assert [r["decision"] for r in _audit(conn)] == ["sold_override"]


def test_sell_at_any_price_with_no_route_backs_off_until_its_ttl(env):
    """Gamma LOW on d3d96ab9c: an attempt that signed nothing was requeued every 1 s for the whole
    180 s TTL, a full exit ladder each time on a coin with no route. It now follows the stuck
    schedule (2/5/10/30 s) and keeps the last step until the TTL ends the order."""
    conn, clock, _ = env
    pos = _open_position(conn)
    ex = _executor([None] * 400)                                          # never a route
    iid = I.enqueue(conn, position_id=pos["id"], action="close_any_price", now_iso=clock.iso())
    start, runs = clock.t, []
    while clock.t - start <= I.DEFAULT_TTL_S + 5:
        if _drain(conn, ex, clock):
            runs.append(clock.t - start)
        clock.t += 0.5
    assert [b - a for a, b in zip(runs, runs[1:])] == [2.0, 5.0, 10.0] + [30.0] * 5
    assert I.read_status(conn, iid)["state"] == I.EXPIRED                 # the TTL, not a refusal


def test_the_re_drive_honours_a_mirror_hold_set_during_the_anchor_hold(env):
    """LOW 2: mirror_disabled (the operator's per-position hold) stops a re-driven mirror sell."""
    conn, clock, _ = env
    pos = _open_position(conn)
    assert _executor([1.0])["exit_fn"](conn, pos, "mirror", DECISION_AT)["reason"] == "held_anchor"
    conn.execute("UPDATE memecoin_positions SET mirror_disabled=1 WHERE id=?", (pos["id"],))
    conn.commit()
    calls = []
    clock.t += 1.5
    _sweep(conn, calls)
    assert calls == []


def test_the_largest_answer_flag_never_outlives_its_exit(env, caplog):
    """LOW 1: a forced attempt's take-largest flag is cleared, so a later preview of the same mint on
    the same thread takes the ladder's normal answer instead of sitting through the dust wait."""
    conn, clock, _ = env
    pos = _open_position(conn)
    ex = _executor([1.0, 15.0])
    A.request_override(pos["id"])
    try:
        assert ex["exit_fn"](conn, pos, "manual", DECISION_AT)["status"] == "closed"
    finally:
        A.clear_override(pos["id"])
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        ex["preview_exit_fn"](_open_position(conn, uid="u2"))
    assert not any("sell ladder dust check fired" in r.message for r in caplog.records)


def test_the_positions_board_carries_the_hold_only_while_it_is_live(monkeypatch, tmp_path):
    """LOW 4 (served side): /api/memecoin/real/positions puts the latest held row on its position."""
    from flask import Flask
    from _web_csrf import csrf_client
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    from sniper.settings import memecoin as cfg
    from sniper.util.clock import real_utc_now_iso
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"8453"})
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": conn}))
    cl = csrf_client(app)
    held, stale = _open_position(conn)["id"], _open_position(conn, uid="u2")["id"]

    def row(pid, decision, at):
        m.record_exit_anchor_audit(conn, position_id=pid, network_id="8453", observed_at=at,
                                   decision=decision, route="uniswap_v2", value_usd=1.0,
                                   anchor_price=1.0, qty=10.0, held_s=0.0, exit_reason="b_stop",
                                   rungs_json="[]")
    row(held, "held", real_utc_now_iso())
    row(stale, "held", "2026-01-01T00:00:00+00:00")

    def board():
        return {p["id"]: p["anchor_hold"] for p in cl.get("/api/memecoin/real/positions").get_json()["positions"]}
    shown = board()
    assert shown[held]["value_usd"] == 1.0 and shown[held]["exit_reason"] == "b_stop"
    assert shown[stale] is None
    row(held, "sold_at_bound", real_utc_now_iso())
    assert board()[held] is None


@pytest.fixture(autouse=True)
def _generic_usd_quote_fixture(monkeypatch):
    """Generic exit plumbing uses a USD-shaped stand-in; ETH canonical accounting has fork coverage."""
    from sniper.execution import evm_wallet as wallet
    monkeypatch.setitem(wallet.CHAIN_QUOTE, "8453",
                        {"token": wallet.CHAIN_USDC["8453"], "decimals": 6, "kind": "usd"})


def test_the_flap_quote_exit_records_a_dust_held_bound_and_never_pages_it(env, monkeypatch, tmp_path):
    # Gamma MED on 8b61dcfd5: on chain 56's flap.sh quote-token exit the guard's hold reaches the
    # runner as a refused route (status "aborted"), not "dust_route_hold". The anchor's bound row must
    # still read dust_held_at_bound and EXIT-ANCHOR-BOUND must never page (the DAODUN class).
    from sniper.execution import evm_flap_quote_exit as fqe
    from sniper.services import memecoin_exit_dust_guard as G
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(G, "newest_own_mark", lambda *a, **k: None)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_QUOTE_EXIT", True)
    monkeypatch.setenv("MEMECOIN_EVM_EXIT_ANCHOR_CHAINS", "8453,56")
    monkeypatch.setattr(fqe, "prewarm", lambda *a, **k: None)
    conn, clock, pages = env
    pid = m.open_position(conn, source_user_id="u1", source_handle="(real)", mint=MINT, ticker="FLP",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at=DECISION_AT,
                          strategy="alpha_b", execution_mode="real", network_id="56")
    m.insert_fill(conn, position_id=pid, kind="entry", price=1.0, qty=10.0, notional=10.0,
                  fill_source="chain", fraction=1.0, created_at=DECISION_AT, tx_sig="E")

    def runner_exit(conn, position, exit_reason, now_iso, *, run_factory, **kw):
        # The real runner's shape: the route's actual quote goes through quote_check_fn, and a
        # refusal ends the run as "aborted".
        run = run_factory(position, str(tmp_path / ("j%d.json" % position["id"])))
        allowed, code = run.quote_check_fn(position, 0.9, 0.8, 10.0)
        return {"status": "closed"} if allowed else {"status": "aborted", "reason": str(code)}
    monkeypatch.setattr(fqe, "runner_exit", runner_exit)
    ex = build_real_evm_executor(
        acct=Account.create(), chain_id="56", rpc_url="rpc", http=_http,
        venue_quote_fn=lambda *a, **k: None, usdc_fn=lambda: 0.0, token_fn=lambda mint: 10.0)
    seen = []
    for step in (0.0, 3.0, 3.5):
        clock.t += step
        seen.append(ex["exit_fn"](conn, m.get_position(conn, pid), "mirror_exit", DECISION_AT)["reason"])
    assert seen == ["held_anchor", "held_anchor", "dust_route_hold"]
    assert m.get_position(conn, pid)["is_open"]
    assert [r["decision"] for r in _audit(conn)] == ["held", "held", "dust_held_at_bound"]
    assert [p[0] for p in pages] == ["dust_route_hold"]
