from types import SimpleNamespace

from scripts import memecoin_opportunity_worker as worker
from sniper.research.memecoin_opportunity.engine import (
    OpportunityEngine,
    _paper_cohort_eligible,
)
from sniper.research.memecoin_opportunity.frenzy import FrenzyCeiling
from sniper.research.memecoin_opportunity.state import PUMP_SOL_QUOTE_MINT
from sniper.research.memecoin_opportunity.store import connect
from sniper.research.memecoin_opportunity.types import VenueId


def test_the_engine_owns_one_frenzy_ceiling(tmp_path):
    engine = OpportunityEngine(
        connect(tmp_path / "radar.db"),
        quote_usd_price_micros={},
        quote_decimals={},
        fee_bps={},
    )

    assert isinstance(engine.frenzy_ceiling, FrenzyCeiling)


def test_an_adaptive_alert_can_join_the_paper_cohort():
    assert _paper_cohort_eligible(
        "pump-adaptive-v3",
        {},
        SimpleNamespace(decision_age_ms=20_000),
    ) is True


def test_adaptive_alert_features_keep_the_quote_and_lp_facts(tmp_path):
    venue = VenueId.PUMPFUN_SOLANA
    engine = OpportunityEngine(
        connect(tmp_path / "radar.db"),
        quote_usd_price_micros={venue: 150_000_000},
        quote_decimals={venue: 9},
        fee_bps={venue: 100},
    )
    state = SimpleNamespace(
        venue_id=venue,
        quote_mint=PUMP_SOL_QUOTE_MINT,
        mayhem_mode=False,
        cashback_enabled=False,
        effective_fee_bps=None,
    )
    result = SimpleNamespace(
        round_trip_return_bps=-400,
        latency_ms=25,
        completed_at="2026-08-11T00:00:20Z",
    )

    features = engine._alert_features(
        state,
        {
            "frenzy_ceiling_atomic_per_s2": 900,
            "liquidity_removed_fraction_bps": 250,
        },
        result,
        quote_age_ms=125,
    )

    assert features["frenzy_ceiling_atomic_per_s2"] == 900
    assert features["liquidity_removed_fraction_bps"] == 250
    assert features["quote_age_ms"] == 125


def test_default_worker_start_activates_only_v2(tmp_path):
    conn = connect(tmp_path / "radar.db")

    worker._activate_paper_lifecycles(
        conn,
        adaptive_v3_enabled=False,
        now_iso="2026-08-11T00:00:00Z",
    )

    versions = [
        row[0]
        for row in conn.execute(
            "SELECT lifecycle_version FROM op_paper_lifecycle_activations "
            "ORDER BY lifecycle_version"
        )
    ]
    # The preregistered integer SHIPS while the cohort stays CLOSED. Opening it needs a
    # separate operator action (`MEMECOIN_RADAR_REALISTIC_V3_ACTIVATE=1`), so a default
    # worker start must not create the realistic-v3 row.
    assert versions == ["radar-realistic-v2"]


