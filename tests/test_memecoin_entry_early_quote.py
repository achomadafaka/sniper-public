"""The J7 Solana early entry quote (2026-09-28, Alpha GO option 4; memecoin_early_quote.py).

The Jupiter entry quote starts at decision start, beside the DexScreener GET and the reservation
stage, for the clip the risk envelope would approve. The executor uses it ONLY for that exact clip
and while it is fresh; everything else discards it (counted) and quotes exactly as before. Flag
MEMECOIN_ENTRY_EARLY_QUOTE (default on; 0 = today's path).
"""
import threading
import time
from types import SimpleNamespace

import pytest

import sniper.services.memecoin_volume_writer  # noqa: F401  the hook imports it lazily (~130 ms)
from sniper.services import memecoin_early_quote as EQ
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg

QUOTE = {"inAmount": "10000000", "outAmount": "500000000", "otherAmountThreshold": "470000000",
         "priceImpactPct": "0.01", "routePlan": [{"swapInfo": {"ammKey": "AMM1"}}]}


@pytest.fixture(autouse=True)
def _fresh_stats(monkeypatch):
    monkeypatch.setattr(EQ, "_STATS", {"skipped": {}, "started": 0, "quoted": 0,
                                       "sizing_refused": {}, "used": 0, "discarded": {},
                                       "refusal_requoted": {}, "refusal_agreed": {}})
    monkeypatch.setattr(R, "_gate_balance_cache", {})
    monkeypatch.setattr(R, "_gate_balance_generation", {}, raising=False)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _early(clip=25.0, refusal=None, quote=QUOTE, clock=time.monotonic, **kw):
    calls = []

    def quote_fn(mint, usd):
        calls.append((mint, usd))
        if isinstance(quote, Exception):
            raise quote
        return dict(quote) if isinstance(quote, dict) else quote
    early = EQ.EarlyEntryQuote(mint="MINT", quote_fn=quote_fn,
                               sizing_fn=lambda: (clip, refusal), clock=clock, **kw)
    return early, calls


# --- the object -----------------------------------------------------------------------------------

def test_the_quote_for_the_approved_clip_is_used_once():
    early, calls = _early()
    assert early.take(25.0) == QUOTE
    assert calls == [("MINT", 25.0)]
    assert early.take(25.0) is None, "a second take must not hand the same quote out again"
    s = EQ.stats()
    assert (s["started"], s["quoted"], s["used"], s["wasted_quotes"]) == (1, 1, 1, 0), s


@pytest.mark.parametrize("clip,expected", [(24.99, "clip_mismatch"), (25.01, "clip_mismatch")])
def test_another_clip_discards_it(clip, expected):
    early, _ = _early()
    assert early.take(clip) is None
    assert EQ.stats()["discarded"] == {expected: 1}
    assert EQ.stats()["wasted_quotes"] == 1


def test_a_stale_quote_is_discarded():
    clock = Clock()
    early, _ = _early(clock=clock)
    early._thread.join(5)
    clock.t += 1.6
    assert early.take(25.0, max_age_s=1.5) is None
    assert EQ.stats()["discarded"] == {"stale": 1}


def test_a_refused_envelope_never_quotes():
    early, calls = _early(clip=None, refusal="risk:re-entry cooldown")
    assert early.take(25.0) is None and calls == []
    s = EQ.stats()
    assert s["sizing_refused"] == {"risk:re-entry cooldown": 1} and s["quoted"] == 0, s
    assert s["discarded"] == {"sizing_refused": 1}


@pytest.mark.parametrize("quote,reason", [(None, "no_quote"), ({}, "no_quote"),
                                          (RuntimeError("boom"), "error")])
def test_a_missing_or_failed_quote_is_discarded_and_never_raises(quote, reason):
    early, _ = _early(quote=quote)
    assert early.take(25.0) is None
    assert EQ.stats()["discarded"] == {reason: 1}


def test_a_quote_still_in_flight_waits_then_gives_up():
    release = threading.Event()

    def slow(mint, usd):
        release.wait(5)
        return dict(QUOTE)
    early = EQ.EarlyEntryQuote(mint="MINT", quote_fn=slow, sizing_fn=lambda: (25.0, None))
    began = time.monotonic()
    assert early.take(25.0, max_wait_s=0.05) is None
    assert time.monotonic() - began < 1.0
    release.set()
    assert EQ.stats()["discarded"] == {"not_ready": 1}


