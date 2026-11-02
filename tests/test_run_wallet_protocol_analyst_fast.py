"""Acceptance tests for the --analyst-fast preset (G5 spec).

Spec: docs/superpowers/specs/2026-05-17-g5-analyst-fast-mode.md §3

Acceptance coverage:
  §3.1  Argparse resolution                          — 2 tests
  §3.2  Resolution semantics inside main()            — preset + archetype stamping
  §3.3  Report stamp on data_quality_flags            — 2 tests
  §3.4  Replay args inclusion                         — 2 tests

All tests must be RED until `scripts/run_wallet_protocol.py` implements the preset.
"""
from __future__ import annotations

import pytest
import json
from types import SimpleNamespace


# ---------------------------------------------------------------------------
# Minimal fake ProtocolReport — only touches data_quality_flags
# ---------------------------------------------------------------------------

def _make_fake_report(wallet: str = "0xdeadbeef") -> object:
    from sniper.analysis.wallet_protocol.types import ProtocolReport
    return ProtocolReport(wallet=wallet)


# ---------------------------------------------------------------------------
# Helpers shared across main()-exercising tests
# ---------------------------------------------------------------------------

FAKE_WALLET = "0xdeadbeef"


def _stub_fetch_and_analyze(monkeypatch, *, report=None):
    """Patch fetch_wallet_data and analyze_wallet so main() never hits the
    network. Returns a namespace with .captured_fetch_kwargs and
    .captured_analyze_kwargs so callers can inspect what was passed."""
    import scripts.run_wallet_protocol as rwp

    ns = SimpleNamespace(captured_fetch_kwargs=None, captured_analyze_kwargs=None)

    if report is None:
        report = _make_fake_report(FAKE_WALLET)

    class _FakeData:
        wallet = FAKE_WALLET
        closed_positions = []
        open_positions = []
        activity_rows = []
        activity_cap_hit = False
        subgraph_fallback_used = False
        replay_surface_meta = None
        leafs_by_cid = None
        nav_lookup = None
        archetype_layer1 = None
        archetype_layer2 = None
        extractor_features_summary = None
        execution_profile = None

    def _fake_fetch(wallet, session, **kwargs):
        ns.captured_fetch_kwargs = kwargs
        return _FakeData()

    def _fake_analyze(wallet, session=None, **kwargs):
        ns.captured_analyze_kwargs = kwargs
        return report

    monkeypatch.setattr(rwp, "fetch_wallet_data", _fake_fetch)
    monkeypatch.setattr(rwp, "analyze_wallet", _fake_analyze)
    monkeypatch.setattr(
        rwp, "maybe_emit_memo",
        lambda report, persist_memo=False: SimpleNamespace(
            status="skipped_missing_env", message=""
        ),
    )

    import sqlite3 as _sqlite3

    class _FakeCursor:
        def fetchall(self):
            return []

        def fetchone(self):
            return None

        def execute(self, *a, **kw):
            return self

        def __iter__(self):
            return iter([])

    class _FakeConn:
        def close(self):
            pass

        def execute(self, *a, **kw):
            return _FakeCursor()

        def cursor(self):
            return _FakeCursor()

    monkeypatch.setattr(_sqlite3, "connect", lambda *a, **kw: _FakeConn())
    monkeypatch.setattr(
        rwp, "fetch_tag_lookups_from_db", lambda conn, cids: (None, None)
    )

    return ns


# ===========================================================================
# §3.1  Argparse resolution — parse time only
# ===========================================================================

def test_analyst_fast_flag_parses():
    """Parse [wallet, '--analyst-fast']; assert args.analyst_fast is True."""
    from scripts.run_wallet_protocol import _build_parser
    args = _build_parser().parse_args([FAKE_WALLET, "--analyst-fast"])
    assert args.analyst_fast is True


def test_analyst_fast_alone_does_not_set_no_flags_at_parse_time():
    """Pins where resolution happens (in main(), not argparse). The preset
    forces no_history/no_exec at main() time, NOT at parse time."""
    from scripts.run_wallet_protocol import _build_parser
    args = _build_parser().parse_args([FAKE_WALLET, "--analyst-fast"])
    assert args.no_history is False
    assert args.no_exec is False


# ===========================================================================
# §3.2  Resolution semantics inside main()
# ===========================================================================

def test_analyst_fast_forces_no_history_and_no_exec(monkeypatch):
    """main() under --analyst-fast must propagate no_history=True (price_history_fn
    is None when passed to analyze_wallet) and disable capital_ledger fetch."""
    import scripts.run_wallet_protocol as rwp

    ns = _stub_fetch_and_analyze(monkeypatch)
    rc = rwp.main([FAKE_WALLET, "--analyst-fast"])

    assert rc == 0, f"main() returned non-zero: {rc}"
    assert ns.captured_analyze_kwargs is not None, "analyze_wallet never called"
    assert ns.captured_analyze_kwargs.get("price_history_fn") is None, (
        "price_history_fn should be None when --analyst-fast forces no_history"
    )
    assert ns.captured_fetch_kwargs.get("with_capital_ledger") is not True


