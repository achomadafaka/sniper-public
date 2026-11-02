"""Copy-lane LP-catastrophic exit + exit route persistence (operator 2026-10-21; Alpha design call).

Signal: pool liquidity L = isqrt(coin_reserve * quote_reserve) from COMPLETE same-slot vault rows on
the position's bound pool. Swaps never lower L in a constant-product pool; an LP withdraw does. The
rail fires at 1 - L_now / L_max_since_bind >= MEMECOIN_REAL_LP_CATASTROPHIC_FRAC (default 0.5).
"""
from __future__ import annotations

import json
import math
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as rr
from sniper.services import memecoin_solana_swap_marks as sm
from sniper.execution.solana_exit_executor import exit_route_fields
from sniper.settings import memecoin as cfg

MINT = "LpRugMint11111111111111111111111111111111pump"
POOL = MINT          # VaultTape rows carry the mint as their pool key (memecoin_solana_ws.VaultTape)
POOL_B = "PumpSwapPoolBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


CPMM_OWNER = "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL"       # raydium_cpmm_buy.CPMM_AUTHORITY
DAMM_V2_OWNER = "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC"


def _binding(pool=POOL, owner=CPMM_OWNER):
    return sm.PoolBinding(mint=MINT, pool=pool, coin_vault="cv", quote_vault="qv",
                          quote_mint="So11111111111111111111111111111111111111112", vault_owner=owner)


def _complete(cache, slot, coin, quote, kind="sol_swap", pool=POOL, d0=1000, d1=-1, sig=None):
    row = {"chain_id": "solana", "pool": pool, "kind": kind, "block_no": slot,
           "tx_sig": sig if sig is not None else f"tx{slot}",      # one named transaction
           "log_index": 0, "maker": None, "amount0": str(d0), "amount1": str(d1),
           "decimals0": 6, "decimals1": 9, "balance0": str(coin), "balance1": str(quote),
           "observed_at": None}
    cache.record(row, _binding(pool), now_ts=1.0, observed_at=f"2026-10-21T12:00:{slot % 60:02d}+00:00")


def _sync(cache, slot, coin, quote, pool=POOL):
    row = {"chain_id": "solana", "pool": pool, "kind": "sol_sync", "block_no": slot, "tx_sig": None,
           "log_index": 0, "maker": None, "amount0": str(coin), "amount1": str(quote),
           "decimals0": 6, "decimals1": 9, "observed_at": None}
    cache.record(row, _binding(pool), now_ts=1.0, observed_at="2026-10-21T12:00:00+00:00")


def _cache(pool=POOL, owner=CPMM_OWNER):
    c = sm.SolanaSwapMarkCache()
    c.set_lp_bindings({pool: _binding(pool, owner)})
    return c


# ------------------------------------------------------------------ the signal

def test_swaps_keep_liquidity_and_an_lp_withdraw_cuts_it():
    c = _cache()
    _complete(c, 100, 1_000_000_000, 50_000_000_000)
    # A big sell: coin in, quote out, k holds (slightly up by fees).
    _complete(c, 101, 1_400_000_000, 35_800_000_000, d0=400_000_000, d1=-14_200_000_000)
    swap = c.lp_removal(MINT)
    assert swap["state"] == "measured" and swap["fraction"] < 0.01
    # The LP withdraws 80% of both sides in one slot.
    _complete(c, 102, 280_000_000, 7_160_000_000, kind="sol_burn", d0=-1_120_000_000,
              d1=-28_640_000_000)
    rug = c.lp_removal(MINT)
    assert rug["state"] == "measured" and rug["fraction"] == pytest.approx(0.8, abs=0.01)
    assert rug["slot"] == 102 and rug["pool"] == POOL and rug["l_before"] > rug["l_after"]


def test_a_one_sided_sync_never_feeds_liquidity():
    """Gamma's tape defect: several same-slot sol_sync rows with different reserves (a fresh vault
    paired with a stale one). They must not move L at all."""
    c = _cache()
    _complete(c, 100, 1_000_000_000, 50_000_000_000)
    for coin, quote in ((10, 10), (1_000_000_000, 1), (5, 50_000_000_000)):
        _sync(c, 101, coin, quote)
    assert c.lp_removal(MINT)["fraction"] == 0.0


def test_no_complete_row_is_blind_not_zero():
    c = _cache()
    _sync(c, 100, 1_000_000_000, 50_000_000_000)
    assert c.lp_removal(MINT) == {"state": "blind", "why": "no_complete_row_since_bind", "pool": POOL}


def test_a_measured_full_drain_trips_and_is_not_ignored():
    """Codex review: a complete same-slot pair with a ZERO side used to be skipped, so a 100% drain
    kept the last healthy state forever."""
    c = _cache()
    _complete(c, 100, 1_000_000_000, 50_000_000_000)
    _complete(c, 101, 0, 0, kind="sol_burn", d0=-1_000_000_000, d1=-50_000_000_000)
    state = c.lp_removal(MINT)
    assert state["state"] == "measured" and state["fraction"] == 1.0 and state["l_after"] == 0


