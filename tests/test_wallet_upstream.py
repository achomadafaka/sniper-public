"""Wallet Map slice 3b: upstream discovery (pre-registration
docs/superpowers/specs/2026-09-30-memecoin-wallet-map-slice3b-upstream-design.md, Gamma CLEAN a6ccd12dd;
amendments A1 and A2 in its build notes). Pure: page fixtures, no network.

The four real transactions in tests/fixtures/memecoin/wallet_upstream_source_buys.json are source buys
of our roster, read 2026-09-30 on the research key for the buyer-rule control (40 of 40 named).
"""
import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.research import wallet_link_graph as g
from scripts.research import wallet_upstream as up

FIX = json.loads((Path(__file__).parent / "fixtures" / "memecoin" / "wallet_upstream_source_buys.json").read_text())
MINT, POOL = "MintAAA", "PoolAAA"
T0 = 1_790_000_000
FEE = 5000
UNIT = {"signature": "src", "t0": T0, "mint": MINT, "source_user_id": "uA", "origin": "detection"}


def _tx(sig, slot, idx, bt, signers, moves, fee=FEE):
    """A `json` transaction. moves: (address, asset, delta); asset "SOL" is the address's native change
    in SOL exactly as given (the fee is NOT added), any other asset is a mint in ui units."""
    keys = list(signers) + [a for a in dict.fromkeys(a for a, _m, _d in moves) if a not in signers]
    pre = {k: 10 ** 10 for k in keys}
    post = dict(pre)
    tpre, tpost = [], []
    for addr, asset, delta in moves:
        if asset == "SOL":
            post[addr] += round(delta * 1e9)
            continue
        dec = 9 if asset == up.WSOL else 6
        bal = {"accountIndex": keys.index(addr), "owner": addr, "mint": asset}
        tpre.append(dict(bal, uiTokenAmount={"amount": str(10 ** 15), "decimals": dec}))
        tpost.append(dict(bal, uiTokenAmount={"amount": str(10 ** 15 + round(delta * 10 ** dec)), "decimals": dec}))
    return {"slot": slot, "transactionIndex": idx, "blockTime": bt,
            "transaction": {"signatures": [sig],
                            "message": {"accountKeys": keys, "header": {"numRequiredSignatures": len(signers)}}},
            "meta": {"err": None, "fee": fee, "preBalances": [pre[k] for k in keys],
                     "postBalances": [post[k] for k in keys], "preTokenBalances": tpre, "postTokenBalances": tpost}}


def _buy(sig, slot, idx, bt, who, sol=0.5, tokens=1000.0):
    return _tx(sig, slot, idx, bt, [who], [(who, "SOL", -sol - FEE / 1e9), (who, MINT, tokens), (POOL, MINT, -tokens)])


def _sell(sig, slot, idx, bt, who, sol=0.5, tokens=1000.0):
    return _tx(sig, slot, idx, bt, [who], [(who, "SOL", sol - FEE / 1e9), (who, MINT, -tokens), (POOL, MINT, tokens)])


def _usdc_buy(sig, slot, idx, bt, who, usdc=60.0, tokens=1000.0, relayer="Relay"):
    return _tx(sig, slot, idx, bt, [relayer, who],
               [(relayer, "SOL", -FEE / 1e9), (who, up.USDC, -usdc), (who, MINT, tokens), (POOL, MINT, -tokens)])


SRC = _buy("src", 100, 7, T0, "SRC", sol=1.0, tokens=1000.0)            # p0 = 0.001 SOL per token


def _after(prices, bt=T0 + 50):
    """Priced buys after the source, in chain order, at the given SOL-per-token prices."""
    return [_buy("e%d" % i, 200 + i, 1, bt, "E%d" % i, sol=p * 1000.0, tokens=1000.0) for i, p in enumerate(prices)]


def _read(txs, *, truncated=False, end=None, launch=False, excluded=(), ours=(), near=False, addrs=("SRC",),
          creator_fn=None):
    return up.upstream_read(txs, UNIT, truncated=truncated, end_txs=end, launch=launch, excluded=set(excluded),
                            source_addresses=set(addrs), ours=set(ours), our_fill_near=near, creator_fn=creator_fn)


# ---------------------------------------------------------------- the buyer rule
def test_a_buyer_is_a_signer_whose_own_balance_rose_and_who_paid_over_the_floor():
    relayed = _tx("relayed", 90, 1, T0 - 20, ["Relay"],                    # the receiver did not sign
                  [("Relay", "SOL", -FEE / 1e9), ("Ghost", MINT, 500.0), (POOL, MINT, -500.0)])
    mover = _tx("mover", 90, 2, T0 - 19, ["Small"],                         # the largest mover did not sign
                [("Small", "SOL", -0.2 - FEE / 1e9), ("Small", MINT, 10.0), ("Whale", MINT, 9000.0),
                 (POOL, MINT, -9010.0)])
    # A claim the wallet signs: it receives the coin, pays the fee, the rent of a new token account
    # (0.00203928 SOL) and a tip (0.001 SOL): 0.00304 SOL beyond the fee, under the 0.01 floor.
    claim = _tx("claim", 90, 3, T0 - 18, ["Claimer"],
                [("Claimer", "SOL", -(FEE / 1e9) - 0.00203928 - 0.001), ("Claimer", MINT, 700.0),
                 ("Distributor", MINT, -700.0)])
    real = _buy("real", 90, 4, T0 - 17, "Early", sol=0.3)
    wsol = _tx("wsol", 90, 5, T0 - 16, ["Wrapped"],
               [("Wrapped", "SOL", -FEE / 1e9), ("Wrapped", up.WSOL, -0.02), ("Wrapped", MINT, 5.0), (POOL, MINT, -5.0)])
    fomo = _usdc_buy("fomo", 90, 6, T0 - 15, "FomoUser", usdc=1.0)
    sale = _sell("sale", 90, 7, T0 - 14, "Seller")             # the pool's balance rises: not a receiver
    spam = _tx("spam", 90, 8, T0 - 13, ["Bot"], [("Bot", "SOL", -FEE / 1e9), ("Bot", up.USDC, -5.0),
                                                ("Bot", "OtherMint", 9.0)])    # names the mint, moves none of it
    swapback = _tx("both", 90, 9, T0 - 12, ["A", "B"], [("A", "SOL", -0.2), ("A", MINT, 5.0), ("B", MINT, -5.0)])
    row, buys = _read([relayed, mover, claim, real, wsol, fomo, sale, spam, swapback, SRC])
    assert [(b["trader"], b["lead_s"], b["slot"], b["tx_index"]) for b in buys] == [
        ("Small", 19, 90, 2), ("Early", 17, 90, 4), ("Wrapped", 16, 90, 5), ("FomoUser", 15, 90, 6), ("A", 12, 90, 9)]
    assert "Whale" not in {b["trader"] for b in buys} and "Relay" not in {b["trader"] for b in buys}
    assert (row["n_before_tx"], row["n_early"], row["n_under_floor"], row["n_unnamed"]) == (9, 5, 1, 1)
    # what the 9 are: a signer bought in 6 (mover, claim, real, wsol, fomo, both), a signer sold in 2
    # (sale, both), none of the mint moved in 1 (spam); the relayed one is none of these
    assert (row["n_before_buy_tx"], row["n_before_sell_tx"], row["n_before_nomove_tx"]) == (6, 2, 1)


def test_a_signers_amount_is_net_of_the_fee_only_when_it_paid_the_fee():
    payer = up.signer_trades(up.view(_buy("a", 1, 1, T0, "W", sol=0.5)), MINT)[0]
    assert (payer["quote"], payer["amount"]) == ("SOL", pytest.approx(0.5, abs=1e-12))
    relayed = _tx("b", 1, 2, T0, ["Relay", "W"], [("Relay", "SOL", -FEE / 1e9), ("W", "SOL", -0.5), ("W", MINT, 9.0),
                                                 (POOL, MINT, -9.0)])
    assert up.signer_trades(up.view(relayed), MINT)[0]["amount"] == pytest.approx(0.5, abs=1e-12)
    sold = up.signer_trades(up.view(_sell("c", 1, 3, T0, "W", sol=0.5)), MINT)[0]
    assert (sold["side"], sold["amount"], sold["paid"]) == ("sell", pytest.approx(0.5, abs=1e-12), False)


def test_usdc_under_one_dollar_does_not_make_a_trade_a_usdc_trade():
    t = _tx("a", 1, 1, T0, ["W"], [("W", "SOL", -0.3 - FEE / 1e9), ("W", up.USDC, -0.5), ("W", MINT, 9.0),
                                   (POOL, MINT, -9.0)])
    tr = up.signer_trades(up.view(t), MINT)[0]
    assert (tr["quote"], tr["amount"]) == ("SOL", pytest.approx(0.3))


@pytest.mark.parametrize("beyond_fee, buyer", [(0.0100001, True), (0.01, False), (0.0099, False)])
def test_the_sol_floor_is_the_fee_plus_a_hundredth_of_a_sol(beyond_fee, buyer):
    t = _tx("b", 90, 1, T0 - 5, ["W"], [("W", "SOL", -(FEE / 1e9) - beyond_fee), ("W", MINT, 1.0), (POOL, MINT, -1.0)])
    _row, buys = _read([t, SRC])
    assert bool(buys) is buyer


@pytest.mark.parametrize("asset, amount, buyer", [(up.USDC, 1.0, True), (up.USDC, 0.99, False),
                                                  (up.WSOL, 0.01, True), (up.WSOL, 0.0099, False)])
def test_the_token_floors_are_one_usdc_and_a_hundredth_of_wsol(asset, amount, buyer):
    t = _tx("b", 90, 1, T0 - 5, ["Relay", "W"], [("Relay", "SOL", -FEE / 1e9), ("W", asset, -amount),
                                                ("W", MINT, 1.0), (POOL, MINT, -1.0)])
    _row, buys = _read([t, SRC])
    assert bool(buys) is buyer


def test_a_transaction_at_or_after_the_source_in_chain_order_is_never_early():
    same_second_before = _buy("b", 100, 3, T0, "Before")
    same_second_after = _buy("a", 100, 9, T0, "After")
    later = _buy("l", 101, 1, T0 + 1, "Later")
    too_old = _buy("o", 10, 1, T0 - 61, "Old")
    edge = _buy("x", 11, 1, T0 - 60, "Edge")
    _row, buys = _read([too_old, edge, same_second_before, SRC, same_second_after, later])
    assert [b["trader"] for b in buys] == ["Edge", "Before"]


@pytest.mark.parametrize('missing',['candidate','source'])
def test_unknown_same_slot_index_is_not_proof_of_an_earlier_buy(missing):
    candidate=_buy('uncertain',100,9 if missing=='candidate' else 3,T0,'Uncertain')
    source=_buy('src',100,7,T0,'SRC',sol=1.0,tokens=1000.0)
    (candidate if missing=='candidate' else source).pop('transactionIndex')
    row,buys=_read([candidate,source])
    assert buys==[] and row['n_order_unknown']==1


