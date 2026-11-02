"""Inline safety host out of monthly RUs (live 2026-10-28: Chainstack HTTP 403 / -32005 'monthly quota'
on every call since 2026-10-22). Not a 429, so no pause; the warmer burned ~5.6 reads/s of 403s. The
host is quota-spent until ONE probe per QUOTA_PROBE_S succeeds; nothing else is sent; a page on entering
and leaving; the warmer stops; the counter reaches the heartbeat."""
import pytest

from sniper.services import memecoin_rh_fast_lane as lane

URL = "https://inline.example/k/"
QUOTA = (403, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message":
         "You've reached your monthly quota of Request Units (RUs)."}}, {})
OK = (200, {"jsonrpc": "2.0", "id": 1, "result": "0x1"}, {})


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def _seam(answers, clock):
    calls, pages = [], []

    def base(method, target, headers=None, body=None, timeout=10):
        calls.append(target)
        return answers.pop(0)
    paced = lane.PacedHttp(min_gap_s=0.0, pause_s=120.0, clock=clock)
    paced.quota_alert = pages.append
    return lane.lane_inline_http(URL, paced, base=base), paced, calls, pages


def test_a_quota_answer_marks_the_host_spent_and_pages_once(caplog):
    clock = Clock()
    http, paced, calls, pages = _seam([QUOTA], clock)
    http("POST", URL)
    assert paced.quota_spent() and paced.counts["quota_403"] == 1 and len(pages) == 1
    with pytest.raises(lane.InlineHostDead):          # a cache miss falls back at once
        http("POST", URL)
    assert len(calls) == 1 and len(pages) == 1        # nothing sent, no second page


def test_only_the_owned_probe_clears_and_a_late_200_does_not():
    clock = Clock()
    http, paced, calls, pages = _seam([QUOTA, OK], clock)
    http("POST", URL)
    paced.note_answer(*OK[:2])                        # a read sent before the mark answering late
    assert paced.quota_spent()
    clock.t += lane.PacedHttp.QUOTA_PROBE_S - 1
    assert not paced.quota_probe_due()
    clock.t += 2
    assert paced.quota_probe_due() and not paced.quota_probe_due()      # one per interval
    paced.quota_probe_result(*QUOTA[:2])              # still spent
    assert paced.quota_spent() and len(pages) == 1
    clock.t += lane.PacedHttp.QUOTA_PROBE_S + 1
    assert paced.quota_probe_due()
    paced.quota_probe_result(*OK[:2])
    assert not paced.quota_spent() and len(pages) == 2 and "back" in pages[-1]
    http("POST", URL)                                 # sends again
    assert len(calls) == 2


def test_the_signal_path_never_waits_on_a_lock_for_the_gate():
    clock = Clock()
    http, paced, calls, pages = _seam([QUOTA], clock)
    http("POST", URL)
    with paced._lock:                                 # a busy lock must not delay a refusal
        with pytest.raises(lane.InlineHostDead):
            http("POST", URL)


def test_other_403s_and_429_are_not_quota():
    clock = Clock()
    http, paced, calls, pages = _seam([(403, {"error": {"code": -32000, "message": "forbidden"}}, {})], clock)
    http("POST", URL)
    assert not paced.quota_spent() and not pages


def test_recovery_runs_with_no_warmer_at_all():
    """CACHED_ONLY=False / INLINE_WARM=False: nothing else touches the host; the recovery step still heals."""
    clock = Clock()
    paced = lane.PacedHttp(min_gap_s=0.0, clock=clock)
    pages = []
    paced.quota_alert = pages.append
    sent = []
    paced.quota_probe = lambda: sent.append(1) or (200, {"jsonrpc": "2.0", "id": 1, "result": "0x1a2b"})
    paced.note_answer(*QUOTA[:2])
    paced.quota_recover()
    assert not sent and paced.quota_spent()            # not due
    clock.t += lane.PacedHttp.QUOTA_PROBE_S + 1
    paced.quota_recover()
    assert sent == [1] and not paced.quota_spent() and len(pages) == 2


@pytest.mark.parametrize("answer", [
    {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "x"}, "result": None},
    {"jsonrpc": "2.0", "id": 1, "result": None},
    {"jsonrpc": "2.0", "id": 1, "result": "latest"},
    {"jsonrpc": "2.0", "id": 1, "result": "0x"},
    "ok",
])
def test_only_a_real_block_number_heals(answer):
    paced = lane.PacedHttp(min_gap_s=0.0)
    paced.note_answer(*QUOTA[:2])
    paced.quota_probe_result(200, answer, gen=paced.quota_gen)
    assert paced.quota_spent()


def test_the_warmer_itself_never_sends_while_spent(monkeypatch):
    monkeypatch.setattr(lane.lane_cfg, "MEMECOIN_RH_FAST_CACHED_ONLY", True)
    clock = Clock()
    paced = lane.PacedHttp(min_gap_s=0.0, clock=clock)
    paced.note_answer(*QUOTA[:2])

    class Cache:
        def next_due(self, now):
            raise AssertionError("the warmer asked for work while the host is quota-spent")
    warmer = lane.InlineWarmer.__new__(lane.InlineWarmer)
    warmer.cache, warmer.host, warmer.counts = Cache(), paced, __import__("collections").Counter()
    warmer.paused_until, warmer.clock = 0.0, clock
    assert warmer.step(1.0) == 1.0


def test_the_heartbeat_carries_the_quota_state(tmp_path):
    import collections
    import sqlite3
    paced = lane.PacedHttp(min_gap_s=0.0)
    paced.note_answer(*QUOTA[:2])

    class D:
        counts = collections.Counter()

    class C:
        counts = collections.Counter()

        def size(self):
            return 0
    db = str(tmp_path / "l.db")
    lane.write_heartbeat(db, D(), C(), lane.PacedHttp(min_gap_s=0.0), inline=paced)
    assert sqlite3.connect(db).execute("select inline_quota from rh_fast_lane_heartbeat").fetchone() == (1,)


def test_a_probe_from_an_older_latch_cannot_clear_a_newer_one():
    clock = Clock()
    paced = lane.PacedHttp(min_gap_s=0.0, clock=clock)
    paced.note_answer(*QUOTA[:2])
    old = paced.quota_gen
    paced.quota_probe_result(*OK[:2], gen=old)        # cleared by its own probe
    paced.note_answer(*QUOTA[:2])                     # a new latch
    paced.quota_probe_result(*OK[:2], gen=old)        # the old probe answering late
    assert paced.quota_spent()