def test_a_concentrated_pool_is_blind_never_measured():
    """Codex's CLMM case: L=1, sqrt range 0.5..2, sqrtP 1 -> 1.99 moves real reserves from
    x=y=0.5 to x=0.0025126, y=1.49 (sqrt(xy) 0.0612, an 87.8% drop) with liquidity unchanged."""
    c = _cache(owner=DAMM_V2_OWNER)
    scale = 10 ** 9
    _complete(c, 100, int(0.5 * scale), int(0.5 * scale))
    _complete(c, 101, int(0.0025126 * scale), int(1.49 * scale))
    state = c.lp_removal(MINT)
    assert state["state"] == "blind" and state["why"] == "venue_not_proven_constant_product"
    unknown = _cache(owner=None)
    _complete(unknown, 100, scale, scale)
    assert unknown.lp_removal(MINT)["why"] == "venue_not_proven_constant_product"


def test_interleaved_vault_notifications_never_fake_a_withdrawal():
    """Codex's VaultTape case: from 100/100, arrival coin@100=10, coin@101=100, quote@100=1000,
    quote@101=100. The true CPMM states 10/1000 and 100/100 both have k=10000; the old tape paired
    coin@101 with quote@100 (L 316 -> 100, a false 68% pull)."""
    from sniper.services.memecoin_solana_ws import VaultTape
    c = _cache()
    tape = VaultTape("cv", "qv", mint=POOL)
    tape.on_balance("cv", 99, 100, decimals=6)
    tape.on_balance("qv", 99, 100, decimals=9)
    rows = [tape.on_balance("cv", 100, 10), tape.on_balance("cv", 101, 100),
            tape.on_balance("qv", 100, 1000), tape.on_balance("qv", 101, 100)]
    complete = [r for r in rows if r and r["kind"] in sm.LP_COMPLETE_KINDS]
    assert [(r["block_no"], r["balance0"], r["balance1"]) for r in complete] == [
        (100, "10", "1000"), (101, "100", "100")]
    for r in complete:
        c.record(r, _binding(), now_ts=1.0, observed_at="2026-10-21T12:00:00+00:00")
    # Unsigned (websocket) pairs are price evidence only: the LP rail stays blind and counts them.
    assert c.lp_removal(MINT)["why"] == "no_complete_row_since_bind" and c.lp_unsigned_rows == 2


def test_a_rebind_with_no_new_row_is_blind_never_the_old_pools_breach():
    """Codex review: the old pool's state used to survive until the NEW pool's first complete row,
    so a gap after a rebind could apply the old pool's breach to the new binding."""
    c = _cache()
    _complete(c, 100, 1_000_000_000, 50_000_000_000, pool=POOL)
    _complete(c, 101, 100_000_000, 5_000_000_000, kind="sol_burn", pool=POOL)    # old pool breached
    assert c.lp_removal(MINT)["fraction"] > 0.5
    c.set_lp_bindings({POOL_B: _binding(POOL_B)})                               # graduation re-bind
    assert c.lp_removal(MINT) == {"state": "blind", "why": "no_complete_row_since_bind",
                                  "pool": POOL_B}
    _complete(c, 102, 50_000_000, 100_000_000, kind="sol_burn", pool=POOL)       # late old-pool frame
    assert c.lp_removal(MINT)["state"] == "blind"
    _complete(c, 200, 5_000_000, 1_000_000, pool=POOL_B)
    state = c.lp_removal(MINT)
    assert state["pool"] == POOL_B and state["fraction"] == 0.0


def test_a_coverage_reset_is_blind():
    c = _cache()
    _complete(c, 100, 1_000_000_000, 50_000_000_000)
    c.invalidate()
    assert c.lp_removal(MINT)["state"] == "blind"
    c.set_lp_bindings({POOL: _binding()})          # the stream re-registers: still blind
    assert c.lp_removal(MINT)["why"] == "no_complete_row_since_bind"


def test_the_allowlisted_owners_are_the_cpmm_and_amm_v4_authorities():
    from sniper.execution.raydium_cpmm_buy import CPMM_AUTHORITY
    assert sm.LP_CONSTANT_PRODUCT_OWNERS == frozenset({
        CPMM_AUTHORITY, "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"})
    # A pre-column AMM v4 pool (no venue_program) is eligible by its vault owner alone.
    assert sm.lp_venue_eligible(sm.RAYDIUM_AMM_V4_AUTHORITY, None) is True
    assert sm.lp_venue_eligible("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j2", None) is False


def test_an_older_slot_never_rewinds_the_state():
    c = _cache()
    _complete(c, 100, 1_000_000_000, 50_000_000_000)
    _complete(c, 102, 200_000_000, 10_000_000_000, kind="sol_burn")
    _complete(c, 101, 1_000_000_000, 50_000_000_000)                 # late frame for an older slot
    assert c.lp_removal(MINT)["fraction"] == pytest.approx(0.8, abs=0.01)


