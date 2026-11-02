from types import SimpleNamespace
import sqlite3

import pytest

from scripts import memecoin_opportunity_worker as worker
from sniper.research.memecoin_opportunity.paper_adapter import (
    ADAPTER_VERSION,
    FILL_MODEL_VERSION,
    QuoteDeferred,
    PaperFillRequest,
    append_paper_fill_request,
)
from sniper.research.memecoin_opportunity.paper_completion import PaperQuoteCompletion
from sniper.research.memecoin_opportunity.paper_lifecycle import (
    activate_paper_lifecycle,
)
from sniper.research.memecoin_opportunity.state import PUMP_SOL_QUOTE_MINT
from sniper.research.memecoin_opportunity.store import connect
from sniper.research.memecoin_opportunity.types import VenueId


def _request():
    return PaperFillRequest(
        request_id="r1", alert_id="a1", launch_id="l1", network_id="solana",
        venue_id="pumpfun_solana", token_id="Mint1", creator_id="Creator1",
        launch_mode="standard", state_fingerprint="sf",
        rule_version="pump-confirmation-truth-v5", evaluator_version="alert-outcome-v4",
        adapter_version=ADAPTER_VERSION, fill_model_version=FILL_MODEL_VERSION,
        clip_usd_micros=25_000_000, decision_at="2026-08-05T00:00:00Z",
        decision_age_ms=20_000, intent_at="2026-08-05T00:00:00Z",
        expires_at="2026-08-05T00:00:30Z", local_qty_atomic=1_050,
    )