def test_known_wrong_clip_does_not_wait_for_unusable_quote():
    class Pending:
        ident = 1
        def __init__(self):
            self.waits = []
        def join(self, seconds):
            self.waits.append(seconds)
        def is_alive(self):
            return True

    early, _ = _early(start=False)
    early.clip = 25.0
    early._thread = Pending()
    assert early.take(10.0) is None
    assert early._thread.waits == []
    assert EQ.stats()["discarded"] == {"clip_mismatch": 1}


def test_the_native_shadow_starts_beside_the_early_quote():
    seen = []

    class Shadow:
        def offer_jupiter(self, quote):
            seen.append(("offer", quote))
    early, _ = _early(shadow_fn=lambda mint, clip: seen.append(("start", mint, clip)) or Shadow())
    assert early.take(25.0) == QUOTE
    assert seen == [("start", "MINT", 25.0), ("offer", QUOTE)]


def test_a_failing_shadow_never_touches_the_quote():
    early, _ = _early(shadow_fn=lambda mint, clip: (_ for _ in ()).throw(RuntimeError("x")))
    assert early.take(25.0) == QUOTE


@pytest.mark.parametrize("ev,quiet,ceiling,expected", [
    (SimpleNamespace(signal_source="manual", their_market_cap=1e5), False, 1e6, "manual"),
    (SimpleNamespace(signal_source="j7", override=True, their_market_cap=1e5), False, 1e6,
     "manual"),
    (SimpleNamespace(signal_source="j7", their_market_cap=1e5), True, 1e6, "quiet_hours"),
    (SimpleNamespace(signal_source="j7", their_market_cap=0.0), False, 1e6, "mcap_unknown"),
    (SimpleNamespace(signal_source="j7", their_market_cap=None), False, 1e6, "mcap_unknown"),
    (SimpleNamespace(signal_source="j7", their_market_cap=2e6), False, 1e6, "mcap_over_ceiling"),
    (SimpleNamespace(signal_source="j7", their_market_cap=1e5), False, 1e6, None),
    (SimpleNamespace(signal_source="j7", their_market_cap=2e6), False, 0, None),
])
def test_the_cheap_prescreen(ev, quiet, ceiling, expected):
    assert EQ.cheap_prescreen(ev, quiet_hours=quiet, mcap_ceiling=ceiling) == expected


# --- the executor ---------------------------------------------------------------------------------

def _execute(early_quote, *, native_shadow_fn=None, max_accounts=None, fresh_quote=QUOTE,
             chase_max_pct=None, usdc_seq=(100.0, 88.0), token_seq=((0.0, 6), (500.0, 6))):
    from test_memecoin_entry_drift import _conn, _reverse_quote
    from sniper.execution.solana_swap_executor import execute_entry
    from sniper.persistence import db_memecoin as m
    conn = _conn()
    pending_id = m.reserve_pending(
        conn, mint="MINT", source_user_id="trader", strategy="alpha_b", notional=12.0,
        reserved_at="2026-07-15T12:00:00+00:00", network_id="solana",
        origin_event_id="exact-event", attempt_id="attempt-1", signal_source="global_feed",
        source_price=0.02, source_market_cap=1_000_000.0,
        observed_at="2026-07-15T11:59:59+00:00", clock_domain="observed_real_utc")
    usdc = iter(usdc_seq)
    token = iter(token_seq)
    fresh = []

    def quote_fn(_mint, usd, **kw):
        fresh.append((usd, kw))
        return dict(fresh_quote)
    result = execute_entry(
        conn, pending_id=pending_id, mint="MINT", usd_amount=12.0, strategy="alpha_b",
        source_user_id="trader", now_iso="2026-07-15T12:00:02+00:00",
        quote_fn=quote_fn, reverse_quote_fn=_reverse_quote,
        sign_fn=lambda _quote: (b"signed", "sig"), send_fn=lambda _raw: True,
        confirm_fn=lambda _sig: "confirmed", usdc_fn=lambda: next(usdc),
        token_fn=lambda _mint: next(token), native_shadow_fn=native_shadow_fn,
        early_quote=early_quote, max_accounts=max_accounts, chase_max_pct=chase_max_pct)
    return result, fresh


