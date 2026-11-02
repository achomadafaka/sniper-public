"""The chain-1 bridge-key seed (Gamma (a), 2026-10-07): ids re-derived from the key fields before any
write, dry run by default, a readable row after --confirm, a rerun that writes nothing."""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from sniper.execution.evm_v4_quote import V4
from sniper.persistence import db_memecoin as m

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location(
        "seed_chain1_bridge_keys", ROOT / "scripts" / "memecoin_seed_chain1_bridge_keys.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _db():
    c = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(c)
    return c


def test_both_keys_hash_back_to_their_pool_ids():
    assert [pid for pid, _k in _script().verified_keys()] == list(_script().KEYS)


def test_dry_run_writes_nothing_and_confirm_stores_readable_rows_once():
    mod, c = _script(), _db()
    written, lines = mod.run(c, confirm=False)
    assert written == 0 and all("would store" in line for line in lines)
    assert c.execute("SELECT COUNT(*) FROM memecoin_evm_v4_pool_keys").fetchone()[0] == 0
    written, _ = mod.run(c, confirm=True)
    assert written == 2
    for pool_id, key in mod.KEYS.items():
        got = m.get_evm_v4_pool_key(c, chain_id="1", pool_id=pool_id, pool_manager=V4["1"]["pool_manager"])
        assert got["fee"] == key["fee"] and got["tick_spacing"] == key["tick_spacing"]
    written, lines = mod.run(c, confirm=True)
    assert written == 0 and all("already stored" in line for line in lines)


def test_a_key_that_does_not_hash_back_is_refused_before_any_write(monkeypatch):
    mod, c = _script(), _db()
    pid = next(iter(mod.KEYS))
    monkeypatch.setitem(mod.KEYS, pid, dict(mod.KEYS[pid], fee=500))
    with pytest.raises(SystemExit, match="REFUSE"):
        mod.run(c, confirm=True)
    assert c.execute("SELECT COUNT(*) FROM memecoin_evm_v4_pool_keys").fetchone()[0] == 0
