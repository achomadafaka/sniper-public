import sqlite3

from sniper.persistence import db_evm_stream as stream
from scripts.research.ink_shadow_report import build_report


def test_shadow_report_keeps_missing_coverage_unknown_and_does_not_write(tmp_path):
    path = tmp_path / "stream.sqlite"
    with sqlite3.connect(path) as setup:
        stream.ensure_stream_tables(setup)
        stream.record_gap(setup, 100, "sequence_gap", end_block_number=102)
        stream.record_canonical_block(setup, 100, "0x" + "a" * 64)
    manifest = {"chain_id": 57073, "funded_activation": "blocked", "venues": []}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    report = build_report(conn, manifest)
    conn.close()
    assert report["funded_activation"] == "blocked"
    assert report["canonical_blocks"] == 1
    assert report["open_gaps"] == [{"from_block": 100, "through_block": 102,
                                    "reason": "sequence_gap"}]
    assert report["canonical_match_rate"] is None
    assert report["g2_shadow_ready"] is False
    with sqlite3.connect(path) as unchanged:
        assert stream.open_gaps(unchanged) == [{"block_number": 100,
                                               "reason": "sequence_gap"}]