def test_analyst_fast_forces_capital_ledger_off_with_warning(monkeypatch, capsys):
    """Combining --analyst-fast with --capital-ledger emits a stderr warning
    that mentions both 'capital_ledger' and 'analyst-fast', and the preset
    wins (fetch called with with_capital_ledger=False)."""
    import scripts.run_wallet_protocol as rwp

    ns = _stub_fetch_and_analyze(monkeypatch)
    rc = rwp.main([FAKE_WALLET, "--analyst-fast", "--capital-ledger"])
    assert rc == 0, f"main() returned non-zero: {rc}"

    stderr = capsys.readouterr().err
    assert "capital_ledger" in stderr, (
        f"Expected 'capital_ledger' in stderr warning. Got: {stderr!r}"
    )
    assert "analyst-fast" in stderr, (
        f"Expected 'analyst-fast' in stderr warning. Got: {stderr!r}"
    )
    assert ns.captured_fetch_kwargs is not None, "fetch_wallet_data never called"
    assert ns.captured_fetch_kwargs.get("with_capital_ledger") is False, (
        f"Expected with_capital_ledger=False, got: {ns.captured_fetch_kwargs}"
    )


def test_analyst_fast_composes_additively_with_no_tags(monkeypatch):
    """Combining --analyst-fast with --no-tags applies both: tag-grain
    lookups skipped AND price_history disabled. No extra warning."""
    import scripts.run_wallet_protocol as rwp

    ns = _stub_fetch_and_analyze(monkeypatch)
    rc = rwp.main([FAKE_WALLET, "--analyst-fast", "--no-tags"])
    assert rc == 0, f"main() returned non-zero: {rc}"

    assert ns.captured_analyze_kwargs is not None, "analyze_wallet never called"
    assert ns.captured_analyze_kwargs.get("price_history_fn") is None, (
        "price_history_fn should be None when --analyst-fast forces no_history"
    )
    assert ns.captured_analyze_kwargs.get("leafs_by_cid") is None, (
        "leafs_by_cid should be None when --no-tags is set"
    )


def test_analyst_fast_keeps_archetype_detection_but_skips_execution_profile(monkeypatch):
    """Analyst-fast omits expensive execution attribution, not archetype
    classification. --no-archetype remains the explicit archetype off switch."""
    import scripts.run_wallet_protocol as rwp
    import scripts.wallet_archetype_extractor as archetype_mod

    calls = SimpleNamespace(archetype=0, execution_profile=0)

    def _fake_extract(wallet, **kwargs):
        calls.archetype += 1
        assert kwargs.get("skip_gate0") is True
        assert kwargs.get("with_execution") is False
        return {
            "classification": {"archetype": "single_sided_specialist"},
            "classification_layer2": {"archetype": "news_reactor"},
            "domain_tags": {"primary_tag_share": 0.75},
            "trade_shape": {
                "usdc_size_cv": 0.42,
                "top_condition_fill_share": 0.2,
            },
        }

    def _trap_execution_profile(*args, **kwargs):
        calls.execution_profile += 1
        raise AssertionError("analyst-fast must not build execution_profile")

    monkeypatch.setattr(archetype_mod, "extract_wallet_archetype", _fake_extract)
    monkeypatch.setattr(rwp, "build_execution_profile", _trap_execution_profile)

    ns = _stub_fetch_and_analyze(monkeypatch)
    rc = rwp.main([FAKE_WALLET, "--analyst-fast"])

    assert rc == 0, f"main() returned non-zero: {rc}"
    assert calls.archetype == 1
    assert calls.execution_profile == 0
    data = ns.captured_analyze_kwargs["pre_fetched_data"]
    assert data.archetype_layer1 == "single_sided_specialist"
    assert data.archetype_layer2 == "news_reactor"
    assert data.extractor_features_summary == {
        "domain_concentration": 0.75,
        "usdc_size_cv": 0.42,
        "top_condition_fill_share": 0.2,
    }


def test_explicit_archetype_stamps_wallet_data_before_analysis(monkeypatch):
    """--archetype is an operator override for report/gate input, not only
    execution-profile input."""
    import scripts.run_wallet_protocol as rwp

    ns = _stub_fetch_and_analyze(monkeypatch)
    rc = rwp.main([FAKE_WALLET, "--archetype", "paired_outcome_arb", "--no-exec"])

    assert rc == 0, f"main() returned non-zero: {rc}"
    data = ns.captured_analyze_kwargs["pre_fetched_data"]
    assert data.archetype_layer1 == "paired_outcome_arb"


