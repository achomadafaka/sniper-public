from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_crypto5m_watchdog_regime_v1_retired_with_fleet():
    # RETIRED 2026-07-08 (maker fleet retirement — no realizable edge): regime-v1 commented out,
    # revival stanza kept as a comment.
    script = (ROOT / "scripts" / "crypto5m_watchdog.sh").read_text()
    active = "\n".join(_active_lines(script))
    assert "maker-regime-v1" not in active
    assert "--campaign maker-regime-v1" in script


def test_crypto5m_watchdog_maker_fleet_fully_retired_2026_07_08():
    # 2026-07-08: cancel-aware queue replay (price_change/cancel stream) proved the whole maker line has
    # NO realizable edge (frozen-f full realizable +$0.004/fill; deep-only null). All campaigns stopped +
    # respawns disabled; the capture data feed stays.
    script = (ROOT / "scripts" / "crypto5m_watchdog.sh").read_text()
    active = "\n".join(_active_lines(script))
    assert 'ensure "crypto5m_maker"' not in active
    for name in ("crypto5m_adaptive_maker_v32f_frozen", "crypto5m_maker_v33_learner",
                 "crypto5m_maker_deep_only_v1", "crypto5m_maker_sweep_1e5", "crypto5m_maker_sweep_5e5"):
        assert f'guard_then_ensure "{name}"' not in active
    assert "CRYPTO5M MAKER FLEET RETIRED 2026-07-08" in script
    # capture STOPPED 2026-07-08 as well — no consumer after the fleet retirement, and it can't test a
    # dead strategy's fills. The parser FIX (69bd231) stays; the live feed is off. Revival = uncomment.
    assert 'ensure "crypto5m_capture"' not in active
    assert "CAPTURE STOPPED 2026-07-08" in script


def test_watchdog_adaptive_v1_stanza_retired_but_documented():
    # RETIRED 2026-07-06: V1 stays in the file as a commented block (revival path + history pointer),
    # but must not be an active ensure.
    script = (ROOT / "scripts" / "crypto5m_watchdog.sh").read_text()

    assert "maker_adaptive_v1.sqlite" in script
    assert "maker_v0.pid" in script


def test_watchdog_v2_retired_but_documented():
    # RETIRED 2026-07-07 (portfolio review): v2 was negative absolute on the forward common set.
    # Commented block kept for revival; DB stays in telemetry retention.
    script = (ROOT / "scripts" / "crypto5m_watchdog.sh").read_text()
    active = "\n".join(_active_lines(script))

    assert 'guard_then_ensure "crypto5m_adaptive_maker_v2"' not in active
    assert "maker_adaptive_v2.pid" not in active
    assert "maker_adaptive_v2.sqlite" in script  # retention list + revival comment retain it


def test_watchdog_adaptive_v3_stanza_retired_but_documented():
    # RETIRED 2026-07-06: V3 superseded by V3.1; commented block kept for revival/history.
    script = (ROOT / "scripts" / "crypto5m_watchdog.sh").read_text()

    assert "maker_adaptive_v3.sqlite" in script
    assert "--maker-adaptive-v3" in script
    assert "--adaptive-control-campaign maker-adaptive-paper-v2" in script


def test_watchdog_v31_retired_but_documented():
    # RETIRED 2026-07-07 (portfolio review): v3.1 was negative absolute on the forward common set.
    script = (ROOT / "scripts" / "crypto5m_watchdog.sh").read_text()
    active = "\n".join(_active_lines(script))

    assert 'guard_then_ensure "crypto5m_adaptive_maker_v31"' not in active
    assert "maker_adaptive_v3_1.pid" not in active
    assert "maker_adaptive_v3_1.sqlite" in script  # retention + revival comment retain it


def _active_lines(script: str) -> list[str]:
    return [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]


def test_watchdog_retired_stanzas_not_active():
    script = (ROOT / "scripts" / "crypto5m_watchdog.sh").read_text()
    active = "\n".join(_active_lines(script))

    # every maker generation — the old ones AND the 2026-07-08 fleet retirement — has no active ensure/guard
    for pid in ("maker_adaptive_v1.pid", "maker_adaptive_v3.pid",
                "maker_adaptive_v2.pid", "maker_adaptive_v3_1.pid", "maker_adaptive_v3_2r.pid",
                "maker_adaptive_v3_2f.pid",
                # fleet retired 2026-07-08 (no realizable edge under cancel-aware replay):
                "maker_adaptive_v3_2f_frozen.pid", "maker_v33_learner.pid", "maker_deep_only_v1.pid",
                "maker_sweep_1e5.pid", "maker_sweep_5e5.pid", "maker_v0.pid"):
        assert pid not in active