def test_the_executor_uses_the_early_quote_and_skips_its_own_quote_and_shadow():
    early, calls = _early(clip=12.0)
    shadows = []
    result, fresh = _execute(early, native_shadow_fn=lambda *a: shadows.append(a))
    assert result["status"] == "filled", result
    assert calls == [("MINT", 12.0)] and fresh == [], "the executor quoted a second time"
    assert shadows == [], "the executor started a second shadow beside a used early quote"


def test_a_discarded_early_quote_leaves_the_executor_path_unchanged():
    early, _ = _early(clip=11.0)                   # sized for another clip
    shadows = []
    result, fresh = _execute(early, native_shadow_fn=lambda *a: shadows.append(a))
    assert result["status"] == "filled", result
    assert fresh == [(12.0, {})] and len(shadows) == 1
    assert EQ.stats()["discarded"] == {"clip_mismatch": 1}


def test_the_oversize_retry_never_takes_the_early_quote():
    early, _ = _early(clip=12.0)
    _result, fresh = _execute(early, max_accounts=48)
    assert fresh and fresh[0][1].get("max_accounts") == 48
    assert EQ.stats()["used"] == 0 and early._taken is False


# --- a refusal on the early quote is confirmed by a fresh one (Alpha doctrine 2026-09-28) ----------

PRICEY = dict(QUOTE, outAmount="250000000", otherAmountThreshold="235000000")   # 0.04 vs source 0.02
CHEAP = dict(QUOTE, outAmount="1000000000", otherAmountThreshold="940000000")   # 0.01
REQUOTED = dict(usdc_seq=(100.0, 100.0, 88.0), token_seq=((0.0, 6), (0.0, 6), (500.0, 6)))


@pytest.mark.parametrize("fresh_quote,status,agreed", [(QUOTE, "filled", False),
                                                       (PRICEY, "skipped_chase", True)])
def test_an_early_chase_refusal_stands_only_if_a_fresh_quote_confirms_it(fresh_quote, status,
                                                                         agreed):
    early, _ = _early(clip=12.0, quote=PRICEY)
    kwargs = REQUOTED if status == "filled" else {}
    result, fresh = _execute(early, fresh_quote=fresh_quote, chase_max_pct=30.0, **kwargs)
    assert result["status"] == status, result
    assert fresh == [(12.0, {})], "the refusal was not re-checked on a fresh quote"
    s = EQ.stats()
    assert s["refusal_requoted"] == {"chase": 1}
    assert s["refusal_agreed"] == ({"chase": 1} if agreed else {})


@pytest.mark.parametrize("fresh_quote,status,agreed", [(QUOTE, "filled", False),
                                                       (CHEAP, "aborted", True)])
def test_an_early_post_peak_refusal_stands_only_if_a_fresh_quote_confirms_it(
        monkeypatch, fresh_quote, status, agreed):
    from sniper.execution import solana_swap_executor as X
    monkeypatch.setattr(X, "_post_peak_confirmation", lambda _c, price, _t: SimpleNamespace(
        confirmed=bool(price and price < 0.015), peak_price=0.05, display_price=0.01,
        exact_price=price, exact_giveback_pct=80.0))
    early, _ = _early(clip=12.0, quote=CHEAP)
    kwargs = REQUOTED if status == "filled" else {}
    result, fresh = _execute(early, fresh_quote=fresh_quote, **kwargs)
    assert result["status"] == status, result
    assert agreed is (result.get("reason") == "post_peak_confirmed")
    assert fresh == [(12.0, {})]
    s = EQ.stats()
    assert s["refusal_requoted"] == {"post_peak": 1}
    assert s["refusal_agreed"] == ({"post_peak": 1} if agreed else {})


def test_an_early_quote_that_passes_is_used_without_a_fresh_one():
    early, _ = _early(clip=12.0)
    result, fresh = _execute(early, chase_max_pct=30.0)
    assert result["status"] == "filled" and fresh == []
    assert EQ.stats()["refusal_requoted"] == {}


