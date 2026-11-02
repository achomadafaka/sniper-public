"""A production assembler must reach scoped readers, while its old policy stays conservative."""
from types import SimpleNamespace
import json
import sqlite3

from scripts import memecoin_desk_intake_assembler as A
from sniper.services import memecoin_desk as desk
from test_desk_ps_loss_integration import provision, add
from test_memecoin_desk_ps_evidence import fixture, mint, NOW, MINT


def test_new_assembler_frontier_requires_strict_loss_schema(tmp_path):
    root, candidate = fixture(tmp_path)
    provision(root)
    add(root, pools=[MINT])
    assert A.committed_through_print(root, candidate, NOW) is True
    assert A.committed_through_print(root, candidate, NOW, integrity=True) is True
    with sqlite3.connect(root / 'pumpswap/tape.db') as db:
        db.execute('DROP INDEX ps_decode_losses_received')
    assert A.committed_through_print(root, candidate, NOW, integrity=True) is False


def test_new_assembler_hard_evidence_reaches_strict_reader(tmp_path, monkeypatch):
    root, candidate = fixture(tmp_path)
    provision(root)
    add(root, pools=[MINT])
    monkeypatch.setattr(A, "read_mint", lambda *a, **kw: (mint(), NOW))
    monkeypatch.setattr(desk, "evaluate_candidate", lambda *a, **kw: {"verdict": "PASS", "reason": "pass"})
    def evaluate(policy):
        return A.evaluate(candidate, "ticket", desk_conn=None, desk_dir=root, rpc=SimpleNamespace(timeout_s=1),
                          clip_usd=25, hard_policy=policy, now_fn=lambda: NOW+.1)[2]["checks"]["sell_probe"]
    assert evaluate({})["status"] == "pass"
    assert evaluate({"intake_source_version": "desk-intake-source-v1"})["status"] == "pass"
    with sqlite3.connect(root / 'pumpswap/tape.db') as db:
        db.execute('UPDATE ps_loss_attribution SET pools_json=?', (json.dumps([candidate['pool']]),))
    assert evaluate({})["status"] == "pass"
    assert evaluate({"intake_source_version": "desk-intake-source-v1"})["status"] == "unknown"
