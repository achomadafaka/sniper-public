import json
import sqlite3

from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.services.memecoin_policy import resolve_paper_arm_policy


def _conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _position(conn, strategy, *, event, cohort="C1", pnl=0.0, status="eligible",
              network_id="solana", closed=True, opened_at="2026-07-28T00:00:00+00:00",
              closed_at="2026-07-28T00:01:00+00:00", mint=None, wallet=None,
              entry_fill=True, exit_fill=True, exit_reason="test",
              exit_fill_source="dexscreener", time_to_peak_s=None):
    mint = mint or f"M-{event}"
    wallet = wallet or f"W-{event}"
    policy_id, policy_version, policy_json = resolve_paper_arm_policy(
        strategy, network_id=network_id, opened_at=opened_at,
    )
    pid = m.open_position(
        conn, source_user_id=wallet, source_handle=wallet, mint=mint, ticker=mint,
        entry_price=1.0, entry_notional=100.0, entry_fill_source="dexscreener",
        entry_lag_s=1.0, their_size_usd=1000.0, opened_at=opened_at,
        strategy=strategy, origin_event_id=event, network_id=network_id,
        assigned_policy_id=policy_id, assigned_policy_version=policy_version,
        assigned_policy_json=policy_json, paper_cohort_id=cohort,
        paper_cohort_json='{"arms":[]}', experiment_status=status,
    )
    if entry_fill:
        m.insert_fill(
            conn, position_id=pid, kind="entry", price=1.0, qty=100.0, notional=100.0,
            fill_source="dexscreener", fraction=1.0, created_at=opened_at,
            origin_event_id=event, network_id=network_id,
        )
    if closed:
        # An exit fill, because production closes always book one. `_add_outcome` now treats a
        # closed row WITHOUT one as `invalid_missing_exit_fill` — the orphan-close malformation,
        # whose stored realized_pnl has no provenance (2026-08-04). Pass exit_fill=False to build
        # that shape deliberately.
        if exit_fill:
            m.insert_fill(
                conn, position_id=pid, kind="bracket_exit", price=1.0 + (pnl / 100.0),
                qty=100.0, notional=100.0 + pnl, fill_source=exit_fill_source, fraction=1.0,
                created_at=closed_at, origin_event_id=event, network_id=network_id,
            )
        m.close_position(conn, pid, closed_at, realized_pnl=pnl, exit_reason=exit_reason)
    if time_to_peak_s is not None:
        m.set_position_instrumentation(
            conn, pid, time_to_peak_s=time_to_peak_s, max_gain_pct=0.0,
        )
    if cohort:
        roster = [
            {"strategy": row[0]} for row in conn.execute(
                "SELECT DISTINCT strategy FROM memecoin_positions WHERE paper_cohort_id=? "
                "ORDER BY strategy",
                (cohort,),
            )
        ]
        conn.execute(
            "UPDATE memecoin_positions SET paper_cohort_json=? WHERE paper_cohort_id=?",
            (json.dumps({"arms": roster}, sort_keys=True, separators=(",", ":")), cohort),
        )
        conn.commit()
    return pid


def test_report_primary_uses_only_same_cohort_exact_events():
    from sniper.services.memecoin_paper_report import build_paper_report

    conn = _conn()
    _position(conn, "control", event="E1", pnl=1.0)
    _position(conn, "alpha_b", event="E1", pnl=3.0)
    _position(conn, "alpha_b", event="E2", pnl=100.0)
    report = build_paper_report(conn, arms=("control", "alpha_b"))
    assert report["forward"]["matched_events"] == 1
    assert {row["strategy"]: row["total_pnl"] for row in report["forward"]["arms"]} == {
        "control": 1.0, "alpha_b": 3.0,
    }
    paired = report["forward"]["paired"][0]
    assert paired["challenger"] == "alpha_b" and paired["mean_delta"] == 2.0


