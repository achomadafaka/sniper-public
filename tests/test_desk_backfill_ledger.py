"""Helius backfill credit ledger (spec 2026-10-27-helius-history-backfill-design.md section 2 and 7). Offline only: no provider call, no network."""
import json
from datetime import datetime, timezone

import pytest

from sniper.services import memecoin_desk_backfill_ledger as L


def ts(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=timezone.utc).timestamp()


NOW = ts(2026, 10, 28, 3)
COSTS = {"getTransactionsForAddress": 50, "getTransaction": 10}


def cycle(**k):
    c = dict(attested_at="2026-10-27T12:00:00Z", attested_by="op", epoch_id="2026-10-27", epoch_start="2026-10-27T00:00:00Z", epoch_end="2026-11-28T00:00:00Z",
             plan_credits=10_000_000, used_at_activation=500_000, month_cap=5_000_000, funded_reserve=2_000_000)
    c.update(k)
    return c


def write(tmp_path, **k):
    p = tmp_path / "cycle.json"
    p.write_text(json.dumps(cycle(**k)))
    return p


def ledger(tmp_path, now=NOW, cap_override=None, costs=COSTS, **k):
    c = L.load_cycle(write(tmp_path, **k), now=now)
    if cap_override is not None:
        c["month_cap"] = cap_override
    return L.Ledger(tmp_path / "credits.db", c, clock=lambda: now, costs=costs)


@pytest.mark.parametrize("k,code", [(dict(used_at_activation=None), "cycle_invalid"), (dict(epoch_end="2026-10-28T01:00:00Z"), "cycle_expired"),
                                    (dict(epoch_start="2026-10-29T00:00:00Z"), "cycle_not_started"), (dict(attested_at="2026-10-30T00:00:00Z"), "attested_at_future"),
                                    (dict(attested_at="2026-10-26T00:00:00Z"), "attested_at_outside_epoch"), (dict(month_cap=float("nan")), "cycle_invalid"),
                                    (dict(plan_credits=10_000_000, used_at_activation=3_000_000), "cycle_unsized"), (dict(funded_reserve=0), "cycle_invalid"),
                                    (dict(month_cap=-1), "cycle_invalid")])
def test_a_bad_cycle_file_is_refused_with_a_typed_code(tmp_path, k, code):
    p = tmp_path / "cycle.json"
    p.write_text(json.dumps(cycle(**k), allow_nan=True))
    with pytest.raises(L.Refusal) as e:
        L.load_cycle(p, now=NOW)
    assert e.value.code == code


def test_a_missing_cycle_file_is_a_refusal(tmp_path):
    with pytest.raises(L.Refusal) as e:
        L.load_cycle(tmp_path / "nope.json", now=NOW)
    assert e.value.code == "no_cycle_attestation"


def test_unknown_method_cost_is_refused_never_guessed(tmp_path):
    lg = ledger(tmp_path)
    with pytest.raises(L.Refusal) as e:
        lg.reserve("getSomethingNew")
    assert e.value.code == "unknown_method_cost"
    with pytest.raises(L.Refusal):
        ledger(tmp_path / "x" if False else tmp_path, costs={}).reserve("getTransaction")          # the default empty table refuses everything


def test_cap_at_the_exact_boundary(tmp_path):
    lg = ledger(tmp_path, cap_override=100)
    lg.reserve("getTransactionsForAddress")
    lg.reserve("getTransactionsForAddress")                           # 100 of 100: allowed
    with pytest.raises(L.Refusal) as e:
        lg.reserve("getTransaction")                                  # 110 > 100
    assert e.value.code == "month_cap"
    assert lg.total() == 100


def test_crash_after_reserve_counts_and_a_restart_does_not_refund(tmp_path):
    lg = ledger(tmp_path, cap_override=100)
    lg.reserve("getTransactionsForAddress")                           # no commit: the process dies here
    del lg
    lg2 = ledger(tmp_path, cap_override=100)
    assert lg2.total() == 50
    lg2.reserve("getTransactionsForAddress")
    with pytest.raises(L.Refusal):
        lg2.reserve("getTransaction")


