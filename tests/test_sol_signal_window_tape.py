"""Solana signer window tape (spec docs/superpowers/specs/2026-10-01-memecoin-solana-signer-window-tape-design.md,
build section). The transactions are REAL mainnet reads already in the repo's fixtures; the reader, the
planner and the job run against a fake research key."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FX = ROOT / "tests" / "fixtures"


def _mod():
    spec = importlib.util.spec_from_file_location("sol_signal_window_tape",
                                                  ROOT / "scripts" / "research" / "sol_signal_window_tape.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


T = _mod()
VENUES = json.loads((FX / "solana" / "funded_entry_venues.json").read_text())
ART = json.loads((FX / "solana" / "trade_of_artifacts.json").read_text())
SWAP2 = json.loads((FX / "memecoin" / "damm_v2_swap2_routed.json").read_text())
OURS = "3EKhb6kyNvNhoAtF9CXxBfneTn4A31TxWvKn5Ppj9bLH"            # our funded Solana wallet


def _by_owner(rows):
    return {r["owner"]: r for r in rows}


# --- one transaction: who traded, and the pool side ---------------------------------------------------

def test_a_pump_curve_buy_is_one_on_curve_leg_priced_by_its_trade_event():
    v = VENUES["pump_curve_entry"]
    legs, swaps = T.parse_tx(v["tx"], v["mint"])
    assert [(l["owner"], l["delta"], l["kind"]) for l in legs] == [(OURS, 3719655987608, "swap")]
    assert legs[0]["fee_payer"] == OURS and legs[0]["slot"] == 440266031 and legs[0]["tx_index"] == 1353
    (s,) = swaps                                      # the curve PDA is off-curve: never a trader
    assert s["venue"] == "pump_curve" and s["source"] == "trade_event" and s["coin_delta"] == -3719655987608
    assert s["quote_mint"] == T.WSOL and s["quote_delta"] > 0 and s["cp"] == 1
    assert s["coin_reserve_after"] > 0 and s["quote_reserve_after"] > 0            # virtual, post-trade
    assert s["fee_bps"] is not None


def test_a_pump_curve_sell_reads_the_seller_and_the_curve():
    v = ART["pump_curve_sell_3zjQ"]
    legs, swaps = T.parse_tx(v["tx"], v["mint"])
    assert [(l["delta"], l["kind"]) for l in legs] == [(-11118198443999, "swap")]
    assert [(s["venue"], s["coin_delta"]) for s in swaps] == [("pump_curve", 11118198443999)]


def test_a_pumpswap_buy_pairs_the_coin_vault_with_its_own_quote_vault():
    v = VENUES["pumpswap_entry"]
    legs, swaps = T.parse_tx(v["tx"], v["mint"])
    assert [(l["owner"], l["delta"]) for l in legs] == [(OURS, 475643406075)]
    (s,) = swaps
    assert s["venue"] == "pump_amm" and s["cp"] == 1 and s["source"] == "vaults"
    assert (s["coin_delta"], s["quote_mint"], s["quote_delta"]) == (-475643406075, T.WSOL, 532676249)
    assert (s["coin_reserve_after"], s["quote_reserve_after"]) == (135295675053835, 134164274359)
    assert s["price"] == pytest.approx((532676249 / 1e9) / (475643406075 / 1e6))


def test_a_routed_buy_over_two_pools_gives_one_row_per_pool():
    legs, swaps = T.parse_tx(SWAP2["tx"], SWAP2["coin"])
    assert [(l["owner"], l["delta"]) for l in legs] == [("B9FJomxFnA5WauXkJdh4GBke3NAaGnC8nwEEW6sEGQ7y",
                                                         6345549874403706)]
    by_vault = {s["coin_vault"][:8]: s for s in swaps}
    assert set(by_vault) == {"HxuBjRMW", "7rCXyUqD"}
    damm = by_vault["HxuBjRMW"]
    assert damm["venue"] == "meteora_damm_v2" and damm["quote_vault"][:8] == "Fn4Jh9Vd"
    assert (damm["coin_delta"], damm["quote_delta"]) == (-5958578839070264, 19243860718)
    assert sum(s["coin_delta"] for s in swaps) == -6345549874403706


def test_a_shared_pool_authority_pairs_by_the_swap_instruction_not_by_the_owner():
    # GpMZbSM2 owns the coin vault AND unrelated WSOL/USDC vaults in this route; the owner rule would
    # price the coin in 99,150 lamports of WSOL dust.
    v = ART["multihop_usdc_buy_8nSQ"]
    legs, swaps = T.parse_tx(v["tx"], v["mint"])
    assert [l["delta"] for l in legs] == [18506138461]
    (s,) = [s for s in swaps if s["coin_vault"][:8] == "3LhsRLu1"]
    assert s["quote_vault"][:8] == "83XvQmfu" and s["quote_delta"] == 410667 and s["quote_decimals"] == 8


def test_an_lp_deposit_is_a_transfer_with_no_pool_swap():
    v = ART["damm_v2_lp_deposit_ANzr"]
    legs, swaps = T.parse_tx(v["tx"], v["mint"])
    assert [(l["delta"], l["kind"]) for l in legs] == [(-14770311613, "transfer")]
    assert swaps == []


def test_a_liquidity_withdrawal_beside_a_dust_swap_is_not_a_trade():
    # Signer-tape smoke run 2026-10-02: DLMM pool 6D4EWF22 paid coin AND WSOL to 4K3a2ucX (a withdrawal)
    # while another DLMM pool swapped 7 coin units. Labelled per transaction, the withdrawal read as a
    # 6.6M-unit buy. An owner whose coin and quote both moved the same way, with no quote moving the
    # other way, is a liquidity move.
    v = json.loads((FX / "memecoin" / "sol_tape_dlmm_lp_withdraw.json").read_text())
    legs, swaps = T.parse_tx(v["tx"], v["mint"])
    assert [(l["owner"][:8], l["delta"], l["kind"]) for l in legs] == [("4K3a2ucX", 6608706, "liquidity")]
    assert [(s["coin_delta"], s["quote_delta"]) for s in swaps] == [(-7, 16)]


def test_off_curve_owners_never_become_traders():
    for v in list(VENUES.values()) + [ART["multihop_usdc_buy_8nSQ"], {"tx": SWAP2["tx"], "mint": SWAP2["coin"]}]:
        legs, _ = T.parse_tx(v["tx"], v["mint"])
        assert legs and all(T.on_curve(l["owner"]) for l in legs)


def test_a_failed_transaction_gives_nothing():
    tx = json.loads(json.dumps(VENUES["pumpswap_entry"]["tx"]))
    tx["meta"]["err"] = {"InstructionError": [3, {"Custom": 6004}]}
    assert T.parse_tx(tx, VENUES["pumpswap_entry"]["mint"]) == ([], [])


def test_two_curve_trades_in_one_transaction_keep_two_rows(tmp_path):
    # A bundler can buy for several wallets in one transaction: one TradeEvent each, and the curve
    # vault moves by their sum, so neither event matches the vault.
    import base64
    v = ART["pump_curve_sell_3zjQ"]
    tx = json.loads(json.dumps(v["tx"]))
    line = next(l for l in tx["meta"]["logMessages"] if l.startswith("Program data: ")
                and base64.b64decode(l[14:])[:8] == T.TRADE_EVENT)
    for amount in (12345, 67890):                             # token_amount of two more trades
        raw = bytearray(base64.b64decode(line[14:]))
        raw[48:56] = amount.to_bytes(8, "little")
        tx["meta"]["logMessages"].append("Program data: " + base64.b64encode(bytes(raw)).decode())
    _legs, swaps = T.parse_tx(tx, v["mint"])
    assert sorted(s["coin_delta"] for s in swaps) == [12345, 67890, 11118198443999]
    db = sqlite3.connect(tmp_path / "t.db")
    db.executescript(T.DDL)
    T._store(db, "k", v["mint"], "entry", T.Read(txs=[tx], covered=[(1, 2, 0)]))
    assert db.execute("SELECT COUNT(*) FROM pool_swaps").fetchone()[0] == 3


# --- the window: segments, buckets, coverage ---------------------------------------------------------

def test_the_plan_is_the_spec_read_order():
    segs = T.plan(1000)
    assert [(s.name, s.lo, s.hi, s.bucket) for s in segs] == [
        ("entry", 980, 1030, 0), ("leaders", 850, 979, 0), ("minute", 1031, 1150, 5),
        ("path", 1151, 1750, 12), ("tail", 1751, 5500, 75)]


def test_a_late_landing_extends_the_entry_segment_and_the_rest_start_after_it():
    segs = {s.name: s for s in T.plan(1000, landing=1200)}
    assert (segs["entry"].lo, segs["entry"].hi) == (980, 1205)
    assert "minute" not in segs and (segs["path"].lo, segs["path"].hi) == (1206, 1750)


def test_uncovered_subtracts_equal_or_finer_reads_only():
    cov = [(100, 199, 0), (300, 399, 12)]
    assert T.uncovered(cov, 150, 450, 12) == [(200, 299), (400, 450)]
    assert T.uncovered(cov, 150, 450, 5) == [(200, 450)]          # a 12-slot sample does not cover 5
    assert T.uncovered(cov, 120, 180, 0) == []


class Chain:
    """A fake research key: `slots` maps slot -> transaction count; pages honour slot filters, order,
    limit and a pagination token, like getTransactionsForAddress."""

    def __init__(self, slots, mint="M1"):
        self.slots, self.mint, self.calls = dict(slots), mint, []

    def txs(self, lo, hi, order):
        out = [{"slot": s, "transactionIndex": i, "sig": "%d-%d" % (s, i)}
               for s in sorted(self.slots) if lo <= s <= hi for i in range(self.slots[s])]
        return out if order == "asc" else out[::-1]

    def __call__(self, method, params):
        self.calls.append((method, params))
        assert method == "getTransactionsForAddress" and params[0] == self.mint
        o = params[1]
        f = o["filters"]["slot"]
        txs = self.txs(f["gte"], f["lte"], o["sortOrder"])
        start = int(o.get("paginationToken") or 0)
        page = txs[start:start + o["limit"]]
        token = str(start + o["limit"]) if start + o["limit"] < len(txs) else None
        return {"data": page, "paginationToken": token}


def test_a_dense_read_follows_the_token_to_the_end():
    chain = Chain({100: 150, 120: 30})
    r = T.read_range(chain, "M1", 90, 130, bucket=0)
    assert len(r.txs) == 180 and r.covered == [(90, 130, 0)] and not r.truncated and len(chain.calls) == 2
    assert chain.calls[0][1][1]["maxSupportedTransactionVersion"] == 1     # v1 transactions exist (09-28)


def test_a_capped_dense_read_covers_only_the_slots_it_finished():
    chain = Chain({100: 150, 101: 150, 102: 150})
    r = T.read_range(chain, "M1", 90, 130, bucket=0, max_pages=2)
    assert r.truncated and r.covered == [(90, 100, 0)]               # slot 101 was read only in part
    r = T.read_range(chain, "M1", 90, 130, bucket=0, max_pages=2, order="desc")
    assert r.truncated and r.covered == [(102, 130, 0)]


def test_a_sampled_read_takes_one_page_a_bucket_and_continues_a_page_that_crossed_it():
    # buckets of 5 from 100: [100..104] has 300 txs (one page, then jump), [105..109] has 50 txs at 106
    # and 60 at 112 (a page that crosses into [110..114] continues), [115..119] is quiet.
    chain = Chain({101: 300, 106: 50, 112: 60})
    r = T.read_range(chain, "M1", 100, 119, bucket=5)
    assert r.covered == [(100, 119, 5)] and not r.truncated
    assert [p["first_slot"] for p in r.pages] == [101, 106, 112]
    assert len(r.txs) == 100 + 100 + 10


def test_a_page_that_ends_on_its_bucket_s_last_slot_jumps():
    chain = Chain({104: 300, 106: 10})                       # bucket [100..104]: slot 104 is its last
    r = T.read_range(chain, "M1", 100, 119, bucket=5)
    assert [p["first_slot"] for p in r.pages] == [104, 106] and len(r.txs) == 110


class ShortChain(Chain):
    """A key whose pages hold fewer rows than asked while a continuation token remains, as the
    provider's own documented example does (R2, Codex review 2026-10-06)."""

    def __init__(self, slots, per_page=1, **kw):
        super().__init__(slots, **kw)
        self.per_page = per_page

    def __call__(self, method, params):
        self.calls.append((method, params))
        o = params[1]
        f = o["filters"]["slot"]
        txs = self.txs(f["gte"], f["lte"], o["sortOrder"])
        start = int(o.get("paginationToken") or 0)
        page = txs[start:start + self.per_page]
        token = str(start + self.per_page) if start + self.per_page < len(txs) else None
        return {"data": page, "paginationToken": token}


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_a_short_page_with_a_continuation_token_is_followed(order):
    """The review's repro: slot 101 plus a token, then slot 102. One call kept 101 and recorded
    (100, 110) as covered; the second page was never asked for."""
    chain = ShortChain({101: 1, 102: 1})
    r = T.read_range(chain, "M1", 100, 110, bucket=0, order=order)
    assert sorted(tx["slot"] for tx in r.txs) == [101, 102] and len(chain.calls) == 2
    assert r.covered == [(100, 110, 0)] and not r.truncated


