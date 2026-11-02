import json
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_policy as policy
from sniper.services.memecoin_copy_trader import copy_event_to_arms
from sniper.services.memecoin_exits import paper_unquotable_cleanup, sweep_exits
from sniper.settings import memecoin as settings


ALL_ARMS = (
    "control", "momentum_a", "alpha_b", "real_shadow", "scalp_c", "scalp_c2",
    "trail_d", "peak_lock_e", "combo_f",
)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _event(event_id="event-1"):
    return SimpleNamespace(
        side="buy", source_user_id="u1", source_handle="alice", mint="MINT", ticker="T",
        their_price=1.0, their_market_cap=2_000_000.0, their_size_usd=2_000.0,
        is_first_buy=True, fomo_event_id=event_id,
        created_at="2026-07-28T00:00:00+00:00",
        received_at="2026-07-28T00:00:01+00:00", received_at_normalized=None,
        lag_s=1.0, signal_source="global_feed", network_id="solana",
    )


def test_every_paper_arm_resolves_a_canonical_frozen_policy():
    resolved = {}
    for arm in ALL_ARMS:
        policy_id, version, payload = policy.resolve_paper_arm_policy(
            arm, entry_mcap=2_000_000.0, network_id="solana",
            opened_at="2026-07-28T00:00:01+00:00",
        )
        resolved[arm] = json.loads(payload)
        assert policy_id and version == 1
        assert payload == json.dumps(resolved[arm], sort_keys=True, separators=(",", ":"),
                                     allow_nan=False)
    assert set(resolved) == set(ALL_ARMS)


def test_policy_identity_is_stable_across_entry_times():
    first = policy.resolve_paper_arm_policy(
        "momentum_a", entry_mcap=2_000_000.0, network_id="solana",
        opened_at="2026-07-28T00:00:01+00:00",
    )
    later = policy.resolve_paper_arm_policy(
        "momentum_a", entry_mcap=2_000_000.0, network_id="solana",
        opened_at="2026-07-29T00:00:01+00:00",
    )
    assert later == first
    assert "entry_context" not in json.loads(first[2])


def test_policy_identity_changes_with_frozen_parameters(monkeypatch):
    monkeypatch.setattr(settings, "MEMECOIN_A_TRAIL_PCT", 15.0)
    first = policy.resolve_paper_arm_policy("momentum_a")
    monkeypatch.setattr(settings, "MEMECOIN_A_TRAIL_PCT", 16.0)
    changed = policy.resolve_paper_arm_policy("momentum_a")
    assert changed[0] != first[0]
    assert changed[2] != first[2]

    conn = _conn()
    for episode_id, assignment in (("episode-1", first), ("episode-2", changed)):
        m.record_policy_assignment(
            conn, episode_id=episode_id, arm_id="momentum_a",
            policy_id=assignment[0], policy_version=assignment[1],
            parameters_json=assignment[2], entry_snapshot_id=None,
            assigned_ts="2026-07-28T00:00:01+00:00",
        )
    assert conn.execute("SELECT COUNT(*) FROM memecoin_policy_versions").fetchone()[0] == 2


def test_policy_validator_rejects_non_object_json():
    assert policy.paper_policy_is_valid({
        "strategy": "control",
        "assigned_policy_json": "[]",
    }) is False


def test_paper_cohort_hash_changes_with_a_rail_parameter(monkeypatch):
    arms = ("control", "alpha_b")
    first_id, first_json = policy.build_paper_cohort(arms)
    same_id, same_json = policy.build_paper_cohort(arms)
    assert first_id == same_id and first_json == same_json
    monkeypatch.setattr(settings, "MEMECOIN_B_STOP_PCT", 999.0)
    assert policy.build_paper_cohort(arms)[0] != first_id


def test_paper_cohort_is_one_id_across_mcap_bands_and_chains():
    """The bug this replaced: scalp_c2's mcap band and real_shadow's per-chain stop leaked into
    cohort identity, so every (band x chain) pair minted its own one-event cohort and the forward
    A/B table — which keeps a single cohort — was permanently empty."""
    arms = ALL_ARMS
    baseline = policy.build_paper_cohort(arms)[0]
    for entry_mcap in (None, 500_000.0, 2_000_000.0, 9_000_000.0, 50_000_000.0):
        for network_id in ("solana", "8453", "4663", "56", "1"):
            resolved = {
                arm: policy.resolve_paper_arm_policy(
                    arm, entry_mcap=entry_mcap, network_id=network_id,
                    opened_at="2026-07-28T00:00:00+00:00",
                ) for arm in arms
            }
            # the resolved per-row policy still varies with the coin — that is the point
            assert all(payload for _id, _v, payload in resolved.values())
            assert policy.build_paper_cohort(arms)[0] == baseline