# ------------------------------------------------------------------ the sweep

ENTRY_PRICE, QTY, NOTIONAL = 1e-4, 1_000_000.0, 100.0


@pytest.fixture(autouse=True)
def _knobs(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_LP_CATASTROPHIC_EXIT", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_LP_CATASTROPHIC_FRAC", 0.5)
    monkeypatch.setattr(rr, "exact_price_relevant", lambda *a, **k: True)
    rr._LP_STATE.clear()
    rr._LP_PAGED.clear()
    rr._LP_CHECK_MS.clear()
    rr._LP_BLIND.clear()
    yield
    rr._LP_STATE.clear()
    rr._LP_PAGED.clear()
    rr._LP_CHECK_MS.clear()
    rr._LP_BLIND.clear()


def _ledger():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = m.open_position(
        conn, source_user_id="u", source_handle="w", mint=MINT, ticker="RUG",
        entry_price=ENTRY_PRICE, entry_notional=NOTIONAL, entry_fill_source="chain", entry_lag_s=0,
        their_size_usd=0, opened_at="2026-10-21T11:59:00+00:00", strategy="alpha_b",
        execution_mode="real", network_id="solana")
    m.insert_fill(conn, position_id=pid, kind="entry", price=ENTRY_PRICE, qty=QTY, notional=NOTIONAL,
                  fill_source="chain", fraction=1.0, created_at="2026-10-21T11:59:00+00:00")
    return conn, pid


class FakeCache:
    def __init__(self, state):
        self.state = state

    def lp_removal(self, mint):
        return dict(self.state)

    def bindings_by_mint(self):
        return {}


def _sweep(conn, cache, monkeypatch, pages=None):
    calls = []
    if pages is not None:
        from sniper.services import memecoin_alerts
        monkeypatch.setattr(memecoin_alerts, "send_alert", lambda kind, text, **k: pages.append((kind, text)))
    context = {"preview_exit_fn": lambda _p: SimpleNamespace(
                   expected_usdc=NOTIONAL, amount_atomic=int(QTY * 1e6), route_id="r",
                   quoted_at="2026-10-21T11:59:59+00:00"),
               "exit_fn": lambda _c, _p, reason, _n: calls.append(reason) or {"status": "unknown"},
               "decimals_fn": lambda _m: 6}
    rr.real_exit_sweep(conn, {"solana": context}, price_fn=lambda _m: {MINT: {"price": ENTRY_PRICE}},
                       now_fn=lambda: "2026-10-21T12:00:00+00:00", reserve_witness_cache=cache)
    return calls


BREACH = {"state": "measured", "fraction": 0.8, "l_before": 1000, "l_after": 200, "slot": 102,
          "max_slot": 100, "pool": POOL, "at": "2026-10-21T12:00:00+00:00"}


def _audit(conn, reason):
    return [dict(r) for r in conn.execute(
        "SELECT allowed, detail FROM memecoin_exit_trigger_audit WHERE reason=?", (reason,))]


def test_a_breach_sells_through_the_exit_seam_audits_and_pages_once(monkeypatch):
    conn, pid = _ledger()
    pages = []
    assert _sweep(conn, FakeCache(BREACH), monkeypatch, pages) == ["lp_catastrophic"]
    rows = _audit(conn, "lp_catastrophic")
    assert rows and json.loads(rows[0]["detail"])["l_after"] == 200
    lp_pages = [text for kind, text in pages if kind == "LP-CATASTROPHIC-EXIT"]
    assert len(lp_pages) == 1 and "1000 -> 200" in lp_pages[0]
    _sweep(conn, FakeCache(BREACH), monkeypatch, pages)
    assert len([k for k, _ in pages if k == "LP-CATASTROPHIC-EXIT"]) == 1, "a position pages once"


@pytest.mark.parametrize("knob, frac, state", [
    (False, 0.5, BREACH), (True, None, BREACH), (True, 0.5, dict(BREACH, fraction=0.49))])
def test_no_fire_when_off_malformed_or_below_threshold(monkeypatch, knob, frac, state):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_LP_CATASTROPHIC_EXIT", knob)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_LP_CATASTROPHIC_FRAC", frac)
    conn, pid = _ledger()
    assert "lp_catastrophic" not in _sweep(conn, FakeCache(state), monkeypatch, [])


def test_blind_is_audited_once_per_transition_never_silent(monkeypatch):
    """Never silent, and never a ledger write on every sweep tick (Gamma LOW 3): one lp_blind row
    when a position becomes blind; a steady blind state writes nothing more, at any age."""
    conn, pid = _ledger()
    writes = []
    real = m.record_exit_trigger_audit

    def spy(c, **kw):
        if kw.get("reason") == "lp_blind":
            writes.append(kw)
        return real(c, **kw)
    monkeypatch.setattr(m, "record_exit_trigger_audit", spy)
    blind = {"state": "blind", "why": "no_complete_row_since_bind"}
    assert "lp_catastrophic" not in _sweep(conn, FakeCache(blind), monkeypatch, [])
    _sweep(conn, FakeCache(blind), monkeypatch, [])
    assert len(writes) == 1
    rows = _audit(conn, "lp_blind")
    assert len(rows) == 1 and json.loads(rows[0]["detail"])["why"] == "no_complete_row_since_bind"