@pytest.mark.parametrize("answers", [
    [{"data": [{"slot": 101}], "paginationToken": "t1"}, {"data": [{"slot": 102}], "paginationToken": "t1"}],
    [{"data": [{"slot": 101}], "paginationToken": "t1"}, {"data": [], "paginationToken": "t2"}],
], ids=["repeated_token", "empty_page_with_token"])
def test_pagination_that_does_not_advance_is_unknown_coverage(answers):
    feed = iter(answers)
    with pytest.raises(T.ReadStop):
        T.read_range(lambda m, p: next(feed), "M1", 100, 110, bucket=0)


def test_a_read_stops_before_any_call_once_the_book_opens():
    chain = Chain({100: 10})
    with pytest.raises(T.ReadStop):
        T.read_range(chain, "M1", 90, 130, bucket=0, flat_fn=lambda: False)
    assert chain.calls == []


# --- the job --------------------------------------------------------------------------------------

LEDGER_DDL = """
CREATE TABLE memecoin_entry_attempts (attempt_id TEXT PRIMARY KEY, origin_event_id TEXT, delivery_lane TEXT,
  signal_source TEXT, network_id TEXT, mint TEXT, source_user_id TEXT, source_handle TEXT, side TEXT,
  decision_started_at TEXT NOT NULL, fill_id INTEGER, tx_sig TEXT, terminal_status TEXT);
CREATE TABLE memecoin_positions (id INTEGER PRIMARY KEY, execution_mode TEXT, network_id TEXT, is_open INTEGER,
  tracking_paused_at TEXT);
CREATE TABLE memecoin_pending_entries (id INTEGER PRIMARY KEY, status TEXT, network_id TEXT);
"""


