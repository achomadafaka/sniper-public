import pytest

from sniper.research.memecoin_opportunity.bags_coverage import (
    BagsForwardCoverage,
    parse_bags_launch_feed,
    reconcile_bags_coverage,
)


def test_parse_documented_bags_launch_feed_shape():
    rows = parse_bags_launch_feed(
        {
            "success": True,
            "response": [
                {"tokenMint": "MintA", "launchSignature": "SigA", "dbcPoolKey": "PoolA"},
                {"tokenMint": "MintB", "launchSignature": "SigB", "dbcPoolKey": "PoolB"},
            ],
        }
    )
    assert [row.token_mint for row in rows] == ["MintA", "MintB"]
    with pytest.raises(ValueError, match="BAGS_FEED_FAILED"):
        parse_bags_launch_feed({"success": False, "error": "nope"})


def test_coverage_distinguishes_verified_gap_quiet_and_unmeasured():
    feed = parse_bags_launch_feed(
        {"success": True, "response": [{"tokenMint": "MintA", "launchSignature": "SigA"}]}
    )
    verified = reconcile_bags_coverage(feed, {"MintA"}, configured=True)
    assert verified["coverage_status"] == "verified"
    assert verified["missing_mints"] == []

    gap = reconcile_bags_coverage(feed, set(), configured=True)
    assert gap["coverage_status"] == "coverage_gap"
    assert gap["missing_mints"] == ["MintA"]

    quiet = reconcile_bags_coverage((), set(), configured=True)
    assert quiet["coverage_status"] == "quiet"

    unmeasured = reconcile_bags_coverage((), set(), configured=False)
    assert unmeasured["coverage_status"] == "unmeasured"
    assert unmeasured["feed_launches"] is None


def test_forward_coverage_baselines_existing_feed_and_audits_only_new_launches():
    audit = BagsForwardCoverage()
    old = parse_bags_launch_feed(
        {"success": True, "response": [
            {"tokenMint": "Old", "launchSignature": "OldSig", "dbcPoolKey": "OldPool"},
        ]}
    )
    audited, detail = audit.observe(old, set())
    assert audited == ()
    assert detail["coverage_status"] == "warming"
    assert detail["missing_mints"] == []

    current = parse_bags_launch_feed(
        {"success": True, "response": [
            {"tokenMint": "New", "launchSignature": "NewSig", "dbcPoolKey": "NewPool"},
            {"tokenMint": "Old", "launchSignature": "OldSig", "dbcPoolKey": "OldPool"},
        ]}
    )
    audited, detail = audit.observe(current, set())
    assert [launch.token_mint for launch in audited] == ["New"]
    assert detail["coverage_status"] == "coverage_gap"
    assert detail["missing_mints"] == ["New"]

    _, detail = audit.observe(current, {"New"})
    assert detail["coverage_status"] == "forward_sample_verified"
    assert detail["audited_launches"] == 1
    assert detail["feed_launches"] == 2
    assert detail["captured_feed_launches"] == 1

    # Hot retention can remove the launch after capture without reopening a gap.
    _, detail = audit.observe(current, set())
    assert detail["coverage_status"] == "forward_sample_verified"


def test_historical_recovery_status_is_not_current_health(tmp_path):
    """A stored Bags recovery row stays available but is not current health."""
    import asyncio

    from scripts.memecoin_opportunity_worker import _bags_coverage_loop
    from sniper.research.memecoin_opportunity.projection import read_radar_snapshot
    from sniper.research.memecoin_opportunity.store import connect
    from sniper.util.clock import real_utc_now_iso

    conn = connect(tmp_path / "op.db")

    class OneShotStop:
        def __init__(self):
            self.calls = 0

        def is_set(self):
            self.calls += 1
            return self.calls > 1

        async def wait(self):
            return True

    # No API key: the loop takes its unconfigured branch and still must report liveness.
    asyncio.run(
        _bags_coverage_loop(conn, api_key="", interval_s=0.01, stop=OneShotStop())
    )

    row = conn.execute(
        "SELECT status,detail_json FROM op_runtime_status "
        "WHERE source_key='opportunity:bags_feed_recovery'"
    ).fetchone()
    assert row is not None, "idle recovery lane must still publish a heartbeat"
    assert row[0] == "healthy"

    snapshot = read_radar_snapshot(conn, now_iso=real_utc_now_iso())
    health = {item["source_key"]: item for item in snapshot["source_health"]}
    assert "opportunity:bags_feed_recovery" not in health


def test_recovery_bound_follows_the_coverage_poll_it_is_driven_by():
    """A fixed bound recreates the false stale it was added to remove.

    The lane heartbeats once per coverage pass, and
    MEMECOIN_OPPORTUNITY_BAGS_COVERAGE_INTERVAL_S is read straight from the environment
    with no validation, so any operator value above the bound would make an entirely
    healthy lane grade degraded forever.
    """
    from scripts.memecoin_opportunity_worker import _bags_recovery_stale_after_s

    # Default 5s poll keeps the socket-matching floor.
    assert _bags_recovery_stale_after_s(5) == 60

    # A slow poll must widen the bound past its own interval, not trip on it.
    assert _bags_recovery_stale_after_s(90) == 90 * 12
    assert _bags_recovery_stale_after_s(90) > 90

    # Garbage never yields a bound below the floor (which would grade a live lane dead).
    for bad in (0, -1, None, "x", float("nan"), float("inf")):
        assert _bags_recovery_stale_after_s(bad) == 60
