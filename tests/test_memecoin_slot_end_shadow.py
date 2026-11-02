"""Slot-end chase reference, SHADOW ONLY (2026-09-29): memecoin_slot_end_shadow in the capture loop.

Synthetic transactions only; nothing here opens a socket. The rules pinned:
  * the end-of-slot price is the pool state after the LAST transaction in the source's slot;
  * prices come from the pool's coin reserve alone (constant product: spot = k / X^2), so the quote
    side, its virtual part and its accrued fees never matter (Gamma, review of 4a128fae5);
  * the post-trade price is the floor of the reference;
  * every unknown is would_admit NULL with a reason, never 1;
  * no research read while a real Solana position is open or a Solana entry is reserved, nor before
    the slot has settled.
"""
import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin_captures as store
from sniper.services import memecoin_slot_end_shadow as S

ROOT = Path(__file__).resolve().parents[1]
SIG = "5ivfHtRQooXunzZ8KagMx41dSdoTkt4GvX7Mqy2PwTwifj6XaeBoX276NbdwmPAxMM6zbht44JuHz8fuRQZwXDxC"
MINT, USDC = "CoinMint111", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BUYER, POOL, VAULT, QVAULT = "Buyer", "PoolAuth", "CoinVault", "QuoteVault"


def _tx(keys, pre, post, *, lam=None, sig=SIG, index=None, slot=100, ixs=None, inner=None):
    """`ixs`/`inner`: [(program, [account addresses])]. By default each venue program in `keys` gets
    one outer instruction over every account."""
    def bals(d):
        return [{"accountIndex": keys.index(a), "owner": o, "mint": mnt,
                 "uiTokenAmount": {"uiAmount": amt}} for a, (o, mnt, amt) in d.items()]

    def ix(prog, accts):
        return {"programIdIndex": keys.index(prog), "accounts": [keys.index(a) for a in accts]}
    if ixs is None:
        ixs = [(k, keys) for k in keys if k in S.VENUES]
    pre_l, post_l = lam or ([0] * len(keys), [0] * len(keys))
    tx = {"slot": slot, "transaction": {"message": {"accountKeys": list(keys),
                                                    "instructions": [ix(*i) for i in ixs]}, "signatures": [sig]},
          "meta": {"preTokenBalances": bals(pre), "postTokenBalances": bals(post),
                   "preBalances": pre_l, "postBalances": post_l,
                   "innerInstructions": [{"index": 0, "instructions": [ix(*i) for i in inner or []]}]}}
    if index is not None:
        tx["transactionIndex"] = index
    return tx


def _amm(coin_pre, coin_post, q_pre, q_post, *, sig=SIG, index=None, program=S.RAYDIUM_CPMM):
    keys = [BUYER, VAULT, QVAULT, "BuyerCoin", program]
    return _tx(keys, {VAULT: (POOL, MINT, coin_pre), QVAULT: (POOL, USDC, q_pre), "BuyerCoin": (BUYER, MINT, 0.0)},
               {VAULT: (POOL, MINT, coin_post), QVAULT: (POOL, USDC, q_post),
                "BuyerCoin": (BUYER, MINT, coin_pre - coin_post)}, sig=sig, index=index)


def _trace(quote=1.9, source=1.2, cap=30.0, gap=58.0):
    return json.dumps({"substage_events": [{"name": "chase_quote_confirmation", "quote_price": quote,
                                            "source_price": source, "cap_pct": cap, "gap_pct": gap,
                                            "reference_price": None, "reference_basis": "avg_fill"}]})


def _rpc(source_tx, slot_list):
    calls = []

    def rpc(method, params):
        calls.append(method)
        if method == "getTransaction":
            return source_tx
        return {"data": slot_list}
    rpc.calls = calls
    return rpc


ITEM = {"attempt_id": "a1", "origin_event_id": "onchain:" + SIG, "mint": MINT,
        "refused_at": "2026-09-29T06:11:05+00:00"}


