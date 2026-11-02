"""The bound pool is the stop's native rung on Solana positions (2026-10-17, Alpha).

Fixture: position 73927 Gigabite (6tsykNmR...pump), slot-lane exotic pump_amm entry through pool
9ymRdTVk... quoted in SPCX (6 decimals, ~$145.40), $86.31 at 1.312978e-04 for 657,360.35065 tokens.
16 s later the Jupiter whole-bag quote read 5.963e-05 (-54.6%) while the entry pool read 1.275e-04
(-2.9%), and b_stop sold for 38.01 USDC (-56%). The pool reserves below are chosen to price the
token at that 1.275e-04 (SPCX per token 8.769e-07, 300M tokens deep)."""
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_native_exit_rung as R
_REAL_FETCH = R._fetch_virtual_quote
from sniper.services import memecoin_real_runner as rr
from sniper.settings import memecoin as cfg

MINT = "6tsykNmR3WDqouj9uVUFZRgR2Kpai4F1Qm1gAcirpump"
SPCX = "SPCXxcqXj6e5dJDVNovHN8744zkbhM2bYudU45BimGb"
POOL = "9ymRdTVkUxipX9cXK6XhmCAsirC57cWeF5oC3HrM26GQ"
ENTRY_PRICE, NOTIONAL = 1.312978489114173e-04, 86.31
QTY = 657360.35065
ATOMIC = 657360350650
SPCX_USD = 145.40
COIN_ATOMS = 300_000_000 * 10 ** 6
QUOTE_ATOMS = int(300_000_000 * (1.275e-04 / SPCX_USD) * 10 ** 6)      # SPCX, 6 decimals
JUPITER_USD = 5.962975856581653e-05 * QTY                                # 39.20


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_NATIVE_RUNG", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_NATIVE_RUNG_DIVERGENCE_PCT", 25.0)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_NATIVE_RUNG_FEE_HIGH_BPS", 125)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_NATIVE_RUNG_FEE_LOW_BPS", 25)
    monkeypatch.setattr(R, "_fetch_virtual_quote", lambda pool: 0)      # never the network here
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)
    from sniper.services import memecoin_live_marks as lm
    monkeypatch.setattr(lm, "fresh_quote_usd",
                        lambda mints, now_iso=None: {SPCX: SPCX_USD, R.WSOL_MINT: SPCX_USD, R.USDC_MINT: SPCX_USD})
    # 73927 decided on the exact preview (its frozen follow-30-50-v2 contract has mirror off).
    monkeypatch.setattr(rr, "exact_price_relevant", lambda *a, **k: True)
    R._HELD_SINCE.clear()
    R._VIRTUAL_QUOTE.clear()
    R._VIRTUAL_QUOTE[POOL] = 0          # the fixture pool, verified PumpSwap (as the background read finds)
    yield
    R._HELD_SINCE.clear()
    R._VIRTUAL_QUOTE.clear()


class Cache:
    def __init__(self, pair=True, quote_atoms=QUOTE_ATOMS, quote_mint=SPCX, age=0.4):
        self.pair, self.quote_atoms, self.quote_mint, self.age = pair, quote_atoms, quote_mint, age

    def bindings_by_mint(self):
        return {MINT: self.quote_mint}

    def exit_rung_pair(self, mint, *, now_ts, max_age_s):
        if not self.pair or self.age > max_age_s:
            return None
        return {"pool": POOL, "quote_mint": self.quote_mint, "quote_decimals": 6, "coin_atoms": COIN_ATOMS,
                "quote_atoms": self.quote_atoms, "slot": 371_000_000, "seen_age_s": self.age}


def _ledger():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = m.open_position(
        conn, source_user_id="u", source_handle="DexGemsReal", mint=MINT, ticker="Gigabite",
        entry_price=ENTRY_PRICE, entry_notional=NOTIONAL, entry_fill_source="chain", entry_lag_s=0,
        their_size_usd=0, opened_at="2026-10-17T15:00:00+00:00", strategy="alpha_b",
        execution_mode="real", network_id="solana")
    m.insert_fill(conn, position_id=pid, kind="entry", price=ENTRY_PRICE, qty=QTY, notional=NOTIONAL,
                  fill_source="chain", fraction=1.0, created_at="2026-10-17T15:00:00+00:00")
    return conn, pid