def test_previous_slot_with_unknown_index_retains_proven_zero_second_order():
    candidate=_buy('before',99,3,T0,'Before');candidate.pop('transactionIndex')
    buy,=_read([candidate,SRC])[1]
    assert buy['order_proven']==1 and buy['tx_index'] is None


def test_a_launch_window_keeps_every_transaction_before_the_source():
    old = _buy("o", 10, 1, T0 - 300, "Old")
    row, buys = _read([old, SRC], launch=True)
    assert [b["trader"] for b in buys] == ["Old"] and buys[0]["lead_s"] == 300
    assert _read([old, SRC], launch=False)[1] == []


def test_a_buyer_counts_once_at_its_first_buy_and_excluded_addresses_are_never_stored():
    txs = [_buy("b1", 90, 1, T0 - 30, "Twice"), _buy("b2", 91, 1, T0 - 10, "Twice"),
           _buy("own", 92, 1, T0 - 9, "OursAddr"), _buy("roster", 93, 1, T0 - 8, "RosterAddr"),
           _buy("dev", 94, 1, T0 - 7, "Creator"), SRC]
    row, buys = _read(txs, excluded={"OursAddr", "RosterAddr"},
                      creator_fn=lambda tx, mint: "Creator" if tx["transaction"]["signatures"][0] == "src" else None)
    assert [(b["trader"], b["lead_s"]) for b in buys] == [("Twice", 30)]
    assert row["n_early"] == 1 and row["n_before_tx"] == 5 and row["n_unnamed"] == 0


# ---------------------------------------------------------------- the control
@pytest.mark.parametrize("case", FIX["cases"], ids=[c["kind"] for c in FIX["cases"]])
def test_the_rule_names_our_source_in_its_own_real_transaction(case):
    unit = {"signature": case["signature"], "t0": case["t0"], "mint": case["mint"],
            "source_user_id": case["source_user_id"], "origin": "detection"}
    row, buys = up.upstream_read([case["tx"]], unit, truncated=False, end_txs=None, launch=False, excluded=set(),
                                 source_addresses=set(case["source_addresses"]), ours=set())
    v = up.view(case["tx"])
    assert len(v["signers"]) == case["expect"]["signers"]
    mine = [t for t in up.signer_trades(v, case["mint"]) if t["trader"] in case["source_addresses"]]
    assert [(t["side"], t["quote"], t["paid"]) for t in mine] == [("buy", case["expect"]["quote"], True)]
    assert mine[0]["amount"] == pytest.approx(case["expect"]["amount"])
    assert mine[0]["tokens"] == pytest.approx(case["expect"]["tokens"])
    # the co-signing fee payer of a Fomo buy receives nothing: it is never a buyer
    assert len(up.signer_trades(v, case["mint"])) == 1
    assert (row["control_named"], row["control_fail"], buys) == (1, None, [])
    rate = case["expect"]["rate"]
    got, legs = up.window_rate([v])
    assert (got, legs) == (pytest.approx(rate), legs) and legs in (1, 2) if rate else (got, legs) == (None, 0)
    assert (row["quote"], row["fx_legs"]) == ("SOL" if rate else case["expect"]["quote"], legs)
    paid_sol = case["expect"]["amount"] if case["expect"]["quote"] == "SOL" else (
        case["expect"]["amount"] / rate if rate else case["expect"]["amount"])
    assert row["p0"] == pytest.approx(paid_sol / case["expect"]["tokens"])


def test_the_control_fails_with_its_reason_and_gives_no_label():
    relayed = _tx("src", 100, 7, T0, ["Relay"], [("Relay", "SOL", -FEE / 1e9), ("SRC", MINT, 9.0), (POOL, MINT, -9.0)])
    credit = _tx("src", 100, 7, T0, ["SRC"], [("SRC", "SOL", -(FEE / 1e9) - 0.003), ("SRC", MINT, 9.0),
                                              (POOL, MINT, -9.0)])
    sold = _sell("src", 100, 7, T0, "SRC")
    other = _buy("src", 100, 7, T0, "SomeoneElse")             # a paid buy by a signer that is not the source
    for txs, reason in ((relayed, "not_signer"), (credit, "under_floor"), (sold, "no_rise"), (other, "not_signer")):
        row, _b = _read([txs] + _after([0.002, 0.002, 0.002]))
        assert (row["control_named"], row["control_fail"]) == (0, reason)
        assert (row["status"], row["label_reason"], row["win"]) == ("no_label", "source_price", None)


def test_the_control_counts_ok_units_with_its_failure_reasons():
    reads = [{"status": "ok", "control_named": 1, "control_fail": None}] * 8 + [
        {"status": "ok", "control_named": 0, "control_fail": "not_signer"},
        {"status": "our_fill", "control_named": 0, "control_fail": "under_floor"},
        {"status": "unread", "control_named": None, "control_fail": None}]
    assert up.control(reads) == (8, 9, {"not_signer": 1})


# ---------------------------------------------------------------- the label
def test_the_label_is_the_median_of_the_last_three_priced_trades_against_the_sources_fill():
    row, _b = _read([SRC] + _after([0.0005, 0.0009, 0.0030, 0.00116, 0.0011]))
    assert row["p0"] == pytest.approx(0.001) and row["p_end"] == pytest.approx(0.00116)
    assert row["ret_60"] == pytest.approx(0.16) and (row["win"], row["status"], row["n_end"]) == (1, "ok", 5)
    row, _b = _read([SRC] + _after([0.0030, 0.00114, 0.0011, 0.0012]))
    assert row["ret_60"] == pytest.approx(0.14) and (row["win"], row["status"]) == (0, "ok")
    assert _read([SRC] + _after([0.001151] * 3))[0]["win"] == 1 and _read([SRC] + _after([0.001149] * 3))[0]["win"] == 0
    # +15.0% is a winner. In binary 0.00115 / 0.001 - 1 is 0.1499999999999999, and no ratio gives exactly
    # 0.15, so the mark carries a 1e-9 tolerance; 1e-8 under the mark is still not a winner.
    at_mark = _read([SRC] + _after([0.00115] * 3))[0]
    assert at_mark["ret_60"] < up.WIN_RET and at_mark["ret_60"] == pytest.approx(0.15) and at_mark["win"] == 1
    assert _read([SRC] + _after([0.00115 - 1e-11] * 3))[0]["win"] == 0


def test_a_sell_prices_the_end_of_the_minute_like_a_buy():
    sells = [_sell("s%d" % i, 200 + i, 1, T0 + 40, "S%d" % i, sol=1.3, tokens=1000.0) for i in range(3)]
    row, _b = _read([SRC] + sells)
    assert row["p_end"] == pytest.approx(0.0013) and row["win"] == 1


def test_only_trades_after_the_source_inside_its_minute_price_the_end():
    txs = [_buy("pre", 90, 1, T0 - 5, "P", sol=9.0), SRC, _buy("same", 100, 9, T0, "Q", sol=9.0),
           _buy("late", 300, 1, T0 + 61, "R", sol=9.0)] + _after([0.001, 0.001], bt=T0 + 60)
    row, _b = _read(txs)
    assert (row["n_end"], row["win"], row["p_end"], row["status"]) == (2, 0, None, "ok")


def test_under_three_priced_trades_in_a_fully_read_minute_is_a_known_loser():
    row, _b = _read([SRC] + _after([0.002, 0.002]))
    assert (row["status"], row["win"], row["ret_60"], row["n_end"]) == ("ok", 0, None, 2)


def test_a_dust_trade_does_not_price_the_end_of_the_minute():
    """Amendment A2. A 0.02 SOL buy that also pays 0.002 SOL of account rent reads 10% too high."""
    dust = [_buy("d%d" % i, 200 + i, 1, T0 + 50, "D%d" % i, sol=0.0999, tokens=10.0) for i in range(3)]
    row, _b = _read([SRC] + dust)
    assert (row["n_end"], row["win"], row["status"]) == (0, 0, "ok")
    big = [_buy("d%d" % i, 200 + i, 1, T0 + 50, "D%d" % i, sol=0.1, tokens=50.0) for i in range(3)]
    assert _read([SRC] + big)[0]["n_end"] == 3


def test_trades_in_another_quote_are_no_label_unless_the_window_holds_a_rate():
    usdc = [_usdc_buy("u%d" % i, 200 + i, 1, T0 + 50, "U%d" % i, usdc=240.0, tokens=1000.0) for i in range(3)]
    row, _b = _read([SRC] + usdc)
    assert (row["status"], row["label_reason"], row["win"], row["n_end_other"], row["quote"]) == (
        "no_label", "other_quote", None, 3, "SOL")
    # Amendment A1: one routed swap in the window shows 120 USDC per SOL on a SOL/USDC pool leg.
    leg = _tx("leg", 150, 1, T0 + 10, ["Router"], [("Router", "SOL", -FEE / 1e9), ("SolUsdcPool", up.WSOL, 2.0),
                                                   ("SolUsdcPool", up.USDC, -240.0)])
    row, _b = _read([SRC, leg] + usdc)
    assert row["fx_rate"] == pytest.approx(120.0) and (row["quote"], row["fx_legs"], row["fx_rejected"]) == ("SOL", 1, None)
    assert row["p_end"] == pytest.approx(0.002) and (row["win"], row["status"], row["n_end_other"]) == (1, "ok", 0)


def test_a_usdc_source_is_priced_in_usdc_when_the_window_holds_no_rate():
    src = _usdc_buy("src", 100, 7, T0, "SRC", usdc=100.0, tokens=1000.0)
    usdc = [_usdc_buy("u%d" % i, 200 + i, 1, T0 + 50, "U%d" % i, usdc=120.0, tokens=1000.0) for i in range(3)]
    row, _b = _read([src] + usdc)
    assert (row["quote"], row["fx_rate"], row["win"]) == ("USDC", None, 1) and row["ret_60"] == pytest.approx(0.2)


def test_the_window_rate_needs_a_non_signer_with_opposite_wsol_and_usdc_moves():
    signer = _tx("a", 1, 1, T0, ["Trader"], [("Trader", up.WSOL, 2.0), ("Trader", up.USDC, -240.0)])
    same_way = _tx("b", 1, 2, T0, ["R"], [("Acct", up.WSOL, 2.0), ("Acct", up.USDC, 240.0)])
    dust = _tx("c", 1, 3, T0, ["R"], [("Acct", up.WSOL, 0.009), ("Acct", up.USDC, -1.0)])
    assert up.window_rate([up.view(t) for t in (signer, same_way, dust)]) == (None, 0)
    legs = [_tx("l%d" % i, 2, i, T0, ["R"], [("Pool", up.WSOL, 1.0), ("Pool", up.USDC, -rate)])
            for i, rate in enumerate((100.0, 120.0, 500.0))]
    assert up.window_rate([up.view(t) for t in legs]) == (pytest.approx(120.0), 3)


def _leg(rate, sig="leg"):
    return _tx(sig, 150, 1, T0 + 10, ["Router"], [("Router", "SOL", -FEE / 1e9), ("SolUsdcPool", up.WSOL, 2.0),
                                                 ("SolUsdcPool", up.USDC, -2.0 * rate)])