# --- the hook and the leg -------------------------------------------------------------------------

def _hook(monkeypatch, tmp_path, *, flag, market_cap=100_000.0, trial_clip=None):
    from test_memecoin_entry_identity import FakeClock, NOW, _arm, _event, _solana_context
    from sniper.persistence import db_memecoin as m
    import sqlite3
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_QUOTE", flag, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_GATE_READS", True, raising=False)
    _arm(monkeypatch)
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    if trial_clip is not None:
        from sniper.services.memecoin_trial import RULE
        m.start_trial(conn, source_user_id="u1", chain="solana", clip_usd=trial_clip, rule=RULE,
                      actor="test", now_iso=NOW)
    t, seen = {}, {}
    ctx = _solana_context({})

    def quote_fn(mint, usd):
        t.setdefault("quote_start", time.monotonic())
        return dict(QUOTE)

    def stats(mint, network_id=None):
        t["get_start"] = time.monotonic()
        time.sleep(0.2)
        t["get_end"] = time.monotonic()
        return {}

    def entry(conn, ev, **kw):
        seen["ev"] = ev
        return {"status": "rejected", "reason": "stub"}
    ctx.update(entry_quote_fn=quote_fn)
    monkeypatch.setattr(R, "real_entry", entry)
    monkeypatch.setattr(R, "_alert_entry_result", lambda *a, **k: None)
    trace = SimpleNamespace(attempt_id="attempt-q", mark=lambda *a, **k: True,
                            finish=lambda *a, **k: True, mark_substage=lambda *a, **k: True)
    ev = _event(network_id="solana", mint="So1Mint", market_cap=market_cap)
    R.real_entry_hook(conn, ev, {"solana": ctx}, now_fn=lambda: NOW, trade_size=10.0,
                      max_age_s=120, stats_fn=stats, trace=trace, trace_clock=FakeClock())
    return t, seen, trace


def test_the_hook_starts_the_quote_inside_the_get(monkeypatch, tmp_path):
    t, seen, trace = _hook(monkeypatch, tmp_path, flag=True)
    early = seen["ev"].entry_early_quote
    assert early is not None and early.trace is trace
    early._thread.join(5)
    assert early.refusal is None, early.refusal
    assert t["quote_start"] < t["get_end"], t
    assert early.clip and early.clip > 0


@pytest.mark.parametrize("flag", [True, False])
def test_unknown_onchain_mcap_overlaps_the_first_lookup(monkeypatch, tmp_path, flag):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from test_memecoin_entry_identity import _arm, _event, NOW
    _arm(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_QUOTE", flag)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_QUOTE_ONCHAIN", True)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_GATE_READS", True)
    monkeypatch.setattr(R, "_early_quote_clip", lambda *a, **k: (175, None))
    monkeypatch.setattr(R, "_alert_entry_result", lambda *a, **kw: None)
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, "u1", "alice")
    m.set_wallet_real_approved(conn, "u1", True, chains=["solana"], force=True)
    quoted, calls, seen = threading.Event(), [], {}

    def quote(mint, clip):
        calls.append((mint, clip))
        quoted.set()
        return dict(QUOTE)

    def stats(*a, **kw):
        seen["overlapped"] = quoted.wait(0.2)
        return {"market_cap": 40000, "price": 0.00004}

    def entry(conn, ev, contexts, **kw):
        seen["early"] = getattr(ev, "entry_early_quote", None)
        return {"status": "rejected", "reason": "fixture"}

    monkeypatch.setattr(R, "real_entry_hook", entry)
    from test_memecoin_solana_source_initiation import receipt, proof_for
    tx, owner = receipt()
    proof = dict(proof_for(tx, owner), source_user_id='u1')
    ev = _event(network_id="solana", mint=proof['mint'], market_cap=None, source="onchain_watch",
                event_id='onchain:' + proof['tx_hash'])
    ev.source_wallet, ev.source_block_number, ev.source_initiation = owner, tx['slot'], proof
    ev.native_failure_fallback = True  # the ordinary lane remains eligible after a native failure
    ev.their_price = 0.00004
    hook = R.make_real_hook(None, {"solana": {
        "usdc_fn": lambda: 300, "sol_fn": lambda: 1, "entry_quote_fn": quote}},
        now_fn=lambda: NOW, trade_size=175, max_age_s=120, stats_fn=stats)
    try:
        hook(conn, ev)
        assert seen["overlapped"] is flag
        if flag:
            early = seen["early"]
            assert early.trace is ev.entry_prereads.trace
            assert early.take(175) == QUOTE
            assert early.take(175) is None
            assert calls == [(ev.mint, 175)]
        else:
            assert seen["early"] is None and calls == []
    finally:
        conn.close()


