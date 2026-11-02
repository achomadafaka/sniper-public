"""scripts/research/rh_fast_decide_read.py counts decisions, not the source_pending_expired rows
(Gamma LOW-1 on 11830c4a4): an expiry has decide_ms 0, a ~2 s handoff and no activity rank, so mixed
in it drags the decide p50 down, inflates the handoff p90 and 'unranked', and grows the would_send
denominator. Expiries print as their own per-blocker count."""
import importlib.util
import sqlite3
from pathlib import Path

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_rh_fast_decide as fast

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "research" / "rh_fast_decide_read.py"


def _read(tmp_path, capsys):
    db = str(tmp_path / "decide.db")
    c = sqlite3.connect(db)
    c.executescript(fast._DECISIONS)
    for reason, decide_ms, handoff_ms, rank in (("would_send", 1.0, 50.0, 3),
                                                ("no_route", 2.0, 60.0, 7),
                                                ("source_pending_expired:proof_absent", 0.0, 2100.0, None),
                                                ("source_pending_expired:proof_absent", 0.0, 2300.0, None),
                                                ("source_pending_expired:resolved_at_deadline", 0.0, 2000.0, None)):
        c.execute("INSERT INTO rh_fast_decisions(tx_hash, mint, decided_at, reason, decide_ms, "
                  "handoff_ms, activity_rank, true_checked) VALUES (?,?,?,?,?,?,?,?)",
                  ("0x" + "ab" * 32, "0x" + "cd" * 20, "2026-10-06T12:00:00+00:00", reason,
                   decide_ms, handoff_ms, rank, 0))
    c.commit()
    c.close()
    ledger = str(tmp_path / "ledger.db")
    lc = sqlite3.connect(ledger)
    m.ensure_memecoin_tables(lc)
    lc.close()
    spec = importlib.util.spec_from_file_location("rh_fast_decide_read", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main(["--since", "2026-10-06T00:00:00+00:00", "--db", db, "--ledger", ledger])
    return capsys.readouterr().out


def test_expired_reasks_are_counted_apart_from_decisions(tmp_path, capsys):
    out = _read(tmp_path, capsys)
    assert "decisions 2 since" in out
    assert "addressable (would_send) 50.0%" in out
    assert "decide_ms: n=2 p50 2.000" in out                    # never the expiries' zeros
    assert "handoff_ms: n=2" in out
    assert "unranked 0 of 2" in out
    assert "expired exact-source re-asks 3: proof_absent 2, resolved_at_deadline 1" in out
