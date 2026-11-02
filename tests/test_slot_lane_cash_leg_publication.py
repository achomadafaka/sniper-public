"""Cash legs publish as each size completes (Codex speed audit 706f4245f, item 2).

refresh_cash_legs fetched full, alternate and wallet-cap sizes serially and published only after
the whole loop, so a fresh full-size leg stayed invisible while a slower secondary size waited or
timed out. Pins: a completed size is visible at once; other current-plan sizes keep their original
stamps; sizes outside the plan are pruned; the primary compatibility field follows the primary;
the requests are exactly the ones the serial loop always made."""
import threading
from types import SimpleNamespace

from sniper.execution import pump_curve_buy as P


def _builder(*, legs=(), leg=None, now=100.0):
    return SimpleNamespace(_cash_legs=tuple(legs), _leg=leg, clock=lambda: now, max_leg_age_s=3.0,
                           refresh_after_s=1.5, leg_slippage_bps=150, user="fake", jup_call=None,
                           rpc=None, _alt_cache={})


def test_a_fresh_primary_is_visible_while_a_slower_secondary_still_waits():
    """The Codex probe, as a test: red before progressive publication."""
    secondary, release, failures, fresh = threading.Event(), threading.Event(), [], []
    b = _builder()

    def fetch(atoms, *args, **kwargs):
        if atoms == 10_000_000:
            leg = {"usdc_atomic": atoms, "fetched_at": 100.0}
            fresh.append(leg)
            return leg
        secondary.set()
        assert release.wait(2)
        return None

    def worker():
        try:
            P.refresh_cash_legs(b, 10.0, 5.0, prefetch_fn=fetch)
        except BaseException as exc:          # noqa: BLE001 - reported by the assert below
            failures.append(exc)
    t = threading.Thread(target=worker)
    t.start()
    try:
        assert secondary.wait(2)
        assert P.matching_cash_leg(b, 10.0) is fresh[0] and b._leg is fresh[0]
    finally:
        release.set()
        t.join(2)
    assert not failures and P.matching_cash_leg(b, 10.0) is fresh[0]


def test_intermediate_publication_keeps_other_plan_sizes_with_their_stamps_and_prunes_the_rest():
    alt_old = {"usdc_atomic": 5_000_000, "fetched_at": 99.9}            # fresh enough: not re-fetched
    stray = {"usdc_atomic": 7_000_000, "fetched_at": 99.9}              # no longer in the plan
    b = _builder(legs=(alt_old, stray))
    seen, calls = [], []

    def fetch(atoms, *args, **kwargs):
        calls.append(atoms)
        if atoms == 10_000_000:
            return {"usdc_atomic": atoms, "fetched_at": 100.0}
        seen.append(tuple(b._cash_legs))                                 # what readers see meanwhile
        return {"usdc_atomic": atoms, "fetched_at": 100.0}
    P.refresh_cash_legs(b, 10.0, 5.0, prefetch_fn=fetch, extra_clip_usds=(3.0,))
    assert calls == [10_000_000, 3_000_000]                              # the old loop's requests
    during = seen[0]
    assert [l["usdc_atomic"] for l in during] == [10_000_000, 5_000_000]
    assert during[1] is alt_old and during[1]["fetched_at"] == 99.9      # original stamp kept
    assert stray not in b._cash_legs
    assert [l["usdc_atomic"] for l in b._cash_legs] == [10_000_000, 5_000_000, 3_000_000]


def test_a_failed_primary_keeps_the_old_primary_and_its_stamp():
    old = {"usdc_atomic": 10_000_000, "fetched_at": 96.0}
    b = _builder(leg=old)
    P.refresh_cash_legs(b, 10.0, prefetch_fn=lambda *a, **k: None)
    assert b._leg is old and b._cash_legs == (old,) and P.matching_cash_leg(b, 10.0) is None   # stale stays stale


def test_the_snapshot_shows_a_fresh_primary_beside_an_older_secondary():
    primary = {"usdc_atomic": 10_000_000, "fetched_at": 99.8}
    secondary = {"usdc_atomic": 5_000_000, "fetched_at": 97.5}
    b = _builder(legs=(primary, secondary), leg=primary, now=100.0)
    snap = P.cash_leg_snapshot(b)
    assert snap == {"10000000": {"age_s": 0.2, "fetched_at": 99.8, "primary": True},
                    "5000000": {"age_s": 2.5, "fetched_at": 97.5, "primary": False}}
    assert P.cash_leg_snapshot(_builder()) == {}


def test_the_heartbeat_exports_slot_lane_cash_legs(tmp_path):
    import json
    from sniper.services.memecoin_runner import write_heartbeat
    primary = {"usdc_atomic": 10_000_000, "fetched_at": 99.8}
    b = _builder(legs=(primary,), leg=primary, now=100.0)
    path = tmp_path / "hb"
    write_heartbeat({}, auth_ok=True, path=path, slot_lane_status={
        "mode": "live", "cash_legs": lambda: {"pump_curve": P.cash_leg_snapshot(b)}})
    out = json.loads(path.read_text())["slot_lane"]["cash_legs"]
    assert out == {"pump_curve": {"10000000": {"age_s": 0.2, "fetched_at": 99.8, "primary": True}}}


def test_a_sample_between_the_two_assignments_shows_the_fresh_published_primary():
    """Gamma info on 7d3727dc9: publish() sets _cash_legs, then _leg. Sampled in between, the new
    tuple's fresh primary must win over the old _leg's stale stamp."""
    old = {"usdc_atomic": 10_000_000, "fetched_at": 96.0}
    new = {"usdc_atomic": 10_000_000, "fetched_at": 99.9}
    b = _builder(legs=(new,), leg=old, now=100.0)                    # the two references out of step
    assert P.cash_leg_snapshot(b) == {"10000000": {"age_s": 0.1, "fetched_at": 99.9, "primary": True}}
