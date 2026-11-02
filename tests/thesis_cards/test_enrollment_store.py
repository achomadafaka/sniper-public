"""SupabasePaperEnrollmentStore (D3) — durable, idempotent, state-preserving.

Runs against a fake PostgREST backend (no network). Proves the adapter inserts once,
returns the existing row unchanged on re-run (so D4-advanced state is never reset), and
plugs into enroll_profile behind the same .upsert() shape as the in-memory store.
"""
from __future__ import annotations

import re
import urllib.parse

from sniper.analysis.thesis_cards.enrollment import PaperEnrollment, enroll_profile
from sniper.analysis.thesis_cards.enrollment_store import SupabasePaperEnrollmentStore, _KEY_COLS
from sniper.analysis.thesis_cards.profile import assemble_profile

_FTD = {"metric": "calibration_edge", "horizon": "to_resolution", "success_threshold": 0.0,
        "kill_threshold": -0.05, "min_forward_n": 20, "stake_convention": "uniform_paper_unit"}


class FakeRest:
    """Minimal PostgREST stand-in: POST resolution=ignore-duplicates + eq-filter GET."""
    def __init__(self):
        self.rows: dict[tuple, dict] = {}
        self.posts = 0

    def __call__(self, method, path, body=None, extra_headers=None):
        if method == "POST":
            self.posts += 1
            row = dict(body[0])
            key = tuple(row[c] for c in _KEY_COLS)
            if key in self.rows:
                return []                       # ignore-duplicates: conflict, no representation
            row.setdefault("state", "queued")
            self.rows[key] = row
            return [dict(row)]
        if method == "GET":
            q = path.split("?", 1)[1] if "?" in path else ""
            filt = {k: urllib.parse.unquote(v) for k, v in re.findall(r"(\w+)=eq\.([^&]+)", q)}
            for row in self.rows.values():
                if all(str(row[c]) == filt.get(c) for c in _KEY_COLS if c in filt):
                    return [dict(row)]
            return []
        raise AssertionError(f"unexpected method {method}")


def _enr(state="queued", **kw):
    base = dict(wallet="0xabc", card_id="sizing_card", card_version="1",
                profile_run_id="run-1", rank=1, q_value=0.01, evidence_state="supported",
                data_tier=None, forward_test_design=_FTD, state=state)
    base.update(kw)
    return PaperEnrollment(**base)


def test_row_payload_has_no_live_trade_fields():
    row = SupabasePaperEnrollmentStore._row(_enr())
    assert set(row) == {"wallet", "card_id", "card_version", "profile_run_id", "rank",
                        "q_value", "evidence_state", "data_tier", "forward_test_design",
                        "enrolled_effect", "state"}
    assert row["forward_test_design"] == _FTD
    assert not ({"price", "size", "order_id", "pnl", "size_usdc"} & set(row))  # no live-trade fields


def test_enroll_profile_records_enrolled_effect_from_card():
    rest = FakeRest()
    store = SupabasePaperEnrollmentStore(request=rest)
    pos = [{"entry_price": 0.45, "size": 1000 if i < 24 else 50, "won": 1 if i < 24 else 0}
           for i in range(40)]
    prof = assemble_profile("0xConv", pos)
    res = enroll_profile(prof, store=store)
    sizing = next(c for c in prof.eligible if c.card_id == "sizing_card")
    assert res.enrollments[0].enrolled_effect == sizing.effect      # S2 baseline = in-sample effect
    assert next(iter(rest.rows.values()))["enrolled_effect"] == sizing.effect


def test_first_upsert_inserts_queued():
    store = SupabasePaperEnrollmentStore(request=FakeRest())
    out = store.upsert(_enr())
    assert out.state == "queued" and out.card_id == "sizing_card"


def test_rerun_preserves_non_queued_state():
    rest = FakeRest()
    store = SupabasePaperEnrollmentStore(request=rest)
    store.upsert(_enr())                              # insert queued
    key = tuple(_enr().__getattribute__(c) for c in _KEY_COLS)
    rest.rows[key]["state"] = "active"               # D4 advances it
    out = store.upsert(_enr(state="queued"))         # re-run tries to enroll again
    assert out.state == "active"                     # existing row returned unchanged
    assert len(rest.rows) == 1                        # no duplicate


def test_distinct_key_inserts_new_row():
    rest = FakeRest()
    store = SupabasePaperEnrollmentStore(request=rest)
    store.upsert(_enr())
    store.upsert(_enr(profile_run_id="run-2"))       # different run id -> new key
    assert len(rest.rows) == 2


def test_enroll_profile_persists_through_supabase_store():
    rest = FakeRest()
    store = SupabasePaperEnrollmentStore(request=rest)
    pos = [{"entry_price": 0.45, "size": 1000 if i < 24 else 50, "won": 1 if i < 24 else 0}
           for i in range(40)]
    prof = assemble_profile("0xConv", pos)
    res = enroll_profile(prof, store=store)
    assert [e.card_id for e in res.enrollments] == ["sizing_card"]
    assert len(rest.rows) == 1
    # idempotent: advance state, re-enroll, state preserved
    key = next(iter(rest.rows))
    rest.rows[key]["state"] = "completed"
    res2 = enroll_profile(prof, store=store)
    assert res2.enrollments[0].state == "completed" and len(rest.rows) == 1
