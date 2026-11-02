import importlib.util
from pathlib import Path
from types import SimpleNamespace

def test_model_switch_does_not_wait_for_old_cli_disconnect():
    spec=importlib.util.spec_from_file_location("ecat_jw",Path(__file__).resolve().parents[1]/"scripts/memecoin_desk_judge_worker.py")
    w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
    class Old:
        options=SimpleNamespace(model="claude-haiku-5-5")
        def close(self):
            raise TimeoutError("old CLI disconnect would wait five seconds")
    slot={"spare":Old()}
    new=SimpleNamespace(options=SimpleNamespace(model="claude-opus-5-5"))
    assert w.spare_for(slot,"claude-opus-5-5",lambda model:new) is new