def test_report_censors_null_and_includes_booked_partial_in_open_pnl():
    from sniper.services.memecoin_paper_report import build_paper_report

    conn = _conn()
    _position(conn, "control", exit_fill=False, event="E0", pnl=None)
    pid = _position(conn, "control", exit_fill=False, event="E1", closed=False)
    m.insert_fill(
        conn, position_id=pid, kind="partial_exit", price=2.0, qty=40.0,
        notional=80.0, fill_source="dexscreener", fraction=0.4,
        created_at="2026-07-28T00:00:30+00:00",
    )
    m.update_position_last_quote(conn, pid, 1.5, "2026-07-28T00:01:00+00:00")
    report = build_paper_report(conn, arms=("control",))
    assert report["coverage"]["censored_outcomes"] == 1
    arm = report["forward"]["arms"][0]
    assert arm["total_pnl"] == 70.0 and arm["censored_outcomes"] == 1


def test_report_chain_filter_scopes_every_aggregate():
    from sniper.services.memecoin_paper_report import build_paper_report

    conn = _conn()
    _position(conn, "control", event="SOL", pnl=5.0, network_id="solana")
    _position(conn, "control", event="ROB", pnl=-5.0, network_id="4663")
    report = build_paper_report(conn, arms=("control",), chain="robin")
    assert report["coverage"]["positions"] == 1
    assert report["forward"]["arms"][0]["total_pnl"] == -5.0
    assert {row["network_id"] for row in report["closed"]} == {"4663"}


def test_report_returns_all_companion_arms_for_recent_episode():
    from sniper.services.memecoin_paper_report import build_paper_report

    conn = _conn()
    _position(conn, "control", event="TARGET", closed=False)
    _position(conn, "alpha_b", event="TARGET", pnl=2.0)
    for index in range(205):
        _position(
            conn, "control", event=f"OTHER-{index}", pnl=0.0,
            opened_at=f"2026-07-28T01:{index % 60:02d}:00+00:00",
            closed_at=f"2026-07-28T01:{index % 60:02d}:30+00:00",
        )
    report = build_paper_report(conn, arms=("control", "alpha_b"), episode_limit=1)
    assert [row["origin_event_id"] for row in report["episodes"]] == ["TARGET"]
    assert report["coverage"]["open_episodes_truncated"] == 0
    target = next(row for row in report["episodes"] if row["origin_event_id"] == "TARGET")
    assert target["forward_matched"] is True
    assert {arm["strategy"] for arm in target["arms"]} == {"control", "alpha_b"}


def test_report_bounds_open_episode_payload_and_discloses_truncation():
    from sniper.services.memecoin_paper_report import build_paper_report

    conn = _conn()
    for index in range(3):
        _position(conn, "control", event=f"OPEN-{index}", closed=False)
    report = build_paper_report(conn, arms=("control",), episode_limit=2)
    assert len(report["episodes"]) == 2
    assert report["coverage"]["open_episodes_truncated"] == 1


def test_report_keeps_all_companion_arms_on_a_partly_closed_episode():
    """An episode must carry every arm, including the ones that already exited — a partial arm
    set reads as a missing arm. (This replaces an assertion that fully-CLOSED episodes ride in
    `episodes` too: the Strategy Lab drops those client-side, so they were pure payload. Closed
    rows are still served, via `closed`.)"""
    from sniper.services.memecoin_paper_report import build_paper_report

    conn = _conn()
    _position(conn, "control", event="MIXED", pnl=1.0, closed=True)
    _position(conn, "alpha_b", event="MIXED", closed=False)
    report = build_paper_report(conn, arms=("control", "alpha_b"), episode_limit=1)
    assert [episode["origin_event_id"] for episode in report["episodes"]] == ["MIXED"]
    assert {row["strategy"] for row in report["episodes"][0]["arms"]} == {
        "control", "alpha_b",
    }


def test_report_censors_synthetic_noquote_close_and_shows_loss_sensitivity():
    from sniper.services.memecoin_paper_report import build_paper_report

    conn = _conn()
    pid = _position(
        conn, "control", exit_fill=False, event="NOQUOTE", pnl=0.0,
        exit_reason="b_timecap_noquote",
    )
    m.insert_fill(
        conn, position_id=pid, kind="bracket_exit", price=1.0, qty=100.0,
        notional=100.0, fill_source="noquote", fraction=1.0,
        created_at="2026-07-28T00:01:00+00:00",
        origin_event_id="NOQUOTE", network_id="solana",
    )
    report = build_paper_report(conn, arms=("control",))
    arm = report["forward"]["arms"][0]
    assert arm["total_pnl"] is None
    assert arm["censored_outcomes"] == 1
    assert arm["conservative_total_pnl"] == -100.0