def test_a_cache_without_the_reader_changes_nothing(monkeypatch):
    conn, pid = _ledger()

    class Old:
        def bindings_by_mint(self):
            return {}
    assert "lp_catastrophic" not in _sweep(conn, Old(), monkeypatch, [])


def test_the_malformed_fraction_parses_to_off():
    for raw in ("", "x", "0", "1", "1.5", "-0.2", "nan"):
        assert cfg._lp_catastrophic_frac(raw) is None, raw
    assert cfg._lp_catastrophic_frac("0.7") == 0.7


# ------------------------------------------------------------------ exit route persistence

def test_exit_route_fields_from_the_signed_quote():
    one = {"routePlan": [{"swapInfo": {"ammKey": "PoolA"}}]}
    two = {"routePlan": [{"swapInfo": {"ammKey": "PoolA"}}, {"swapInfo": {"ammKey": "PoolB"}}]}
    assert exit_route_fields(one) == {"quote_route_target": "PoolA", "executed_pool": "PoolA"}
    assert exit_route_fields(two) == {"quote_route_target": "PoolA,PoolB", "executed_pool": None}
    assert exit_route_fields({"_sniper_route_target": "NativePool"}) == {
        "quote_route_target": "NativePool", "executed_pool": "NativePool"}
    assert exit_route_fields({}) == {"quote_route_target": None, "executed_pool": None}
    assert exit_route_fields(None) == {"quote_route_target": None, "executed_pool": None}


def test_full_and_partial_exit_fills_store_the_route():
    conn, pid = _ledger()
    conn.execute("UPDATE memecoin_positions SET close_claim_id='c', close_sig='S' WHERE id=?", (pid,))
    assert m.book_full_exit_and_finalize(
        conn, position_id=pid, claim_id="c", sig="S", closed_at="2026-10-21T12:01:00+00:00",
        realized_pnl=-50.0, exit_reason="lp_catastrophic", fill_kind="bracket_exit", price=5e-5,
        qty=QTY, notional=50.0, quote_route_target="PoolA", executed_pool="PoolA")
    row = conn.execute("SELECT quote_route_target, executed_pool FROM memecoin_fills "
                       "WHERE kind='bracket_exit'").fetchone()
    assert tuple(row) == ("PoolA", "PoolA")
    conn2, pid2 = _ledger()
    m.book_derisk_fill(conn2, position_id=pid2, at="2026-10-21T12:01:00+00:00", price=2e-4,
                       qty=QTY / 2, notional=100.0, fraction=0.5, tx_sig="D",
                       quote_route_target="PoolA,PoolB", executed_pool=None)
    row = conn2.execute("SELECT quote_route_target, executed_pool FROM memecoin_fills "
                        "WHERE kind='derisk'").fetchone()
    assert tuple(row) == ("PoolA,PoolB", None)


@pytest.mark.parametrize("price_ratio, extra", [
    (0.5, {"MEMECOIN_REAL_STOP_CONFIRM_SWEEPS": 5}),       # a stop that would wait on debounce
    (3.0, {"MEMECOIN_REAL_TP_PCT": 50.0, "_tp_window": 30.0}),  # a TP held by its confirm window
])
def test_an_lp_breach_outranks_a_pending_price_rail(monkeypatch, price_ratio, extra):
    """Codex review: the LP check ran only when no price rail nominated, so a stop or TP that then
    waited on debounce / no-loss / confirmation / the TP window suppressed the emergency."""
    for key, value in extra.items():
        if key == "_tp_window":
            monkeypatch.setattr(rr, "_tp_confirm_window_s", lambda: value)
        else:
            monkeypatch.setattr(cfg, key, value)
    conn, pid = _ledger()
    calls = []
    context = {"preview_exit_fn": lambda _p: SimpleNamespace(
                   expected_usdc=NOTIONAL * price_ratio, amount_atomic=int(QTY * 1e6), route_id="r",
                   quoted_at="2026-10-21T11:59:59+00:00"),
               "exit_fn": lambda _c, _p, reason, _n: calls.append(reason) or {"status": "unknown"},
               "decimals_fn": lambda _m: 6}
    rr.real_exit_sweep(conn, {"solana": context},
                       price_fn=lambda _m: {MINT: {"price": ENTRY_PRICE * price_ratio}},
                       now_fn=lambda: "2026-10-21T12:00:00+00:00", reserve_witness_cache=FakeCache(BREACH))
    assert calls == ["lp_catastrophic"]


