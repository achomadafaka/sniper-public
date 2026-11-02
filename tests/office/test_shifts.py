"""Task 19 (Lambda): shifts and written handovers. Pure scheduling/validation; the scheduler hooks are Codex's (T6)."""
import hashlib
from pathlib import Path

import pytest

from sniper.office import shifts as S

CFG = Path(__file__).resolve().parents[2] / "config" / "office" / "shifts.json"
DAY = 86400.0
D0 = 1_800_000_000.0 // DAY * DAY


def cfg():
    return S.load(CFG, hashlib.sha256(CFG.read_bytes()).hexdigest())


def test_config_is_pinned_and_covers_the_whole_day_without_gaps_or_overlap(tmp_path):
    c = cfg()
    spans = sorted((s["start_hour"], s["end_hour"]) for s in c["shifts"])
    assert spans[0][0] == 0 and spans[-1][1] == 24 and all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
    other = tmp_path / "s.json"
    other.write_text(CFG.read_text().replace('"end_hour": 8', '"end_hour": 7'))
    with pytest.raises(S.ConfigMismatch):
        S.load(other, hashlib.sha256(CFG.read_bytes()).hexdigest())


def test_shift_at_and_boundaries_in_utc():
    c = cfg()
    assert S.shift_at(c, D0 + 3 * 3600)["id"] == "night" and S.shift_at(c, D0 + 8 * 3600)["id"] == "day" and S.shift_at(c, D0 + 23.99 * 3600)["id"] == "evening"


def test_handover_window_is_the_minutes_around_each_boundary():
    c = cfg()
    assert S.in_handover(c, D0 + 8 * 3600 - 60) and S.in_handover(c, D0 + 8 * 3600 + 5 * 60) and not S.in_handover(c, D0 + 8 * 3600 + 20 * 60)
    assert S.in_handover(c, D0 + 5) and S.in_handover(c, D0 + 23.9 * 3600)            # the midnight boundary wraps the day: 06 minutes before it counts


def test_the_floor_is_never_unstaffed_a_missing_mandatory_seat_is_reported():
    c = cfg()
    assert S.missing_seats(c, {"Head_or_deputy": 1, "Risk": 1, "Trader": 1, "Desk ops": 1, "Postmortem": 1}) == []
    assert S.missing_seats(c, {"Head_or_deputy": 1, "Risk": 0, "Trader": 1}) == ["Desk ops", "Postmortem", "Risk"]       # absent and zero are both missing


def test_a_handover_note_needs_every_section_and_evidence_refs():
    c = cfg()
    note = {"open_positions": [{"mint": "M", "ref": "ledger:1"}], "theses_in_flight": [], "watchlists": [{"mint": "W", "ref": "memo:9"}], "alerts": []}
    assert S.validate_note(c, note) == []
    bad = {"open_positions": [{"mint": "M"}], "watchlists": []}
    problems = S.validate_note(c, bad)
    assert "missing_section:theses_in_flight" in problems and "missing_section:alerts" in problems and "no_evidence_ref:open_positions[0]" in problems


def test_an_unprocessed_opportunity_in_the_handover_window_stays_in_the_universe_as_skip():
    c = cfg()
    t = D0 + 8 * 3600 + 60
    assert S.classify_opportunity(c, t, processed=False) == {"in_universe": True, "state": "skip_unprocessed_handover"}
    assert S.classify_opportunity(c, t, processed=True) == {"in_universe": True, "state": "processed"}
    assert S.classify_opportunity(c, D0 + 12 * 3600, processed=False) == {"in_universe": True, "state": "unprocessed"}       # a stall outside handover is a stall, not a handover SKIP


def test_null_latency_is_frozen_per_shift_and_missing_is_refused_not_defaulted():
    table = {"night": 14.0, "day": 9.0, "evening": 11.0}
    c = cfg()
    assert S.null_latency(c, table, D0 + 9 * 3600) == 9.0
    with pytest.raises(S.MissingLatency):
        S.null_latency(c, {"night": 14.0, "day": 9.0}, D0 + 20 * 3600)


def test_occurrence_is_dated_and_the_handover_id_is_stable_across_restarts_and_distinct_across_days():
    c = cfg()
    a = S.occurrence(c, D0 + 9 * 3600)
    assert a == {"shift_id": "day", "start_t": D0 + 8 * 3600, "end_t": D0 + 16 * 3600}
    assert S.occurrence(c, D0 + 15.9 * 3600) == a                                           # same occurrence at any time inside it (a restart recomputes the same value)
    nxt = S.occurrence(c, D0 + DAY + 9 * 3600)
    assert nxt["shift_id"] == "day" and nxt["start_t"] == a["start_t"] + DAY                # the same label on the next day is a different occurrence
    h = S.handover_id(c, D0 + 16 * 3600 + 5)                                                # day -> evening
    assert h == S.handover_id(c, D0 + 16 * 3600 + 600) and len(h) == 32 and h != S.handover_id(c, D0 + DAY + 16 * 3600 + 5)


def test_the_midnight_handover_belongs_to_the_boundary_instant_not_to_two_dates():
    c = cfg()
    before = S.handover_id(c, D0 + 23.99 * 3600)            # evening shift still running: its NEXT boundary is midnight; handover_id is for the shift that has just STARTED
    just_after = S.handover_id(c, D0 + DAY + 60)             # evening -> night at the next UTC midnight
    later = S.handover_id(c, D0 + DAY + 3 * 3600)
    assert just_after == later and before != just_after
    assert S.occurrence(c, D0 + DAY + 60) == {"shift_id": "night", "start_t": D0 + DAY, "end_t": D0 + DAY + 8 * 3600}
