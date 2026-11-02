"""Paper ledger: book sweeps, policy identity, crossing rule, and exit accounting."""
import importlib.util
import pathlib
import sqlite3
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "run_hl_altshort_paper",
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "run_hl_altshort_paper.py")
paper = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(paper)


def test_book_sweep_is_volume_weighted_not_top_of_book():
    levels = [{"px": 100.0, "sz": 1.0}, {"px": 99.0, "sz": 10.0}]
    px, used = paper.walk_book(levels, 200.0)          # 100 usd at 100, 100 usd at 99
    assert used == 2
    assert px == pytest.approx(200.0 / (1.0 + 100.0 / 99.0))
    assert px < 100.0                                   # sweeping costs more than the top level


def test_small_clip_fills_inside_the_first_level():
    levels = [{"px": 100.0, "sz": 1.0}, {"px": 99.0, "sz": 10.0}]
    px, used = paper.walk_book(levels, 40.0)
    assert used == 1 and px == pytest.approx(100.0)


def test_insufficient_depth_refuses_rather_than_extrapolating():
    px, _ = paper.walk_book([{"px": 100.0, "sz": 0.1}], 5_000.0)
    assert px is None


def test_zero_and_negative_levels_are_skipped():
    px, used = paper.walk_book(
        [{"px": 0.0, "sz": 5.0}, {"px": -1.0, "sz": 5.0}, {"px": 10.0, "sz": 100.0}], 100.0)
    assert used == 1 and px == pytest.approx(10.0)


def test_arm_identity_is_policy_not_code():
    """A code edit must not mint a new arm; a policy edit must."""
    first = paper.arm_id()
    assert first == paper.arm_id()
    original = paper.POLICY["cap_hours"]
    try:
        paper.POLICY["cap_hours"] = 96
        assert paper.arm_id() != first
    finally:
        paper.POLICY["cap_hours"] = original
    assert paper.arm_id() == first


def test_arm_row_rejects_a_policy_change_under_the_same_id(tmp_path):
    conn = paper.connect(str(tmp_path / "p.db"))
    paper.ensure_arm(conn, 40.0, 5, 1_000)
    conn.execute("UPDATE hl_paper_arms SET policy_hash='tampered'")
    with pytest.raises(RuntimeError):
        paper.ensure_arm(conn, 40.0, 5, 2_000)


def test_arm_row_rejects_persisted_semantic_drift(tmp_path):
    conn = paper.connect(str(tmp_path / "p.db"))
    paper.ensure_arm(conn, 40.0, 5, 1_000)
    conn.execute("UPDATE hl_paper_arms SET policy_json='{}'")
    with pytest.raises(RuntimeError):
        paper.ensure_arm(conn, 40.0, 5, 2_000)


def test_funding_percentile_needs_history_and_never_guesses():
    f24, pct = paper.funding_self_pct("NOPE", {}, 1_000 * paper.HOUR_MS)
    assert f24 is None and pct is None


def test_funding_percentile_ranks_within_the_coins_own_history():
    now = 2_000 * paper.HOUR_MS
    rates = {now - h * paper.HOUR_MS: 0.00001 for h in range(1, 721)}
    for h in range(1, 25):                              # last 24h far above the coin's own norm
        rates[now - h * paper.HOUR_MS] = 0.001
    f24, pct = paper.funding_self_pct("X", {"X": rates}, now)
    assert f24 == pytest.approx(10.0)
    assert pct > 0.94993                                # would be skipped by the frozen gate


def test_schema_holds_the_dual_size_fill_columns(tmp_path):
    conn = paper.connect(str(tmp_path / "p.db"))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(hl_paper_positions)")}
    assert {"entry_px_clip", "entry_px_ref", "exit_px_clip", "exit_px_ref"} <= cols
    assert {"modelled_gross_bps", "fill_delta_bps"} <= cols   # the calibration deliverable
    assert "funding_provenance_status" in cols


def test_incomplete_hour_is_never_a_nomination_bar():
    rows = [
        {"t": 0, "T": 3_599_999, "c": "1.0"},
        {"t": 3_600_000, "T": 7_199_999, "c": "1.1"},
        {"t": 7_200_000, "T": 10_799_999, "c": "1.4"},
    ]
    assert paper.completed_hourly_closes(rows, now_ms=8_000_000) == [
        (0, 1.0), (3_600_000, 1.1)
    ]


