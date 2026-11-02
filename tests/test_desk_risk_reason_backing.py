"""A model's structure-break EXIT needs the code-computed confirmation."""
import json

import pytest

from sniper.services import memecoin_desk_risk_live as L
from test_memecoin_desk_risk_live import POLICY, BOUNDS
from test_memecoin_desk_risk_watcher import Env, pos


@pytest.mark.parametrize("sb", [0, None])
def test_a_structure_break_exit_without_confirmation_is_a_hold(sb):
    verdict = L.validate('[{"k":"pos:1","a":"E","r":null,"c":10}]',
                         blocks=[{"k":"pos:1", "sb":sb, "pnl_pct_exact":None}],
                         policies={"pos:1": POLICY}, bounds=BOUNDS)["pos:1"]
    assert verdict == ("HOLD", None, "unbacked_reason:10", 10)


def test_a_confirmed_structure_break_exit_remains_allowed():
    verdict = L.validate('[{"k":"pos:1","a":"E","r":null,"c":10}]',
                         blocks=[{"k":"pos:1", "sb":1, "pnl_pct_exact":None}],
                         policies={"pos:1": POLICY}, bounds=BOUNDS)["pos:1"]
    assert verdict == ("EXIT", None, "exit", 10)


def test_the_watcher_persists_the_refusal_and_counts_it(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, [pos(1)])
    e.answer = lambda blocks: json.dumps([{"k":"pos:1", "a":"E", "r":None, "c":10}])
    stats = e.tick(fill_sigs_of=lambda _: (), attention_fn=lambda *_: [])
    row = e.rows()[0]
    assert (row["action"], row["reason"], row["reason_code"]) == ("HOLD", "unbacked_reason:10", 10)
    assert row["expires_at"] is None and row["call_error"] is None
    assert stats["unbacked_decisions"] == 1 and stats["failed_calls"] == 0