def test_noquote_loss_sensitivity_keeps_booked_partial_exit_proceeds():
    from sniper.services.memecoin_paper_report import build_paper_report

    conn = _conn()
    pid = _position(
        conn, "control", exit_fill=False, event="PARTIAL-NOQUOTE", pnl=0.0,
        exit_reason="b_timecap_noquote",
    )
    m.insert_fill(
        conn, position_id=pid, kind="partial_exit", price=2.0, qty=50.0,
        notional=100.0, fill_source="modeled", fraction=0.5,
        created_at="2026-07-28T00:00:30+00:00",
        origin_event_id="PARTIAL-NOQUOTE", network_id="solana",
    )
    m.insert_fill(
        conn, position_id=pid, kind="bracket_exit", price=1.0, qty=50.0,
        notional=50.0, fill_source="noquote", fraction=1.0,
        created_at="2026-07-28T00:01:00+00:00",
        origin_event_id="PARTIAL-NOQUOTE", network_id="solana",
    )
    report = build_paper_report(conn, arms=("control",))
    assert report["forward"]["arms"][0]["conservative_total_pnl"] == 0.0


def test_report_excludes_position_missing_its_entry_fill():
    from sniper.services.memecoin_paper_report import build_paper_report

    conn = _conn()
    _position(conn, "control", event="CORRUPT", pnl=10.0, entry_fill=False)
    report = build_paper_report(conn, arms=("control",))
    assert report["forward"]["matched_events"] == 0
    assert report["coverage"]["missing_entry_fill"] == 1


def test_report_primary_selects_latest_cohort_instead_of_pooling_policy_versions():
    from sniper.services.memecoin_paper_report import build_paper_report

    conn = _conn()
    for arm, pnl in (("control", 1.0), ("alpha_b", 2.0)):
        _position(conn, arm, event="OLD", cohort="C1", pnl=pnl,
                  opened_at="2026-07-28T00:00:00+00:00")
    for arm, pnl in (("control", 10.0), ("alpha_b", 30.0)):
        _position(conn, arm, event="NEW", cohort="C2", pnl=pnl,
                  opened_at="2026-07-28T02:00:00+00:00")
    report = build_paper_report(conn, arms=("control", "alpha_b"))
    assert report["forward"]["cohort_id"] == "C2"
    assert report["forward"]["matched_events"] == 1
    assert {row["strategy"]: row["total_pnl"] for row in report["forward"]["arms"]} == {
        "control": 10.0, "alpha_b": 30.0,
    }
    assert report["coverage"]["forward_cohorts"] == 2


def test_report_rejects_cohort_whose_frozen_arm_roster_differs_from_runtime():
    from sniper.services.memecoin_paper_report import build_paper_report

    conn = _conn()
    _position(conn, "control", event="ROSTER", cohort="C1", pnl=1.0)
    conn.execute(
        "UPDATE memecoin_positions SET paper_cohort_json=?",
        (json.dumps({"arms": [{"strategy": "control"}, {"strategy": "retired_arm"}]}),),
    )
    conn.commit()
    report = build_paper_report(conn, arms=("control",))
    assert report["forward"]["matched_events"] == 0


def test_peak_in_first_sweep_counts_zero_second_peak():
    from sniper.services.memecoin_paper_report import build_paper_report

    conn = _conn()
    _position(conn, "control", event="PEAK0", time_to_peak_s=0.0)
    report = build_paper_report(conn, arms=("control",))
    assert report["forward"]["arms"][0]["peak_in_first_sweep_rate"] == 1.0