def _band(rates):
    """One read per rate: a SOL source followed by three USDC buys at 240 USDC per 1000 tokens."""
    windows = {}
    for i, rate in enumerate(rates):
        src = dict(SRC, transaction={"signatures": ["src%d" % i], "message": SRC["transaction"]["message"]})
        usdc = [_usdc_buy("u%d" % j, 200 + j, 1, T0 + 50, "U%d" % j, usdc=240.0) for j in range(3)]
        windows["src%d" % i] = [src] + ([_leg(rate)] if rate else []) + usdc
    (reads, _b, _c, _w), _s = _run(_units(len(rates)) if len(rates) > 1 else [dict(UNIT, signature="src0")],
                                   window=lambda u: (windows[u["signature"]], 1, False))
    return reads


def test_a_window_rate_far_from_the_runs_median_is_no_rate():
    """Gamma's addition to A1: one odd leg must not move every price of a unit."""
    reads = _band([120.0, 121.0, 200.0, 126.0, 126.1])
    assert [r["fx_rate"] for r in reads[:2]] == [pytest.approx(120.0), pytest.approx(121.0)]
    assert [(r["status"], r["quote"], r["win"]) for r in reads[:2]] == [("ok", "SOL", 1)] * 2
    odd = reads[2]                                             # 200 against a median of 126: no rate
    assert (odd["fx_rate"], odd["fx_rejected"], odd["fx_legs"]) == (None, pytest.approx(200.0), 1)
    assert (odd["status"], odd["label_reason"], odd["quote"], odd["win"], odd["n_end_other"]) == (
        "no_label", "other_quote", "SOL", None, 3), "the registered same-quote rule"
    assert reads[3]["fx_rate"] == pytest.approx(126.0) and reads[3]["fx_rejected"] is None     # exactly the median
    assert all("_alt" not in r for r in reads)
    # 5% is the band: 126 x 1.05 = 132.3
    edge = _band([126.0, 126.0, 126.0, 132.2, 132.4])
    assert [r["fx_rate"] is None for r in edge] == [False, False, False, False, True]


def test_a_lone_window_rate_and_a_run_without_rates_are_left_alone():
    assert _band([200.0])[0]["fx_rate"] == pytest.approx(200.0)
    reads = _band([None, None])
    assert [(r["fx_rate"], r["fx_legs"], r["status"], r["label_reason"]) for r in reads] == [
        (None, 0, "no_label", "other_quote")] * 2 and all("_alt" not in r for r in reads)


def test_our_own_fill_inside_the_minute_takes_the_unit_out():
    ours = _sell("ourexit", 210, 1, T0 + 30, "US", sol=5.0)
    row, _b = _read([SRC, ours] + _after([0.002, 0.002, 0.002]), ours={"ourexit"})
    assert (row["status"], row["our_fill"], row["win"], row["n_end"]) == ("our_fill", 1, 1, 3)
    row, _b = _read([SRC] + _after([0.002, 0.002, 0.002]), near=True)      # the ledger says we filled nearby
    assert (row["status"], row["our_fill"]) == ("our_fill", 1)
    assert _read([SRC] + _after([0.002, 0.002, 0.002]))[0]["status"] == "ok"


# ---------------------------------------------------------------- status
def test_a_missing_source_a_cut_window_and_a_clock_mismatch():
    assert _read([_buy("x", 90, 1, T0 - 5, "B")]) == ({"status": "not_found"}, [])
    assert _read([_buy("x", 90, 1, T0 - 5, "B")], truncated=True) == ({"status": "truncated_before"}, [])
    assert _read([_buy("src", 100, 7, T0 + 1, "SRC")]) == ({"status": "clock_mismatch"}, [])


def test_a_window_cut_after_the_source_keeps_its_buyers_and_needs_the_end_page():
    early = _buy("b", 90, 1, T0 - 5, "Early")
    row, buys = _read([early, SRC] + _after([0.002] * 3), truncated=True, end=None)
    assert (row["status"], row["label_reason"], [b["trader"] for b in buys]) == ("no_label", "end_unread", ["Early"])
    row, buys = _read([early, SRC], truncated=True, end=_after([0.002] * 3, bt=T0 + 58))
    assert (row["status"], row["win"], len(buys)) == ("ok", 1, 1)
    row, _b = _read([early, SRC], truncated=True, end=_after([0.002] * 2))   # a cut minute is never "thin"
    assert (row["status"], row["label_reason"], row["win"]) == ("no_label", "end_thin", None)


# ---------------------------------------------------------------- the reads
def _units(n=1, **kw):
    return [dict(UNIT, signature="src%d" % i if n > 1 else "src", **kw) for i in range(n)]


def _run(units, *, budget=100, first=T0 - 600, window=None, end=None, flat=True, stop_at=None):
    seen = {"age": [], "window": [], "end": []}

    def fetch_age(u):
        seen["age"].append(u["signature"])
        if stop_at == "age":
            raise g.ReadStop("HTTP 429")
        return first, 1

    def fetch_window(u, start):
        seen["window"].append((u["signature"], start))
        if stop_at == "window":
            raise g.ReadStop("HTTP 429")
        return window(u) if window else ([dict(SRC, transaction={"signatures": [u["signature"]],
                                                                 "message": SRC["transaction"]["message"]})], 1, False)

    def fetch_end(u):
        seen["end"].append(u["signature"])
        return (end or []), 1
    out = up.read_upstream(units, fetch_age=fetch_age, fetch_window=fetch_window, fetch_end=fetch_end, budget=budget,
                           flat_fn=(lambda: flat) if isinstance(flat, bool) else flat, excluded=set(),
                           addresses_for=lambda uid: {"SRC"}, ours=set(), our_fill_near=lambda u: False,
                           stop=g.ReadStop)
    return out, seen


def test_the_window_starts_sixty_seconds_before_the_source_or_at_the_first_transaction_of_a_launch():
    (reads, _b, calls, why), seen = _run(_units())
    assert seen["window"] == [("src", T0 - 60)] and (reads[0]["launch"], reads[0]["age_s"], calls, why) == (0, 600, 2, "done")
    (reads, _b, _c, _w), seen = _run(_units(), first=T0 - 40)
    assert seen["window"] == [("src", T0 - 40)] and (reads[0]["launch"], reads[0]["age_s"]) == (1, 40)
    (reads, _b, _c, _w), seen = _run(_units(), first=T0 - 60)
    assert seen["window"] == [("src", T0 - 60)] and reads[0]["launch"] == 1
    (reads, _b, _c, _w), seen = _run(_units(), first=T0 - 61)
    assert reads[0]["launch"] == 0


def test_a_stored_age_skips_the_probe_and_an_empty_probe_is_retried_three_runs():
    (reads, _b, calls, _w), seen = _run(_units(age_s=30))
    assert seen["age"] == [] and seen["window"] == [("src", T0 - 30)] and calls == 1 and reads[0]["calls"] == 1
    (reads, _b, calls, _w), seen = _run(_units(), first=None)
    assert (reads[0]["status"], reads[0]["empty_probes"], seen["window"], calls) == ("unread", 1, [], 1)
    (reads, *_), _s = _run(_units(empty_probes=1), first=None)
    assert (reads[0]["status"], reads[0]["empty_probes"]) == ("unread", 2)
    (reads, *_), _s = _run(_units(empty_probes=2), first=None)
    assert (reads[0]["status"], reads[0]["empty_probes"]) == ("probe_empty", 3)
    (reads, *_), _s = _run(_units(), first=T0 + 5)             # a "first" transaction after the buy: an index fault
    assert reads[0]["status"] == "unread"


def test_the_end_page_is_read_only_when_the_window_was_cut_after_the_source():
    cut_after = lambda u: ([SRC], 10, True)                   # noqa: E731
    (reads, _b, calls, _w), seen = _run(_units(), window=cut_after, end=_after([0.002] * 3))
    assert seen["end"] == ["src"] and (reads[0]["status"], reads[0]["win"], calls, reads[0]["calls"]) == ("ok", 1, 12, 12)
    cut_before = lambda u: ([_buy("x", 1, 1, T0 - 50, "B")], 10, True)      # noqa: E731
    (reads, _b, calls, _w), seen = _run(_units(), window=cut_before)
    assert seen["end"] == [] and (reads[0]["status"], calls) == ("truncated_before", 11)
    (reads, *_), seen = _run(_units())
    assert seen["end"] == []


def test_a_read_starts_only_when_its_worst_case_fits_and_while_the_book_is_flat():
    assert up.UP_CALLS_MAX == 12
    (reads, _b, calls, why), seen = _run(_units(), budget=11)
    assert (reads, calls, why, seen["age"]) == ([], 0, "budget", [])
    (reads, _b, calls, why), _s = _run(_units(3), budget=13)
    assert (len(reads), calls, why) == (1, 2, "budget")
    (reads, _b, calls, why), seen = _run(_units(), flat=False)
    assert (reads, why, seen["age"]) == ([], "solana book open", [])
    book = iter([True, False])
    (reads, _b, calls, why), _s = _run(_units(3), flat=lambda: next(book))
    assert (len(reads), why) == (1, "solana book open")


def test_a_stop_records_the_unit_unread_counts_its_calls_and_ends_the_pass():
    (reads, _b, calls, why), _s = _run(_units(2), stop_at="window")
    assert [(r["signature"], r["status"], r["calls"]) for r in reads] == [("src0", "unread", 1)]
    assert calls == 1 and why == "stopped: HTTP 429"
    (reads, _b, calls, why), _s = _run(_units(2), stop_at="age")
    assert [(r["status"], r["calls"]) for r in reads] == [("unread", 0)] and calls == 0


# ---------------------------------------------------------------- units
def test_one_unit_per_coin_at_the_earliest_source_buy():
    units = [{"signature": "b", "source_user_id": "uB", "mint": "M1", "origin": "fill", "t0": 50, "slot": 9, "idx": 1},
             {"signature": "a", "source_user_id": "uA", "mint": "M1", "origin": "detection", "t0": 50, "slot": 8, "idx": 4},
             {"signature": "c", "source_user_id": "uC", "mint": "M1", "origin": "detection", "t0": 70, "slot": 20, "idx": 1},
             {"signature": "d", "source_user_id": "uA", "mint": "M2", "origin": "detection", "t0": 10, "slot": 1, "idx": 1}]
    assert sorted(u["signature"] for u in up.upstream_units(units)) == ["a", "d"]


def test_pending_keeps_unread_units_detection_first_and_newest_first():
    units = [dict(UNIT, signature=s, t0=t, origin=o) for s, t, o in
             (("old", 1, "detection"), ("new", 9, "detection"), ("fill", 99, "fill"), ("done", 5, "detection"),
              ("again", 3, "detection"))]
    done = {"done": {"status": "ok"}, "again": {"status": "unread", "empty_probes": 2}}
    out = up.pending(units, done, {"new": 44})
    assert [(u["signature"], u["empty_probes"], u["age_s"]) for u in out] == [
        ("new", 0, 44), ("again", 2, None), ("old", 0, None), ("fill", 0, None)]


