"""G4 — Acceptance tests for `tag_lookup_state` on `data_completeness`.

Spec: docs/superpowers/specs/2026-05-17-g4-tag-lookup-state.md §3
Origin: docs/audits/2026-05-10-wallet-analyzer-live-sampling.md G4

Surfaces tag-lookup availability as a typed field so downstream consumers
can tell apart `--no-tags` from `lookup failed` from `lookup found nothing`.
Today the only signal is a stderr breadcrumb.

Acceptance coverage:
  §3.1  Field presence + type                — 2 tests
  §3.2  Explicit state passthrough            — valid states + invalid rejection
  §3.3  Inference from leafs_by_cid           — 3 tests
  §3.4  Flag not double-stamped               — 1 test

All tests must be RED until `analyze_wallet` accepts `tag_lookup_state` and
plumbs it into `data_completeness` + appends `TAG_LOOKUP_UNAVAILABLE` to
`data_quality_flags` when unavailable.
"""
from __future__ import annotations

import time

import pytest

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.types import WalletData


VALID_STATES = {"ok", "empty", "unavailable", "skipped"}


def _now() -> int:
    return int(time.time())


def _pos(
    cid: str = "cid_a",
    realizedPnl: float = 50.0,
    ts: int | None = None,
) -> dict:
    return {
        "conditionId": cid,
        "outcome": "Yes",
        "curPrice": 1.0,
        "avgPrice": 0.50,
        "size": 100,
        "realizedPnl": realizedPnl,
        "timestamp": ts if ts is not None else _now(),
        "eventSlug": f"slug-{cid}",
    }


def _wallet_data(closed_positions=None) -> WalletData:
    return WalletData(
        wallet="0xabc",
        closed_positions=list(closed_positions or [_pos()] * 3),
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
        fetch_termination=None,
    )


def _run(*, tag_lookup_state=None, leafs_by_cid=None, nav_lookup=None):
    data = _wallet_data()
    return analyze_wallet(
        "0xabc",
        pre_fetched_data=data,
        n_iter=50,
        seed=1,
        tag_lookup_state=tag_lookup_state,
        leafs_by_cid=leafs_by_cid,
        nav_lookup=nav_lookup,
    )


# ===========================================================================
# §3.1  Field presence + type
# ===========================================================================

def test_tag_lookup_state_key_always_present():
    report = _run()
    dc = report.data_completeness
    assert "tag_lookup_state" in dc, (
        f"tag_lookup_state missing. Keys: {sorted(dc.keys())}"
    )
    assert dc["tag_lookup_state"] in VALID_STATES, (
        f"tag_lookup_state must be one of {VALID_STATES}, got {dc['tag_lookup_state']!r}"
    )


def test_tag_lookup_state_is_string_not_none():
    """Default — when no state given and no leafs_by_cid given — must be
    a string from VALID_STATES, NOT None. Conservative interpretation:
    treat absence-of-info as 'unavailable'."""
    report = _run()
    dc = report.data_completeness
    assert dc["tag_lookup_state"] is not None
    assert isinstance(dc["tag_lookup_state"], str)
    assert dc["tag_lookup_state"] == "unavailable"


# ===========================================================================
# §3.2  Explicit state passthrough
# ===========================================================================

def test_explicit_state_ok():
    report = _run(tag_lookup_state="ok", leafs_by_cid={"cid_a": ["politics"]})
    assert report.data_completeness["tag_lookup_state"] == "ok"
    assert "TAG_LOOKUP_UNAVAILABLE" not in report.data_quality_flags


def test_explicit_state_empty():
    report = _run(tag_lookup_state="empty", leafs_by_cid={})
    assert report.data_completeness["tag_lookup_state"] == "empty"
    assert "TAG_LOOKUP_UNAVAILABLE" not in report.data_quality_flags


def test_explicit_state_unavailable():
    report = _run(tag_lookup_state="unavailable", leafs_by_cid=None)
    assert report.data_completeness["tag_lookup_state"] == "unavailable"
    count = report.data_quality_flags.count("TAG_LOOKUP_UNAVAILABLE")
    assert count == 1, (
        f"Expected exactly 1 TAG_LOOKUP_UNAVAILABLE flag, got {count}. "
        f"Flags: {report.data_quality_flags}"
    )


def test_explicit_state_skipped():
    """Operator passed --no-tags: distinct from 'unavailable'. No flag."""
    report = _run(tag_lookup_state="skipped", leafs_by_cid=None)
    assert report.data_completeness["tag_lookup_state"] == "skipped"
    assert "TAG_LOOKUP_UNAVAILABLE" not in report.data_quality_flags