def test_report_separates_legacy_matched_rows_from_forward():
    from sniper.services.memecoin_paper_report import build_paper_report

    conn = _conn()
    for arm, pnl in (("control", 2.0), ("alpha_b", -1.0)):
        _position(conn, arm, event="OLD", cohort=None, status="legacy_unversioned", pnl=pnl)
    report = build_paper_report(conn, arms=("control", "alpha_b"))
    assert report["forward"]["matched_events"] == 0
    assert report["legacy"]["matched_events"] == 1
    assert report["coverage"]["legacy_rows"] == 2


def test_report_classifies_pre_migration_null_rows_and_historical_fomo():
    from sniper.services.memecoin_paper_report import build_paper_report

    conn = _conn()
    for arm in ("control", "alpha_b"):
        pid = _position(conn, arm, exit_fill=False, event="OLD-NULL", cohort=None, pnl=1.0)
        conn.execute(
            "UPDATE memecoin_positions SET experiment_status=NULL,paper_cohort_id=NULL WHERE id=?",
            (pid,),
        )
    fomo = _position(conn, "control", exit_fill=False, event="FOMO", cohort=None, pnl=99.0)
    conn.execute(
        "UPDATE memecoin_positions SET experiment_status=NULL,entry_fill_source='fomo' WHERE id=?",
        (fomo,),
    )
    conn.execute("UPDATE memecoin_fills SET fill_source='fomo' WHERE position_id=?", (fomo,))
    conn.commit()
    report = build_paper_report(conn, arms=("control", "alpha_b"))
    assert report["legacy"]["matched_events"] == 1
    assert report["coverage"]["legacy_rows"] == 3
    assert report["coverage"]["excluded_fill"] == 1


def test_paper_report_route_validates_chain_and_uses_configured_arms(monkeypatch):
    conn = _conn()
    _position(conn, "control", event="E1", pnl=1.0)
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_ARMS", ["control"])
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": conn}))
    client = app.test_client()
    assert client.get("/api/memecoin/paper-report?chain=all").get_json()["forward"][
        "matched_events"
    ] == 1
    bad = client.get("/api/memecoin/paper-report?chain=polygon")
    assert bad.status_code == 400 and bad.get_json()["error"] == "invalid_chain"


def test_report_projection_avoids_select_star_and_still_serves_every_view_column():
    """`SELECT p.*` cost 1550ms of a 1610ms report on the live ledger. The projection must
    stay a strict superset of what the API exposes, or fields silently serialise as null."""
    import inspect

    from sniper.services import memecoin_paper_report as report_module

    source = inspect.getsource(report_module._project_rows)
    assert "p.*" not in source
    assert set(report_module._VIEW_COLS) <= set(report_module._PROJECT_COLS)

    conn = _conn()
    _position(conn, "control", event="E1", pnl=1.0)
    view = report_module.build_paper_report(conn, arms=("control",))["closed"][0]
    for column in report_module._VIEW_COLS:
        assert column in view, column
    assert view["ticker"] == "M-E1" and view["entry_notional"] == 100.0


def test_episodes_carry_only_events_with_a_live_arm():
    """The Strategy Lab renders open episodes only; padding the list with fully-closed events
    shipped ~440 KiB per poll that the UI filtered straight back out."""
    from sniper.services.memecoin_paper_report import build_paper_report

    conn = _conn()
    _position(conn, "control", event="DONE", pnl=1.0, closed=True)
    _position(conn, "control", event="LIVE", closed=False)
    report = build_paper_report(conn, arms=("control",))
    assert [e["origin_event_id"] for e in report["episodes"]] == ["LIVE"]
    # the closed row is still reachable — it just travels in `closed`, not twice.
    assert {r["origin_event_id"] for r in report["closed"]} == {"DONE"}


def test_paired_exposes_drop_top_and_closed_only_sensitivity():
    """A mean carried by one winner is not an effect: dropping it must flip the sign."""
    from sniper.services.memecoin_paper_report import build_paper_report

    conn = _conn()
    for event, control_pnl, challenger_pnl in (
        ("E1", 0.0, 100.0), ("E2", 0.0, -1.0), ("E3", 0.0, -1.0), ("E4", 0.0, -1.0),
    ):
        _position(conn, "control", event=event, pnl=control_pnl)
        _position(conn, "alpha_b", event=event, pnl=challenger_pnl)
    paired = build_paper_report(conn, arms=("control", "alpha_b"))["forward"]["paired"][0]
    assert paired["paired_outcomes"] == 4
    assert paired["mean_delta"] == 24.25            # one winner carries it
    assert paired["mean_delta_drop_top1"] == -1.0   # and it is the only winner
    assert paired["mean_delta_drop_top2"] == -1.0   # nothing profitable left to drop
    assert paired["closed_paired_outcomes"] == 4 and paired["closed_mean_delta"] == 24.25