def _ledger(tmp_path, attempts, open_solana=False):
    p = tmp_path / "ledger.db"
    c = sqlite3.connect(p)
    c.executescript(LEDGER_DDL)
    for a in attempts:
        c.execute("INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, delivery_lane, signal_source, "
                  "network_id, mint, source_user_id, source_handle, side, decision_started_at, fill_id, tx_sig) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (a["id"], a["origin"], a.get("lane"), a.get("src"), a.get("net", "solana"), a["mint"], "u1", "h1",
                   a.get("side", "buy"), a["at"], a.get("fill_id"), a.get("tx_sig")))
    if open_solana:
        c.execute("INSERT INTO memecoin_positions VALUES (1, 'real', 'solana', 1, NULL)")
    c.commit()
    c.close()
    return p


SIG_A, SIG_B, OUR_TX = "5" * 87 + "A", "4" * 87 + "B", "3" * 87 + "C"


def test_signals_keep_signature_lanes_and_count_the_rest_out_of_scope(tmp_path):
    led = sqlite3.connect(_ledger(tmp_path, [
        {"id": "a1", "origin": "slotlane:" + SIG_A, "mint": "M1", "at": "2026-10-02T01:00:00+00:00"},
        {"id": "a2", "origin": "onchain:" + SIG_A, "mint": "M1", "at": "2026-10-02T01:00:01+00:00"},
        {"id": "a3", "origin": "swap:raybot:%s:Trader:M2" % SIG_B, "mint": "M2", "at": "2026-10-02T01:00:02+00:00",
         "fill_id": 7, "tx_sig": OUR_TX},
        {"id": "a4", "origin": "pump:%s:M3:buy:Addr" % SIG_B, "mint": "M3", "at": "2026-10-02T01:00:03+00:00"},
        {"id": "a5", "origin": "swap:0d3869c0-0035-4f31-b457-43df14196011", "mint": "M4",
         "at": "2026-10-02T01:00:04+00:00"},
        {"id": "a6", "origin": "manual:x", "mint": "M5", "at": "2026-10-02T01:00:05+00:00"},
        {"id": "a7", "origin": "onchain:" + SIG_A, "mint": "M1", "at": "2026-10-02T01:00:06+00:00", "net": "4663"},
        {"id": "a8", "origin": "onchain:" + SIG_A, "mint": "M1", "at": "2026-10-02T01:00:07+00:00", "side": "sell"},
    ]))
    sigs, out = T.signals_from_ledger(led, "2026-10-02T00:00:00+00:00")
    assert sorted(sigs) == sorted([SIG_A + ":M1", SIG_B + ":M2", SIG_B + ":M3"])
    assert sigs[SIG_A + ":M1"]["attempts"] == ["a1", "a2"] and sigs[SIG_B + ":M2"]["landing_sig"] == OUR_TX
    assert out == 2


