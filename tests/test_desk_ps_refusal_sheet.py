"""Capacity refusals preserve the exact constructed sheet across save/record/recovery."""
import base64
import hashlib
import json
import zlib
from contextlib import nullcontext

import pytest

from sniper.services import memecoin_desk_context_facts as C
from sniper.services import memecoin_desk_judge as J
from sniper.persistence import db_memecoin_desk as D
from test_memecoin_desk_judge import databases  # noqa: F401
from test_desk_ps_inputs_integration import ready, block, refresh, Clock


@pytest.mark.parametrize("recover", [False, True])
def test_codex_busy_preserves_context_sheet_hash_and_deadline(databases, tmp_path, recover):
    ticket, _ = ready(databases, tmp_path / "tape")
    def busy(*_, **__):
        return {"call_error": "codex_busy", "no_model_call": True, "model_requested": J.GPT_MODEL}
    def crash(*_, **__):
        raise RuntimeError("saved first")
    def run():
        return J.run_once(databases[0], lambda: nullcontext(databases[1]), call_model=busy,
            system_prompt="fixture", worker="busy-test", now_fn=Clock(), calls_per_hour=40, asof_fn=refresh,
            ps_inputs_fn=lambda *a: block(*a, tmp=tmp_path / "tape"), context_fn=lambda *_: C.unknown_block("no_source"),
            **({"record_fn": crash} if recover else {}))
    if recover:
        with pytest.raises(RuntimeError, match="saved first"):
            run()
        J.recover(databases[0], lambda: nullcontext(databases[1]), now_fn=Clock())
    else:
        run()
    saved = json.loads(databases[0].execute("SELECT result_json FROM desk_judge_calls").fetchone()[0])
    exact = zlib.decompress(base64.b64decode(saved["sheet_zb64"])).decode()
    vet = next(s for s in D.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet")["inputs"]
    assert vet["sheet"] == json.loads(exact)
    assert vet["sheet_sha256"] == saved["sheet_sha256"] == hashlib.sha256(exact.encode()).hexdigest()
    assert vet["deadline_s"] == saved["deadline_s"]
