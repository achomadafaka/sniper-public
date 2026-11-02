"""Dip retry max premium (operator 2026-10-09: "lower the dip retry to 10% above the entry"; Alpha's
design, A and B approved). MEMECOIN_REAL_DIP_RETRY_MAX_PREMIUM_PCT caps the dip only: the watch hits at
min(gate cap, knob) (EVM: the knob restated on the quote basis through the refused quote's k), and the
decision a hit runs carries ev.dip_cap, which both executors enforce on the fresh quote AND on the
signed minimum, so a dip fill never lands above the gate reference x (1 + min(cap, knob)). The first
attempt and every non-dip entry never carry it. Unset = today."""
import json

import pytest

from sniper.execution import solana_swap_executor as ex
from sniper.services import memecoin_dip_retry as D
from sniper.settings import memecoin as cfg
from test_memecoin_dip_retry import (WITHIN, _chase, _conn, _dip, _enqueue, _ev, _hits,  # noqa: F401
                                     _Quotes, _seed, _watch, _cfg)
from test_memecoin_entry_chase_quote_recheck import _c, _Deps


@pytest.fixture
def knob(_cfg):  # noqa: F811
    _cfg.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_MAX_PREMIUM_PCT", 10.0, raising=False)
    return _cfg


# ------------------------------------------------------------------------------------ the knob

@pytest.mark.parametrize("raw, want", [("", None), ("10", 10.0), ("2.5", 2.5), ("0", None), ("-3", None),
                                       ("nan", None), ("inf", None), ("ten", None)])
def test_the_knob_parses_positive_finite_percent_or_is_unset(raw, want):
    said = []
    assert cfg.parse_dip_retry_max_premium(raw, log=said.append) == want
    assert bool(said) == (raw not in ("", "10", "2.5"))


def test_the_knob_is_unset_by_default():
    assert cfg.parse_dip_retry_max_premium(None) is None


# ----------------------------------------------------------------------------------- the watch
# reference 1e-5 USD/token, clip $100 at 6 decimals: out = 100 / price x 1e6

def _out(premium_pct):
    return int(100 / (1e-5 * (1 + premium_pct / 100)) * 1e6)


def test_with_the_knob_the_watch_hits_only_within_ten_percent(tmp_path, knob):
    _seed(tmp_path)
    w = _watch(tmp_path, _Quotes([_out(20.0), _out(9.0)]))
    w.tick()
    assert w.take_due() == set()                      # +20%: inside the gate's 30, over the knob
    w.tick()
    assert w.take_due() == {"e1"}


def test_without_the_knob_the_watch_hits_at_the_gate_cap_as_today(tmp_path):
    _seed(tmp_path)
    w = _watch(tmp_path, _Quotes([_out(20.0)]))
    w.tick()
    assert w.take_due() == {"e1"}


@pytest.mark.parametrize("dip, network, want", [
    ({"cap_pct": 30.0}, "solana", 10.0),
    ({"cap_pct": 8.0}, "solana", 8.0),                                   # a cap only, never above
    ({"cap_pct": 24.2, "min_ratio": 1 / 0.92}, "4663", 1.10 * 0.92 * 100 - 100),
    ({"cap_pct": 24.2}, "4663", 10.0),                                   # k unknown: the knob
    ({"cap_pct": 0.5, "min_ratio": 1 / 0.92}, "4663", 0.5),
])
def test_the_watch_cap_is_min_of_the_gate_and_the_knob_on_the_quote_basis(knob, dip, network, want):
    assert D.dip_watch_cap(dip, network) == pytest.approx(want)


def test_the_watch_cap_without_the_knob_is_the_gate_cap(_cfg):  # noqa: F811
    assert D.dip_watch_cap({"cap_pct": 30.0, "min_ratio": 1.1}, "4663") == 30.0


# --------------------------------------------------------------- the hit carries the cap, the log names it

