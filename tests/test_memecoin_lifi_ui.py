from pathlib import Path

import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_real_runner import lifi_status_for_contexts


def test_robinhood_venue_card_renders_runner_lifi_state():
    source = Path("static/app.js").read_text()
    card = source[source.index("_venueCard(name"):source.index("_knobsStrip(k)")]

    for field in (
        "mode", "remaining", "exit_reserve", "slots_used", "slot_limit",
        "manifest_hash", "disable_reasons",
    ):
        assert field in card
    assert "STALE" in card
    assert "this._realStatus.lifi" in card


def test_runner_disabled_lifi_snapshot_has_complete_fields(monkeypatch):
    monkeypatch.setenv("MEMECOIN_LIFI_MODE", "disabled")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)

    state = lifi_status_for_contexts(
        conn, {}, "2026-08-30T20:00:00+00:00")

    required = {
        "corrected_at", "runner_heartbeat_at", "release_sha", "manifest_hash",
        "chain_id", "custody_wallet", "mode", "authenticated", "effective_limit",
        "remaining", "reset_s", "in_flight", "exit_reserve", "slots_used",
        "slot_limit", "entry_admitted", "disable_reasons", "unresolved_approvals",
        "unresolved_swaps", "unresolved_revokes", "unresolved_finality",
    }
    assert set(state) == required
    assert state["mode"] == "disabled"
    assert state["disable_reasons"] == ["disabled"]
    assert state["entry_admitted"] is False