def test_lost_response_is_ambiguous_and_counted_in_full(tmp_path):
    lg = ledger(tmp_path)
    a = lg.reserve("getTransactionsForAddress")
    lg.mark_ambiguous(a)
    assert lg.total() == 50 and lg.counts() == {"ambiguous": 1}
    b = lg.reserve("getTransaction")
    lg.commit(b)
    assert lg.total() == 60 and lg.counts() == {"ambiguous": 1, "committed": 1}


def test_a_clean_commit_is_charged_at_its_actual_cost_not_the_reservation(tmp_path):
    lg = ledger(tmp_path)
    a = lg.reserve("getTransactionsForAddress")
    lg.commit(a, actual=10)                                           # the clean response reports its real (lower) cost
    assert lg.total() == 10
    b = lg.reserve("getTransactionsForAddress")
    lg.commit(b)                                                      # no actual reported: the documented reservation
    assert lg.total() == 60


def test_quota_stop_survives_a_restart_for_the_whole_utc_night_only(tmp_path):
    lg = ledger(tmp_path)
    lg.record_stop("max_usage")
    del lg
    with pytest.raises(L.Refusal) as e:
        ledger(tmp_path).reserve("getTransaction")                    # cron re-run the same night
    assert e.value.code == "quota_stop"
    later = ts(2026, 10, 29, 1)                                       # next UTC night
    ledger(tmp_path, now=later).reserve("getTransaction")


def test_billing_rollover_needs_a_new_cycle_and_does_not_leak_usage(tmp_path):
    lg = ledger(tmp_path)
    lg.reserve("getTransactionsForAddress")
    after = ts(2026, 11, 28, 1)
    with pytest.raises(L.Refusal) as e:
        L.load_cycle(write(tmp_path), now=after)                      # the old file is expired
    assert e.value.code == "cycle_expired"
    new = write(tmp_path, epoch_id="2026-11-28", epoch_start="2026-11-28T00:00:00Z", epoch_end="2026-12-28T00:00:00Z", attested_at="2026-11-28T00:30:00Z", used_at_activation=0)
    lg2 = L.Ledger(tmp_path / "credits.db", L.load_cycle(new, now=after), clock=lambda: after, costs=COSTS)
    assert lg2.total() == 0
    lg2.reserve("getTransaction")


def test_the_same_epoch_id_with_changed_identity_is_refused_but_a_changed_cap_does_not_reset(tmp_path):
    lg = ledger(tmp_path)
    lg.reserve("getTransactionsForAddress")
    with pytest.raises(L.Refusal) as e:
        ledger(tmp_path, plan_credits=20_000_000)
    assert e.value.code == "epoch_identity_changed"
    with pytest.raises(L.Refusal):
        ledger(tmp_path, epoch_end="2026-12-31T00:00:00Z")
    lg2 = ledger(tmp_path, month_cap=4_000_000, funded_reserve=1_000_000)
    assert lg2.total() == 50                                           # changing the cap or the reserve never resets usage


def test_budget_is_month_cap_minus_our_ledger_total_and_run_cap_binds_first(tmp_path):
    lg = ledger(tmp_path, cap_override=1000)
    assert lg.budget(run_cap=100) == 100
    lg.reserve("getTransactionsForAddress")
    assert lg.budget(run_cap=100) == 100 and lg.budget(run_cap=10_000) == 950


def test_run_cap_is_enforced_per_run(tmp_path):
    lg = ledger(tmp_path)
    lg.start_run(run_cap=100)
    lg.reserve("getTransactionsForAddress")
    lg.reserve("getTransactionsForAddress")
    with pytest.raises(L.Refusal) as e:
        lg.reserve("getTransaction")
    assert e.value.code == "run_cap"