def test_prices_come_from_the_coin_reserve_alone():
    """The source takes the coin reserve 1000 -> 900; later in the slot a bot takes it to 800. Source
    fill $1.2: post-trade = 1.2 x 1000/900, slot end = post x (900/800)^2. The quote vault reads
    nonsense on purpose (PumpSwap's balances omit the virtual quote V): it must not matter."""
    src = _amm(1000.0, 900.0, 500.0, 499.0, index=0)
    later = _amm(900.0, 800.0, 499.0, 1.0, sig="Z" * 88, index=1)
    row = S.evaluate(ITEM, _trace(quote=1.9), _rpc(src, [src, later]))
    post = 1.2 * 1000.0 / 900.0
    assert row["reason"] is None and row["venue"] == "raydium_cpmm" and row["in_slot_after"] == 1
    assert (row["coin_pre"], row["coin_post"], row["coin_end"]) == (1000.0, 900.0, 800.0)
    assert row["n_coin_legs"] == 1 and row["vault_program"] == S.RAYDIUM_CPMM
    assert row["ref_post_usd"] == pytest.approx(post)
    assert row["ref_end_slot"] == pytest.approx(post * (900.0 / 800.0) ** 2)
    assert row["gap_end_pct"] == pytest.approx((1.9 / row["ref_end_slot"] - 1) * 100)
    assert row["would_admit"] == 1, "12.6% under a 30% cap"


def test_a_pool_whose_balances_show_no_quote_side_is_still_priced():
    keys = [BUYER, VAULT, "BuyerCoin", S.PUMP_AMM]
    src = _tx(keys, {VAULT: (POOL, MINT, 1000.0), "BuyerCoin": (BUYER, MINT, 0.0)},
              {VAULT: (POOL, MINT, 900.0), "BuyerCoin": (BUYER, MINT, 100.0)}, index=0)
    row = S.evaluate(ITEM, _trace(), _rpc(src, [src]))
    assert row["reason"] is None and row["venue"] == "pumpswap"
    assert row["ref_end_slot"] == pytest.approx(1.2 * 1000.0 / 900.0)


def test_the_post_trade_price_is_the_floor_of_the_reference():
    src = _amm(1000.0, 900.0, 500.0, 560.0, index=3)
    fell = _amm(900.0, 950.0, 560.0, 530.0, sig="Z" * 88, index=4)       # a seller after the source
    row = S.evaluate(ITEM, _trace(quote=1.3), _rpc(src, [src, fell]))
    assert row["coin_end"] == 950.0
    assert row["ref_end_slot"] == pytest.approx(row["ref_post_usd"]) == pytest.approx(1.2 * 1000.0 / 900.0)


def test_the_pump_curve_uses_its_virtual_token_reserve():
    """Virtual tokens = the curve's token account + 73,000,000 (TradeEvent, 16 of 16 source txs)."""
    keys = [BUYER, VAULT, POOL, "BuyerCoin", S.PUMP]
    src = _tx(keys, {VAULT: (POOL, MINT, 700_000_000.0), "BuyerCoin": (BUYER, MINT, 0.0)},
              {VAULT: (POOL, MINT, 690_000_000.0), "BuyerCoin": (BUYER, MINT, 10_000_000.0)}, index=0)
    row = S.evaluate(ITEM, _trace(), _rpc(src, [src]))
    assert row["venue"] == "pump_curve" and row["in_slot_after"] == 0, row
    assert (row["coin_pre"], row["coin_post"]) == (773_000_000.0, 763_000_000.0)
    assert row["ref_post_usd"] == pytest.approx(1.2 * 773_000_000.0 / 763_000_000.0)


def test_the_coin_vault_is_the_coin_account_that_paid_the_buyer():
    """A routed buy SOL -> X -> coin: pool A's vaults move too, but only one coin account fell."""
    keys = [BUYER, VAULT, QVAULT, "AWsol", "AX", "BuyerCoin", S.RAYDIUM_CPMM]
    wsol = "So11111111111111111111111111111111111111112"
    src = _tx(keys,
              {VAULT: (POOL, MINT, 1000.0), QVAULT: (POOL, "XMint", 500.0), "AWsol": (POOL, wsol, 9000.0),
               "AX": (POOL, "XMint", 7000.0), "BuyerCoin": (BUYER, MINT, 0.0)},
              {VAULT: (POOL, MINT, 900.0), QVAULT: (POOL, "XMint", 560.0), "AWsol": (POOL, wsol, 9003.0),
               "AX": (POOL, "XMint", 6940.0), "BuyerCoin": (BUYER, MINT, 100.0)}, index=0)
    pool, why = S.pool_of(src, MINT)
    assert why is None and pool["vault"] == VAULT and (pool["coin_pre"], pool["coin_post"]) == (1000.0, 900.0)


LAUNCHLAB = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"