def test_open_pairs_are_excluded_from_the_closed_only_delta():
    from sniper.services.memecoin_paper_report import build_paper_report

    conn = _conn()
    _position(conn, "control", event="E1", pnl=0.0)
    _position(conn, "alpha_b", event="E1", pnl=5.0)
    _position(conn, "control", event="E2", closed=False)
    _position(conn, "alpha_b", event="E2", closed=False)
    for pid, in conn.execute("SELECT id FROM memecoin_positions WHERE is_open=1").fetchall():
        m.update_position_last_quote(conn, pid, 1.0, "2026-07-28T00:02:00+00:00")
    conn.commit()
    paired = build_paper_report(conn, arms=("control", "alpha_b"))["forward"]["paired"][0]
    assert paired["paired_outcomes"] == 2
    assert paired["closed_paired_outcomes"] == 1 and paired["closed_mean_delta"] == 5.0


def test_thin_sample_is_graded_insufficient_evidence():
    """Five events on three coins cannot rank arms; the surface must say so itself."""
    from sniper.services.memecoin_paper_report import build_paper_report

    conn = _conn()
    _position(conn, "control", event="E1", pnl=1.0)
    _position(conn, "alpha_b", event="E1", pnl=2.0)
    forward = build_paper_report(conn, arms=("control", "alpha_b"))["forward"]
    assert forward["decision_grade"] == "blocked_protocol"
    assert forward["evidence_clusters"] == 1
    assert forward["min_evidence_clusters"] >= 1
    assert "modeled-fill" in forward["label"]


def test_sufficient_sample_grade_clears_once_clusters_reach_the_floor(monkeypatch):
    from sniper.services.memecoin_paper_report import build_paper_report

    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_PAPER_MIN_EVIDENCE_CLUSTERS", 2)
    conn = _conn()
    for event in ("E1", "E2"):
        _position(conn, "control", event=event, pnl=1.0)
        _position(conn, "alpha_b", event=event, pnl=2.0)
    forward = build_paper_report(conn, arms=("control", "alpha_b"))["forward"]
    assert forward["evidence_clusters"] == 2
    assert forward["decision_grade"] == "blocked_protocol"


def test_cohort_identity_changes_when_the_bet_size_changes():
    """Arms share one bet per event, so a paired dollar delta is only equal-capital while the
    notional is constant. Conviction sizing varies it — it must start a new cohort, not pool."""
    from sniper.services.memecoin_policy import build_paper_cohort

    hundred, contract = build_paper_cohort(("control",), notional_usd=100)
    three_hundred, _ = build_paper_cohort(("control",), notional_usd=300)
    same, _ = build_paper_cohort(("control",), notional_usd=100.0)
    assert hundred != three_hundred
    assert hundred == same
    assert json.loads(contract)["notional_usd"] == 100.0


def test_cleanup_marked_close_is_censored_not_measured():
    """AUDIT 2026-08-03 finding 2. `paper_unquotable_cleanup` books the STALE last-known mark as
    `fill_source='cleanup'` and stamps `exit_reason='unquotable'`.

    The censor predicate only fired on `unquotable AND no last_quote_price`, and the observed-exit
    columns excluded `noquote` alone — so a cleanup mark counted as an OBSERVED outcome. Four 4663
    STONKBROKER arms were rendered as known -$2.24 results despite no executable exit quote ever
    existing on that chain. A stale mark is not a transactable price.
    """
    from sniper.services.memecoin_paper_report import build_paper_report
    conn = _conn()
    pid = _position(conn, "control", event="E1", closed=False, network_id="4663")
    conn.execute("UPDATE memecoin_positions SET last_quote_price=0.9 WHERE id=?", (pid,))
    m.insert_fill(conn, position_id=pid, kind="manual_close", price=0.9, qty=100.0, notional=90.0,
                  fill_source="cleanup", fraction=1.0, created_at="2026-07-28T00:01:00+00:00")
    m.close_position(conn, pid, "2026-07-28T00:01:00+00:00", realized_pnl=-10.0,
                     exit_reason="unquotable")

    report = build_paper_report(conn, arms=("control",), chain="robin")
    row = [r for r in report["closed"] if r["id"] == pid][0]
    assert row["outcome_state"] == "censored_noquote", row["outcome_state"]
    assert row["full_book_pnl"] is None