class Key:
    """The research key for a whole job: statuses, the tip, and one mint's transactions."""

    def __init__(self, slots, tip, chain):
        self.slots, self.tip, self.chain, self.calls = slots, tip, chain, []

    def __call__(self, method, params):
        self.calls.append(method)
        if method == "getSignatureStatuses":
            return {"value": [{"slot": self.slots[s], "err": None} if s in self.slots else None for s in params[0]]}
        if method == "getSlot":
            return self.tip
        return self.chain(method, params)


def _db(tmp_path):
    return tmp_path / "tape.db"


def test_a_window_is_read_only_after_it_closes_and_then_once(tmp_path):
    led = _ledger(tmp_path, [{"id": "a1", "origin": "slotlane:" + SIG_A, "mint": "M1",
                              "at": "2026-10-02T01:00:00+00:00"}])
    key = Key({SIG_A: 10_000}, tip=10_000 + 4500, chain=Chain({10_000: 3, 10_200: 2}))
    r = T.run(led, _db(tmp_path), rpc=key, since="2026-10-02T00:00:00+00:00")
    assert r["read"] == 0 and r["waiting"] == 1                      # S + 4500 + 32 is not yet past
    key.tip = 10_000 + 4500 + 33
    r = T.run(led, _db(tmp_path), rpc=key, since="2026-10-02T00:00:00+00:00")
    assert r["read"] == 1
    db = sqlite3.connect(_db(tmp_path))
    assert db.execute("SELECT status, s_slot FROM signals").fetchall() == [("read", 10_000)]
    n = len(key.chain.calls)
    assert T.run(led, _db(tmp_path), rpc=key, since="2026-10-02T00:00:00+00:00")["read"] == 0
    assert len(key.chain.calls) == n