def test_paper_cohort_ignores_the_release_sha_and_arm_order():
    """A deploy is not a new experiment. Release identity churned per deploy and reset the cohort
    before the evidence floor could ever be reached; it lives in policy_versions.code_commit."""
    contract = json.loads(policy.build_paper_cohort(("control", "alpha_b"))[1])
    assert "code_sha" not in contract
    assert policy.build_paper_cohort(("control", "alpha_b"))[0] == policy.build_paper_cohort(
        ("alpha_b", "control"),
    )[0]


def test_entry_fanout_persists_one_cohort_and_a_policy_for_every_arm():
    conn = _conn()
    arms = ("control", "alpha_b", "trail_d")
    result = copy_event_to_arms(
        conn, _event(), 100.0, "2026-07-28T00:00:01+00:00", arms=arms,
        fill_fn=lambda *_a, **_k: (1.01, "dexscreener"),
        stats_fn=lambda *_a, **_k: {"price": 1.0, "liquidity": 100_000.0},
    )
    assert result == {arm: "opened" for arm in arms}
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM memecoin_positions ORDER BY strategy",
    )]
    assert len(rows) == len(arms)
    assert {row["strategy"] for row in rows} == set(arms)
    assert len({row["paper_cohort_id"] for row in rows}) == 1
    assert len({row["paper_cohort_json"] for row in rows}) == 1
    assert {row["experiment_status"] for row in rows} == {"eligible"}
    assert all(row["assigned_policy_id"] for row in rows)
    assert all(row["assigned_policy_version"] == 1 for row in rows)
    assert all(json.loads(row["assigned_policy_json"]) for row in rows)


def test_paper_entry_position_and_fill_are_atomic(monkeypatch):
    conn = _conn()

    def fail_fill(*_args, **_kwargs):
        raise RuntimeError("injected fill crash")

    monkeypatch.setattr(m, "insert_fill", fail_fill)
    with pytest.raises(RuntimeError, match="injected fill crash"):
        copy_event_to_arms(
            conn, _event(), 100.0, "2026-07-28T00:00:01+00:00", arms=("control",),
            fill_fn=lambda *_a, **_k: (1.0, "dexscreener"), stats_fn=lambda *_a, **_k: {},
        )
    assert conn.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 0


def test_fomo_fallback_is_visible_but_not_experiment_eligible():
    conn = _conn()
    result = copy_event_to_arms(
        conn, _event(), 100.0, "2026-07-28T00:00:01+00:00", arms=("control",),
        fill_fn=lambda *_a, **_k: (1.0, "fomo"), stats_fn=lambda *_a, **_k: {},
    )
    assert result == {"control": "opened"}
    row = dict(conn.execute("SELECT * FROM memecoin_positions").fetchone())
    assert row["experiment_status"] == "excluded_fill"
    assert row["paper_cohort_id"] and row["assigned_policy_json"]


def _open_frozen(arm):
    conn = _conn()
    result = copy_event_to_arms(
        conn, _event(), 100.0, "2026-07-28T00:00:01+00:00", arms=(arm,),
        fill_fn=lambda *_a, **_k: (1.0, "dexscreener"), stats_fn=lambda *_a, **_k: {},
    )
    assert result == {arm: "opened"}
    return conn, dict(conn.execute("SELECT * FROM memecoin_positions").fetchone())


def _prices(price):
    return {("solana", "MINT"): {"price": price, "liquidity": 1e15}}


def test_control_uses_entry_time_take_profit_after_settings_change(monkeypatch):
    monkeypatch.setattr(settings, "MEMECOIN_TAKE_PROFIT_PCT", 50.0)
    conn, pos = _open_frozen("control")
    monkeypatch.setattr(settings, "MEMECOIN_TAKE_PROFIT_PCT", 5.0)
    sweep_exits(conn, _prices(1.10), "2026-07-28T00:00:15+00:00")
    assert m.get_position(conn, pos["id"])["is_open"] == 1