def test_executable_close_stays_measured():
    """Guard the other direction: a real route-quote exit must remain a known outcome."""
    from sniper.services.memecoin_paper_report import build_paper_report
    conn = _conn()
    pid = _position(conn, "control", event="E1", closed=False, network_id="solana")
    m.insert_fill(conn, position_id=pid, kind="bracket_exit", price=1.1, qty=100.0, notional=110.0,
                  fill_source="route_quote", fraction=1.0, created_at="2026-07-28T00:01:00+00:00")
    m.close_position(conn, pid, "2026-07-28T00:01:00+00:00", realized_pnl=10.0,
                     exit_reason="b_stop")

    report = build_paper_report(conn, arms=("control",))
    row = [r for r in report["closed"] if r["id"] == pid][0]
    assert row["outcome_state"] == "closed"
    assert row["full_book_pnl"] == 10.0


# --- realized vs unrealized accounting (2026-08-04) ----------------------------------------------

def test_arm_row_separates_realized_from_unrealized_pnl():
    """A chain with NO measurable close still showed a headline PnL made entirely of open marks,
    rendered identically to a chain with a real track record. 4663 is exactly that: every close
    books a non-executable leg (censored) or a fomo-priced mirror fill (excluded).
    """
    from sniper.services.memecoin_paper_report import _arm_summary

    closed_measured = {"strategy": "control", "is_open": 0, "report_pnl": 12.0,
                       "report_conservative_pnl": 12.0, "opened_at": None, "closed_at": None}
    closed_censored = {"strategy": "control", "is_open": 0, "report_pnl": None,
                       "report_conservative_pnl": -100.0, "opened_at": None, "closed_at": None}
    open_marked = {"strategy": "control", "is_open": 1, "report_pnl": -5.0,
                   "report_conservative_pnl": -5.0, "opened_at": None, "closed_at": None}

    both = _arm_summary([(("c", "e1"), [closed_measured, closed_censored, open_marked])],
                        ("control",))[0]
    assert both["realized_pnl"] == 12.0
    assert both["open_marked_pnl"] == -5.0
    assert both["total_pnl"] == 7.0            # the sum, unchanged for compatibility
    assert both["closed_known"] == 1
    assert both["closed_censored"] == 1        # split out of the old single censored counter
    assert both["open_unpriced"] == 0

    # The 4663 shape: closes exist but none is measurable.
    blind = _arm_summary([(("c", "e1"), [closed_censored, open_marked])], ("control",))[0]
    assert blind["closed"] == 1
    assert blind["closed_known"] == 0
    # None, never 0.0 — "no evidence" must not render as "breakeven".
    assert blind["realized_pnl"] is None
    assert blind["open_marked_pnl"] == -5.0
    assert blind["total_pnl"] == -5.0
    assert blind["win_rate"] is None


def test_section_flags_a_chain_whose_pnl_is_entirely_unrealized():
    from sniper.services.memecoin_paper_report import _section

    censored = {"strategy": "control", "is_open": 0, "report_pnl": None,
                "report_conservative_pnl": -100.0, "opened_at": None, "closed_at": None,
                "network_id": "4663", "mint": "M", "source_user_id": "w1"}
    marked = {"strategy": "control", "is_open": 1, "report_pnl": -5.0,
              "report_conservative_pnl": -5.0, "opened_at": None, "closed_at": None,
              "network_id": "4663", "mint": "M", "source_user_id": "w1"}
    blind = _section([(("c", "e1"), [censored, marked])], ("control",))
    assert blind["realized_closes"] == 0
    assert blind["pnl_is_all_unrealized"] is True

    measured = dict(censored, report_pnl=3.0)
    ok = _section([(("c", "e1"), [measured, marked])], ("control",))
    assert ok["realized_closes"] == 1
    assert ok["pnl_is_all_unrealized"] is False