def test_a_second_signal_on_the_same_mint_reads_only_what_the_first_did_not(tmp_path):
    led = _ledger(tmp_path, [
        {"id": "a1", "origin": "slotlane:" + SIG_A, "mint": "M1", "at": "2026-10-02T01:00:00+00:00"},
        {"id": "a2", "origin": "onchain:" + SIG_B, "mint": "M1", "at": "2026-10-02T01:00:10+00:00"}])
    key = Key({SIG_A: 10_000, SIG_B: 10_010}, tip=20_000, chain=Chain({10_000: 3}))
    T.run(led, _db(tmp_path), rpc=key, since="2026-10-02T00:00:00+00:00")
    db = sqlite3.connect(_db(tmp_path))
    ranges = [tuple(p[1][1]["filters"]["slot"].values()) for p in key.chain.calls]
    assert len(ranges) == len(set(ranges))
    lo_hi = sorted((r[0], r[1]) for r in ranges)
    # the second window's entry segment (9,990..10,040) is dense already read up to 10,030
    assert (10_031, 10_040) in lo_hi
    assert db.execute("SELECT COUNT(*) FROM signals WHERE status='read'").fetchone()[0] == 2


def test_an_open_book_reads_nothing_and_leaves_windows_pending(tmp_path):
    led = _ledger(tmp_path, [{"id": "a1", "origin": "slotlane:" + SIG_A, "mint": "M1",
                              "at": "2026-10-02T01:00:00+00:00"}], open_solana=True)
    key = Key({SIG_A: 10_000}, tip=20_000, chain=Chain({10_000: 3}))
    r = T.run(led, _db(tmp_path), rpc=key, since="2026-10-02T00:00:00+00:00")
    assert key.calls == [] and r["why"] == "solana book open"
    assert sqlite3.connect(_db(tmp_path)).execute("SELECT status FROM signals").fetchall() == [("pending",)]


