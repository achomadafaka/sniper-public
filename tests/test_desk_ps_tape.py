"""PumpSwap charts (operator order (a), 2026-10-23): the canonical pool's swaps from data/desk/pumpswap/tape.db feed the
structure block (JUDGE, RISK), RISK's 1 s bars and the attention tape, so a coin is not blind after graduation."""
import json
import sqlite3

import pytest

import test_desk_structure_tape as T
from sniper.services import memecoin_desk_ps_tape as PT
from sniper.services import memecoin_desk_structure_tape as ST

MINT = "AEPBdj3RViMJKCHM1xiVaM1VNuP7VbZRSN68gu5xpump"
POOL = PT.canonical_pool(MINT)
NOW, SOL = T.NOW, T.SOL
COLS = ("pool, mint, quote_mint, slot, signature, ordinal, is_buy, actor, coin_creator, base_atomic, quote_atomic, "
        "post_base_reserve_atomic, post_quote_reserve_atomic, block_time, received_at, payload_json, "
        "pre_base_reserve_atomic, pre_quote_reserve_atomic, tx_index")


def ps_tape(desk, rows, *, gaps=(), proof=0):
    (desk / "pumpswap").mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(PT.tape_path(desk)))
    c.execute("CREATE TABLE swaps_ps(%s, PRIMARY KEY(signature, ordinal))" % COLS)
    c.execute("CREATE INDEX IF NOT EXISTS swaps_ps_received ON swaps_ps(received_at)")
    c.execute("CREATE INDEX IF NOT EXISTS swaps_ps_pool_slot ON swaps_ps(pool,slot)")
    c.execute("CREATE TABLE gaps_ps(at REAL, cause TEXT, last_slot INTEGER)")
    proof_payload = json.dumps({"ix_name": "buy", "virtual_quote_source": PT.VQ_SOURCE,
                                "virtual_quote_atomic": str(proof), "virtual_quote_reserves": proof}) if proof is not None else "{}"
    base = 1_000_000_000_000                            # 1e6 tokens left in the pool
    state = lambda price_usd: str(int(price_usd / SOL * (base / 1e6) * 1e9))     # noqa: E731  quote reserve at a price
    prev = None
    for i, (rel, price_usd, buy, usd, actor, slot, pool, qmint) in enumerate(rows):
        post_q = state(price_usd)                       # the pool AFTER swap i; a sell's post is only the next row's pre
        pre_q, prev = (prev if prev is not None else post_q), post_q
        c.execute("INSERT INTO swaps_ps VALUES(%s)" % ",".join("?" * 19),
                  (pool, MINT, qmint, slot, "ps%d" % i, 0, buy, actor, "CR", "1000", str(int(usd / SOL * 1e9)),
                   str(base) if buy else None, post_q if buy else None, int(NOW + rel), NOW + rel,
                   proof_payload if i == 0 else "{}", str(base), pre_q, 0))
    c.executemany("INSERT INTO gaps_ps VALUES(?,?,?)", [(NOW + g[0], g[1], 0) if isinstance(g, tuple) else (NOW + g, "worker_start", 0)
                                                        for g in gaps])
    c.commit(); c.close()


def curve(tmp, swaps=T.BASE, **kw):
    T.MINT = MINT                                        # the curve builder writes its module MINT
    try:
        return T.build(tmp, swaps, **kw)
    finally:
        T.MINT = "MintStruct"


ROWS = [(-8, 2.0, 1, 50, "P1", 170, POOL, PT.WSOL_MINT), (-6, 2.2, 1, 60, "P2", 175, POOL, PT.WSOL_MINT),
        (-4, 2.1, 0, 20, "P3", 180, POOL, PT.WSOL_MINT), (-3, 2.15, 1, 10, "P4", 181, POOL, PT.WSOL_MINT)]


def test_rows_price_from_post_reserves_plus_the_proven_virtual_quote(tmp_path):
    desk = curve(tmp_path)
    ps_tape(desk, ROWS + [(-2.5, 9.0, 1, 5, "X", 182, "OtherPool", PT.WSOL_MINT),     # another pool: never read
                          (-2, 9.0, 1, 5, "Y", 183, POOL, "USDC")])                    # non-wSOL row: skipped
    rows = PT.load_ps_swaps(desk, MINT, NOW - 60, NOW, SOL)
    assert [r["signature"] for r in rows] == ["ps0", "ps1", "ps2", "ps3"]
    # the sell (ps2) is priced from the next swap's pre state, i.e. the pool right after it
    assert [round(r["price_usd"], 4) for r in rows] == [2.0, 2.2, 2.1, 2.15] and rows[0]["usd"] == pytest.approx(50)


