"""Two ways a study arm fails silently, given a voice.

Neither raises, neither shows in the ledger, and both leave the arm reporting under a name it is
no longer running. That combination is why they are logged rather than merely tested.
"""
import json
import sqlite3

import pytest

from sniper.services import memecoin_exits as E
from sniper.settings import memecoin as S

NOW = "2026-08-29T18:00:00+00:00"
OLD = "2026-08-29T06:00:00+00:00"     # 12h, past the 6h backlog threshold
RECENT = "2026-08-29T17:50:00+00:00"


def _valid_policy(arm):
    """Built from the REAL contract in `paper_policy_is_valid`, not from a guess at it.

    My first draft set every required param to 1.0, which fails: `mirror_on_sell` must be a bool
    and a bool is rejected for every other name. A fixture that encodes my belief about validity
    would have let the malformed-assignment test pass while the valid-assignment test failed, which
    is exactly what it did.
    """
    from sniper.services.memecoin_policy import _PAPER_REQUIRED_PARAMS
    params = {k: 1.0 for k in _PAPER_REQUIRED_PARAMS[arm]}
    params["mirror_on_sell"] = False
    return json.dumps({"policy_id": arm, "params": params})


class _Conn:
    def __init__(self, rows):
        self.rows = rows


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    monkeypatch.setattr(E.m, "open_positions_by_strategy",
                        lambda conn, arm: [r for r in conn.rows if r["strategy"] == arm])


def _pos(pid, arm, opened, policy):
    return {"id": pid, "strategy": arm, "opened_at": opened,
            "assigned_policy_json": policy, "experiment_status": "eligible"}


def test_a_backlog_position_with_no_cutoff_is_loud(monkeypatch):
    monkeypatch.setattr(S, "MEMECOIN_STUDY_SWEEP_CLOSURE_BEFORE", "", raising=False)
    c = _Conn([_pos(1, "funded_clone", OLD, _valid_policy("funded_clone"))])
    w = E.study_arm_preflight(c, NOW)
    assert len(w) == 1 and "is unset" in w[0] and "position 1" in w[0]


def test_a_backlog_position_WITH_a_cutoff_is_silent(monkeypatch):
    """The cutoff is the fix for this case, so its presence must end the warning — otherwise the
    guard cries after the problem is solved and gets ignored when it matters."""
    monkeypatch.setattr(S, "MEMECOIN_STUDY_SWEEP_CLOSURE_BEFORE", "2026-08-29T13:07:00Z",
                        raising=False)
    c = _Conn([_pos(1, "funded_clone", OLD, _valid_policy("funded_clone"))])
    assert E.study_arm_preflight(c, NOW) == []


def test_a_RECENT_position_with_no_cutoff_is_silent(monkeypatch):
    """A position opened inside the sweep's own lifetime was evaluated. Warning on it would make
    the unset default noisy everywhere, which is how a guard trains people to ignore it."""
    monkeypatch.setattr(S, "MEMECOIN_STUDY_SWEEP_CLOSURE_BEFORE", "", raising=False)
    c = _Conn([_pos(1, "funded_clone", RECENT, _valid_policy("funded_clone"))])
    assert E.study_arm_preflight(c, NOW) == []


def test_a_malformed_assignment_is_loud(monkeypatch):
    """Every rail falls back to a default and the arm keeps its name. This is the failure that
    cost me a test fixture: the row looked fine and measured nothing."""
    monkeypatch.setattr(S, "MEMECOIN_STUDY_SWEEP_CLOSURE_BEFORE", "2026-08-29T13:07:00Z",
                        raising=False)
    c = _Conn([_pos(7, "profile_matched_v1", RECENT, '{"params": {}}')])
    w = E.study_arm_preflight(c, NOW)
    assert len(w) == 1 and "no valid frozen assignment" in w[0] and "position 7" in w[0]


def test_a_valid_assignment_is_silent(monkeypatch):
    monkeypatch.setattr(S, "MEMECOIN_STUDY_SWEEP_CLOSURE_BEFORE", "2026-08-29T13:07:00Z",
                        raising=False)
    c = _Conn([_pos(7, "profile_matched_v1", RECENT, _valid_policy("profile_matched_v1"))])
    assert E.study_arm_preflight(c, NOW) == []


def test_both_conditions_on_one_position_report_separately(monkeypatch):
    """They are different defects with different fixes and must not collapse into one line."""
    monkeypatch.setattr(S, "MEMECOIN_STUDY_SWEEP_CLOSURE_BEFORE", "", raising=False)
    c = _Conn([_pos(3, "funded_clone", OLD, "not json")])
    assert len(E.study_arm_preflight(c, NOW)) == 2


def test_the_guard_covers_every_study_arm(monkeypatch):
    """A third arm added to STUDY_ARMS must be swept too — the loop reads the constant, and this
    pins that it is not a hardcoded pair."""
    monkeypatch.setattr(S, "MEMECOIN_STUDY_SWEEP_CLOSURE_BEFORE", "x", raising=False)
    c = _Conn([_pos(i, arm, RECENT, "bad") for i, arm in enumerate(E.STUDY_ARMS, start=1)])
    assert len(E.study_arm_preflight(c, NOW)) == len(E.STUDY_ARMS)