def _sweep(conn, cache, usd, display=1.275e-04):
    calls = []
    context = {
        "preview_exit_fn": lambda _p: SimpleNamespace(expected_usdc=usd, amount_atomic=ATOMIC, route_id="r",
                                                      quoted_at="2026-10-17T15:59:54+00:00"),
        "exit_fn": lambda _c, _p, reason, _n: calls.append(reason) or {"status": "unknown"},
        "decimals_fn": lambda _mint: 6}
    rr.real_exit_sweep(conn, {"solana": context}, price_fn=lambda _m: {MINT: {"price": display}},
                       now_fn=lambda: "2026-10-17T15:59:55+00:00", reserve_witness_cache=cache)
    return calls


def _audit(conn, pid):
    import json
    row = conn.execute("SELECT allowed, detail FROM memecoin_exit_trigger_audit WHERE position_id=? "
                       "AND reason='b_stop' ORDER BY rowid DESC", (pid,)).fetchone()
    return row["allowed"], json.loads(row["detail"])


def test_the_73927_stop_does_not_fire_on_the_phantom_jupiter_price_while_the_pool_reads_healthy():
    conn, pid = _ledger()
    assert _sweep(conn, Cache(), JUPITER_USD) == []
    allowed, detail = _audit(conn, pid)
    assert allowed == 0 and detail["code"] == "route_divergent"
    assert detail["rung_jupiter_usd"] == pytest.approx(39.20, abs=0.01)
    assert detail["rung_native_low_usd"] == pytest.approx(82.6, abs=0.6)     # ~-4%: no stop
    assert detail["rung_display_price"] == 1.275e-04 and detail["rung_pool"] == POOL
    assert detail["rung_pool_age_s"] == 0.4 and "rung_jupiter_age_s" in detail
    assert detail["rung_decided"] == "native", "the trace names the rung that decided"
    assert detail["rung_mark_unit"] == pytest.approx(1.275e-04, rel=1e-3), "the on-chain mark at decision"