def test_a_binding_that_returns_to_an_old_pool_starts_blind():
    """A -> B -> A: neither A's breached peak nor late A frames that arrived while B was bound may
    come back with the binding."""
    c = _cache()
    _complete(c, 100, 1_000_000_000, 50_000_000_000, pool=POOL)
    _complete(c, 101, 100_000_000, 5_000_000_000, kind="sol_burn", pool=POOL)
    c.set_lp_bindings({POOL_B: _binding(POOL_B)})
    _complete(c, 102, 2_000_000_000, 100_000_000_000, pool=POOL)                # late A frame
    c.set_lp_bindings({POOL: _binding(POOL)})
    assert c.lp_removal(MINT) == {"state": "blind", "why": "no_complete_row_since_bind", "pool": POOL}


# ------------------------------------------------------------------ venue program at bind (Alpha)

from sniper.services import memecoin_solana_amounts as sa  # noqa: E402

CV, QV = "CoinVaultVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV", "QuoteVaultVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"


def _venue_tx(outer, inner=()):
    """outer/inner = [(program_id, [accounts])] in jsonParsed shape."""
    ix = lambda program, accounts: {"programId": program, "accounts": list(accounts)}
    return {"transaction": {"message": {"accountKeys": [{"pubkey": "Payer"}],
                                        "instructions": [ix(p, a) for p, a in outer]}},
            "meta": {"innerInstructions": [{"index": 0, "instructions": [ix(p, a) for p, a in inner]}]}}


def test_the_venue_is_the_known_program_that_names_both_vaults():
    pump = sa.PUMPSWAP_PROGRAM
    assert sa.venue_program(_venue_tx([(pump, ["Pool", CV, QV])]), CV, QV) == pump
    # Jupiter-routed: the router's outer instruction also names both vaults; only the inner
    # venue program counts.
    routed = _venue_tx([("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4", [CV, QV, "x"])],
                       [(sa.RAYDIUM_AMM_V4_PROGRAM, ["Amm", CV, QV])])
    assert sa.venue_program(routed, CV, QV) == sa.RAYDIUM_AMM_V4_PROGRAM
    assert sa.venue_program(_venue_tx([(sa.RAYDIUM_CLMM_PROGRAM, [CV, QV])]), CV, QV) == \
        sa.RAYDIUM_CLMM_PROGRAM
    two = _venue_tx([(pump, [CV, QV]), (sa.RAYDIUM_CPMM_PROGRAM, [CV, QV])])
    assert sa.venue_program(two, CV, QV) is None                     # ambiguous -> unknown
    assert sa.venue_program(_venue_tx([(pump, [CV])]), CV, QV) is None   # one vault only
    assert sa.venue_program({}, CV, QV) is None and sa.venue_program(None, CV, QV) is None


@pytest.mark.parametrize("program, owner, eligible", [
    (sa.PUMPSWAP_PROGRAM, None, True),
    (sa.RAYDIUM_AMM_V4_PROGRAM, None, True),
    (sa.RAYDIUM_CPMM_PROGRAM, None, True),
    (None, CPMM_OWNER, True),                                      # pre-column CPMM row
    (sa.RAYDIUM_CLMM_PROGRAM, None, False), (sa.METEORA_DLMM_PROGRAM, None, False),
    (sa.METEORA_DAMM_V2_PROGRAM, None, False), (sa.METEORA_DBC_PROGRAM, None, False),
    (sa.RAYDIUM_LAUNCHLAB_PROGRAM, None, False), (None, None, False),
])
def test_only_constant_product_venue_programs_are_measured(program, owner, eligible):
    c = sm.SolanaSwapMarkCache()
    c.set_lp_bindings({POOL: sm.PoolBinding(mint=MINT, pool=POOL, coin_vault="cv", quote_vault="qv",
                                            quote_mint="So1", vault_owner=owner,
                                            venue_program=program)})
    _complete(c, 100, 1_000_000_000, 50_000_000_000)
    assert (c.lp_removal(MINT)["state"] == "measured") is eligible


def test_the_tape_records_the_venue_program_at_bind(tmp_path):
    import sqlite3 as _sq
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.services import memecoin_solana_tape as st
    from test_memecoin_solana_tape import COIN, COIN_VAULT, POOL_OWNER, WSOL_VAULT, _cursor_db, _tx
    tape = _cursor_db()
    ensure_feature_tables(tape)
    ledger = _sq.connect(":memory:")
    ledger.execute("CREATE TABLE memecoin_fills(position_id INTEGER,kind TEXT,tx_sig TEXT,id INTEGER)")
    ledger.execute("INSERT INTO memecoin_fills VALUES (7, 'entry', 'entry-sig', 1)")
    tx = _tx([(COIN, POOL_OWNER, COIN_VAULT, 1000), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 500)],
             [(COIN, POOL_OWNER, COIN_VAULT, 900), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 560)])
    tx["transaction"]["message"]["instructions"] = [
        {"programId": sa.PUMPSWAP_PROGRAM, "accounts": ["PoolX", COIN_VAULT, WSOL_VAULT]}]
    vault = st.resolve_position_pool(tape, ledger, {"id": 7, "mint": COIN}, lambda _sig: tx)
    assert vault == COIN_VAULT
    row = tape.execute("SELECT venue_program FROM sol_pool_vaults WHERE pool=?", (COIN_VAULT,)).fetchone()
    assert row[0] == sa.PUMPSWAP_PROGRAM


