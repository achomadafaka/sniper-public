import base64
import importlib.util
import hashlib
import json
from pathlib import Path
import zlib

import pytest

spec = importlib.util.spec_from_file_location("speed_replay", Path(__file__).parents[1] / "scripts/research/desk_judge_speed_replay.py")
R = importlib.util.module_from_spec(spec)


def load():
    spec.loader.exec_module(R)
    return R


def test_frozen_sheet_requires_stored_bytes_and_matching_hash():
    r = load()
    sheet = '{"k":"test","value":0}'
    data = {"sheet_zb64": base64.b64encode(zlib.compress(sheet.encode())).decode(),
            "sheet_sha256": hashlib.sha256(sheet.encode()).hexdigest()}
    assert r.exact_sheet(data) == sheet
    with pytest.raises(ValueError):
        r.exact_sheet({**data, "sheet_sha256": "bad"})
    with pytest.raises(ValueError):
        r.exact_sheet({"asof": {}})


def test_balanced_arm_order_and_missing_or_invalid_verdict_stays_void():
    r = load()
    orders = [r.arm_order(i) for i in range(4)]
    assert len(set(orders)) == 4
    assert all(set(order) == set(r.ARMS) for order in orders)
    assert r.letter('{"v":"E"}') == "E"
    assert r.letter('```json\n{"v":"E"}\n```') == "VOID"
    assert r.letter(None) == "VOID"


def test_summary_cannot_recommend_arm_with_missing_or_losing_added_enter():
    r = load()
    cases = [{"call_id": "a", "live_letter": "E"}, {"call_id": "b", "live_letter": "S"}]
    rows = [{"call_id": "a", "arm": "haiku_off", "letter": "E", "wall_ms": 1000},
            {"call_id": "b", "arm": "haiku_off", "letter": "E", "wall_ms": 2000}]
    result = r.summarize(cases, rows)["haiku_off"]
    assert result["live_enter_retention"] == 1
    assert result["added_enters"] == ["b"] and result["missing"] == 0
    assert result["confusion"] == {"E->E": 1, "S->E": 1}
    assert "recommended" not in result


def test_completed_study_refuses_any_more_calls(tmp_path, monkeypatch):
    r = load()
    (tmp_path / "complete.json").write_text('{}')
    monkeypatch.setattr(r.sys, "argv", ['replay', 'run', '--out', str(tmp_path)])
    with pytest.raises(SystemExit):
        r.main()


def test_resume_reads_completed_pairs_after_acquiring_lock(tmp_path, monkeypatch):
    r = load()
    case = {"call_id": "a", "sheet": '{}', "sheet_sha256": r.digest('{}')}
    manifest = {"cases": [case], "system": 'test', "system_sha256": r.digest('test')}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    def locked(*a):
        (tmp_path / 'replay.jsonl').write_text(''.join(json.dumps({"call_id": 'a', "arm": arm})+'\n' for arm in r.ARMS))
    async def repeated(*a):
        pytest.fail('repeated a pair completed before lock acquisition')
    monkeypatch.setattr(r.fcntl, 'flock', locked)
    monkeypatch.setattr(r, 'busy', lambda *a: False)
    monkeypatch.setattr(r, 'invoke', repeated)
    monkeypatch.setattr(r.sys, 'argv', ['replay', 'run', '--out', str(tmp_path)])
    r.main()
