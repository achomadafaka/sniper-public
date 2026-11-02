"""Batch runner — acceptance tests (fakes only, no network/DB).

Covers the §8 list: opened-not-resolved eligibility (no profit gate), as_of cap, dry-run
non-persistence, persist, rerun skip/retry, backtest enrolled_at, forward-tracking advance,
counts, unique-wallet promotion, activity-tier readout, and the Null Calibration Gate.
"""
from __future__ import annotations

from sniper.analysis.thesis_cards.becker_feed import Candidate
from sniper.analysis.thesis_cards.enrollment import InMemoryPaperEnrollmentStore, PaperEnrollment
from sniper.analysis.thesis_cards.batch_runner import (
    BatchCounts, InMemoryCheckpoint, map_enrollment_positions, null_calibration_rate,
    replication_check, run_batch,
)
from sniper.analysis.thesis_cards.forward_window_provider import _row_is_negrisk

AS_OF = 1000


def _pos(avg, won, ts, size, cid=None):
    cur = 1.0 if won else 0.0
    rpnl = (size / avg) * (cur - avg)                 # round-trips to `size` via ADR-004 mapping
    return {"avgPrice": avg, "curPrice": cur, "timestamp": ts, "realizedPnl": rpnl,
            "conditionId": cid or f"0x{ts}"}


def _conviction(base_ts, n_win=17, n_los=8, big=1000, small=50, avg=0.45):
    rows = [_pos(avg, 1, base_ts + i, big) for i in range(n_win)]
    rows += [_pos(avg, 0, base_ts + 500 + i, small) for i in range(n_los)]
    return rows


def _wallet_positions():
    # pre-as_of in-sample (supported sizing card) + 2 non-overlapping forward batches
    return _conviction(0, 24, 16) + _conviction(1100) + _conviction(2100)


def _fetch(_wallet):
    return _wallet_positions()


CANDS = [Candidate(wallet="0xwin", opened=200), Candidate(wallet="0xfloor", opened=35)]
SCHEDULE = [2000, 3000]                                # two slices -> two-pass promotion


class RecordingStore:
    def __init__(self):
        self.upserts = []
        self.inner = InMemoryPaperEnrollmentStore()
    def upsert(self, e):
        self.upserts.append(e)
        return self.inner.upsert(e)
    def iter_active(self):
        return self.inner.iter_active()
    def update_tracking(self, **kw):
        return self.inner.update_tracking(**kw)


# --- eligibility / mapping ---

def test_eligibility_is_opened_not_resolved_no_profit_gate():
    # a LOSING wallet (mostly losers) is still processed — no profit filter
    losers = [_pos(0.45, 0, i, 100) for i in range(40)]
    counts = run_batch([Candidate("0xloser", opened=40)], fetch_positions=lambda w: losers,
                       as_of=AS_OF)
    assert counts.eligible == 1 and counts.profiles_built == 1   # not filtered out for losing


def test_card_input_capped_at_as_of():
    raw = [_pos(0.45, 1, 500, 100), _pos(0.45, 1, 1500, 100)]   # one pre, one post as_of
    mapped = map_enrollment_positions(raw, AS_OF, _row_is_negrisk)
    assert len(mapped) == 1                                       # the 1500 (post-as_of) excluded


# --- persistence modes ---

def test_dry_run_does_not_persist():
    counts = run_batch(CANDS, fetch_positions=_fetch, as_of=AS_OF, store=None)
    assert counts.enrollments_queued > 0                         # built + counted, but no store


def test_persist_writes_enrollments():
    store = RecordingStore()
    run_batch(CANDS, fetch_positions=_fetch, as_of=AS_OF, store=store)
    assert store.upserts and all(isinstance(e, PaperEnrollment) for e in store.upserts)


def test_backtest_sets_enrolled_at_to_as_of():
    store = RecordingStore()
    run_batch(CANDS, fetch_positions=_fetch, as_of=AS_OF, store=store)
    assert all(e.enrolled_at == AS_OF for e in store.upserts)


def test_no_writes_outside_paper_card_enrollments():
    # structural: the store only ever receives PaperEnrollment upserts
    store = RecordingStore()
    run_batch(CANDS, fetch_positions=_fetch, as_of=AS_OF, store=store)
    assert {type(e).__name__ for e in store.upserts} == {"PaperEnrollment"}


# --- checkpoint / bounds ---

