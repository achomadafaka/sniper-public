"""G3 — Acceptance tests for `triage_context` on `ProtocolReport`.

Spec: docs/superpowers/specs/2026-05-17-g3-triage-context.md §3
Origin: docs/audits/2026-05-10-wallet-analyzer-live-sampling.md G3

Adds an optional `triage_context: dict | None` field on `ProtocolReport`
plus a `--triage-context-json` CLI flag for operator-stamped metadata
(leaderboard source/period/rank/PnL at capture). Verbatim passthrough;
no schema validation. Replay round-trips the raw JSON string.

12 tests covering:
  §3.1  Field default                       — 2 tests
  §3.2  Analyzer kwarg passthrough          — 3 tests
  §3.3  CLI argparse                        — 2 tests
  §3.4  CLI stamping + JSON error           — 3 tests
  §3.5  Replay round-trip                   — 2 tests

All tests must be RED until ProtocolReport gets the new field,
analyze_wallet gets the kwarg, the CLI parses --triage-context-json,
and replay_wallet_run reconstructs the flag.
"""
from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


def _now() -> int:
    return int(time.time())


def _wallet_data() -> WalletData:
    return WalletData(
        wallet="0xabc",
        closed_positions=[],
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=None,
        fetched_at=_now(),
        subgraph_fallback_used=False,
        walk_full_history=False,
        n_unmapped_fills=0,
    )


FAKE_WALLET = "0xdeadbeef"


# ===========================================================================
# §3.1  Field default
# ===========================================================================

def test_triage_context_defaults_to_none_on_bare_protocolreport():
    bare = ProtocolReport(wallet="0xnone")
    assert bare.triage_context is None


def test_triage_context_defaults_to_none_after_analyze_wallet_without_kwarg():
    report = analyze_wallet("0xabc", pre_fetched_data=_wallet_data(), n_iter=50, seed=1)
    assert report.triage_context is None


# ===========================================================================
# §3.2  Analyzer kwarg passthrough
# ===========================================================================

def test_analyze_wallet_stamps_triage_context_when_kwarg_passed():
    ctx = {"source": "polymarket_public_leaderboard"}
    report = analyze_wallet(
        "0xabc", pre_fetched_data=_wallet_data(), n_iter=50, seed=1,
        triage_context=ctx,
    )
    assert report.triage_context == {"source": "polymarket_public_leaderboard"}


def test_analyze_wallet_preserves_arbitrary_keys():
    ctx = {
        "source": "polymarket_public_leaderboard",
        "period": "MONTH",
        "fetched_at": 1747700000,
        "rank": 3,
        "lb_pnl_at_capture": 1760000.0,
        "note": "free text",
        "nested": {"a": [1, 2, 3]},
    }
    report = analyze_wallet(
        "0xabc", pre_fetched_data=_wallet_data(), n_iter=50, seed=1,
        triage_context=ctx,
    )
    assert report.triage_context == ctx


def test_analyze_wallet_does_not_mutate_caller_state_after_stamp():
    """Mutating the caller's dict after analyze_wallet must not retroactively
    edit report.triage_context. Pins the safer behaviour (analyze_wallet
    must take a defensive copy)."""
    ctx = {"source": "lb", "rank": 3}
    report = analyze_wallet(
        "0xabc", pre_fetched_data=_wallet_data(), n_iter=50, seed=1,
        triage_context=ctx,
    )
    snapshot = copy.deepcopy(report.triage_context)
    ctx["rank"] = 999
    ctx["new_key"] = "added later"
    assert report.triage_context == snapshot, (
        f"analyze_wallet must defensively copy triage_context. "
        f"Pre-mutation: {snapshot}; post-mutation: {report.triage_context}"
    )


# ===========================================================================
# §3.3  CLI argparse
# ===========================================================================

def test_cli_parses_triage_context_json_flag():
    from scripts.run_wallet_protocol import _build_parser
    args = _build_parser().parse_args(
        [FAKE_WALLET, "--triage-context-json", '{"a": 1}']
    )
    assert args.triage_context_json == '{"a": 1}'


def test_cli_defaults_triage_context_json_to_none():
    from scripts.run_wallet_protocol import _build_parser
    args = _build_parser().parse_args([FAKE_WALLET])
    assert args.triage_context_json is None


# ===========================================================================
# §3.4  CLI stamping + JSON error
# ===========================================================================