def test_worker_probe_quote_is_nonblocking_and_uses_the_configured_usd_clip():
    economics = {"expected_out_atomic": 1_000, "floor_out_atomic": 940}

    class Probe:
        calls = []

        def paper_quote(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return {"status": "pending"}
            return {"status": "filled", "economics": economics}

    engine = SimpleNamespace(
        states={
            "l1": SimpleNamespace(
                venue_id=VenueId.PUMPFUN_SOLANA,
                quote_mint=PUMP_SOL_QUOTE_MINT,
                token_id="Mint1",
            )
        },
        _market_input=lambda _state: (100_000_000, 9, 100, 0),
    )
    quote = worker._paper_probe_quote_fn(Probe(), engine)
    request = {
        "request_id": "r1", "launch_id": "l1", "token_id": "Mint1",
        "clip_usd_micros": 25_000_000,
    }

    with pytest.raises(QuoteDeferred):
        quote(request)
    assert quote(request) == economics
    assert Probe.calls[0]["amount_atomic"] == 250_000_000
    assert Probe.calls[0]["input_decimals"] == 9
    assert Probe.calls[0]["output_decimals"] == 6


def test_worker_adapter_failure_is_reported_without_escaping_capture(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    append_paper_fill_request(conn, _request())

    result = worker._drive_paper_adapter_safely(
        conn,
        quote_fn=lambda _row: (_ for _ in ()).throw(RuntimeError("boom")),
        now_iso="2026-08-05T00:00:02Z",
    )

    assert result == {"adapter_error": 1}
    status = conn.execute(
        "SELECT status,blocker_code FROM op_runtime_status "
        "WHERE source_key='opportunity:paper_adapter'"
    ).fetchone()
    assert tuple(status) == ("degraded", "PAPER_ADAPTER_FAILURE")


def test_worker_exit_probe_sells_the_exact_entry_floor_quantity():
    economics = {"expected_out_atomic": 26_000_000, "floor_out_atomic": 25_000_000}

    class Probe:
        calls = []

        def paper_exit_quote(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return {"status": "pending"}
            return {"status": "filled", "economics": economics}

    quote = worker._paper_exit_quote_fn(Probe())
    request = {
        "observation_id": "obs-1",
        "token_id": "Mint1",
        "quantity_atomic": 8_800,
        "token_decimals": 6,
        "quote_asset": PUMP_SOL_QUOTE_MINT,
        "quote_decimals": 9,
    }

    with pytest.raises(QuoteDeferred):
        quote(request)
    assert quote(request) == economics
    assert Probe.calls[0] == {
        "request_id": "obs-1",
        "input_mint": "Mint1",
        "output_mint": PUMP_SOL_QUOTE_MINT,
        "amount_atomic": 8_800,
        "input_decimals": 6,
        "output_decimals": 9,
    }


def test_worker_paper_dispatch_uses_the_evm_probe_for_bags_and_pons():
    economics = {"expected_out_atomic": 1_000, "floor_out_atomic": 970}

    class EvmProbe:
        entry_calls = []
        exit_calls = []

        def paper_entry(self, **kwargs):
            self.entry_calls.append(kwargs)
            return {"status": "filled", "economics": economics}

        def paper_exit(self, **kwargs):
            self.exit_calls.append(kwargs)
            return {"status": "filled", "economics": economics}

    state = SimpleNamespace(
        launch_id="l1",
        venue_id=VenueId.PONS_V1_ROBINHOOD_4663,
        token_id="0x1111111111111111111111111111111111111111",
    )
    engine = SimpleNamespace(
        states={"l1": state},
        _market_input=lambda _state: (4_000_000_000, 18, 100, 0),
    )
    evm = EvmProbe()
    entry_quote = worker._paper_quote_fn(None, evm, engine)
    exit_quote = worker._paper_exit_quote_dispatch_fn(None, evm)

    assert entry_quote({
        "request_id": "r1",
        "launch_id": "l1",
        "token_id": state.token_id,
        "venue_id": "pons_v1_4663",
        "clip_usd_micros": 25_000_000,
        "state_fingerprint": "state-1",
    }) == economics
    assert evm.entry_calls[0]["quote_in_atomic"] == 6_250_000_000_000_000

    assert exit_quote({
        "observation_id": "o1",
        "venue_id": "pons_v1_4663",
        "token_id": state.token_id,
        "quantity_atomic": 1_000,
        "token_decimals": 18,
        "quote_decimals": 18,
        "route_binding": {"pool_id": "pool"},
    }) == economics
    assert evm.exit_calls[0]["venue_id"] is VenueId.PONS_V1_ROBINHOOD_4663


def test_worker_tracks_and_drains_the_requested_exit_bucket(monkeypatch):
    sampler = worker.PaperSampler()
    request = {
        "observation_id": "obs-5000",
        "request_id": "entry-1",
        "venue_id": "pumpfun_solana",
        "scheduled_age_ms": 5_000,
        "now_iso": "2026-08-11T12:00:05Z",
        "token_id": "Mint1",
        "quantity_atomic": 8_800,
        "token_decimals": 6,
        "quote_asset": PUMP_SOL_QUOTE_MINT,
        "quote_decimals": 9,
        "route_binding": {},
    }
    completion = PaperQuoteCompletion(
        observation_id="obs-5000",
        venue_id="pumpfun_solana",
        status="filled",
        economics={"expected_out_atomic": 25_000_000},
        failure_reason=None,
        requested_at="2026-08-11T12:00:05Z",
        completed_at="2026-08-11T12:00:11Z",
    )

    class Probe:
        provider_calls = 0

        def paper_exit_quote(self, **_kwargs):
            self.provider_calls += 1
            return {"status": "pending"}

        @staticmethod
        def paper_request_pending(_request_id):
            return True

        @staticmethod
        def drain_paper_completions(*, limit=32):
            assert limit == 32
            return (completion,)

    probe = Probe()
    quote = worker._paper_exit_quote_dispatch_fn(
        probe,
        None,
        sampler=sampler,
    )
    with pytest.raises(QuoteDeferred):
        quote(request)

    stored = []
    monkeypatch.setattr(
        worker,
        "append_bound_paper_completion",
        lambda _conn, *, request, completion: stored.append(
            (dict(request), completion)
        ) or True,
    )
    counters = worker._drain_paper_quote_completions(
        object(),
        sampler=sampler,
        solana_probe=probe,
        evm_probe=None,
    )

    assert probe.provider_calls == 1
    assert counters == {
        "completed": 1,
        "stored": 1,
        "orphaned": 0,
        "deferred": 0,
    }
    assert stored[0][0]["scheduled_age_ms"] == 5_000
    assert stored[0][1].observation_id == "obs-5000"


def test_worker_does_not_count_a_bucket_that_waits_outside_a_full_queue():
    sampler = worker.PaperSampler()

    class Probe:
        @staticmethod
        def paper_exit_quote(**_kwargs):
            return {"status": "pending"}

        @staticmethod
        def paper_request_pending(_request_id):
            return False

    quote = worker._paper_exit_quote_dispatch_fn(
        Probe(),
        None,
        sampler=sampler,
    )
    with pytest.raises(QuoteDeferred):
        quote({
            "observation_id": "obs-capacity",
            "request_id": "entry-1",
            "venue_id": "pumpfun_solana",
            "scheduled_age_ms": 5_000,
            "now_iso": "2026-08-11T12:00:05Z",
            "token_id": "Mint1",
            "quantity_atomic": 8_800,
            "token_decimals": 6,
            "quote_asset": PUMP_SOL_QUOTE_MINT,
            "quote_decimals": 9,
            "route_binding": {},
        })

    assert sampler.active_count == 0


def test_worker_retries_a_failed_completion_append_without_losing_later_rows(
    monkeypatch,
):
    sampler = worker.PaperSampler()
    requests = [
        {
            "observation_id": "obs-5000",
            "request_id": "entry-1",
            "venue_id": "pumpfun_solana",
            "scheduled_age_ms": 5_000,
        },
        {
            "observation_id": "obs-10000",
            "request_id": "entry-1",
            "venue_id": "pumpfun_solana",
            "scheduled_age_ms": 10_000,
        },
    ]
    completions = tuple(
        PaperQuoteCompletion(
            observation_id=request["observation_id"],
            venue_id="pumpfun_solana",
            status="filled",
            economics={"expected_out_atomic": 25_000_000},
            failure_reason=None,
            requested_at="2026-08-11T12:00:05Z",
            completed_at="2026-08-11T12:00:11Z",
        )
        for request in requests
    )
    for request in requests:
        sampler.track(request, requested_at="2026-08-11T12:00:05Z")

    class Probe:
        batch = completions

        def drain_paper_completions(self, *, limit=32):
            result, self.batch = self.batch, ()
            return result

    attempts = []

    def append(_conn, *, request, completion):
        attempts.append(completion.observation_id)
        if (
            completion.observation_id == "obs-5000"
            and attempts.count("obs-5000") == 1
        ):
            raise sqlite3.OperationalError("locked")
        return True

    monkeypatch.setattr(worker, "append_bound_paper_completion", append)
    probe = Probe()

    first = worker._drain_paper_quote_completions(
        object(), sampler=sampler, solana_probe=probe, evm_probe=None,
    )
    second = worker._drain_paper_quote_completions(
        object(), sampler=sampler, solana_probe=probe, evm_probe=None,
    )

    assert first == {
        "completed": 2,
        "stored": 1,
        "orphaned": 0,
        "deferred": 1,
    }
    assert second == {
        "completed": 0,
        "stored": 1,
        "orphaned": 0,
        "deferred": 0,
    }
    assert attempts == ["obs-5000", "obs-10000", "obs-5000"]
    assert sampler.retry_count == 0


def test_worker_drains_bucket_completions_before_the_v2_lifecycle(monkeypatch):
    order = []

    class Sampler:
        active_count = 0

        @staticmethod
        def persistence_due(*, now_iso):
            return bool(now_iso)

        def maybe_persist(self, *_args, **_kwargs):
            order.append("persist")
            return True

    class Probe:
        @staticmethod
        def metrics():
            return {}

    sampler = Sampler()

    monkeypatch.setattr(
        worker,
        "_drain_paper_quote_completions",
        lambda *_args, **_kwargs: order.append("drain") or {},
    )
    monkeypatch.setattr(
        worker,
        "_drive_realistic_v3_safely",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        worker,
        "_drive_paper_lifecycle_safely",
        lambda *_args, **_kwargs: order.append("v2") or {},
    )

    worker._drive_paper_lifecycles_safely(
        object(),
        quote_fn=lambda _request: {},
        observation_fn=lambda *_args, **_kwargs: {},
        now_iso="2026-08-11T12:00:11Z",
        sampler=sampler,
        solana_probe=Probe(),
        evm_probe=Probe(),
    )

    assert order[:2] == ["drain", "v2"]


def test_worker_persists_sampler_metrics_after_the_v2_lifecycle(monkeypatch):
    order = []

    class Sampler:
        active_count = 0

        @staticmethod
        def persistence_due(*, now_iso):
            return True

        def maybe_persist(self, _conn, *, now_iso, probe_metrics):
            order.append(("persist", now_iso, probe_metrics))
            return True

    class Probe:
        def __init__(self, name):
            self.name = name

        def metrics(self):
            return {"probe": self.name}

    monkeypatch.setattr(
        worker,
        "_drain_paper_quote_completions",
        lambda *_args, **_kwargs: order.append(("drain",)) or {},
    )
    monkeypatch.setattr(
        worker,
        "_drive_realistic_v3_safely",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        worker,
        "_drive_paper_lifecycle_safely",
        lambda *_args, **_kwargs: order.append(("v2",)) or {},
    )

    worker._drive_paper_lifecycles_safely(
        object(),
        quote_fn=lambda _request: {},
        observation_fn=lambda *_args, **_kwargs: {},
        now_iso="2026-08-11T12:00:11Z",
        sampler=Sampler(),
        solana_probe=Probe("solana"),
        evm_probe=Probe("evm"),
    )

    assert order == [
        ("drain",),
        ("v2",),
        (
            "persist",
            "2026-08-11T12:00:11Z",
            {
                "solana": {"probe": "solana"},
                "evm": {"probe": "evm"},
            },
        ),
    ]


def test_worker_does_not_lock_probe_metrics_before_sampler_write_is_due(monkeypatch):
    class Sampler:
        active_count = 0

        @staticmethod
        def persistence_due(*, now_iso):
            assert now_iso == "2026-08-11T12:00:11Z"
            return False

        @staticmethod
        def maybe_persist(*_args, **_kwargs):
            raise AssertionError("the status write is not due")

    class Probe:
        @staticmethod
        def metrics():
            raise AssertionError("the probe lock is not due")

    monkeypatch.setattr(
        worker,
        "_drain_paper_quote_completions",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        worker,
        "_drive_realistic_v3_safely",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        worker,
        "_drive_paper_lifecycle_safely",
        lambda *_args, **_kwargs: {},
    )

    worker._drive_paper_lifecycles_safely(
        object(),
        quote_fn=lambda _request: {},
        observation_fn=lambda *_args, **_kwargs: {},
        now_iso="2026-08-11T12:00:11Z",
        sampler=Sampler(),
        solana_probe=Probe(),
        evm_probe=Probe(),
    )


def test_sampler_status_write_failure_does_not_stop_the_v2_lifecycle(monkeypatch):
    class Sampler:
        active_count = 0

        @staticmethod
        def persistence_due(*, now_iso):
            return bool(now_iso)

        @staticmethod
        def maybe_persist(*_args, **_kwargs):
            raise sqlite3.OperationalError("locked")

    class Probe:
        @staticmethod
        def metrics():
            return {}

    monkeypatch.setattr(
        worker,
        "_drain_paper_quote_completions",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        worker,
        "_drive_realistic_v3_safely",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        worker,
        "_drive_paper_lifecycle_safely",
        lambda *_args, **_kwargs: {"eligible": 1},
    )
    monkeypatch.setattr(
        worker,
        "upsert_runtime_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("still locked")
        ),
    )

    result = worker._drive_paper_lifecycles_safely(
        object(),
        quote_fn=lambda _request: {},
        observation_fn=lambda *_args, **_kwargs: {},
        now_iso="2026-08-11T12:00:11Z",
        sampler=Sampler(),
        solana_probe=Probe(),
        evm_probe=Probe(),
    )

    assert result["v2"] == {"eligible": 1}


def test_worker_lifecycle_failure_is_reported_without_escaping_capture(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")

    result = worker._drive_paper_lifecycle_safely(
        conn,
        quote_fn=lambda _row: (_ for _ in ()).throw(RuntimeError("boom")),
        now_iso="2026-08-06T00:00:02Z",
    )

    assert result == {"eligible": 0, "quoted": 0, "completed": 0, "censored": 0, "extra": 0}
    status = conn.execute(
        "SELECT status,blocker_code FROM op_runtime_status "
        "WHERE source_key='opportunity:paper_lifecycle'"
    ).fetchone()
    assert tuple(status) == ("healthy", None)


def test_quote_path_rows_keep_each_venue_failure_separate():
    rows = worker._quote_path_status_rows(
        solana_metrics={
            "requested": 3,
            "completed": 2,
            "failed": 1,
            "pending": 0,
            "queued": 0,
            "paper_queued": 0,
            "last_request_at": "2026-08-11T12:00:08Z",
            "last_success_at": "2026-08-11T12:00:09Z",
            "last_failure_at": "2026-08-11T12:00:07Z",
            "last_failure": "REMOTE_QUOTE_PROVIDER_ERROR",
        },
        evm_metrics={
            "queue_depth": 0,
            "by_venue": {
                "bags_robinhood_4663": {
                    "requested": 1,
                    "completed": 0,
                    "failed": 1,
                    "pending": 0,
                    "last_request_at": "2026-08-11T12:00:08Z",
                    "last_success_at": None,
                    "last_failure_at": "2026-08-11T12:00:09Z",
                    "last_failure": "RuntimeError",
                },
                "pons_v1_4663": {
                    "requested": 0,
                    "completed": 0,
                    "failed": 0,
                    "pending": 0,
                    "last_request_at": None,
                    "last_success_at": None,
                    "last_failure_at": None,
                    "last_failure": None,
                },
            },
        },
        now_iso="2026-08-11T12:00:10Z",
    )

    assert rows["pumpfun_solana"]["status"] == "healthy"
    assert rows["pumpfun_solana"]["detail"]["state"] == "current"
    assert rows["bags_robinhood_4663"]["status"] == "degraded"
    assert rows["bags_robinhood_4663"]["blocker_code"] == "QUOTE_PATH_FAILURE"
    assert rows["pons_v1_4663"]["status"] == "healthy"
    assert rows["pons_v1_4663"]["detail"]["state"] == "idle"


def test_quote_path_row_degrades_a_stuck_direct_request():
    rows = worker._quote_path_status_rows(
        solana_metrics={
            "requested": 1,
            "completed": 0,
            "failed": 0,
            "pending": 1,
            "queued": 1,
            "paper_queued": 0,
            "last_request_at": "2026-08-11T11:57:00Z",
            "last_success_at": None,
            "last_failure_at": None,
            "last_failure": None,
        },
        evm_metrics={"queue_depth": 0, "by_venue": {}},
        now_iso="2026-08-11T12:00:10Z",
    )

    assert rows["pumpfun_solana"]["status"] == "degraded"
    assert rows["pumpfun_solana"]["blocker_code"] == "QUOTE_PATH_STALE"


def test_worker_publishes_one_direct_quote_status_for_each_venue(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    rows = {
        venue: {
            "status": "healthy",
            "blocker_code": None,
            "queue_depth": 0,
            "detail": {"state": "idle"},
        }
        for venue in (
            "pumpfun_solana",
            # The writer must reject this retired venue.
            "bags_robinhood_4663",
            "pons_v1_4663",
        )
    }

    worker._publish_quote_path_status_rows(
        conn,
        rows,
        now_iso="2026-08-11T12:00:10Z",
    )

    stored = conn.execute(
        "SELECT source_key,status,blocker_code,queue_depth "
        "FROM op_runtime_status WHERE source_key LIKE 'opportunity:quote:%' "
        "ORDER BY source_key"
    ).fetchall()
    # The writer does not store the retired venue row.
    assert [tuple(row) for row in stored] == [
        ("opportunity:quote:pons_v1_4663", "healthy", None, 0),
        ("opportunity:quote:pumpfun_solana", "healthy", None, 0),
    ]


def test_runtime_timing_metrics_keep_fixed_keys_and_limit_counts():
    metrics = worker._RuntimeTimingMetrics()

    metrics.observe("main_loop", 0.05, limit_s=0.1)
    metrics.observe("main_loop", 0.2, limit_s=0.1)
    metrics.observe("maintenance", 2.0, limit_s=1.0)

    snapshot = metrics.snapshot()
    assert set(snapshot) == set(worker._RUNTIME_TIMING_KEYS)
    assert snapshot["main_loop"] == {
        "last_duration_ms": 200,
        "max_duration_ms": 200,
        "runs": 2,
        "over_limit_count": 1,
        "last_over_limit": True,
        "limit_ms": 100,
    }
    assert snapshot["maintenance"]["over_limit_count"] == 1
    assert snapshot["pump_lane"]["runs"] == 0

    with pytest.raises(ValueError, match="unknown runtime timing key"):
        metrics.observe("unbounded-key", 1.0, limit_s=1.0)