def test_a_trial_armed_wallet_s_early_quote_is_sized_at_the_trial_clip(monkeypatch, tmp_path):
    """Trial 5's first fill (Tekkerrss solana, 2026-10-02 00:52:56Z) reserved the $20 trial clip while
    the early quote priced $123.61, so the executor re-quoted. Rule-5: the early quote stays an
    estimate and the executor still uses only the reserved clip; this sizes the estimate the way the
    reservation will (the open trial's marker, `size_cap_usd`), on the production hook path."""
    _t, seen, _trace = _hook(monkeypatch, tmp_path, flag=True)
    seen["ev"].entry_early_quote._thread.join(5)
    full = seen["ev"].entry_early_quote.clip
    assert full and full > 6.0, full
    (tmp_path / "trial").mkdir()
    _t, seen, _trace = _hook(monkeypatch, tmp_path / "trial", flag=True, trial_clip=6.0)
    early = seen["ev"].entry_early_quote
    early._thread.join(5)
    assert early.refusal is None and early.clip == 6.0, (early.clip, early.refusal)


def test_flag_off_is_todays_path(monkeypatch, tmp_path):
    t, seen, _trace = _hook(monkeypatch, tmp_path, flag=False)
    assert getattr(seen["ev"], "entry_early_quote", None) is None and "quote_start" not in t


def test_an_unknown_mcap_starts_nothing_and_is_counted(monkeypatch, tmp_path):
    t, seen, _trace = _hook(monkeypatch, tmp_path, flag=True, market_cap=0.0)
    assert getattr(seen["ev"], "entry_early_quote", None) is None
    assert EQ.stats()["skipped"] == {"mcap_unknown": 1}


def test_the_early_clip_is_the_clip_the_reservation_approves(monkeypatch, tmp_path):
    """Same envelope, same balances, same scale: the early sizing and reserve_entry agree."""
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_market_state as MS
    from sniper.services.memecoin_risk import reserve_entry
    from test_memecoin_entry_identity import _arm
    _arm(monkeypatch)
    monkeypatch.setattr(MS, "market_risk_scale", lambda *a, **k: 1.0)
    path = str(tmp_path / "ledger.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pre = SimpleNamespace(balances=lambda: (300.0, 1.0, False, 5.0))
    ev = SimpleNamespace(mint="So1Mint", signal_source="j7")
    clip, why = R._early_quote_clip(ev, pre, path)
    assert why is None and clip > 0
    decision, pid = reserve_entry(conn, mint="So1Mint", source_user_id="u1", strategy="alpha_b",
                                  day="2026-07-15", usdc_balance=300.0, sol_balance=1.0,
                                  now_iso="2026-07-15T12:00:00+00:00")
    assert decision.allowed and pid, decision
    assert decision.approved_size_usdc == clip
    # the coin is now held: the early envelope refuses it (one per coin) and quotes nothing
    clip2, why2 = R._early_quote_clip(ev, pre, path)
    assert clip2 is None and why2.startswith("risk:"), why2


def test_early_clip_deducts_retained_spend_and_rejects_expired_history(monkeypatch, tmp_path):
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_market_state as MS
    from test_memecoin_entry_identity import _arm
    _arm(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "fraction")
    monkeypatch.setattr(cfg, "MEMECOIN_BANKROLL_FRACTION", .1)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 5)
    monkeypatch.setattr(MS, "market_risk_scale", lambda *a, **k: 1.0)
    path, conn = _ledger(tmp_path)
    pending = m.reserve_pending(
        conn, mint="Earlier", source_user_id="u1", strategy="alpha_b", notional=100,
        reserved_at="2026-10-09T15:00:00Z", network_id="solana", attempt_id="earlier")
    m.confirm_pending(conn, pending)
    stamps = []
    pre = SimpleNamespace(balances=lambda: (300.0, 1.0, False, 5.0), balance_read_started=100)
    ev = SimpleNamespace(mint="So1Next", signal_source="j7")
    ordinary, _ = R._early_quote_clip(ev, pre, path)
    pre.snapshot_debits_fn = lambda started: stamps.append(started) or {
        "pending_ids": (pending,), "extra_sol_atomic": 5_000_000}
    retained, why = R._early_quote_clip(ev, pre, path)
    assert why is None and 0 < retained < ordinary and stamps == [100]

    def expired(started):
        raise ValueError("snapshot history expired")

    pre.snapshot_debits_fn = expired
    assert R._early_quote_clip(ev, pre, path) == (None, "snapshot debit unavailable")
    conn.close()


