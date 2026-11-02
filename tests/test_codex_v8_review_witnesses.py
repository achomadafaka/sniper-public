"""Offline review witnesses for immutable RISK v8 pin 4082ad95e."""
import json
import time

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_desk_risk_apply as A
from sniper.services import memecoin_desk_risk_live as L
from sniper.settings import memecoin as cfg
from test_memecoin_desk_risk_apply import desk, _acks, _effective, _quote
from test_desk_risk_loosen_trail import _at, _loosen, _run


@pytest.mark.parametrize("citation", [[], {}])
def test_nested_citation_refuses_one_item_and_keeps_the_other(citation):
    blocks = [{"k": "pos:1"}, {"k": "pos:2"}]
    policies = {b["k"]: {"trail_pct": 15} for b in blocks}
    response = [{"k": "pos:1", "a": "L", "r": {"trail_pct": 22}, "c": 17, "f": [citation]},
                {"k": "pos:2", "a": "E", "r": None, "c": 1}]
    out = L.validate(json.dumps(response), blocks=blocks, policies=policies,
                     bounds={"trail_pct": (8, 30)})
    assert out["pos:1"][:3] == ("HOLD", None, "loosen_bad_cite")
    assert out["pos:2"][0] == "EXIT"


def test_frozen_cohort_ceiling_survives_new_global_bounds(monkeypatch, tmp_path):
    import test_memecoin_desk_audit_fixes as fixtures
    original = fixtures._metadata

    def metadata(conn):
        meta = original(conn)
        bounds = cfg.parse_desk_rail_bounds("tp_pct:15-150,sl_pct:10-40,trail_pct:8-20")
        meta["cohort_id"] = m.register_desk_cohort(
            conn, producer="H", policy={"rail_bounds": bounds},
            prompt_version="desk-judge-v6", created_at="2026-10-21T12:00:00+00:00")
        return meta

    monkeypatch.setattr(fixtures, "_metadata", metadata)
    c, pos, db, r = desk.__wrapped__(monkeypatch, tmp_path)
    cohort_id = json.loads(pos["funded_contract_json"])["source"]["desk"]["cohort_id"]
    stored = json.loads(c.execute("SELECT policy_json FROM memecoin_desk_cohorts WHERE cohort_id=?",
                                  (cohort_id,)).fetchone()[0])
    assert stored["policy"]["rail_bounds"]["trail_pct"][1] == 20
    assert cfg.MEMECOIN_DESK_RAIL_BOUNDS["trail_pct"][1] == 30
    decision = _loosen(r, pos, 22)
    _run(c, pos, db)
    ack = _acks(c)[decision]
    assert (ack["status"], _effective(c, pos)["trail_pct"]) == ("refused", 15.0), ack


def test_sixty_second_gap_starts_when_change_applies_not_when_sweep_started(desk, monkeypatch):
    from sniper.util import clock
    monkeypatch.setattr(clock, "offset_s", lambda: 0.0)
    c, pos, db, r = desk
    now = time.time()
    first = _loosen(r, pos, 18, now=now)
    _quote(c, pos, now=now - 1)
    A.apply_orders(c, pos, now_epoch=now, now_iso=_at(now - 20), path=db)
    assert _acks(c)[first]["status"] == "applied"
    second = _loosen(r, pos, 22, now=now + 41)
    _quote(c, pos, now=now + 40)
    A.apply_orders(c, pos, now_epoch=now + 41, now_iso=_at(now + 41), path=db)
    ack = _acks(c)[second]
    assert (ack["status"], ack["reason"]) == ("refused", "loosen_rate_limited"), ack
