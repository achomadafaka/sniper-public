"""Item 6 — feed staleness gate + oldest-first. On the real path, a buy event older than a tight
max-age is DROPPED (the pump is over; buying it is a dead bag with a burned exit signal — the
outage-replay hole). Backlog is processed chronologically. Opt-in via entry_max_age_s so the paper
A/B (Gate A evidence) is byte-unchanged; only the real leg enables it."""
from sniper.services.memecoin_runner import is_stale_entry, sort_events_oldest_first


class _Ev:
    def __init__(self, created_at): self.created_at = created_at


def test_stale_buy_beyond_max_age_is_dropped():
    assert is_stale_entry("2026-07-09T10:00:00Z", "2026-07-09T10:03:30Z", 120) is True   # 210s > 120


def test_fresh_buy_within_max_age_kept():
    assert is_stale_entry("2026-07-09T10:00:00Z", "2026-07-09T10:01:00Z", 120) is False  # 60s < 120


def test_unparseable_timestamp_is_treated_stale_fail_closed():
    assert is_stale_entry("not-a-time", "2026-07-09T10:00:00Z", 120) is True   # can't age-check -> drop


def test_backlog_sorted_oldest_first():
    evs = [_Ev("2026-07-09T10:02:00Z"), _Ev("2026-07-09T10:00:00Z"), _Ev("2026-07-09T10:01:00Z")]
    out = sort_events_oldest_first(evs)
    assert [e.created_at for e in out] == ["2026-07-09T10:00:00Z", "2026-07-09T10:01:00Z", "2026-07-09T10:02:00Z"]