def test_a_hit_carries_min_cap_knob_on_the_event_and_logs_which_cap_applied(knob):
    c = _conn()
    ev = _ev()
    _enqueue(c, ev, _chase(c, ev))
    seen = []

    def attempt(conn, e):
        seen.append(getattr(e, "dip_cap", None))
        return {"status": "skipped_chase", "reason": "dip cap"}
    _hits(c, attempt)
    [cap] = seen
    assert cap == {"reference_price": pytest.approx(1e-5), "max_pct": 10.0, "knob_pct": 10.0,
                   "gate_cap_pct": 30.0}
    [log] = [dict(r) for r in c.execute("SELECT * FROM memecoin_dip_retry_log")]
    assert (log["cap_pct"], log["gate_cap_pct"], log["cap_source"]) == (10.0, 30.0, "dip_knob")


def test_without_the_knob_the_event_carries_nothing_and_the_log_says_gate():
    c = _conn()
    ev = _ev()
    _enqueue(c, ev, _chase(c, ev))
    seen = []
    _hits(c, lambda conn, e: seen.append(hasattr(e, "dip_cap")) or {"status": "filled", "position_id": 1})
    assert seen == [False]
    [log] = [dict(r) for r in c.execute("SELECT * FROM memecoin_dip_retry_log")]
    assert (log["cap_pct"], log["gate_cap_pct"], log["cap_source"]) == (30.0, 30.0, "gate")


def test_a_terminal_row_logs_the_cap_it_watched(knob):
    c = _conn()
    ev = _ev()
    _enqueue(c, ev, _chase(c, ev))
    D.run_hits(c, lambda *a: {}, "2026-09-30T12:01:00+00:00", watch=type("W", (), {"take_due": lambda s: set()})(),
               event_from_json=lambda j: None)
    [log] = [dict(r) for r in c.execute("SELECT * FROM memecoin_dip_retry_log")]
    assert log["status"] == "expired" and log["cap_source"] == "dip_knob" and log["cap_pct"] == 10.0


# ---------------------------------------------------------------- Solana: the decision enforces it

def _sol_entry(monkeypatch, *, out, dip_cap):
    """Source 1.0, chase reference 1.0, chase cap 30%, hard cap 35%. $10 for out/1e6 tokens."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0, raising=False)
    c, pid = _c(source_price=1.0)
    d, signed = _Deps(), []
    kw = {"dip_cap": dip_cap} if dip_cap is not None else {}
    result = ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b", source_user_id="u1",
        now_iso="2026-10-09T10:40:00+00:00",
        quote_fn=lambda *_: {"inAmount": "10000000", "outAmount": str(out),
                             "otherAmountThreshold": str(int(out * 0.85)), "slippageBps": 1500,
                             "priceImpactPct": "0.001"},
        reverse_quote_fn=lambda _, amount: {"inAmount": str(amount), "outAmount": "9900000",
                                            "otherAmountThreshold": "9900000"},
        usdc_fn=d.usdc_fn, token_fn=lambda _: (0.0, 6), sign_fn=lambda q: signed.append(q),
        send_fn=d.send_fn, confirm_fn=d.confirm_fn, chase_max_pct=30.0, chase_reference_price=1.0, **kw)
    c.close()
    return result, signed, d


DIP = {"reference_price": 1.0, "max_pct": 10.0, "knob_pct": 10.0, "gate_cap_pct": 30.0}


def test_a_solana_dip_decision_above_the_knob_is_refused_before_signing(monkeypatch):
    """THE CHECK (Solana): +20% passes every gate today but not the dip's 10%."""
    result, signed, d = _sol_entry(monkeypatch, out=8_333_333, dip_cap=DIP)
    assert result["status"] == "skipped_chase" and result["reason"].startswith("dip cap:")
    assert not signed and "send" not in d.calls


def test_a_solana_dip_decision_within_the_knob_signs_a_minimum_at_ten_percent(monkeypatch):
    """THE CHECK (Solana): +5% signs, and the signed minimum caps the fill at reference x 1.10."""
    result, signed, _d = _sol_entry(monkeypatch, out=9_523_809, dip_cap=DIP)
    assert len(signed) == 1
    assert 10.0 / (int(signed[0]["otherAmountThreshold"]) / 1e6) <= 1.10 + 1e-9


