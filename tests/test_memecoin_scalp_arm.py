"""`scalp_c`: mcap-conditioned forward-paper arm with an immutable entry policy."""
import json
import math
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_copy_trader import copy_event_to_arms
from sniper.services.memecoin_exits import sweep_exits
from sniper.services.memecoin_policy import (
    ExitPolicy, deserialize_policy, policy_exit_reason, resolve_policy, serialize_policy,
)


def _snap(mcap):
    return {"entry_mcap": mcap}


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _event(mcap=200_000_000):
    return SimpleNamespace(
        side="buy", source_user_id="u1", source_handle="alice", mint="MINT", ticker="T",
        their_price=1.0, their_market_cap=mcap, their_size_usd=2_000.0, is_first_buy=True,
        fomo_event_id="e1", created_at="2026-07-14T20:00:00+00:00",
        received_at="2026-07-14T20:00:01+00:00", received_at_normalized=None,
        lag_s=1.0, signal_source="global_feed", network_id="solana",
    )


def test_big_caps_get_a_scalp_not_a_moonshot():
    pol, pid = resolve_policy(_snap(200_000_000), arm="scalp_c")
    assert pol.tp_pct <= 20
    assert pol.max_hold_s < 6 * 3600
    assert pid.startswith("scalp_c")


def test_small_caps_keep_the_moonshot_rail():
    pol, _ = resolve_policy(_snap(500_000), arm="scalp_c")
    assert pol.tp_pct >= 50


def test_take_profit_falls_monotonically_as_mcap_rises():
    tps = [resolve_policy(_snap(mc), arm="scalp_c")[0].tp_pct
           for mc in (500_000, 3_000_000, 15_000_000, 100_000_000)]
    assert tps == sorted(tps, reverse=True)


def test_unknown_or_invalid_mcap_falls_back_never_refuses():
    for mcap in (None, 0, -1, "bad", float("nan")):
        pol, pid = resolve_policy(_snap(mcap), arm="scalp_c")
        assert pol.tp_pct > 0 and pol.stop_pct < 0
        assert "fallback" in pid


def test_policy_id_names_the_band():
    _, big = resolve_policy(_snap(100_000_000), arm="scalp_c")
    _, small = resolve_policy(_snap(500_000), arm="scalp_c")
    assert big != small


def test_other_arms_are_untouched():
    for arm in ("control", "alpha_b", "real_shadow"):
        before, pid = resolve_policy(_snap(200_000_000), arm=arm)
        assert "scalp" not in pid
        assert before == resolve_policy({}, arm=arm)[0]


def test_policy_json_round_trips_and_encodes_infinite_hold_as_null():
    policy, _ = resolve_policy(_snap(500_000), arm="scalp_c")
    payload = serialize_policy(policy)
    assert json.loads(payload)["max_hold_s"] is None
    assert deserialize_policy(payload) == policy


def test_policy_json_rejects_nonfinite_or_malformed_values():
    base, _ = resolve_policy(_snap(100_000_000), arm="scalp_c")
    with pytest.raises(ValueError, match="tp_pct"):
        serialize_policy(replace(base, tp_pct=float("nan")))
    bad = json.loads(serialize_policy(base))
    bad["stop_pct"] = math.inf
    with pytest.raises(ValueError, match="policy JSON"):
        deserialize_policy(json.dumps(bad))


def test_position_freezes_policy_and_writes_matching_audit_rows(monkeypatch):
    conn = _conn()
    out = copy_event_to_arms(
        conn, _event(), 100.0, "2026-07-14T20:00:01+00:00", arms=("scalp_c",),
        fill_fn=lambda *args, **kwargs: (1.0, "dexscreener"),
        stats_fn=lambda *args, **kwargs: {"market_cap": 190_000_000.0},
    )
    assert out == {"scalp_c": "opened"}
    pos = dict(conn.execute(
        "SELECT * FROM memecoin_positions WHERE strategy='scalp_c'",
    ).fetchone())
    frozen = deserialize_policy(pos["assigned_policy_json"])
    assert pos["entry_mcap"] == 200_000_000.0
    assert pos["assigned_policy_id"].startswith("scalp_c")
    assert pos["assigned_policy_version"] == 1
    assert frozen.tp_pct <= 20

    # Runtime settings can change after entry; the position's exact policy cannot.
    from sniper.settings import memecoin as settings
    monkeypatch.setattr(settings, "MEMECOIN_TAKE_PROFIT_PCT", 999.0)
    assert deserialize_policy(pos["assigned_policy_json"]) == frozen

    version = conn.execute(
        "SELECT parameters_json, immutable_hash FROM memecoin_policy_versions "
        "WHERE policy_id=? AND version=?",
        (pos["assigned_policy_id"], pos["assigned_policy_version"]),
    ).fetchone()
    assert version["parameters_json"] == pos["assigned_policy_json"]
    assert version["immutable_hash"]
    assignment = conn.execute(
        "SELECT policy_id, policy_version, entry_snapshot_id FROM memecoin_policy_assignments "
        "WHERE arm_id='scalp_c'",
    ).fetchone()
    assert assignment["policy_id"] == pos["assigned_policy_id"]
    assert assignment["policy_version"] == 1
    assert assignment["entry_snapshot_id"] is not None


def test_policy_version_id_cannot_be_reused_with_different_parameters():
    conn = _conn()
    policy, pid = resolve_policy(_snap(100_000_000), arm="scalp_c")
    payload = serialize_policy(policy)
    m.record_policy_assignment(
        conn, episode_id="ep1", arm_id="scalp_c", policy_id=pid, policy_version=1,
        parameters_json=payload, entry_snapshot_id=1, assigned_ts="t1",
    )
    changed = serialize_policy(replace(policy, tp_pct=policy.tp_pct + 1))
    with pytest.raises(ValueError, match="immutable policy collision"):
        m.record_policy_assignment(
            conn, episode_id="ep2", arm_id="scalp_c", policy_id=pid, policy_version=1,
            parameters_json=changed, entry_snapshot_id=2, assigned_ts="t2",
        )