def test_hourly_closes_fetches_only_after_cached_decision(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        paper, "_post_info",
        lambda payload: captured.setdefault("payload", payload) or [],
    )
    now = 200 * paper.HOUR_MS
    cached = 150 * paper.HOUR_MS

    paper.hourly_closes("ALT", now_ms=now, start_ms=cached + paper.HOUR_MS)

    req = captured["payload"]["req"]
    assert req["startTime"] == cached + paper.HOUR_MS
    assert req["endTime"] == now


def test_causal_regime_uses_only_trailing_provider_cased_bars():
    decision = 72 * paper.HOUR_MS
    bars = {
        "kPEPE": [(0, 100.0), (decision, 90.0)],
        "ALT": [(0, 200.0), (decision, 180.0)],
    }
    regime = paper.causal_regime_snapshot(
        bars, decision_ts_ms=decision, universe=["kPEPE", "ALT"]
    )
    assert regime == {
        "decision_ts_ms": decision, "eligible_n": 2, "covered_n": 2,
        "coverage": 1.0, "basket_return_bps": pytest.approx(-1000.0),
        "label": "bearish",
    }


def test_exit_walks_the_exact_quantity_opened():
    entry = paper.walk_book_notional(
        [{"px": 100.0, "sz": 1.0}, {"px": 99.0, "sz": 10.0}], 200.0
    )
    assert entry == pytest.approx({"px": 200.0 / (1.0 + 100.0 / 99.0),
                                   "qty": 1.0 + 100.0 / 99.0, "levels": 2})
    exit_fill = paper.walk_book_quantity(
        [{"px": 101.0, "sz": 1.0}, {"px": 102.0, "sz": 10.0}], entry["qty"]
    )
    assert exit_fill["qty"] == pytest.approx(entry["qty"])
    assert exit_fill["levels"] == 2


def _crossing_closes():
    values = [1.0] * 73 + [1.29, 1.31]
    return [(i * paper.HOUR_MS, value) for i, value in enumerate(values)]


def _episode_closes(decision_hour, *, trough_hour=30, trough=0.5):
    values = [0.60] * (decision_hour + 1)
    values[trough_hour] = trough
    values[decision_hour - 1] = trough * 1.29
    values[decision_hour] = trough * 1.31
    return [(i * paper.HOUR_MS, value) for i, value in enumerate(values)]


def test_repeat_crossings_from_one_trough_are_persisted_but_stood_down(tmp_path, monkeypatch):
    conn = paper.connect(str(tmp_path / "p.db"))
    aid = paper.ensure_arm(conn, 40.0, 5, 10_000)
    current = {"decision": 74}
    monkeypatch.setattr(
        paper, "hourly_closes",
        lambda _coin, **_kw: _episode_closes(current["decision"]),
    )
    monkeypatch.setattr(paper, "funding_self_pct", lambda *_a: (10.0, 1.0))
    monkeypatch.setattr(paper.time, "sleep", lambda _s: None)

    for decision in (74, 76, 81, 85):
        current["decision"] = decision
        assert paper.scan_and_open(
            conn, aid, ["BOME"], {}, clip_usd=40.0, max_concurrent=5,
            now_ms=(decision + 1) * paper.HOUR_MS + 1, pace_s=0,
        ) == 0

    rows = conn.execute(
        "SELECT episode_id,episode_primary,portfolio_slot_eligible,entry_status,entry_reason "
        "FROM hl_paper_observations ORDER BY decision_ts_ms"
    ).fetchall()
    assert len(rows) == 4 and len({row["episode_id"] for row in rows}) == 1
    assert [row["episode_primary"] for row in rows] == [1, 0, 0, 0]
    assert [row["entry_reason"] for row in rows] == [None] + ["repeat_same_episode"] * 3
    assert all(row["portfolio_slot_eligible"] == 0 for row in rows[1:])