@pytest.mark.parametrize("bound", [True, False])
def test_the_leg_hands_the_executor_only_this_attempts_quote(monkeypatch, tmp_path, bound):
    from test_memecoin_entry_drift import _conn
    from sniper.services import memecoin_arming
    from sniper.services.memecoin_real_leg import real_entry
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    trace = SimpleNamespace(attempt_id="attempt-leg", mark=lambda *a, **k: True,
                            mark_substage=lambda *a, **k: True)
    early = SimpleNamespace(trace=trace if bound else object())
    ev = SimpleNamespace(fomo_event_id="e1", source_user_id="trader", source_handle="alice",
                         signal_source="global_feed", network_id="solana", mint="MINT",
                         ticker="M", side="buy", their_price=0.02, their_market_cap=1e6,
                         created_at="2026-07-15T11:59:59+00:00", entry_early_quote=early)
    seen = {}

    def execute_fn(conn, pid, mint, usd, now_iso, impact_cap_pct=None, trace=None,
                   early_quote=None):
        seen["early"] = early_quote
        return {"status": "aborted", "reason": "stub"}
    real_entry(_conn(), ev, day="2026-07-15", usdc_balance=300.0, sol_balance=1.0,
               now_iso="2026-07-15T12:00:00+00:00", trade_size=10.0,
               token_safety_fn=lambda _m: (True, "ok"), execute_fn=execute_fn,
               kill_path=str(tmp_path / "kill"), trace=trace)
    assert "early" in seen, "the entry never reached the executor"
    assert (seen["early"] is early) is bound


def test_the_funded_binding_passes_the_early_quote_except_on_the_retry(monkeypatch):
    import sqlite3
    from solders.keypair import Keypair
    from sniper.execution import solana_real_seams as S
    from sniper.execution import solana_swap_executor
    from sniper.execution.submission_transport import AttemptState
    from sniper.persistence import db_memecoin as m
    from test_memecoin_fast_submit_wiring import StubTransport
    seen = []

    def fake_execute_entry(conn, **kw):
        seen.append(kw.get("early_quote"))
        return {"status": "aborted", "reason": "stub"}
    monkeypatch.setattr(solana_swap_executor, "execute_entry", fake_execute_entry)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = m.reserve_pending(conn, mint="MINT", source_user_id="u", strategy="alpha_b",
                            notional=10.0, reserved_at="t", network_id="solana", attempt_id="a")
    transport = StubTransport(AttemptState.ACCEPTED)
    ex = S.build_real_solana_executor(keypair=Keypair(), rpc_url="https://rpc",
                                      http=lambda *a, **k: (200, {"result": None}, None),
                                      submission_transport=transport,
                                      usdc_fn=lambda: 10.0, token_fn=lambda _m: 0.0)
    assert ex["entry_quote_fn"] is not None and ex["native_shadow_start_fn"] is not None
    window = dict(entry_min_slot=100, entry_max_slot=101,
                  entry_head_fn=lambda: (100, time.monotonic()))
    ex["execute_fn"](conn, pid, "MINT", 10.0, "t", early_quote="E", **window)
    ex["execute_fn"](conn, pid, "MINT", 10.0, "t", early_quote="E", max_accounts=48, **window)
    assert seen == ["E", None]
    assert transport.calls == 0