def test_without_dip_cap_the_same_solana_quote_signs_as_today(monkeypatch):
    _result, signed, _d = _sol_entry(monkeypatch, out=8_333_333, dip_cap=None)
    assert len(signed) == 1
    assert 10.0 / (int(signed[0]["otherAmountThreshold"]) / 1e6) > 1.10


def test_an_unknown_solana_quote_price_is_refused_under_a_dip_cap(monkeypatch):
    result, signed, _d = _sol_entry(monkeypatch, out=0, dip_cap=DIP)
    assert result["status"] != "filled" and not signed


# ------------------------------------------------------------------- EVM: the signed minimum

def _evm(dip_cap, *, buy, minimum, copy_cap=0.0):
    from sniper.execution.evm_swap_executor import execute_entry_evm
    from sniper.persistence import db_memecoin as m
    from test_memecoin_evm_entry_executor import _QUOTE, _c as _ec, _seams
    c = _ec()
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-10-09T10:40:00Z", network_id="4663",
                            source_price=1.0)
    q = {**_QUOTE, "buyAmount": str(buy), "minBuyAmount": str(minimum)}
    seams, calls = _seams(quote=q, token=((0.0, 6), (buy / 1e6, 6)))
    old = cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT
    cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT = copy_cap
    try:
        kw = {"dip_cap": dip_cap} if dip_cap is not None else {}
        res = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
                                strategy="alpha_b", source_user_id="trader1", now_iso="t", **seams, **kw)
    finally:
        cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT = old
    c.close()
    return res, calls


def test_an_evm_dip_decision_whose_signed_minimum_is_over_the_knob_is_refused():
    """THE CHECK (EVM): quote 1.05, signed worst 10/8.7 = 1.149 > 1.10: refused, nothing signed."""
    res, calls = _evm(DIP, buy=9_523_809, minimum=8_700_000)
    assert res["status"] == "skipped_chase" and res["reason"].startswith("dip cap:")
    assert calls["sign"] == 0 and calls["send"] == 0


def test_an_evm_dip_decision_whose_signed_minimum_is_within_the_knob_passes_the_dip_check():
    res, calls = _evm(DIP, buy=9_523_809, minimum=9_200_000)          # worst 1.087
    assert res["status"] == "filled" and calls["sign"] >= 1


def test_without_dip_cap_the_same_evm_quote_is_not_refused_by_a_dip_check():
    res, _calls = _evm(None, buy=9_523_809, minimum=8_700_000)
    assert res["status"] == "filled"


# ----------------------------------------------------- B: an executor without the kwarg is refused

def test_a_dip_event_reaching_a_solana_executor_without_the_kwarg_is_refused():
    from sniper.services import memecoin_real_leg as leg
    assert leg.dip_cap_refusal(type("E", (), {"dip_cap": DIP})(), lambda conn, **kw: None) is None
    assert leg.dip_cap_refusal(type("E", (), {"dip_cap": DIP})(),
                               lambda conn, *, pending_id=None: None) == "dip_cap_unsupported"
    assert leg.dip_cap_refusal(type("E", (), {})(), lambda conn, *, pending_id=None: None) is None


# ------------------------------------------------- the wiring a live dip actually crosses

def _leg_entry(monkeypatch, tmp_path, execute_fn, dip_cap):
    from types import SimpleNamespace
    from sniper.services import memecoin_arming
    from sniper.services.memecoin_real_leg import real_entry
    from test_memecoin_entry_drift import _conn as _dc
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    ev = SimpleNamespace(fomo_event_id="dip:s", source_user_id="trader", source_handle="alice",
                         signal_source="follow_activity", network_id="solana",
                         mint="C1ankMint1111111111111111111111111111111pump", ticker="M", side="buy",
                         their_price=0.02, their_market_cap=1e6, created_at="2026-10-09T10:39:59+00:00")
    if dip_cap is not None:
        ev.dip_cap = dip_cap
    conn = _dc()
    res = real_entry(conn, ev, day="2026-10-09", usdc_balance=300.0, sol_balance=1.0,
                     now_iso="2026-10-09T10:40:00+00:00", trade_size=10.0,
                     token_safety_fn=lambda _m: (True, "ok"), execute_fn=execute_fn,
                     kill_path=str(tmp_path / "kill"))
    return res, conn