def _stub_cli(monkeypatch):
    """Stub fetch/analyze; capture triage_context kwarg passed to analyze."""
    import scripts.run_wallet_protocol as rwp

    captured = SimpleNamespace(triage_context=None, kwargs_seen=False)
    report = ProtocolReport(wallet=FAKE_WALLET)

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
        return _FakeData()

    def _fake_analyze(wallet, **kwargs):
        captured.kwargs_seen = True
        captured.triage_context = kwargs.get("triage_context")
        return report

    monkeypatch.setattr(rwp, "fetch_wallet_data", _fake_fetch)
    monkeypatch.setattr(rwp, "analyze_wallet", _fake_analyze)
    monkeypatch.setattr(
        rwp, "maybe_emit_memo",
        lambda r, persist_memo=False: SimpleNamespace(
            status="skipped_missing_env", message=""
        ),
    )

    import sqlite3 as _sqlite3

    class _FakeConn:
        def close(self):
            pass

        def execute(self, *a, **kw):
            class _C:
                def fetchall(self_):
                    return []
                def __iter__(self_):
                    return iter([])
            return _C()

    monkeypatch.setattr(_sqlite3, "connect", lambda *a, **kw: _FakeConn())

    return captured


def test_cli_passes_parsed_dict_to_analyze_wallet(monkeypatch):
    import scripts.run_wallet_protocol as rwp

    captured = _stub_cli(monkeypatch)
    rc = rwp.main([
        FAKE_WALLET,
        "--triage-context-json",
        '{"source": "lb", "period": "MONTH"}',
    ])
    assert rc == 0, f"main returned non-zero: {rc}"
    assert captured.kwargs_seen, "analyze_wallet was never called"
    assert captured.triage_context == {"source": "lb", "period": "MONTH"}


def test_cli_no_triage_context_kwarg_when_flag_absent(monkeypatch):
    import scripts.run_wallet_protocol as rwp

    captured = _stub_cli(monkeypatch)
    rc = rwp.main([FAKE_WALLET])
    assert rc == 0, f"main returned non-zero: {rc}"
    assert captured.triage_context is None


def test_cli_rejects_malformed_json_with_exit_2(monkeypatch, capsys):
    """Malformed JSON exits 2 before fetching, with a stderr error
    mentioning the flag name."""
    import scripts.run_wallet_protocol as rwp

    # Stub fetch + analyze so a fall-through wouldn't accidentally succeed.
    captured = _stub_cli(monkeypatch)
    rc = rwp.main([FAKE_WALLET, "--triage-context-json", "not-json"])
    assert rc == 2, f"Expected exit 2 on malformed JSON, got: {rc}"
    stderr = capsys.readouterr().err
    assert "triage-context-json" in stderr, (
        f"Expected 'triage-context-json' in stderr error. Got: {stderr!r}"
    )
    assert not captured.kwargs_seen, (
        "analyze_wallet should not have been called on malformed JSON"
    )


# ===========================================================================
# §3.5  Replay round-trip
# ===========================================================================

def test_replay_args_json_records_triage_context_json_raw_string():
    from scripts.run_wallet_protocol import _build_parser, _replay_args_json
    args = _build_parser().parse_args(
        [FAKE_WALLET, "--triage-context-json", '{"a": 1}']
    )
    payload = json.loads(_replay_args_json(args))
    assert "triage_context_json" in payload, (
        f"'triage_context_json' missing from _replay_args_json. "
        f"Keys: {list(payload)}"
    )
    assert payload["triage_context_json"] == '{"a": 1}', (
        f"Expected raw JSON string preservation, got: "
        f"{payload['triage_context_json']!r}"
    )


def test_replay_argv_reconstructs_flag_when_set():
    from scripts.replay_wallet_run import _argv_from_args_json
    from scripts.run_wallet_protocol import _build_parser, _replay_args_json

    args = _build_parser().parse_args(
        [FAKE_WALLET, "--triage-context-json", '{"a": 1}']
    )
    args_json = _replay_args_json(args)
    argv = _argv_from_args_json(args_json, FAKE_WALLET)

    assert "--triage-context-json" in argv, (
        f"--triage-context-json missing from reconstructed argv: {argv}"
    )
    idx = argv.index("--triage-context-json")
    assert argv[idx + 1] == '{"a": 1}', (
        f"JSON string not preserved verbatim. Got: {argv[idx + 1]!r}"
    )
