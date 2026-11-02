"""Formula pin for the Solana exit grid study (pre-registration
docs/superpowers/specs/2026-10-16-solana-exit-grid-preregistration.md). Synthetic series only."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "research"))
import solana_exit_grid_study as g  # noqa: E402


def test_the_grid_is_72_arms_and_holds_the_control():
    assert len(g.ARMS) == 72 and len(set(g.ARMS)) == 72 and g.CONTROL in g.ARMS


# --- the rules ------------------------------------------------------------------------------------

def test_the_trigger_is_the_live_convention_value_over_entry_notional():
    """unit_price / entry_price - 1 is expected_usdc / notional - 1 for a whole bag."""
    assert g.simulate([(1.0, 124.99)], 100.0, tp=25) is None
    assert g.simulate([(1.0, 124.99), (2.0, 125.0)], 100.0, tp=25) == (2.0, 125.0, "tp")
    assert g.simulate([(1.0, 65.01), (2.0, 65.0)], 100.0, sl=35) == (2.0, 65.0, "stop")


def test_one_quote_meeting_several_rules_exits_once_with_the_live_order():
    assert g.simulate([(1.0, 10.0)], 100.0, sl=35, catastrophe=80) == (1.0, 10.0, "stop")
    assert g.simulate([(1.0, 10.0)], 100.0, catastrophe=80) == (1.0, 10.0, "catastrophe")
    assert g.simulate([(14400.0, 200.0)], 100.0, tp=50) == (14400.0, 200.0, "tp")


def test_stop_off_still_carries_the_contract_catastrophe():
    rules = g.grid_rules((None, None, None), g.CONTROL_CATASTROPHE)
    assert g.simulate([(1.0, 21.0), (2.0, 20.0)], 100.0, **rules) == (2.0, 20.0, "catastrophe")


def test_the_trail_arms_at_its_gain_then_exits_on_the_giveback():
    rules = g.grid_rules((None, None, 10), None)
    assert g.simulate([(1, 105.0), (2, 94.6)], 100.0, **rules) is None          # never armed
    path = [(1, 110.0), (2, 121.0), (3, 109.0), (4, 108.9)]
    assert g.simulate(path, 100.0, **rules) == (4, 108.9, "trail")              # 121 x 0.9


def test_max_hold_exits_at_the_first_quote_at_or_after_the_horizon():
    assert g.simulate([(14399.0, 100.0), (14401.0, 101.0)], 100.0) == (14401.0, 101.0, "max_hold")


def test_the_as_run_contract_reads_zero_as_off():
    r = g.contract_rules({"tp_pct": 0.0, "stop_pct": 35.0, "trail_pct": 0.0, "catastrophe_pct": 80.0,
                          "max_hold_s": 14400.0})
    assert r == {"tp": None, "sl": 35.0, "trail": None, "catastrophe": 80.0, "max_hold_s": 14400.0,
                 "debounce": 1, "fill_offset_s": 0.0, "no_loss_s": 0.0, "spacing_s": 0.75}


def _pos(series, realized=90.0, wallet="w", pid=1, policy=None):
    return {"id": pid, "wallet": wallet, "series": series, "notional": 100.0, "life": 60.0,
            "exit_fills": [{"notional": realized}], "exit_reason": "b_stop",
            "policy": policy or {"tp_pct": 25.0, "stop_pct": 35.0, "catastrophe_pct": 80.0,
                                 "max_hold_s": 14400.0}}


def test_an_arm_that_never_fires_inherits_the_last_quote_not_the_fill():
    """Gamma B1: every value in a difference is on the quote basis."""
    arms = g.price_arms(_pos([(1.0, 100.0), (2.0, 101.0)], realized=90.0))
    assert arms[(None, None, None)] == (101.0, False)
    arms = g.price_arms(_pos([(1.0, 111.0)]))
    assert arms[(10, None, None)] == (111.0, True)
    assert g.price_arms(_pos([(1.0, 100.0)]), [], inherit=90.0)[g.CONTROL] == (90.0, False)


def test_a_looser_arm_inheriting_the_controls_exit_quote_differs_by_exactly_zero():
    arms = g.price_arms(_pos([(1.0, 110.0), (2.0, 125.0)], realized=118.0))
    assert arms[g.CONTROL] == (125.0, True)
    assert arms[(35, 35, None)] == (125.0, False)
    book = [{"id": 1, "wallet": None, "notional": 100.0, "arms": arms}]
    row = g.analyse(book, np.random.default_rng(1), clusters_by_wallet=True)["arms"]
    assert row[g.arm_label((35, 35, None))]["mean_diff"] == 0.0


# --- the as-run loss-rail mechanics (Gamma B2) -------------------------------------------------------

def test_a_debounce_of_two_needs_two_consecutive_spaced_hits_and_a_recovery_resets_it():
    path = [(1.0, 64.0), (2.0, 70.0), (3.0, 64.0), (4.0, 64.5)]
    assert g.simulate(path, 100.0, sl=35, debounce=1) == (1.0, 64.0, "stop")
    assert g.simulate(path, 100.0, sl=35, debounce=2) == (4.0, 64.5, "stop")
    close = [(1.0, 64.0), (1.5, 64.0), (2.0, 64.0)]          # hit 2 only once 0.75 s old
    assert g.simulate(close, 100.0, sl=35, debounce=2) == (2.0, 64.0, "stop")


def test_the_instant_bypass_at_fifty_percent_and_its_grace():
    deep = [(1.0, 49.0), (2.0, 49.0), (3.0, 49.0)]
    assert g.simulate(deep, 100.0, sl=35, catastrophe=80, debounce=3) == (3.0, 49.0, "stop")
    assert g.simulate([(25.0, 49.0)], 100.0, sl=35, catastrophe=80, debounce=3) == (
        25.0, 49.0, "stop")                                   # past the 20 s grace: first read
    assert g.simulate(deep, 100.0, sl=35, catastrophe=80, debounce=3, fill_offset_s=-20.0) == (
        1.0, 49.0, "stop")                                    # the grace counts from the fill
    assert g.simulate([(1.0, 19.0)], 100.0, sl=35, catastrophe=80, debounce=3) == (
        1.0, 19.0, "stop")                                    # past the floor lifts the grace
    assert g.simulate([(1.0, 49.0)], 100.0, catastrophe=80, debounce=3) is None


def test_an_unfired_arm_inherits_the_as_run_decision_quote_not_a_later_one():
    """Gamma R2: quotes after the live decision (taken while it sold) are not the inherited exit."""
    arms = g.price_arms(_pos([(1.0, 110.0), (2.0, 125.0), (3.0, 120.0)], realized=118.0))
    assert arms[g.CONTROL] == (125.0, True)
    assert arms[(35, 35, None)] == (125.0, False)


def test_the_stop_hit_spacing_starts_at_its_deploy():
    """Gamma R1: before 2026-09-27T04:38:38Z every sweep counted."""
    close = [(1.0, 64.0), (1.5, 64.0)]
    assert g.simulate(close, 100.0, sl=35, debounce=2, spacing_s=0.0) == (1.5, 64.0, "stop")
    assert g.simulate(close, 100.0, sl=35, debounce=2) is None
    pre = dict(_pos(close, policy={"tp_pct": 25.0, "stop_pct": 35.0, "catastrophe_pct": 80.0,
                                   "max_hold_s": 14400.0, "stop_debounce_sweeps": 2}), spacing_s=0.0)
    assert g.price_arms(pre)[g.CONTROL] == (64.0, True)
    by = {p["id"]: p for p in g.load(_ledger())}
    assert by[1]["spacing_s"] == 0.0 and by[10]["spacing_s"] == 0.75


def test_every_arm_carries_the_positions_own_debounce():
    pos = _pos([(1.0, 64.0), (2.0, 70.0), (3.0, 101.0)],
               policy={"tp_pct": 25.0, "stop_pct": 35.0, "catastrophe_pct": 80.0,
                       "max_hold_s": 14400.0, "stop_debounce_sweeps": 2})
    assert g.price_arms(pos)[g.CONTROL] == (101.0, False)          # one hit does not sell


def test_the_no_loss_window_defers_every_loss_read_counted_from_the_fill():
    path = [(5.0, 10.0), (30.5, 60.0), (31.5, 60.0), (32.5, 60.0)]
    rules = dict(sl=35, catastrophe=80, debounce=2, no_loss_s=30.0, fill_offset_s=1.0)
    assert g.simulate(path, 100.0, **rules) == (32.5, 60.0, "stop")   # 30.5 s is fill age 29.5
    assert g.simulate(path, 100.0, **dict(rules, no_loss_s=0.0)) == (5.0, 10.0, "stop")
    early = dict(_pos([(5.0, 10.0), (40.0, 101.0)]), no_loss_s=30.0)
    assert g.price_arms(early)[g.CONTROL] == (101.0, False)          # the window reaches the arms


def test_the_no_loss_window_belongs_to_positions_opened_before_its_switch_off():
    by = {p["id"]: p for p in g.load(_ledger())}
    assert by[1]["no_loss_s"] == 30.0                                 # 2026-09-10


def test_a_pending_loss_read_takes_no_other_exit_on_that_quote():
    path = [(14400.0, 60.0), (14401.0, 70.0)]
    assert g.simulate(path, 100.0, sl=35, debounce=2) == (14401.0, 70.0, "max_hold")


def test_the_trail_is_not_armed_before_its_gain():
    assert g.simulate([(1, 105.0), (2, 94.0)], 100.0, trail=(10, 10)) is None


def test_the_control_contract_is_exactly_the_control():
    base = {"tp_pct": 25.0, "stop_pct": 35.0, "trail_pct": 0.0, "catastrophe_pct": 80.0,
            "max_hold_s": 14400.0, "stop_debounce_sweeps": 3}
    assert g.is_control_contract(base)
    for k, v in (("tp_pct", 15.0), ("max_hold_s", 780.0), ("trail_pct", 10.0)):
        assert not g.is_control_contract(dict(base, **{k: v}))
    assert g.contract_rules(base)["debounce"] == 3


# --- coverage and exclusion ---------------------------------------------------------------------

def test_coverage_counts_both_ends():
    share, gaps = g.coverage([2.0, 3.0, 4.0], 10.0)
    assert share == pytest.approx((1 + 1 + 5) / 10.0)
    assert gaps == [(0.0, 2.0), (2.0, 3.0), (3.0, 4.0), (4.0, 10.0)]
    assert g.first_bad_gap([(0.0, 2.0), (2.0, 70.0)]) == (2.0, 70.0)
    assert g.first_bad_gap([(0.0, 60.0)]) is None


def test_a_gap_cause_separates_dead_route_from_outage():
    assert g.gap_cause(True, False) == "stale"
    assert g.gap_cause(False, True) == "quote_miss"
    assert g.gap_cause(False, False) == "outage"


# --- fees ---------------------------------------------------------------------------------------

def test_native_cost_is_fee_plus_tips_including_loaded_addresses():
    tx = {"meta": {"fee": 5000, "preBalances": [10 ** 9, 0, 7, 3], "postBalances": [10 ** 9 - 6005000, 1000, 6000007, 3],
                   "loadedAddresses": {"writable": ["NOZ"], "readonly": ["R"]}},
          "transaction": {"message": {"accountKeys": ["ME", "SENDER"]}}}
    assert g.native_cost_lamports(tx, {"SENDER", "NOZ"}) == (5000 + 1000 + 6000000, 6001000)
    assert g.native_cost_lamports({"meta": None}, set()) is None


def test_sol_usd_comes_from_the_quotes_own_wsol_to_usdc_leg():
    ev = ('{"route_plan": [{"inputMint": "X", "outputMint": "%s", "inAmount": "1", "outAmount": "2"},'
          ' {"inputMint": "%s", "outputMint": "%s", "inAmount": "2000000000", "outAmount": "210000000"}]}'
          % (g.WSOL, g.WSOL, g.USDC))
    assert g.sol_usd_from_plan(ev) == pytest.approx(105.0)
    assert g.sol_usd_from_plan('{"route_plan": []}') is None and g.sol_usd_from_plan(None) is None


# --- statistics ---------------------------------------------------------------------------------

def test_holm_steps_down():
    assert g.holm({"a": 0.001, "b": 0.01, "c": 0.03}, 0.025) == {"a", "b"}
    assert g.holm({"a": 0.009, "b": 0.01}, 0.025) == {"a", "b"}
    assert g.holm({}, 0.025) == set()


def test_holm_first_threshold_is_alpha_over_m():
    assert g.holm({"a": 0.0126, "b": 0.5}, 0.025) == set()
    assert g.holm({"a": 0.0125, "b": 0.5}, 0.025) == {"a"}


def test_the_bootstrap_is_seeded_signed_and_cluster_weighted():
    sums, sizes = [3.0, 0.5, 1.0], [3, 1, 2]
    a = g.boot_stats(sums, sizes, g.boot_draws(3, np.random.default_rng(g.SEED)))
    b = g.boot_stats(sums, sizes, g.boot_draws(3, np.random.default_rng(g.SEED)))
    assert a == b and a[0] == pytest.approx(4.5 / 6) and a[3] < 0.001
    neg = g.boot_stats([-1.0, -2.0], [1, 1], g.boot_draws(2, np.random.default_rng(1)))
    assert neg[3] > 0.99
    tie = g.boot_stats([0.0, 0.0], [1, 1], g.boot_draws(2, np.random.default_rng(1)))
    assert tie[3] == 1.0                                            # a tie is no evidence


def _book(n, better_arm, uninherit=None):
    """n positions: every arm equals the control except `better_arm`, which fires 5-15 pts higher."""
    out = []
    for i in range(n):
        arms = {arm: (100.0, True) for arm in g.ARMS}
        arms[better_arm] = (110.0 + (5 if i % 2 else -5), True)
        if uninherit:
            arms[uninherit] = (100.0, i % 4 == 0)                   # 75% inherited
        out.append({"id": i, "wallet": "w%d" % (i % 4), "notional": 100.0, "arms": arms})
    return out


def test_an_arm_that_is_better_everywhere_beats_the_control_pooled_and_per_wallet():
    arm = (10, 20, None)
    book = _book(40, arm, uninherit=(15, 20, None))
    for clusters in (True, False):
        got = g.analyse(book, np.random.default_rng(g.SEED), clusters_by_wallet=clusters)
        assert got["beats_control"] == [g.arm_label(arm)]
        row = got["arms"][g.arm_label((15, 20, None))]
        assert row["inherited_share"] == pytest.approx(0.75) and not row["identifiable"]
        assert got["arms"][g.arm_label(arm)]["mean_diff"] == pytest.approx(0.10)


def test_pooled_reads_resample_wallets_not_positions():
    """Two wallets that disagree: positions look decisive, wallets do not."""
    book = []
    for i in range(40):
        arms = {arm: (100.0, True) for arm in g.ARMS}
        arms[(10, 20, None)] = (120.0 if i < 20 else 90.0, True)
        book.append({"id": i, "wallet": "a" if i < 20 else "b", "notional": 100.0, "arms": arms})
    lab = g.arm_label((10, 20, None))
    by_pos = g.analyse(book, np.random.default_rng(g.SEED), clusters_by_wallet=False)["arms"][lab]
    by_wal = g.analyse(book, np.random.default_rng(g.SEED), clusters_by_wallet=True)["arms"][lab]
    assert by_pos["p_one_sided"] < 0.05 < 0.2 < by_wal["p_one_sided"]


def test_nothing_beats_a_control_it_equals():
    book = _book(30, g.CONTROL)
    got = g.analyse(book, np.random.default_rng(g.SEED), clusters_by_wallet=True)
    assert got["beats_control"] == []


def test_validity_replays_the_contracts_own_max_hold():
    short = {"tp_pct": 25.0, "stop_pct": 35.0, "catastrophe_pct": 80.0, "max_hold_s": 780.0}
    p = dict(_pos([(700.0, 101.0), (781.0, 102.0), (900.0, 103.0)], realized=102.0, policy=short),
             exit_reason="b_timecap")
    got = g.validity([p])["per_rail"]["max_hold"]
    assert got["fired"] == 1 and got["reason_match_share"] == 1.0 and got["passed"]


def test_validity_reports_each_rail_and_a_signed_haircut():
    stops = [_pos([(1.0, 100.0), (2.0, 64.0)], realized=60.8, pid=i) for i in range(3)]
    got = g.validity(stops)
    assert got["per_rail"]["stop"]["signed_rel_err_median"] == pytest.approx(64.0 / 60.8 - 1)
    assert got["passed"] is False and got["stop_passed"] is False


def test_the_validity_gate_uses_the_as_run_contract():
    match = [_pos([(1.0, 100.0), (2.0, 64.0)], realized=64.0, pid=i) for i in range(5)]
    assert g.validity(match)["passed"]
    assert g.validity(match)["per_rail"]["stop"]["reason_match_share"] == 1.0
    tp15 = {"tp_pct": 15.0, "stop_pct": 35.0, "catastrophe_pct": 80.0, "max_hold_s": 14400.0}
    off = [dict(_pos([(1.0, 116.0)], realized=116.0, pid=i, policy=tp15), exit_reason="real_tp")
           for i in range(5)]
    assert g.validity(off)["passed"]                                  # TP 15 as run, not 25
    wrong = [_pos([(1.0, 100.0), (2.0, 64.0)], realized=50.0, pid=i) for i in range(5)]
    assert not g.validity(wrong)["passed"]


def test_the_read_runs_once(tmp_path):
    out = tmp_path / "read.json"
    out.write_text("{}")
    assert g.main(["--read", "--out", str(out)]) == 2
    assert g.main(["--read"]) == 2


def test_the_read_glue_runs_end_to_end_on_a_synthetic_book(tmp_path, monkeypatch):
    """One read, so the glue must not crash on it: validity, arms, sensitivities, fees, output."""
    monkeypatch.setattr(g, "BOOT_N", 200)
    monkeypatch.setattr(g, "boot_draws", lambda n, rng, b=200: rng.integers(0, n, size=(b, n)))
    ev = ('{"route_plan": [{"inputMint": "%s", "outputMint": "%s", "inAmount": "1000000000",'
          ' "outAmount": "150000000"}]}' % (g.WSOL, g.USDC))
    book = []
    for i in range(25):
        top = 130.0 if i % 2 else 112.0
        p = _pos([(1.0, 100.0), (2.0, top), (3.0, 64.0)], realized=125.0 if i % 2 else 64.0, pid=i)
        p.update(cause=None, control_contract=True, opened=1.7e9 + i * 600,
                 exit_reason="real_tp" if i % 2 else "b_stop",
                 quotes=[(1.7e9 + i * 600 + 1, 1.7e9 + i * 600 + 1, 100.0, ev)],
                 entry_fill={"tx_sig": "e%d" % i})
        book.append(p)
    dead = dict(_pos([(1.0, 100.0), (2.0, 60.0)], pid=99), cause="quote_miss", cut_at=1.5,
                series_cut=[(1.0, 100.0)], opened=1.7e9, quotes=[], control_contract=True)
    thin = dict(_pos([(1.0, 100.0), (9.0, 70.0)], pid=97), cause="thin", opened=1.7e9, quotes=[],
                control_contract=True)
    partial = dict(_pos([], realized=150.0, pid=98), cause="partial_exit")
    other = dict(book[0], id=96, control_contract=False)
    monkeypatch.setattr(g, "read_fees", lambda inc: ({p["id"]: (0.01, 0.0001) for p in inc},
                                                     {"entries_with_tip": 0, "stopped": None}))
    out = tmp_path / "read.json"
    assert g.run_read(book + [other, dead, thin, partial], out) == 0
    assert dead["arms"][g.CONTROL] == (90.0, False)                # no quote at its close: the fill
    got = __import__("json").loads(out.read_text())
    assert got["validity"]["passed"] and got["partial_exits"]["n"] == 1
    assert set(got) >= {"per_wallet", "pooled", "sensitivity_fired_only_pooled",
                        "sensitivity_dead_route_kept", "sensitivity_thin_kept", "absolute", "fees",
                        "control_fired_share", "sl_dimension_read", "other_contracts_apart"}
    assert got["sensitivity_thin_kept"]["added_positions"] == 1
    assert got["pooled"]["n"] == 25 and got["other_contracts_apart"]["n"] == 1
    assert got["sl_dimension_read"] is True
    assert got["sensitivity_dead_route_kept"]["added_positions"] == 1
    assert got["absolute"]["positions_with_fees"] == 25


# --- load(): causes, quote age, partial exits, the dead-route cut (Gamma's surviving mutants) -------

def _ledger():
    import json
    import sqlite3
    from datetime import datetime, timedelta, timezone
    from sniper.persistence import db_memecoin as m
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    t0 = datetime(2026, 9, 10, tzinfo=timezone.utc)
    iso = lambda dt: dt.isoformat()
    control = {"policy_id": "follow-30-50-v2", "tp_pct": 25.0, "stop_pct": 35.0, "trail_pct": 0.0,
               "catastrophe_pct": 80.0, "max_hold_s": 14400.0, "stop_debounce_sweeps": 1}

    def position(pid, start, life, rows, *, policy=control, exits=1, age=0.1):
        opened = t0 + timedelta(seconds=start)
        conn.execute(
            "INSERT INTO memecoin_positions (id, source_user_id, mint, opened_at, opened_recorded_at, "
            "closed_at, is_open, exit_reason, funded_contract_json, execution_mode, network_id) "
            "VALUES (?, 'w', ?, ?, ?, ?, 0, 'b_stop', ?, 'real', 'solana')",
            (pid, "M%d" % pid, iso(opened), iso(opened + timedelta(seconds=2)),
             iso(opened + timedelta(seconds=life)), json.dumps({"policy": policy})))
        conn.execute("INSERT INTO memecoin_fills (position_id, kind, notional, qty, tx_sig) "
                     "VALUES (?, 'entry', 100, 1000, 'e')", (pid,))
        for _ in range(exits):
            conn.execute("INSERT INTO memecoin_fills (position_id, kind, notional, qty, tx_sig) "
                         "VALUES (?, 'bracket_exit', 90, 1000, 'x')", (pid,))
        for t in rows:
            at = opened + timedelta(seconds=t)
            conn.execute("INSERT INTO memecoin_route_series (position_id, mint, network_id, seam, "
                         "observed_at, quoted_at, expected_usdc) VALUES (?, ?, 'solana', "
                         "'exit_decision', ?, ?, 100)",
                         (pid, "M%d" % pid, iso(at), iso(at - timedelta(seconds=age))))
    every = lambda a, b, step=1: list(range(a, b, step))
    position(1, 0, 150, every(1, 150))                          # included, and alive for 2's gap
    position(2, 0, 150, every(1, 31) + every(140, 150))         # quote_miss: gap 30 -> 140
    position(3, 10_000, 150, every(1, 31))                      # outage: nobody recorded
    position(4, 20_000, 150, every(1, 150), age=10.0)           # stale: rows too old
    position(5, 30_000, 150, every(1, 150, 8))                  # thin: 8 s apart
    position(6, 40_000, 150, every(1, 150), exits=2)            # partial exit
    position(7, 50_000, 150, every(1, 150), policy=dict(control, tp_pct=15.0))
    position(8, -10 * 86400, 150, every(1, 150))                # opened 09-01: pre_rules_era
    position(9, 8 * 86400, 150, every(1, 150))                  # opened 09-18: window off
    position(10, 18 * 86400, 150, every(1, 150))                # opened 09-28: spacing on
    conn.commit()
    return conn


def test_load_labels_every_cause_from_the_recording_alone():
    by = {p["id"]: p for p in g.load(_ledger())}
    assert {i: by[i]["cause"] for i in by} == {
        1: None, 2: "quote_miss", 3: "outage", 4: "stale", 5: "thin", 6: "partial_exit", 7: None,
        8: "pre_rules_era", 9: None, 10: None}
    assert by[9]["no_loss_s"] == 0.0
    assert by[1]["control_contract"] and not by[7]["control_contract"]
    assert len(by[1]["series"]) == 149 and by[1]["fill_offset"] == pytest.approx(2.0)
    assert by[2]["cut_at"] == pytest.approx(30.0) and len(by[2]["series"]) == 40
    assert [t for t, _v in by[2]["series_cut"]] == [float(t) for t in range(1, 31)]


def test_a_failed_stop_replay_leaves_the_stop_dimension_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "boot_draws", lambda n, rng, b=200: rng.integers(0, n, size=(b, n)))
    monkeypatch.setattr(g, "read_fees", lambda inc: ({}, {"entries_with_tip": 0, "stopped": None}))
    book = []
    for i in range(30):
        stop = i < 6
        p = _pos([(1.0, 100.0), (2.0, 64.0 if stop else 125.0)], realized=50.0 if stop else 125.0,
                 pid=i)
        p.update(cause=None, control_contract=True, opened=1.7e9, quotes=[],
                 exit_reason="b_stop" if stop else "real_tp", entry_fill={"tx_sig": None})
        book.append(p)
    out = tmp_path / "read.json"
    assert g.run_read(book, out) == 0
    got = __import__("json").loads(out.read_text())
    assert got["validity"]["passed"] and not got["validity"]["stop_passed"]
    assert got["sl_dimension_read"] is False
    assert {k.split("/")[1] for k in got["pooled"]["arms"]} == {"sl35"}