def test_the_venue_is_the_program_whose_instruction_carries_the_vault():
    """d8b429bc: the coin's pool is LaunchLab, PumpSwap is another hop. Program presence said
    pumpswap and priced a LaunchLab vault as constant product (Gamma N1)."""
    keys = [BUYER, VAULT, "OtherVault", "BuyerCoin", S.PUMP_AMM, LAUNCHLAB]
    pre = {VAULT: (POOL, MINT, 1000.0), "OtherVault": ("P2", "XMint", 50.0), "BuyerCoin": (BUYER, MINT, 0.0)}
    post = {VAULT: (POOL, MINT, 900.0), "OtherVault": ("P2", "XMint", 40.0), "BuyerCoin": (BUYER, MINT, 100.0)}
    src = _tx(keys, pre, post, ixs=[(S.PUMP_AMM, ["OtherVault"]), (LAUNCHLAB, [VAULT])], index=0)
    pool, why = S.pool_of(src, MINT)
    assert why == "venue_unsupported" and pool["vault_program"] == LAUNCHLAB and pool["n_coin_legs"] == 1


def test_a_venue_reached_through_an_inner_instruction_is_found():
    keys = [BUYER, VAULT, "BuyerCoin", "JUP6Router", S.PUMP_AMM]
    src = _tx(keys, {VAULT: (POOL, MINT, 1000.0), "BuyerCoin": (BUYER, MINT, 0.0)},
              {VAULT: (POOL, MINT, 900.0), "BuyerCoin": (BUYER, MINT, 100.0)},
              ixs=[("JUP6Router", keys)], inner=[(S.PUMP_AMM, [VAULT, "BuyerCoin"])], index=0)
    pool, why = S.pool_of(src, MINT)
    assert why is None and pool["venue"] == "pumpswap"
    assert pool["vault_program"] == ",".join(sorted(["JUP6Router", S.PUMP_AMM]))


def test_a_vault_carried_by_two_venues_is_ambiguous():
    keys = [BUYER, VAULT, "BuyerCoin", S.PUMP_AMM, S.RAYDIUM_CPMM]
    src = _tx(keys, {VAULT: (POOL, MINT, 1000.0), "BuyerCoin": (BUYER, MINT, 0.0)},
              {VAULT: (POOL, MINT, 900.0), "BuyerCoin": (BUYER, MINT, 100.0)}, index=0)
    assert S.pool_of(src, MINT)[1] == "venue_ambiguous"


def test_two_paying_coin_vaults_are_a_split_route():
    """A CP leg and a DLMM leg: their X0/X1 differ (1.10 vs 1.02 on f4566263), so no one leg prices
    the source's fill (Gamma N2)."""
    keys = [BUYER, VAULT, "DlmmVault", "BuyerCoin", S.PUMP_AMM]
    src = _tx(keys, {VAULT: (POOL, MINT, 1000.0), "DlmmVault": ("Dlmm", MINT, 500.0), "BuyerCoin": (BUYER, MINT, 0.0)},
              {VAULT: (POOL, MINT, 900.0), "DlmmVault": ("Dlmm", MINT, 480.0), "BuyerCoin": (BUYER, MINT, 120.0)},
              index=0)
    row = S.evaluate(ITEM, _trace(), _rpc(src, [src]))
    assert row["would_admit"] is None and row["reason"] == "split_route" and row["n_coin_legs"] == 2
    assert row["pool_vault"] == VAULT and row["vault_program"] == S.PUMP_AMM


@pytest.mark.parametrize("gap_over_cap, admit", [(-0.01, 1), (0.5, 0)])
def test_the_cap_boundary(gap_over_cap, admit):
    src = _amm(1000.0, 900.0, 500.0, 560.0, index=0)
    ref = 1.2 * 1000.0 / 900.0
    row = S.evaluate(ITEM, _trace(quote=ref * (1 + (30.0 + gap_over_cap) / 100)), _rpc(src, [src]))
    assert row["gap_end_pct"] == pytest.approx(30.0 + gap_over_cap) and row["would_admit"] == admit


def test_a_slot_longer_than_five_pages_is_unknown():
    src = _amm(1000.0, 900.0, 500.0, 560.0, index=0)
    pages = []

    def rpc(method, params):
        if method == "getTransaction":
            return src
        pages.append(params[1].get("paginationToken"))
        return {"data": [src] if len(pages) == 1 else [], "paginationToken": "p%d" % len(pages)}
    row = S.evaluate(ITEM, _trace(), rpc)
    assert pages == [None, "p1", "p2", "p3", "p4"]
    assert row["would_admit"] is None and row["reason"] == "slot_truncated"


