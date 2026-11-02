"""One JUDGE call deadline: the SDK adapter and validate_verdict read the same knob; a replay uses the deadline stamped with the call."""
import math
from contextlib import nullcontext

import pytest

from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk_contract as contract
from sniper.services import memecoin_desk_judge as J
from test_memecoin_desk_contract import BOUNDS, TIMING_CAPS, verdict
from test_memecoin_desk_judge import Clock, databases, enter, model_returning, ready_ttl  # noqa: F401


def judge(elapsed_ms, **kw):
    return contract.validate_verdict(verdict(), opportunity_id="desk:U:fixture", bounds=BOUNDS, timing_caps=TIMING_CAPS,
                                     elapsed_ms=elapsed_ms, **kw)


def test_default_is_12_seconds_and_env_unset_or_blank_uses_it():
    assert contract.judge_deadline_s({}) == 12.0 and contract.judge_deadline_s({"MEMECOIN_DESK_JUDGE_DEADLINE_S": "  "}) == 12.0
    assert contract.JUDGE_DEADLINE_DEFAULT_S == 12.0 == J.DEADLINE_S


def test_knob_is_read_and_validated():
    assert contract.judge_deadline_s({"MEMECOIN_DESK_JUDGE_DEADLINE_S": "8.5"}) == 8.5
    assert contract.judge_deadline_s({"MEMECOIN_DESK_JUDGE_DEADLINE_S": "60"}) == 60.0
    for bad in ("abc", "0", "-1", "nan", "inf", "61", "1e9"):
        with pytest.raises(ValueError, match="MEMECOIN_DESK_JUDGE_DEADLINE_S"):
            contract.judge_deadline_s({"MEMECOIN_DESK_JUDGE_DEADLINE_S": bad})


def test_validate_verdict_bound_follows_the_knob_not_a_second_constant(monkeypatch):
    monkeypatch.delenv("MEMECOIN_DESK_JUDGE_DEADLINE_S", raising=False)
    assert judge(6603)["action"] == "ENTER"                         # the real call that timed out under the old 5 s
    assert judge(12_000)["action"] == "ENTER" and judge(12_001)["reason"] == "judge_timeout"
    monkeypatch.setenv("MEMECOIN_DESK_JUDGE_DEADLINE_S", "20")
    assert judge(19_000)["action"] == "ENTER" and judge(20_001)["reason"] == "judge_timeout"


def test_a_real_timeout_is_still_a_typed_skip():
    out = judge(60_001, deadline_s=60)
    assert out["action"] == "SKIP" and out["reason"] == "judge_timeout"
    assert judge(1000, call_error="timeout")["reason"] == "judge_timeout"       # the adapter's own timeout cause


def test_a_stamped_deadline_wins_over_the_current_knob(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DESK_JUDGE_DEADLINE_S", "12")
    assert judge(6603, deadline_s=5.0)["reason"] == "judge_timeout"             # saved under the old 5 s: still a timeout on replay
    monkeypatch.setenv("MEMECOIN_DESK_JUDGE_DEADLINE_S", "5")
    assert judge(6603, deadline_s=12.0)["action"] == "ENTER"                    # saved under 12 s: still valid on replay


@pytest.mark.parametrize("bad", [0, -1, float("nan"), math.inf, 61, "12"])
def test_invalid_stamped_deadline_is_a_typed_skip(bad):
    assert judge(1000, deadline_s=bad)["reason"] == "invalid_deadline"


def run(databases, clock, seconds=1.5):
    return J.run_once(databases[0], lambda: nullcontext(databases[1]),
                      call_model=model_returning(enter, calls=[], clock=clock, seconds=seconds),
                      system_prompt="SYS", worker="w", now_fn=clock, calls_per_hour=100)


def vet(databases, ticket):
    return [s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet"][0]


def test_the_call_stamps_the_deadline_into_the_saved_result_and_the_vet_inputs(databases, monkeypatch):
    monkeypatch.setenv("MEMECOIN_DESK_JUDGE_DEADLINE_S", "15")
    ticket = ready_ttl(databases)
    run(databases, Clock())
    saved = databases[0].execute("SELECT result_json FROM desk_judge_calls").fetchone()[0]
    assert '"deadline_s":15.0' in saved
    assert vet(databases, ticket)["inputs"]["deadline_s"] == 15.0


def test_the_real_6_6_second_call_is_an_enter_under_the_default_and_a_timeout_under_5(databases, monkeypatch):
    monkeypatch.delenv("MEMECOIN_DESK_JUDGE_DEADLINE_S", raising=False)
    ticket = ready_ttl(databases)
    run(databases, Clock(), seconds=6.6)
    assert vet(databases, ticket)["verdict"] == "ENTER"


def test_a_5_second_knob_still_times_a_6_6_second_call_out(databases, monkeypatch):
    monkeypatch.setenv("MEMECOIN_DESK_JUDGE_DEADLINE_S", "5")
    ticket = ready_ttl(databases)
    run(databases, Clock(), seconds=6.6)
    v = vet(databases, ticket)
    assert v["verdict"] == "SKIP" and v["reason"] == "judge_timeout"


def test_a_saved_result_without_a_stamp_is_judged_under_the_legacy_5_seconds(databases, monkeypatch):
    monkeypatch.setenv("MEMECOIN_DESK_JUDGE_DEADLINE_S", "12")
    real = store.save_judge_response

    def old_worker(conn, *, result, **kw):                       # a worker from before the stamp existed
        return real(conn, result={k: v for k, v in result.items() if k != "deadline_s"}, **kw)
    monkeypatch.setattr(store, "save_judge_response", old_worker)
    ticket = ready_ttl(databases)
    run(databases, Clock(), seconds=6.6)
    v = vet(databases, ticket)
    assert v["verdict"] == "SKIP" and v["reason"] == "judge_timeout" and v["inputs"]["deadline_s"] == 5.0   # never silently revalidated at 12 s


def test_sdk_adapter_default_reads_the_same_knob(monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("jw_deadline", Path(__file__).resolve().parents[1] / "scripts" / "memecoin_desk_judge_worker.py")
    jw = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "jw_deadline", jw)
    spec.loader.exec_module(jw)
    seen = []

    class Stub:
        pass
    import asyncio
    real = asyncio.wait_for

    async def spy(coro, timeout):
        seen.append(timeout)
        coro.close()
        raise asyncio.TimeoutError()
    monkeypatch.setattr(asyncio, "wait_for", spy)
    monkeypatch.setenv("MEMECOIN_DESK_JUDGE_DEADLINE_S", "9")
    try:
        jw.sdk_call_model("s", "u")
    except Exception:
        pass
    monkeypatch.setattr(asyncio, "wait_for", real)
    assert seen == [9.0]
    seen.clear()
    monkeypatch.setattr(asyncio, "wait_for", spy)
    jw.sdk_call_model("s", "u", deadline_s=12.0)                                   # explicit callers (RISK, post-mortem) keep theirs
    assert seen == [12.0]