def test_rerun_skips_done_retries_failed():
    cp = InMemoryCheckpoint()
    cp.record("0xwin", "done", {})
    cp.record("0xfloor", "failed", {})
    counts = run_batch(CANDS, fetch_positions=_fetch, as_of=AS_OF, checkpoint=cp)
    assert counts.wallets_scanned == 1                           # 0xwin skipped, 0xfloor retried


def test_max_wallets_bounds_scan():
    counts = run_batch(CANDS, fetch_positions=_fetch, as_of=AS_OF, max_wallets=1)
    assert counts.wallets_scanned == 1


# --- forward tracking / promotion / tiers ---

def test_forward_tracking_promotes_and_counts_unique_and_tier():
    store = RecordingStore()
    counts = run_batch(CANDS, fetch_positions=_fetch, as_of=AS_OF, store=store,
                       run_forward=True, now_schedule=SCHEDULE, min_opened=30)
    assert "0xwin" in counts.unique_wallets_promoted                   # two-pass promotion
    assert counts.by_activity_tier["above_2x"]["promoted"] >= 1        # 0xwin opened=200 > 60
    assert counts.d4_advanced.get("promoted", 0) == len(counts.unique_wallets_promoted)


def test_counts_output_is_accurate():
    counts = run_batch(CANDS, fetch_positions=_fetch, as_of=AS_OF)
    d = counts.as_dict()
    assert d["wallets_scanned"] == 2 and d["eligible"] == 2
    assert isinstance(d["unique_wallets_promoted"], int)


# --- Null Calibration Gate ---

def test_null_calibration_rate_is_low_for_shuffled_skill():
    # the same conviction wallet promotes unshuffled, but rarely under outcome-shuffle (null)
    res = null_calibration_rate(CANDS, fetch_positions=_fetch, as_of=AS_OF,
                                now_schedule=SCHEDULE, seed=1)
    assert set(res) == {"null_promotion_rate", "promoted", "eligible"}
    assert res["eligible"] == 2
    assert res["null_promotion_rate"] <= 0.5                           # shuffle destroys the size↔win link


def test_negrisk_lookup_cleans_in_sample_card():
    # a conviction wallet enrolls (sizing admitted) — but if the lookup flags ALL its
    # conditions as negRisk, every in-sample position is dropped -> no enrollable card.
    raw = _conviction(0, 24, 16)                       # distinct cids "0x0".."0x515"
    without = run_batch([Candidate("0xw", opened=99)], fetch_positions=lambda w: raw,
                        as_of=AS_OF, negrisk_lookup=None)
    cleaned = run_batch([Candidate("0xw", opened=99)], fetch_positions=lambda w: raw,
                        as_of=AS_OF, negrisk_lookup=lambda cids: set(cids))   # all negRisk
    assert without.cards_admitted == 1                 # sizing admitted normally
    assert cleaned.cards_admitted == 0                 # all positions excluded -> nothing enrollable


def _noise_positions():
    # size uncorrelated with outcome -> no sizing edge; in-sample + 2 forward batches
    pre = [_pos(0.5, i % 2, i, 100 + (i % 5) * 10) for i in range(40)]
    b1 = [_pos(0.5, i % 2, 1100 + i, 100 + (i % 5) * 10) for i in range(25)]
    b2 = [_pos(0.5, i % 2, 2100 + i, 100 + (i % 5) * 10) for i in range(25)]
    return pre + b1 + b2


def test_replication_check_replicates_for_skilled_population():
    cands = [Candidate(f"0xskill{i}", 99) for i in range(6)]
    out = replication_check(cands, fetch_positions=lambda w: _wallet_positions(),
                            as_of=AS_OF, now_schedule=SCHEDULE, seed=1)
    assert out["replicates"] is True
    assert out["a"]["beats_null"] and out["b"]["beats_null"]
    assert out["a"]["null_rate"] == 0.0 and out["b"]["null_rate"] == 0.0


def test_replication_check_fails_for_noise_population():
    cands = [Candidate(f"0xnoise{i}", 99) for i in range(6)]
    out = replication_check(cands, fetch_positions=lambda w: _noise_positions(),
                            as_of=AS_OF, now_schedule=SCHEDULE, seed=1)
    assert out["replicates"] is False
    assert out["a"]["real_rate"] == 0.0 and out["b"]["real_rate"] == 0.0