def test_open_marked_pnl_is_not_called_unrealized_because_it_carries_booked_partials():
    """An open row's report_pnl is `booked + (mark - entry) * remaining`. `booked` is REALIZED
    proceeds from partial exits already taken — live robin carries ~$150 of it across 9 open
    rows — so naming the total "unrealized" was inexact. The booked half is now exposed.
    """
    from sniper.services.memecoin_paper_report import _arm_summary

    partly_sold = {"strategy": "control", "is_open": 1, "report_pnl": 20.0,
                   "report_booked_pnl": 14.0, "report_conservative_pnl": 20.0,
                   "opened_at": None, "closed_at": None}
    row = _arm_summary([(("c", "e1"), [partly_sold])], ("control",))[0]
    assert row["open_marked_pnl"] == 20.0
    assert row["open_booked_pnl"] == 14.0     # already realized, was hidden inside "unrealized"
    assert "unrealized_pnl" not in row


def test_cluster_count_alone_cannot_grade_a_cohort_with_no_measurable_close():
    """4663 is on track to accrue 30 clusters while never pricing one exit. Sample size and
    measurability are different questions; passing the first must not imply the second."""
    from sniper.services.memecoin_paper_report import _section

    def cohort(n_events, measurable):
        matches = []
        for i in range(n_events):
            row = {"strategy": "control", "is_open": 0,
                   "report_pnl": 1.0 if measurable else None,
                   "report_conservative_pnl": -100.0, "opened_at": None, "closed_at": None,
                   "network_id": "4663", "mint": f"M{i}", "source_user_id": f"w{i}"}
            matches.append(((f"c", f"e{i}"), [row]))
        return _section(matches, ("control",))

    blind = cohort(40, measurable=False)
    assert blind["evidence_clusters"] >= blind["min_evidence_clusters"]
    assert blind["min_arm_closed_known"] == 0
    assert blind["decision_grade"] == "blocked_protocol"   # NOT sufficient_sample

    real = cohort(40, measurable=True)
    assert real["decision_grade"] == "blocked_protocol"

    thin = cohort(3, measurable=True)
    assert thin["decision_grade"] == "blocked_protocol"


def test_open_exposure_attributes_marks_per_wallet():
    """The arm matrix is CLOSED-only, so a chain whose losses sit in open inventory was
    unattributable — live robin showed +$218 from 10 closes while carrying -$1,017 across 143
    open rows, and hopa read +$61 closed while holding -$291 open. Same sign flip, per wallet.
    """
    import sqlite3

    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_paper_report import open_exposure_by_wallet

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    exposure = open_exposure_by_wallet(c, "robin")
    assert exposure == {}          # empty ledger -> no exposure, not a crash


def test_all_unpriced_wallet_reports_unknown_exposure_not_breakeven():
    """0.0 would render as breakeven. A wallet whose every open row is unpriced knows NOTHING
    about its exposure — the same "no evidence reads as neutral" failure the realized/unrealized
    split exists to prevent."""
    import sqlite3

    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_paper_report import open_exposure_by_wallet

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "w1", "alice")
    pid = m.open_position(
        c, source_user_id="w1", source_handle="alice", mint="MINT", ticker="T",
        entry_price=0.001, entry_notional=100.0, their_size_usd=10.0,
        opened_at="2026-08-04T00:00:00+00:00", strategy="control", network_id="solana",
        origin_event_id="ev1", entry_fill_source="route_quote", entry_lag_s=0.5)
    m.insert_fill(c, position_id=pid, kind="entry", price=0.001, qty=100_000.0,
                  notional=100.0, fill_source="route_quote", fraction=1.0,
                  created_at="2026-08-04T00:00:00+00:00")
    c.commit()                                   # open, never marked

    got = open_exposure_by_wallet(c, "solana")["w1"]
    assert got["open_positions"] == 1
    assert got["open_unpriced"] == 1
    assert got["open_marked_pnl"] is None        # unknown, NOT 0.0

    # Once a mark exists it becomes a number.
    c.execute("UPDATE memecoin_positions SET last_quote_price=0.0012 WHERE id=?", (pid,))
    c.commit()
    assert open_exposure_by_wallet(c, "solana")["w1"]["open_marked_pnl"] == 20.0