def test_a_migrated_curve_at_the_slot_end_is_unknown():
    """Migration empties the curve's token account; X would read as the bare 73M offset."""
    keys = [BUYER, VAULT, POOL, "BuyerCoin", S.PUMP]
    src = _tx(keys, {VAULT: (POOL, MINT, 700_000_000.0), "BuyerCoin": (BUYER, MINT, 0.0)},
              {VAULT: (POOL, MINT, 690_000_000.0), "BuyerCoin": (BUYER, MINT, 10_000_000.0)}, index=0)
    mig = _tx(keys, {VAULT: (POOL, MINT, 690_000_000.0)}, {VAULT: (POOL, MINT, 0.0)}, sig="Z" * 88, index=1)
    row = S.evaluate(ITEM, _trace(), _rpc(src, [src, mig]))
    assert row["would_admit"] is None and row["reason"] == "slot_end_unpriced"


@pytest.mark.parametrize("why, item, trace, src, slot_list, reason", [
    ("no chase trace", ITEM, json.dumps({}), None, [], "no_chase_trace"),
    ("a J7 uuid origin", {**ITEM, "origin_event_id": "swap:9f1c2e2a-uuid"}, _trace(), None, [],
     "no_source_signature"),
    ("unreadable tx", ITEM, _trace(), None, [], "source_tx_unreadable"),
    ("another venue", ITEM, _trace(), _amm(1000.0, 900.0, 500.0, 560.0, program="LaunchLab111"), [],
     "venue_unsupported"),
    ("the source missing from the slot list", ITEM, _trace(), _amm(1000.0, 900.0, 500.0, 560.0), [],
     "source_not_in_slot_list"),
])
def test_every_unknown_is_null_with_a_reason(why, item, trace, src, slot_list, reason):
    row = S.evaluate(item, trace, _rpc(src, slot_list))
    assert row["would_admit"] is None and row["reason"] == reason, why


# ---------------------------------------------------------------------------- the worker

def _worker(flat, now, rpc):
    rows = []
    w = S.Worker(rpc=rpc, trace_fn=lambda aid: _trace(), record_fn=rows.append, flat_fn=lambda: flat,
                 now_fn=lambda: now, sleep_fn=lambda s: None)
    return w, rows


def test_no_research_read_while_a_real_solana_position_is_open():
    src = _amm(1000.0, 900.0, 500.0, 560.0, index=0)
    rpc = _rpc(src, [src])
    w, rows = _worker(False, datetime(2026, 9, 29, 7, 0, tzinfo=timezone.utc), rpc)
    w.submit(ITEM)
    assert w.step() and rpc.calls == [] and rows == [] and w._q.qsize() == 1, "it waits, it keeps the item"


def test_no_read_before_the_slot_has_settled():
    src = _amm(1000.0, 900.0, 500.0, 560.0, index=0)
    rpc = _rpc(src, [src])
    early = datetime.fromisoformat(ITEM["refused_at"]) + timedelta(seconds=5)
    w, rows = _worker(True, early, rpc)
    w.submit(ITEM)
    assert w.step() and rpc.calls == [] and w._q.qsize() == 1


def test_a_flat_settled_book_reads_and_records_one_row():
    src = _amm(1000.0, 900.0, 500.0, 560.0, index=0)
    rpc = _rpc(src, [src])
    w, rows = _worker(True, datetime(2026, 9, 29, 7, 0, tzinfo=timezone.utc), rpc)
    w.submit(ITEM)
    assert w.step()
    assert rpc.calls == ["getTransaction", "getTransactionsForAddress"]
    assert len(rows) == 1 and rows[0]["computed_at"].startswith("2026-09-29T07:00")


def test_a_failure_is_a_null_row_never_a_raise():
    def boom(method, params):
        raise ValueError("bad json")
    w, rows = _worker(True, datetime(2026, 9, 29, 7, 0, tzinfo=timezone.utc), boom)
    w.submit(ITEM)
    w.step()
    assert rows[0]["would_admit"] is None and rows[0]["reason"] == "error:ValueError"


def test_a_research_key_that_is_also_funded_is_refused():
    key = "https://mainnet.helius-rpc.com/?api-key=11111111-2222-3333-4444-555555555555"
    rpc, why = S.rpc_from_env({"MEMECOIN_RESEARCH_HELIUS_RPC_URLS": key, "MEMECOIN_SOLANA_RPC_URL": key})
    assert rpc is None and "also a funded key" in why
    rpc, why = S.rpc_from_env({"MEMECOIN_RESEARCH_HELIUS_RPC_URLS": key,
                               "MEMECOIN_SOLANA_RPC_URL": key.replace("1111", "9999")})
    assert rpc is not None and why is None
    assert S.rpc_from_env({})[0] is None


