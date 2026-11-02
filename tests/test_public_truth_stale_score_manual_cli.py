import subprocess
import sys
from pathlib import Path


HEADER = (
    "signal_id,market_url,market_id,condition_id,token_id,outcome,rule_text_hash,"
    "resolution_source_url,source_lane,source_family,source_account_id,source_url,"
    "source_timestamp,fetch_timestamp,observed_source_value,rule_match_status,"
    "finality_status,confirmation_status,stake_usdc,modeled_cost_per_share,"
    "orderbook_file,notes\n"
)


def test_manual_scoring_cli_writes_lane_segmented_report(tmp_path: Path):
    signals = tmp_path / "signals.csv"
    book_dir = tmp_path / "books"
    book_dir.mkdir()
    out = tmp_path / "report.md"

    signals.write_text(
        HEADER
        + "s1,https://polymarket.com/event/x,m1,c1,t1,YES,sha256:abc,"
        "https://official.example/final,resolution_authoritative,sports_official,,"
        "https://official.example/final,2026-06-15T21:00:00Z,2026-06-15T21:00:10Z,"
        "home_won,exact_match,official_final,still_unconfirmed,20,0.005,"
        "orderbook_signal_001.json,\n",
        encoding="utf-8",
    )
    (book_dir / "orderbook_signal_001.json").write_text(
        '{"asks":[{"price":"0.70","size":"10"},{"price":"0.75","size":"40"}]}',
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/public_truth_stale_score_manual.py",
            "--signals",
            str(signals),
            "--books-dir",
            str(book_dir),
            "--out",
            str(out),
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    report = out.read_text(encoding="utf-8")
    assert "Lane A: resolution_authoritative" in report
    assert "s1" in report
    assert "entered |" in report


def test_manual_scoring_cli_rejects_below_edge_threshold(tmp_path: Path):
    signals = tmp_path / "signals.csv"
    book_dir = tmp_path / "books"
    book_dir.mkdir()
    out = tmp_path / "report.md"

    signals.write_text(
        HEADER
        + "s2,https://polymarket.com/event/y,m2,c2,t2,YES,sha256:def,"
        "https://official.example/final,resolution_authoritative,sports_official,,"
        "https://official.example/final,2026-06-15T22:00:00Z,2026-06-15T22:00:10Z,"
        "home_won,exact_match,official_final,still_unconfirmed,20,0.005,"
        "orderbook_signal_002.json,\n",
        encoding="utf-8",
    )
    (book_dir / "orderbook_signal_002.json").write_text(
        '{"asks":[{"price":"0.98","size":"100"}]}',
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/public_truth_stale_score_manual.py",
            "--signals",
            str(signals),
            "--books-dir",
            str(book_dir),
            "--out",
            str(out),
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    report = out.read_text(encoding="utf-8")
    assert "rejected | net_edge_below_threshold" in report


def test_manual_scoring_cli_rejects_insufficient_depth_without_crashing(tmp_path: Path):
    signals = tmp_path / "signals.csv"
    book_dir = tmp_path / "books"
    book_dir.mkdir()
    out = tmp_path / "report.md"

    signals.write_text(
        HEADER
        + "s3,https://polymarket.com/event/z,m3,c3,t3,YES,sha256:ghi,"
        "https://official.example/final,resolution_authoritative,sports_official,,"
        "https://official.example/final,2026-06-15T23:00:00Z,2026-06-15T23:00:10Z,"
        "home_won,exact_match,official_final,still_unconfirmed,20,0.005,"
        "orderbook_signal_003.json,\n",
        encoding="utf-8",
    )
    (book_dir / "orderbook_signal_003.json").write_text(
        '{"asks":[{"price":"0.70","size":"1"}]}',
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/public_truth_stale_score_manual.py",
            "--signals",
            str(signals),
            "--books-dir",
            str(book_dir),
            "--out",
            str(out),
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    report = out.read_text(encoding="utf-8")
    assert "rejected | insufficient_depth" in report


def test_manual_scoring_cli_rejects_lane_a_without_official_finality(tmp_path: Path):
    signals = tmp_path / "signals.csv"
    book_dir = tmp_path / "books"
    book_dir.mkdir()
    out = tmp_path / "report.md"

    signals.write_text(
        HEADER
        + "s4,https://polymarket.com/event/a,m4,c4,t4,YES,sha256:jkl,"
        "https://official.example/final,resolution_authoritative,sports_official,,"
        "https://official.example/final,2026-06-16T00:00:00Z,2026-06-16T00:00:10Z,"
        "home_won,exact_match,source_lead,still_unconfirmed,20,0.005,"
        "orderbook_signal_004.json,\n",
        encoding="utf-8",
    )
    (book_dir / "orderbook_signal_004.json").write_text(
        '{"asks":[{"price":"0.70","size":"100"}]}',
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/public_truth_stale_score_manual.py",
            "--signals",
            str(signals),
            "--books-dir",
            str(book_dir),
            "--out",
            str(out),
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    report = out.read_text(encoding="utf-8")
    assert "rejected | lane_a_requires_official_final" in report


def test_manual_scoring_cli_rejects_non_fixed_stake(tmp_path: Path):
    signals = tmp_path / "signals.csv"
    book_dir = tmp_path / "books"
    book_dir.mkdir()
    out = tmp_path / "report.md"

    signals.write_text(
        HEADER
        + "s5,https://polymarket.com/event/b,m5,c5,t5,YES,sha256:mno,"
        "https://official.example/final,resolution_authoritative,sports_official,,"
        "https://official.example/final,2026-06-16T01:00:00Z,2026-06-16T01:00:10Z,"
        "home_won,exact_match,official_final,still_unconfirmed,25,0.005,"
        "orderbook_signal_005.json,\n",
        encoding="utf-8",
    )
    (book_dir / "orderbook_signal_005.json").write_text(
        '{"asks":[{"price":"0.70","size":"100"}]}',
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/public_truth_stale_score_manual.py",
            "--signals",
            str(signals),
            "--books-dir",
            str(book_dir),
            "--out",
            str(out),
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    report = out.read_text(encoding="utf-8")
    assert "rejected | non_fixed_stake" in report