# ---------------------------------------------------------------- the null
DAY = 86400


def _u(sig, src="uA", win=0, launch=0, t0=None, origin="detection", status="ok", **kw):
    return dict({"signature": sig, "source_user_id": src, "launch": launch, "status": status, "origin": origin,
                 "win": win, "t0": T0 if t0 is None else t0, "mint": "m_" + sig, "ret_60": 0.2 if win else -0.1,
                 "control_named": 1, "control_fail": None}, **kw)


def _stratum(src, n, winners, launch=0, **kw):
    return [_u("%s_%d_%d" % (src, launch, i), src, int(i < winners), launch, **kw) for i in range(n)]


def test_the_null_of_a_unit_by_hand():
    reads = _stratum("uA", 11, 3) + _stratum("uB", 10, 5) + _stratum("uC", 1, 0)
    nulls = up.unit_nulls(reads)
    # a uA loser: n = 10 others of uA with w = 3; the 21 other units hold 8 winners
    assert nulls["uA_0_5"] == (pytest.approx((3 + 10 * 8 / 21) / 20), 10)
    # a uA winner leaves itself out of both counts
    assert nulls["uA_0_0"] == (pytest.approx((2 + 10 * 7 / 21) / 20), 10)
    # a source with no other unit: n = 0 gives the launch-flag rate
    assert nulls["uC_0_0"] == (pytest.approx(8 / 21), 0)


def test_no_null_when_the_launch_flag_set_is_under_ten_and_strata_never_mix():
    reads = _stratum("uA", 12, 3) + _stratum("uA", 10, 9, launch=1)
    nulls = up.unit_nulls(reads)
    assert not any(sig.startswith("uA_1_") for sig in nulls), "10 launch units: 9 others, under 10"
    assert nulls["uA_0_11"] == (pytest.approx((3 + 10 * 3 / 11) / 21), 11), "the launch winners are not in it"
    reads += [_u("uA_1_extra", "uA", 1, 1)]
    assert up.unit_nulls(reads)["uA_1_9"] == (pytest.approx((9 + 1 + 10 * 10 / 10) / 20), 10)


def test_fill_fed_our_fill_and_unlabelled_units_never_enter_a_null():
    base = _stratum("uA", 12, 3)
    noise = (_stratum("uF", 30, 30, origin="fill") + _stratum("uO", 30, 30, status="our_fill")
             + [_u("nolabel", "uA", None, status="no_label")])
    assert up.unit_nulls(base + noise) == up.unit_nulls(base)


# ---------------------------------------------------------------- the test
def test_the_poisson_binomial_tail_by_hand():
    assert up.pb_tail(1, [0.5, 0.5]) == pytest.approx(0.75)
    assert up.pb_tail(2, [0.2, 0.5, 0.9]) == pytest.approx(0.55)
    assert up.pb_tail(3, [0.2, 0.5, 0.9]) == pytest.approx(0.09)
    assert up.pb_tail(0, [0.2, 0.5]) == 1.0 and up.pb_tail(3, [0.2, 0.5]) == 0.0
    assert up.pb_tail(3, [0.3] * 8) == pytest.approx(g.binomial_tail(3, 8, 0.3))


def _rows(reads, buys, *, copy_bots=(), hubs=(), funder_read=None, prior=None, at="2026-09-30T12:00:00+00:00"):
    traders = {b["trader"] for bs in buys.values() for b in bs}
    rows, nulls = up.wallet_rows(reads, buys, copy_bots=set(copy_bots), hub_funded=set(hubs),
                                 funder_read=traders if funder_read is None else set(funder_read),
                                 prior=prior or {}, window_from="2026-09-16T00:00:00+00:00", computed_at=at)
    return {r["trader"]: r for r in rows}, nulls


def _early(buys, trader, sigs, lead=20):
    for s in sigs:
        buys.setdefault(s, []).append({"trader": trader, "lead_s": lead})
    return buys


def _two_days(reads, sigs):
    for r in reads:
        if r["signature"] in sigs:
            r["t0"] = T0 + DAY
    return reads


def test_a_wallet_early_on_winners_of_a_source_that_rarely_wins_is_a_candidate():
    reads = _two_days(_stratum("uA", 40, 6) + _stratum("uB", 40, 4), {"uA_0_0", "uA_0_1"})
    mine = ["uA_0_%d" % i for i in range(6)] + ["uA_0_10", "uA_0_11"]
    rows, nulls = _rows(reads, _early({}, "W", mine, lead=25))
    w = rows["W"]
    assert (w["m"], w["k"], w["days"], w["m_small"], w["lead_median_s"]) == (8, 6, 2, 0, 25)
    assert w["expected"] == pytest.approx(sum(nulls[s][0] for s in mine), abs=1e-6)
    assert w["p_value"] == pytest.approx(up.pb_tail(6, [nulls[s][0] for s in mine]))
    assert w["q_value"] == pytest.approx(w["p_value"], abs=1e-6) and w["verdict"] == "candidate"
    assert w["first_candidate_at"] == "2026-09-30T12:00:00+00:00" and w["fwd_verdict"] == "pending"
    coins = json.loads(w["coins"])
    assert len(coins) == 8 and set(coins[0]) == {"mint", "t0", "lead_s", "ret_60", "win", "p_null"}


def test_one_day_of_winners_never_makes_a_candidate():
    reads = _two_days(_stratum("uA", 40, 6) + _stratum("uB", 40, 4), {"uA_0_10"})     # a LOSER on another day
    rows, _n = _rows(reads, _early({}, "W", ["uA_0_%d" % i for i in range(6)] + ["uA_0_10", "uA_0_11"]))
    assert rows["W"]["q_value"] <= 0.05 and rows["W"]["days"] == 1 and rows["W"]["verdict"] == "not_distinguished"


def test_two_winners_never_make_a_candidate_however_small_q_is():
    reads = _two_days(_stratum("uC", 120, 2), {"uC_0_0"})
    rows, _n = _rows(reads, _early({}, "W", ["uC_0_%d" % i for i in range(8)]))
    w = rows["W"]
    assert (w["k"], w["days"]) == (2, 2) and w["q_value"] <= 0.05 and w["verdict"] == "not_distinguished"


def test_a_wallet_in_front_of_a_source_whose_coins_win_as_often_is_not_a_candidate():
    reads = _two_days(_stratum("uH", 40, 30) + _stratum("uB", 200, 4), {"uH_0_0", "uH_0_1"})
    mine = ["uH_0_%d" % i for i in range(6)] + ["uH_0_35", "uH_0_36"]
    rows, nulls = _rows(reads, _early({}, "W", mine))
    assert rows["W"]["k"] == 6 and rows["W"]["p_value"] > 0.3 and rows["W"]["verdict"] == "not_distinguished"
    pooled = up.pb_tail(6, [34 / 240] * 8)
    assert pooled < 0.001, "against all coins the same record would have passed"


def test_three_wins_on_strata_with_no_other_winner_are_not_a_candidate_by_that_alone():
    """Gamma R2: a raw rate over 10 units would be 0 and one win would make the tail exactly 0."""
    reads = []
    for s in ("uD1", "uD2", "uD3"):
        reads += _stratum(s, 11, 1)
    reads += _stratum("uZ", 30, 15)
    reads = _two_days(reads, {"uD1_0_0"})
    mine = ["uD1_0_0", "uD2_0_0", "uD3_0_0", "uD1_0_5", "uD1_0_6", "uD2_0_5", "uD2_0_6", "uD3_0_5"]
    rows, nulls = _rows(reads, _early({}, "W", mine))
    assert nulls["uD1_0_0"] == (pytest.approx((0 + 10 * 17 / 62) / 20), 10), "w = 0 of n = 10, and p_i is not 0"
    w = rows["W"]
    assert (w["m"], w["k"], w["days"]) == (8, 3, 2) and w["p_value"] > 0.05 and w["verdict"] == "not_distinguished"
    raw = [0.0, 0.0, 0.0] + [1 / 10] * 5                      # the unshrunk rates of the same 8 units
    assert up.pb_tail(3, raw) < 0.01, "the raw rate would have called it"


def test_m_small_counts_the_units_compared_mostly_with_other_sources():
    reads = _stratum("uA", 11, 2) + _stratum("uS", 10, 1)
    mine = ["uA_0_%d" % i for i in range(5)] + ["uS_0_0", "uS_0_1", "uS_0_2"]
    rows, nulls = _rows(reads, _early({}, "W", mine))
    assert (nulls["uA_0_0"][1], nulls["uS_0_0"][1]) == (10, 9), "10 others is not small, 9 is"
    assert (rows["W"]["m"], rows["W"]["m_small"]) == (8, 3)


def test_the_family_is_every_wallet_with_eight_units_whatever_its_wins():
    reads = _two_days(_stratum("uA", 40, 6) + _stratum("uB", 40, 4), {"uA_0_0", "uA_0_1"})
    buys = _early({}, "Hot", ["uA_0_%d" % i for i in range(6)] + ["uA_0_10", "uA_0_11"])
    _early(buys, "Cold", ["uB_0_%d" % i for i in range(10, 18)])
    _early(buys, "Seven", ["uB_0_%d" % i for i in range(20, 27)])
    rows, _n = _rows(reads, buys)
    assert set(rows) == {"Hot", "Cold"}, "a wallet under 8 units has no row"
    assert (rows["Cold"]["k"], rows["Cold"]["p_value"], rows["Cold"]["q_value"]) == (0, 1.0, 1.0)
    assert rows["Cold"]["verdict"] == "not_distinguished"
    # Benjamini-Hochberg over a family of 2: the smaller p is doubled
    assert rows["Hot"]["q_value"] == pytest.approx(rows["Hot"]["p_value"] * 2, abs=1e-6)


def test_the_verdict_reads_the_bh_q_not_the_raw_p():
    """Gamma on d39e021b4: the slice's only claim. One wallet at p = 0.038 is a candidate alone; among
    four tested wallets its q is 0.15 and it is not."""
    reads = []
    for s in ("uD1", "uD2", "uD3"):
        reads += _stratum(s, 11, 1)
    reads = _two_days(reads + _stratum("uZ", 30, 6), {"uD1_0_0"})
    mine = ["uD1_0_0", "uD2_0_0", "uD3_0_0", "uD1_0_5", "uD1_0_6", "uD2_0_5", "uD2_0_6", "uD3_0_5"]
    rows, _n = _rows(reads, _early({}, "W", mine))
    assert 0.03 < rows["W"]["p_value"] < 0.05 and rows["W"]["verdict"] == "candidate"
    buys = _early({}, "W", mine)
    for i, cold in enumerate(("Cold1", "Cold2", "Cold3")):
        _early(buys, cold, ["uZ_0_%d" % j for j in range(6 + 8 * i, 14 + 8 * i)])        # 8 losers each
    rows, _n = _rows(reads, buys)
    w = rows["W"]
    assert w["p_value"] <= up.BH_Q < w["q_value"] and w["q_value"] == pytest.approx(w["p_value"] * 4, abs=1e-6)
    assert (w["k"], w["days"], w["verdict"], w["first_candidate_at"]) == (3, 2, "not_distinguished", None)