# ---------------------------------------------------------------------------- the loop and the store

def _loop():
    spec = importlib.util.spec_from_file_location("rcl", ROOT / "scripts/memecoin_refusal_capture_loop.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_store_keeps_the_audit_columns(tmp_path):
    conn = store.connect(tmp_path / "caps.db")
    store.record_slot_end_shadow(conn, {"attempt_id": "a3", "reason": "split_route", "n_coin_legs": 2,
                                        "vault_program": S.PUMP_AMM})
    assert tuple(conn.execute("SELECT n_coin_legs, vault_program FROM memecoin_slot_end_shadow").fetchone()) \
        == (2, S.PUMP_AMM)


def test_the_store_keeps_null_as_null(tmp_path):
    conn = store.connect(tmp_path / "caps.db")
    store.record_slot_end_shadow(conn, {"attempt_id": "a1", "would_admit": None, "reason": "no_source_signature"})
    row = conn.execute("SELECT would_admit, reason FROM memecoin_slot_end_shadow").fetchone()
    assert tuple(row) == (None, "no_source_signature")


def test_the_loop_io_fails_closed_and_writes_on_the_workers_own_connection(tmp_path):
    loop = _loop()
    trace_fn, flat_fn, record_fn = loop.slot_end_io(tmp_path / "missing-ledger.db", tmp_path / "caps.db")
    assert flat_fn() is False, "an unreadable ledger is not a flat book"
    record_fn({"attempt_id": "a2", "would_admit": 1, "reason": None})
    conn = sqlite3.connect(tmp_path / "caps.db")
    assert conn.execute("SELECT would_admit FROM memecoin_slot_end_shadow WHERE attempt_id='a2'").fetchone()[0] == 1


def test_a_polled_refusal_carries_its_attempt_id():
    from sniper.persistence import db_memecoin as m
    loop = _loop()
    ledger = sqlite3.connect(":memory:")
    ledger.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(ledger)
    ledger.execute("INSERT INTO memecoin_entry_attempts (attempt_id, network_id, mint, origin_event_id, "
                   "decision_started_at, terminal_status, terminal_reason, completed_at) VALUES "
                   "(?,?,?,?,?,?,?,?)", ("a9", "solana", MINT, "onchain:" + SIG, "2026-09-29T06:11:05+00:00",
                                         "skipped_chase", "exact quote is +64% above source price (cap 30%)",
                                         "2026-09-29T06:11:06+00:00"))
    out, _cursor = loop.poll_refusals(ledger, since_iso="2026-09-29T06:00:00+00:00")
    assert out and out[0]["attempt_id"] == "a9" and out[0]["gate"] == "chase_quote"


def test_the_slot_end_is_the_state_after_the_last_transaction_in_the_slot():
    src = _amm(1000.0, 900.0, 500.0, 560.0, index=2)
    bot1 = _amm(900.0, 800.0, 560.0, 640.0, sig="Y" * 88, index=5)
    bot2 = _amm(800.0, 700.0, 640.0, 735.0, sig="Z" * 88, index=9)
    row = S.evaluate(ITEM, _trace(quote=2.2), _rpc(src, [src, bot1, bot2]))
    assert row["in_slot_after"] == 2 and row["coin_end"] == 700.0
    assert row["ref_end_slot"] == pytest.approx(1.2 * (1000.0 / 900.0) * (900.0 / 700.0) ** 2)


@pytest.mark.parametrize("network, status, flat", [
    ("solana", "reserved", False), ("56", "reserved", True), ("solana", "confirmed", True)])
def test_a_reserved_solana_entry_is_not_a_flat_book(tmp_path, network, status, flat):
    """The research-read rule is 0 open AND 0 reserved (Gamma M2 on 4a128fae5)."""
    from sniper.persistence import db_memecoin as m
    path = tmp_path / "ledger.db"
    ledger = sqlite3.connect(path)
    m.ensure_memecoin_tables(ledger)
    ledger.execute("INSERT INTO memecoin_pending_entries (mint, network_id, status, reserved_at) "
                   "VALUES (?,?,?,?)", (MINT, network, status, "2026-09-29T07:00:00+00:00"))
    ledger.commit()
    ledger.close()
    _trace_fn, flat_fn, _record_fn = _loop().slot_end_io(path, tmp_path / "caps.db")
    assert flat_fn() is flat
