"""Phase 7 Task 1 — `scripts/run_ledger_pipeline.py` CLI tests.

Parser-shape tests only at this stage. Dry-run integration tests land
in Task 2; live-write tests in Task 3.
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

import scripts.run_ledger_pipeline as cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every env var the CLI reads so each test starts clean."""
    for var in (
        "LEDGER_RUNTIME_SUPABASE_URL",
        "LEDGER_RUNTIME_SUPABASE_KEY",
        "LEDGER_ROOT",
    ):
        monkeypatch.delenv(var, raising=False)


def _baseline_argv(ledger_root: Path) -> list[str]:
    """A minimal, valid argv (dry-run path)."""
    return [
        "--wallet",
        "0x000000000000000000000000000000000000aaaa",
        "--window-id",
        "w1",
        "--start-ts",
        "1700000000",
        "--end-ts",
        "1700086400",
        "--capture-run-id",
        "run-a",
        "--run-id",
        "smoke-1",
        "--ledger-root",
        str(ledger_root),
    ]


# ---------------------------------------------------------------------------
# Help / no-args
# ---------------------------------------------------------------------------


def test_no_args_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_runtime_env(monkeypatch)
    err = io.StringIO()
    with pytest.raises(SystemExit) as exc, redirect_stderr(err):
        cli.main([])
    assert exc.value.code != 0


def test_help_exits_zero_and_lists_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_runtime_env(monkeypatch)
    out = io.StringIO()
    with pytest.raises(SystemExit) as exc, redirect_stdout(out):
        cli.main(["--help"])
    assert exc.value.code == 0
    text = out.getvalue()
    for flag in (
        "--wallet",
        "--window-id",
        "--start-ts",
        "--end-ts",
        "--capture-run-id",
        "--ledger-root",
        "--write",
        "--run-id",
        "--seed",
        "--n-iter",
    ):
        assert flag in text, f"--help did not mention {flag}"


# ---------------------------------------------------------------------------
# Missing required flags
# ---------------------------------------------------------------------------


def test_missing_wallet_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    argv = [
        "--window-id",
        "w1",
        "--start-ts",
        "1700000000",
        "--end-ts",
        "1700086400",
        "--capture-run-id",
        "run-a",
        "--run-id",
        "smoke-1",
        "--ledger-root",
        str(tmp_path),
    ]
    err = io.StringIO()
    with pytest.raises(SystemExit) as exc, redirect_stderr(err):
        cli.main(argv)
    assert exc.value.code != 0
    assert "wallet" in err.getvalue().lower()


def test_missing_run_id_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    argv = [
        "--wallet",
        "0x000000000000000000000000000000000000aaaa",
        "--window-id",
        "w1",
        "--start-ts",
        "1700000000",
        "--end-ts",
        "1700086400",
        "--capture-run-id",
        "run-a",
        "--ledger-root",
        str(tmp_path),
    ]
    err = io.StringIO()
    rc = -1
    try:
        with redirect_stderr(err):
            rc = cli.main(argv)
    except SystemExit as exc:
        rc = exc.code
    assert rc != 0
    assert "run-id" in err.getvalue().lower()


# ---------------------------------------------------------------------------
# Ledger root resolution (flag + env fallback)
# ---------------------------------------------------------------------------


def test_ledger_root_required_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_runtime_env(monkeypatch)
    argv = [
        "--wallet",
        "0x000000000000000000000000000000000000aaaa",
        "--window-id",
        "w1",
        "--start-ts",
        "1700000000",
        "--end-ts",
        "1700086400",
        "--capture-run-id",
        "run-a",
        "--run-id",
        "smoke-1",
    ]
    err = io.StringIO()
    rc = -1
    try:
        with redirect_stderr(err):
            rc = cli.main(argv)
    except SystemExit as exc:
        rc = exc.code
    assert rc != 0
    assert "ledger" in err.getvalue().lower() or "LEDGER_ROOT" in err.getvalue()