@pytest.mark.parametrize("status,body,kind", [
    (429, b'{"error":"max usage reached"}', "quota"), (429, b"Too Many Requests", "rate"), (402, b"Payment Required: Max Usage", "quota"),
    (403, b'{"message":"max usage reached"}', "quota"), (200, json.dumps({"jsonrpc": "2.0", "error": {"code": -32429, "message": "max usage reached"}}).encode(), "quota"),
    (200, json.dumps({"jsonrpc": "2.0", "error": {"code": -32602, "message": "bad params"}}).encode(), "rpc_error"),
    (200, json.dumps({"jsonrpc": "2.0", "result": {"data": []}}).encode(), "ok"), (500, b"boom", "http_error"), (200, b"<html>not json", "malformed")])
def test_response_classification(status, body, kind):
    assert L.classify(status, body) == kind


def test_rate_retries_are_bounded_and_each_one_is_charged(tmp_path):
    lg = ledger(tmp_path, cap_override=10_000)
    n = 0
    for _ in range(10):
        if not lg.rate_retry_allowed(page="p1"):
            break
        lg.reserve("getTransactionsForAddress", page="p1")
        n += 1
    assert n == 4 and lg.total() == 200                              # the first attempt plus 3 retries


def test_the_default_billing_clock_is_corrected_utc_not_the_host_clock(tmp_path, monkeypatch):
    import time
    from sniper.util import clock as C
    offset_file = tmp_path / "offset.json"
    offset_file.write_text(json.dumps({"offset_s": 3 * 3600.0, "at": time.time()}))             # a host running 3 h fast
    monkeypatch.setattr(C, "_OFFSET_FILE", str(offset_file))
    assert abs(L.real_utc_now() - (time.time() - 3 * 3600.0)) < 2.0
    c = L.load_cycle(write(tmp_path, epoch_start=datetime.fromtimestamp(time.time() - 3 * 3600.0 - 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           epoch_end=datetime.fromtimestamp(time.time() + 86400 * 20, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           attested_at=datetime.fromtimestamp(time.time() - 3 * 3600.0 - 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))        # attested before corrected now, after the epoch start
    lg = L.Ledger(tmp_path / "credits.db", c, costs=COSTS)                                        # default clock
    night = datetime.fromtimestamp(time.time() - 3 * 3600.0, timezone.utc).date().isoformat()
    lg.record_stop("max_usage")
    assert lg.con.execute("select utc_night from stops").fetchone()[0] == night


def test_a_renamed_or_overlapping_cycle_cannot_open_a_fresh_zero_spend_bucket(tmp_path):
    lg = ledger(tmp_path, cap_override=100)
    lg.reserve("getTransactionsForAddress")
    lg.reserve("getTransactionsForAddress")                           # 100 of 100 under 2026-10-27
    with pytest.raises(L.Refusal) as e:
        ledger(tmp_path, epoch_id="renamed")                          # identical provider dates under another id
    assert e.value.code == "epoch_overlap"
    partial = L.load_cycle(write(tmp_path, epoch_id="partial", epoch_start="2026-10-28T00:00:00Z", epoch_end="2026-12-20T00:00:00Z", attested_at="2026-10-28T01:00:00Z"), now=NOW)
    with pytest.raises(L.Refusal):
        L.Ledger(tmp_path / "credits.db", partial, clock=lambda: NOW, costs=COSTS)       # partial overlap with the first epoch
    nxt = L.load_cycle(write(tmp_path, epoch_id="next", epoch_start="2026-11-28T00:00:00Z", epoch_end="2026-12-28T00:00:00Z", attested_at="2026-11-28T00:10:00Z",
                             used_at_activation=0), now=ts(2026, 11, 28, 1))
    assert L.Ledger(tmp_path / "credits.db", nxt, clock=lambda: ts(2026, 11, 28, 1), costs=COSTS).total() == 0         # the real next cycle (starts at the old end) stays valid


@pytest.mark.parametrize("bad", [-100, 0, 1.5, True, "50", None])
def test_a_bad_cost_table_entry_is_refused_at_reserve(tmp_path, bad):
    lg = ledger(tmp_path, costs={"m": bad})
    with pytest.raises(L.Refusal) as e:
        lg.reserve("m")
    assert e.value.code in ("credits_invalid", "unknown_method_cost") and lg.total() == 0


@pytest.mark.parametrize("bad", [-100, 1.5, True, "5"])
def test_commit_refuses_a_negative_float_or_bool_actual_and_changes_nothing(tmp_path, bad):
    lg = ledger(tmp_path)
    a = lg.reserve("getTransactionsForAddress")
    with pytest.raises(L.Refusal):
        lg.commit(a, actual=bad)
    assert lg.total() == 50 and lg.counts() == {}                      # still open and still charged in full


def test_a_commit_is_terminal_exact_and_epoch_bound(tmp_path):
    lg = ledger(tmp_path)
    a = lg.reserve("getTransactionsForAddress")
    lg.commit(a)
    with pytest.raises(L.Refusal) as e:
        lg.commit(a, actual=0)                                          # a second commit cannot refund
    assert e.value.code == "attempt_not_open" and lg.total() == 50
    b = lg.reserve("getTransaction")
    lg.mark_ambiguous(b)
    with pytest.raises(L.Refusal):
        lg.commit(b, actual=0)                                          # a late response after ambiguous cannot refund either
    with pytest.raises(L.Refusal):
        lg.mark_ambiguous(a)                                            # nor can a committed row go back
    with pytest.raises(L.Refusal):
        lg.commit(9999)                                                 # unknown attempt
    assert lg.total() == 60


def test_an_actual_above_the_reservation_is_charged_and_stops_further_attempts(tmp_path):
    """Delta: start_run(100), reserve 50, commit actual 500 left run_spent at 50 and a 10-credit reserve still succeeded."""
    lg = ledger(tmp_path)
    lg.start_run(run_cap=100)
    a = lg.reserve("getTransactionsForAddress")
    lg.commit(a, actual=500)
    assert lg.total() == 500 and lg.run_spent == 500                    # the paid amount is kept, never hidden
    with pytest.raises(L.Refusal) as e:
        lg.reserve("getTransaction")
    assert e.value.code in ("cost_bound_exceeded", "quota_stop")
    with pytest.raises(L.Refusal):
        ledger(tmp_path).reserve("getTransaction")                      # persisted: a cron re-run the same UTC night refuses too
    ledger(tmp_path, now=ts(2026, 10, 29, 1)).reserve("getTransaction")  # next night is open again (the operator fixes the cost table meanwhile)


def test_an_actual_below_the_reservation_returns_budget_to_the_run_but_ambiguous_never_does(tmp_path):
    lg = ledger(tmp_path)
    lg.start_run(run_cap=100)
    a = lg.reserve("getTransactionsForAddress")
    lg.commit(a, actual=10)
    assert lg.run_spent == 10
    b = lg.reserve("getTransactionsForAddress")
    lg.mark_ambiguous(b)
    assert lg.run_spent == 60 and lg.total() == 60


def test_a_crash_while_writing_the_over_bound_stop_leaves_neither_half(tmp_path, monkeypatch):
    """Delta: settlement autocommitted actual 500, then the stop insert crashed; a restart admitted the next reserve. Now settlement + stop are one transaction."""
    lg = ledger(tmp_path)
    lg.start_run(100)
    a = lg.reserve("getTransactionsForAddress")
    monkeypatch.setattr(lg, "record_stop", lambda *args: (_ for _ in ()).throw(OSError("crash before stop write")))
    with pytest.raises(OSError):
        lg.commit(a, actual=500)
    restarted = ledger(tmp_path)
    if restarted.total() == 500:                                        # if the paid amount survived, the stop must have too
        with pytest.raises(L.Refusal):
            restarted.reserve("getTransaction")
    else:
        assert restarted.total() == 50 and restarted.counts() == {}     # rolled back: still the open reservation, charged in full
        restarted.mark_ambiguous(a)                                      # and the runner treats an unknown outcome as ambiguous
        assert restarted.total() == 50