# ------------------------------------------------------------------ Codex FAILs on a539c6b03

def _row(slot, coin, quote, pool=POOL, kind="sol_swap"):
    return {"chain_id": "solana", "pool": pool, "kind": kind, "block_no": slot, "tx_sig": f"tx{slot}",
            "log_index": 0, "maker": None, "amount0": "1", "amount1": "-1", "decimals0": 6,
            "decimals1": 9, "balance0": str(coin), "balance1": str(quote), "observed_at": None}


def test_within_slot_multi_writes_are_blind_never_a_fabricated_pair():
    """Codex: start 100/100; slot 100 holds two real states 10/1000 then 100/100 (both k=10000).
    Arrival coin 10, coin 100, quote 1000, quote 100 used to pair coin 100 with quote 1000 (L 316),
    and the next real 10/1000 (L 100) then read as a 68.4% withdrawal."""
    from sniper.services.memecoin_solana_ws import VaultTape
    c = _cache()
    tape = VaultTape("cv", "qv", mint=POOL)
    tape.on_balance("cv", 98, 90, decimals=6)
    tape.on_balance("qv", 98, 111, decimals=9)
    rows = [tape.on_balance("cv", 99, 100), tape.on_balance("qv", 99, 100)]     # L 100, complete
    rows += [tape.on_balance("cv", 100, 10), tape.on_balance("cv", 100, 100),
             tape.on_balance("qv", 100, 1000), tape.on_balance("qv", 100, 100)]
    rows += [tape.on_balance("cv", 101, 10), tape.on_balance("qv", 101, 1000)]  # the next real state
    for r in rows:
        if r:
            c.record(r, _binding(), now_ts=1.0, observed_at="2026-10-21T12:00:00+00:00")
    complete = [(r["block_no"], r["balance0"], r["balance1"]) for r in rows
                if r and r["kind"] in sm.LP_COMPLETE_KINDS]
    assert all(slot != 100 for slot, _, _ in complete)          # the ambiguous slot never pairs
    assert c.lp_removal(MINT)["state"] == "blind"               # unsigned rows never feed LP


def test_the_unsigned_websocket_lane_never_nominates(monkeypatch):
    """Codex's exact-HEAD witness on 062bef02e, lifted: parse_frame -> VaultTape.on_balance ->
    cache.record -> _lp_catastrophic_reason. Swaps A 10/1000 and B 1000/10 in slot 100 keep k; the
    crossed unsigned pair 10/10 used to read as a 90% pull and nominate before the late void."""
    from sniper.services.memecoin_solana_ws import VaultTape, parse_frame
    binding = _binding()
    cache = sm.SolanaSwapMarkCache()
    cache.set_lp_bindings({binding.pool: binding})
    tape = VaultTape("cv", "qv", mint=binding.pool)

    def frame(vault, slot, amount):
        message = {"method": "accountNotification", "params": {
            "subscription": 1 if vault == "cv" else 2,
            "result": {"context": {"slot": slot}, "value": {"data": {"parsed": {"info": {
                "tokenAmount": {"amount": str(amount), "decimals": 6 if vault == "cv" else 9}}}}}}}}
        _, account, slot_, amount_, decimals = parse_frame(message, {1: "cv", 2: "qv"})
        row = tape.on_balance(account, slot_, amount_, decimals=decimals)
        if row:
            cache.record(row, binding, now_ts=1, observed_at="2026-10-21T12:00:00+00:00")
        return row
    seen = []
    for args in (("cv", 98, 90), ("qv", 98, 111), ("cv", 99, 100), ("qv", 99, 100),
                 ("cv", 100, 10), ("qv", 100, 10), ("cv", 100, 1000)):
        frame(*args)
        seen.append(_nominates(monkeypatch, cache, MINT))
    assert seen == [None] * 7
    assert cache.lp_removal(MINT)["state"] == "blind" and cache.lp_unsigned_rows >= 2


def test_a_signed_one_transaction_burn_still_nominates(monkeypatch):
    coin, cv, qv, cache, apply = _grpc_lane(True)
    for args in ((cv, 98, 1, 90, "s0"), (qv, 98, 2, 111, "s0"), (cv, 99, 3, 1000, "t0"),
                 (qv, 99, 4, 1000, "t0")):
        apply(*args)
    assert _nominates(monkeypatch, cache, coin) is None
    apply(cv, 100, 5, 100, "rug")                               # the LP withdraws 90% of both sides
    apply(qv, 100, 6, 100, "rug")
    assert cache.lp_removal(coin)["fraction"] == pytest.approx(0.9)
    assert _nominates(monkeypatch, cache, coin) == rr.LP_CATASTROPHIC_REASON