def test_momentum_uses_entry_time_rollover_after_settings_change(monkeypatch):
    monkeypatch.setattr(settings, "MEMECOIN_A_ROLLOVER_SWEEPS", 5)
    conn, pos = _open_frozen("momentum_a")
    monkeypatch.setattr(settings, "MEMECOIN_A_ROLLOVER_SWEEPS", 1)
    sweep_exits(conn, _prices(1.10), "2026-07-28T00:00:15+00:00")
    sweep_exits(conn, _prices(1.09), "2026-07-28T00:00:30+00:00")
    assert m.get_position(conn, pos["id"])["is_open"] == 1


def test_alpha_uses_entry_time_hold_after_settings_change(monkeypatch):
    monkeypatch.setattr(settings, "MEMECOIN_B_MAX_HOLD_S", 3600.0)
    conn, pos = _open_frozen("alpha_b")
    monkeypatch.setattr(settings, "MEMECOIN_B_MAX_HOLD_S", 1.0)
    sweep_exits(conn, _prices(1.0), "2026-07-28T00:00:10+00:00")
    assert m.get_position(conn, pos["id"])["is_open"] == 1


def test_trail_uses_entry_time_activation_after_settings_change(monkeypatch):
    monkeypatch.setattr(settings, "MEMECOIN_TRAIL_D_ACTIVATE_PCT", 50.0)
    monkeypatch.setattr(settings, "MEMECOIN_TRAIL_D_TRAIL_PCT", 5.0)
    conn, pos = _open_frozen("trail_d")
    monkeypatch.setattr(settings, "MEMECOIN_TRAIL_D_ACTIVATE_PCT", 5.0)
    sweep_exits(conn, _prices(1.20), "2026-07-28T00:00:15+00:00")
    sweep_exits(conn, _prices(1.10), "2026-07-28T00:00:30+00:00")
    assert m.get_position(conn, pos["id"])["is_open"] == 1


def test_peak_lock_uses_entry_time_stall_count_after_settings_change(monkeypatch):
    monkeypatch.setattr(settings, "MEMECOIN_PEAK_E_ACTIVATE_PCT", 5.0)
    monkeypatch.setattr(settings, "MEMECOIN_PEAK_E_STALL_SWEEPS", 5)
    conn, pos = _open_frozen("peak_lock_e")
    monkeypatch.setattr(settings, "MEMECOIN_PEAK_E_STALL_SWEEPS", 1)
    sweep_exits(conn, _prices(1.10), "2026-07-28T00:00:15+00:00")
    sweep_exits(conn, _prices(1.10), "2026-07-28T00:00:30+00:00")
    assert m.get_position(conn, pos["id"])["is_open"] == 1


def test_combo_uses_entry_time_partial_trigger_after_settings_change(monkeypatch):
    monkeypatch.setattr(settings, "MEMECOIN_COMBO_F_TRIGGER_PCT", 50.0)
    conn, pos = _open_frozen("combo_f")
    monkeypatch.setattr(settings, "MEMECOIN_COMBO_F_TRIGGER_PCT", 5.0)
    sweep_exits(conn, _prices(1.10), "2026-07-28T00:00:15+00:00")
    current = m.get_position(conn, pos["id"])
    assert current["is_open"] == 1 and current["derisked_at"] is None
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? AND kind='partial_exit'",
        (pos["id"],),
    ).fetchone()[0] == 0


def test_real_shadow_uses_entry_time_take_profit_after_settings_change(monkeypatch):
    monkeypatch.setattr(settings, "MEMECOIN_REAL_TP_PCT", 50.0)
    conn, pos = _open_frozen("real_shadow")
    monkeypatch.setattr(settings, "MEMECOIN_REAL_TP_PCT", 5.0)
    sweep_exits(conn, _prices(1.10), "2026-07-28T00:00:15+00:00")
    assert m.get_position(conn, pos["id"])["is_open"] == 1


def test_uniform_hold_uses_entry_time_cap_after_settings_change(monkeypatch):
    monkeypatch.setattr(settings, "MEMECOIN_PAPER_MAX_HOLD_D", 14.0)
    conn, pos = _open_frozen("control")
    monkeypatch.setattr(settings, "MEMECOIN_PAPER_MAX_HOLD_D", 0.0001)
    sweep_exits(conn, _prices(1.0), "2026-07-28T00:00:20+00:00")
    assert m.get_position(conn, pos["id"])["is_open"] == 1


