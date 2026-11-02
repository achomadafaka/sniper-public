"""Instrument only (Alpha 2026-10-02, after the on-chain lane latency read): every return of
_start_early_entry_quote names its cause on the trace (6 of 12 on-chain sends carried no mark), and
the entry's sell proof records its own fallback branch. No behaviour changes."""
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.execution import solana_swap_executor as ex
from sniper.services import memecoin_early_quote as EQ
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg


def _marks():
    marks = []
    return marks, SimpleNamespace(attempt_id="a1", mark_substage=lambda name, **kw: marks.append((name, kw)))


def _ev(**kw):
    return SimpleNamespace(mint="M", signal_source="onchain_watch", their_market_cap=None,
                           delivery_lane=None, source_user_id="u1", **kw)


@pytest.mark.parametrize("case,expected", [
    ("flag_off", "skip:flag_off"),
    ("no_prereads", "skip:no_prereads"),
    ("j7_shadow", "skip:j7_shadow"),
    ("no_quote_seam", "skip:no_quote_seam"),
    ("no_db_path", "skip:no_db_path"),
    ("error", "skip:error:RuntimeError"),
])
def test_every_way_the_early_quote_does_not_start_is_marked(monkeypatch, case, expected):
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_QUOTE", case != "flag_off", raising=False)
    monkeypatch.setattr(EQ, "cheap_prescreen", lambda ev, **kw: None)
    marks, trace = _marks()
    ev = _ev()
    contexts = {"solana": {"entry_quote_fn": lambda *a: {}}}
    if case == "j7_shadow":
        ev.delivery_lane = R.J7_SHADOW_LANE
    if case == "no_quote_seam":
        contexts = {"solana": {}}
    if case == "error":
        monkeypatch.setattr(R, "context_for", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    conn = sqlite3.connect(":memory:")              # an in-memory ledger has no db path
    pre = None if case == "no_prereads" else object()
    assert R._start_early_entry_quote(conn, ev, contexts, trace, pre) is None
    assert marks == [("early_quote", {"used": False, "reason": expected})]


def test_a_prescreen_skip_keeps_its_mark():
    marks, trace = _marks()
    ev = _ev()
    R._start_early_entry_quote(sqlite3.connect(":memory:"), ev,
                               {"solana": {"entry_quote_fn": lambda *a: {}}}, trace, object())
    assert marks and marks[0][0] == "early_quote" and marks[0][1]["reason"].startswith("skip:")


def test_a_started_early_quote_is_marked_started(monkeypatch, tmp_path):
    from test_memecoin_entry_early_quote import _hook
    marks = []
    import test_memecoin_entry_early_quote as T
    real_ns = T.SimpleNamespace

    def ns(**kw):
        if "mark_substage" in kw:
            kw["mark_substage"] = lambda name, **f: marks.append((name, f))
        return real_ns(**kw)
    monkeypatch.setattr(T, "SimpleNamespace", ns)
    _t, seen, _trace = _hook(monkeypatch, tmp_path, flag=True)
    seen["ev"].entry_early_quote._thread.join(5)
    assert ("early_quote_start", {"started": True}) in marks


# --- the sell proof's own fallback branch -------------------------------------------------------------

def test_the_sell_proofs_fallback_tag_reaches_the_trace():
    from test_memecoin_entry_call_timings import _Trace
    from test_memecoin_entry_roundtrip_cap import _c, _Deps
    conn, pid = _c()
    d = _Deps(usd_back=9.9)
    trace = _Trace()
    tag = {"branch": "thin", "raydium": "no_quote", "jupiter_impact_pct": 0.2, "raydium_wait_ms": 12.5}

    def reverse(mint, atomic):
        return {"inAmount": str(atomic), "outAmount": "9900000", "otherAmountThreshold": "9800000",
                "_sniper_fallback": dict(tag)}
    try:
        ex.execute_entry(
            conn, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b", source_user_id="u1",
            now_iso="2026-09-27T00:00:00Z", quote_fn=d.quote_fn, reverse_quote_fn=reverse,
            sign_fn=d.sign_fn, send_fn=d.send_fn, confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn,
            token_fn=d.token_fn, impact_cap_pct=15.0, trace=trace)
    finally:
        conn.close()
    got = [s for s in trace.substages if s["name"] == "reverse_quote_fallback"]
    assert len(got) == 1 and {k: got[0][k] for k in tag} == tag
