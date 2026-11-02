"""Desk origin, build step 1 (spec 2026-10-21-desk-execution-plumbing-design.md §1.5, §2.4-§2.5).

A desk entry rides the manual-intent queue with signal_source='desk'. It skips the SELECTION gates
like a manual buy (the seat is the decision), but unlike manual it keeps the capacity gates, carries
seat rails that code bounds-checks and never clamps, writes its own override authority, and may
never hold a coin another funded lane holds.
"""
from __future__ import annotations

import math
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_manual_intents as mi
from sniper.services import memecoin_origin as origin
import sniper.settings.memecoin as cfg

T0 = "2026-10-21T12:00:00+00:00"
MINT = "DeskMint1111111111111111111111111111111111pump"
BOUNDS = "tp_pct:15-150,sl_pct:10-40,trail_pct:8-30"
GOOD = {"tp_pct": 60.0, "sl_pct": 25.0, "trail_pct": 15.0}


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m._ensure_memecoin_tables_in_transaction(c)
    return c


@pytest.fixture(autouse=True)
def _desk_knobs(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CHAINS", frozenset({"solana"}))
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CLIP_USD", 25.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_MAX_CONCURRENT", 5)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_DAILY_LOSS_USD", 200.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RAIL_BOUNDS", cfg.parse_desk_rail_bounds(BOUNDS))


def _metadata(c):
    cid = m.register_desk_cohort(c, producer="H", policy={"fixture": "execution-foundation", "rail_bounds": cfg.parse_desk_rail_bounds(BOUNDS)},
                                prompt_version="desk-judge-v6", created_at=T0)
    return {"cohort_id": cid, "producer": "H", "opportunity_id": "desk:H:foundation",
            "snapshot_hash": "a" * 64, "verdict_price": VERDICT_PRICE}


# A well-formed S6 cap (memecoin_desk.verdict_price shape): a desk entry without one is refused.
VERDICT_PRICE = {"ref_price_usd": 4.1e-05, "mcap_usd": 41000.0, "liq_usd": 5000.0, "supply_tokens": 1e9,
                 "clip_usd": 25.0, "tolerance_pct": 3.0, "fee_pct": 1.25, "impact_pct": 0.5, "cap_pct": 4.75,
                 "cap_price_usd": 4.1e-05 * 1.0475, "snapshot_at": 1.0, "source": "pumpfun_grpc:curve_reserves",
                 "basis": "emission"}


def _contract(c):
    import json
    return json.dumps({"source": {"desk": _metadata(c)}})


def _book_refusal(c, **kw):
    return origin.desk_book_refusal(c, desk_metadata=_metadata(c), **kw)


def _desk(conn, *, rails=GOOD, size_usd=25.0, ticket="t1", **kw):
    return mi.enqueue(conn, mint=MINT, network_id="solana", now_iso=T0, size_usd=size_usd,
                      rails=rails, rails_mode="custom", signal_source="desk",
                      desk_ticket_id=ticket, **kw, desk_metadata=_metadata(conn))


# ----------------------------------------------------------------- origin classification

def test_desk_bypasses_selection_but_keeps_capacity():
    assert origin.bypasses_selection("desk") and origin.bypasses_selection("manual")
    assert not origin.bypasses_selection("onchain_watch") and not origin.bypasses_selection(None)
    # (a) manual skips concurrency/quiet hours; a desk entry does not.
    assert origin.enforces_capacity("desk") and not origin.enforces_capacity("manual")
    assert origin.enforces_capacity("slot_lane")


def test_runner_and_legs_route_through_the_origin_helpers():
    from sniper.services.memecoin_real_runner import is_manual_entry

    class Ev:
        signal_source = "desk"
    assert is_manual_entry(Ev())   # selection bypass, same as manual
    from sniper.services import memecoin_real_leg, memecoin_evm_leg
    import inspect
    for mod in (memecoin_real_leg, memecoin_evm_leg):
        assert "enforces_capacity(" in inspect.getsource(mod), mod.__name__


