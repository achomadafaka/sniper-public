"""Tests for scripts/execution_profile_shortlist.py — collection + summary."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.execution_profile_shortlist import (
    collect_shortlist,
    run,
    summarise,
    write_manifest,
)


def _seed_db(path: Path, *, followed: list[str], watchlist: list[str]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE followed_wallets "
        "(wallet TEXT PRIMARY KEY, label TEXT DEFAULT '', added_at TEXT, status TEXT)"
    )
    conn.execute(
        "CREATE TABLE watchlist_wallets "
        "(wallet TEXT PRIMARY KEY, label TEXT DEFAULT '', added_ts INTEGER DEFAULT 0, "
        "last_synced_ts INTEGER DEFAULT 0, updated_at TEXT)"
    )
    for w in followed:
        conn.execute("INSERT INTO followed_wallets (wallet) VALUES (?)", (w,))
    for w in watchlist:
        conn.execute(
            "INSERT INTO watchlist_wallets (wallet, added_ts) VALUES (?, 0)", (w,)
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# collect_shortlist
# ---------------------------------------------------------------------------


def test_collect_shortlist_unions_and_dedupes(tmp_path):
    db = tmp_path / "t.db"
    _seed_db(
        db,
        followed=["0x" + "a" * 40, "0x" + "b" * 40],
        watchlist=["0x" + "B" * 40, "0x" + "c" * 40],  # dup 'B' in uppercase
    )
    out = collect_shortlist(str(db))
    assert out == sorted({"0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40})


def test_collect_shortlist_rejects_malformed(tmp_path):
    db = tmp_path / "t.db"
    _seed_db(
        db,
        followed=["0xtest123", "", "not-an-address", "0x" + "a" * 40],
        watchlist=[],
    )
    out = collect_shortlist(str(db))
    assert out == ["0x" + "a" * 40]


def test_collect_shortlist_tolerates_missing_tables(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    # Only create followed_wallets — watchlist is missing
    conn.execute("CREATE TABLE followed_wallets (wallet TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO followed_wallets VALUES ('0x" + "a" * 40 + "')")
    conn.commit()
    conn.close()
    out = collect_shortlist(str(db))
    assert out == ["0x" + "a" * 40]


# ---------------------------------------------------------------------------
# summarise
# ---------------------------------------------------------------------------


def test_summarise_populated_envelope():
    envelope = {
        "wallet": "0xabc",
        "classification": {"archetype": "directional_discretionary"},
        "execution_profile": {
            "status": "populated",
            "confidence_tier": "medium",
            "drift_1h": {"n": 30, "mean": -0.05, "median": -0.04},
            "skew_vs_preceding_bar": {"n": 25, "median": 0.0},
        },
        "edge_hypotheses": {
            "entry_timing_edge": {"level": "low"},
        },
    }
    s = summarise(envelope)
    assert s["classification"] == "directional_discretionary"
    assert s["status"] == "populated"
    assert s["tier"] == "medium"
    assert s["n_skew"] == 25
    assert s["n_drift_1h"] == 30
    assert s["entry_timing_level"] == "low"


def test_summarise_stub_envelope():
    envelope = {
        "wallet": "0xabc",
        "classification": {"archetype": "paired_outcome_arb"},
        "execution_profile": {"status": "not_run", "reason": "layer1_terminal"},
        "edge_hypotheses": {"entry_timing_edge": {"level": "unknown"}},
    }
    s = summarise(envelope)
    assert s["status"] == "not_run"
    assert s["reason"] == "layer1_terminal"
    assert s["n_skew"] is None
    assert s["entry_timing_level"] == "unknown"


# ---------------------------------------------------------------------------
# run — integration with mocked extractor
# ---------------------------------------------------------------------------


def test_run_writes_one_json_per_wallet_and_manifest(tmp_path):
    wallets = ["0x" + "a" * 40, "0x" + "b" * 40]
    out = tmp_path / "profiles"

    def fake_extract(wallet, skip_gate0=False, with_execution=False, force_execution=False):
        return {
            "wallet": wallet,
            "classification": {"archetype": "directional_discretionary"},
            "execution_profile": {
                "status": "populated",
                "confidence_tier": "medium",
                "drift_1h": {"n": 30, "mean": -0.05, "median": -0.04},
                "skew_vs_preceding_bar": {"n": 25, "median": 0.0},
            },
            "edge_hypotheses": {"entry_timing_edge": {"level": "low"}},
        }

    with patch("scripts.execution_profile_shortlist.extract_wallet_archetype", fake_extract):
        summaries = run(wallets, out_dir=out, force=False, skip_gate0=True)

    assert len(summaries) == 2
    for w in wallets:
        p = out / f"{w}.json"
        assert p.exists()
        envelope = json.loads(p.read_text())
        assert envelope["wallet"] == w

    manifest_path = write_manifest(summaries, out)
    manifest = json.loads(manifest_path.read_text())
    assert manifest["n_wallets"] == 2
    assert len(manifest["summaries"]) == 2


def test_run_records_failure_per_wallet(tmp_path):
    wallets = ["0x" + "a" * 40, "0x" + "b" * 40]
    out = tmp_path / "profiles"

    def fake_extract(wallet, **_):
        if wallet.endswith("a" * 40):
            raise RuntimeError("simulated fetch error")
        return {
            "wallet": wallet,
            "classification": {"archetype": "mixed_unresolved"},
            "execution_profile": {"status": "not_run", "reason": "gate"},
            "edge_hypotheses": {"entry_timing_edge": {"level": "unknown"}},
        }

    with patch("scripts.execution_profile_shortlist.extract_wallet_archetype", fake_extract):
        summaries = run(wallets, out_dir=out, force=False, skip_gate0=True)

    assert len(summaries) == 2
    # Failing wallet: no JSON on disk, summary records runner_error
    assert not (out / (wallets[0] + ".json")).exists()
    assert summaries[0]["status"] == "runner_error"
    assert "simulated fetch error" in summaries[0]["reason"]
    # Successful wallet: JSON written
    assert (out / (wallets[1] + ".json")).exists()
    assert summaries[1]["status"] == "not_run"