def test_new_causal_trough_resets_nomination_episode(tmp_path, monkeypatch):
    conn = paper.connect(str(tmp_path / "p.db"))
    aid = paper.ensure_arm(conn, 40.0, 5, 10_000)
    current = {"rows": _episode_closes(74)}
    monkeypatch.setattr(paper, "hourly_closes", lambda _coin, **_kw: current["rows"])
    monkeypatch.setattr(paper, "funding_self_pct", lambda *_a: (10.0, 1.0))
    monkeypatch.setattr(paper.time, "sleep", lambda _s: None)
    paper.scan_and_open(
        conn, aid, ["BOME"], {}, clip_usd=40.0, max_concurrent=5,
        now_ms=75 * paper.HOUR_MS + 1, pace_s=0,
    )
    current["rows"] = _episode_closes(85, trough_hour=75, trough=0.4)
    paper.scan_and_open(
        conn, aid, ["BOME"], {}, clip_usd=40.0, max_concurrent=5,
        now_ms=86 * paper.HOUR_MS + 1, pace_s=0,
    )
    rows = conn.execute(
        "SELECT episode_id,episode_primary,trough_ts_ms FROM hl_paper_observations "
        "ORDER BY decision_ts_ms"
    ).fetchall()
    assert len(rows) == 2 and rows[0]["episode_id"] != rows[1]["episode_id"]
    assert [row["episode_primary"] for row in rows] == [1, 1]
    assert [row["trough_ts_ms"] for row in rows] == [30 * paper.HOUR_MS, 75 * paper.HOUR_MS]


def test_capacity_marks_portfolio_ineligible_but_does_not_censor_event_proof(tmp_path, monkeypatch):
    conn = paper.connect(str(tmp_path / "p.db"))
    aid = paper.ensure_arm(conn, 40.0, 1, 10_000)
    monkeypatch.setattr(paper, "hourly_closes", lambda _coin, **_kw: _crossing_closes())
    monkeypatch.setattr(paper, "funding_self_pct", lambda *_a: (1.0, 0.5))
    monkeypatch.setattr(paper, "book_fill", lambda _coin, _amount, _side, **_kw: {
        "clip_px": 100.0, "clip_qty": 0.4, "clip_levels": 1,
        "ref_px": 99.0, "ref_qty": 50.0, "ref_levels": 2,
        "mid": 100.5, "spread_bps": 10.0,
    })
    monkeypatch.setattr(paper.time, "sleep", lambda _s: None)
    opened = paper.scan_and_open(
        conn, aid, ["kPEPE", "kBONK"], {}, clip_usd=40.0, max_concurrent=1,
        now_ms=75 * paper.HOUR_MS + 1_000, pace_s=0,
    )
    assert opened == 2
    assert conn.execute("SELECT COUNT(*) FROM hl_paper_observations").fetchone()[0] == 2
    assert {row[0] for row in conn.execute(
        "SELECT portfolio_slot_eligible FROM hl_paper_observations"
    )} == {0, 1}
    assert {row[0] for row in conn.execute(
        "SELECT regime_label FROM hl_paper_observations"
    )} == {"bullish"}


def test_scan_persists_hourly_regime_even_without_a_nomination(tmp_path, monkeypatch):
    conn = paper.connect(str(tmp_path / "p.db"))
    aid = paper.ensure_arm(conn, 40.0, 5, 10_000)
    closes = [(i * paper.HOUR_MS, 1.0) for i in range(75)]
    monkeypatch.setattr(paper, "hourly_closes", lambda _coin, **_kw: closes)
    monkeypatch.setattr(paper.time, "sleep", lambda _s: None)
    assert paper.scan_and_open(
        conn, aid, ["kPEPE", "ALT"], {}, clip_usd=40.0, max_concurrent=5,
        now_ms=75 * paper.HOUR_MS + 1_000, pace_s=0,
    ) == 0
    row = conn.execute("SELECT * FROM hl_paper_regimes").fetchone()
    assert row["decision_ts_ms"] == 74 * paper.HOUR_MS
    assert row["label"] == "neutral"
    assert row["coverage"] == 1.0


def test_scan_stops_between_provider_calls_without_writing_partial_regime(tmp_path, monkeypatch):
    conn = paper.connect(str(tmp_path / "p.db"))
    aid = paper.ensure_arm(conn, 40.0, 5, 10_000)
    fetched = []
    monkeypatch.setattr(paper, "hourly_closes", lambda coin, **_kw: fetched.append(coin) or [])
    monkeypatch.setattr(paper.time, "sleep", lambda _s: None)
    paper.scan_and_open(
        conn, aid, ["A", "B"], {}, clip_usd=40.0, max_concurrent=5,
        now_ms=75 * paper.HOUR_MS, pace_s=0, should_stop=lambda: len(fetched) >= 1,
    )
    assert fetched == ["A"]
    assert conn.execute("SELECT COUNT(*) FROM hl_paper_regimes").fetchone()[0] == 0