def test_the_leg_hands_a_dip_cap_to_an_executor_that_takes_it(monkeypatch, tmp_path):
    seen = {}

    def execute_fn(conn, pid, mint, usd, now_iso, impact_cap_pct=None, dip_cap=None):
        seen["dip_cap"] = dip_cap
        return {"status": "aborted", "reason": "stub"}
    _leg_entry(monkeypatch, tmp_path, execute_fn, DIP)
    assert seen == {"dip_cap": DIP}


def test_the_leg_refuses_a_dip_for_an_executor_that_cannot_enforce_it_and_releases(monkeypatch, tmp_path):
    called = []

    def legacy(conn, pid, mint, usd, now_iso, impact_cap_pct=None):
        called.append(pid)
        return {"status": "filled"}
    res, conn = _leg_entry(monkeypatch, tmp_path, legacy, DIP)
    assert res["status"] == "rejected" and res["reason"] == "dip_cap_unsupported" and called == []
    assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved'").fetchone()[0] == 0


def test_a_non_dip_entry_reaches_a_legacy_executor_unchanged(monkeypatch, tmp_path):
    called = []
    _leg_entry(monkeypatch, tmp_path, lambda conn, pid, mint, usd, now_iso, impact_cap_pct=None:
               called.append(pid) or {"status": "aborted"}, None)
    assert len(called) == 1


def test_the_funded_solana_binding_forwards_the_dip_cap(monkeypatch):
    import sqlite3
    import time
    from solders.keypair import Keypair
    from sniper.execution import solana_real_seams as S
    from sniper.execution import solana_swap_executor
    from sniper.execution.submission_transport import AttemptState
    from sniper.persistence import db_memecoin as m
    from test_memecoin_fast_submit_wiring import StubTransport
    seen = []
    monkeypatch.setattr(solana_swap_executor, "execute_entry",
                        lambda conn, **kw: seen.append(kw.get("dip_cap")) or {"status": "aborted", "reason": "stub"})
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = m.reserve_pending(conn, mint="MINT", source_user_id="u", strategy="alpha_b",
                            notional=10.0, reserved_at="t", network_id="solana", attempt_id="a")
    transport = StubTransport(AttemptState.ACCEPTED)
    ex_ = S.build_real_solana_executor(keypair=Keypair(), rpc_url="https://rpc",
                                       http=lambda *a, **k: (200, {"result": None}, None),
                                       submission_transport=transport,
                                       usdc_fn=lambda: 10.0, token_fn=lambda _m: 0.0)
    ex_["execute_fn"](conn, pid, "MINT", 10.0, "t", dip_cap=DIP,
                      entry_min_slot=100, entry_max_slot=101,
                      entry_head_fn=lambda: (100, time.monotonic()))
    assert seen == [DIP]
    assert transport.calls == 0


def test_a_dip_decision_never_falls_back_to_lifi_which_cannot_enforce_the_cap():
    from sniper.execution.evm_swap_executor import execute_entry_evm
    from sniper.persistence import db_memecoin as m
    from test_memecoin_evm_entry_executor import _c as _ec, _seams
    lifi = []
    for cap in (DIP, None):
        c = _ec()
        pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                                notional=10.0, reserved_at="2026-10-09T10:40:00Z", network_id="4663",
                                source_price=1.0)
        seams, _calls = _seams(quote=None)
        kw = {"dip_cap": cap} if cap is not None else {}
        execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="trader1", now_iso="t",
                          quote_miss_fn=lambda: {"code": "no_route", "detail": "none", "rungs": []},
                          lifi_entry_fn=lambda miss, cap=cap: lifi.append(cap) or {"status": "aborted"},
                          **seams, **kw)
        c.close()
    assert lifi == [None]                        # only the non-dip entry reached LiFi