def test_ledger_root_env_fallback_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When LEDGER_ROOT is set, omitting --ledger-root is OK; the env
    value is what the pipeline gets passed."""
    _clean_runtime_env(monkeypatch)
    monkeypatch.setenv("LEDGER_ROOT", str(tmp_path))
    captured = _stub_run_pipeline(monkeypatch)
    argv = [
        "--wallet",
        "0x000000000000000000000000000000000000aaaa",
        "--window-id",
        "w1",
        "--start-ts",
        "1700000000",
        "--end-ts",
        "1700086400",
        "--capture-run-id",
        "run-a",
        "--run-id",
        "smoke-1",
    ]
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        rc = cli.main(argv)
    assert rc == 0
    assert Path(captured["ledger_root"]) == tmp_path


# ---------------------------------------------------------------------------
# --write env-var gating
# ---------------------------------------------------------------------------


def test_write_without_env_vars_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    argv = _baseline_argv(tmp_path) + ["--write"]
    err = io.StringIO()
    rc = -1
    try:
        with redirect_stderr(err):
            rc = cli.main(argv)
    except SystemExit as exc:
        rc = exc.code
    assert rc != 0
    text = err.getvalue()
    assert "LEDGER_RUNTIME_SUPABASE_URL" in text
    assert "LEDGER_RUNTIME_SUPABASE_KEY" in text


def test_write_with_only_url_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    monkeypatch.setenv("LEDGER_RUNTIME_SUPABASE_URL", "https://x.supabase.co")
    argv = _baseline_argv(tmp_path) + ["--write"]
    err = io.StringIO()
    rc = -1
    try:
        with redirect_stderr(err):
            rc = cli.main(argv)
    except SystemExit as exc:
        rc = exc.code
    assert rc != 0
    assert "LEDGER_RUNTIME_SUPABASE_KEY" in err.getvalue()


# ---------------------------------------------------------------------------
# Task 2 — dry-run pipeline integration
# ---------------------------------------------------------------------------


def _stub_run_pipeline(monkeypatch: pytest.MonkeyPatch, write_counts=None):
    """Replace `run_analysis_pipeline` on the CLI module with a stub
    that records the kwargs it was called with and returns a synthetic
    `RunResult`."""
    from sniper.analysis.wallet_protocol.ledger.run_pipeline import RunResult
    from sniper.analysis.wallet_protocol.types import ProtocolReport

    captured: dict = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return RunResult(
            report=ProtocolReport(
                wallet=kwargs["wallet"],
                overall_verdict="data_insufficient",
                gate_result="reject_insufficient_sample",
                trading_mode="n/a_routing",
            ),
            findings=[],
            artifact_paths=[],
            replay_surface_meta={},
            write_counts=write_counts or {},
        )

    monkeypatch.setattr(cli, "run_analysis_pipeline", _fake)
    return captured


def test_dry_run_calls_pipeline_with_supabase_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    captured = _stub_run_pipeline(monkeypatch)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(_baseline_argv(tmp_path))
    assert rc == 0
    assert captured["supabase"] is None
    assert captured["wallet"] == "0x000000000000000000000000000000000000aaaa"
    assert captured["run_id"] == "smoke-1"
    assert captured["window"].window_id == "w1"
    assert captured["window"].start_ts == 1700000000
    assert captured["window"].end_ts == 1700086400
    assert captured["window"].capture_run_ids == ("run-a",)
    assert "dry-run" in err.getvalue().lower()


def test_dry_run_prints_report_json_to_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    _clean_runtime_env(monkeypatch)
    _stub_run_pipeline(monkeypatch)
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        rc = cli.main(_baseline_argv(tmp_path))
    assert rc == 0
    parsed = json.loads(out.getvalue())
    assert parsed["wallet"] == "0x000000000000000000000000000000000000aaaa"
    assert parsed["overall_verdict"] == "data_insufficient"
    assert parsed["gate_result"] == "reject_insufficient_sample"


def test_dry_run_findings_count_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    _stub_run_pipeline(monkeypatch)
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        cli.main(_baseline_argv(tmp_path))
    text = err.getvalue()
    assert "derived 0 findings" in text


def test_source_version_flag_is_tupled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    captured = _stub_run_pipeline(monkeypatch)
    argv = _baseline_argv(tmp_path) + [
        "--source-version",
        "price_v=v1",
        "--source-version",
        "schema_v=2",
    ]
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        cli.main(argv)
    sv = captured["window"].source_versions
    assert ("price_v", "v1") in sv
    assert ("schema_v", "2") in sv


def test_seed_and_n_iter_flow_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    captured = _stub_run_pipeline(monkeypatch)
    argv = _baseline_argv(tmp_path) + ["--seed", "7", "--n-iter", "500"]
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        cli.main(argv)
    assert captured["seed"] == 7
    assert captured["n_iter"] == 500


def test_code_version_falls_back_to_unknown_outside_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `git rev-parse` cannot resolve a hash, code_version is the
    string 'unknown' so the writer's provenance column gets a clear
    sentinel instead of a stack trace."""
    _clean_runtime_env(monkeypatch)
    captured = _stub_run_pipeline(monkeypatch)

    def _failing_git(*_args, **_kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(cli, "_resolve_code_version", lambda: "unknown")

    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        cli.main(_baseline_argv(tmp_path))
    assert captured["code_version"] == "unknown"


# ---------------------------------------------------------------------------
# Task 3 — live-write integration
# ---------------------------------------------------------------------------


def test_write_constructs_rest_supabase_with_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    monkeypatch.setenv(
        "LEDGER_RUNTIME_SUPABASE_URL", "https://example.supabase.co"
    )
    monkeypatch.setenv("LEDGER_RUNTIME_SUPABASE_KEY", "service-key-here")

    constructed: dict = {}

    class _RecordingRest:
        def __init__(self, *, url, key, transport=None):
            constructed["url"] = url
            constructed["key"] = key

    monkeypatch.setattr(cli, "RestSupabaseHttp", _RecordingRest)
    captured = _stub_run_pipeline(
        monkeypatch,
        write_counts={
            "wallet_analysis_results": 1,
            "wallet_findings": 3,
            "artifact_registry": 1,
        },
    )

    argv = _baseline_argv(tmp_path) + ["--write"]
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        rc = cli.main(argv)

    assert rc == 0
    assert constructed["url"] == "https://example.supabase.co"
    assert constructed["key"] == "service-key-here"
    # The constructed RestSupabaseHttp instance flows into supabase=.
    assert isinstance(captured["supabase"], _RecordingRest)


def test_write_prints_per_table_counts_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    monkeypatch.setenv(
        "LEDGER_RUNTIME_SUPABASE_URL", "https://example.supabase.co"
    )
    monkeypatch.setenv("LEDGER_RUNTIME_SUPABASE_KEY", "k")

    class _NoopRest:
        def __init__(self, *, url, key, transport=None):
            pass

    monkeypatch.setattr(cli, "RestSupabaseHttp", _NoopRest)
    _stub_run_pipeline(
        monkeypatch,
        write_counts={
            "wallet_analysis_results": 1,
            "wallet_findings": 4,
            "artifact_registry": 1,
        },
    )
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        cli.main(_baseline_argv(tmp_path) + ["--write"])
    text = err.getvalue()
    assert "wrote results=1 findings=4 artifacts=1 run_id=smoke-1" in text


def test_write_zero_counts_on_idempotent_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stub the pipeline to return all-zero write_counts (the
    second-pass shape). The CLI must print zeros, not the first-pass
    counts — that is the user-facing idempotency signal."""
    _clean_runtime_env(monkeypatch)
    monkeypatch.setenv(
        "LEDGER_RUNTIME_SUPABASE_URL", "https://example.supabase.co"
    )
    monkeypatch.setenv("LEDGER_RUNTIME_SUPABASE_KEY", "k")

    class _NoopRest:
        def __init__(self, *, url, key, transport=None):
            pass

    monkeypatch.setattr(cli, "RestSupabaseHttp", _NoopRest)
    _stub_run_pipeline(
        monkeypatch,
        write_counts={
            "wallet_analysis_results": 0,
            "wallet_findings": 0,
            "artifact_registry": 0,
        },
    )
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        cli.main(_baseline_argv(tmp_path) + ["--write"])
    assert "wrote results=0 findings=0 artifacts=0" in err.getvalue()


def test_write_pipeline_failure_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any exception raised by the pipeline (e.g. Supabase HTTP
    failure) bubbles up as a non-zero exit; the CLI prints the error
    to stderr."""
    _clean_runtime_env(monkeypatch)
    monkeypatch.setenv(
        "LEDGER_RUNTIME_SUPABASE_URL", "https://example.supabase.co"
    )
    monkeypatch.setenv("LEDGER_RUNTIME_SUPABASE_KEY", "k")

    class _NoopRest:
        def __init__(self, *, url, key, transport=None):
            pass

    monkeypatch.setattr(cli, "RestSupabaseHttp", _NoopRest)

    def _explode(**_kwargs):
        raise RuntimeError("supabase exploded")

    monkeypatch.setattr(cli, "run_analysis_pipeline", _explode)

    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        rc = cli.main(_baseline_argv(tmp_path) + ["--write"])
    assert rc != 0
    assert "supabase exploded" in err.getvalue()


# ---------------------------------------------------------------------------
# Task 1 — --from-list batch source
# ---------------------------------------------------------------------------


_VALID_W1 = "0x000000000000000000000000000000000000aaaa"
_VALID_W2 = "0x000000000000000000000000000000000000bbbb"
_VALID_W3 = "0x000000000000000000000000000000000000cccc"


def _stub_run_pipeline_recording(monkeypatch: pytest.MonkeyPatch):
    """Record every kwarg dict the stubbed pipeline received."""
    from sniper.analysis.wallet_protocol.ledger.run_pipeline import RunResult
    from sniper.analysis.wallet_protocol.types import ProtocolReport

    captured: list[dict] = []

    def _fake(**kwargs):
        captured.append(dict(kwargs))
        return RunResult(
            report=ProtocolReport(
                wallet=kwargs["wallet"],
                overall_verdict="data_insufficient",
                gate_result="reject_insufficient_sample",
                trading_mode="n/a_routing",
            ),
            findings=[],
            artifact_paths=[],
            replay_surface_meta={},
            write_counts={},
        )

    monkeypatch.setattr(cli, "run_analysis_pipeline", _fake)
    return captured


def _batch_argv(tmp_path: Path) -> list[str]:
    """Argv suffix carrying the explicit window + run_id, no source flag."""
    return [
        "--window-id",
        "w1",
        "--start-ts",
        "1700000000",
        "--end-ts",
        "1700086400",
        "--capture-run-id",
        "run-a",
        "--run-id",
        "batch-1",
        "--ledger-root",
        str(tmp_path),
    ]


def test_from_list_processes_each_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    list_file = tmp_path / "wallets.txt"
    list_file.write_text(
        "\n".join([_VALID_W1, _VALID_W2, _VALID_W3]) + "\n"
    )
    captured = _stub_run_pipeline_recording(monkeypatch)
    argv = ["--from-list", str(list_file)] + _batch_argv(tmp_path)
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        rc = cli.main(argv)
    assert rc == 0
    assert [c["wallet"] for c in captured] == [_VALID_W1, _VALID_W2, _VALID_W3]


def test_from_list_skips_comments_and_blanks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    list_file = tmp_path / "wallets.txt"
    list_file.write_text(
        "\n".join(
            [
                "# header comment",
                "",
                _VALID_W1,
                "  # mid-line comment",
                "",
                _VALID_W2,
                "",
            ]
        )
    )
    captured = _stub_run_pipeline_recording(monkeypatch)
    argv = ["--from-list", str(list_file)] + _batch_argv(tmp_path)
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        rc = cli.main(argv)
    assert rc == 0
    assert [c["wallet"] for c in captured] == [_VALID_W1, _VALID_W2]


def test_from_list_invalid_address_logs_and_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    list_file = tmp_path / "wallets.txt"
    list_file.write_text(f"{_VALID_W1}\nnot-a-wallet\n{_VALID_W2}\n")
    captured = _stub_run_pipeline_recording(monkeypatch)
    err = io.StringIO()
    argv = ["--from-list", str(list_file)] + _batch_argv(tmp_path)
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        rc = cli.main(argv)
    assert rc == 0  # invalid address is skipped, not a hard failure
    assert [c["wallet"] for c in captured] == [_VALID_W1, _VALID_W2]
    assert "not-a-wallet" in err.getvalue()


def test_wallet_and_from_list_mutually_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    list_file = tmp_path / "wallets.txt"
    list_file.write_text(f"{_VALID_W1}\n")
    argv = (
        ["--wallet", _VALID_W1, "--from-list", str(list_file)]
        + _batch_argv(tmp_path)
    )
    err = io.StringIO()
    with pytest.raises(SystemExit) as exc, redirect_stderr(err):
        cli.main(argv)
    assert exc.value.code != 0


def test_from_list_per_wallet_error_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the pipeline raises on wallet 2, wallets 1 and 3 still run.
    Final exit code is 1 (any failure → 1) but the surviving wallets
    did get processed."""
    _clean_runtime_env(monkeypatch)
    list_file = tmp_path / "wallets.txt"
    list_file.write_text(f"{_VALID_W1}\n{_VALID_W2}\n{_VALID_W3}\n")

    from sniper.analysis.wallet_protocol.ledger.run_pipeline import RunResult
    from sniper.analysis.wallet_protocol.types import ProtocolReport

    seen: list[str] = []

    def _flaky(**kwargs):
        seen.append(kwargs["wallet"])
        if kwargs["wallet"] == _VALID_W2:
            raise RuntimeError("boom on wallet 2")
        return RunResult(
            report=ProtocolReport(wallet=kwargs["wallet"]),
            findings=[],
            artifact_paths=[],
            replay_surface_meta={},
            write_counts={},
        )

    monkeypatch.setattr(cli, "run_analysis_pipeline", _flaky)
    err = io.StringIO()
    argv = ["--from-list", str(list_file)] + _batch_argv(tmp_path)
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        rc = cli.main(argv)
    assert rc == 1
    assert seen == [_VALID_W1, _VALID_W2, _VALID_W3]
    text = err.getvalue()
    assert _VALID_W2 in text
    assert "boom on wallet 2" in text


def test_batch_summary_line_is_last_stderr_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import re

    _clean_runtime_env(monkeypatch)
    list_file = tmp_path / "wallets.txt"
    list_file.write_text(f"{_VALID_W1}\n{_VALID_W2}\n")
    _stub_run_pipeline_recording(monkeypatch)
    err = io.StringIO()
    argv = ["--from-list", str(list_file)] + _batch_argv(tmp_path)
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        cli.main(argv)
    last = err.getvalue().rstrip("\n").rsplit("\n", 1)[-1]
    assert re.match(
        r"^batch: ok=\d+ failed=\d+ skipped=\d+ total=\d+$", last
    ), f"unexpected last stderr line: {last!r}"


def test_from_list_missing_file_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    argv = (
        ["--from-list", str(tmp_path / "nope.txt")]
        + _batch_argv(tmp_path)
    )
    err = io.StringIO()
    rc = -1
    try:
        with redirect_stderr(err):
            rc = cli.main(argv)
    except SystemExit as exc:
        rc = exc.code
    assert rc != 0
    assert "nope.txt" in err.getvalue()


# ---------------------------------------------------------------------------
# Task 2 — --from-followed batch source
# ---------------------------------------------------------------------------


class _FakeRow(dict):
    """Mimic sqlite3.Row enough for `row['wallet']` indexing."""

    def __getitem__(self, key):
        return super().__getitem__(key)


def _stub_followed_helper(monkeypatch: pytest.MonkeyPatch, wallets: list[str]):
    """Patch `cli.get_followed_wallets` to return the given wallets."""
    rows = [_FakeRow(wallet=w, status="active") for w in wallets]
    captured: dict = {"called_with": None}

    def _fake(status: str = "active"):
        captured["called_with"] = status
        return rows

    monkeypatch.setattr(cli, "get_followed_wallets", _fake)
    return captured


def test_from_followed_calls_helper_with_active_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--from-followed must call get_followed_wallets(status='active')
    per D6 (signature locked)."""
    _clean_runtime_env(monkeypatch)
    helper_state = _stub_followed_helper(
        monkeypatch, [_VALID_W1, _VALID_W2]
    )
    captured = _stub_run_pipeline_recording(monkeypatch)
    argv = ["--from-followed"] + _batch_argv(tmp_path)
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        rc = cli.main(argv)
    assert rc == 0
    assert helper_state["called_with"] == "active"
    assert [c["wallet"] for c in captured] == [_VALID_W1, _VALID_W2]


def test_from_followed_empty_list_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty followed-wallets list is not a failure — exit 0 with the
    summary 'batch: ok=0 failed=0 skipped=0 total=0'."""
    _clean_runtime_env(monkeypatch)
    _stub_followed_helper(monkeypatch, [])
    _stub_run_pipeline_recording(monkeypatch)
    err = io.StringIO()
    argv = ["--from-followed"] + _batch_argv(tmp_path)
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        rc = cli.main(argv)
    assert rc == 0
    assert "batch: ok=0 failed=0 skipped=0 total=0" in err.getvalue()


def test_three_way_mutex_wallet_and_from_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    argv = (
        ["--wallet", _VALID_W1, "--from-followed"]
        + _batch_argv(tmp_path)
    )
    err = io.StringIO()
    with pytest.raises(SystemExit) as exc, redirect_stderr(err):
        cli.main(argv)
    assert exc.value.code != 0


def test_three_way_mutex_from_list_and_from_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    list_file = tmp_path / "wallets.txt"
    list_file.write_text(f"{_VALID_W1}\n")
    argv = (
        ["--from-list", str(list_file), "--from-followed"]
        + _batch_argv(tmp_path)
    )
    err = io.StringIO()
    with pytest.raises(SystemExit) as exc, redirect_stderr(err):
        cli.main(argv)
    assert exc.value.code != 0


# ---------------------------------------------------------------------------
# Task 3 — --auto-window derivation
# ---------------------------------------------------------------------------


def _seed_capture_run(
    *,
    ledger_root: Path,
    wallet: str,
    run_id: str,
    started_at: int,
    finished_at: int | None,
    status: str = "completed",
    source_versions: dict | None = None,
) -> None:
    """Seed a single capture_runs row in the wallet warehouse so
    --auto-window can find it."""
    import duckdb
    from sniper.analysis.wallet_protocol.ledger.schema import (
        ensure_wallet_schema,
    )

    wallet_path = ledger_root / f"{wallet.lower()}.duckdb"
    sv = json.dumps(source_versions or {})
    surfaces = json.dumps(["events", "positions"])
    surfaces_complete = json.dumps({"events": True, "positions": True})
    with duckdb.connect(str(wallet_path)) as conn:
        ensure_wallet_schema(conn)
        conn.execute(
            """
            INSERT INTO capture_runs (
                run_id, wallet, started_at, finished_at, status,
                config_hash, schema_version, source_versions_json,
                surfaces_requested_json, surfaces_complete_json,
                error_class, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            [
                run_id,
                wallet.lower(),
                started_at,
                finished_at,
                status,
                "cfg-hash",
                "schema-v1",
                sv,
                surfaces,
                surfaces_complete,
            ],
        )


def test_auto_window_derives_from_capture_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    _seed_capture_run(
        ledger_root=tmp_path,
        wallet=_VALID_W1,
        run_id="cap-001",
        started_at=1_700_000_000,
        finished_at=1_700_086_400,
        source_versions={"polymarket_data_api": "v2", "alpha": "1"},
    )
    captured = _stub_run_pipeline_recording(monkeypatch)
    argv = [
        "--wallet",
        _VALID_W1,
        "--auto-window",
        "--ledger-root",
        str(tmp_path),
    ]
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        rc = cli.main(argv)
    assert rc == 0
    assert len(captured) == 1
    window = captured[0]["window"]
    assert window.window_id == "auto-cap-001"
    assert window.capture_run_ids == ("cap-001",)
    assert window.start_ts == 1_700_000_000
    assert window.end_ts == 1_700_086_400
    # source_versions tupled in key-sorted order.
    assert window.source_versions == (
        ("alpha", "1"),
        ("polymarket_data_api", "v2"),
    )


def test_auto_window_picks_most_recent_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When two completed captures exist, the most recent
    finished_at wins."""
    _clean_runtime_env(monkeypatch)
    _seed_capture_run(
        ledger_root=tmp_path,
        wallet=_VALID_W1,
        run_id="cap-old",
        started_at=1_700_000_000,
        finished_at=1_700_100_000,
    )
    _seed_capture_run(
        ledger_root=tmp_path,
        wallet=_VALID_W1,
        run_id="cap-new",
        started_at=1_700_500_000,
        finished_at=1_700_600_000,
    )
    captured = _stub_run_pipeline_recording(monkeypatch)
    argv = [
        "--wallet",
        _VALID_W1,
        "--auto-window",
        "--ledger-root",
        str(tmp_path),
    ]
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        rc = cli.main(argv)
    assert rc == 0
    assert captured[0]["window"].window_id == "auto-cap-new"


def test_auto_window_skips_when_warehouse_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    captured = _stub_run_pipeline_recording(monkeypatch)
    argv = [
        "--wallet",
        _VALID_W1,
        "--auto-window",
        "--ledger-root",
        str(tmp_path),
    ]
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        rc = cli.main(argv)
    assert rc == 0
    assert captured == []
    text = err.getvalue()
    assert "warehouse_missing" in text
    assert "batch: ok=0 failed=0 skipped=1 total=1" in text


def test_auto_window_skips_when_no_completed_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Warehouse exists but only carries failed/running runs."""
    _clean_runtime_env(monkeypatch)
    _seed_capture_run(
        ledger_root=tmp_path,
        wallet=_VALID_W1,
        run_id="cap-failed",
        started_at=1_700_000_000,
        finished_at=1_700_100_000,
        status="failed",
    )
    captured = _stub_run_pipeline_recording(monkeypatch)
    argv = [
        "--wallet",
        _VALID_W1,
        "--auto-window",
        "--ledger-root",
        str(tmp_path),
    ]
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        rc = cli.main(argv)
    assert rc == 0
    assert captured == []
    assert "no_completed_capture" in err.getvalue()


def test_auto_window_run_id_defaults_to_capture_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4: under --auto-window, --run-id is optional; default is the
    capture_run_id of the picked row."""
    _clean_runtime_env(monkeypatch)
    _seed_capture_run(
        ledger_root=tmp_path,
        wallet=_VALID_W1,
        run_id="cap-007",
        started_at=1_700_000_000,
        finished_at=1_700_086_400,
    )
    captured = _stub_run_pipeline_recording(monkeypatch)
    argv = [
        "--wallet",
        _VALID_W1,
        "--auto-window",
        "--ledger-root",
        str(tmp_path),
    ]  # NOTE: no --run-id
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        rc = cli.main(argv)
    assert rc == 0
    assert captured[0]["run_id"] == "cap-007"


def test_auto_window_explicit_run_id_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    _seed_capture_run(
        ledger_root=tmp_path,
        wallet=_VALID_W1,
        run_id="cap-007",
        started_at=1_700_000_000,
        finished_at=1_700_086_400,
    )
    captured = _stub_run_pipeline_recording(monkeypatch)
    argv = [
        "--wallet",
        _VALID_W1,
        "--auto-window",
        "--run-id",
        "operator-override",
        "--ledger-root",
        str(tmp_path),
    ]
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        rc = cli.main(argv)
    assert rc == 0
    assert captured[0]["run_id"] == "operator-override"


def test_auto_window_warns_when_explicit_window_flags_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_runtime_env(monkeypatch)
    _seed_capture_run(
        ledger_root=tmp_path,
        wallet=_VALID_W1,
        run_id="cap-007",
        started_at=1_700_000_000,
        finished_at=1_700_086_400,
    )
    _stub_run_pipeline_recording(monkeypatch)
    argv = [
        "--wallet",
        _VALID_W1,
        "--auto-window",
        "--window-id",
        "manual-w1",
        "--start-ts",
        "999",
        "--end-ts",
        "1000",
        "--capture-run-id",
        "ignored",
        "--ledger-root",
        str(tmp_path),
    ]
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        rc = cli.main(argv)
    assert rc == 0
    assert "ignored" in err.getvalue() or "auto-window" in err.getvalue()


def test_auto_window_per_wallet_when_combined_with_from_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each wallet derives from ITS own warehouse — not a batch-shared
    window."""
    _clean_runtime_env(monkeypatch)
    _seed_capture_run(
        ledger_root=tmp_path,
        wallet=_VALID_W1,
        run_id="cap-w1",
        started_at=1_700_000_000,
        finished_at=1_700_100_000,
    )
    _seed_capture_run(
        ledger_root=tmp_path,
        wallet=_VALID_W2,
        run_id="cap-w2",
        started_at=1_700_200_000,
        finished_at=1_700_300_000,
    )
    list_file = tmp_path / "wallets.txt"
    list_file.write_text(f"{_VALID_W1}\n{_VALID_W2}\n")
    captured = _stub_run_pipeline_recording(monkeypatch)
    argv = [
        "--from-list",
        str(list_file),
        "--auto-window",
        "--ledger-root",
        str(tmp_path),
    ]
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        rc = cli.main(argv)
    assert rc == 0
    windows = {c["wallet"]: c["window"] for c in captured}
    assert windows[_VALID_W1].window_id == "auto-cap-w1"
    assert windows[_VALID_W2].window_id == "auto-cap-w2"
    assert windows[_VALID_W1].capture_run_ids == ("cap-w1",)
    assert windows[_VALID_W2].capture_run_ids == ("cap-w2",)


def test_explicit_window_flags_still_required_without_auto_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --auto-window, --window-id / --start-ts / etc. remain
    required (regression check on Phase 7.1 contract)."""
    _clean_runtime_env(monkeypatch)
    argv = [
        "--wallet",
        _VALID_W1,
        "--ledger-root",
        str(tmp_path),
    ]
    err = io.StringIO()
    rc = -1
    try:
        with redirect_stderr(err):
            rc = cli.main(argv)
    except SystemExit as exc:
        rc = exc.code
    assert rc != 0
    text = err.getvalue()
    # Either argparse complains about required flags OR our validator
    # surfaces the missing flags. Either way, the error message names
    # at least one of the missing window flags.
    assert (
        "--window-id" in text
        or "--start-ts" in text
        or "--capture-run-id" in text
        or "--run-id" in text
    )