def _insert_open_position(conn, aid, *, now_ms, deadline_ms, entry=100.0):
    conn.execute(
        "INSERT INTO hl_paper_observations "
        "(obs_id,arm_id,coin,decision_ts_ms,decision_close,runup_72h,funding_24h_bps,"
        "funding_self_pct,gated_in,gate_reason,created_ts_ms,portfolio_slot_eligible,entry_status) "
        "VALUES ('o',?,'kPEPE',1,1,0.31,1,0.5,1,'accepted',1,1,'opened')", (aid,)
    )
    conn.execute(
        "INSERT INTO hl_paper_positions "
        "(position_id,arm_id,obs_id,coin,status,clip_usd,entry_ts_ms,entry_px_clip,"
        "entry_qty_clip,entry_px_ref,entry_qty_ref,entry_mid,entry_spread_bps,stop_px,target_px,"
        "deadline_ts_ms) VALUES ('o',?,'o','kPEPE','open',40,?,?,?,?,?,100,10,120,85,?)",
        (aid, now_ms - paper.HOUR_MS, entry, 0.4, 99.0, 50.0, deadline_ms),
    )


def test_missing_close_funding_never_becomes_zero_net(tmp_path, monkeypatch):
    conn = paper.connect(str(tmp_path / "p.db"))
    aid = paper.ensure_arm(conn, 40.0, 5, 1)
    _insert_open_position(conn, aid, now_ms=10_000, deadline_ms=20_000)
    monkeypatch.setattr(paper, "book_fill", lambda *_a, **_kw: {
        "clip_px": 121.0, "ref_px": 121.5, "mid": 120.5, "spread_bps": 5.0,
        "clip_qty": 0.4, "ref_qty": 50.0,
    })
    monkeypatch.setattr(paper, "fetch_funding_history", lambda *_a, **_kw: (_ for _ in ()).throw(
        TimeoutError("funding unavailable")
    ))
    monkeypatch.setattr(paper.time, "sleep", lambda _s: None)
    assert paper.manage_open(conn, aid, now_ms=10_000, pace_s=0) == 1
    row = conn.execute("SELECT * FROM hl_paper_positions").fetchone()
    assert row["funding_status"] == "fetch_failed"
    assert row["funding_bps"] is None and row["net_bps_clip"] is None


def test_close_dual_writes_canonical_funding_contribution(tmp_path, monkeypatch):
    conn = paper.connect(str(tmp_path / "p.db"))
    aid = paper.ensure_arm(conn, 40.0, 5, 1)
    now_ms = paper.HOUR_MS + 1
    _insert_open_position(conn, aid, now_ms=now_ms, deadline_ms=now_ms)
    monkeypatch.setattr(paper, "book_fill", lambda *_a, **_kw: {
        "clip_px": 121.0, "ref_px": 121.5, "mid": 120.5, "spread_bps": 5.0,
        "clip_qty": 0.4, "ref_qty": 50.0,
    })
    monkeypatch.setattr(paper, "fetch_funding_history", lambda *_a, **_kw: [
        {"time": paper.HOUR_MS, "rate": 0.0001}
    ])
    monkeypatch.setattr(paper.time, "sleep", lambda _s: None)
    assert paper.manage_open(conn, aid, now_ms=now_ms, pace_s=0) == 1
    row = conn.execute("SELECT * FROM hl_paper_positions").fetchone()
    assert row["funding_pnl_bps"] == 1.0
    assert row["funding_bps"] == 1.0
    assert row["funding_status"] == "complete"
    assert row["funding_provenance_status"] == "complete"


def test_legacy_observations_without_episode_stay_unclassified(tmp_path):
    conn = paper.connect(str(tmp_path / "p.db"))
    aid = paper.ensure_arm(conn, 40.0, 5, 1)
    conn.execute(
        "INSERT INTO hl_paper_observations(obs_id,arm_id,coin,decision_ts_ms,decision_close,"
        "runup_72h,gated_in,gate_reason,created_ts_ms) VALUES ('legacy',?,'ALT',1,1,0,0,'x',1)",
        (aid,),
    )
    row = conn.execute(
        "SELECT episode_id,episode_primary FROM hl_paper_observations WHERE obs_id='legacy'"
    ).fetchone()
    assert row["episode_id"] is None and row["episode_primary"] is None


