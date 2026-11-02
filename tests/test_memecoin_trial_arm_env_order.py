"""scripts/memecoin_trial_arm.py reads .env BEFORE the settings module is imported (2026-10-07,
china_sniper_1): a module-level import of memecoin_trial_ops froze MEMECOIN_EVM_ALLOWLIST at its code
default (8453,4663), so an evm: roster id got no 56 address and the start refused 'no proven 56
address'. Run as the operator runs it: a fresh process, a state root whose .env names chain 56."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UID = "evm:0xd1bee21a7898232c337e46bc399488be065bae4f"


def test_the_trial_start_sees_the_env_allowlist_from_its_own_state_root(tmp_path):
    db = tmp_path / "sniper_tracker.db"
    c = sqlite3.connect(db)
    sys.path.insert(0, str(ROOT))
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_axiom import ensure_axiom_tables
    m.ensure_memecoin_tables(c)
    ensure_axiom_tables(c)
    c.execute("CREATE TABLE IF NOT EXISTS memecoin_wallet_addresses (source_user_id TEXT, handle TEXT, "
              "network_id TEXT, address TEXT, verify_status TEXT)")
    m.add_wallet(c, UID, "china_sniper_1")
    c.commit()
    c.close()
    (tmp_path / ".env").write_text("MEMECOIN_EVM_ALLOWLIST=8453,4663,56\nMEMECOIN_REAL_TRIAL_CLIP_USD_56=25\n")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("MEMECOIN_") and k not in ("DB_PATH", "SNIPER_DB_PATH")}
    env.update(SNIPER_STATE_ROOT=str(tmp_path), PYTHONPATH=str(ROOT))
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "memecoin_trial_arm.py"), "start",
                          "china_sniper_1", "56"], capture_output=True, text=True, env=env, timeout=120)
    text = out.stdout + out.stderr
    assert "no proven 56 address" not in text, text[-800:]
    assert "'addresses': 1" in text and "'clip_usd': 25.0" in text, text[-800:]
