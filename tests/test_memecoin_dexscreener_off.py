"""DexScreener out, phase A: the per-consumer switch, the F1 consumer counters, E3 (an inert id),
and the funded exit sweep's own marks (X1 and X2).

Spec docs/superpowers/specs/2026-09-28-memecoin-dexscreener-out-phase-a-design.md, with Alpha's
decisions: E3 stays on, X1 and X2 ship together default off, E5 is deferred."""
import inspect
import threading
import time
from types import SimpleNamespace

import pytest

from sniper.execution import solana_quote as SQ
from sniper.services import memecoin_fastlane as FL
from sniper.services import memecoin_live_marks as LM
from sniper.services import memecoin_real_runner as R
from sniper.services.memecoin_solana_amounts import USDC, WSOL
from sniper.settings import memecoin as cfg


@pytest.fixture(autouse=True)
def _switch_empty(monkeypatch):
    monkeypatch.delenv("MEMECOIN_DEXSCREENER_OFF", raising=False)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# --- the switch ------------------------------------------------------------------------------------

def test_the_switch_reads_known_ids_at_call_time(monkeypatch, caplog):
    assert not any(cfg.dexscreener_off(i, env={}) for i in ("E3", "X1", "X2"))
    env = {"MEMECOIN_DEXSCREENER_OFF": " x1, X2 ,E99, E5, e9"}
    assert cfg.dexscreener_off("X1", env=env) and cfg.dexscreener_off("X2", env=env)
    assert not cfg.dexscreener_off("E3", env=env)
    # E5 is deferred (Alpha 2026-09-28) and E99 does not exist: both ignored, both named in a warning.
    assert not cfg.dexscreener_off("E5", env=env) and not cfg.dexscreener_off("E99", env=env)
    assert "E5" in caplog.text and "E99" in caplog.text
    # E9 joined the phase-A ids on 2026-09-29 (Alpha, D-E9); it used to be this test's unknown id.
    assert cfg.dexscreener_off("E9", env=env)
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E3")
    assert cfg.dexscreener_off("E3") and not cfg.dexscreener_off("X1")


def test_an_entry_can_name_one_chain_and_chain_names_are_canonical(caplog):
    env = {"MEMECOIN_DEXSCREENER_OFF": "X1:4663, x1:bsc, X2:base, X1:atlantis, E3:solana"}
    assert cfg.dexscreener_off("X1", "4663", env=env) and cfg.dexscreener_off("X1", "56", env=env)
    assert not cfg.dexscreener_off("X1", "8453", env=env) and not cfg.dexscreener_off("X1", "solana", env=env)
    assert cfg.dexscreener_off("X2", "8453", env=env) and not cfg.dexscreener_off("X2", "56", env=env)
    assert cfg.dexscreener_off("E3", "solana", env=env) and not cfg.dexscreener_off("E3", "56", env=env)
    assert not cfg.dexscreener_off("X1", env=env), "a chain entry is not a bare entry"
    assert "atlantis" in caplog.text
    bare = {"MEMECOIN_DEXSCREENER_OFF": "X1"}
    assert all(cfg.dexscreener_off("X1", c, env=bare) for c in ("solana", "4663", "56", None))


