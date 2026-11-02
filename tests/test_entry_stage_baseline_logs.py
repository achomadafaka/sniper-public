import gzip
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('entry_baseline', Path(__file__).parents[1] / 'scripts/research/entry_stage_baseline.py')
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)


def test_rotated_logs_are_read_once_and_in_time_order(tmp_path, monkeypatch):
    log = tmp_path / 'memecoin_runner.log'
    old = '2026-09-26T00:00:00Z slot lane old\n'
    new = '2026-10-02T00:00:00Z slot lane new\n'
    log.write_text(new)
    Path(str(log) + '.1').write_text(old + new)
    with gzip.open(str(log) + '.2.gz', 'wt') as f:
        f.write(old)
    monkeypatch.setattr(B, 'LOG', str(log))
    # Existing implementation reads only the current file.
    lines = list(B.log_lines()) if hasattr(B, 'log_lines') else log.read_text().splitlines(True)
    assert lines == [old, new]


def test_slot_coverage_uses_current_rows_not_static_gap_list():
    rows = [{"source_slot": 12, "landing_slot": 13, "recv_slot": 12}, {}]
    assert B.slot_coverage(rows) == {"attempts": 2, "source_slot_missing": 1,
                                     "landing_slot_missing": 1, "recv_slot_missing": 1}