def test_malformed_forward_policy_is_durably_excluded_before_fallback():
    conn, pos = _open_frozen("control")
    conn.execute(
        "UPDATE memecoin_positions SET assigned_policy_json='{' WHERE id=?", (pos["id"],),
    )
    conn.commit()
    sweep_exits(conn, _prices(1.0), "2026-07-28T00:00:15+00:00")
    assert m.get_position(conn, pos["id"])["experiment_status"] == "invalid_policy"


def test_unquotable_cleanup_uses_each_rows_frozen_threshold(monkeypatch):
    conn = _conn()
    ids = []
    for index, threshold in enumerate((3600.0, 10800.0)):
        ev = _event(f"event-{index}")
        ev.mint = f"MINT-{index}"
        copy_event_to_arms(
            conn, ev, 100.0, "2026-07-28T00:00:00+00:00", arms=("control",),
            fill_fn=lambda *_a, **_k: (1.0, "dexscreener"), stats_fn=lambda *_a, **_k: {},
        )
        row = dict(conn.execute(
            "SELECT * FROM memecoin_positions WHERE mint=?", (ev.mint,),
        ).fetchone())
        payload = json.loads(row["assigned_policy_json"])
        payload["params"]["paper_unquotable_s"] = threshold
        conn.execute(
            "UPDATE memecoin_positions SET assigned_policy_json=? WHERE id=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), row["id"]),
        )
        ids.append(row["id"])
    conn.commit()
    monkeypatch.setattr(settings, "MEMECOIN_PAPER_UNQUOTABLE_S", 1.0)
    closed = dict(paper_unquotable_cleanup(conn, "2026-07-28T02:00:00+00:00"))
    assert ids[0] in closed and ids[1] not in closed


def test_sell_mirror_uses_frozen_row_policy_not_static_arm_tuple(monkeypatch):
    monkeypatch.setattr(settings, "MEMECOIN_REAL_MIRROR_SIZE_CHAINS", frozenset({"solana"}))

    def forbidden(*args, **kwargs):
        pytest.fail("funded size overlay overrode the frozen paper mirror flag")

    monkeypatch.setattr("sniper.services.memecoin_real_runner.source_sell_share", forbidden)
    conn, pos = _open_frozen("control")
    payload = json.loads(pos["assigned_policy_json"])
    payload["params"]["mirror_on_sell"] = False
    conn.execute(
        "UPDATE memecoin_positions SET assigned_policy_json=? WHERE id=?",
        (json.dumps(payload, sort_keys=True, separators=(",", ":")), pos["id"]),
    )
    conn.commit()
    sell = _event()
    sell.side = "sell"
    result = copy_event_to_arms(
        conn, sell, 100.0, "2026-07-28T00:00:20+00:00", arms=("control",),
        stats_fn=lambda *_a, **_k: {"price": 1.1, "liquidity": 100_000.0},
    )
    assert result == {"control": "ignored"}
    assert m.get_position(conn, pos["id"])["is_open"] == 1


def test_paper_entry_persists_observed_provenance_without_fabricating_route_identity():
    conn = _conn()
    result = copy_event_to_arms(
        conn, _event(), 100.0, "2026-07-28T00:00:01+00:00", arms=("control",),
        stats_fn=lambda *_a, **_k: {"price": 1.02, "liquidity": 10_000.0},
        # the fill is an EXECUTABLE route quote now, so it is injected like every other seam;
        # 1.0302 is what a route would return for the 1.02 mid this stats_fn reports.
        quote_fn=lambda *_a, **_k: (1.0302, "route_quote"),
    )
    assert result == {"control": "opened"}
    pos = dict(conn.execute("SELECT * FROM memecoin_positions").fetchone())
    fill = dict(conn.execute("SELECT * FROM memecoin_fills WHERE kind='entry'").fetchone())
    # ⚠️ `quote_price` IS THE ROUTE QUOTE, NOT THE MID (changed 2026-09-24). It used to hold the
    # DexScreener mid here while the funded path held the route quote it signs against, so
    # `fill/quote - 1` measured a mid-to-executable spread on paper (11.74% mean over 228 rows) and
    # true execution slippage on funded (0.86%), and the two were read as the same quantity. The
    # mid is still recorded, in `memecoin_entry_snapshots.features_json["price"]`, which is its
    # own field rather than a column shared with a different meaning.
    assert pos["their_price"] == 1.0 and pos["quote_price"] == pytest.approx(1.0302)
    # The drift is now quote-against-source on both sides and therefore comparable: 3.02%, not the
    # 2.0% the mid produced.
    assert pos["source_drift_pct"] == pytest.approx(3.02)
    # ABSENT, NOT ZERO: paper never executes, so it has no fill to compare against its quote.
    assert pos["execution_slippage_pct"] is None
    assert pos["total_copy_drift_pct"] == pytest.approx(pos["fill_gap_pct"])
    assert pos["quote_id"] is None and pos["quote_route_target"] is None
    assert pos["quote_age_ms"] is None and pos["quote_observed_at"] is None
    assert fill["source_price"] == 1.0 and fill["quote_price"] == pytest.approx(1.0302)
    assert fill["origin_event_id"] == "event-1" and fill["network_id"] == "solana"