def test_a_copy_bot_and_a_hub_funded_wallet_are_stored_and_never_tested():
    reads = _two_days(_stratum("uA", 40, 6) + _stratum("uB", 40, 4), {"uA_0_0", "uA_0_1"})
    mine = ["uA_0_%d" % i for i in range(6)] + ["uA_0_10", "uA_0_11"]
    buys = {}
    for t in ("Bot", "Hub", "Unread", "Hot"):
        _early(buys, t, mine)
    rows, _n = _rows(reads, buys, copy_bots={"Bot"}, hubs={"Hub"}, funder_read={"Bot", "Hub", "Hot"})
    assert (rows["Bot"]["excluded"], rows["Bot"]["verdict"], rows["Bot"]["verdict_reason"],
            rows["Bot"]["p_value"]) == ("copy_bot", "untested", "copy_bot", None)
    assert (rows["Hub"]["excluded"], rows["Hub"]["verdict"], rows["Hub"]["p_value"]) == ("hub_funded", "untested", None)
    # An unread funder keeps the verdict unknown, but the wallet IS in the family: its p is computed
    # and counted in BH (spec slice 3b "The test"; Gamma on run 32).
    assert (rows["Unread"]["verdict"], rows["Unread"]["verdict_reason"]) == ("unknown", "funder_unread")
    assert rows["Unread"]["p_value"] == pytest.approx(rows["Hot"]["p_value"])
    assert rows["Hot"]["verdict"] == "candidate" and rows["Hot"]["q_value"] == pytest.approx(rows["Hot"]["p_value"], abs=1e-6)


def test_wallets_with_an_unread_funder_count_in_the_bh_family():
    """Gamma on Wallet Map run 32: the code ran BH over the wallets whose funder was read (2), the spec
    over every non-excluded wallet with m >= 8 (7). Here W alone would be a candidate at q = p; with
    three unread wallets in the family its q is 4p and it is not."""
    reads = []
    for s in ("uD1", "uD2", "uD3"):
        reads += _stratum(s, 11, 1)
    reads = _two_days(reads + _stratum("uZ", 30, 6), {"uD1_0_0"})
    mine = ["uD1_0_0", "uD2_0_0", "uD3_0_0", "uD1_0_5", "uD1_0_6", "uD2_0_5", "uD2_0_6", "uD3_0_5"]
    buys = _early({}, "W", mine)
    for i, cold in enumerate(("Cold1", "Cold2", "Cold3")):
        _early(buys, cold, ["uZ_0_%d" % j for j in range(6 + 8 * i, 14 + 8 * i)])
    rows, _n = _rows(reads, buys, funder_read={"W"})
    w = rows["W"]
    assert w["q_value"] == pytest.approx(w["p_value"] * 4, abs=1e-6) and w["verdict"] == "not_distinguished"
    for cold in ("Cold1", "Cold2", "Cold3"):
        c = rows[cold]
        assert (c["verdict"], c["verdict_reason"], c["p_value"], c["q_value"]) == ("unknown", "funder_unread", 1.0, 1.0)


def test_a_failed_control_gives_no_wallet_a_verdict():
    reads = _two_days(_stratum("uA", 40, 6) + _stratum("uB", 40, 4), {"uA_0_0", "uA_0_1"})
    mine = ["uA_0_%d" % i for i in range(6)] + ["uA_0_10", "uA_0_11"]
    for r in reads[40:49]:                                     # 9 of 80 failed: 88.75% named
        r.update(control_named=0, control_fail="not_signer")
    rows, _n = _rows(reads, _early({}, "W", mine))
    assert (rows["W"]["verdict"], rows["W"]["verdict_reason"], rows["W"]["p_value"],
            rows["W"]["first_candidate_at"]) == ("unknown", "control", None, None)
    reads[48].update(control_named=1, control_fail=None)       # 8 of 80 failed: exactly 90%
    rows, _n = _rows(reads, _early({}, "W", mine))
    assert rows["W"]["verdict"] == "candidate"


# ---------------------------------------------------------------- the forward grade
def _forward(n_after, wins_after, prior):
    """W's forward units are uB's first `n_after`, in time order; `wins_after` is how many of the first
    ones win, or the set of winning positions."""
    reads = _stratum("uA", 40, 6) + _stratum("uB", 40, 4)
    after = ["uB_0_%d" % i for i in range(n_after)]
    winners = set(range(wins_after)) if isinstance(wins_after, int) else set(wins_after)
    for r in reads:
        if r["signature"] in after:
            r["t0"] = T0 + 2 * DAY + int(r["signature"].rsplit("_", 1)[1])
            r["win"] = int(int(r["signature"].rsplit("_", 1)[1]) in winners)
    return _rows(reads, _early({}, "W", after + ["uA_0_20"]), prior=prior, at="2026-10-03T00:00:00+00:00")


def test_the_forward_read_happens_once_at_exactly_eight_units():
    from datetime import datetime, timezone
    stamp = datetime.fromtimestamp(T0 + DAY, tz=timezone.utc).isoformat()
    prior = {"W": dict.fromkeys(up.CARRIED, None) | {"first_candidate_at": stamp}}
    rows, _n = _forward(7, 4, prior)
    w = rows["W"]
    assert (w["first_candidate_at"], w["fwd_m"], w["fwd_verdict"], w["fwd_k"], w["fwd_p"]) == (stamp, 7, "pending", None, None)
    assert w["m"] == 8 and w["verdict"] in ("candidate", "not_distinguished")

    rows, nulls = _forward(8, 4, prior)
    w = rows["W"]
    probs = [nulls["uB_0_%d" % i][0] for i in range(8)]
    assert (w["fwd_m"], w["fwd_k"], w["fwd_read_at"]) == (8, 4, "2026-10-03T00:00:00+00:00")
    assert w["fwd_expected"] == pytest.approx(sum(probs), abs=1e-6) and w["fwd_p"] == pytest.approx(up.pb_tail(4, probs))
    assert w["fwd_verdict"] == "confirmed" and w["first_candidate_at"] == stamp

    # a ninth forward unit changes nothing: the read is carried, never recomputed
    done = {"W": {c: w[c] for c in up.CARRIED}}
    rows, _n = _forward(9, 0, done)
    assert {c: rows["W"][c] for c in up.CARRIED} == done["W"]


def test_a_forward_read_without_enough_wins_is_not_confirmed_and_uses_only_the_first_eight():
    from datetime import datetime, timezone
    stamp = datetime.fromtimestamp(T0 + DAY, tz=timezone.utc).isoformat()
    prior = {"W": dict.fromkeys(up.CARRIED, None) | {"first_candidate_at": stamp}}
    rows, _n = _forward(8, 0, prior)
    assert (rows["W"]["fwd_k"], rows["W"]["fwd_p"], rows["W"]["fwd_verdict"]) == (0, 1.0, "not_confirmed")
    rows, nulls = _forward(12, {0, 9, 10, 11}, prior)          # three of the four wins come after the eighth
    assert (rows["W"]["fwd_m"], rows["W"]["fwd_k"]) == (8, 1)
    assert rows["W"]["fwd_expected"] == pytest.approx(sum(nulls["uB_0_%d" % i][0] for i in range(8)), abs=1e-6)


def test_the_first_candidate_stamp_never_moves():
    reads = _two_days(_stratum("uA", 40, 6) + _stratum("uB", 40, 4), {"uA_0_0", "uA_0_1"})
    mine = ["uA_0_%d" % i for i in range(6)] + ["uA_0_10", "uA_0_11"]
    prior = {"W": dict.fromkeys(up.CARRIED, None) | {"first_candidate_at": "2026-09-20T00:00:00+00:00"}}
    rows, _n = _rows(reads, _early({}, "W", mine), prior=prior)
    assert (rows["W"]["verdict"], rows["W"]["first_candidate_at"]) == ("candidate", "2026-09-20T00:00:00+00:00")


def test_a_wallet_that_was_once_a_candidate_keeps_its_row_below_eight_units():
    prior = {"Gone": dict.fromkeys(up.CARRIED, None) | {"first_candidate_at": "2026-09-20T00:00:00+00:00"}}
    rows, _n = _rows(_stratum("uA", 40, 6), {}, prior=prior)
    assert (rows["Gone"]["m"], rows["Gone"]["verdict"], rows["Gone"]["verdict_reason"],
            rows["Gone"]["first_candidate_at"]) == (0, "untested", "m", "2026-09-20T00:00:00+00:00")


# ---------------------------------------------------------------- the pass
SINCE = "2026-09-16T00:00:00+00:00"


def _slot(sig, uid, mint, detected_at="2026-09-30T00:00:01+00:00", slot=100, index=7):
    return {"signature": sig, "source_user_id": uid, "network_id": "solana", "mint": mint, "detected_at": detected_at,
            "status": "ok", "slot": slot, "index": index, "companions": [], "undecoded": 0, "truncated": False}


def _ledger(sigs=(("src", MINT),)):
    from sniper.persistence import db_memecoin as m
    ledger = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(ledger)
    for sig, mint in sigs:
        ledger.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, address, mint, block_time, "
                       "detected_at, created_at) VALUES (?, 'uA', 'SRC', ?, ?, 't', 't')", (sig, mint, T0))
    return ledger


def _pass(tmp_path, monkeypatch, *, ledger=None, window=None, verdicts=False, budget=100, age=T0 - 600, **kw):
    graph = tmp_path / "graph.db"
    monkeypatch.setattr(g, "solana_owners", lambda ledger: {"SRC": "uA", "RosterAddr": "uB"})
    ledger = ledger or _ledger()
    if not graph.exists():
        g.save_reads(graph, [_slot("src", "uA", MINT)], read_at="t")
    seen = {"age": 0}

    def fetch_age(u):
        seen["age"] += 1
        return age, 1
    out = g.upstream_pass(ledger, graph, SINCE, budget=budget, flat_fn=lambda: True, own_addresses={"OursAddr"},
                          verdicts=verdicts, now_iso="2026-09-30T12:00:00+00:00", fetch_age=fetch_age,
                          fetch_window=lambda u, start: (window or [SRC], 1, False),
                          fetch_end=lambda u: ([], 1), creator_fn=None, **kw)
    return out, sqlite3.connect(graph), seen


