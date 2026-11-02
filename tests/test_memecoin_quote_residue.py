"""Quote-asset residue: recorded at the fill, swept back to USDC through the EXISTING holding sell
(docs/superpowers/specs/2026-09-27-slot-lane-any-venue-design.md section 8.1). The sweep only
enqueues memecoin_holding_sell_intents; it never sells a balance the operator holds on purpose."""
import contextlib
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_quote_residue as Q
from sniper.settings import memecoin as cfg

ZEC = "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS"
JUP = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
OWNER = "3EKhb6kyrKQSzzYgR3oHVBtGwHKo6YzDxSjgEz2Bfpg6"
T0, T1, T2 = "2026-09-28T01:00:00+00:00", "2026-09-28T01:05:00+00:00", "2026-09-28T01:10:00+00:00"


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    Q._STATE.update(last_run=None, foreign_logged={})
    with contextlib.closing(c):
        yield c


def _snapshot(conn, rows, observed_at=T2):
    m.replace_other_holdings(conn, "solana", [{"mint": mint, "amount_raw": raw, "decimals": 8}
                                              for mint, raw in rows], observed_at=observed_at,
                             unpriced_count=0)


def _residue(conn, mint, atomic, usd, at=T1):
    assert Q.record_fill_residue(conn, quote_mint=mint, residue_atomic=atomic, quote_usd=usd * 1e8 / atomic,
                                 decimals=8, venue="raydium_cpmm", attempt_id="a1", entry_sig="sig1",
                                 recorded_at=at)


def _intents(conn):
    return [dict(r) for r in conn.execute("SELECT mint, state, allow_small FROM memecoin_holding_sell_intents")]


def test_the_residue_is_read_from_the_receipt_not_the_quote():
    meta = {"preTokenBalances": [{"mint": ZEC, "owner": OWNER, "uiTokenAmount": {"amount": "100"}}],
            "postTokenBalances": [{"mint": ZEC, "owner": OWNER, "uiTokenAmount": {"amount": "160"}},
                                  {"mint": ZEC, "owner": "Pool", "uiTokenAmount": {"amount": "9"}}]}
    assert Q.residue_from_receipt(meta, OWNER, ZEC) == 60
    fresh = {"preTokenBalances": [], "postTokenBalances": meta["postTokenBalances"]}
    assert Q.residue_from_receipt(fresh, OWNER, ZEC) == 160          # the ATA was created in the fill
    assert Q.residue_from_receipt({"preTokenBalances": [], "postTokenBalances": []}, OWNER, ZEC) is None


def test_sol_usdc_and_nothing_are_never_recorded(conn):
    for mint in Q.NEVER_SWEPT:
        assert not Q.record_fill_residue(conn, quote_mint=mint, residue_atomic=5, quote_usd=1.0, decimals=6,
                                         venue="v", attempt_id="a", entry_sig="s", recorded_at=T1)
    assert not Q.record_fill_residue(conn, quote_mint=ZEC, residue_atomic=0, quote_usd=1.0, decimals=8,
                                     venue="v", attempt_id="a", entry_sig="s", recorded_at=T1)
    assert m.unswept_quote_residue(conn, "solana") == {}


