"""Tier W forward pass: a tape db without tx_index (the 10-21 db) must not crash the pass; the value is NULL (unknown), never 0."""
import importlib.util
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("srcfwd", ROOT / "scripts/research/source_entry_window_fwd.py")
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)


def test_tape_forward_reads_a_db_without_tx_index(tmp_path, monkeypatch):
    p = tmp_path / "2026-10-21.db"
    c = sqlite3.connect(p)
    c.execute("create table swaps(slot integer, signature text, ordinal integer, mint text, curve text, is_buy integer, quote_atomic integer, token_atomic integer, actor text, "
              "virtual_quote integer, virtual_token integer, block_time integer, received_at real, real_token integer, fee_bps integer, creator_fee_bps integer)")
    tc = 1_792_000_000.0
    c.execute("insert into swaps values(1,'s',0,'M','C',1,10,5,'a',1,1,?,?,1,95,30)", (int(tc) + 5, tc + 5))
    c.commit()
    c.close()
    monkeypatch.setattr(F, "TAPE_DB", str(tmp_path / "2026-10-%02d.db"))
    monkeypatch.setattr(F, "TAPE_DAYS", {21: None})
    monkeypatch.setattr(F, "datetime", type("D", (), {"fromtimestamp": staticmethod(lambda t, tz: __import__("datetime").datetime(2026, 10, 21, tzinfo=tz))}))
    out, _ = F.tape_forward("M", tc)
    assert len(out) == 1 and out[0]["idx"] is None and out[0]["sig"] == "s"