def test_open_exposure_uses_the_reports_own_mark_model():
    """It must not re-derive the mark in SQL: one definition (`_add_outcome`) or the trader table
    and the paper report can disagree about the same position."""
    import inspect

    from sniper.services import memecoin_paper_report as rep

    src = inspect.getsource(rep.open_exposure_by_wallet)
    assert "_add_outcome" in src
    assert "_project_rows" in src
    assert "SELECT" not in src.upper().replace("_PROJECT_ROWS", "")


def test_zero_recovery_contribution_can_be_POSITIVE_when_partials_beat_the_write_off():
    """"Censoring can never improve the number" is FALSE as a general invariant (2026-08-04
    review). If observable partial exits exceed the cost of the unobserved remainder, the
    censored row's zero-recovery contribution is positive.

    $100 entry, half observably sold for $200, remaining half written to zero -> +$100.
    """
    from datetime import datetime, timezone

    from sniper.services.memecoin_paper_report import _add_outcome

    row = _add_outcome({
        "is_open": 0, "entry_price": 0.001, "exit_reason": "real_trail_noquote",
        "last_quote_price": None, "realized_pnl": None,
        "report_entry_qty": 100_000.0,
        "report_exit_qty": 100_000.0,          # fully exited...
        "report_exit_proceeds": 200.0,
        "report_observed_exit_qty": 50_000.0,  # ...but only half at an observable price
        "report_observed_exit_proceeds": 200.0,
    }, datetime(2026, 8, 4, tzinfo=timezone.utc))

    assert row["report_outcome_state"] == "censored_noquote"
    assert row["report_pnl"] is None                     # omitted from the known subtotal
    # observed_booked (200 - 50) - entry * observed_remaining (50) = +100
    assert row["report_conservative_pnl"] == 100.0       # POSITIVE, not -entry_notional


def test_missing_entry_fill_is_omitted_not_written_off():
    """Both values are None — the row is excluded from every total, never charged as a loss."""
    from datetime import datetime, timezone

    from sniper.services.memecoin_paper_report import _add_outcome

    row = _add_outcome({
        "is_open": 0, "entry_price": 0.001, "exit_reason": "bracket_exit",
        "last_quote_price": None, "realized_pnl": 5.0,
        "report_entry_qty": 0.0, "report_exit_qty": 0.0, "report_exit_proceeds": 0.0,
        "report_observed_exit_qty": 0.0, "report_observed_exit_proceeds": 0.0,
    }, datetime(2026, 8, 4, tzinfo=timezone.utc))
    assert row["report_outcome_state"] == "invalid_missing_entry_fill"
    assert row["report_pnl"] is None and row["report_conservative_pnl"] is None


def test_a_stale_mark_is_flagged_but_never_penalised():
    """The zero-recovery stress does NOT touch marked opens, however old the mark. That is why it
    is not a floor on eventual PnL — only on missingness."""
    from datetime import datetime, timedelta, timezone

    from sniper.services.memecoin_paper_report import _add_outcome

    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    row = _add_outcome({
        "is_open": 1, "entry_price": 0.001, "exit_reason": None,
        "last_quote_price": 0.002,
        "last_quote_ts": (now - timedelta(hours=6)).isoformat(),
        "realized_pnl": None, "report_entry_qty": 100_000.0, "report_exit_qty": 0.0,
        "report_exit_proceeds": 0.0, "report_observed_exit_qty": 0.0,
        "report_observed_exit_proceeds": 0.0,
    }, now)
    assert row["report_mark_stale"] is True               # flagged...
    assert row["report_conservative_pnl"] == row["report_pnl"] == 100.0   # ...never penalised
