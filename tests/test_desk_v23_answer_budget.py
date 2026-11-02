"""v23 answer budget: a hard output-token cap from MEMECOIN_DESK_JUDGE_MAX_OUTPUT_TOKENS, frozen per cohort."""
import importlib.util
from pathlib import Path

import pytest

from test_memecoin_desk_contract import judge, verdict

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("judge_worker_budget", ROOT / "scripts" / "memecoin_desk_judge_worker.py")
W = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(W)


@pytest.mark.parametrize("raw,want", [(None, None), ("", None), (" 600 ", 600), ("64", 64), ("32000", 32000)])
def test_budget_parse(raw, want):
    env = {} if raw is None else {"MEMECOIN_DESK_JUDGE_MAX_OUTPUT_TOKENS": raw}
    assert W.answer_budget(env) == want


@pytest.mark.parametrize("raw", ["63", "32001", "1e3", "-5", "x", "600.0"])
def test_malformed_budget_refuses(raw):
    with pytest.raises(ValueError):
        W.answer_budget({"MEMECOIN_DESK_JUDGE_MAX_OUTPUT_TOKENS": raw})


def test_budget_reaches_the_cli_env_and_none_leaves_options_unchanged():
    on = W._sdk_options("sys", model="m", effort="low", thinking=None, persist_session=False, max_output_tokens=600)
    off = W._sdk_options("sys", model="m", effort="low", thinking=None, persist_session=False)
    assert on.env == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "600"} and not off.env


def test_mismatch_is_a_named_no_answer_skip():
    out = judge(verdict(), call_error="answer_budget_mismatch")
    assert out["action"] == "SKIP" and out["reason"] == "judge_answer_budget_mismatch"


def test_cut_answer_is_malformed_skip():
    assert judge('{"k": "desk:U:fixture", "v": "E", "e": nu')["reason"] == "malformed_json"


@pytest.mark.parametrize("bad", [63, 32001, 600.0, "600"])
def test_provisioning_refuses_a_malformed_frozen_budget(bad):
    from sniper.services import memecoin_desk as desk
    with pytest.raises(ValueError):
        desk._v23_policy({"judge_max_output_tokens": bad})