def test_watchdog_v32r_and_v32f_learners_retired():
    script = (ROOT / "scripts" / "crypto5m_watchdog.sh").read_text()
    active = "\n".join(_active_lines(script))

    # v3.2r (markout reward lever) RETIRED 2026-07-07 — inert once frozen, folded into v3.3
    assert "--adaptive-reward-version maker-reward-v3.2" not in active
    assert 'guard_then_ensure "crypto5m_adaptive_maker_v32r"' not in active

    # v3.2f old learner RETIRED 2026-07-07 — no lift vs frozen-f
    assert 'guard_then_ensure "crypto5m_adaptive_maker_v32f"' not in active
    assert "--campaign maker-adaptive-paper-v3.2f " not in active  # trailing space: not the -frozen one
    # frozen-f RETIRED 2026-07-08 with the whole fleet (full realizable edge = +$0.004/fill, a naive-fill
    # fantasy under cancel-aware queue replay). Revival stanza kept as a comment.
    assert "--campaign maker-adaptive-paper-v3.2f-frozen" not in active
    assert "maker_adaptive_v3_2f_frozen.sqlite" in script


def test_watchdog_retention_keeps_prints_72h_for_flow_validation():
    script = (ROOT / "scripts" / "crypto5m_watchdog.sh").read_text()

    assert "COALESCE(event_type,'') != 'last_trade_price'" in script
    assert "event_type = 'last_trade_price'" in script
    assert "72 * 3600 * 1000" in script


def test_watchdog_no_active_maker_challengers_after_retirement():
    # 2026-07-08 fleet retirement: no maker campaign is guarded/ensured any longer, but each stanza is
    # kept as a comment (revival path + history pointer).
    script = (ROOT / "scripts" / "crypto5m_watchdog.sh").read_text()
    active = "\n".join(_active_lines(script))

    for name, db, camp in (
        ("crypto5m_adaptive_maker_v32f_frozen", "maker_adaptive_v3_2f_frozen.sqlite",
         "maker-adaptive-paper-v3.2f-frozen"),
        ("crypto5m_maker_v33_learner", "maker_v33_learner.sqlite", "maker-v33-learner-paper"),
        ("crypto5m_maker_sweep_1e5", "maker_sweep_1e5.sqlite", "maker-sweep-floor-1e5"),
        ("crypto5m_maker_sweep_5e5", "maker_sweep_5e5.sqlite", "maker-sweep-floor-5e5"),
        ("crypto5m_maker_deep_only_v1", "maker_deep_only_v1.sqlite", "maker-deep-only-v1"),
    ):
        assert f'guard_then_ensure "{name}"' not in active           # not respawned
        assert f'"data/crypto5m/{db}" "{camp}"' in script            # revival stanza retained as comment


def test_watchdog_deep_only_v1_retired_2026_07_08():
    # RETIRED 2026-07-08: deep-only realizability was null under the cancel-aware replay (trades-only
    # +0.09/fill CI incl 0 at N=259; clean-middle slightly negative). Stanza kept as comment for history.
    script = (ROOT / "scripts" / "crypto5m_watchdog.sh").read_text()
    active = "\n".join(_active_lines(script))
    assert "--deep-only-min-depth 0.03" not in active
    assert "--deep-only-min-depth 0.03" in script      # revival comment retains it
    assert "maker-deep-only-v1" not in active


def test_watchdog_regime_gated_and_sweeps_all_retired():
    # regime-gated v1/v2 retired 2026-07-07; the floor sweep + the rest of the fleet retired 2026-07-08
    # (whole maker line has no realizable edge). All stanzas kept as comments for revival/history.
    script = (ROOT / "scripts" / "crypto5m_watchdog.sh").read_text()
    active = "\n".join(_active_lines(script))
    assert 'guard_then_ensure "crypto5m_maker_regime_gated_v1"' not in active
    assert 'guard_then_ensure "crypto5m_maker_regime_gated_v2"' not in active
    assert "--regime-gated-ramp" not in active
    assert "--regime-gated-floor" not in active
    # floor sweep RETIRED 2026-07-08 — not launched, but names kept for retention + revival
    for camp in ("maker-sweep-floor-1e5", "maker-sweep-floor-5e5"):
        assert f"--campaign {camp} " not in active
        assert camp in script


def test_watchdog_guard_helper_has_fail_open_and_cull_semantics():
    script = (ROOT / "scripts" / "crypto5m_watchdog.sh").read_text()
    # fail-open + pause semantics present in the campaign guard helper
    assert "kill_recommended" in script or "fail-open" in script
    # ensure() culls duplicate writers (added 2026-07-07 after the double-writer race)
    assert "CULLED duplicate" in script


def test_watchdog_telemetry_retention_covers_all_campaign_dbs():
    script = (ROOT / "scripts" / "crypto5m_watchdog.sh").read_text()

    for db in ("maker_v0.sqlite", "maker_adaptive_v2.sqlite", "maker_adaptive_v3_1.sqlite",
               "maker_adaptive_v3_2r.sqlite", "maker_adaptive_v3_2f.sqlite",
               "maker_adaptive_v3_2f_frozen.sqlite", "maker_v33_learner.sqlite"):
        assert script.count(db) >= 1
    assert 'for t in ("maker_quotes", "maker_regime_decisions")' in script
    assert "7 * 86400" in script