# --- C1 clip_mismatch (Alpha 2026-10-07): the coin-level regime scale is unknown before the GET -------
# 15 of the 17 C1 clip_mismatch discards since 10-01 were reserved at exactly 0.75 x the early clip:
# the early sizing runs at the market regime scale (1.0 on hot_broad/normal) and the classification
# after the GET gives a hot_narrow coin MEMECOIN_FUNDED_REGIME_RISK_SCALE. The early thread now also
# quotes that scaled clip, beside the first, and the executor takes whichever clip was reserved.

def _early_alt(alt=18.75, alt_quote=None, **kw):
    calls = []
    alt_quote = dict(QUOTE, outAmount="375000000") if alt_quote is None else alt_quote

    def quote_fn(mint, usd):
        calls.append((mint, usd))
        q = alt_quote if usd == alt else dict(QUOTE)
        if isinstance(q, Exception):
            raise q
        return q
    early = EQ.EarlyEntryQuote(mint="MINT", quote_fn=quote_fn,
                               sizing_fn=lambda: (25.0, None, {"alt_clips": [alt]}), **kw)
    return early, calls


def test_the_regime_scaled_clip_is_quoted_beside_and_used_when_reserved():
    seen = []
    trace = SimpleNamespace(mark_substage=lambda name, **kw: seen.append((name, kw)))
    early, calls = _early_alt(trace=trace)
    q = early.take(18.75)
    assert q["outAmount"] == "375000000"
    assert sorted(calls) == [("MINT", 18.75), ("MINT", 25.0)]
    s = EQ.stats()
    assert (s["quoted"], s["used"], s["wasted_quotes"]) == (2, 1, 1), s
    (name, mark), = seen
    assert name == "early_quote" and mark["used"] is True and mark["clip"] == 18.75 and mark["alt"] is True


def test_the_full_clip_still_takes_the_first_quote():
    early, _ = _early_alt()
    assert early.take(25.0)["outAmount"] == QUOTE["outAmount"]


def test_a_clip_matching_neither_is_still_a_mismatch():
    early, _ = _early_alt()
    assert early.take(12.0) is None and EQ.stats()["discarded"] == {"clip_mismatch": 1}


@pytest.mark.parametrize("alt_quote,reason", [({}, "no_quote"), (RuntimeError("boom"), "error")])
def test_a_failed_scaled_quote_is_discarded(alt_quote, reason):
    early, _ = _early_alt(alt_quote=alt_quote)
    assert early.take(18.75) is None and EQ.stats()["discarded"] == {reason: 1}


def test_a_stale_scaled_quote_is_discarded():
    clock = Clock()
    early, _ = _early_alt(clock=clock)
    early._thread.join(5)
    early._alt_thread.join(5)
    clock.t += 1.6
    assert early.take(18.75, max_age_s=1.5) is None and EQ.stats()["discarded"] == {"stale": 1}