def test_the_last_sell_in_the_window_stays_unpriced(tmp_path):
    desk = curve(tmp_path)
    ps_tape(desk, ROWS[:3])
    rows = PT.load_ps_swaps(desk, MINT, NOW - 60, NOW, SOL)
    assert rows[-1]["is_buy"] == 0 and rows[-1]["price_usd"] is None and rows[-1]["usd"] == pytest.approx(20)


def test_no_proven_virtual_quote_prices_nothing_but_keeps_the_flow(tmp_path):
    desk = curve(tmp_path)
    ps_tape(desk, ROWS, proof=None)
    rows = PT.load_ps_swaps(desk, MINT, NOW - 60, NOW, SOL)
    assert len(rows) == 4 and all(r["price_usd"] is None for r in rows) and rows[1]["usd"] == pytest.approx(60)


def test_no_pumpswap_tape_is_no_rows(tmp_path):
    assert PT.load_ps_swaps(curve(tmp_path), MINT, NOW - 60, NOW, SOL) == []


def test_once_the_pool_trades_the_chart_is_the_pool_only(tmp_path):
    desk = curve(tmp_path)
    before = ST.structure_for(MINT, desk_dir=desk, as_of=NOW, read_margin_s=0.0)
    ps_tape(desk, ROWS)
    after = ST.structure_for(MINT, desk_dir=desk, as_of=NOW, read_margin_s=0.0)
    assert before["price_usd"] == pytest.approx(1.7, rel=1e-6)
    assert after["tape"] == "ok" and after["price_usd"] == pytest.approx(2.15, rel=1e-6)  # the newest swap is the pool's
    assert after["meta"]["ps_swaps_read"] == 4 and after["windows"]["15s"]["n_swaps"] == 4      # pool only: no venue step
    assert after["meta"]["venue"] == "pumpswap"


def test_a_pumpswap_capture_gap_blinds_only_a_coin_with_pool_rows(tmp_path):
    desk = curve(tmp_path)
    ps_tape(desk, [], gaps=((-30, "worker_start"),))
    assert ST.structure_for(MINT, desk_dir=desk, as_of=NOW, read_margin_s=0.0)["meta"]["gaps_in_range"] == 0
    desk2 = curve(tmp_path / "b")
    ps_tape(desk2, ROWS, gaps=((-30, "worker_start"), (-29, "decode_loss"), (-28, "virtual_quote_unproven")))
    assert ST.structure_for(MINT, desk_dir=desk2, as_of=NOW, read_margin_s=0.0)["meta"]["gaps_in_range"] == 1   # decode losses excluded


def test_a_null_pool_row_is_blind_never_a_skip(tmp_path):
    desk = curve(tmp_path)
    ps_tape(desk, ROWS)
    c = sqlite3.connect(str(PT.tape_path(desk)))
    c.execute("UPDATE swaps_ps SET actor=NULL WHERE signature='ps1'")
    c.commit(); c.close()
    out = ST.structure_for(MINT, desk_dir=desk, as_of=NOW, read_margin_s=0.0)
    assert out["tape"] == "unknown" and out["reason"].startswith("tape_unreadable")


def test_bars_are_one_second_candles_in_sol_per_token(tmp_path):
    desk = curve(tmp_path)
    ps_tape(desk, ROWS)
    bars = PT.bars_1s(PT.load_ps_swaps(desk, MINT, NOW - 60, NOW, SOL))
    assert [b["t"] for b in bars] == [int(NOW - 8), int(NOW - 6), int(NOW - 4), int(NOW - 3)]
    assert bars[0]["c"] == pytest.approx(2.0 / SOL) and bars[2]["c"] == pytest.approx(2.1 / SOL)
    assert bars[2]["sell_usd"] == pytest.approx(20)


