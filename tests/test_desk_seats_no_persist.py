"""Every production desk seat opts out of CLI session persistence (hundreds of calls a day must not fill the
operator's chat list). The adapter still returns session_id / result_id (live smoke 2026-10-22: both present,
billed, echo ok, no new session file). Pinned per seat by reading the real call site, so a refactor that drops the
flag fails here."""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SEATS = {"judge": "scripts/memecoin_desk_judge_worker.py", "crawler": "scripts/memecoin_desk_crawler.py",
         "postmortem": "scripts/memecoin_desk_postmortem.py", "risk_shadow": "scripts/memecoin_desk_risk_shadow.py",
         "risk_watcher": "scripts/memecoin_desk_risk_watcher.py"}


def calls(path):
    tree = ast.parse((ROOT / path).read_text())
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", None)) == "sdk_call_model"]


@pytest.mark.parametrize("seat", sorted(SEATS))
def test_the_seat_calls_the_adapter_with_persist_session_false(seat):
    found = calls(SEATS[seat])
    assert len(found) == 1, "expected exactly one adapter call site in %s" % SEATS[seat]
    kw = {k.arg: k.value for k in found[0].keywords}
    assert isinstance(kw.get("persist_session"), ast.Constant) and kw["persist_session"].value is False


def test_the_adapter_default_stays_true_for_everything_else():
    import inspect
    import importlib.util
    spec = importlib.util.spec_from_file_location("jw_np", ROOT / "scripts/memecoin_desk_judge_worker.py")
    jw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(jw)
    assert inspect.signature(jw.sdk_call_model).parameters["persist_session"].default is True