def test_migration_tolerates_legacy_not_null_episode_column(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(paper.SCHEMA.replace(
        "episode_id TEXT, episode_primary INTEGER,",
        "episode_id TEXT, episode_primary INTEGER NOT NULL DEFAULT 1,",
    ))
    conn.execute(
        "INSERT INTO hl_paper_observations(obs_id,arm_id,coin,decision_ts_ms,decision_close,"
        "runup_72h,gated_in,gate_reason,created_ts_ms) VALUES "
        "('legacy','arm','ALT',1,1,0,0,'x',1)"
    )
    conn.commit()
    conn.close()

    migrated = paper.connect(str(db_path))
    row = migrated.execute(
        "SELECT episode_id,episode_primary FROM hl_paper_observations WHERE obs_id='legacy'"
    ).fetchone()

    assert row["episode_id"] is None and row["episode_primary"] == 1


def test_cap_fill_delta_compares_book_to_mid_not_itself(tmp_path, monkeypatch):
    conn = paper.connect(str(tmp_path / "p.db"))
    aid = paper.ensure_arm(conn, 40.0, 5, 1)
    _insert_open_position(conn, aid, now_ms=10_000, deadline_ms=9_000)
    monkeypatch.setattr(paper, "book_fill", lambda *_a, **_kw: {
        "clip_px": 101.0, "ref_px": 101.5, "mid": 100.0, "spread_bps": 5.0,
        "clip_qty": 0.4, "ref_qty": 50.0,
    })
    monkeypatch.setattr(paper, "fetch_funding_history", lambda *_a, **_kw: [])
    monkeypatch.setattr(paper.time, "sleep", lambda _s: None)
    paper.manage_open(conn, aid, now_ms=10_000, pace_s=0)
    row = conn.execute("SELECT * FROM hl_paper_positions").fetchone()
    assert row["modelled_gross_bps"] == pytest.approx(0.0)
    assert row["gross_bps_clip"] == pytest.approx(-100.0)
    assert row["fill_delta_bps"] == pytest.approx(-100.0)


def test_execution_size_and_capacity_are_part_of_arm_identity():
    assert paper.arm_id(40.0, 5) != paper.arm_id(50.0, 5)
    assert paper.arm_id(40.0, 5) != paper.arm_id(40.0, 6)


def test_new_arm_heartbeat_is_published_before_first_universe_scan(tmp_path, monkeypatch):
    db_path = tmp_path / "paper.db"
    heartbeat_path = tmp_path / "paper.heartbeat"
    sources = {}
    monkeypatch.setattr(
        paper, "seed_funding_cache",
        lambda path: sources.setdefault("cache", path) and {},
    )
    monkeypatch.setattr(
        paper, "extend_funding_from_tape",
        lambda cache, path, **_kw: sources.setdefault("tape", path) and 0,
    )
    monkeypatch.setattr(paper, "live_universe", lambda: ["kPEPE"])
    monkeypatch.setattr(paper, "manage_open", lambda *_a, **_kw: 0)

    def assert_heartbeat_precedes_scan(*_args, **_kwargs):
        payload = __import__("json").loads(heartbeat_path.read_text())
        assert payload["arm_id"] == paper.arm_id(40.0, 5)
        assert payload["phase"] == "scanning"
        return 0

    monkeypatch.setattr(paper, "scan_and_open", assert_heartbeat_precedes_scan)
    assert paper.main([
        "--db-path", str(db_path), "--heartbeat", str(heartbeat_path),
        "--funding-cache-dir", "/shared/funding", "--funding-tape", "/shared/tape.db",
        "--pace-seconds", "0", "--once",
    ]) == 0
    assert sources == {"cache": "/shared/funding", "tape": "/shared/tape.db"}


def test_funding_tape_extension_reads_only_after_cursor(tmp_path):
    tape = tmp_path / "tape.db"
    conn = __import__("sqlite3").connect(tape)
    conn.execute("CREATE TABLE hl_squeeze_marks(coin TEXT, ts_ms INTEGER, funding REAL)")
    conn.executemany("INSERT INTO hl_squeeze_marks VALUES (?,?,?)", [
        ("BTC", 100, 0.001), ("ETH", 200, 0.002), ("BTC", 300, 0.003),
    ])
    conn.commit()
    conn.close()
    cache = {}

    cursor = paper.extend_funding_from_tape(cache, str(tape), after_ts_ms=150)

    assert cursor == 300
    assert cache["BTC"][0] == 0.003
    assert cache["ETH"][0] == 0.002


def test_completed_provider_hour_advances_once_per_hour():
    assert paper.completed_provider_hour(7_200_001) == 3_600_000
    assert paper.completed_provider_hour(10_799_999) == 3_600_000
    assert paper.completed_provider_hour(10_800_000) == 7_200_000
