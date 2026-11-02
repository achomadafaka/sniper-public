"""Source-sell mirror (operator order 2026-09-27): a registry owner's own sell exits our copy of it.

Spec: docs/superpowers/specs/2026-09-27-memecoin-pump-id-sell-mirror-design.md, amended by the
operator: fire on the PROCESSED sell frame, with no balance read on the decision path, and an
unknown curve holding counts as a full dump.

The frames are real (tests/fixtures/memecoin/source_sell_frames.json): untaxxable's curve full dump
of 65891's coin, the same owner's 64% curve trim of 65826's coin, and hashbergers' PumpSwap full
dump of 65906's coin.
"""
import contextlib
import importlib.util
import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as rr
from sniper.services import memecoin_slot_signal as sig
from sniper.services import memecoin_source_sell_mirror as ssm
from sniper.settings import memecoin as cfg

FX = json.loads((Path(__file__).parent / "fixtures" / "memecoin" / "source_sell_frames.json")
                .read_text())
OPENED = "2026-09-27T00:00:00+00:00"
AFTER = "2026-09-27T00:00:30+00:00"


def _sells(pid):
    e = FX[pid]
    return sig.decode_owner_sells(e["frame"], e["owner"], sig._owner_bytes(e["owner"]),
                                  e["source_user_id"], e["handle"], recv_monotonic=100.0,
                                  recv_utc="host")


def _owner_balance(pid, when):
    e = FX[pid]
    rows = e["tx"]["meta"]["preTokenBalances" if when == "pre" else "postTokenBalances"]
    return sum(int(b["uiTokenAmount"]["amount"]) for b in rows
               if b["owner"] == e["owner"] and b["mint"] == e["mint"])


# ---- decode: the socket reads the owner's own sells exactly --------------------------------

def test_a_curve_full_dump_decodes_to_the_whole_pre_sell_balance():
    [s] = _sells("65891")
    assert (s.venue, s.mint, s.pool, s.pre_atomic) == ("pump_curve", FX["65891"]["mint"], None, None)
    assert s.sold_atomic == _owner_balance("65891", "pre") and _owner_balance("65891", "post") == 0
    assert s.slot == FX["65891"]["tx"]["slot"] and s.owner == FX["65891"]["owner"]


def test_a_curve_trim_decodes_to_exactly_the_tokens_that_left():
    [s] = _sells("65826")
    assert s.sold_atomic == _owner_balance("65826", "pre") - _owner_balance("65826", "post")
    assert round(s.sold_atomic / _owner_balance("65826", "pre"), 3) == 0.640


def test_a_pumpswap_sell_carries_the_owners_pre_sell_balance():
    [s] = _sells("65906")
    assert (s.venue, s.mint) == ("pump_amm", None) and s.pool.startswith("4TcaXZVY")
    assert s.pre_atomic == _owner_balance("65906", "pre") == s.sold_atomic


def test_another_wallets_sell_and_an_owners_buy_are_not_sells():
    e = FX["65891"]
    other = FX["65906"]["owner"]
    assert sig.decode_owner_sells(e["frame"], other, sig._owner_bytes(other), "u", None,
                                  recv_monotonic=0.0, recv_utc="x") == []
    frames = json.loads((Path(__file__).parent / "fixtures" / "memecoin" /
                         "slot_signal_frames.json").read_text())
    buyer = "2SQarCLrLGA73Hwqfo5fegUbzff88uFMnEq8Y5jP65Yf"     # the curve_buy frame's owner
    seller = "D4pycXUXjs7FEWES39RMwfrs2E5LsbxGm5asCX6Tthqu"    # the curve_sell frame's owner
    assert sig.decode_owner_sells(frames["curve_buy"], buyer, sig._owner_bytes(buyer), "u", None,
                                  recv_monotonic=0.0, recv_utc="x") == []
    [sold] = sig.decode_owner_sells(frames["curve_sell"], seller, sig._owner_bytes(seller), "u",
                                    None, recv_monotonic=0.0, recv_utc="x")
    assert sold.venue == "pump_curve" and sold.sold_atomic > 0
    assert sig.decode_owner_buy(frames["curve_sell"], seller, sig._owner_bytes(seller), "u",
                                recv_monotonic=0.0, recv_utc="x") is None


# ---- the confirmed watcher: the backup row carries the exact share --------------------------