def test_invalid_explicit_state_rejected():
    with pytest.raises(ValueError, match="tag_lookup_state"):
        _run(tag_lookup_state="not_a_real_state")


# ===========================================================================
# §3.3  Inference from leafs_by_cid when state not explicit
# ===========================================================================

def test_inferred_unavailable_when_leafs_by_cid_none():
    """No explicit state, no tag data → 'unavailable' + flag."""
    report = _run(tag_lookup_state=None, leafs_by_cid=None)
    assert report.data_completeness["tag_lookup_state"] == "unavailable"
    assert "TAG_LOOKUP_UNAVAILABLE" in report.data_quality_flags


def test_inferred_empty_when_leafs_by_cid_empty_dict():
    """Empty dict ≠ None. Lookup ran but returned no rows for any cid."""
    report = _run(tag_lookup_state=None, leafs_by_cid={})
    assert report.data_completeness["tag_lookup_state"] == "empty"
    assert "TAG_LOOKUP_UNAVAILABLE" not in report.data_quality_flags


def test_inferred_ok_when_leafs_by_cid_populated():
    report = _run(tag_lookup_state=None, leafs_by_cid={"cid_a": ["politics"]})
    assert report.data_completeness["tag_lookup_state"] == "ok"
    assert "TAG_LOOKUP_UNAVAILABLE" not in report.data_quality_flags


# ===========================================================================
# §3.4  Flag not double-stamped
# ===========================================================================

def test_unavailable_flag_appended_exactly_once():
    """Defensive guard: a single analyze_wallet call appends the flag
    once, not multiple times. Pins the contract even though analyze_wallet
    builds a fresh report per call."""
    report = _run(tag_lookup_state="unavailable")
    count = report.data_quality_flags.count("TAG_LOOKUP_UNAVAILABLE")
    assert count == 1, (
        f"Flag must appear exactly once. Got {count}. "
        f"Flags: {report.data_quality_flags}"
    )


# ===========================================================================
# CLI integration — `scripts/run_wallet_protocol.py` threading
# Closes reviewer-flagged gap (G4 review 2026-05-17): CLI-side state
# tracking was only covered by integration runs.
# ===========================================================================

from types import SimpleNamespace as _SN


def _stub_cli(monkeypatch, *, report_holder):
    """Patch fetch + analyze + sqlite3 so main() can run without I/O.
    Records the tag_lookup_state passed into analyze_wallet."""
    import scripts.run_wallet_protocol as rwp

    captured = _SN(tag_lookup_state=None)

    class _FakeData:
        wallet = "0xdeadbeef"
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
        captured.tag_lookup_state = kwargs.get("tag_lookup_state")
        return report_holder[0]

    monkeypatch.setattr(rwp, "fetch_wallet_data", _fake_fetch)
    monkeypatch.setattr(rwp, "analyze_wallet", _fake_analyze)
    monkeypatch.setattr(
        rwp, "maybe_emit_memo",
        lambda r, persist_memo=False: _SN(status="skipped_missing_env", message=""),
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


def test_cli_passes_skipped_when_no_tags_flag(monkeypatch):
    """--no-tags → CLI passes 'skipped' into analyze_wallet, NOT 'unavailable'."""
    import scripts.run_wallet_protocol as rwp

    holder = [analyze_wallet(  # cheap pre-built report
        "0xdeadbeef", pre_fetched_data=_wallet_data(), n_iter=10, seed=1,
    )]
    captured = _stub_cli(monkeypatch, report_holder=holder)

    rc = rwp.main(["0xdeadbeef", "--no-tags"])
    assert rc == 0
    assert captured.tag_lookup_state == "skipped", (
        f"Expected 'skipped' for --no-tags, got: {captured.tag_lookup_state!r}"
    )


def test_cli_passes_unavailable_when_tag_lookup_raises(monkeypatch):
    """Live branch with no --no-tags + tag-lookup exception → 'unavailable'."""
    import scripts.run_wallet_protocol as rwp

    def _explode(conn, cids):
        raise RuntimeError("market_tags table missing (simulated)")

    monkeypatch.setattr(rwp, "fetch_tag_lookups_from_db", _explode)

    holder = [analyze_wallet(
        "0xdeadbeef", pre_fetched_data=_wallet_data(), n_iter=10, seed=1,
    )]
    captured = _stub_cli(monkeypatch, report_holder=holder)

    rc = rwp.main(["0xdeadbeef"])
    assert rc == 0
    assert captured.tag_lookup_state == "unavailable", (
        f"Expected 'unavailable' on tag-lookup exception, got: "
        f"{captured.tag_lookup_state!r}"
    )