def test_paper_entry_refuses_to_book_a_fill_without_an_executable_quote():
    """The measurement bug this closes: the old path fell back to a DexScreener mid, which booked
    a median -0.35% fill gap against the copied trader (paper claimed we filled BETTER than the
    wallet) while the funded leg measured +7.54% WORSE on the same events. An unpriceable event
    must be NO evidence, never fabricated evidence."""
    conn = _conn()
    result = copy_event_to_arms(
        conn, _event(), 100.0, "2026-07-28T00:00:01+00:00", arms=("control", "alpha_b"),
        stats_fn=lambda *_a, **_k: {"price": 1.02, "liquidity": 10_000.0},
        quote_fn=lambda *_a, **_k: (None, "quote_unavailable"),
    )
    assert result == {"control": "skipped_no_executable_quote",
                      "alpha_b": "skipped_no_executable_quote"}
    assert conn.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_fills").fetchone()[0] == 0


def test_fill_model_is_cohort_identity_so_the_two_pricings_never_pool():
    """Mid-priced and route-priced rows measure different things; one mean over both is meaningless."""
    contract = json.loads(policy.build_paper_cohort(("control",), notional_usd=100.0)[1])
    assert contract["fill_model"] == "jupiter-executable-route-v2"
    import sniper.services.memecoin_copy_trader as ct
    original = ct.PAPER_FILL_MODEL
    try:
        ct.PAPER_FILL_MODEL = "dexscreener-mid-impact-v1"          # the superseded model
        legacy_id = policy.build_paper_cohort(("control",), notional_usd=100.0)[0]
    finally:
        ct.PAPER_FILL_MODEL = original
    assert legacy_id != policy.build_paper_cohort(("control",), notional_usd=100.0)[0]


def test_paper_exit_without_an_executable_quote_closes_as_noquote_and_is_censored():
    """Exit twin of the entry guarantee. Pricing an exit off a stale mid let paper 'sell' a
    dumping coin above where a route would fill, compounding the entry optimism on the same
    trade. No quote -> a NOQUOTE close the report censors, never a fabricated price."""
    from sniper.services import memecoin_exits
    from sniper.services.memecoin_paper_report import _add_outcome
    from datetime import datetime, timezone

    conn = _conn()
    copy_event_to_arms(
        conn, _event(), 100.0, "2026-07-28T00:00:01+00:00", arms=("control",),
        stats_fn=lambda *_a, **_k: {"price": 1.0, "liquidity": 100_000.0},
        quote_fn=lambda *_a, **_k: (1.0, "route_quote"),
    )
    pid = conn.execute("SELECT id FROM memecoin_positions").fetchone()[0]
    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    memecoin_exits._exit_quote_fn = lambda *_a, **_k: (None, "quote_unavailable")
    try:
        out = memecoin_exits._close(conn, pos, 0.4, 100_000.0, "2026-07-28T01:00:00+00:00", "e_stop")
    finally:
        import importlib
        importlib.reload(memecoin_exits)
    assert out["exit_reason"] == "e_stop_noquote"        # reason carries the censor marker
    fill = dict(conn.execute("SELECT * FROM memecoin_fills WHERE kind='bracket_exit'").fetchone())
    assert fill["fill_source"] == "noquote"
    assert fill["price"] == pytest.approx(1.0)           # entry-priced, i.e. a 0-PnL placeholder
    row = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    row["report_entry_qty"], row["report_exit_qty"] = 100.0, 100.0
    row["report_exit_proceeds"] = 100.0
    row["report_observed_exit_qty"] = row["report_observed_exit_proceeds"] = 0.0
    row["report_fomo_fills"] = 0
    scored = _add_outcome(row, datetime.now(timezone.utc))
    assert scored["report_outcome_state"] == "censored_noquote"
    assert scored["report_pnl"] is None                  # censored, never counted as a real outcome
