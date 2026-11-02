"""Copy-logic build (plan 2026-07-05): chase guard + SL/TP brackets + exit_reason.
Lands on top of shipped P1 (partial_exit/manual_buy/open_qty/_realized_from_exits)."""
import sqlite3

from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


# ── Task 1: config + migrations + fill-kind wiring ──────────────────────────
def test_migrations_and_exit_kinds():
    c = _conn()
    pos_cols = {r[1] for r in c.execute("PRAGMA table_info(memecoin_positions)")}
    ev_cols = {r[1] for r in c.execute("PRAGMA table_info(memecoin_events)")}
    assert "exit_reason" in pos_cols
    assert {"wouldbe_price", "chase_gap_pct"} <= ev_cols
    assert "bracket_exit" in m.EXIT_KINDS          # brackets reduce open qty


def test_close_position_records_reason():
    c = _conn()
    pid = m.open_position(c, source_user_id="u1", source_handle="a", mint="M", ticker="T",
                          entry_price=0.005, entry_notional=100.0, entry_fill_source="jupiter",
                          entry_lag_s=0.3, their_size_usd=1.0, opened_at="t")
    m.close_position(c, pid, "t2", 10.0, exit_reason="stop_loss")
    assert m.list_closed_positions(c)[0]["exit_reason"] == "stop_loss"


def test_set_event_chase():
    c = _conn()
    ev = {"fomo_event_id": "e1", "source_user_id": "u1", "source_handle": "a", "mint": "M",
          "ticker": "T", "side": "buy", "their_price": 0.005, "their_market_cap": 1.0,
          "their_size_usd": 1.0, "is_first_buy": 1, "created_at": "t", "received_at": "t",
          "lag_s": 0.3}
    m.insert_event(c, ev)
    m.set_event_chase(c, "e1", wouldbe_price=0.0061, chase_gap_pct=22.0)
    row = c.execute("SELECT wouldbe_price, chase_gap_pct FROM memecoin_events WHERE fomo_event_id='e1'").fetchone()
    assert abs(row[0] - 0.0061) < 1e-9 and abs(row[1] - 22.0) < 1e-9


# ── Task 2: thread exit_reason + count bracket_exit in realized PnL ──────────
from sniper.feeds.fomo_feed import CopyEvent
from sniper.services import memecoin_copy_trader as eng


def _ev(side, uid="u1", mint="M"):
    return CopyEvent(uid, "a", mint, "T", side, 0.005, 1e6, 1.0, True, "e", "t", "t", 0.3)


def test_mirror_close_tags_reason():
    c = _conn()
    eng.on_copy_event(c, _ev("buy"), 100.0, "t", fill_fn=lambda *a, **k: (0.005, "jupiter"))
    from sniper.services.memecoin_copy_trader import copy_event_to_arms
    copy_event_to_arms(c, _ev("sell"), 100.0, "t2", arms=["control"],
                       fill_fn=lambda *a, **k: (None, None),
                       exit_quote_fn=lambda mint, net, qty: (0.010, "route_quote"))
    assert m.list_closed_positions(c)[0]["exit_reason"] == "mirror"


# ── Task 3: entry chase guard (+ counterfactual) ────────────────────────────
def _buy_event(their_price):
    return CopyEvent("u1", "a", "M", "T", "buy", their_price, 1e6, 1.0, True, "ev1", "t", "t", 0.3)


def test_chase_guard_skips_when_ran_up():
    c = _conn()
    m.insert_event(c, _buy_event(0.005).__dict__)
    # ⚠️ THE REACH VALUE IS THE GAP, AND THE OLD ONE NO LONGER REACHES. This asserted +20% against
    # paper's own flat 15% cap; the gate now applies FUNDED's cap, which is 30% above the nano band
    # and 35% inside it, so +20% is ADMITTED and the old fixture passed through. 0.009 is +80%,
    # over either branch.
    r = eng.on_copy_event(c, _buy_event(0.005), 100.0, "t", fill_fn=lambda *a, **k: (0.009, "dexscreener"))
    assert r == "skipped_chase_over_funded_cap"
    assert m.get_open_position(c, "u1", "M") is None
    row = c.execute("SELECT wouldbe_price, chase_gap_pct FROM memecoin_events WHERE fomo_event_id='ev1'").fetchone()
    assert abs(row[0] - 0.009) < 1e-9 and abs(row[1] - 80.0) < 0.5


def test_chase_guard_allows_within_threshold():
    c = _conn()
    m.insert_event(c, _buy_event(0.005).__dict__)
    r = eng.on_copy_event(c, _buy_event(0.005), 100.0, "t", fill_fn=lambda *a, **k: (0.0054, "dexscreener"))
    assert r == "opened"                      # +8% < 15%


def test_chase_guard_disabled_when_their_price_zero():
    c = _conn()
    ev = CopyEvent("u1", "a", "M", "T", "buy", 0.0, 1e6, 1.0, True, "ev2", "t", "t", 0.3)
    m.insert_event(c, ev.__dict__)
    assert eng.on_copy_event(c, ev, 100.0, "t", fill_fn=lambda *a, **k: (0.006, "x")) == "opened"


# ── Task 4: batched DexScreener price map ───────────────────────────────────
from sniper.execution import solana_quote as q