def _watcher():
    spec = importlib.util.spec_from_file_location(
        "onchain_wallet_watch", Path(__file__).parents[1] / "scripts/research/onchain_wallet_watch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_watcher_classifies_each_real_sell_with_its_exact_balances():
    w = _watcher()
    for pid in ("65891", "65826", "65906"):
        e = FX[pid]
        [(mint, sold, pre, post)] = w.classify_sell(e["tx"], e["owner"])
        assert (pre, post) == (_owner_balance(pid, "pre"), _owner_balance(pid, "post"))
        assert sold == pre - post and mint == e["mint"]
        assert w.classify_buy(e["tx"], e["owner"]) is None


ROUTES = json.loads((Path(__file__).parent / "fixtures" / "memecoin" / "source_sell_routes.json")
                    .read_text())


@pytest.mark.parametrize("case", ["curve_sale_closing_ata", "dflow_usdc_sale_fee_payer_not_owner",
                                  "routed_coin_usdc_sol", "dflow_coin_usdc_sale_second_signer"])
def test_real_sells_through_other_routes_still_classify_exactly(case):
    """Review S27-01: the refund and counterparty proofs must not drop a real sale. Real frames: a
    curve sell that closes the account, a DFlow->Manifest USDC sale whose fee payer is DFlow's
    relayer (base and quote vaults have different PDA owners), a coin->USDC->SOL route."""
    e = ROUTES[case]
    meta = e["tx"]["meta"]

    def held(rows, mint):
        return sum(int(b["uiTokenAmount"]["amount"]) for b in rows
                   if b.get("owner") == e["owner"] and b.get("mint") == mint)
    [(mint, sold, pre, post)] = _watcher().classify_sell(e["tx"], e["owner"])
    assert (pre, post) == (held(meta["preTokenBalances"], mint), held(meta["postTokenBalances"], mint))
    assert sold == pre - post > 0



def test_a_sol_to_usdc_swap_is_not_a_coin_sale():
    """J7 reports StonkyScheffler's SOL -> USDC swaps as 'sells' of So111... (item 3, 2026-09-28:
    6 of its 6 'missed' sells). No coin moved, so the watcher must write no sell row and no buy."""
    e = ROUTES["dflow_sol_to_usdc_quote_leg"]
    w = _watcher()
    assert w.classify_sell(e["tx"], e["owner"]) == []
    assert w.classify_buy(e["tx"], e["owner"]) is None

RENT = 2_039_280
SOL = 1_000_000_000


def _tx(lamports, pre_tok, post_tok, fee=5000):
    """lamports: [(key, pre, post)] in account order, index 0 pays the fee; *_tok: [(account,
    owner, mint, atomic)]. Owners are real curve points (wallets) or program addresses."""
    keys = [str(k) for k, _a, _b in lamports]
    at = {k: i for i, k in enumerate(keys)}

    def rows(toks):
        return [{"accountIndex": at[str(a)], "owner": str(o), "mint": mint,
                 "uiTokenAmount": {"amount": str(n), "uiAmount": n}} for a, o, mint, n in toks]
    return {"transaction": {"message": {"accountKeys": keys}},
            "meta": {"err": None, "fee": fee, "preBalances": [a for _k, a, _b in lamports],
                     "postBalances": [b for _k, _a, b in lamports],
                     "preTokenBalances": rows(pre_tok), "postTokenBalances": rows(post_tok)}}


def _keys():
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    wallet, other = Keypair().pubkey(), Keypair().pubkey()
    curve = Pubkey.find_program_address([b"bonding-curve"], Pubkey.from_string(sig.PUMP_PROGRAM))[0]
    return SimpleNamespace(wallet=wallet, other=other, curve=curve,
                           accounts=[Pubkey.new_unique() for _ in range(4)])


@pytest.mark.parametrize("rent", [RENT, 2_074_080])      # classic and a Token-2022 account
def test_a_transfer_that_closes_the_account_is_not_a_sale(rent):
    """S27-01's witness: every token sent to another wallet, the empty account closed, its rent
    refunded. No swap, so no sale."""
    k = _keys()
    mine, theirs = k.accounts[:2]
    tx = _tx([(k.wallet, SOL, SOL - 5000 + rent), (mine, rent, 0), (theirs, RENT, RENT)],
             [(mine, k.wallet, "A", 100), (theirs, k.other, "A", 0)], [(theirs, k.other, "A", 100)])
    assert _watcher().classify_sell(tx, str(k.wallet)) == []
    plain = _tx([(k.wallet, SOL, SOL - 5000), (mine, rent, rent), (theirs, RENT, RENT)],
                [(mine, k.wallet, "A", 100), (theirs, k.other, "A", 0)],
                [(mine, k.wallet, "A", 0), (theirs, k.other, "A", 100)])
    assert _watcher().classify_sell(plain, str(k.wallet)) == []


def test_a_recipient_that_pays_for_its_own_account_does_not_make_a_transfer_a_sale():
    """The recipient pays the fee and its new account's rent, so it looks like a counterparty
    that paid. Only the refund proof stops this one: the source gained its rent back and nothing."""
    k = _keys()
    mine, theirs = k.accounts[:2]
    tx = _tx([(k.other, SOL, SOL - 5000 - RENT), (k.wallet, SOL, SOL + RENT), (mine, RENT, 0),
              (theirs, 0, RENT)],
             [(mine, k.wallet, "A", 100)], [(theirs, k.other, "A", 100)])
    assert _watcher().classify_sell(tx, str(k.wallet)) == []


def test_a_swap_of_one_coin_does_not_make_a_transfer_of_another_a_sale():
    k = _keys()
    my_a, their_a, my_b, curve_b = k.accounts
    tx = _tx([(k.wallet, SOL, SOL - 5000 + SOL // 2), (k.curve, 10 * SOL, 10 * SOL - SOL // 2),
              (my_a, RENT, RENT), (their_a, RENT, RENT), (my_b, RENT, RENT), (curve_b, RENT, RENT)],
             [(my_a, k.wallet, "A", 100), (their_a, k.other, "A", 0), (my_b, k.wallet, "B", 50),
              (curve_b, k.curve, "B", 1000)],
             [(my_a, k.wallet, "A", 0), (their_a, k.other, "A", 100), (my_b, k.wallet, "B", 0),
              (curve_b, k.curve, "B", 1050)])
    assert _watcher().classify_sell(tx, str(k.wallet)) == [("B", 50, 50, 0)]


# ---- the decision -------------------------------------------------------------------------

@pytest.fixture
def world(monkeypatch):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    calls = []
    lock = threading.Lock()

    def exit_fn(conn, pos, reason, now):
        calls.append((pos["id"], reason, lock.locked()))
        return {"status": "closed"}
    monkeypatch.setattr(rr, "_alert_exit_result", lambda *a, **k: None)
    monkeypatch.setattr(rr, "check_wallet_demotion", lambda *a, **k: False)
    clock = SimpleNamespace(t=100.0)
    mirror = ssm.SourceSellMirror(enabled=True, shadow=False, min_fraction=0.30,
                                  clock=lambda: clock.t, real_now_fn=lambda: AFTER)
    return SimpleNamespace(conn=c, calls=calls, lock=lock, clock=clock, mirror=mirror,
                           contexts={"solana": {"exit_fn": exit_fn}})


def _open(w, pid, *, opened_at=OPENED):
    e = FX[pid]
    return m.open_position(w.conn, source_user_id=e["source_user_id"], source_handle=e["handle"],
                           mint=e["mint"], ticker="T", entry_price=1e-5, entry_notional=100.0,
                           entry_fill_source="chain", entry_lag_s=1.0, their_size_usd=100.0,
                           opened_at=opened_at, strategy=cfg.MEMECOIN_REAL_POLICY,
                           execution_mode="real", network_id="solana")


def _run(w, *items):
    for item in items:
        w.mirror.offer(item)
    return w.mirror.run(w.conn, w.contexts, exit_lock=w.lock)


def _audit(w, pid):
    row = w.conn.execute("SELECT allowed, detail, lane FROM memecoin_exit_trigger_audit "
                         "WHERE position_id=? AND reason=?", (pid, ssm.REASON)).fetchone()
    return row["allowed"], json.loads(row["detail"]), row["lane"]


def test_a_curve_dump_with_no_known_holding_fires_at_once_under_the_exit_lock(world):
    pid = _open(world, "65891")
    [rec] = _run(world, *_sells("65891"))
    assert rec["outcome"] == "mirrored" and rec["basis"] == "holding_unknown_fire"
    assert world.calls == [(pid, "mirror_source_sell", True)]
    allowed, detail, lane = _audit(world, pid)
    assert allowed == 1 and lane == "source_sell_mirror" and detail["code"] == "size_mirror"
    assert detail["source_signature"] == FX["65891"]["frame"]["params"]["result"]["value"]["signature"]
    assert detail["source_slot"] == FX["65891"]["tx"]["slot"]


def test_a_pumpswap_dump_needs_no_read_and_fires(world):
    pid = _open(world, "65906")
    world.mirror.pools = {s.pool: {"base_mint": FX["65906"]["mint"]} for s in _sells("65906")}
    [rec] = _run(world, *_sells("65906"))
    assert (rec["outcome"], rec["basis"], rec["share"]) == ("mirrored", "event_pre_balance", 1.0)
    assert world.calls == [(pid, "mirror_source_sell", True)]


def test_trims_add_up_and_the_second_one_crosses_the_threshold(world):
    pid = _open(world, "65826")
    [whole] = _sells("65826")
    owner, mint = whole.owner, whole.mint
    world.mirror.offer_buy(SimpleNamespace(owner=owner, mint=mint, pool=None,
                                           token_amount=whole.sold_atomic * 5))
    trim = [whole.__class__(**{**whole.__dict__, "signature": "T%d" % i, "slot": whole.slot + i})
            for i in (1, 2)]                      # two 20% sales of the decoded holding
    [first] = _run(world, trim[0])
    assert (first["outcome"], first["basis"], first["share"]) == ("held_below", "decoded_buys", 0.2)
    assert world.calls == []
    [second] = _run(world, trim[1])
    assert second["outcome"] == "mirrored" and second["cumulative"] == pytest.approx(0.4, abs=1e-3)
    assert world.calls == [(pid, "mirror_source_sell", True)]


def test_a_sale_larger_than_the_decoded_holding_is_a_full_dump(world):
    _open(world, "65826")
    [s] = _sells("65826")
    world.mirror.offer_buy(SimpleNamespace(owner=s.owner, mint=s.mint, pool=None,
                                           token_amount=s.sold_atomic // 2))
    [rec] = _run(world, s)
    assert (rec["outcome"], rec["basis"]) == ("mirrored", "sold_exceeds_decoded_holding_fire")


def test_a_sell_before_our_entry_is_not_ours_to_mirror(world):
    _open(world, "65891", opened_at="2026-09-27T00:05:00+00:00")
    [rec] = _run(world, *_sells("65891"))
    assert rec["outcome"] == "before_entry" and world.calls == []


def test_a_sell_that_lands_before_our_row_is_matched_when_the_row_appears(world):
    [s] = _sells("65891")
    assert _run(world, s) == [] and world.mirror.snapshot()["pending"] == 1
    world.clock.t += 2.0
    pid = _open(world, "65891")
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert rec["outcome"] == "mirrored" and world.calls == [(pid, "mirror_source_sell", True)]


def test_an_unmatched_sell_expires_as_no_position(world):
    assert _run(world, *_sells("65891")) == []
    world.clock.t += ssm.MATCH_WINDOW_S + 0.1
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert rec["outcome"] == "no_position"
    assert world.mirror.snapshot()["slot_processed"]["no_position"] == 1


def test_shadow_decides_and_records_but_never_exits(world):
    pid = _open(world, "65891")
    world.mirror.shadow = True
    [rec] = _run(world, *_sells("65891"))
    assert rec["outcome"] == "mirrored" and rec["shadow"] and world.calls == []
    allowed, detail, _lane = _audit(world, pid)
    assert allowed == 0 and detail["code"] == "size_mirror_shadow"


def test_a_paused_position_is_left_to_the_operator(world):
    pid = _open(world, "65891")
    world.conn.execute("UPDATE memecoin_positions SET tracking_paused_at=? WHERE id=?", (OPENED, pid))
    world.conn.commit()
    [rec] = _run(world, *_sells("65891"))
    assert rec["outcome"] == "tracking_paused" and world.calls == []


def _watcher_row(w, pid, signature):
    e = FX[pid]
    pre, post = _owner_balance(pid, "pre"), _owner_balance(pid, "post")
    m.record_onchain_sell(w.conn, signature=signature, source_user_id=e["source_user_id"],
                          handle=e["handle"], address=e["owner"], mint=e["mint"],
                          sold_atomic=pre - post, pre_atomic=pre, post_atomic=post,
                          slot=e["tx"]["slot"], block_time=e["tx"]["blockTime"],
                          detected_at=AFTER, created_at=AFTER)


def _before_block(pid):
    """Opened a minute before the source's sell landed (the watcher dates it by block time)."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(FX[pid]["tx"]["blockTime"] - 60, tz=timezone.utc).isoformat()


def test_the_watcher_backs_up_a_sell_the_socket_missed_with_its_exact_share(world):
    pid = _open(world, "65826", opened_at=_before_block("65826"))
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == []  # cursor set
    sig65826 = FX["65826"]["frame"]["params"]["result"]["value"]["signature"]
    _watcher_row(world, "65826", sig65826)
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert (rec["path"], rec["basis"]) == ("watcher_confirmed", "transaction_balances")
    assert rec["share"] == pytest.approx(0.64, abs=1e-3) and rec["outcome"] == "mirrored"
    assert world.calls == [(pid, "mirror_source_sell", True)]


def test_the_same_sell_from_both_paths_is_decided_once(world):
    _open(world, "65891")
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)          # cursor set
    [rec] = _run(world, *_sells("65891"))
    assert rec["path"] == "slot_processed"
    _watcher_row(world, "65891", rec["sig"])
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == []
    assert len(world.calls) == 1
    snap = world.mirror.snapshot()
    assert snap["watcher_confirmed"]["seen"] == 1 and snap["watcher_confirmed"]["mirrored"] == 0


def _one_tx(pid, signature, slot):
    [s] = _sells(pid)
    return s.__class__(**{**s.__dict__, "signature": signature, "slot": slot})


def test_one_transaction_that_sells_two_held_coins_exits_both(world):
    """Review S27-03's witness: untaxxable holds both coins; one transaction, two sell events."""
    a, b = _open(world, "65891"), _open(world, "65826")
    assert FX["65891"]["owner"] == FX["65826"]["owner"]
    recs = _run(world, _one_tx("65891", "TX", 7), _one_tx("65826", "TX", 7))
    assert sorted(r["position_id"] for r in recs if r["outcome"] == "mirrored") == [a, b]
    assert sorted(c[0] for c in world.calls) == [a, b]


def test_the_watcher_keeps_every_coin_one_transaction_sold(world):
    a, b = (_open(world, pid, opened_at=_before_block(pid)) for pid in ("65891", "65826"))
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)          # cursor set
    for pid in ("65891", "65826", "65826"):                     # the second 65826 is a re-write
        _watcher_row(world, pid, "TX")
    v2 = world.conn.execute("SELECT mint FROM onchain_wallet_sells_v2 WHERE signature='TX'")
    assert sorted(r[0] for r in v2) == sorted([FX["65891"]["mint"], FX["65826"]["mint"]])
    old = world.conn.execute("SELECT COUNT(*) FROM onchain_wallet_sells WHERE signature='TX'")
    assert old.fetchone()[0] == 1                               # the old table, for old readers
    recs = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert sorted(r["position_id"] for r in recs) == [a, b]
    assert sorted(c[0] for c in world.calls) == [a, b]


def test_no_network_read_runs_on_the_mirror_consumer(world, monkeypatch):
    """Review S27-04: an audit-only balance read (1.5 s timeout) ran on the sole consumer, between
    one protective exit and the next queued one. It is gone; the exact share of the same sale is in
    the watcher's onchain_wallet_sells_v2 row."""
    import inspect
    import urllib.request
    assert not hasattr(ssm, "owner_balance_read")
    assert "audit_read_fn" not in inspect.signature(ssm.SourceSellMirror).parameters
    opened = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: opened.append(a))
    _open(world, "65891"), _open(world, "65826")
    recs = _run(world, *_sells("65891"), _one_tx("65826", "TX2", 9))
    assert [r["outcome"] for r in recs] == ["mirrored", "mirrored"] and opened == []


def test_two_decisions_in_one_minute_keep_two_audit_rows(world):
    """The audit table keeps one row per position, reason and minute for the price rails. A HOLD
    then a MIRROR of the same position in one minute must both stay."""
    import itertools
    pid = _open(world, "65826")
    ticks = itertools.count()
    world.mirror._real_now = lambda: "2026-09-27T00:00:30.%06d+00:00" % next(ticks)
    [whole] = _sells("65826")
    world.mirror.offer_buy(_buy(whole.sold_atomic * 5))
    [hold] = _run(world, _curve_sale(whole.sold_atomic, "T1", 1))
    [fire] = _run(world, _curve_sale(whole.sold_atomic, "T2", 2))
    assert (hold["outcome"], fire["outcome"]) == ("held_below", "mirrored")
    codes = [json.loads(r[0])["code"] for r in world.conn.execute(
        "SELECT detail FROM memecoin_exit_trigger_audit WHERE position_id=? ORDER BY id", (pid,))]
    assert codes == ["size_hold", "size_mirror"]


def test_a_disabled_mirror_takes_nothing(world):
    _open(world, "65891")
    world.mirror.enabled = False
    assert _run(world, *_sells("65891")) == [] and world.calls == []


def test_a_mirror_close_is_a_mirror_exit_fill():
    assert m.full_exit_fill_kind("mirror_source_sell") == "mirror_exit"


def test_a_mirror_close_gets_one_counterfactual_quote_at_600s_and_nothing_else(world):
    from sniper.services import memecoin_post_tp_followup as ptf
    closed = "2026-09-27T00:01:00+00:00"
    pid = _open(world, "65891")
    m.insert_fill(world.conn, position_id=pid, kind="mirror_exit", price=5e-6, qty=10_000_000.0,
                  notional=50.0, fill_source="chain", fraction=1.0, created_at=closed)
    m.claim_close(world.conn, pid)
    m.finalize_close(world.conn, pid, closed, -50.0, exit_reason="mirror_source_sell")
    # A take-profit schedule that is also due must not be drained by the mirror's pass.
    m.schedule_post_tp_followups(world.conn, position_id=999, network_id="solana", mint="TP",
                                 closed_at=OPENED, token_qty_atomic="1", token_decimals=6,
                                 exit_usdc=1.0, now_iso=OPENED, horizons=(60,))
    quoted = []

    def quote(mint, atomic, slippage):
        quoted.append((mint, atomic))
        return {"outAmount": "20000000"}
    early = ptf.post_tp_pass(world.conn, since_iso=OPENED, now_iso="2026-09-27T00:05:00+00:00",
                             decimals_fn=lambda _m: 6, exit_quote_fn=quote,
                             exit_reasons=("mirror_source_sell",), horizons=(600,))
    assert early["scheduled"] == 1 and early["quoted"] == 0 and quoted == []
    due = ptf.post_tp_pass(world.conn, since_iso=OPENED, now_iso="2026-09-27T00:11:01+00:00",
                           decimals_fn=lambda _m: 6, exit_quote_fn=quote,
                           exit_reasons=("mirror_source_sell",), horizons=(600,))
    assert due["quoted"] == 1 and quoted == [(FX["65891"]["mint"], 10_000_000 * 10 ** 6)]
    [result] = due["results"]
    assert result["followup_id"] == "%d:600" % pid and result["usdc"] == 20.0


def test_a_sale_before_our_entry_still_shrinks_the_decoded_holding(world):
    [whole] = _sells("65826")
    world.mirror.offer_buy(SimpleNamespace(owner=whole.owner, mint=whole.mint, pool=None,
                                           token_amount=whole.sold_atomic * 5))
    early = whole.__class__(**{**whole.__dict__, "signature": "EARLY"})
    later = whole.__class__(**{**whole.__dict__, "signature": "LATER", "slot": whole.slot + 9})
    _open(world, "65826", opened_at="2026-09-27T00:00:10+00:00")
    world.mirror._real_now = lambda: "2026-09-27T00:00:05+00:00"
    [first] = _run(world, early)                 # seen before our entry: not ours, but real
    assert first["outcome"] == "before_entry"
    world.mirror._real_now = lambda: AFTER
    [second] = _run(world, later)
    assert second["share"] == pytest.approx(0.25) and second["outcome"] == "held_below"


# ---- S27-02: a sale is sized in the order events arrive --------------------------------------

def _curve_sale(amount, signature, slot_offset=0):
    [whole] = _sells("65826")
    return whole.__class__(**{**whole.__dict__, "sold_atomic": amount, "signature": signature,
                              "slot": whole.slot + slot_offset})


def _buy(amount, signature=None):
    [whole] = _sells("65826")
    return SimpleNamespace(owner=whole.owner, mint=whole.mint, pool=None, token_amount=amount,
                           signature=signature)


def test_a_buy_that_arrives_after_a_sale_does_not_shrink_its_share(world):
    """Review S27-02's witness: hold 100, sell 40, buy 1000 in one drain is a 40% sale."""
    pid = _open(world, "65826")
    world.mirror.offer_buy(_buy(100))
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    world.mirror.offer(_curve_sale(40, "S"))
    world.mirror.offer_buy(_buy(1000))
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert (rec["share"], rec["basis"], rec["outcome"]) == (0.4, "decoded_buys", "mirrored")
    assert world.calls == [(pid, "mirror_source_sell", True)]


def test_a_sale_waiting_for_our_row_keeps_the_share_it_was_seen_with(world):
    world.mirror.offer_buy(_buy(100))
    assert _run(world, _curve_sale(40, "S")) == []             # our position row is not written yet
    world.mirror.offer_buy(_buy(1000))
    world.clock.t += 2.0
    _open(world, "65826")
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert (rec["share"], rec["outcome"]) == (0.4, "mirrored")


def test_a_buy_frame_delivered_twice_is_counted_once(world):
    _open(world, "65826")
    world.mirror.offer_buy(_buy(100, "B1"))
    world.mirror.offer_buy(_buy(100, "B1"))
    [rec] = _run(world, _curve_sale(40, "S"))
    assert rec["share"] == 0.4


def test_one_sale_seen_by_two_paths_leaves_the_holding_smaller_once(world):
    _open(world, "65826")
    s = _curve_sale(40, "S")
    acct = _watched(world, "65826", 100, s.slot - 3)
    world.mirror.offer_buy(_buy(100))
    world.mirror.offer_account(acct, s.slot, 60, 99.8)          # the account socket saw it first
    world.mirror.offer(s)                                        # then the logs frame
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert world.mirror._held[(s.owner, s.mint)] == 60


def test_a_sale_on_a_pool_mapped_late_still_leaves_the_holding_smaller(world):
    """A PumpSwap sale seen before its pool maps to a coin is sized when the mapping arrives, and
    taken off the holding then, so the next sale reads the smaller holding (Alpha, 2026-09-27)."""
    _open(world, "65826")
    world.mirror.min_fraction = 0.9                              # decide, never exit
    [whole] = _sells("65826")
    world.mirror.offer_buy(_buy(100))
    amm = whole.__class__(**{**whole.__dict__, "venue": "pump_amm", "mint": None, "pool": "POOL",
                             "sold_atomic": 40, "pre_atomic": None, "signature": "AMM"})
    assert _run(world, amm) == []                                # pool unknown: waits
    world.mirror.pools = {"POOL": {"base_mint": whole.mint}}
    [first] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    [second] = _run(world, _curve_sale(30, "LATER", slot_offset=1))
    assert (first["share"], second["share"]) == (0.4, 0.5)


def test_the_slot_lane_hands_the_mirror_every_sell_and_a_tap_of_every_buy(monkeypatch):
    """Wiring: the socket's on_sell is the mirror, every own buy still reaches the lane FIRST and
    is then tapped for the mirror's holdings, and the mirror gets the lane's pool cache."""
    from sniper.services import memecoin_slot_lane as lane

    class Thread:
        def __init__(self, *, target, name, daemon):
            pass

        def start(self):
            pass
    monkeypatch.setattr("threading.Thread", Thread)
    seen = {}

    def signal_start_fn(on_buy, *, owners_fn, status, on_sell=None):
        seen.update(on_buy=on_buy, on_sell=on_sell)
    pools = object()
    mirror = ssm.SourceSellMirror(enabled=True, shadow=False, min_fraction=0.3,
                                  real_now_fn=lambda: AFTER)
    rc = dict(usdc_fn=lambda: None, sol_fn=lambda: None, latest_blockhash_fn=lambda: None,
              slot_lock_key="fixture")
    lane.start_slot_lane(rc, builders={"pump_curve": object(),
                                       "pump_amm": SimpleNamespace(pools=pools)},
                         signal_start_fn=signal_start_fn, sol_usd_fn=lambda: None,
                         log=lambda _: None, roster_path="unused", conn_factory=object,
                         readonly_conn_factory=object, mirror=mirror)
    assert seen["on_sell"] == mirror.offer_many and mirror.pools is pools
    seen["on_buy"](SimpleNamespace(owner="O", mint="M", pool=None, token_amount=7))
    [(kind, buy, _mono)] = [mirror._queue.get_nowait()]
    assert kind == "buy" and buy.token_amount == 7


def _flaky(world, statuses):
    """exit_fn answering `statuses` in order (the last repeats)."""
    answers = list(statuses)

    def exit_fn(conn, pos, reason, now):
        status = answers.pop(0) if len(answers) > 1 else answers[0]
        world.calls.append((pos["id"], reason, world.lock.locked(), status))
        if status == "raise":
            raise RuntimeError("provider down before signing")
        return {"status": status}
    world.contexts["solana"]["exit_fn"] = exit_fn


def test_an_exit_that_failed_with_nothing_sent_is_retried_until_it_closes(world):
    """Alpha's fix-forward: the operator's intent is "get out"; the rails are not the plan."""
    pid = _open(world, "65891")
    _flaky(world, ["stuck", "closed"])
    [first] = _run(world, *_sells("65891"))
    assert first["exit_status"] == "stuck" and first["retry_in_s"] == 0.5
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == []  # not yet due
    world.clock.t += 0.5
    [retry] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert (retry["outcome"], retry["exit_status"], retry["attempt"]) == ("mirror_retry", "closed", 2)
    assert [c[3] for c in world.calls] == ["stuck", "closed"] and all(c[2] for c in world.calls)
    assert world.mirror.snapshot()["exit_retries"] == 1


def test_a_raise_before_signing_is_retried_too(world):
    _open(world, "65891")
    _flaky(world, ["raise", "closed"])
    [first] = _run(world, *_sells("65891"))
    assert first["exit_status"] == "error" and first["retry_in_s"] == 0.5
    world.clock.t += 0.5
    [retry] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert retry["exit_status"] == "closed"


def test_an_ambiguous_send_is_never_retried(world):
    _open(world, "65891")
    _flaky(world, ["unknown"])
    [first] = _run(world, *_sells("65891"))
    assert first["exit_status"] == "unknown" and "retry_in_s" not in first
    world.clock.t += 10.0
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == []
    assert len(world.calls) == 1


def test_three_failed_retries_hand_the_position_to_the_rails(world):
    _open(world, "65891")
    _flaky(world, ["stuck"])
    _run(world, *_sells("65891"))
    delays = []
    for delay in (0.5, 1.5, 3.0):
        world.clock.t += delay
        [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
        delays.append(rec.get("retry_in_s"))
    assert delays == [1.5, 3.0, None] and rec["handed_to_rails"]
    world.clock.t += 60.0
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == []
    assert len(world.calls) == 4 and world.mirror.snapshot()["handed_to_rails"] == 1


def test_a_retry_stops_when_another_exit_already_owns_the_position(world):
    pid = _open(world, "65891")
    _flaky(world, ["stuck", "closed"])
    _run(world, *_sells("65891"))
    m.claim_position_close(world.conn, pid, OPENED, "b_stop")       # a rail took it meanwhile
    world.clock.t += 0.5
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert rec["outcome"] == "retry_dropped" and len(world.calls) == 1


# ---- the account path: a routed seller's sale, exactly, whatever the router ------------------

def test_an_account_notification_reads_the_balance_or_a_closed_account_as_zero():
    live = {"params": {"result": {"context": {"slot": 7}, "value": {
        "lamports": 2039280, "data": {"parsed": {"info": {"tokenAmount": {"amount": "640"}}}}}}}}
    closed = {"params": {"result": {"context": {"slot": 8}, "value": {
        "lamports": 0, "data": ["", "base64"]}}}}
    assert ssm.parse_account_notification(live) == (7, 640)
    assert ssm.parse_account_notification(closed) == (8, 0)
    assert ssm.parse_account_notification({"params": {"result": {}}}) is None


class _Watch:
    def __init__(self, balances):
        self.balances, self.want, self.status, self.reads = dict(balances), None, {}, {}

    def resolve(self, owner, mint):
        got = self.balances.get((owner, mint))
        return None if got is None else ("ATA:" + mint[:6], got[0], got[1])

    def read_account(self, account):
        return self.reads.get(account)

    def set_want(self, accounts):
        self.want = set(accounts)


def _watched(world, pid, amount, slot):
    e = FX[pid]
    world.mirror.account_watch = _Watch({(e["owner"], e["mint"]): (amount, slot)})
    # The resolver thread's answer, delivered at once so these tests stay deterministic.
    world.mirror._submit_resolve = lambda key: world.mirror._resolved.put(
        (key, world.mirror.account_watch.resolve(*key)))
    world.mirror.refresh_accounts(world.conn)
    return "ATA:" + e["mint"][:6]


def test_a_slow_baseline_read_never_holds_the_mirror_thread(world, monkeypatch):
    """Review DR-02: the baseline read ran on the mirror thread before every pass, so a decision
    waited up to its 1.5 s timeout (longer on a DNS stall). The read now runs on its own thread."""
    release, read_on = threading.Event(), []
    monkeypatch.setattr(ssm, "held_owner_mints", lambda c: [(1, "u", "h", "owner", "mint", OPENED)])

    def resolve(owner, mint):
        read_on.append(threading.current_thread().name)
        release.wait(5.0)
        return None

    world.mirror.account_watch = SimpleNamespace(resolve=resolve, set_want=lambda _: None)
    decided = threading.Event()

    class Decided(BaseException):
        pass

    def run(*args, **kwargs):
        decided.set()
        raise Decided

    monkeypatch.setattr(world.mirror, "run", run)

    def serve():
        with contextlib.suppress(Decided):
            world.mirror.serve_forever(conn_factory=lambda: world.conn, contexts={},
                                       exit_lock=world.lock, log=lambda _: None)

    worker = threading.Thread(target=serve, name="mirror-under-test", daemon=True)
    worker.start()
    try:
        assert decided.wait(1.0), "the baseline read held the mirror thread"
        for _ in range(100):
            if read_on:
                break
            time.sleep(0.01)
        assert read_on and read_on[0] != "mirror-under-test"
    finally:
        release.set()
        worker.join(6.0)


def test_a_drop_in_the_source_account_is_a_sale_with_its_exact_share(world):
    pid = _open(world, "65826")
    acct = _watched(world, "65826", 100_000, 1_000)
    assert world.mirror.account_watch.want == {acct}
    world.mirror.offer_account(acct, 1_005, 64_000, 100.0)
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert (rec["path"], rec["basis"], rec["share"]) == ("account_processed", "account_balance", 0.36)
    assert rec["outcome"] == "mirrored" and world.calls == [(pid, "mirror_source_sell", True)]


def test_a_rise_moves_the_baseline_and_a_later_drop_is_measured_from_it(world):
    _open(world, "65826")
    acct = _watched(world, "65826", 100_000, 1_000)
    world.mirror.offer_account(acct, 1_001, 400_000, 100.0)   # the source bought more
    world.mirror.offer_account(acct, 1_002, 300_000, 100.0)   # then sold a quarter of it
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert rec["share"] == 0.25 and rec["outcome"] == "held_below" and world.calls == []


def test_a_closed_source_account_is_a_full_dump(world):
    _open(world, "65826")
    acct = _watched(world, "65826", 100_000, 1_000)
    world.mirror.offer_account(acct, 1_003, 0, 100.0)
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert rec["share"] == 1.0 and rec["outcome"] == "mirrored"


def test_a_stale_notification_is_ignored(world):
    _open(world, "65826")
    acct = _watched(world, "65826", 100_000, 1_000)
    world.mirror.offer_account(acct, 999, 10, 100.0)
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == []


def test_the_account_baseline_sizes_a_curve_sell_from_the_logs_exactly(world):
    """The trade-off the operator accepted (an unknown holding fires) is gone for a held coin."""
    _open(world, "65826")
    [s] = _sells("65826")
    _watched(world, "65826", _owner_balance("65826", "pre"), s.slot - 3)
    [rec] = _run(world, s)
    assert rec["basis"] == "account_baseline" and rec["share"] == pytest.approx(0.64, abs=1e-3)


def test_one_sale_seen_by_the_logs_and_the_account_is_decided_once(world):
    pid = _open(world, "65891")
    [s] = _sells("65891")
    acct = _watched(world, "65891", _owner_balance("65891", "pre"), s.slot - 3)
    world.mirror.offer_account(acct, s.slot, 0, 99.8)          # the account socket was first
    [first] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert first["path"] == "account_processed" and first["outcome"] == "mirrored"
    [dup] = _run(world, s)                                     # the logs frame, 0.2 s later
    assert (dup["outcome"], dup["first_path"]) == ("duplicate", "account_processed")
    assert dup["lag_behind_first_ms"] == pytest.approx(200.0)
    assert world.calls == [(pid, "mirror_source_sell", True)]


def test_two_signed_sales_in_one_slot_both_count(world):
    """Review DR-01: the (owner, coin, slot) key that matches the account path to the logs also
    collapsed two DISTINCT signed sales in one slot, so the second read 'duplicate' and stayed in
    the decoded holding. 100 held, two sales of 20: 20% then 25% of the 80 left, cumulative 40%."""
    _open(world, "65826")
    world.mirror.offer_buy(_buy(100, "buy"))
    rows = _run(world, _curve_sale(20, "sale-A"), _curve_sale(20, "sale-B"))
    assert [r["outcome"] for r in rows] == ["held_below", "mirrored"], rows
    assert rows[-1]["cumulative"] == pytest.approx(0.4)
    assert world.mirror._held[(_buy(1).owner, _buy(1).mint)] == 60


def test_split_pool_sale_counts_the_complete_transaction(world):
    _open(world, "65826")
    mint = FX["65826"]["mint"]
    world.mirror.pools = {"A": {"base_mint": mint}, "B": {"base_mint": mint}}
    world.mirror.offer_buy(_buy(100))
    [base] = _sells("65826")
    legs = [base.__class__(**{**base.__dict__, "signature": "split", "slot": 123,
                              "venue": "pump_amm", "mint": None, "pool": pool,
                              "sold_atomic": 20, "pre_atomic": pre})
            for pool, pre in (("A", 100), ("B", 80))]
    world.mirror.offer_many(legs)
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert (rec["share"], rec["cumulative"], rec["outcome"]) == (0.4, 0.4, "mirrored")
    assert world.mirror._held[(base.owner, mint)] == 60
    world.mirror.offer_many(legs)  # duplicate socket delivery does not count twice
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == []


def test_retry_rechecks_current_mirror_disable(world):
    pid = _open(world, "65826")
    world.mirror._retries = [{"due": 99, "attempt": 1, "position_id": pid, "sig": "s",
                              "path": ssm.PATH_SLOT, "handle": "h", "uid": "u"}]
    world.conn.execute("UPDATE memecoin_positions SET mirror_disabled=1 WHERE id=?", (pid,))
    world.conn.commit()
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert rec["outcome"] == "retry_dropped" and world.calls == []


def test_two_account_drops_notified_in_one_slot_are_two_sales(world):
    _open(world, "65826")
    acct = _watched(world, "65826", 100_000, 1_000)
    world.mirror.offer_account(acct, 1_005, 90_000, 100.0)
    world.mirror.offer_account(acct, 1_005, 60_000, 100.1)
    rows = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert [r["share"] for r in rows] == [0.1, pytest.approx(1 / 3, abs=1e-4)], rows
    assert rows[-1]["outcome"] == "mirrored"


def test_a_closed_position_stops_being_watched(world):
    pid = _open(world, "65826")
    _watched(world, "65826", 100_000, 1_000)
    m.claim_close(world.conn, pid)
    m.finalize_close(world.conn, pid, AFTER, 0.0, exit_reason="real_tp")
    world.mirror.refresh_accounts(world.conn)
    assert world.mirror.account_watch.want == set()


# ---- the subscribe-ack gap: a drop between the baseline read and the subscription -------------
#
# accountSubscribe notifies only changes AFTER it registers, and it registers 0.5-4 s after the
# baseline read. 66021 (2026-09-27): the source transferred 54% of its bag at 13:34:20Z, 1.2 s after
# our fill, no notification ever came, and the mirror fired 45 s later on the source's next buy
# with a share of 0.4853 (the transfer net of that buy). Each ack now re-reads the account once.

def _acked(world, acct, read):
    """The subscribe ack for `acct`, then the resolver thread's re-read of it, delivered at once."""
    world.mirror.account_watch.reads[acct] = read
    world.mirror._submit_reread = lambda account, ack_mono, generation: world.mirror._queue.put(
        ("reread", (account, world.mirror.account_watch.read_account(account), ack_mono, 40.0,
                    generation),
         world.clock.t))
    world.mirror.offer_ack(acct, world.clock.t)
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == []  # the ack alone
    return world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)


def test_a_drop_before_the_subscription_fires_on_the_ack_reread(world):
    pid = _open(world, "65826")
    world.conn.execute("UPDATE memecoin_positions SET opened_recorded_at=? WHERE id=?",
                       (OPENED, pid))             # the fill, 30 s before the mirror's clock
    acct = _watched(world, "65826", 100_000, 1_000)
    [rec] = _acked(world, acct, (1_004, 46_000))
    assert (rec["path"], rec["share"], rec["outcome"]) == ("account_processed", 0.54, "mirrored")
    assert world.calls == [(pid, "mirror_source_sell", True)]
    [note] = world.mirror.notes
    assert "fill_to_ack_ms=30000" in note and "reread slot=1004 amount=46000" in note, note
    assert world.mirror.snapshot()["reread"] == {"acks": 1, "rereads": 1, "failed": 0, "stale": 0}


def test_an_account_closed_before_the_subscription_is_a_full_dump(world):
    _open(world, "65826")
    acct = _watched(world, "65826", 100_000, 1_000)
    [rec] = _acked(world, acct, (1_004, 0))
    assert rec["share"] == 1.0 and rec["outcome"] == "mirrored"


def test_a_reread_older_than_the_baseline_is_dropped(world):
    _open(world, "65826")
    acct = _watched(world, "65826", 100_000, 1_000)
    assert _acked(world, acct, (999, 10)) == [] and world.calls == []
    assert "stale" in world.mirror.notes[0]
    assert world.mirror.snapshot()["reread"]["stale"] == 1


def test_a_reread_equal_to_an_earlier_notification_is_one_decision(world):
    pid = _open(world, "65826")
    acct = _watched(world, "65826", 100_000, 1_000)
    world.mirror.offer_account(acct, 1_005, 64_000, 100.0)          # the notification came first
    [first] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert first["share"] == 0.36 and first["outcome"] == "mirrored"
    assert _acked(world, acct, (1_005, 64_000)) == []
    assert world.calls == [(pid, "mirror_source_sell", True)]


def test_late_same_slot_reread_cannot_restore_a_sold_balance(world):
    _open(world, "65826")
    acct = _watched(world, "65826", 100, 1000)
    world.mirror.offer_account(acct, 1005, 80, 99.0)
    [first] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert first["share"] == 0.2
    world.mirror._from_reread((acct, (1005, 100), 98.0, 20.0, 0), 100.0)
    world.mirror.offer_account(acct, 1006, 75, 100.0)
    [second] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert second["share"] == pytest.approx(5 / 80)
    assert second["cumulative"] == 0.25


def test_an_unchanged_reread_leaves_the_baseline_for_the_next_drop(world):
    _open(world, "65826")
    acct = _watched(world, "65826", 100_000, 1_000)
    assert _acked(world, acct, (1_004, 100_000)) == []
    world.mirror.offer_account(acct, 1_006, 64_000, 100.0)
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert rec["share"] == 0.36


def test_a_failed_reread_decides_nothing_and_is_counted(world):
    _open(world, "65826")
    acct = _watched(world, "65826", 100_000, 1_000)
    assert _acked(world, acct, None) == [] and world.calls == []
    assert world.mirror.snapshot()["reread"]["failed"] == 1
    assert "reread failed" in world.mirror.notes[0]


def test_the_socket_hands_each_subscribe_ack_to_on_ack():
    import asyncio

    class Stop(BaseException):
        pass

    acks = []

    def on_ack(account, recv_mono):
        acks.append(account)
        raise Stop                      # ends _run; its `except Exception` does not catch this

    class WS:
        def __init__(self):
            self.replies = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, raw):
            msg = json.loads(raw)
            if msg.get("method") == "accountSubscribe":
                self.replies.append(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": 77}))

        async def recv(self):
            return self.replies.pop(0)

    watch = ssm.OwnerAccountWatch(ws_url="ws://x", rpc_url="http://x", sink=lambda *a: None,
                                  connect=lambda url: WS(), on_ack=on_ack)
    watch.set_want({"ACCT"})
    with pytest.raises(Stop):
        asyncio.run(watch._run())
    assert acks == ["ACCT"] and watch.status["acks"] == 1


def test_the_reread_reads_that_exact_account_and_a_null_as_closed():
    bodies = []
    answers = iter([
        {"result": {"context": {"slot": 9}, "value": [{"lamports": 2039280, "data": {"parsed": {
            "info": {"tokenAmount": {"amount": "640"}}}}}]}},
        {"result": {"context": {"slot": 10}, "value": [None]}},
        {"error": {"code": 429, "message": "rate limited"}},
    ])

    def post(body):
        bodies.append(json.loads(body))
        return next(answers)

    watch = ssm.OwnerAccountWatch(ws_url="ws://x", rpc_url="http://x", sink=None, post=post)
    assert watch.read_account("ACCT") == (9, 640)
    assert watch.read_account("ACCT") == (10, 0)
    assert watch.read_account("ACCT") is None                 # an error body is a failed read
    assert bodies[0]["params"][0] == ["ACCT"]
    assert bodies[0]["params"][1]["commitment"] == "processed"


# ---- subscribe early: the source account is watched from our reservation, not from our fill ----
#
# Operator order 2026-09-27 ("a, sell at once"): a source drop between our reservation and our
# confirmed fill fires the full mirror exit as soon as our position row exists. Reservation ->
# fill is p50 1.97 s on funded Solana, so the subscription must go live from the reservation.

def _reserve(world, key):
    e = FX[key]
    return m.reserve_pending(world.conn, mint=e["mint"], source_user_id=e["source_user_id"],
                             strategy=cfg.MEMECOIN_REAL_POLICY, notional=100.0,
                             reserved_at=OPENED, network_id="solana", attempt_id="att-" + key)


def test_a_reserved_coin_is_watched_before_the_fill(world):
    _reserve(world, "65826")
    acct = _watched(world, "65826", 100_000, 1_000)
    assert world.mirror.account_watch.want == {acct}


def test_a_drop_between_reservation_and_fill_fires_once_the_position_row_exists(world):
    pend = _reserve(world, "65826")
    acct = _watched(world, "65826", 100_000, 1_000)
    world.mirror.offer_account(acct, 1_005, 40_000, 100.0)     # the source sells 60% first
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == []   # no row yet
    pid = _open(world, "65826")                                 # our fill lands
    assert m.confirm_pending(world.conn, pend)
    world.clock.t += 0.5
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert (rec["path"], rec["share"], rec["outcome"]) == ("account_processed", 0.6, "mirrored")
    assert world.calls == [(pid, "mirror_source_sell", True)]


def test_a_failed_reservation_unsubscribes_and_fires_nothing(world):
    pend = _reserve(world, "65826")
    acct = _watched(world, "65826", 100_000, 1_000)
    world.mirror.offer_account(acct, 1_005, 40_000, 100.0)
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == []
    assert m.release_pending(world.conn, pend, "broadcast_rejected")
    world.clock.t += ssm.ACCOUNT_REFRESH_S
    world.mirror.refresh_accounts(world.conn)
    assert world.mirror.account_watch.want == set()
    world.clock.t += ssm.MATCH_WINDOW_S
    rows = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert [r["outcome"] for r in rows] == ["no_position"] and world.calls == []


def test_a_pending_baseline_miss_is_asked_again_next_scan_and_backs_off_only_once_filled(world):
    """At reservation a node that has not seen the source's buy finds no token account."""
    pend = _reserve(world, "65826")
    world.mirror.account_watch = _Watch({})
    asked = []

    def submit(key):
        asked.append(key)
        world.mirror._resolved.put((key, world.mirror.account_watch.resolve(*key)))
    world.mirror._submit_resolve = submit
    world.mirror.refresh_accounts(world.conn)
    world.clock.t += ssm.ACCOUNT_REFRESH_S
    world.mirror.refresh_accounts(world.conn)
    assert len(asked) == 2                                       # pending: the next scan asks again
    _open(world, "65826")
    assert m.confirm_pending(world.conn, pend)
    world.clock.t += ssm.ACCOUNT_REFRESH_S
    world.mirror.refresh_accounts(world.conn)                    # filled: asks, misses, backs off
    world.clock.t += 1.0
    world.mirror.refresh_accounts(world.conn)
    assert len(asked) == 3


def test_a_resolver_answer_wakes_the_mirror_thread(world):
    world.mirror.account_watch = _Watch({("o", "m"): (5, 1)})
    world.mirror._submit_resolve(("o", "m"))                    # the real resolver thread
    kind, _obj, _mono = world.mirror._queue.get(timeout=2.0)
    assert kind == "wake"
    world.mirror._queue.put((kind, _obj, _mono))
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == []


def test_set_want_wakes_the_socket_instead_of_waiting_for_its_timer():
    import asyncio
    sent, connected = [], threading.Event()

    class WS:
        async def __aenter__(self):
            connected.set()
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, raw):
            sent.append((time.monotonic(), json.loads(raw).get("method")))

        async def recv(self):
            await asyncio.sleep(3600)

    watch = ssm.OwnerAccountWatch(ws_url="ws://x", rpc_url="http://x", sink=lambda *a: None,
                                  connect=lambda url: WS())
    watch.start()
    assert connected.wait(2.0)
    time.sleep(0.1)          # its first refresh ran with nothing wanted; the timer is ~0.9 s away
    asked = time.monotonic()
    watch.set_want({"ACCT"})
    deadline = asked + 2.0
    while not sent and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sent and sent[0][1] == "accountSubscribe"
    assert sent[0][0] - asked < 0.4, sent[0][0] - asked


def test_a_fill_committing_between_the_two_reads_never_drops_the_pair(world, monkeypatch):
    """Entry finalization confirms the reservation and opens the position in ONE commit. If it
    lands between the refresh's two reads, reading positions first would see neither and
    unsubscribe the account mid-fill. Reservations are read first, so the pair is always seen."""
    pend = _reserve(world, "65826")
    e = FX["65826"]
    real_pending, real_open = m.reserved_pending_sources, m.open_positions_by_strategy
    state = {"committed": False}

    def commit_after(fn):
        def wrapped(*args, **kwargs):
            out = fn(*args, **kwargs)
            if not state["committed"]:
                state["committed"] = True
                _open(world, "65826")
                assert m.confirm_pending(world.conn, pend)
            return out
        return wrapped
    monkeypatch.setattr(m, "reserved_pending_sources", commit_after(real_pending))
    monkeypatch.setattr(m, "open_positions_by_strategy", commit_after(real_open))
    rows = ssm.held_owner_mints(world.conn)
    assert [(r[3], r[4]) for r in rows] == [(e["owner"], e["mint"])], rows


def test_the_ack_line_of_a_reserved_coin_measures_from_the_reservation(world):
    _reserve(world, "65826")
    acct = _watched(world, "65826", 100_000, 1_000)
    assert _acked(world, acct, (1_004, 100_000)) == []
    [note] = world.mirror.notes
    assert "pos=None reserved_to_ack_ms=30000" in note, note


@pytest.mark.parametrize('amount', [None, 'bad', '-1', 1.5, True, str(2**64), '9' * 5000])
def test_unknown_atomic_balance_cannot_become_a_sell(amount, capsys):
    e = FX['65891']
    tx = json.loads(json.dumps(e['tx']))
    for row in tx['meta']['postTokenBalances']:
        if row.get('owner') == e['owner'] and row.get('mint') == e['mint']:
            row['uiTokenAmount']['amount'] = amount
    assert _watcher().classify_sell(tx, e['owner']) == []
    assert 'atomic balance unknown' in capsys.readouterr().err