def test_a_dump_the_bound_pool_confirms_still_stops():
    conn, _pid = _ledger()
    assert _sweep(conn, Cache(quote_atoms=QUOTE_ATOMS // 3), JUPITER_USD) == ["b_stop"]


def test_the_stop_fires_when_jupiter_recovers_to_within_the_divergence_of_the_pool():
    conn, pid = _ledger()
    pool = Cache(quote_atoms=int(QUOTE_ATOMS * 0.73))     # the pool itself at about -30%: above the stop
    assert _sweep(conn, pool, JUPITER_USD) == []          # Jupiter -54.6%: diverged, held
    assert _audit(conn, pid)[1]["code"] == "route_divergent"
    near = 0.60 * NOTIONAL                                 # Jupiter -40%: within 25% of the pool rung
    assert _sweep(conn, pool, near) == ["b_stop"]
    assert _audit(conn, pid)[1]["rung_verdict"] == "rungs_agree"


def test_an_unreadable_or_stale_pool_keeps_the_stop_and_audits_route_unreadable():
    """Gamma BLOCK B2: no native evidence, no hold (the display never decides)."""
    conn, pid = _ledger()
    assert _sweep(conn, Cache(age=120.0), JUPITER_USD) == ["b_stop"]   # the pool's read is stale
    assert _audit(conn, pid)[1]["rung_verdict"] == "route_unreadable"
    conn, _pid = _ledger()
    assert _sweep(conn, Cache(pair=False), JUPITER_USD) == ["b_stop"], "no bound pool: today's stop"


def test_catastrophe_stays_on_the_worst_rung():
    conn, _pid = _ledger()
    assert _sweep(conn, Cache(), 0.15 * NOTIONAL) == ["b_stop"]          # -85% on Jupiter: always out


def test_a_sol_pool_hold_is_bounded_by_the_unquotable_window(monkeypatch):
    conn, _pid = _ledger()
    clock = [1000.0]
    monkeypatch.setattr(rr.time, "monotonic", lambda: clock[0])
    sol = Cache(quote_mint=R.WSOL_MINT)
    assert _sweep(conn, sol, JUPITER_USD) == []
    clock[0] += 899.0
    assert _sweep(conn, sol, JUPITER_USD) == []
    clock[0] += 2.0
    assert _sweep(conn, sol, JUPITER_USD) == ["b_stop"]


def test_an_exotic_pool_hold_is_capped_at_the_exotic_window(monkeypatch):
    """Gamma LOW: the native value never prices the SPCX -> SOL leg Jupiter may be pricing."""
    conn, pid = _ledger()
    clock = [1000.0]
    monkeypatch.setattr(rr.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_NATIVE_RUNG_EXOTIC_HOLD_S", 120.0)
    assert _sweep(conn, Cache(), JUPITER_USD) == []
    assert _audit(conn, pid)[1]["rung_max_hold_s"] == 120.0
    clock[0] += 121.0
    assert _sweep(conn, Cache(), JUPITER_USD) == ["b_stop"]


def test_the_pessimistic_bound_past_the_stop_fires_even_when_the_optimistic_one_is_above():
    """Gamma BLOCK B1: a hold needs even the low bound above the stop."""
    fire, detail = R.judge(1, jupiter_usd=25.0, native_low=50.0, native_high=70.0, quantity=100.0,
                           display_price=None, entry_price=1.0, stop_pct=35.0, catastrophe_pct=80.0,
                           divergence_pct=25.0, max_hold_s=900.0, now_ts=0.0)
    assert fire and detail["rung_verdict"] == "native_confirms_stop"       # low -50%, high -30%
    fire, detail = R.judge(2, jupiter_usd=25.0, native_low=70.0, native_high=80.0, quantity=100.0,
                           display_price=None, entry_price=1.0, stop_pct=35.0, catastrophe_pct=80.0,
                           divergence_pct=25.0, max_hold_s=900.0, now_ts=0.0)
    assert not fire and detail["code"] == "route_divergent"               # low -30%: above the stop


def test_a_sol_quoted_pool_gets_the_same_rung_and_the_kill_knob_restores_todays_stop(monkeypatch):
    conn, pid = _ledger()
    assert _sweep(conn, Cache(quote_mint=R.WSOL_MINT), JUPITER_USD) == []      # 73930-shaped: SOL pool
    assert _audit(conn, pid)[1]["code"] == "route_divergent"
    conn, _pid = _ledger()
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_NATIVE_RUNG", False)
    assert _sweep(conn, Cache(), JUPITER_USD) == ["b_stop"]


def test_the_band_bounds():
    pair = {"coin_atoms": 1_000_000, "quote_atoms": 1_000_000, "quote_decimals": 6}
    low, high = R.band(pair, 10_000, 1.0, fee_low_bps=25, fee_high_bps=125, virtual=500_000)
    assert low < high, "virtual quote and the lower fee only raise the optimistic bound"
    for unknown in (None, R.NOT_PUMPSWAP):
        assert R.band(pair, 10_000, 1.0, fee_low_bps=25, fee_high_bps=125, virtual=unknown) == (None, None)
    assert R.native_rung_usd(pair, 10 ** 12, 1.0, 0, 10 ** 9) <= 1.0, "never more than the real reserve"


@pytest.mark.parametrize("owner, data_ok, want", [
    ("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA", True, 1234),
    ("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo", True, R.NOT_PUMPSWAP),      # Meteora DLMM
    ("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA", False, R.NOT_PUMPSWAP),    # not a Pool account
])
def test_only_a_verified_pumpswap_pool_gets_a_rung(monkeypatch, owner, data_ok, want):
    import base64
    import struct
    from sniper.execution import pumpswap_buy as PS
    from sniper.execution import solana_real_seams as S
    raw = bytearray(PS.POOL_VIRTUAL_QUOTE_OFFSET + 16)
    if data_ok:
        raw[:8] = PS.POOL_ACCOUNT_DISCRIMINATOR
    struct.pack_into("<q", raw, PS.POOL_VIRTUAL_QUOTE_OFFSET, 1234)
    body = {"result": {"value": {"owner": owner, "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}}}
    monkeypatch.setattr(S, "_rpc_call", lambda url, method, params, http: (200, body))
    monkeypatch.setattr(cfg, "solana_read_rpc", lambda: "https://read.invalid")
    assert _REAL_FETCH(POOL) == want      # the fixture stubs the module attribute; this is the real one


def test_the_virtual_quote_is_read_once_in_the_background_never_on_the_sweep():
    import threading
    gate, calls = threading.Event(), []

    def fetch(pool):
        calls.append(pool)
        gate.wait(2)
        return 1234
    assert R.virtual_quote("P1", fetch_fn=fetch) is None, "unknown now: never a wait"
    assert R.virtual_quote("P1", fetch_fn=fetch) is None and len(calls) <= 1, "one read in flight"
    gate.set()
    for _ in range(200):
        if R._VIRTUAL_QUOTE.get("P1") == 1234:
            break
        import time
        time.sleep(0.01)
    assert R.virtual_quote("P1", fetch_fn=fetch) == 1234 and calls == ["P1"]


def test_a_blind_jupiter_records_what_the_native_rung_alone_says():
    import json
    conn, pid = _ledger()
    calls = []
    context = {"preview_exit_fn": lambda _p: None, "decimals_fn": lambda _m: 6,
               "exit_fn": lambda _c, _p, reason, _n: calls.append(reason) or {"status": "unknown"}}
    blind = Cache(quote_atoms=QUOTE_ATOMS // 3)          # the pool is at about -68%
    rr.real_exit_sweep(conn, {"solana": context}, price_fn=lambda _m: {MINT: {"price": 1.275e-04}},
                       now_fn=lambda: "2026-10-17T15:59:55+00:00", reserve_witness_cache=blind)
    assert calls == [], "phase A never sells on the native rung"
    row = conn.execute("SELECT detail FROM memecoin_exit_trigger_audit WHERE position_id=? AND "
                       "reason='rails_blind'", (pid,)).fetchone()
    native = json.loads(row["detail"])["native"]
    assert native["native_stop"] is True and native["pool"] == POOL and native["native_best_pnl_pct"] < -35


def test_no_cache_or_a_failing_rung_keeps_the_stop(monkeypatch):
    conn, _pid = _ledger()
    assert _sweep(conn, None, JUPITER_USD) == ["b_stop"]
    conn, _pid = _ledger()

    class Broken(Cache):
        def exit_rung_pair(self, *a, **k):
            raise RuntimeError("boom")
    assert _sweep(conn, Broken(), JUPITER_USD) == ["b_stop"]


def test_the_cache_reader_returns_one_fresh_amm_pool_only():
    from sniper.services.memecoin_solana_swap_marks import PoolBinding, SolanaSwapMarkCache
    cache = SolanaSwapMarkCache()
    row = {"kind": "sol_sync", "pool": POOL, "block_no": 5, "amount0": COIN_ATOMS,
           "amount1": QUOTE_ATOMS, "decimals0": 6, "decimals1": 6}
    assert cache.record(row, PoolBinding(MINT, POOL, "coin", "quote", SPCX),
                        now_ts=100.0, observed_at="2026-10-17T15:59:55+00:00")
    got = cache.exit_rung_pair(MINT, now_ts=110.0, max_age_s=60)
    assert got["pool"] == POOL and got["quote_atoms"] == QUOTE_ATOMS and got["seen_age_s"] == 10.0
    assert cache.exit_rung_pair(MINT, now_ts=200.0, max_age_s=60) is None, "stale"
    assert cache.record(dict(row, pool="OtherPool", block_no=6),
                        PoolBinding(MINT, "OtherPool", "coin2", "quote2", SPCX),
                        now_ts=100.0, observed_at="2026-10-17T15:59:55+00:00")
    assert cache.exit_rung_pair(MINT, now_ts=110.0, max_age_s=60) is None, "two pools: ambiguous"


def test_an_unverified_or_not_yet_verified_pool_keeps_todays_stop():
    for verdict in (R.NOT_PUMPSWAP, None):            # e.g. a Meteora DLMM pool bound as `amm`
        conn, _pid = _ledger()
        R._VIRTUAL_QUOTE.pop(POOL, None)
        if verdict is not None:
            R._VIRTUAL_QUOTE[POOL] = verdict
        else:
            R._VQ_INFLIGHT.add(POOL)                  # its ownership read is still in flight
        assert _sweep(conn, Cache(), JUPITER_USD) == ["b_stop"]
        R._VQ_INFLIGHT.discard(POOL)


@pytest.mark.parametrize("value,cached", [(R.NOT_PUMPSWAP, True), (0, True), (1234, True), (None, False)])
def test_completed_pool_lookup_caches_known_rejection_but_retries_unknown(monkeypatch, value, cached):
    class CompletedThread:
        def __init__(self, *, target, **kwargs):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(R.threading, "Thread", CompletedThread)
    calls = []
    pool = "cache-verdict-" + str(value)
    R._VIRTUAL_QUOTE.pop(pool, None)
    R._VQ_INFLIGHT.discard(pool)
    fetch = lambda address: calls.append(address) or value
    assert R.virtual_quote(pool, fetch_fn=fetch) is None
    assert R.virtual_quote(pool, fetch_fn=fetch) == (value if cached else None)
    assert calls == [pool] * (1 if cached else 2)
