"""Item 18 — arming + kill switch. Real entries require MEMECOIN_REAL_ENABLED AND no kill switch.
The kill switch is a file checked every cycle + before every submit; tripping it halts ENTRIES
(exits keep running so open real positions still get their brackets)."""
from sniper.services import memecoin_arming as a
from sniper.settings import memecoin as cfg


def test_kill_switch_file_toggles(tmp_path):
    p = str(tmp_path / "kill")
    assert a.is_kill_tripped(p) is False
    a.trip_kill_switch(p)
    assert a.is_kill_tripped(p) is True
    a.clear_kill_switch(p, expected_token=a.kill_switch_status(p)["kill_token"])
    assert a.is_kill_tripped(p) is False


def test_real_entries_gated_by_flag_and_kill(monkeypatch, tmp_path):
    p = str(tmp_path / "kill")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", False)
    assert a.real_entries_allowed(kill_path=p) is False      # flag off -> disarmed
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    assert a.real_entries_allowed(kill_path=p) is True        # armed, no kill
    a.trip_kill_switch(p)
    assert a.real_entries_allowed(kill_path=p) is False       # kill halts entries even when armed


def test_disarmed_by_default():
    # nothing is armed unless explicitly enabled
    assert cfg.MEMECOIN_REAL_ENABLED is False


def test_the_two_stand_downs_are_distinguishable():
    """`disarmed` covered two different operator situations with one label. A deliberate
    stand-down and a forgotten emergency stop must not read the same on the refusal tape --
    on 2026-09-02 the kill switch stayed tripped 11.6 hours because they did."""
    import sniper.settings.memecoin as cfg
    from sniper.services.memecoin_arming import real_entry_block_reason
    import tempfile, os, pathlib

    d = tempfile.mkdtemp()
    kill = os.path.join(d, "memecoin_kill_switch")
    prev = cfg.MEMECOIN_REAL_ENABLED
    try:
        cfg.MEMECOIN_REAL_ENABLED = False
        assert real_entry_block_reason(kill) == "real_disabled"

        cfg.MEMECOIN_REAL_ENABLED = True
        assert real_entry_block_reason(kill) is None, "clear switch + armed must allow entries"

        pathlib.Path(kill).write_text("tripped by test")
        assert real_entry_block_reason(kill) == "kill_switch_tripped"

        # Precedence: a disabled arm is reported even while the switch is also tripped, because
        # clearing the switch alone would not restore entries.
        cfg.MEMECOIN_REAL_ENABLED = False
        assert real_entry_block_reason(kill) == "real_disabled"
    finally:
        cfg.MEMECOIN_REAL_ENABLED = prev


def test_the_reason_reaches_the_refusal_tape():
    """The label is only worth anything if _trace_terminal carries it into terminal_reason."""
    from sniper.services.memecoin_real_runner import _trace_terminal
    assert _trace_terminal({"status": "disarmed", "reason": "kill_switch_tripped"}) == (
        "refused", "kill_switch_tripped")
    # Unchanged when no reason is supplied -- the pre-existing shape still degrades to the status.
    assert _trace_terminal({"status": "disarmed"}) == ("refused", "disarmed")


def test_the_kill_switch_names_who_holds_it(tmp_path):
    """Five deploy holds were cleared from the dashboard within seconds on 2026-09-17 because the
    cockpit showed only a boolean. The file already carried the source."""
    from sniper.services.memecoin_arming import (
        clear_kill_switch, kill_switch_source, trip_kill_switch)
    path = str(tmp_path / "kill")

    assert kill_switch_source(path) is None
    trip_kill_switch(path, source="alpha-deploy:20260917T191020Z-4178714")
    assert kill_switch_source(path) == "alpha-deploy:20260917T191020Z-4178714"
    clear_kill_switch(path, source="web:127.0.0.1",
                      expected_token=a.kill_switch_status(path)["kill_token"], override_deploy=True)
    assert kill_switch_source(path) is None


def test_the_status_reports_the_dashboard_build_it_serves():
    """A page left open since before a deploy keeps its old JS; on 2026-09-17 such a page sent no
    kill-switch token and five Clear clicks were rejected with nothing on screen saying why."""
    import os
    from sniper.routes.api_memecoin import _ui_build

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert _ui_build() == str(int(os.path.getmtime(os.path.join(here, "static", "app.js"))))