def test_desk_is_its_own_analytics_bucket_never_copy():
    c = _conn()
    c.execute("INSERT INTO memecoin_positions(source_user_id, mint, network_id, execution_mode, "
              "is_open, signal_source, funded_contract_json) VALUES (NULL, ?, 'solana', 'real', 1, "
              "'desk', '{\"source\": {\"signal_source\": \"desk\"}}')", (MINT,))
    row = c.execute(f"SELECT ({m.signal_source_sql()}) AS s, ({m.copy_only_sql()}) AS copy "
                    "FROM memecoin_positions").fetchone()
    assert row["s"] == "desk" and row["copy"] == 0


# ----------------------------------------------------------------- rail bounds

def test_rail_bounds_parse_and_fail_closed():
    b = cfg.parse_desk_rail_bounds(BOUNDS)
    assert b == {"tp_pct": (15.0, 150.0), "sl_pct": (10.0, 40.0), "trail_pct": (8.0, 30.0)}
    for bad in ("", "sl_pct:40-10", "sl_pct:x-2", "nope:1-2", "sl_pct:10", "sl_pct:-5-10",
                "tp_pct:15-150"):   # the last one has no sl_pct bound: nothing can pass
        assert cfg.parse_desk_rail_bounds(bad) == {}, bad


@pytest.mark.parametrize("sl", [None, 0, 0.0, -10, 9.99, 40.01, math.nan, math.inf, -math.inf,
                                True, "25"])
def test_required_stop_rejects_every_bad_value(sl):
    rails = dict(GOOD, sl_pct=sl)
    if sl is None:
        rails.pop("sl_pct")
    ok, reason = mi.validate_desk_rails(rails, cfg.MEMECOIN_DESK_RAIL_BOUNDS)
    assert not ok and "sl_pct" in reason


def test_rails_outside_bounds_or_unbounded_are_rejected_not_clamped():
    b = cfg.MEMECOIN_DESK_RAIL_BOUNDS
    assert mi.validate_desk_rails(GOOD, b) == (True, None)
    assert not mi.validate_desk_rails(dict(GOOD, tp_pct=151), b)[0]
    assert not mi.validate_desk_rails(dict(GOOD, trail_pct=7.9), b)[0]
    assert not mi.validate_desk_rails(dict(GOOD, derisk_pct=30), b)[0]      # no bound = not allowed
    assert not mi.validate_desk_rails(dict(GOOD, tp_target_mcap=1e6), b)[0]
    assert not mi.validate_desk_rails({"sl_pct": 25}, {})[0]                 # malformed knob


# ----------------------------------------------------------------- enqueue

def test_desk_intent_freezes_inputs_before_enqueue_returns():
    c = _conn()
    iid = _desk(c)
    row = dict(c.execute("SELECT * FROM memecoin_manual_intents WHERE intent_id=?", (iid,)).fetchone())
    assert row["signal_source"] == "desk" and row["desk_ticket_id"] == "t1"
    assert row["rails_mode"] == "custom" and row["size_usd"] == 25.0
    assert (row["tp_pct"], row["sl_pct"], row["trail_pct"]) == (60.0, 25.0, 15.0)
    assert row["override"] == 0


def test_a_manual_intent_is_unchanged():
    c = _conn()
    iid = mi.enqueue(c, mint=MINT, network_id="solana", now_iso=T0)
    row = c.execute("SELECT signal_source, desk_ticket_id FROM memecoin_manual_intents "
                    "WHERE intent_id=?", (iid,)).fetchone()
    assert (row["signal_source"], row["desk_ticket_id"]) == ("manual", None)


@pytest.mark.parametrize("kw, why", [
    ({"rails": dict(GOOD, sl_pct=50)}, "sl_pct"),
    ({"override": True}, "override"),
    ({"size_usd": 25.01}, "clip"),
    ({"size_usd": None, "size_pct_equity": 0.1}, "size"),
    ({"ticket": None}, "ticket"),
])
def test_desk_enqueue_refuses(kw, why):
    c = _conn()
    with pytest.raises(ValueError, match=why):
        _desk(c, **kw)
    assert c.execute("SELECT COUNT(*) FROM memecoin_manual_intents").fetchone()[0] == 0