def test_the_sweep_is_inert_with_its_flag_off(conn, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_QUOTE_RESIDUE_SWEEP", False, raising=False)
    _residue(conn, ZEC, 1_000_000, 9.0)
    _snapshot(conn, [(ZEC, "1000000")])
    assert Q.run_sweep_once(conn, now_iso=T2) == [] and _intents(conn) == []


def test_residue_the_wallet_holds_alone_is_enqueued_once_to_the_existing_holding_sell(conn, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_QUOTE_RESIDUE_SWEEP", True, raising=False)
    _residue(conn, ZEC, 600_000, 4.0)
    _residue(conn, ZEC, 400_000, 3.0, at=T1)
    _snapshot(conn, [(ZEC, "1010000")])                         # within x1.02 of the recorded 1,000,000
    lines = []
    d = Q.run_sweep_once(conn, now_iso=T2, log=lines.append, clock=lambda: 0.0)
    assert [(x["mint"], x["action"], x["reason"]) for x in d] == [(ZEC, "enqueue", "residue")]
    assert _intents(conn) == [{"mint": ZEC, "state": "queued", "allow_small": 0}]
    assert m.unswept_quote_residue(conn, "solana") == {}              # both rows now belong to it
    assert "residue 1000000" in lines[0]
    assert Q.run_sweep_once(conn, now_iso=T2, clock=lambda: 61.0) == []   # nothing left to sweep


def test_a_foreign_balance_is_never_sold(conn, monkeypatch):
    """Alpha 2026-09-28: JUP, Fartcoin, PENGU and APE are census quote assets the operator may hold on
    purpose. The holding sell takes the WHOLE balance, so a balance above the recorded residue x1.02
    is logged foreign_balance (once per change) and no intent is ever written for that mint."""
    monkeypatch.setattr(cfg, "MEMECOIN_QUOTE_RESIDUE_SWEEP", True, raising=False)
    _residue(conn, JUP, 1_000_000, 8.0)
    _snapshot(conn, [(JUP, "50000000")])                         # the operator's own 50x more
    lines = []
    d = Q.run_sweep_once(conn, now_iso=T2, log=lines.append, clock=lambda: 0.0)
    assert d[0]["reason"] == "foreign_balance" and d[0]["balance_raw"] == 50_000_000
    assert _intents(conn) == [] and len(lines) == 1 and "50000000" in lines[0]
    Q.run_sweep_once(conn, now_iso=T2, log=lines.append, clock=lambda: 61.0)
    assert _intents(conn) == [] and len(lines) == 1               # still held: no intent, no new line


@pytest.mark.parametrize("case,reason", [
    ("under", "under_threshold"), ("stale", "snapshot_older_than_residue"), ("absent", "not_held"),
    ("live", "sell_in_progress"),
])
def test_other_skips(conn, monkeypatch, case, reason):
    monkeypatch.setattr(cfg, "MEMECOIN_QUOTE_RESIDUE_SWEEP", True, raising=False)
    _residue(conn, ZEC, 1_000_000, 1.0 if case == "under" else 9.0)
    if case == "absent":
        _snapshot(conn, [])
    else:
        _snapshot(conn, [(ZEC, "1000000")], observed_at=T0 if case == "stale" else T2)
    if case == "live":
        m.enqueue_holding_sell(conn, "solana", ZEC, now_iso=T2)
    d = Q.run_sweep_once(conn, now_iso=T2, clock=lambda: 0.0)
    assert d[0]["reason"] == reason and len(_intents(conn)) == (1 if case == "live" else 0)


def test_an_aged_small_residue_is_swept_with_allow_small_and_a_failed_sell_frees_it(conn, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_QUOTE_RESIDUE_SWEEP", True, raising=False)
    _residue(conn, ZEC, 100_000, 0.8, at="2026-09-20T01:00:00+00:00")
    _snapshot(conn, [(ZEC, "100000")])
    d = Q.run_sweep_once(conn, now_iso=T2, clock=lambda: 0.0)
    assert d[0]["action"] == "enqueue" and d[0]["allow_small"] is True
    intent = conn.execute("SELECT intent_id FROM memecoin_holding_sell_intents").fetchone()[0]
    m.resolve_holding_sell(conn, intent, "failed", now_iso=T2, reason="no route")
    assert list(m.unswept_quote_residue(conn, "solana")) == [ZEC]   # the residue is still there


def test_the_planner_never_sweeps_sol_or_usdc_even_if_a_row_claims_it():
    for mint in Q.NEVER_SWEPT:
        d = Q.plan_sweep({mint: {"atomic": 10**9, "usd": 100.0, "oldest": T1, "newest": T1, "ids": [1]}},
                         {mint: (10**9, T2)}, now_iso=T2)
        assert d[0]["action"] == "skip" and d[0]["reason"] == "never_swept"


# --- the runner-loop call site (scripts/memecoin_runner_loop.py) -------------------------------------

def _call_site():
    import os
    import textwrap
    src = open(os.path.join(os.path.dirname(__file__), "..", "scripts", "memecoin_runner_loop.py")).read()
    start = src.rindex("\n", 0, src.index("# QUOTE-RESIDUE SWEEP (design 8.1)")) + 1
    end = src.index("# END QUOTE-RESIDUE SWEEP", start)
    after = src.index("if _oh.thread_enabled():", end)
    assert end < after                        # the call sits before the reviewed holding pass
    return textwrap.dedent(src[start:end])


def _exec_site(monkeypatch, *, flag, sweep):
    import traceback
    monkeypatch.setattr(cfg, "MEMECOIN_QUOTE_RESIDUE_SWEEP", flag, raising=False)
    monkeypatch.setattr(Q, "run_sweep_once", sweep)
    clock_reads, lines = [], []
    ns = {"conn": object(), "_p": lines.append, "traceback": traceback,
          "real_utc_now_iso": lambda: clock_reads.append(1) or T2}
    exec(_call_site() + "\nreached_the_holding_pass = True\n", ns)
    return ns, clock_reads, lines


def test_a_raising_sweep_still_reaches_the_holding_pass(monkeypatch):
    """Alpha: as first placed, a sweep exception skipped the rest of the other-holdings block,
    including start_pass, the operator's reviewed sell path."""
    def boom(conn, **kw):
        raise RuntimeError("ledger locked")
    ns, _reads, lines = _exec_site(monkeypatch, flag=True, sweep=boom)
    assert ns.get("reached_the_holding_pass") is True
    assert len(lines) == 1 and "[err] quote residue sweep failed" in lines[0] and "ledger locked" in lines[0]


def test_flag_off_reads_no_clock_and_calls_no_sweep(monkeypatch):
    called = []
    ns, reads, lines = _exec_site(monkeypatch, flag=False, sweep=lambda conn, **kw: called.append(kw))
    assert ns.get("reached_the_holding_pass") is True and reads == [] and called == [] and lines == []
    ns, reads, _ = _exec_site(monkeypatch, flag=True, sweep=lambda conn, **kw: called.append(kw))
    assert reads == [1] and called == [{"now_iso": T2, "log": ns["_p"]}]