def test_the_pass_stores_the_read_and_its_early_buyers_and_reports_the_control(tmp_path, monkeypatch):
    window = [_buy("b", 90, 1, T0 - 30, "Early"), _buy("own", 91, 1, T0 - 20, "OursAddr"),
              _buy("ros", 92, 1, T0 - 10, "RosterAddr"), SRC] + _after([0.002] * 3)
    out, conn, _s = _pass(tmp_path, monkeypatch, window=window)
    assert (out["reads_new"], out["calls"], out["stop"], out["units"], out["control"], out["status"]) == (
        1, 2, "done", 1, "1/1", {"ok": 1})
    assert (out["thin_losers"], out["fx_rejected"]) == (0, 0)
    assert out["wallets"] is None and out["funder_wanted"] == [], "no verdict without the flag"
    row = conn.execute("SELECT status, launch, age_s, n_early, win, control_named, origin, method_version, p_null "
                       "FROM memecoin_wallet_upstream_reads").fetchall()
    assert row == [("ok", 0, 600, 1, 1, 1, "detection", up.UPSTREAM_METHOD, None)]
    assert conn.execute("SELECT trader, lead_s FROM memecoin_wallet_early_buys").fetchall() == [("Early", 30)]
    assert conn.execute("SELECT COUNT(*) FROM memecoin_wallet_upstream").fetchone()[0] == 0


def test_the_pass_counts_the_units_that_lose_by_the_thin_minute_rule_alone(tmp_path, monkeypatch):
    """Gamma on A2: the share of not-winners that traded under 3 priced trades is visible per run."""
    out, conn, _s = _pass(tmp_path, monkeypatch, window=[SRC] + _after([0.002] * 2))
    assert (out["status"], out["thin_losers"]) == ({"ok": 1}, 1)
    assert conn.execute("SELECT win, n_end, fx_legs, fx_rejected, n_before_buy_tx, n_before_sell_tx, "
                        "n_before_nomove_tx FROM memecoin_wallet_upstream_reads").fetchall() == [(0, 2, 0, None, 0, 0, 0)]


def test_the_pass_counts_and_stores_a_rate_outside_the_runs_band(tmp_path, monkeypatch):
    graph = tmp_path / "graph.db"
    rates = {"s0": 120.0, "s1": 121.0, "s2": 200.0, "s3": 126.0, "s4": 126.1}
    g.save_reads(graph, [_slot(sig, "uA", "M" + sig) for sig in rates], read_at="t")
    monkeypatch.setattr(g, "solana_owners", lambda ledger: {"SRC": "uA"})

    def window(u, start):
        mint = u["mint"]
        src = _tx(u["signature"], 100, 7, T0, ["SRC"], [("SRC", "SOL", -1.0 - FEE / 1e9), ("SRC", mint, 1000.0),
                                                        (POOL, mint, -1000.0)])
        after = [_tx("e%d" % i, 200 + i, 1, T0 + 50, ["E%d" % i], [("E%d" % i, "SOL", -2.0 - FEE / 1e9),
                                                                   ("E%d" % i, mint, 1000.0), (POOL, mint, -1000.0)])
                 for i in range(3)]
        return [src, _leg(rates[u["signature"]])] + after, 1, False
    out = g.upstream_pass(_ledger([(sig, "M" + sig) for sig in rates]), graph, SINCE, budget=100,
                          flat_fn=lambda: True, own_addresses=set(), verdicts=False,
                          now_iso="2026-09-30T12:00:00+00:00", fetch_age=lambda u: (T0 - 600, 1),
                          fetch_window=window, fetch_end=lambda u: ([], 1), creator_fn=None)
    assert (out["reads_new"], out["fx_rejected"], out["status"]) == (5, 1, {"ok": 5})
    rows = sqlite3.connect(graph).execute("SELECT signature, fx_rate, fx_rejected, fx_legs FROM "
                                          "memecoin_wallet_upstream_reads WHERE fx_rejected IS NOT NULL").fetchall()
    assert rows == [("s2", None, 200.0, 1)]


def test_a_reads_table_made_before_the_counters_gains_their_columns(tmp_path, monkeypatch):
    """2ae02483d created the table without the three POST HOC counters; a run of the next release on
    that table must not fail on a missing column."""
    graph = tmp_path / "graph.db"
    g.save_reads(graph, [_slot("src", "uA", MINT)], read_at="t")
    conn = sqlite3.connect(graph)
    old = up.UPSTREAM_SCHEMA
    for col, kind in up.ADDED_COLS.items():           # the three counters, then A1b's two columns
        assert ("    %s %s,\n" % (col, kind)) in old
        old = old.replace("    %s %s,\n" % (col, kind), "")
    conn.executescript(old)
    conn.execute("INSERT INTO memecoin_wallet_upstream_reads (signature, origin, status, method_version, read_at) "
                 "VALUES ('src', 'detection', 'unread', ?, 't')", (up.UPSTREAM_METHOD,))
    conn.commit()
    conn.close()
    out, conn, _s = _pass(tmp_path, monkeypatch, window=[_buy("b", 90, 1, T0 - 30, "Early"), SRC])
    assert out["status"] == {"ok": 1}
    assert conn.execute("SELECT n_before_buy_tx, n_before_sell_tx, n_before_nomove_tx "
                        "FROM memecoin_wallet_upstream_reads").fetchall() == [(1, 0, 0)]


def test_an_empty_unit_list_drops_nothing(tmp_path, monkeypatch):
    """Gamma LOW on d39e021b4: a slot-read table that answers nothing for the window must not delete
    reads that cost research calls."""
    graph = tmp_path / "graph.db"
    conn = g.graph_connection(graph)
    up.save_reads(conn, [dict(UNIT, signature="kept", status="ok", win=1, control_named=1)],
                  [{"signature": "kept", "trader": "X", "lead_s": 1, "slot": 1, "tx_index": 1, "tokens": 1.0}],
                  read_at="t")
    conn.close()
    monkeypatch.setattr(g, "solana_owners", lambda ledger: {})
    out = g.upstream_pass(_ledger(()), graph, SINCE, budget=100, flat_fn=lambda: True, own_addresses=set(),
                          verdicts=True, now_iso="2026-09-30T12:00:00+00:00", creator_fn=None)
    assert (out["units"], out["pruned"], out["status"], out["control"], out["wallets"]) == (
        0, "skipped_no_units", {}, "0/0", 0)
    conn = sqlite3.connect(graph)
    assert conn.execute("SELECT signature FROM memecoin_wallet_upstream_reads").fetchall() == [("kept",)]
    assert conn.execute("SELECT trader FROM memecoin_wallet_early_buys").fetchall() == [("X",)]


def test_a_read_unit_is_not_read_again_and_a_unit_that_left_the_window_is_dropped(tmp_path, monkeypatch):
    out, conn, seen = _pass(tmp_path, monkeypatch, window=[_buy("b", 90, 1, T0 - 30, "Early"), SRC])
    conn.close()
    out, conn, seen = _pass(tmp_path, monkeypatch)
    assert (out["reads_new"], out["calls"], seen["age"]) == (0, 0, 0)
    gone = sqlite3.connect(tmp_path / "graph.db")
    up.save_reads(gone, [dict(UNIT, signature="gone", status="ok")], [{"signature": "gone", "trader": "X",
                                                                      "lead_s": 1, "slot": 1, "tx_index": 1,
                                                                      "tokens": 1.0}], read_at="t")
    gone.close()
    out, conn, _s = _pass(tmp_path, monkeypatch)
    assert out["pruned"] == 1
    assert conn.execute("SELECT signature FROM memecoin_wallet_upstream_reads").fetchall() == [("src",)]
    assert conn.execute("SELECT signature FROM memecoin_wallet_early_buys").fetchall() == [("src",)]


def test_slice_threes_stored_age_skips_the_probe_and_reads_the_launch_window(tmp_path, monkeypatch):
    graph = tmp_path / "graph.db"
    g.save_reads(graph, [_slot("src", "uA", MINT)], read_at="t")
    g.save_impact_reads(graph, [{"signature": "src", "source_user_id": "uA", "network_id": "solana", "mint": MINT,
                                 "origin": "detection", "t0": T0, "age_s": 25, "status": "launch", "calls": 1}],
                        read_at="t")
    starts = []
    monkeypatch.setattr(g, "solana_owners", lambda ledger: {"SRC": "uA"})
    out = g.upstream_pass(_ledger(), graph, SINCE, budget=100, flat_fn=lambda: True, own_addresses=set(),
                          verdicts=False, now_iso="2026-09-30T12:00:00+00:00",
                          fetch_age=lambda u: pytest.fail("the age is stored"),
                          fetch_window=lambda u, start: (starts.append(start) or [SRC], 1, False),
                          fetch_end=lambda u: ([], 1), creator_fn=None)
    assert starts == [T0 - 25] and out["calls"] == 1
    assert sqlite3.connect(graph).execute("SELECT launch, age_s FROM memecoin_wallet_upstream_reads").fetchall() == [(1, 25)]


def test_a_fill_of_ours_near_the_source_buy_in_the_ledger_takes_the_unit_out(tmp_path, monkeypatch):
    from sniper.persistence import db_memecoin as m
    from datetime import datetime, timezone
    ledger = _ledger()
    pid = m.open_position(ledger, source_user_id="uA", source_handle="h", mint=MINT, ticker="T", entry_price=1.0,
                          entry_notional=10.0, entry_fill_source="chain", entry_lag_s=1.0, their_size_usd=0.0,
                          opened_at="2026-09-30T00:00:00+00:00", strategy="alpha_b", execution_mode="real",
                          network_id="solana")
    at = datetime.fromtimestamp(T0 + up.LABEL_S + up.OUR_FILL_SLACK_S, tz=timezone.utc).isoformat()
    ledger.execute("INSERT INTO memecoin_fills (position_id, kind, tx_sig, created_at, network_id) "
                   "VALUES (?, 'exit', 'oursig', ?, 'solana')", (pid, at))
    out, conn, _s = _pass(tmp_path, monkeypatch, ledger=ledger, window=[SRC] + _after([0.002] * 3))
    assert out["status"] == {"our_fill": 1} and out["control"] == "0/0"
    ledger.execute("UPDATE memecoin_fills SET created_at=? WHERE tx_sig='oursig'",
                   (datetime.fromtimestamp(T0 + up.LABEL_S + up.OUR_FILL_SLACK_S + 1, tz=timezone.utc).isoformat(),))
    # a paper fill (no signature) inside the minute is not ours on chain
    ledger.execute("INSERT INTO memecoin_fills (position_id, kind, tx_sig, created_at, network_id) "
                   "VALUES (?, 'entry', NULL, ?, 'solana')", (pid, datetime.fromtimestamp(T0 + 5, tz=timezone.utc).isoformat()))
    (tmp_path / "graph.db").unlink()
    out, conn, _s = _pass(tmp_path, monkeypatch, ledger=ledger, window=[SRC] + _after([0.002] * 3))
    assert out["status"] == {"ok": 1}


def test_a_pump_profile_source_is_named_by_the_address_in_its_id(tmp_path, monkeypatch):
    graph = tmp_path / "graph.db"
    g.save_reads(graph, [_slot("src", "pump:SRC", MINT)], read_at="t")
    monkeypatch.setattr(g, "solana_owners", lambda ledger: {})
    out = g.upstream_pass(_ledger(), graph, SINCE, budget=100, flat_fn=lambda: True, own_addresses=set(),
                          verdicts=False, now_iso="2026-09-30T12:00:00+00:00", fetch_age=lambda u: (T0 - 600, 1),
                          fetch_window=lambda u, start: ([_buy("again", 90, 1, T0 - 9, "SRC"), SRC], 1, False),
                          fetch_end=lambda u: ([], 1), creator_fn=None)
    assert out["control"] == "1/1"
    assert sqlite3.connect(graph).execute("SELECT COUNT(*) FROM memecoin_wallet_early_buys").fetchone()[0] == 0