def test_an_unknown_signature_is_retried_then_unresolved(tmp_path):
    led = _ledger(tmp_path, [{"id": "a1", "origin": "slotlane:" + SIG_A, "mint": "M1",
                              "at": "2026-10-02T01:00:00+00:00"}])
    key = Key({}, tip=20_000, chain=Chain({}))
    for _ in range(3):
        T.run(led, _db(tmp_path), rpc=key, since="2026-10-02T00:00:00+00:00")
    assert sqlite3.connect(_db(tmp_path)).execute("SELECT status FROM signals").fetchall() == [("unresolved",)]


def test_a_read_stopped_mid_window_stays_pending_and_keeps_its_finished_segments(tmp_path):
    led = _ledger(tmp_path, [{"id": "a1", "origin": "slotlane:" + SIG_A, "mint": "M1",
                              "at": "2026-10-02T01:00:00+00:00"}])
    chain = Chain({10_000: 3})
    key = Key({SIG_A: 10_000}, tip=20_000, chain=chain)
    real = chain.__call__

    def quota_after_two(method, params):
        if len(chain.calls) >= 2:
            raise T.ReadStop("max usage")
        return real(method, params)
    key.chain = quota_after_two
    r = T.run(led, _db(tmp_path), rpc=key, since="2026-10-02T00:00:00+00:00")
    db = sqlite3.connect(_db(tmp_path))
    assert r["why"].startswith("max usage") and db.execute("SELECT status FROM signals").fetchall() == [("pending",)]
    assert db.execute("SELECT COUNT(*) FROM coverage").fetchone()[0] >= 1
    key.chain = chain
    n = len(chain.calls)
    T.run(led, _db(tmp_path), rpc=key, since="2026-10-02T00:00:00+00:00")
    assert db.execute("SELECT status FROM signals").fetchall() == [("read",)]
    assert (9_980, 10_030) not in [tuple(p[1][1]["filters"]["slot"].values()) for p in chain.calls[n:]]


def test_the_first_run_needs_a_start_and_later_runs_keep_it(tmp_path):
    led = _ledger(tmp_path, [])
    key = Key({}, tip=20_000, chain=Chain({}))
    with pytest.raises(SystemExit):
        T.run(led, _db(tmp_path), rpc=key, since=None)
    T.run(led, _db(tmp_path), rpc=key, since="2026-10-02T00:00:00+00:00")
    assert T.run(led, _db(tmp_path), rpc=key, since=None)["since"] == "2026-10-02T00:00:00+00:00"


def test_a_rerun_never_reads_a_finished_segment_of_the_same_signal_again(tmp_path):
    # A busy coin: the leaders segment is cut by its 5 + 5 pages by design. A run stopped later in the
    # window must not spend another 10 pages on the leaders' middle when it resumes.
    led = _ledger(tmp_path, [{"id": "a1", "origin": "slotlane:" + SIG_A, "mint": "M1",
                              "at": "2026-10-02T01:00:00+00:00"}])
    chain = Chain({s: 120 for s in range(9_850, 9_980)})
    key = Key({SIG_A: 10_000}, tip=20_000, chain=chain)
    real = chain.__call__

    def stop_in_minute(method, params):
        if params[1]["filters"]["slot"]["gte"] >= 10_031:
            raise T.ReadStop("max usage")
        return real(method, params)
    key.chain = stop_in_minute
    T.run(led, _db(tmp_path), rpc=key, since="2026-10-02T00:00:00+00:00")
    leaders = lambda calls: [c for c in calls if c[1][1]["filters"]["slot"]["lte"] <= 9_979]   # noqa: E731
    assert len(leaders(chain.calls)) == 10
    key.chain = chain
    n = len(chain.calls)
    T.run(led, _db(tmp_path), rpc=key, since="2026-10-02T00:00:00+00:00")
    assert leaders(chain.calls[n:]) == []


def test_a_truncated_entry_stays_flagged_across_a_stop_and_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "ENTRY_SAFETY_PAGES", 1)
    led = _ledger(tmp_path, [{"id": "a1", "origin": "slotlane:" + SIG_A, "mint": "M1",
                              "at": "2026-10-02T01:00:00+00:00"}])
    chain = Chain({10_000: 150})
    key = Key({SIG_A: 10_000}, tip=20_000, chain=chain)
    real = chain.__call__

    def stop_after_entry(method, params):
        if params[1]["filters"]["slot"]["lte"] < 9_980:
            raise T.ReadStop("max usage")
        return real(method, params)
    key.chain = stop_after_entry
    T.run(led, _db(tmp_path), rpc=key, since="2026-10-02T00:00:00+00:00")
    key.chain = chain
    T.run(led, _db(tmp_path), rpc=key, since="2026-10-02T00:00:00+00:00")
    assert sqlite3.connect(_db(tmp_path)).execute("SELECT status, truncated FROM signals").fetchall() == [
        ("truncated", 1)]