def test_desk_enqueue_refuses_when_disabled_or_off_chain(monkeypatch):
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_ENABLED", False)
    with pytest.raises(ValueError, match="disabled"):
        _desk(c)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_ENABLED", True)
    with pytest.raises(ValueError, match="chain"):
        mi.enqueue(c, mint="0x" + "ab" * 20, network_id="8453", now_iso=T0, size_usd=25.0,
                   rails=GOOD, rails_mode="custom", signal_source="desk", desk_ticket_id="t9", desk_metadata=_metadata(c))


def test_desk_requires_custom_rails_mode():
    c = _conn()
    with pytest.raises(ValueError, match="custom"):
        mi.enqueue(c, mint=MINT, network_id="solana", now_iso=T0, size_usd=25.0, rails=GOOD,
                   signal_source="desk", desk_ticket_id="t1", desk_metadata=_metadata(c))


# ----------------------------------------------------------------- drain + R1

def _open_position(c, pid=4242, signal_source="desk", uid=None, mint=MINT, network="solana"):
    c.execute("INSERT INTO memecoin_positions(id, source_user_id, mint, network_id, execution_mode, "
              "is_open, signal_source, entry_notional, opened_at) "
              "VALUES (?, ?, ?, ?, 'real', 1, ?, 25.0, ?)", (pid, uid, mint, network, signal_source, T0))


def test_drain_passes_the_desk_origin_to_the_buy_fn():
    c = _conn()
    _desk(c)
    seen = {}

    def buy_fn(conn, mint, uid, **kw):
        seen.update(kw)
        return {"status": "refused", "reason": "test"}
    mi.drain(c, buy_fn, now_fn=lambda: T0)
    assert seen["signal_source"] == "desk" and seen["desk_ticket_id"] == "t1"
    assert seen["size_usd"] == 25.0


def test_manual_drain_does_not_grow_new_kwargs():
    c = _conn()
    mi.enqueue(c, mint=MINT, network_id="solana", now_iso=T0)
    seen = {}

    def buy_fn(conn, mint, uid, **kw):
        seen.update(kw)
        return {"status": "refused"}
    mi.drain(c, buy_fn, now_fn=lambda: T0)
    assert "signal_source" not in seen and "desk_ticket_id" not in seen


def test_desk_rails_arm_with_desk_authority():
    c = _conn()
    iid = _desk(c)
    _open_position(c)
    mi.drain(c, lambda *a, **k: {"status": "filled", "position_id": 4242}, now_fn=lambda: T0)
    ov = m.effective_exit_override(c, 4242)
    assert ov["source"] == "desk" and ov["actor"] == "desk:t1"
    assert (ov["sl_pct"], ov["tp_pct"], ov["trail_pct"], ov["derisk_pct"]) == (25.0, 60.0, 15.0, 0.0)
    assert mi.read_status(c, iid)["state"] == mi.FILLED


def test_desk_fill_whose_rails_fail_is_refused_and_closed(monkeypatch):
    """(h) R1: a terminally filled desk intent with no rails is not allowed to exist."""
    c = _conn()
    iid = _desk(c)
    _open_position(c)

    def boom(*a, **k):
        raise RuntimeError("override write failed")
    monkeypatch.setattr(mi, "apply_rails", boom)
    mi.drain(c, lambda *a, **k: {"status": "filled", "position_id": 4242}, now_fn=lambda: T0)
    row = c.execute("SELECT state, reason, position_id FROM memecoin_manual_intents "
                    "WHERE intent_id=?", (iid,)).fetchone()
    assert row["state"] == mi.REFUSED and row["reason"].startswith("desk_rails_not_armed")
    assert row["position_id"] == 4242
    close = c.execute("SELECT position_id, action, actor FROM memecoin_manual_exit_intents").fetchone()
    assert (close["position_id"], close["action"], close["actor"]) == (4242, "close", "desk:t1")


# ----------------------------------------------------------------- R2 authority

def test_desk_ranks_directly_after_operator():
    # desk_risk (RISK-live TIGHTEN, built stricter from the desk row) sits between the two
    assert m._EXIT_OVERRIDE_PRIORITY[:3] == ["operator", "desk_risk", "desk"]
    c = _conn()
    _open_position(c)
    m.set_position_exit_override(c, 4242, "micro", sl_pct=50, actor="micro", now_iso=T0)
    m.set_position_exit_override(c, 4242, "auto-classify", sl_pct=60, actor="a", now_iso=T0)
    m.set_position_exit_override(c, 4242, "desk", sl_pct=20, actor="desk:t1", now_iso=T0)
    assert m.effective_exit_override(c, 4242)["source"] == "desk"
    m.set_position_exit_override(c, 4242, "operator", sl_pct=35, actor="operator", now_iso=T0)
    assert m.effective_exit_override(c, 4242)["source"] == "operator"