def test_a_per_chain_off_asks_only_for_the_other_chains(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X1:4663")
    monkeypatch.setattr(SQ, "dexscreener_cooldown_active", lambda *a, **k: False)
    asked = []

    def http(method, url):
        asked.append(url)
        return 200, {"pairs": []}, None
    SQ.dexscreener_stats_batch_keyed([("solana", "SolMint"), ("4663", "0xrobin")], http=http,
                                     funded=True, consumer="X1")
    assert len(asked) == 1 and "SolMint" in asked[0] and "0xrobin" not in asked[0]
    assert SQ.dexscreener_consumer_counts()["suppressed"].get("X1:4663", 0) >= 1


# --- F1: consumer tags and counters -----------------------------------------------------------------

def test_a_suppressed_consumer_makes_no_request_and_is_counted(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X1")
    before = SQ.dexscreener_consumer_counts()

    def boom(*a, **k):
        raise AssertionError("a suppressed consumer must not reach DexScreener")
    assert SQ.dexscreener_stats_batch_keyed([("solana", "Mint1")], http=boom, funded=True,
                                            consumer="X1") == {}
    after = SQ.dexscreener_consumer_counts()
    assert after["suppressed"].get("X1:solana", 0) == before["suppressed"].get("X1:solana", 0) + 1
    assert after["calls"].get("X1", 0) == before["calls"].get("X1", 0)


def test_a_tagged_call_is_counted_and_an_untagged_one_says_so(monkeypatch):
    monkeypatch.setattr(SQ, "dexscreener_cooldown_active", lambda *a, **k: False)
    before = SQ.dexscreener_consumer_counts()
    asked = []

    def http(method, url):
        asked.append(url)
        return 200, {"pairs": []}, None
    SQ.dexscreener_stats_batch_keyed([("solana", "Mint1")], http=http, funded=True, consumer="X2")
    SQ.dexscreener_stats_batch_keyed([("solana", "Mint1")], http=http, funded=True)
    after = SQ.dexscreener_consumer_counts()
    assert len(asked) == 2
    assert after["calls"].get("X2", 0) == before["calls"].get("X2", 0) + 1
    assert after["calls"].get("untagged", 0) == before["calls"].get("untagged", 0) + 1


# --- quote-asset USD from the live marks' own source: cache only, fail closed per asset -------------

NOW = "2026-09-29T00:30:00+00:00"
STOCK = "XsStockMint"


def _ago(seconds):
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(NOW) - timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def book(monkeypatch):
    """Fresh single-writer holders for SOL and the exotic quote assets."""
    sol, exotic = LM.SingleWriterHolder(), LM.QuoteBook()
    monkeypatch.setattr(LM, "SOL_USD", sol)
    monkeypatch.setattr(LM, "EXOTIC_USD", exotic)

    def put(sol_usd=None, sol_age=5.0, **assets):
        if sol_usd is not None:
            sol.publish(LM.SOL_USD_WRITER, {WSOL: sol_usd}, _ago(sol_age))
        for mint, (usd, age) in assets.items():
            exotic.publish(LM.EXOTIC_WRITER, mint, usd, _ago(age))
    return put


def test_fresh_quote_usd_prices_sol_usdc_and_other_quote_assets(book):
    book(sol_usd=150.5, **{STOCK: (250.0, 8.0)})
    assert LM.fresh_quote_usd({WSOL, USDC, STOCK, "NotAsked"}, now_iso=NOW) == {
        WSOL: 150.5, USDC: 1.0, STOCK: 250.0}
    assert LM.fresh_quote_usd({USDC}, now_iso=NOW) == {USDC: 1.0}, "only the asked mints"


def test_an_entry_older_than_the_bound_is_absent_per_asset(book):
    book(sol_usd=150.5, sol_age=LM.MAX_QUOTE_USD_AGE_S + 1, **{STOCK: (250.0, 10.0)})
    assert LM.fresh_quote_usd({WSOL, STOCK}, now_iso=NOW) == {STOCK: 250.0}


@pytest.mark.parametrize("sol", [19.9, 2000.1, 0.0, -1.0, float("nan"), float("inf")])
def test_an_implausible_sol_rate_is_absent(book, sol):
    LM.SOL_USD._value = ({WSOL: sol}, _ago(1))            # bypass publish's own positivity check
    assert WSOL not in LM.fresh_quote_usd({WSOL}, now_iso=NOW)


@pytest.mark.parametrize("usd", [0.0, -3.0, float("inf"), 1e7])
def test_an_implausible_other_rate_is_absent(book, usd):
    LM.EXOTIC_USD._value = {STOCK: (usd, _ago(1))}
    assert LM.fresh_quote_usd({STOCK}, now_iso=NOW) == {}


def test_an_undated_rate_is_absent_except_the_usdc_constant(book):
    LM.EXOTIC_USD._value = {STOCK: (250.0, None)}
    assert LM.fresh_quote_usd({STOCK, USDC}, now_iso=NOW) == {USDC: 1.0}


def test_a_writer_holding_its_lock_never_blocks_the_read(book):
    book(sol_usd=150.5, **{STOCK: (250.0, 1.0)})
    holding = threading.Event()
    release = threading.Event()

    def writer():
        with LM.EXOTIC_USD._claim, LM.SOL_USD._claim:
            holding.set()
            release.wait(5)
    th = threading.Thread(target=writer)
    th.start()
    try:
        holding.wait(5)
        started = time.monotonic()
        assert LM.fresh_quote_usd({WSOL, STOCK}, now_iso=NOW) == {WSOL: 150.5, STOCK: 250.0}
        assert time.monotonic() - started < 0.05
    finally:
        release.set()
        th.join(5)


def test_the_read_does_no_io(book, monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("fresh_quote_usd did I/O"))
    book(sol_usd=150.5)
    assert LM.fresh_quote_usd({WSOL}, now_iso=NOW) == {WSOL: 150.5}


# --- X2: the fast lane's map ------------------------------------------------------------------------

def test_x2_off_asks_no_dexscreener_and_prices_quote_assets_ourselves(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X2")
    monkeypatch.setattr(LM, "fresh_quote_usd", lambda mints, **k: {WSOL: 150.5, USDC: 1.0})
    marks, usd = FL._batched_mid_snapshot(
        [{"id": 1, "network_id": "solana", "mint": "Mint1"}], quote_mints={WSOL, USDC, "Xs"},
        batch_fn=lambda keys: pytest.fail("DexScreener asked"))
    assert marks == {} and usd == {WSOL: 150.5, USDC: 1.0}


def test_x2_on_is_todays_batch_tagged_x2(monkeypatch):
    seen = {}

    def batch(keys, **kw):
        seen.update(kw)
        return {("solana", "Mint1"): {"price": 2.0}, ("solana", WSOL): {"price": 150.0}}
    monkeypatch.setattr(SQ, "dexscreener_stats_batch_keyed", batch)
    marks, usd = FL._batched_mid_snapshot(
        [{"id": 1, "network_id": "solana", "mint": "Mint1"}], quote_mints={WSOL})
    assert seen == {"funded": True, "consumer": "X2"}
    assert marks[("solana", "Mint1")]["price"] == 2.0 and usd == {WSOL: 150.0}


# --- X1: the sweep's own read -----------------------------------------------------------------------

class _Cache:
    def __init__(self):
        self.asked = None

    def quote_mints(self):
        return {WSOL, "XsStock"}

    def fresh_price_map(self, quote_usd, *, now_ts, max_age_s):
        self.asked = dict(quote_usd)
        out = {}
        if WSOL in quote_usd:
            out[("solana", "Mint1")] = {"price": 0.01 * quote_usd[WSOL], "source": "fastlane_sol_swap"}
            out[("solana", "NotHeld")] = {"price": 1.0, "source": "fastlane_sol_swap"}
        return out


POSITIONS = [{"id": 1, "network_id": "solana", "mint": "Mint1"},
             {"id": 2, "network_id": "56", "mint": "0xab"}]


def test_x1_off_builds_the_sweep_map_from_our_own_marks(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X1")
    monkeypatch.setattr(SQ, "dexscreener_stats_batch_keyed",
                        lambda *a, **k: pytest.fail("DexScreener asked"))
    monkeypatch.setattr(LM, "fresh_quote_usd", lambda mints, **k: {WSOL: 150.0} if WSOL in mints else {})
    cache = _Cache()
    pm = R._sweep_price_map(POSITIONS, price_fn=None, swap_mark_cache=cache, now_ts=5.0)
    assert cache.asked == {WSOL: 150.0}, "an asset the source cannot price is absent"
    assert pm == {("solana", "Mint1"): {"price": 1.5, "source": "fastlane_sol_swap"}}, (
        "held positions only; 56 has no own mark and falls to the exact preview")


def test_x1_off_without_a_sol_rate_prices_nothing_rather_than_guessing(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X1")
    monkeypatch.setattr(LM, "fresh_quote_usd", lambda mints, **k: {})
    assert R._sweep_price_map(POSITIONS, price_fn=None, swap_mark_cache=_Cache(), now_ts=5.0) == {}


def test_x1_on_is_todays_batch_tagged_x1_and_a_given_map_wins(monkeypatch):
    seen = {}

    def batch(keys, **kw):
        seen.update(kw, keys=sorted(keys))
        return {}
    monkeypatch.setattr(SQ, "dexscreener_stats_batch_keyed", batch)
    R._sweep_price_map(POSITIONS, price_fn=None, swap_mark_cache=_Cache(), now_ts=5.0)
    assert seen["funded"] is True and seen["consumer"] == "X1" and len(seen["keys"]) == 2
    monkeypatch.setattr(SQ, "dexscreener_stats_batch_keyed", lambda *a, **k: pytest.fail("asked"))
    assert R._sweep_price_map(POSITIONS, price_fn=lambda mints: {"k": 1}, swap_mark_cache=None,
                              now_ts=5.0) == {"k": 1}


def test_the_exit_sweep_reads_its_map_through_the_helper():
    src = inspect.getsource(R.real_exit_sweep)
    assert "_sweep_price_map(" in src and "dexscreener_stats_batch_keyed(" not in src


# --- WO-4: "priceable" without a DexScreener mid -------------------------------------------------------

def _prepass(monkeypatch, *, last_exact_age_s, chain="solana"):
    import unittest.mock as mock
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    from sniper.execution.exit_preview import PreviewResult
    outcomes = []
    from datetime import datetime, timedelta, timezone
    now_dt = datetime(2026, 9, 28, 23, 59, tzinfo=timezone.utc)
    now = now_dt.isoformat()
    last_at = (None if last_exact_age_s is None
               else (now_dt - timedelta(seconds=last_exact_age_s)).isoformat())
    pos = {"id": 9, "mint": "Mint9" if chain == "solana" else "0x" + "9" * 40,
           "network_id": chain, "entry_price": 1.0,
           "last_executable_at": last_at, "last_executable_price": 1.0}
    preview = PreviewResult.failed(QuoteFailure.NO_ROUTE, provider_id="solana:jupiter",
                                   provider_group="solana:exact_quote")
    with mock.patch.object(R, "context_for_position", lambda c, p: {"preview_exit_fn": None}), \
         mock.patch("sniper.execution.solana_quote.quote_for", lambda pm, mint, net: {}), \
         mock.patch.object(R, "_resolve_override_and_mcap", lambda *a, **k: (None, None)), \
         mock.patch.object(R, "effective_trail", lambda pos, ov: (30.0, False)), \
         mock.patch.object(R, "_effective_derisk_pct", lambda pos, ov: 40.0), \
         mock.patch.object(R, "_executable_unit_price", lambda conn, pos, preview, **k: None), \
         mock.patch("sniper.analysis.memecoin_quote_failures.sweep_miss_verdicts",
                    lambda o: outcomes.extend(o) or {}):
        R._wo4_miss_prepass(
            [pos], conn=None, contexts={}, price_map={}, frozen_by_id={9: None}, engine_ids=set(),
            cfg=type("C", (), {"MEMECOIN_REAL_UNSELLABLE_SWEEPS": 0})(),
            preview_by_position={9: preview}, now_iso=now)
    return outcomes


def test_wo4_counts_a_failed_quote_on_a_recently_priced_position_when_the_mid_is_ours(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X1,X2")
    assert [o[0] for o in _prepass(monkeypatch, last_exact_age_s=100)] == [9]
    assert _prepass(monkeypatch, last_exact_age_s=1000) == [], "older than 900 s: a dead chart"
    assert _prepass(monkeypatch, last_exact_age_s=None) == [], "never priced: a dead chart"


def test_wo4_is_todays_filter_while_the_exit_switches_are_empty(monkeypatch):
    assert _prepass(monkeypatch, last_exact_age_s=100) == []


# --- E3: an inert id ---------------------------------------------------------------------------------

def _hook(monkeypatch, tmp_path):
    import sqlite3
    from test_memecoin_entry_identity import NOW, FakeClock, _arm, _event, _solana_context
    from sniper.persistence import db_memecoin as m
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_QUOTE", False, raising=False)
    _arm(monkeypatch)
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    seen = {}

    def entry(conn, ev, **kw):
        seen["reached_entry"] = True
        return {"status": "rejected", "reason": "stub"}
    monkeypatch.setattr(R, "real_entry", entry)
    monkeypatch.setattr(R, "_alert_entry_result", lambda *a, **k: None)
    monkeypatch.setattr(R, "chase_impact_allowance", lambda *a, **k: (None, "test"))
    trace = SimpleNamespace(attempt_id="attempt-e3", mark=lambda *a, **k: True,
                            finish=lambda *a, **k: True, mark_substage=lambda *a, **k: True)
    ev = _event(network_id="solana", mint="So1Mint", market_cap=100_000.0)   # their_price 0.25

    def stats(mint, network_id=None):
        return {"price": 0.60, "market_cap": 100_000.0}                      # +140% over theirs
    res = R.real_entry_hook(conn, ev, {"solana": _solana_context({})}, now_fn=lambda: NOW,
                            trade_size=10.0, max_age_s=120, stats_fn=stats, trace=trace,
                            trace_clock=FakeClock())
    return res, seen


def test_e3_stays_on_by_default(monkeypatch, tmp_path):
    res, seen = _hook(monkeypatch, tmp_path)
    assert res["status"] == "skipped_chase" and "already ran" in res["reason"] and not seen


def test_the_e3_id_turns_the_display_chase_off(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E3")
    res, seen = _hook(monkeypatch, tmp_path)
    assert seen.get("reached_entry"), res


# --- visibility ---------------------------------------------------------------------------------------

def test_the_heartbeat_carries_the_dexscreener_counters(tmp_path):
    import json
    from sniper.services import memecoin_runner as MR
    path = tmp_path / "hb.json"
    MR.write_heartbeat({"events": 0}, auth_ok=True, path=str(path),
                       dexscreener={"calls": {"X2": 3}, "suppressed": {"X1": 1}})
    assert json.loads(path.read_text())["dexscreener"] == {"calls": {"X2": 3}, "suppressed": {"X1": 1}}


def test_x2_off_on_one_evm_chain_keeps_solana_on_dexscreener(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X2:56")
    monkeypatch.setattr(LM, "fresh_quote_usd", lambda *a, **k: pytest.fail("own rate read"))
    asked = []

    def batch(keys):
        asked.extend(keys)
        return {("solana", "Mint1"): {"price": 2.0}, ("56", "0xab"): {"price": 9.0},
                ("solana", WSOL): {"price": 150.0}}
    marks, usd = FL._batched_mid_snapshot(
        [{"id": 1, "network_id": "solana", "mint": "Mint1"},
         {"id": 2, "network_id": "56", "mint": "0xab"}], quote_mints={WSOL}, batch_fn=batch)
    assert ("56", "0xab") not in asked and set(marks) == {("solana", "Mint1")} and usd == {WSOL: 150.0}


def test_x2_off_on_solana_prices_its_marks_ourselves(monkeypatch):
    """The fast lane's provider mid covers Solana only (`mid_scope`); EVM positions are priced there
    by their exact previews, so X2 on an EVM chain changes nothing and Solana is the real switch."""
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X2:solana")
    monkeypatch.setattr(LM, "fresh_quote_usd", lambda mints, **k: {WSOL: 151.0})
    marks, usd = FL._batched_mid_snapshot(
        [{"id": 1, "network_id": "solana", "mint": "Mint1"},
         {"id": 2, "network_id": "56", "mint": "0xab"}], quote_mints={WSOL},
        batch_fn=lambda keys: pytest.fail("DexScreener asked"))
    assert marks == {} and usd == {WSOL: 151.0}
    assert not FL.mid_scope("56") and FL.mid_scope("solana")


def test_x1_off_on_one_evm_chain_leaves_the_others_on_dexscreener(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X1:56")
    seen = {}

    def batch(keys, **kw):
        seen["keys"] = sorted(keys)
        return {("solana", "Mint1"): {"price": 3.0}}
    monkeypatch.setattr(SQ, "dexscreener_stats_batch_keyed", batch)
    pm = R._sweep_price_map(POSITIONS, price_fn=None, swap_mark_cache=_Cache(), now_ts=5.0)
    assert seen["keys"] == [("solana", "Mint1")] and pm == {("solana", "Mint1"): {"price": 3.0}}


def test_wo4_own_priceable_follows_the_chain(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X1:56,X2:56")
    assert _prepass(monkeypatch, last_exact_age_s=100) == [], "a Solana position: today's filter"
    assert [o[0] for o in _prepass(monkeypatch, last_exact_age_s=100, chain="56")] == [9]


def test_e3_off_on_another_chain_leaves_the_solana_chase_on(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E3:56")
    res, seen = _hook(monkeypatch, tmp_path)
    assert res["status"] == "skipped_chase" and not seen


def test_e3_off_on_solana_by_name(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E3:solana")
    res, seen = _hook(monkeypatch, tmp_path)
    assert seen.get("reached_entry"), res


def test_the_evm_only_activation_never_touches_the_sol_rate(monkeypatch, tmp_path):
    """Alpha's activation, X1 and X2 on 4663 and 56 only: no Solana consumer is off, so no quote-asset
    USD is ever asked for."""
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X1:4663,X2:4663,X1:56,X2:56")
    monkeypatch.setattr(LM, "fresh_quote_usd", lambda *a, **k: pytest.fail("SOL rate asked"))
    FL._batched_mid_snapshot(
        [{"id": 1, "network_id": "solana", "mint": "Mint1"},
         {"id": 2, "network_id": "56", "mint": "0xab"}], quote_mints={WSOL},
        batch_fn=lambda keys: {})
    monkeypatch.setattr(SQ, "dexscreener_stats_batch_keyed", lambda keys, **kw: {})
    R._sweep_price_map(POSITIONS + [{"id": 3, "network_id": "4663", "mint": "0xcd"}], price_fn=None,
                       swap_mark_cache=_Cache(), now_ts=5.0)
    _prepass(monkeypatch, last_exact_age_s=100)
    _prepass(monkeypatch, last_exact_age_s=100, chain="56")


def test_both_exit_maps_hand_the_adapter_their_own_clock(monkeypatch):
    """Gamma LOW: without a now_iso the adapter reads the clock itself; the sweep and the fast lane
    already hold one."""
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X1:solana,X2:solana")
    seen = []
    monkeypatch.setattr(LM, "fresh_quote_usd",
                        lambda mints, now_iso=None: seen.append(now_iso) or {WSOL: 150.0})
    FL._batched_mid_snapshot([{"id": 1, "network_id": "solana", "mint": "Mint1"}],
                             quote_mints={WSOL}, batch_fn=lambda keys: {},
                             now_iso_fn=lambda: "2026-09-29T00:40:00+00:00")
    R._sweep_price_map(POSITIONS, price_fn=None, swap_mark_cache=_Cache(), now_ts=5.0,
                       now_iso="2026-09-29T00:40:01+00:00")
    assert seen == ["2026-09-29T00:40:00+00:00", "2026-09-29T00:40:01+00:00"]
    call = inspect.getsource(R.real_exit_sweep).split("_sweep_price_map(")[1].split("# ")[0]
    assert "now_iso=now_iso" in call, "the sweep passes its own now_iso"


# --- (ii): the 4663 curve-mark fallback stays off where X1 is off (Beta R3, Alpha 2026-09-29) --------

def _curve_fallback(monkeypatch):
    reads = []
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "evm_rpc_for_chain", lambda chain: "http://custody.invalid")
    import sniper.execution.evm_launchpad_curve as curve
    monkeypatch.setattr(curve, "mark_price_entry",
                        lambda chain, mint, rpc, observed_at=None: reads.append(mint) or {"price": 1.0})
    positions = [{"id": 1, "network_id": "4663", "mint": "0x" + "1" * 40},
                 {"id": 2, "network_id": "4663", "mint": "0x" + "2" * 40}]
    out = R._launchpad_curve_price_fallback(positions, {}, "2026-09-29T00:40:00+00:00")
    return reads, out


def test_the_curve_fallback_is_todays_while_x1_is_on_for_4663(monkeypatch):
    reads, out = _curve_fallback(monkeypatch)
    assert len(reads) == 2 and len(out) == 2


def test_the_curve_fallback_makes_no_read_where_x1_is_off(monkeypatch):
    """With X1 off on 4663 every 4663 position is absent from the sweep's map, so the fallback would
    make one serial read on the funded RPC per position per sweep. Exact-policy decisions never read
    it; the display falls to the exact price."""
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X1:4663,X1:56")
    reads, out = _curve_fallback(monkeypatch)
    assert reads == [] and out == {}
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X1:56")
    reads, _out = _curve_fallback(monkeypatch)
    assert len(reads) == 2, "another chain's switch leaves 4663's fallback as today"


# --- E6: liquidity is telemetry; its switch changes what is recorded, never a decision ----------------
# Spec docs/superpowers/specs/2026-09-29-memecoin-dexscreener-out-e6-e7-design.md (Alpha GO: all four
# chains; st["liquidity"] untouched because the dust-pair check, E4, reads it to null prices).

def test_e6_is_a_per_chain_switch_id(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E6:4663")
    assert cfg.dexscreener_off("E6", "4663") and not cfg.dexscreener_off("E6", "solana")


def _own_depth(monkeypatch, value):
    from sniper.persistence import db_memecoin as m
    monkeypatch.setattr(cfg, "MEMECOIN_POOL_DEPTH_MAX_AGE_S", 600.0, raising=False)
    monkeypatch.setattr(m, "pool_depth_usd", lambda *a, **k: value)


def test_e6_off_takes_the_classification_liquidity_from_our_own_sources(monkeypatch):
    provider = {"liquidity": 50_000.0, "price": 0.6}
    _own_depth(monkeypatch, 50.0)
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E6")
    out = R._funded_market_stats(provider, "2026-09-29T02:30:00+00:00", conn=object(),
                                 chain_id="4663", mint="0xab")
    assert out["liquidity"] == 50.0 and out["liquidity_source"] == R._pool_depth_source()
    assert provider["liquidity"] == 50_000.0, "the caller's snapshot (E4's input) is never touched"
    _own_depth(monkeypatch, None)
    ev = SimpleNamespace(implied_quote_reserve_usd=200.0, implied_quote_leg="pump_amm")
    out = R._funded_market_stats(provider, "2026-09-29T02:30:00+00:00", conn=object(),
                                 chain_id="solana", mint="So1", ev=ev)
    assert out["liquidity"] == 200.0 and out["liquidity_source"] == "implied_source_trade:pump_amm"
    out = R._funded_market_stats(provider, "2026-09-29T02:30:00+00:00", conn=object(),
                                 chain_id="8453", mint="0xcd")
    assert out.get("liquidity") is None and out.get("liquidity_source") is None, "no own source: blank"


def test_e6_on_keeps_the_provider_liquidity(monkeypatch):
    _own_depth(monkeypatch, 50.0)
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E6:solana")
    out = R._funded_market_stats({"liquidity": 50_000.0}, "2026-09-29T02:30:00+00:00",
                                 conn=object(), chain_id="4663", mint="0xab")
    assert out["liquidity"] == 50_000.0 and out.get("liquidity_source") is None


_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
_CURVE = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


@pytest.mark.parametrize("valuation, want, leg", [
    ({"source_impact_ratio": 1.5, "source_size_usd": 100.0, "program_id": _AMM}, 200.0, "pump_amm"),
    ({"source_impact_ratio": 1.5, "source_size_usd": 100.0, "impact_leg": "raydium_cpmm"}, 200.0,
     "raydium_cpmm"),
    # Gamma LOW 1: Q = spent / (r - 1) holds on constant-product legs only. The pump curve's r is
    # over VIRTUAL reserves, and a launchlab or unlabelled leg is some other reserve: unknown.
    ({"source_impact_ratio": 1.5, "source_size_usd": 100.0, "program_id": _CURVE}, None, None),
    ({"source_impact_ratio": 1.5, "source_size_usd": 100.0, "impact_leg": "raydium_launchlab"},
     None, None),
    ({"source_impact_ratio": 1.5, "source_size_usd": 100.0}, None, None),
    ({"source_impact_ratio": 1.0, "source_size_usd": 100.0, "program_id": _AMM}, None, None),
    ({"source_impact_ratio": 0.9, "source_size_usd": 100.0, "program_id": _AMM}, None, None),
    ({"source_impact_ratio": 1.5, "source_size_usd": 0, "program_id": _AMM}, None, None),
    ({}, None, None),
])
def test_the_onchain_event_carries_the_quote_reserve_its_source_trade_implies(valuation, want, leg):
    import json
    from sniper.services.memecoin_onchain_lane import OnchainEv
    row = {"mint": "So1", "source_user_id": "u1", "handle": "h", "network_id": "solana",
           "source_price": 0.1, "signature": "sig", "detected_at": "2026-09-29T02:30:00+00:00",
           "token_delta": 1000.0, "sol_spent": None, "source_size_usd": 100.0,
           "block_time": None, "source_valuation_json": json.dumps(valuation)}
    ev = OnchainEv(row)
    assert ev.implied_quote_reserve_usd == (pytest.approx(want) if want is not None else None)
    assert ev.implied_quote_leg == leg


def test_e6_off_feeds_the_chase_telemetry_the_classification_number(monkeypatch):
    classification = SimpleNamespace(facts={"liquidity": 50.0})
    st = {"liquidity": 50_000.0}
    assert R._telemetry_liquidity(st, classification, "4663") == 50_000.0
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E6:4663")
    assert R._telemetry_liquidity(st, classification, "4663") == 50.0
    assert R._telemetry_liquidity(st, None, "4663") is None


_REAL_FUNDED_STATS = R._funded_market_stats


def _e6_hook(monkeypatch, tmp_path, *, e6_off, price):
    """The same coin entered with E6 on (provider $50,000) or off (own $50). A $1,000 dust floor
    makes E4 matter: were the own number to reach st["liquidity"], E4 would null the display price
    and the chase gate would stop refusing."""
    import sqlite3
    from test_memecoin_entry_identity import NOW, FakeClock, _arm, _event, _solana_context
    from sniper.persistence import db_memecoin as m
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_QUOTE", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SNAPSHOT_MIN_PAIR_LIQUIDITY_USD", 1000.0, raising=False)
    _own_depth(monkeypatch, 50.0)
    if e6_off:
        monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E6")
    else:
        monkeypatch.delenv("MEMECOIN_DEXSCREENER_OFF", raising=False)
    _arm(monkeypatch)
    conn = sqlite3.connect(str(tmp_path / ("ledger%d%s.db" % (e6_off, price))))
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    seen = {}

    def entry(conn, ev, **kw):
        seen["entry_kwargs"] = {k: v for k, v in kw.items()
                                if isinstance(v, (int, float, str, bool, type(None)))}
        c = kw.get("classification")
        seen["classification"] = c.to_dict() if c is not None else None
        return {"status": "rejected", "reason": "stub"}
    monkeypatch.setattr(R, "real_entry", entry)
    monkeypatch.setattr(R, "_alert_entry_result", lambda *a, **k: None)
    monkeypatch.setattr(R, "chase_impact_allowance", lambda *a, **k: (None, "test"))
    real_stats = _REAL_FUNDED_STATS            # never a previous run's wrapper

    def record(stats, *a, **k):
        out = real_stats(stats, *a, **k)
        if k.get("chain_id"):          # the classification's calls (a pre-fetch one has no stats)
            seen.setdefault("classification_liquidity", []).append(
                ((stats or {}).get("liquidity"), out.get("liquidity"), out.get("liquidity_source")))
        return out
    monkeypatch.setattr(R, "_funded_market_stats", record)
    subs = []
    trace = SimpleNamespace(attempt_id=None, mark=lambda *a, **k: True,
                            finish=lambda *a, **k: True,
                            mark_substage=lambda name, **k: subs.append((name, k.get("allowed"))) or True)
    ev = _event(network_id="solana", mint="So1Mint", market_cap=100_000.0)   # their_price 0.25
    res = R.real_entry_hook(conn, ev, {"solana": _solana_context({})}, now_fn=lambda: NOW,
                            trade_size=10.0, max_age_s=120, trace=trace, trace_clock=FakeClock(),
                            stats_fn=lambda mint, network_id=None: {
                                "price": price, "market_cap": 100_000.0, "liquidity": 50_000.0},
                            # a context WITHOUT market_stats: classification runs, stats are fetched
                            classification_context={"effective_mcap": 100_000.0,
                                                    "assumed_micro": False, "consensus_n_60m": 0,
                                                    "mcap_basis": "event"})
    return res, seen, subs


def test_e6_off_never_reaches_the_dust_check(monkeypatch, tmp_path):
    """The E4 trap: a display price 2.4x the source fill is refused by the chase gate with E6 on and
    off alike. Own liquidity in st["liquidity"] would null that price and admit the coin."""
    on_res, _on, on_subs = _e6_hook(monkeypatch, tmp_path, e6_off=False, price=0.60)
    off_res, _off, off_subs = _e6_hook(monkeypatch, tmp_path, e6_off=True, price=0.60)
    assert on_res["status"] == off_res["status"] == "skipped_chase", (on_res, off_res)
    assert on_res == off_res and on_subs == off_subs


def test_e6_off_changes_telemetry_only(monkeypatch, tmp_path):
    on_res, on_seen, on_subs = _e6_hook(monkeypatch, tmp_path, e6_off=False, price=0.25)
    off_res, off_seen, off_subs = _e6_hook(monkeypatch, tmp_path, e6_off=True, price=0.25)
    assert on_res == off_res, (on_res, off_res)
    assert on_subs == off_subs, "every gate decided the same"
    assert on_seen.get("entry_kwargs") == off_seen.get("entry_kwargs"), "same clip and sizing"

    def decided(c):            # the classification without its liquidity telemetry
        c = dict(c)
        c["facts"] = {k: v for k, v in c["facts"].items() if k not in ("liquidity", "liquidity_source")}
        c["reason_codes"] = [r for r in c["reason_codes"] if not r.startswith("liquidity:")]
        return c
    assert decided(on_seen["classification"]) == decided(off_seen["classification"])
    on_fetched = [c for c in on_seen["classification_liquidity"] if c[0] == 50_000.0]
    off_fetched = [c for c in off_seen["classification_liquidity"] if c[0] == 50_000.0]
    assert on_fetched and on_fetched[-1][1:] == (50_000.0, None), on_seen
    assert off_fetched and off_fetched[-1][1:] == (50.0, R._pool_depth_source()), off_seen


# --- E7: the sell-share gate reads our own tape's split on the tape chains ------------------------------

_OWN = {"usd": 900.0, "reason": "ok", "covered_s": 300.0,
        "pools": [{"pool": "0xpair", "usd": 800.0, "swaps": 10, "buys": 1, "sells": 9}]}
_DEX = {"txns_m5_buys": 5, "txns_m5_sells": 5}


def test_e7_on_is_todays_dexscreener_counts_and_records_ours_beside_them(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS", {"4663", "8453"}, raising=False)
    got, basis, own = R._sell_share_input(_DEX, _OWN, "4663")
    assert got is _DEX and basis == "m5_txn_counts" and own["txns_m5_sells"] == 9


def test_e7_off_on_a_tape_chain_reads_our_split(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS", {"4663", "8453"}, raising=False)
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E7:4663")
    got, basis, own = R._sell_share_input(_DEX, _OWN, "4663")
    assert basis == "onchain_pool_counts" and got == own
    from sniper.services.memecoin_sell_share import sell_share_decision
    assert sell_share_decision(got, cap_pct=80, min_txns=8)[0] is False, "9 of 10 sells refuses"
    assert sell_share_decision(_DEX, cap_pct=80, min_txns=8)[0] is True, "DexScreener's 50% admits"


def test_e7_off_with_no_measured_window_admits_as_a_missing_dexscreener_field_does(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS", {"4663", "8453"}, raising=False)
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E7")
    got, basis, own = R._sell_share_input(_DEX, {"usd": None, "reason": "protocol_lagging"}, "8453")
    assert own is None and basis == "onchain_pool_counts" and got == {}
    from sniper.services.memecoin_sell_share import sell_share_decision
    assert sell_share_decision(got, cap_pct=80, min_txns=8)[0] is True


def test_e7_off_on_a_chain_without_our_tape_keeps_dexscreener(monkeypatch):
    """Solana stays on DexScreener (Alpha): a bare E7 must not make its gate stop gating. 56 has its
    own tape since entry-gates step 4 (the BSC observer's source-pair split,
    tests/test_memecoin_bsc_own_pool_e7.py)."""
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS", {"4663", "8453"}, raising=False)
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E7")
    for chain in ("solana",):
        got, basis, _own = R._sell_share_input(_DEX, None, chain)
        assert got is _DEX and basis == "m5_txn_counts_no_own_tape"


def test_the_gate_reads_its_input_through_the_helper():
    import inspect
    src = inspect.getsource(R.real_entry_hook)
    assert "_sell_share_input(st, _onchain_m5, identity.network_id)" in src


def _e7_hook(monkeypatch, tmp_path, *, e7_off):
    """One coin through the entry hook with DexScreener's m5 counts at 50% sells and our own tape's
    pair at 90% sells (9 of 10)."""
    import sqlite3
    from test_memecoin_entry_identity import NOW, FakeClock, _arm, _event, _solana_context
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_volume_writer as vw
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_QUOTE", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_SELL_SHARE_PCT", 80.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SELL_SHARE_MIN_TXNS", 8, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS", {"solana"}, raising=False)
    if e7_off:
        monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E7:solana")
    else:
        monkeypatch.delenv("MEMECOIN_DEXSCREENER_OFF", raising=False)
    monkeypatch.setattr(vw, "gate_evidence", lambda *a, **k: dict(_OWN))
    _arm(monkeypatch)
    conn = sqlite3.connect(str(tmp_path / ("e7ledger%d.db" % e7_off)))
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    monkeypatch.setattr(R, "real_entry", lambda conn, ev, **kw: {"status": "rejected", "reason": "stub"})
    monkeypatch.setattr(R, "_alert_entry_result", lambda *a, **k: None)
    monkeypatch.setattr(R, "chase_impact_allowance", lambda *a, **k: (None, "test"))
    subs = {}
    trace = SimpleNamespace(attempt_id=None, mark=lambda *a, **k: True, finish=lambda *a, **k: True,
                            mark_substage=lambda name, **k: subs.setdefault(name, k) or True)
    ev = _event(network_id="solana", mint="So1Mint", market_cap=100_000.0)
    res = R.real_entry_hook(conn, ev, {"solana": _solana_context({})}, now_fn=lambda: NOW,
                            trade_size=10.0, max_age_s=120, trace=trace, trace_clock=FakeClock(),
                            stats_fn=lambda mint, network_id=None: {
                                "price": 0.25, "market_cap": 100_000.0, **_DEX})
    return res, subs


def test_e7_off_the_gate_refuses_on_our_split_and_on_records_it(monkeypatch, tmp_path):
    on_res, on_subs = _e7_hook(monkeypatch, tmp_path, e7_off=False)
    off_res, off_subs = _e7_hook(monkeypatch, tmp_path, e7_off=True)
    assert on_subs["sell_share"]["allowed"] is True and on_subs["sell_share"]["basis"] == "m5_txn_counts"
    assert on_subs["sell_share"]["own_share_pct"] == pytest.approx(90.0), "shadow recorded while on"
    assert off_res["status"] == "rejected" and "sell share 90%" in off_res["reason"], off_res
    assert off_subs["sell_share"]["basis"] == "onchain_pool_counts"
    assert off_subs["sell_share"]["own_pool"] == "0xpair"