def test_a_pruned_slot_never_pairs_with_the_latest_balance():
    """Gamma LOW 1: a notification more than 64 slots late re-creates its slot with one side only."""
    from sniper.services.memecoin_solana_ws import VaultTape
    tape = VaultTape("cv", "qv", mint=POOL)
    tape.on_balance("cv", 1, 100, decimals=6)
    tape.on_balance("qv", 1, 100, decimals=9)
    tape.on_balance("cv", 10, 50)                               # slot 10, coin side only
    for s in range(11, 200):
        tape.on_balance("cv", s, 50 + s)                        # prunes slot 10
    late = tape.on_balance("qv", 10, 200)
    assert late["kind"] == "sol_sync" and "balance0" not in late


def test_a_to_b_to_a_through_the_production_merge_never_keeps_the_stale_pool():
    """Codex: the stream's merge used dict.update(); A->B->A kept B after A, so B stayed bound and
    B's breach applied while fresh A was ignored."""
    import asyncio
    c = sm.SolanaSwapMarkCache()
    bindings = {POOL: _binding(POOL)}
    refreshes = iter([({}, {POOL_B: _binding(POOL_B)}), ({}, {POOL: _binding(POOL)})])
    seen = []

    async def stream(vaults, on_row, *, refresh_fn=None, **kw):
        await on_row(_row(100, 1_000_000_000, 50_000_000_000))
        refresh_fn()                                            # A -> B
        await on_row(_row(101, 1_000_000_000, 50_000_000_000, pool=POOL_B))
        await on_row(_row(102, 10_000_000, 500_000_000, pool=POOL_B, kind="sol_burn"))   # B breached
        seen.append(c.lp_removal(MINT))
        refresh_fn()                                            # B -> A
        seen.append(c.lp_removal(MINT))
        await on_row(_row(103, 1_000_000_000, 50_000_000_000))
        seen.append(c.lp_removal(MINT))

    asyncio.run(sm.run_price_stream_epoch({}, bindings, c, ws_url="x", duration_s=5,
                                          stream_fn=stream, refresh_fn=lambda: next(refreshes),
                                          now_iso_fn=lambda: "2026-10-21T12:00:00+00:00"))
    assert seen[0]["pool"] == POOL_B and seen[0]["fraction"] > 0.9
    assert seen[1]["state"] == "blind" and seen[1].get("pool") == POOL
    assert seen[2]["pool"] == POOL and seen[2]["fraction"] == 0.0
    assert set(bindings) == {POOL}


def test_two_pools_for_one_mint_are_blind():
    c = sm.SolanaSwapMarkCache()
    c.set_lp_bindings({POOL: _binding(POOL), POOL_B: _binding(POOL_B)})
    assert c.lp_removal(MINT) == {"state": "blind", "why": "unbound"}


# ------------------------------------------------------------------ Codex P1 on 56a8c4676 (authority)

def _grpc_lane(sigs=True):
    """Codex's witness, lifted: the real apply_account_row -> VaultTape -> cache.record publication
    (run_price_stream_epoch.on_row), then the funded rail's own _lp_catastrophic_reason."""
    import base58
    import struct
    coin = base58.b58encode(bytes(range(32))).decode()
    cv = base58.b58encode(bytes(range(1, 33))).decode()
    qv = base58.b58encode(bytes(range(2, 34))).decode()
    quote = "So11111111111111111111111111111111111111112"
    binding = sm.PoolBinding(mint=coin, pool=cv, coin_vault=cv, quote_vault=qv, quote_mint=quote,
                             vault_owner=CPMM_OWNER)
    from sniper.services.memecoin_solana_ws import VaultTape
    tape, cache, ordering = VaultTape(cv, qv, mint=cv), sm.SolanaSwapMarkCache(), {}
    cache.set_lp_bindings({cv: binding})

    def apply(vault, slot, version, amount, sig):
        raw = bytearray(165)
        raw[:32] = base58.b58decode(coin if vault == cv else quote)
        struct.pack_into("<Q", raw, 64, amount)
        raw[108] = 1
        row = sm.apply_account_row(dict(
            pubkey=vault, slot=slot, write_version=version, data=bytes(raw), lamports=1,
            owner="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", connection_generation=1,
            watch_generation=1, txn_signature=sig if sigs else None),
            {cv: tape, qv: tape}, {cv: binding}, decimals={cv: 6, qv: 9}, ordering=ordering)
        if row is not None:
            cache.record(row, binding, now_ts=1, observed_at="2026-10-21T12:00:00+00:00")
        return row
    return coin, cv, qv, cache, apply


def _nominates(monkeypatch, cache, coin):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_LP_CATASTROPHIC_EXIT", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_LP_CATASTROPHIC_FRAC", 0.5)
    from sniper.services import memecoin_alerts
    monkeypatch.setattr(memecoin_alerts, "send_alert", lambda *a, **kw: None)
    conn, pid = _ledger()
    conn.execute("UPDATE memecoin_positions SET mint=? WHERE id=?", (coin, pid))
    conn.commit()
    try:
        return rr._lp_catastrophic_reason(conn, m.get_position(conn, pid), cache,
                                          "2026-10-21T12:00:00+00:00", "review")
    finally:
        conn.close()