def test_auto_classify_never_writes_on_a_desk_position():
    import inspect
    from sniper.services import memecoin_entry_classifier
    assert "bypasses_selection(" in inspect.getsource(memecoin_entry_classifier)


# ----------------------------------------------------------------- R3 one coin, one lane

def _reserve(c, *, signal_source, uid=None, mint=MINT, network="solana"):
    return m.reserve_pending(c, mint=mint, source_user_id=uid, strategy="control", notional=25.0,
                             reserved_at=T0, network_id=network, signal_source=signal_source, funded_contract_json=_contract(c))


def test_desk_cannot_reserve_a_coin_a_copy_position_holds():
    c = _conn()
    _open_position(c, pid=1, signal_source="onchain_watch", uid="wallet-a")
    assert _reserve(c, signal_source="desk") is None


def test_copy_cannot_reserve_a_coin_a_desk_position_holds():
    c = _conn()
    _open_position(c, pid=1, signal_source="desk")
    assert _reserve(c, signal_source="slot_lane", uid="wallet-a") is None


def test_copy_on_copy_is_unchanged():
    c = _conn()
    _open_position(c, pid=1, signal_source="onchain_watch", uid="wallet-a")
    assert _reserve(c, signal_source="onchain_watch", uid="wallet-b") is not None


def test_unresolved_reservation_blocks_both_ways():
    c = _conn()
    assert _reserve(c, signal_source="slot_lane", uid="wallet-a") is not None
    assert _reserve(c, signal_source="desk") is None
    c2 = _conn()
    assert _reserve(c2, signal_source="desk") is not None
    assert _reserve(c2, signal_source="slot_lane", uid="wallet-a") is None


def test_evm_identity_is_case_blind_and_solana_is_exact():
    c = _conn()
    evm = "0x" + "Ab" * 20
    _open_position(c, pid=1, signal_source="desk", mint=evm.lower(), network="8453")
    assert _reserve(c, signal_source="onchain_watch", uid="w", mint=evm, network="8453") is None
    c2 = _conn()
    _open_position(c2, pid=1, signal_source="desk", mint=MINT.lower())
    assert _reserve(c2, signal_source="onchain_watch", uid="w") is not None


def test_null_source_user_id_never_doubles_a_desk_coin():
    """Desk rows carry no wallet. SQLite's UNIQUE treats NULLs as distinct, so this must hold
    through the coin guards, not the per-wallet open index."""
    c = _conn()
    assert _reserve(c, signal_source="desk") is not None
    assert _reserve(c, signal_source="desk") is None           # reserved coin guard
    c2 = _conn()
    _open_position(c2, pid=1, signal_source="desk")
    assert _reserve(c2, signal_source="desk") is None          # open-position guard


# ----------------------------------------------------------------- desk book limits

def test_desk_book_counts_and_loss_since_midnight():
    c = _conn()
    _open_position(c, pid=1)
    _open_position(c, pid=2, signal_source="onchain_watch", uid="w", mint="other")
    _reserve(c, signal_source="desk", mint="third")
    assert m.desk_open_count(c) == 2
    c.execute("INSERT INTO memecoin_positions(id, mint, network_id, execution_mode, is_open, "
              "signal_source, realized_pnl, closed_at) VALUES (3, 'x', 'solana', 'real', 0, 'desk', "
              "-150.0, '2026-10-21T01:00:00+00:00'), (4, 'y', 'solana', 'real', 0, 'desk', -999.0, "
              "'2026-10-20T23:59:00+00:00'), (5, 'z', 'solana', 'real', 0, 'onchain_watch', -500.0, "
              "'2026-10-21T02:00:00+00:00')")
    assert m.desk_realized_pnl_since(c, "2026-10-21T00:00:00+00:00") == -150.0