def test_dexscreener_batch_chunks_and_maps():
    calls = []
    def http(method, url, headers=None, body=None):
        calls.append(url)
        tail = url.split("?")[0].rsplit("/", 1)[1]   # a cache-bust query is not a mint
        pairs = [{"chainId": "solana", "pairAddress": "pair-" + mt,
                  "baseToken": {"address": mt}, "priceUsd": "0.01",
                  "marketCap": "1000000", "liquidity": {"usd": "50000"},
                  "volume": {"h24": "12345"}} for mt in tail.split(",")]
        return 200, {"pairs": pairs}, None
    mints = [f"m{i}" for i in range(35)]           # >30 -> 2 requests
    out = q.dexscreener_stats_batch(mints, http=http)
    assert len(calls) == 2
    assert out["m0"]["price"] == 0.01 and out["m34"]["liquidity"] == 50000.0


# ── Task 5: SL/TP bracket evaluator ─────────────────────────────────────────
from sniper.services import memecoin_brackets as br


def _open(c, entry=0.005, qty=20000.0, uid="u1", mint="M", network_id=None):
    pid = m.open_position(c, source_user_id=uid, source_handle="a", mint=mint, ticker="T",
                          entry_price=entry, entry_notional=100.0, entry_fill_source="jupiter",
                          entry_lag_s=0.3, their_size_usd=1.0, opened_at="t",
                          network_id=network_id)
    m.insert_fill(c, position_id=pid, kind="entry", price=entry, qty=qty, notional=100.0,
                  fill_source="jupiter", fraction=1.0, created_at="t")
    return pid


def test_stop_loss_closes():
    c = _conn(); pid = _open(c)
    out = br.evaluate_brackets(c, {"M": {"price": 0.002, "liquidity": 1e15}}, "t2", sl_pct=60, tp_pct=500)  # -60%
    assert out and out[0]["exit_reason"] == "stop_loss"
    assert m.get_position(c, pid)["is_open"] == 0


def test_take_profit_closes():
    c = _conn(); pid = _open(c)
    out = br.evaluate_brackets(c, {"M": {"price": 0.030, "liquidity": 1e15}}, "t2", sl_pct=60, tp_pct=500)  # +500%
    assert out[0]["exit_reason"] == "take_profit"


def test_no_trigger_stays_open():
    c = _conn(); pid = _open(c)
    assert br.evaluate_brackets(c, {"M": {"price": 0.004, "liquidity": 1e15}}, "t2", sl_pct=60, tp_pct=500) == []
    assert m.get_position(c, pid)["is_open"] == 1


def test_bracket_uses_open_qty_after_partial():
    c = _conn(); pid = _open(c, entry=0.005, qty=20000.0)
    eng.partial_exit(c, pid, 0.5, "t1", price_fn=lambda mint, http=None: {"price": 0.006, "liquidity": 1e15})
    assert abs(m.open_qty(c, pid) - 10000.0) < 1e-6      # half remains
    out = br.evaluate_brackets(c, {"M": {"price": 0.002, "liquidity": 1e15}}, "t3", sl_pct=60, tp_pct=500)
    assert out[0]["exit_reason"] == "stop_loss"
    realized = m.get_position(c, pid)["realized_pnl"]
    # partial: (0.006-0.005)*10000 = +10 ; bracket: (0.002-0.005)*10000 = -30 ; total = -20
    assert abs(realized - (-20.0)) < 1e-6


# ── Task 6: runner bracket_pass (batched) ───────────────────────────────────
from sniper.services import memecoin_runner as run


def test_bracket_pass_closes_via_batched_prices():
    c = _conn(); pid = _open(c, entry=0.005)
    def price_fn(mints, http=None):
        return {mt: {"price": 0.002, "liquidity": 1e15} for mt in mints}     # all -60%
    out = run.bracket_pass(c, price_fn=price_fn, now_fn=lambda: "t2")
    assert out and out[0]["exit_reason"] == "stop_loss"
    assert m.get_position(c, pid)["is_open"] == 0


def test_bracket_pass_uses_a_fresh_solana_swap_mark_when_dex_has_no_pair():
    c = _conn(); pid = _open(c, entry=0.005)
    out = run.bracket_pass(
        c,
        price_fn=lambda _mints: {},
        supplemental_price_map={
            ("solana", "M"): {
                "price": 0.002,
                "liquidity": 1e15,
                "source": "fastlane_sol_swap",
            }
        },
        now_fn=lambda: "t2",
    )

    assert out and out[0]["exit_reason"] == "stop_loss"
    assert m.get_position(c, pid)["is_open"] == 0


# ── Regression: EVM checksummed-address key mismatch (multichain feed) ───────
def test_evm_checksum_mint_resolves_batch_and_brackets():
    # DexScreener returns EVM baseToken.address CHECKSUMMED; we store the mint lowercase.
    checksummed = "0x7A848a5A8169aa6a2f603D056A749f924F504444"
    lower = checksummed.lower()

    def http(method, url, headers=None, body=None):
        return 200, {"pairs": [{"chainId": "base", "pairAddress": "base-pair",
                     "baseToken": {"address": checksummed}, "priceUsd": "0.002",
                     "marketCap": "1", "liquidity": {"usd": "1"}, "volume": {"h24": "1"}}]}, None

    pm = q.dexscreener_stats_batch_keyed([("8453", lower)], http=http)
    assert pm.get(("8453", lower)) and pm[("8453", lower)]["price"] == 0.002
    c = _conn(); pid = _open(c, entry=0.005, mint=lower, network_id="8453")
    out = br.evaluate_brackets(c, pm, "t2", sl_pct=60, tp_pct=500)
    assert out and out[0]["exit_reason"] == "stop_loss"          # EVM bracket now fires
    assert m.get_position(c, pid)["is_open"] == 0