def test_the_research_key_stop_becomes_a_read_stop(monkeypatch):
    import sys
    sys.path.insert(0, str(ROOT / "scripts" / "research"))
    import funded_exit_truth as fet

    def quota(method, params):
        raise fet.Stop("HTTP 429 max usage")
    monkeypatch.setattr(fet, "rpc", quota)
    with pytest.raises(T.ReadStop, match="max usage"):
        T._research_rpc()("getSlot", [])


def test_a_landing_that_appears_after_s_resolved_still_gets_its_slot(tmp_path):
    # Gamma LOW 3: landing slots were looked up only while S was unknown, so a fill recorded after S
    # resolved never stretched the entry segment.
    p = _ledger(tmp_path, [{"id": "a1", "origin": "slotlane:" + SIG_A, "mint": "M1", "at": "2026-10-02T01:00:00+00:00"}])
    key = Key({SIG_A: 10_000, OUR_TX: 10_040}, tip=10_100, chain=Chain({}))
    T.run(p, _db(tmp_path), rpc=key, since="2026-10-02T00:00:00+00:00")
    led = sqlite3.connect(p)
    led.execute("UPDATE memecoin_entry_attempts SET fill_id=7, tx_sig=? WHERE attempt_id='a1'", (OUR_TX,))
    led.commit()
    T.run(p, _db(tmp_path), rpc=key, since="2026-10-02T00:00:00+00:00")
    assert sqlite3.connect(_db(tmp_path)).execute("SELECT s_slot, landing_slot FROM signals").fetchall() == [
        (10_000, 10_040)]


def test_the_book_is_checked_before_the_status_and_tip_calls_too(tmp_path):
    # Gamma LOW 4: the book opening after the run-start check must stop the run before getSlot.
    p = _ledger(tmp_path, [{"id": "a1", "origin": "slotlane:" + SIG_A, "mint": "M1", "at": "2026-10-02T01:00:00+00:00"}])
    key = Key({SIG_A: 10_000}, tip=20_000, chain=Chain({10_000: 1}))
    real = key.__call__

    def open_after_statuses(method, params):
        out = real(method, params)
        if method == "getSignatureStatuses":
            led = sqlite3.connect(p)
            led.execute("INSERT INTO memecoin_positions VALUES (1, 'real', 'solana', 1, NULL)")
            led.commit()
        return out
    r = T.run(p, _db(tmp_path), rpc=open_after_statuses, since="2026-10-02T00:00:00+00:00")
    assert key.calls == ["getSignatureStatuses"] and "book open" in r["why"]


def test_a_reserved_entry_with_no_network_counts_as_solana(tmp_path):
    p = _ledger(tmp_path, [{"id": "a1", "origin": "slotlane:" + SIG_A, "mint": "M1", "at": "2026-10-02T01:00:00+00:00"}])
    led = sqlite3.connect(p)
    led.execute("INSERT INTO memecoin_pending_entries VALUES (1, 'reserved', NULL)")
    led.commit()
    key = Key({SIG_A: 10_000}, tip=20_000, chain=Chain({}))
    assert T.run(p, _db(tmp_path), rpc=key, since="2026-10-02T00:00:00+00:00")["why"] == "solana book open"
    assert key.calls == []

@pytest.mark.parametrize('reply', [None, {}, {'error': {'code': -32000}}, {'data': None}, {'data': 'wrong'}])
def test_provider_failure_cannot_be_recorded_as_quiet_complete_window(reply):
    with pytest.raises(T.ReadStop, match='invalid transaction page'):
        T.read_range(lambda *_: reply, 'mint', 100, 200, bucket=0)


def test_explicit_successful_empty_page_can_prove_quiet_window():
    r = T.read_range(lambda *_: {'data': []}, 'mint', 100, 200, bucket=0)
    assert r.covered == [(100, 200, 0)] and r.txs == []
