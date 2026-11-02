"""Pure parts of the agent-setup script: wallet-file persistence contract."""
import importlib.util
import json
import os
import stat
import sys

import pytest


def _load():
    spec = importlib.util.spec_from_file_location("hl_real_setup_agent",
                                                  "scripts/hl_real_setup_agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hl_real_setup_agent"] = mod
    spec.loader.exec_module(mod)
    return mod


S = _load()


def test_save_wallet_file_0600_and_shape(tmp_path):
    p = str(tmp_path / "w.json")
    S.save_wallet_file(p, "0xAbC", "0x" + "11" * 32)
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600
    data = json.load(open(p))
    assert data == {"address": "0xAbC", "private_key": "0x" + "11" * 32}


def test_save_wallet_file_refuses_overwrite(tmp_path):
    p = str(tmp_path / "w.json")
    S.save_wallet_file(p, "0xA", "0xk")
    with pytest.raises(FileExistsError):
        S.save_wallet_file(p, "0xB", "0xk2")
    S.save_wallet_file(p, "0xB", "0xk2", force=True)
    assert json.load(open(p))["address"] == "0xB"