@pytest.mark.parametrize("sigs", [True, False])
def test_cross_transaction_vault_writes_never_nominate_through_the_funded_rail(monkeypatch, sigs):
    """Codex's exact arrival: baseline 100/100; slot 100 holds swaps A 10/1000 and B 1000/10 (k
    unchanged); coinA(v10)=10 then quoteB(v20)=10 used to publish 10/10 (fraction 0.9) and the
    funded rail nominated lp_catastrophic before coinB(v19) could void it."""
    coin, cv, qv, cache, apply = _grpc_lane(sigs)
    for args in ((cv, 98, 1, 90, "s0"), (qv, 98, 2, 111, "s0"), (cv, 99, 3, 100, "t0"),
                 (qv, 99, 4, 100, "t0")):
        apply(*args)
    base = cache.lp_removal(coin)
    if sigs:
        # A clean tx-bound pair (one write per vault, same signature) IS a baseline: not always blind.
        assert base["state"] == "measured" and base["l_before"] == 100 and base["fraction"] == 0.0
    else:
        assert base["state"] == "blind"     # an unnamed write never pairs (lp_unsigned_rows)
    seen = [_nominates(monkeypatch, cache, coin)]
    apply(cv, 100, 10, 10, "A")
    apply(qv, 100, 20, 10, "B")
    seen.append(_nominates(monkeypatch, cache, coin))
    apply(cv, 100, 19, 1000, "B")            # B's coin write: B's own pair, its true post-state
    seen.append(_nominates(monkeypatch, cache, coin))
    apply(qv, 100, 11, 1000, "A")            # older per-account version: ignored by ordering
    seen.append(_nominates(monkeypatch, cache, coin))
    assert seen == [None, None, None, None]
    if sigs:
        assert cache.lp_removal(coin)["fraction"] == 0.0


def test_transitions_audit_blind_measurable_and_a_new_blind_reason_only(monkeypatch):
    conn, pid = _ledger()
    writes = []                                   # the audit table upserts per minute: spy the writes
    real = m.record_exit_trigger_audit
    monkeypatch.setattr(m, "record_exit_trigger_audit", lambda c, **kw: writes.append(kw) or real(c, **kw))
    healthy = dict(BREACH, fraction=0.0, l_after=1000)
    seq = [{"state": "blind", "why": "no_complete_row_since_bind"}] * 3 + [healthy] * 2 + [
        {"state": "blind", "why": "unbound"}, {"state": "blind", "why": "unbound"}]
    for state in seq:
        _sweep(conn, FakeCache(state), monkeypatch, [])
    lp = [(w["reason"], json.loads(w["detail"]).get("why"), json.loads(w["detail"])["previous"])
          for w in writes if w["reason"] in ("lp_blind", "lp_measurable")]
    assert lp == [("lp_blind", "no_complete_row_since_bind", None),
                  ("lp_measurable", None, "blind:no_complete_row_since_bind"),
                  ("lp_blind", "unbound", "measured")]
    assert rr.lp_rail_status()["blind_checks"] == {"no_complete_row_since_bind": 3, "unbound": 2}


def test_each_position_check_is_timed_and_reaches_the_heartbeat(monkeypatch, tmp_path):
    from sniper.services import memecoin_runner as runner
    conn, pid = _ledger()
    for _ in range(3):
        _sweep(conn, FakeCache(dict(BREACH, fraction=0.0, l_after=1000)), monkeypatch, [])
    cache = sm.SolanaSwapMarkCache()
    cache.lp_unsigned_rows = 4
    status = rr.lp_rail_status(cache)
    assert status["check_ms"]["n"] == 3 and 0 <= status["check_ms"]["p50"] <= status["check_ms"]["max"]
    assert status["unsigned_complete_rows"] == 4
    path = tmp_path / "hb.json"
    runner.write_heartbeat({}, auth_ok=True, path=str(path), lp_rail=status)
    assert json.loads(path.read_text())["lp_rail"]["check_ms"]["n"] == 3
    body = open("scripts/memecoin_runner_loop.py").read()
    assert body.count("lp_rail=lp_rail_status(_swap_mark_cache)") == 2


def test_concurrent_lanes_page_a_breach_exactly_once(monkeypatch):
    """Gamma LOW 2: the periodic sweep and the hot lanes share the page set under one lock."""
    import threading
    from sniper.services import memecoin_alerts
    pages = []
    monkeypatch.setattr(memecoin_alerts, "send_alert", lambda kind, text, **k: pages.append(kind))
    conn, pid = _ledger()
    pos = m.get_position(conn, pid)
    gate = threading.Barrier(8)

    def lane():
        c = sqlite3.connect(":memory:")
        m.ensure_memecoin_tables(c)
        gate.wait()
        rr._lp_catastrophic_reason(c, pos, FakeCache(BREACH), "2026-10-21T12:00:00+00:00", "hot")
    threads = [threading.Thread(target=lane) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert pages.count("LP-CATASTROPHIC-EXIT") == 1