def test_later_buyer_in_same_token_episode_reuses_first_frozen_policy():
    conn = _conn()
    first = _event(200_000_000)
    copy_event_to_arms(
        conn, first, 100.0, "2026-07-14T20:00:01+00:00", arms=("scalp_c",),
        fill_fn=lambda *args, **kwargs: (1.0, "dexscreener"), stats_fn=lambda *args, **kwargs: {},
    )
    second = _event(500_000)
    second.fomo_event_id = "e2"
    second.source_user_id = "u2"
    second.source_handle = "bob"
    copy_event_to_arms(
        conn, second, 100.0, "2026-07-14T20:05:01+00:00", arms=("scalp_c",),
        fill_fn=lambda *args, **kwargs: (1.0, "dexscreener"), stats_fn=lambda *args, **kwargs: {},
    )
    rows = conn.execute(
        "SELECT assigned_policy_id, assigned_policy_json FROM memecoin_positions "
        "WHERE strategy='scalp_c' ORDER BY id",
    ).fetchall()
    assert len(rows) == 2
    assert tuple(rows[0]) == tuple(rows[1])
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_policy_assignments WHERE arm_id='scalp_c'",
    ).fetchone()[0] == 1


def test_position_schema_has_frozen_policy_columns():
    conn = _conn()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(memecoin_positions)")}
    assert {"entry_mcap", "assigned_policy_id", "assigned_policy_version",
            "assigned_policy_json"} <= cols


def _open_scalp(conn, *, mcap=200_000_000):
    result = copy_event_to_arms(
        conn, _event(mcap), 100.0, "2026-07-14T20:00:01+00:00", arms=("scalp_c",),
        fill_fn=lambda *args, **kwargs: (1.0, "dexscreener"), stats_fn=lambda *args, **kwargs: {},
    )
    assert result == {"scalp_c": "opened"}
    return conn.execute(
        "SELECT id FROM memecoin_positions WHERE strategy='scalp_c'",
    ).fetchone()[0]


def _prices(price):
    return {("solana", "MINT"): {"price": price, "liquidity": 1e15}}


def test_policy_exit_reason_orders_take_profit_stop_and_timecap():
    policy, _ = resolve_policy(_snap(200_000_000), arm="scalp_c")
    assert policy_exit_reason(policy, policy.tp_pct + 0.1, 1.0) == "take_profit"
    assert policy_exit_reason(policy, policy.stop_pct - 0.1, 1.0) == "stop"
    assert policy_exit_reason(policy, 0.0, policy.max_hold_s + 1) == "timecap"
    assert policy_exit_reason(policy, 0.0, 1.0) is None


@pytest.mark.parametrize(
    ("price", "now_iso", "reason"),
    [
        (1.13, "2026-07-14T20:01:01+00:00", "scalp_take_profit"),
        (0.79, "2026-07-14T20:01:01+00:00", "scalp_stop"),
        (1.00, "2026-07-14T22:00:02+00:00", "scalp_timecap"),
    ],
)
def test_scalp_sweep_closes_on_its_frozen_policy(price, now_iso, reason):
    conn = _conn()
    pid = _open_scalp(conn)
    sweep_exits(conn, _prices(price), now_iso)
    pos = m.get_position(conn, pid)
    assert pos["is_open"] == 0
    assert pos["exit_reason"] == reason


def test_scalp_noquote_timecap_uses_existing_honest_close_path():
    conn = _conn()
    pid = _open_scalp(conn)
    sweep_exits(conn, {}, "2026-07-14T22:00:02+00:00")
    pos = m.get_position(conn, pid)
    assert pos["is_open"] == 0
    assert pos["exit_reason"] == "scalp_timecap_noquote"
    # UPDATED 2026-08-29: a noquote time-cap never priced the exit, so the outcome is unknown.
    assert pos["realized_pnl"] is None


def test_sweep_never_re_resolves_policy_after_entry(monkeypatch):
    conn = _conn()
    pid = _open_scalp(conn)
    import sniper.services.memecoin_policy as policies
    monkeypatch.setattr(
        policies, "_SCALP_BANDS",
        ((0.0, "changed", 999.0, -99.0, 999_999.0),),
    )
    sweep_exits(conn, _prices(1.13), "2026-07-14T20:01:01+00:00")
    assert m.get_position(conn, pid)["exit_reason"] == "scalp_take_profit"


def test_scalp_mirrors_the_trader_sell():
    conn = _conn()
    _open_scalp(conn)
    sell = _event()
    sell.side = "sell"
    sell.fomo_event_id = "sell-1"
    out = copy_event_to_arms(
        conn, sell, 100.0, "2026-07-14T20:05:01+00:00", arms=("scalp_c",),
        stats_fn=lambda *args, **kwargs: {"price": 1.05, "liquidity": 1e15},
    )
    assert out == {"scalp_c": "exit"}
    assert conn.execute(
        "SELECT is_open FROM memecoin_positions WHERE strategy='scalp_c'",
    ).fetchone()[0] == 0


def test_default_arm_roster_includes_the_forward_scalp_experiment():
    from sniper.settings.memecoin import DEFAULT_MEMECOIN_ARMS
    assert DEFAULT_MEMECOIN_ARMS == [
        "control", "momentum_a", "alpha_b", "scalp_c", "scalp_c2", "trail_d", "peak_lock_e",
        "combo_f",
    ]