def test_worker_activation_does_not_inherit_process_authority(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMECOIN_RADAR_REALISTIC_V3_ACTIVATE", "1")
    conn = connect(tmp_path / "radar.db")

    worker._activate_paper_lifecycles(
        conn,
        adaptive_v3_enabled=False,
        now_iso="2026-08-11T00:00:00Z",
    )

    versions = [
        row[0]
        for row in conn.execute(
            "SELECT lifecycle_version FROM op_paper_lifecycle_activations "
            "ORDER BY lifecycle_version"
        )
    ]
    assert versions == ["radar-realistic-v2"]


def test_enabled_worker_start_activates_v2_and_v3(tmp_path):
    conn = connect(tmp_path / "radar.db")

    worker._activate_paper_lifecycles(
        conn,
        adaptive_v3_enabled=True,
        now_iso="2026-08-11T00:00:00Z",
    )

    versions = {
        row[0]
        for row in conn.execute(
            "SELECT lifecycle_version FROM op_paper_lifecycle_activations"
        )
    }
    # adaptive v3 is its own flag; realistic v3 still needs its own authority
    assert versions == {"radar-adaptive-v3", "radar-realistic-v2"}


def test_default_worker_cycle_does_not_drive_v3(monkeypatch, tmp_path):
    monkeypatch.setattr(
        worker,
        "drive_paper_lifecycle",
        lambda *args, **kwargs: {"v2_quoted": 3},
    )

    def unexpected_v3_drive(*_args, **_kwargs):
        raise AssertionError("adaptive v3 drive was not gated")

    monkeypatch.setattr(
        worker,
        "drive_adaptive_lifecycle",
        unexpected_v3_drive,
        raising=False,
    )

    counters = worker._drive_paper_lifecycles_safely(
        connect(tmp_path / "radar.db"),
        quote_fn=lambda request: request,
        observation_fn=lambda *args, **kwargs: {},
        now_iso="2026-08-11T00:00:00Z",
        adaptive_v3_enabled=False,
    )

    assert counters["v2"] == {"v2_quoted": 3}
    assert counters["v3"] == {"disabled": 1}
    # realistic v3 has no activation row until its baseline age is preregistered
    assert counters["realistic_v3"]["eligible"] == 0


def test_enabled_worker_cycle_drives_v3(monkeypatch, tmp_path):
    monkeypatch.setattr(
        worker,
        "drive_paper_lifecycle",
        lambda *args, **kwargs: {"v2_quoted": 3},
    )
    monkeypatch.setattr(
        worker,
        "drive_adaptive_lifecycle",
        lambda *args, **kwargs: {"v3_quoted": 2},
        raising=False,
    )

    counters = worker._drive_paper_lifecycles_safely(
        connect(tmp_path / "radar.db"),
        quote_fn=lambda request: request,
        observation_fn=lambda *args, **kwargs: {},
        now_iso="2026-08-11T00:00:00Z",
        adaptive_v3_enabled=True,
    )

    assert counters["v2"] == {"v2_quoted": 3}
    assert counters["v3"] == {"v3_quoted": 2}
    assert counters["realistic_v3"]["eligible"] == 0


def test_the_adaptive_observation_fails_closed_without_the_entry_snapshot():
    engine = SimpleNamespace(
        states={},
        state_by_fingerprint={},
        last_observations={},
        last_interval_features={},
    )
    entry = {
        "launch_id": "launch-1",
        "state_fingerprint": "missing-state",
    }

    facts = worker._adaptive_observation_facts(
        engine, entry, now_iso="2026-08-11T00:00:00Z",
    )

    assert facts["tape_complete"] is False
    assert facts["creator_sale_since_entry"] is None
    assert facts["depth_provenance"] == "missing"


def test_the_preregistered_baseline_age_is_the_measured_integer(tmp_path):
    """15,000 ms is DERIVED, not chosen, and it is what activates v3.

    Setting `MAX_BASELINE_AGE_MS` is itself the activation trigger: the worker attempts
    `activate_realistic_v3` on every start and only skips it via
    `BaselineAgeNotPreregistered`. There is no second switch.

    The value came from the frozen rule over the SEALED paper-sampler-v2 window
    (2026-08-11T20:21Z .. 2026-08-12T20:21Z, 38 eligible, p90 14,032 ms). Reproduce with
    `scripts/radar_v3_baseline_age_preregistration.py`.
    """
    from sniper.research.memecoin_opportunity import paper_lifecycle_realistic_v3 as v3

    assert v3.MAX_BASELINE_AGE_MS == 15_000
    assert v3.BASELINE_AGE_MS == 5_000, "the scheduled baseline quote is unchanged"
    assert v3.TARGET_BPS == 1_200
    assert v3.STOP_MOVE_BPS == -800
    config = v3._config()
    assert config["max_baseline_age_ms"] == 15_000
    assert config["stop_reference"] == "on_time_first_quote"
    assert config["paper_only"] is True