def _ledger(tmp_path):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    path = str(tmp_path / "ledger.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return path, conn


def test_at_market_scale_one_the_sizing_names_the_clip_a_hot_narrow_reservation_approves(
        monkeypatch, tmp_path):
    from sniper.services import memecoin_market_state as MS
    from test_memecoin_entry_identity import _arm
    _arm(monkeypatch)
    monkeypatch.setattr(MS, "market_risk_scale", lambda *a, **k: 1.0)
    monkeypatch.setattr(cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 0.75)
    path, conn = _ledger(tmp_path)
    pre = SimpleNamespace(balances=lambda: (300.0, 1.0, False, 5.0))
    clip, why, facts = R._early_quote_clip(SimpleNamespace(mint="So1Mint", signal_source="j7"), pre, path,
                                           with_alts=True)
    assert why is None and clip > 0
    (alt,) = facts["alt_clips_fn"]()
    # reserve_entry runs this envelope at classification.risk_scale (0.75 for a hot_narrow coin)
    from sniper.services.memecoin_risk import build_memecoin_risk_context, memecoin_entry_allowed
    import sqlite3
    ro = sqlite3.connect(path)
    ro.row_factory = sqlite3.Row
    ctx = build_memecoin_risk_context(ro, usdc_balance=300.0, sol_balance=1.0, day="2026-07-15",
                                      risk_scale=0.75)
    assert memecoin_entry_allowed(ctx, mint="So1Mint", trade_size=ctx.trade_size_usd).allowed
    assert alt == float(ctx.trade_size_usd) < clip


def test_at_a_scaled_market_no_second_clip_is_quoted(monkeypatch, tmp_path):
    from sniper.services import memecoin_market_state as MS
    from test_memecoin_entry_identity import _arm
    _arm(monkeypatch)
    monkeypatch.setattr(MS, "market_risk_scale", lambda *a, **k: 0.75)
    monkeypatch.setattr(cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 0.75)
    path, _conn = _ledger(tmp_path)
    pre = SimpleNamespace(balances=lambda: (300.0, 1.0, False, 5.0))
    clip, why, facts = R._early_quote_clip(SimpleNamespace(mint="So1Mint", signal_source="j7"), pre, path,
                                           with_alts=True)
    assert why is None and clip > 0 and facts["alt_clips_fn"]() == []


def test_the_first_quote_never_waits_for_the_scaled_clip_sizing():
    """Rule 2 (nothing slows the fill): the alternate sizing (~5 ms live) runs on its own thread."""
    release = threading.Event()
    calls = []

    def quote_fn(mint, usd):
        calls.append(usd)
        return dict(QUOTE)

    def slow_alts():
        release.wait(5)
        return [18.75]
    early = EQ.EarlyEntryQuote(mint="MINT", quote_fn=quote_fn,
                               sizing_fn=lambda: (25.0, None, {"alt_clips_fn": slow_alts}))
    began = time.monotonic()
    assert early.take(25.0) == QUOTE
    assert time.monotonic() - began < 1.0 and calls == [25.0]
    release.set()
    early._alt_thread.join(5)
    assert calls == [25.0, 18.75]


def test_a_scaled_clip_still_being_sized_is_waited_for_then_used():
    release = threading.Event()
    early = EQ.EarlyEntryQuote(mint="MINT", quote_fn=lambda mint, usd: dict(QUOTE, outAmount=str(int(usd))),
                               sizing_fn=lambda: (25.0, None, {"alt_clips_fn": lambda: release.wait(5) and [18.75]}))
    threading.Timer(0.05, release.set).start()
    assert early.take(18.75, max_wait_s=2.0)["outAmount"] == "18"


def test_every_alternate_is_registered_before_any_alternate_is_quoted():
    """Gamma NIT: take() iterates the alternates while their thread may still be quoting; the dict
    is published whole, once, so it never changes size under that iteration."""
    seen = []
    holder = {}

    def quote_fn(mint, usd):
        if usd != 25.0:
            seen.append((usd, sorted(holder["early"]._alts)))
        return dict(QUOTE)
    release = threading.Event()
    early = EQ.EarlyEntryQuote(mint="MINT", quote_fn=quote_fn, start=False,
                               sizing_fn=lambda: (25.0, None, {"alt_clips_fn": lambda: [18.75, 12.0]}))
    holder["early"] = early
    early.run()
    early._alt_thread.join(5)
    # The alternates are quoted concurrently (2026-10-08), so their order is not fixed.
    assert sorted(seen) == [(12.0, [12.0, 18.75]), (18.75, [12.0, 18.75])]


def test_the_alternates_are_quoted_concurrently_not_one_after_another():
    """2026-10-08 (above-nano clip, Alpha's condition): a second alternate (the raised clip beside the
    regime clip) must not add a quote round-trip to the first. Each alternate quotes on its own thread:
    here 18.75's quote completes only once 12.0's has started, which one thread could never do."""
    started_12 = threading.Event()

    def quote_fn(mint, usd):
        if usd == 12.0:
            started_12.set()
        if usd == 18.75:
            assert started_12.wait(2.0), "alternates were quoted one after another"
        return dict(QUOTE, outAmount=str(int(usd * 100)))
    early = EQ.EarlyEntryQuote(mint="MINT", quote_fn=quote_fn, start=False,
                               sizing_fn=lambda: (25.0, None, {"alt_clips_fn": lambda: [18.75, 12.0]}))
    began = time.monotonic()
    early.run()
    early._alt_thread.join(5)
    assert time.monotonic() - began < 1.0
    assert early.take(18.75)["outAmount"] == "1875"