def _watcher():
    import importlib.util
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    spec = importlib.util.spec_from_file_location("rw_ps", root / "scripts/memecoin_desk_risk_watcher.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _candles(desk, rows):
    (desk / "candles_1s").mkdir(exist_ok=True)
    c = sqlite3.connect(str(desk / "candles_1s" / (T.DAY + ".db")))
    c.execute("CREATE TABLE candles_1s(pool TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL, buy_usd REAL, sell_usd REAL, "
              "n_swaps INTEGER, n_makers INTEGER, slot_lo INTEGER, slot_hi INTEGER)")
    c.executemany("INSERT INTO candles_1s VALUES(?,?,?,?,?,?,?,?,1,1,0,0)", rows)
    c.commit(); c.close()


def test_risk_bars_continue_on_the_pool_after_the_last_curve_bar(tmp_path):
    W = _watcher()
    desk = curve(tmp_path)
    _candles(desk, [("CURVE", int(NOW - 20), 0.01, 0.01, 0.01, 0.017, 1, 0), ("CURVE", int(NOW - 7), 0.02, 0.02, 0.02, 0.02, 1, 0)])
    ps_tape(desk, ROWS)
    bars = W.bars_since(desk, "CURVE", NOW - 30, NOW, mint=MINT)
    assert [b["t"] for b in bars] == [int(NOW - 20), int(NOW - 7), int(NOW - 6), int(NOW - 4), int(NOW - 3)]  # -8 overlaps
    assert W.bars_since(desk, "CURVE", NOW - 30, NOW) == bars[:2]                               # no mint: curve only


def test_risk_bars_leave_the_pool_out_without_a_fresh_sol_price(tmp_path):
    W = _watcher()
    desk = curve(tmp_path, heartbeat={"ts": NOW - 60})
    _candles(desk, [("CURVE", int(NOW - 20), 0.01, 0.01, 0.01, 0.017, 1, 0)])
    ps_tape(desk, ROWS)
    assert [b["t"] for b in W.bars_since(desk, "CURVE", NOW - 30, NOW, mint=MINT)] == [int(NOW - 20)]


def test_attention_tape_reads_the_pool_after_graduation(tmp_path):
    from sniper.services import memecoin_desk_attention_tape as AT
    desk = curve(tmp_path, launch=None)                          # launch not retained: creator from the pool
    ps_tape(desk, ROWS, gaps=(-5,))
    rows = AT.load_swaps(desk, MINT, NOW - 60, NOW, SOL)
    assert {"ps0", "ps1", "ps2", "ps3"} <= {r["signature"] for r in rows} and rows[-1]["signature"] == "ps3"
    assert [r["received_at"] for r in rows] == sorted(r["received_at"] for r in rows)       # one stream, receipt order
    assert AT.creator_of(desk, MINT, NOW) == "CR"
    assert [g[1] for g in AT.gaps_between(desk, NOW - 60, NOW, mint=MINT)] == ["pumpswap"]
    assert AT.gaps_between(desk, NOW - 60, NOW) == []                                        # no mint: curve gaps only


def test_attention_tape_read_error_is_its_own_tape_unreadable(tmp_path):
    from sniper.services import memecoin_desk_attention_tape as AT
    desk = curve(tmp_path)
    ps_tape(desk, ROWS)
    c = sqlite3.connect(str(PT.tape_path(desk)))
    c.execute("UPDATE swaps_ps SET actor=NULL")
    c.commit(); c.close()
    with pytest.raises(AT.TapeUnreadable):
        AT.load_swaps(desk, MINT, NOW - 60, NOW, SOL)



def test_an_unmapped_pool_with_null_mint_and_quote_is_still_read(tmp_path):
    desk = curve(tmp_path)
    ps_tape(desk, ROWS)
    c = sqlite3.connect(str(PT.tape_path(desk)))
    c.execute("UPDATE swaps_ps SET mint=NULL, quote_mint=NULL")
    c.commit(); c.close()
    assert len(PT.load_ps_swaps(desk, MINT, NOW - 60, NOW, SOL)) == 4


def test_a_non_null_mismatch_is_rejected(tmp_path):
    desk = curve(tmp_path)
    ps_tape(desk, ROWS)
    c = sqlite3.connect(str(PT.tape_path(desk)))
    c.execute("UPDATE swaps_ps SET mint='SomeOtherMint' WHERE signature='ps0'")
    c.commit(); c.close()
    assert [r["signature"] for r in PT.load_ps_swaps(desk, MINT, NOW - 60, NOW, SOL)] == ["ps1", "ps2", "ps3"]


def test_a_lost_swap_of_this_pool_is_its_own_gap(tmp_path):
    desk = curve(tmp_path)
    ps_tape(desk, ROWS)
    c = sqlite3.connect(str(PT.tape_path(desk)))
    c.execute("UPDATE swaps_ps SET pre_quote_reserve_atomic='123' WHERE signature='ps1'")   # ps0's post != ps1's pre
    c.commit(); c.close()
    rows, gaps = PT.load_ps_swaps(desk, MINT, NOW - 60, NOW, SOL, with_gaps=True)
    assert gaps == [NOW - 6]
    # Gamma (b): one price-only print at ps1 = its pre state (the pool right after the lost swap), no flow
    [print_] = [r for r in rows if r.get("price_only")]
    assert print_["received_at"] == NOW - 6 and print_["usd"] == 0.0 and print_["actor"] is None
    out = ST.structure_for(MINT, desk_dir=desk, as_of=NOW, read_margin_s=0.0)
    w15 = out["windows"]["15s"]
    assert out["meta"]["gaps_in_range"] == 0 and w15["state"] == "ok"                     # counted, not blind
    assert (w15["swaps_lost"], w15["flow_is_lower_bound"], w15["n_swaps"]) == (1, True, 4)   # the print is not a swap


def test_three_lost_swaps_in_a_window_blind_it(tmp_path):
    desk = curve(tmp_path)
    buys = [(-9 + i, 2.0 + i / 10, 1, 10, "B%d" % i, 170 + i, POOL, PT.WSOL_MINT) for i in range(5)]
    ps_tape(desk, buys)
    c = sqlite3.connect(str(PT.tape_path(desk)))
    c.execute("UPDATE swaps_ps SET pre_quote_reserve_atomic='123' WHERE signature IN ('ps1', 'ps2', 'ps3')")
    c.commit(); c.close()
    w15 = ST.structure_for(MINT, desk_dir=desk, as_of=NOW, read_margin_s=0.0)["windows"]["15s"]
    assert (w15["state"], w15["reason"]) == ("unknown", "swaps_lost")


def test_a_graduated_curve_whose_pool_reads_nothing_is_blind(tmp_path):
    desk = curve(tmp_path)
    c = sqlite3.connect(str(desk / "swaps" / (T.DAY + ".db")))
    c.execute("ALTER TABLE swaps ADD COLUMN real_quote INTEGER")
    c.execute("UPDATE swaps SET real_quote=? WHERE signature='sig6'", (PT.GRADUATION_REAL_SOL,))
    c.commit(); c.close()
    out = ST.structure_for(MINT, desk_dir=desk, as_of=NOW, read_margin_s=0.0)
    assert out["tape"] == "unknown" and out["reason"] == "pool_unread"
    c = sqlite3.connect(str(desk / "swaps" / (T.DAY + ".db")))
    c.execute("UPDATE swaps SET real_quote=1")
    c.commit(); c.close()
    assert ST.structure_for(MINT, desk_dir=desk, as_of=NOW, read_margin_s=0.0)["tape"] == "ok"   # not graduated: curve chart


REAL = json.loads((__import__("pathlib").Path(__file__).parent / "fixtures/memecoin/ps_tape_slice_20261023.json").read_text())


def _real_tape(tmp_path, label):
    """A read-only slice of the live PumpSwap tape (2026-10-23): 'unmapped' = 2DCcA4Nh's pool, every row mint/quote NULL;
    'mapped' = a mapped canonical pool. Gamma's two d0ece5d6f bugs reproduce on these rows, never on synthetic ones."""
    desk = tmp_path / "desk"
    (desk / "pumpswap").mkdir(parents=True)
    c = sqlite3.connect(str(PT.tape_path(desk)))
    cols = REAL["columns"]
    c.execute("CREATE TABLE swaps_ps(%s)" % ",".join(cols))
    c.execute("CREATE INDEX IF NOT EXISTS swaps_ps_received ON swaps_ps(received_at)")
    c.execute("CREATE INDEX IF NOT EXISTS swaps_ps_pool_slot ON swaps_ps(pool,slot)")
    c.execute("CREATE TABLE gaps_ps(at REAL, cause TEXT, last_slot INTEGER)")
    p = REAL["pools"][label]
    c.executemany("INSERT INTO swaps_ps VALUES(%s)" % ",".join("?" * len(cols)), p["rows"])
    c.commit(); c.close()
    return desk, p


@pytest.mark.parametrize("label", ["unmapped", "mapped"])
def test_real_tape_pool_rows_are_read_and_priced(tmp_path, label):
    desk, p = _real_tape(tmp_path, label)
    assert PT.canonical_pool(p["mint"]) == p["pool"]
    ts = [r[REAL["columns"].index("received_at")] for r in p["rows"]]
    rows, gaps = PT.load_ps_swaps(desk, p["mint"], min(ts), max(ts) + 1, 180.0, with_gaps=True)
    assert len(rows) == len(p["rows"])                                     # unmapped NULL rows included (BUG 1)
    priced = sum(r["price_usd"] is not None for r in rows)
    payloads = [r[REAL["columns"].index("payload_json")] for r in sorted(
        p["rows"], key=lambda r: (r[REAL["columns"].index("slot")], r[REAL["columns"].index("tx_index")] or -1,
                                  r[REAL["columns"].index("ordinal")]))]
    first_proof = next(i for i, x in enumerate(payloads) if PT._proof(x) is not None)
    assert priced >= len(rows) - first_proof - 2          # every row from the first proof on, bar the last sell(s)
    assert first_proof == (7 if label == "unmapped" else first_proof)
    assert len(gaps) <= 1                                                   # consecutive pairs chain (99.9% live)