def test_analyst_fast_fixture_mode_without_explicit_no_flags_has_no_advisory_warning(
    monkeypatch, capsys,
):
    """The --from-fixture advisory is for operator-passed --no-* flags, not
    flags internally forced by --analyst-fast."""
    import scripts.run_wallet_protocol as rwp

    class _FixtureData:
        wallet = FAKE_WALLET
        closed_positions = []
        open_positions = []
        activity_rows = []
        activity_cap_hit = False
        subgraph_fallback_used = False
        replay_surface_meta = None
        leafs_by_cid = None
        nav_lookup = None
        archetype_layer1 = None
        archetype_layer2 = None
        extractor_features_summary = None
        execution_profile = None

    monkeypatch.setattr(rwp, "load_fetch_snapshot", lambda path: _FixtureData())
    monkeypatch.setattr(rwp, "build_fixture_closures", lambda data: (None, None))
    monkeypatch.setattr(
        rwp,
        "analyze_wallet",
        lambda *a, **kw: _make_fake_report(FAKE_WALLET),
    )

    rc = rwp.main([FAKE_WALLET, "--from-fixture", "fixture.json", "--analyst-fast"])

    assert rc == 0, f"main() returned non-zero: {rc}"
    stderr = capsys.readouterr().err
    assert "--no-* flags are advisory" not in stderr


# ===========================================================================
# §3.3  Report stamp
# ===========================================================================

EXPECTED_FLAG = "ANALYST_FAST_MODE_OMITTED:price_history,exec_attribution,capital_ledger"


def test_analyst_fast_stamps_omitted_flag_on_data_quality_flags(monkeypatch):
    """After analyze_wallet returns, CLI appends the omitted-surfaces flag
    onto report.data_quality_flags exactly once."""
    import scripts.run_wallet_protocol as rwp

    report = _make_fake_report(FAKE_WALLET)
    assert report.data_quality_flags == [], "precondition: fresh report has empty flags"

    _stub_fetch_and_analyze(monkeypatch, report=report)
    rc = rwp.main([FAKE_WALLET, "--analyst-fast"])
    assert rc == 0, f"main() returned non-zero: {rc}"

    count = report.data_quality_flags.count(EXPECTED_FLAG)
    assert count == 1, (
        f"Expected exactly 1 occurrence of {EXPECTED_FLAG!r} in "
        f"data_quality_flags, got {count}. Flags: {report.data_quality_flags}"
    )


def test_no_stamp_when_analyst_fast_absent(monkeypatch):
    """Stamp is conditional on the preset, not unconditional. Without the
    flag, no ANALYST_FAST_MODE_OMITTED entry appears."""
    import scripts.run_wallet_protocol as rwp
    from scripts.run_wallet_protocol import _build_parser

    report = _make_fake_report(FAKE_WALLET)
    _stub_fetch_and_analyze(monkeypatch, report=report)

    args = _build_parser().parse_args([FAKE_WALLET])
    assert args.analyst_fast is False, (
        "args.analyst_fast must default to False when flag is absent"
    )

    rc = rwp.main([FAKE_WALLET])
    assert rc == 0, f"main() returned non-zero: {rc}"

    omitted = [f for f in report.data_quality_flags if "ANALYST_FAST_MODE_OMITTED" in f]
    assert omitted == [], (
        f"No ANALYST_FAST_MODE_OMITTED flag should appear without --analyst-fast. "
        f"Got: {omitted}"
    )


# ===========================================================================
# §3.4  Replay args
# ===========================================================================

def test_replay_args_json_includes_analyst_fast_true_when_set():
    from scripts.run_wallet_protocol import _build_parser, _replay_args_json
    args = _build_parser().parse_args([FAKE_WALLET, "--analyst-fast"])
    payload = json.loads(_replay_args_json(args))
    assert "analyst_fast" in payload, (
        f"'analyst_fast' key missing from _replay_args_json output. "
        f"Keys: {list(payload)}"
    )
    assert payload["analyst_fast"] is True, (
        f"Expected analyst_fast=True, got: {payload['analyst_fast']!r}"
    )


def test_replay_args_json_includes_analyst_fast_false_by_default():
    """Explicit absence — replay correctness needs the key to be False, not omitted."""
    from scripts.run_wallet_protocol import _build_parser, _replay_args_json
    args = _build_parser().parse_args([FAKE_WALLET])
    payload = json.loads(_replay_args_json(args))
    assert "analyst_fast" in payload, (
        f"'analyst_fast' key missing from _replay_args_json output without flag. "
        f"Keys: {list(payload)}"
    )
    assert payload["analyst_fast"] is False, (
        f"Expected analyst_fast=False by default, got: {payload['analyst_fast']!r}"
    )


@pytest.fixture(autouse=True)
def _polymarket_is_sinkholed(monkeypatch):
    """This host resolves *.polymarket.com to an unreachable address (2026-07-29 sinkhole, moved to an
    unreachable IPv6 on 2026-09-22), so a live activity fetch hangs past the test budget instead of
    failing fast. These tests exercise flags and stamps, never the data: fail the fetch at once, as
    a refused connection did before."""
    from data import polymarket_activity_client as _pac

    def _no_network(path, **_kw):
        raise _pac.PolymarketActivityError("request failed after retries: sinkholed in tests")
    monkeypatch.setattr(_pac, "_get_json", _no_network)