def test_verdicts_are_written_only_with_the_flag_and_unread_funders_are_asked_for(tmp_path, monkeypatch):
    graph = tmp_path / "graph.db"
    mints = ["M%02d" % i for i in range(24)]
    g.save_reads(graph, [_slot("s%02d" % i, "uA", mint) for i, mint in enumerate(mints)], read_at="t")
    ledger = _ledger([("s%02d" % i, mint) for i, mint in enumerate(mints)])
    conn = sqlite3.connect(graph)
    conn.executescript(g.SCHEMA)
    reads = [dict(_u("s%02d" % i, "uA", int(i < 3)), mint=mints[i], t0=T0) for i in range(24)]
    buys = [{"signature": "s%02d" % i, "trader": t, "lead_s": 9, "slot": 1, "tx_index": 1, "tokens": 1.0}
            for i in range(8) for t in ("Fam", "Bot")]
    up.save_reads(conn, reads, buys, read_at="t")
    conn.execute("INSERT INTO memecoin_wallet_edges (network_id, edge_type, src, dst, n_coins, first_seen, last_seen, "
                 "coverage, evidence, method_version, computed_at) VALUES ('solana', 'slot_follower', 'uA', 'Bot', 3, "
                 "'t', 't', '{}', '[]', 'v', 't'), ('solana', 'co_buy_slot', 'uA', 'Fam', 3, 't', 't', '{}', '[]', 'v', 't')")
    conn.commit()
    conn.close()
    out, conn, _s = _pass(tmp_path, monkeypatch, ledger=ledger, budget=0)
    assert out["wallets"] is None and conn.execute("SELECT COUNT(*) FROM memecoin_wallet_upstream").fetchone()[0] == 0
    out, conn, _s = _pass(tmp_path, monkeypatch, ledger=ledger, budget=0, verdicts=True)
    assert out["wallets"] == 2 and out["funder_wanted"] == ["Fam"] and out["stop"] == "skipped (budget 0)"
    rows = dict((r[0], r[1:]) for r in conn.execute(
        "SELECT trader, m, k, verdict, verdict_reason, excluded FROM memecoin_wallet_upstream"))
    assert rows == {"Fam": (8, 3, "unknown", "funder_unread", None), "Bot": (8, 3, "untested", "copy_bot", "copy_bot")}
    assert conn.execute("SELECT COUNT(*) FROM memecoin_wallet_upstream_reads WHERE p_null IS NOT NULL").fetchone()[0] == 24
    # once its funder is read (and is no hub) the wallet is tested
    g.save_funders(tmp_path / "graph.db", [{"wallet": "Fam", "status": "resolved", "funder": "F1"}], read_at="t")
    out, conn, _s = _pass(tmp_path, monkeypatch, ledger=ledger, budget=0, verdicts=True)
    assert out["funder_wanted"] == []
    assert conn.execute("SELECT verdict FROM memecoin_wallet_upstream WHERE trader='Fam'").fetchone()[0] == "not_distinguished"


def test_a_wallet_funded_by_a_hub_is_excluded(tmp_path):
    graph = tmp_path / "graph.db"
    g.save_funders(graph, [{"wallet": "W%d" % i, "status": "resolved", "funder": "Exchange"} for i in range(4)]
                   + [{"wallet": "Solo", "status": "resolved", "funder": "Friend"},
                      {"wallet": "NoHist", "status": "no_history"}, {"wallet": "Later", "status": "unread"}],
                   read_at="t")
    final, hubs = g.funder_state(graph)
    assert final == {"W0", "W1", "W2", "W3", "Solo", "NoHist"} and hubs == {"W0", "W1", "W2", "W3"}
    g.save_funders(graph, [{"wallet": "W3", "status": "resolved", "funder": "Other"}], read_at="t")
    assert g.funder_state(graph)[1] == set(), "3 wallets on one funder is not a hub (more than HUB_FUNDED)"


# ---------------------------------------------------------------- run()
def _run_job(tmp_path, monkeypatch, upstream_fn, *, upstream_budget=150, verdicts=False, impact_calls=0):
    from sniper.persistence import db_memecoin as m
    ledger_path, graph = tmp_path / "ledger.db", tmp_path / "graph.db"
    c = sqlite3.connect(ledger_path)
    m.ensure_memecoin_tables(c)
    c.commit()
    c.close()
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_OWN_ADDRESSES", "OwnSolAddr1")
    seen = {}

    def funders(pending, *, budget, flat_fn, fetch):
        seen["funder_budget"], seen["wanted"] = budget, list(pending)
        return [], 0, "done"
    monkeypatch.setattr(g, "impact_pass", lambda *a, **k: {"reads_new": 0, "calls": impact_calls, "stop": "done",
                                                           "units": 0, "wallets": 0})
    monkeypatch.setattr(g, "upstream_pass", upstream_fn)
    monkeypatch.setattr(g, "read_funders", funders)
    monkeypatch.setattr(g, "solana_owners", lambda ledger: {"RosterAddr": "uA"})
    g.run(argparse.Namespace(ledger=str(ledger_path), graph=str(graph), days=14, helius_budget=0,
                             fill_read_budget=None, funder_budget=50, impact_budget=300,
                             upstream_budget=upstream_budget, upstream_verdicts=verdicts))
    row = sqlite3.connect(graph).execute(
        "SELECT helius_calls, note FROM memecoin_wallet_graph_runs ORDER BY rowid DESC LIMIT 1").fetchone()
    return seen, row


def test_run_gives_the_pass_its_budget_and_flag_and_writes_the_control_into_the_note(tmp_path, monkeypatch, capsys):
    got = {}

    def upstream(ledger, graph_path, since, **kw):
        got.update(kw)
        return {"reads_new": 4, "calls": 40, "stop": "budget", "units": 9, "pruned": 0, "status": {"ok": 3, "no_label": 1},
                "control": "3/3", "control_fails": {}, "thin_losers": 2, "fx_rejected": 1, "wallets": None,
                "funder_wanted": ["FamWallet"]}
    seen, (calls, note) = _run_job(tmp_path, monkeypatch, upstream, upstream_budget=5000, impact_calls=1900)
    assert got["budget"] == g.SLOT_BUDGET_MAX - 1900 and got["verdicts"] is False
    assert got["own_addresses"] == {"OwnSolAddr1"}
    assert calls == 1940 and seen["funder_budget"] == 50
    assert seen["wanted"][:2] == ["RosterAddr", "FamWallet"], "the roster first, then the family"
    assert ("upstream_reads_new=4 upstream_stop=budget upstream_calls=40 upstream_control=3/3 "
            "upstream_control_fails={} upstream_status={\"no_label\":1,\"ok\":3} upstream_thin_losers=2 "
            "upstream_fx_rejected=1 upstream_pruned=0 upstream_wallets=off") in note
    assert '"upstream": {"reads_new": 4' in capsys.readouterr().out
    identity=json.loads((tmp_path/'wallet_identity.json').read_text())
    assert identity['version']==1
    _run_job(tmp_path, monkeypatch, upstream, verdicts=True)
    assert got["verdicts"] is True and got["budget"] == 150


def test_a_failing_upstream_pass_does_not_end_the_hourly_run(tmp_path, monkeypatch, capsys):
    def upstream(ledger, graph_path, since, **kw):
        raise RuntimeError("database is locked")
    seen, (calls, note) = _run_job(tmp_path, monkeypatch, upstream)
    assert seen["funder_budget"] == 50 and calls == 0
    assert "upstream_reads_new=0 upstream_stop=error:RuntimeError upstream_calls=0 upstream_control=0/0" in note


def test_identity_publish_failure_keeps_previous_artifact_and_completed_graph(tmp_path,monkeypatch,capsys):
    def upstream(*args,**kw):
        return dict(reads_new=0,calls=0,stop='done',units=0,pruned=0,status={},control='0/0',
                    control_fails={},thin_losers=0,fx_rejected=0,wallets=None,funder_wanted=[])
    p=tmp_path/'wallet_identity.json';p.write_text('{"previous":true}')
    def fail(*args,**kw):raise OSError('disk unavailable')
    monkeypatch.setattr('sniper.services.wallet_identity.publish_identity',fail)
    _,(_,note)=_run_job(tmp_path,monkeypatch,upstream)
    assert json.loads(p.read_text())=={'previous':True}
    assert 'upstream_stop=done' in note
    assert 'wallet identity refresh failed' in capsys.readouterr().err


def test_the_cli_defaults_are_the_registered_budget_and_no_verdicts(monkeypatch):
    got = {}
    monkeypatch.setattr(g, "run", lambda args: got.update(vars(args)) or 0)
    monkeypatch.setattr("sys.argv", ["wallet_link_graph.py", "run"])
    assert g.main() == 0 and (got["upstream_budget"], got["upstream_verdicts"]) == (up.UPSTREAM_BUDGET, False) == (150, False)
    monkeypatch.setattr("sys.argv", ["wallet_link_graph.py", "run", "--upstream-verdicts", "--upstream-budget", "7"])
    g.main()
    assert (got["upstream_budget"], got["upstream_verdicts"]) == (7, True)


class _Rpc:
    """funded_exit_truth.rpc's shape: records every request, answers from a script."""

    def __init__(self, answers):
        self.answers, self.asked = list(answers), []

    def __call__(self, method, params):
        self.asked.append((method, json.loads(json.dumps(params))))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_the_window_request_is_succeeded_transactions_ascending_from_the_start_to_the_end_of_the_minute():
    full = {"data": [{"n": i} for i in range(100)], "paginationToken": "more"}
    rpc = _Rpc([full, {"data": [{"n": 1}], "paginationToken": None}])
    txs, calls, truncated = g.helius_upstream_window("MintX", T0 - 40, T0, rpc=rpc)
    assert (len(txs), calls, truncated) == (101, 2, False)
    method, (address, opts) = rpc.asked[0]
    assert (method, address) == ("getTransactionsForAddress", "MintX")
    assert opts["filters"] == {"status": "succeeded", "blockTime": {"gte": T0 - 40, "lte": T0 + 60}}
    assert (opts["sortOrder"], opts["limit"], opts["transactionDetails"]) == ("asc", 100, "full")
    assert rpc.asked[1][1][1]["paginationToken"] == "more"
    rpc = _Rpc([full] * 10)
    assert g.helius_upstream_window("MintX", T0 - 60, T0, rpc=rpc)[1:] == (10, True), "10 pages, then cut"