def test_desk_limits_refuse_before_any_entry_work():
    c = _conn()
    for i in range(5):
        _open_position(c, pid=10 + i, mint=f"m{i}")
    assert _book_refusal(c, now_iso=T0) == "desk_max_concurrent"
    c2 = _conn()
    c2.execute("INSERT INTO memecoin_positions(id, mint, network_id, execution_mode, is_open, "
               "signal_source, realized_pnl, closed_at) VALUES (3, 'x', 'solana', 'real', 0, 'desk', "
               "-200.0, '2026-10-21T01:00:00+00:00')")
    assert _book_refusal(c2, now_iso=T0) == "desk_daily_loss"
    assert _book_refusal(_conn(), now_iso=T0) is None


# ----------------------------------------------------------------- end to end on a test ledger

def test_one_desk_intent_end_to_end_at_the_smallest_clip(monkeypatch):
    """Hand-written desk intent -> drain -> make_real_buy_fn -> real_entry_hook -> executor stub.
    The position is a desk position with no wallet, its contract keeps the catastrophe floor, and
    the seat's rails arm under desk authority."""
    import json
    from sniper.services import memecoin_arming
    from sniper.services import memecoin_real_runner as runner
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(runner, "_alert_entry_result", lambda *a, **k: None)
    clip = float(cfg.min_trade_floor_usd_for("solana"))
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CLIP_USD", clip)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    spent = []

    def execute(conn, pending_id, mint, size, now_iso, impact_cap_pct=None, **kw):
        spent.append(size)
        row = dict(conn.execute("SELECT * FROM memecoin_pending_entries WHERE id=?",
                                (pending_id,)).fetchone())
        pid = m.open_position(
            conn, source_user_id=row["source_user_id"], source_handle="(real)", mint=mint,
            ticker="DSK", entry_price=0.0001, entry_notional=size, entry_fill_source="chain",
            entry_lag_s=0.0, their_size_usd=0.0, opened_at=now_iso, strategy=row["strategy"],
            execution_mode="real", network_id="solana", signal_source=row["signal_source"],
            funded_contract_json=row["funded_contract_json"])
        conn.execute("UPDATE memecoin_pending_entries SET status='confirmed' WHERE id=?",
                     (pending_id,))
        return {"status": "filled", "position_id": pid}

    contexts = {"solana": {"usdc_fn": lambda: 300.0, "sol_fn": lambda: 1.0,
                           "token_safety_fn": lambda mint, **kw: (True, "ok"),
                           "execute_fn": execute, "kill_path": None}}
    stats = lambda mint, network_id=None: {"market_cap": 60_000.0, "price": 0.0001,
                                           "liquidity_usd": 20_000.0}
    from sniper.util.clock import real_utc_now_iso
    iid = mi.enqueue(c, mint=MINT, network_id="solana", now_iso=real_utc_now_iso(),
                     size_usd=clip, rails=GOOD, rails_mode="custom", signal_source="desk",
                     desk_ticket_id="e2e", desk_metadata=_metadata(c))
    done = mi.drain(c, runner.make_real_buy_fn(contexts, stats_fn=stats), now_fn=real_utc_now_iso)
    status = mi.read_status(c, iid)
    assert done == [(iid, mi.FILLED)], status
    assert spent == [clip]
    pos = dict(c.execute("SELECT * FROM memecoin_positions WHERE id=?",
                         (status["position_id"],)).fetchone())
    assert pos["signal_source"] == "desk" and pos["source_user_id"] is None
    # The funded exit sweep selects open positions by strategy == MEMECOIN_REAL_POLICY
    # (real_exit_sweep). A desk-only strategy name would leave the position with no exits at all.
    assert pos["strategy"] == cfg.MEMECOIN_REAL_POLICY
    policy = json.loads(pos["funded_contract_json"])["policy"]
    assert policy["policy_id"] != "manual-unmanaged-v1" and float(policy["catastrophe_pct"]) > 0
    ov = m.effective_exit_override(c, pos["id"])
    assert (ov["source"], ov["actor"], ov["sl_pct"]) == ("desk", "desk:e2e", 25.0)
    row = c.execute(f"SELECT ({m.signal_source_sql()}) FROM memecoin_positions WHERE id=?",
                    (pos["id"],)).fetchone()
    assert row[0] == "desk"