def test_the_end_page_request_is_the_last_hundred_succeeded_transactions_of_the_minute():
    """Gamma on d39e021b4: on a busy coin failed bot buys would fill a 100-row page."""
    rpc = _Rpc([{"data": [{"n": 1}, {"n": 2}], "paginationToken": "ignored"}])
    assert g.helius_end_page("MintX", T0, rpc=rpc) == ([{"n": 1}, {"n": 2}], 1)
    method, (address, opts) = rpc.asked[0]
    assert (method, address, len(rpc.asked)) == ("getTransactionsForAddress", "MintX", 1)
    assert opts["filters"] == {"status": "succeeded", "blockTime": {"gte": T0, "lte": T0 + 60}}
    assert (opts["sortOrder"], opts["limit"], opts["transactionDetails"], opts["encoding"]) == ("desc", 100, "full", "json")
    with pytest.raises(g.ReadStop):
        g.helius_end_page("MintX", T0, rpc=_Rpc([TimeoutError("slow")]))
    with pytest.raises(g.ReadStop):
        g.helius_end_page("MintX", T0, rpc=_Rpc([]), flat_fn=lambda: False)


def test_the_reads_never_touch_the_funded_read_lane():
    src = (Path(g.__file__).read_text() + Path(up.__file__).read_text())
    start = Path(g.__file__).read_text().index("def helius_upstream_window")
    end = Path(g.__file__).read_text().index("# ---------------------------------------------------------------- brain projection")
    body = Path(g.__file__).read_text()[start:end] + Path(up.__file__).read_text()
    for banned in ("api.mainnet-beta", "dexscreener", "geckoterminal", "jupiter", "solana_real_seams",
                   "evm_real_seams", "solana_rpc_pool"):
        assert banned not in body.lower(), banned
    assert "fet.rpc" in body and "pick_research_url" in body and src


# ---------------------------------------------------------------- amendment A1b (Gamma bff373583)
DAY = 86_400


def _band_at(specs, stored=None):
    """One read per (t0, rate): a SOL source at t0, one SOL/USDC leg, three USDC buys at t0 + 50."""
    windows, units = {}, []
    for i, (t0, rate) in enumerate(specs):
        sig = "src%d" % i
        leg = _tx("leg%d" % i, 150, 1, t0 + 10, ["Router"], [("Router", "SOL", -FEE / 1e9),
                                                             ("SolUsdcPool", up.WSOL, 2.0),
                                                             ("SolUsdcPool", up.USDC, -2.0 * rate)])
        windows[sig] = [_buy(sig, 100, 7, t0, "SRC", sol=1.0, tokens=1000.0), leg] + [
            _usdc_buy("u%d_%d" % (i, j), 200 + j, 1, t0 + 50, "U%d" % j, usdc=240.0) for j in range(3)]
        units.append(dict(UNIT, signature=sig, t0=t0))
    reads, _b, _c, _w = up.read_upstream(
        units, fetch_age=lambda u: (T0 - 600, 1), fetch_window=lambda u, start: (windows[u["signature"]], 1, False),
        fetch_end=lambda u: ([], 1), budget=1000, flat_fn=lambda: True, excluded=set(),
        addresses_for=lambda uid: {"SRC"}, ours=set(), our_fill_near=lambda u: False, stop=g.ReadStop,
        stored_windows=stored or {})
    return reads


def test_a1b_keeps_a_real_sol_move_across_days_that_a1_rejects():
    """Alpha's case: SOL ~100 on one day and ~120 three days later, read in one run (a ~20% move)."""
    day0 = [(T0 + 60 * i, rate) for i, rate in enumerate((100.0, 100.5, 101.0, 99.5))]
    day3 = [(T0 + 3 * DAY + 60 * i, rate) for i, rate in enumerate((120.0, 121.0, 119.0, 120.5))]
    reads = _band_at(day0 + day3)
    assert [r["fx_rejected"] for r in reads] == [None] * 8                  # A1b: every rate kept
    assert all(r["fx_rate"] and r["quote"] == "SOL" and r["status"] == "ok" for r in reads)
    # A1 (the run's median, ~110) rejects all eight: kept for the sensitivity line, not applied
    assert [r["fx_a1_rejected"] for r in reads] == [rate for _t, rate in day0 + day3]
    alt = json.loads(reads[0]["fx_alt_label"])                               # the same-quote label, stored
    assert (alt["fx_rate"], alt["status"], alt["label_reason"]) == (None, "no_label", "other_quote")
    assert up.a1_label(reads[0])["status"] == "no_label" and up.a1b_label(reads[0])["status"] == "ok"


def test_a1b_keeps_a_rate_with_fewer_than_three_windows_within_twelve_hours():
    reads = _band_at([(T0, 100.0), (T0 + 60, 100.0), (T0 + 120, 100.0), (T0 + DAY, 150.0)])
    assert [r["fx_rejected"] for r in reads] == [None] * 4
    assert reads[3]["fx_a1_rejected"] == pytest.approx(150.0)               # A1 would have rejected it
    # the three at T0 have only 2 others each within 12 h: kept whatever their spread
    assert [r["fx_rate"] for r in _band_at([(T0, 100.0), (T0 + 60, 200.0), (T0 + 120, 100.0)])] == [
        pytest.approx(100.0), pytest.approx(200.0), pytest.approx(100.0)]


def test_a1b_counts_stored_windows_from_any_run_kept_or_rejected():
    stored = {"old0": (T0 - 3600, 100.0), "old1": (T0 + 3600, 100.0), "old2": (T0 - 11 * 3600, 100.0),
              "far": (T0 - 13 * 3600, 130.0)}                                # outside 12 h: not counted
    reads = _band_at([(T0, 130.0)], stored=stored)
    odd = reads[0]
    assert (odd["fx_rate"], odd["fx_rejected"]) == (None, pytest.approx(130.0))   # 3 stored at 100 within 12 h
    assert odd["fx_a1_rejected"] is None                                     # A1: the run is this one read
    assert json.loads(odd["fx_alt_label"])["fx_rate"] == pytest.approx(130.0)     # the FX label, stored
    assert up.a1_label(odd)["status"] == "ok"
    # a stored window of the SAME unit (a re-read) is not one of its neighbours
    again = _band_at([(T0, 130.0)], stored=dict(stored, src0=(T0, 100.0), old2=(T0 - 13 * 3600, 100.0)))
    assert again[0]["fx_rejected"] is None                                   # only 2 others within 12 h


def _stored_read(sig, rate, rejected=None):
    return dict.fromkeys(up.READ_COLS) | {
        "signature": sig, "mint": "M" + sig, "source_user_id": "uA", "origin": "detection", "t0": T0,
        "status": "ok", "quote": "SOL", "fx_rate": None if rejected else rate, "fx_rejected": rejected,
        "fx_legs": 1, "win": 1, "control_named": 1, "empty_probes": 0}


def test_stored_a1_rows_move_to_a1b_and_only_a_changed_one_is_read_again(tmp_path, monkeypatch):
    graph = tmp_path / "graph.db"
    rates = {"s0": 110.0, "s1": 111.0, "s2": 112.0, "s3": 115.0}
    g.save_reads(graph, [_slot(sig, "uA", "M" + sig) for sig in rates], read_at="t")
    conn = sqlite3.connect(graph)
    stored = [_stored_read(s, r) for s, r in rates.items() if s != "s3"] + [_stored_read("s3", 115.0, 115.0)]
    up.save_reads(conn, stored, [{"signature": "s3", "trader": "OldBuyer", "lead_s": 5, "slot": 1,
                                  "tx_index": 1, "tokens": 1.0}], read_at="2026-09-30T10:00:00+00:00")
    conn.execute("UPDATE memecoin_wallet_upstream_reads SET method_version=?", (up.UPSTREAM_METHOD_A1,))
    conn.execute("INSERT INTO memecoin_wallet_upstream (trader, method_version, m, verdict, computed_at, "
                 "first_candidate_at) VALUES ('W', ?, 8, 'untested', 't', '2026-09-29')", (up.UPSTREAM_METHOD_A1,))
    conn.commit()
    # A1b over the stored windows: s3 (115) is 3.6% off the median of the 3 others (111): kept, so it
    # needs the FX label, which A1 never stored -> read again. s0..s2 are unchanged -> moved in place.
    assert up.relabel_a1b(conn, dry_run=True) == {"rated": 4, "a1_rejected": 1, "a1b_rejected": 0,
                                                  "kept_sparse": 0, "reread": ["s3"]}
    monkeypatch.setattr(g, "solana_owners", lambda ledger: {"SRC": "uA"})
    windows_read = []

    def window(u, start):
        windows_read.append(u["signature"])
        mint = u["mint"]
        src = _tx(u["signature"], 100, 7, T0, ["SRC"], [("SRC", "SOL", -1.0 - FEE / 1e9), ("SRC", mint, 1000.0),
                                                        (POOL, mint, -1000.0)])
        after = [_tx("e%d" % i, 200 + i, 1, T0 + 50, ["E%d" % i], [("E%d" % i, "SOL", -2.0 - FEE / 1e9),
                                                                   ("E%d" % i, mint, 1000.0), (POOL, mint, -1000.0)])
                 for i in range(3)]
        return [src, _leg(rates[u["signature"]])] + after, 1, False
    out = g.upstream_pass(_ledger([(sig, "M" + sig) for sig in rates]), graph, SINCE, budget=100,
                          flat_fn=lambda: True, own_addresses=set(), verdicts=False,
                          now_iso="2026-10-01T16:00:00+00:00", fetch_age=lambda u: (T0 - 600, 1),
                          fetch_window=window, fetch_end=lambda u: ([], 1), creator_fn=None)
    assert windows_read == ["s3"] and out["reads_new"] == 1
    rows = {r[0]: r[1:] for r in conn.execute(
        "SELECT signature, method_version, fx_rate, fx_rejected, fx_a1_rejected, read_at "
        "FROM memecoin_wallet_upstream_reads")}
    assert rows["s0"] == (up.UPSTREAM_METHOD, 110.0, None, None, "2026-09-30T10:00:00+00:00")
    # s3: the FX label now, A1's rejection kept for the sensitivity line, the first read time kept
    assert rows["s3"] == (up.UPSTREAM_METHOD, pytest.approx(115.0), None, pytest.approx(115.0),
                          "2026-09-30T10:00:00+00:00")
    assert conn.execute("SELECT trader FROM memecoin_wallet_early_buys WHERE signature='s3'").fetchall() == []
    assert conn.execute("SELECT method_version, first_candidate_at FROM memecoin_wallet_upstream").fetchall() == [
        (up.UPSTREAM_METHOD, "2026-09-29")]
    assert up.relabel_a1b(conn, dry_run=True)["reread"] == []                # once: nothing left


def test_a1b_counts_a_window_at_exactly_twelve_hours_and_not_one_second_more():
    """Gamma L1: the stamped text says within 12 hours, either side: inclusive."""
    edge = {"a": (T0 - 12 * 3600, 100.0), "b": (T0 + 12 * 3600, 100.0), "c": (T0 - 60, 100.0)}
    assert _band_at([(T0, 130.0)], stored=edge)[0]["fx_rejected"] == pytest.approx(130.0)
    past = dict(edge, a=(T0 - 12 * 3600 - 1, 100.0))
    assert _band_at([(T0, 130.0)], stored=past)[0]["fx_rejected"] is None
