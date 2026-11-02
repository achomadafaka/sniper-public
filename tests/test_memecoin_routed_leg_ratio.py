"""Routed USDC buys get the source's post-trade price from the ONE venue leg that bought the whole coin
delta (spec 2026-09-28-memecoin-chase-post-trade-reference-design.md, amendment "routed USDC buys").
Fixture: the 16 real receipts of every Solana detection without a post price, 14:50-17:40Z."""
import dataclasses
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from sniper.execution.raydium_launchlab_buy import LaunchLabTrade
from sniper.services import memecoin_onchain_lane as lane
from sniper.services import memecoin_slot_signal as ss
from sniper.services import memecoin_source_valuation as sv
from sniper.settings import memecoin as cfg

FIX = json.loads((Path(__file__).parent / "fixtures/memecoin/slot_routed_leg_ratio_20260928.json").read_text())
ROWS = {r["signature"][:8]: r for r in FIX["rows"]}


def pairer_events(logs):
    """The venue events the routed pairer puts on a RoutedBuy, from the same logs, by its own decoders."""
    frame = {"params": {"result": {"value": {"logs": logs}}}}
    return ss._cpmm_swaps(frame, True) + ss._launchlab_trades(frame, True) + ss.routed_pump_buys(frame, True)


@pytest.mark.parametrize("key", sorted(ROWS))
def test_every_real_routed_receipt_binds_or_refuses_as_pinned(key):
    r = ROWS[key]
    ratio, why = ss.routed_leg_ratio(pairer_events(r["logs"]), r["mint"], r["token_delta_atomic"])
    if r["expect_ratio"] is None:
        assert (ratio, why) == (None, r["expect_unknown"])
    else:
        assert why == r["expect_leg"] and ratio == pytest.approx(r["expect_ratio"], rel=1e-12)


def test_coverage_is_eight_of_sixteen():
    legs = [r["expect_leg"] for r in ROWS.values()]
    assert legs.count("raydium_launchlab") == 6 and legs.count("pump_amm") == 2 and legs.count(None) == 8


@pytest.mark.parametrize("key", [k for k, r in sorted(ROWS.items()) if r["expect_leg"] == "raydium_launchlab"])
def test_launchlab_base_side_ratio_equals_its_quote_side_ratio(key):
    # Independent check of the constant-product claim: post/average from the QUOTE side, in leg units.
    r = ROWS[key]
    ev, = [e for e in pairer_events(r["logs"]) if isinstance(e, LaunchLabTrade)]
    out, net = ev.real_base_after - ev.real_base_before, ev.real_quote_after - ev.real_quote_before
    quote_side = ((ev.virtual_quote + ev.real_quote_after) / (ev.virtual_base - ev.real_base_after)) / (net / out)
    ratio, _ = ss.routed_leg_ratio(pairer_events(r["logs"]), r["mint"], r["token_delta_atomic"])
    assert ratio == pytest.approx(quote_side, rel=1e-9)


def test_the_transfer_fee_slack_admits_up_to_ten_percent_and_no_further():
    r = ROWS["3uyuV2at"]                                    # a LaunchLab Token-2022 coin with a 3% fee
    evs = pairer_events(r["logs"])
    ev, = [e for e in evs if isinstance(e, LaunchLabTrade)]
    out = ev.real_base_after - ev.real_base_before
    assert r["token_delta_atomic"] * 100 // 97 in (out, out - 1)         # the fee is exactly 3%
    ok = lambda delta: ss.routed_leg_ratio(evs, r["mint"], delta)[0] is not None  # noqa: E731
    assert ok(out)                                          # no fee
    assert ok(-(-out * 9 // 10))                            # the smallest delta with out <= delta / 0.9
    assert not ok(out * 9 // 10 - 1)                        # a bigger fee than 10%: no binding
    assert not ok(out + 1)                                  # out < delta: another leg added coins


@pytest.mark.parametrize("key", ["5dqkeENM", "34BKLpq5", "2iJgZnQR", "3K2YFyKL", "3ZAqTxur"])
def test_a_real_split_route_gets_no_ratio_although_one_leg_is_decodable(key):
    r = ROWS[key]
    evs = pairer_events(r["logs"])
    assert evs, "the decodable leg is there"
    assert ss.routed_leg_ratio(evs, r["mint"], r["token_delta_atomic"]) == (None, "no_whole_leg")


def test_the_cpmm_branch_uses_the_output_vault():
    r = ROWS["3ZAqTxur"]                                    # Orca + CPMM split; bind the CPMM leg alone
    evs = pairer_events(r["logs"])
    cp, = [e for e in evs if getattr(e, "output_mint", None) == r["mint"]]
    ratio, why = ss.routed_leg_ratio(evs, r["mint"], cp.output_amount)
    assert why == "raydium_cpmm"
    assert ratio == pytest.approx(cp.output_vault_before / (cp.output_vault_before - cp.output_amount))


def test_a_leg_into_another_coin_never_binds_even_at_the_right_amount():
    r = ROWS["3ZAqTxur"]
    cp, = [e for e in pairer_events(r["logs"]) if getattr(e, "output_mint", None) == r["mint"]]
    other = dataclasses.replace(cp, output_mint="So11111111111111111111111111111111111111112")
    assert ss.routed_leg_ratio((other,), r["mint"], cp.output_amount) == (None, "no_whole_leg")
    curve = ss.RoutedPumpBuy(venue="pump_curve", trade_event_log="", token_program=None, fields=tuple(dict(
        mint="OtherCoin1111111111111111111111111111111111", token_amount=1000, virtual_token_reserves=100_000,
        virtual_sol_reserves=1, sol_amount=1).items()))
    assert ss.routed_leg_ratio((curve,), r["mint"], 1000) == (None, "no_whole_leg")
    assert ss.routed_leg_ratio((dataclasses.replace(curve, fields=tuple({**dict(curve.fields), "mint": r["mint"]}
                                                                          .items())),), r["mint"], 1000)[1] == "pump_curve"


def test_two_qualifying_legs_are_ambiguous():
    r = ROWS["3uyuV2at"]
    evs = pairer_events(r["logs"])
    assert ss.routed_leg_ratio(evs + evs, r["mint"], r["token_delta_atomic"]) == (None, "ambiguous_leg")


def test_a_launchlab_event_off_the_constant_product_curve_gives_no_ratio():
    r = ROWS["3uyuV2at"]
    ev, = [e for e in pairer_events(r["logs"]) if isinstance(e, LaunchLabTrade)]
    bent = dataclasses.replace(ev, virtual_quote=ev.virtual_quote * 2)
    assert ss.routed_leg_ratio((bent,), r["mint"], r["token_delta_atomic"]) == (None, "no_whole_leg")


def test_an_implausible_ratio_is_dropped():
    r = ROWS["3z92UxWr"]
    ev, = [e for e in pairer_events(r["logs"]) if isinstance(e, ss.RoutedPumpBuy)]
    f = dict(ev.fields)
    f["real_token_reserves"] = f["token_amount"] + 1                 # the pool nearly emptied: ratio huge
    thin = dataclasses.replace(ev, fields=tuple(f.items()))
    assert ss.routed_leg_ratio((thin,), r["mint"], r["token_delta_atomic"]) == (None, "ratio_implausible")


def _confirmed_row(tmp_path, r, monkeypatch):
    import asyncio
    import time
    from scripts.research import onchain_wallet_watch as watcher
    from sniper.persistence import db_memecoin as m
    conn = sqlite3.connect(tmp_path / "l.db")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    monkeypatch.setattr(watcher, "_http_rpc", lambda *_: r["tx"])
    monkeypatch.setattr(watcher, "_stage_write", lambda *_: None)
    monkeypatch.setattr(watcher, "send_wake", lambda *_: None)
    asyncio.run(watcher.process_signature(r["signature"], r["owner"],
        dict(uid="u1", handle=r["handle"], status="armed"), conn, sol_usd=(150., time.time())))
    return conn.execute("SELECT * FROM onchain_wallet_detections").fetchone()


def test_the_confirmed_route_stores_the_post_price_and_the_lane_reads_it(tmp_path, monkeypatch):
    r = ROWS["33dgwYSz"]
    row = _confirmed_row(tmp_path, r, monkeypatch)
    v = json.loads(row["source_valuation_json"])
    assert v["basis"] == "route_owner_quote_out" and v["source_initiation"] is not None and v["impact_leg"] == "raydium_launchlab"
    assert v["source_impact_ratio"] == pytest.approx(r["expect_ratio"], rel=1e-12)
    assert v["source_post_price"] == pytest.approx(row["source_price"] * r["expect_ratio"], rel=1e-12)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "post_trade", raising=False)
    ev = lane.OnchainEv(row)
    assert ev.chase_reference_basis == "post_trade"
    assert ev.chase_reference_price == pytest.approx(v["source_post_price"])
    assert ev.chase_reference_note.startswith("routed_leg:raydium_launchlab;")
    back = lane.event_from_retry_payload(lane.retry_payload(ev))
    assert back.chase_reference_note == ev.chase_reference_note     # the entry retry keeps the note
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "avg_fill", raising=False)
    assert lane.OnchainEv(row).chase_reference_note is None


def test_a_routed_split_route_keeps_the_average_and_says_why(tmp_path, monkeypatch):
    r = ROWS["5dqkeENM"]
    row = _confirmed_row(tmp_path, r, monkeypatch)
    v = json.loads(row["source_valuation_json"])
    assert v.get("source_post_price") is None and v["impact_unknown"] == "no_whole_leg"
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "post_trade", raising=False)
    ev = lane.OnchainEv(row)
    assert (ev.chase_reference_price, ev.chase_reference_basis, ev.chase_reference_note) == (None, "avg_fill", None)


def test_the_event_path_reference_carries_no_routed_note(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "post_trade", raising=False)
    row = {"mint": "m", "source_user_id": "u", "handle": "h", "network_id": "solana", "source_size_usd": 1.0,
           "source_price": 1e-4, "signature": "s", "detected_at": "2026-09-28T14:13:24+00:00",
           "source_valuation_json": json.dumps({"basis": "pump_event_net_sol", "source_post_price": 1.2e-4})}
    ev = lane.OnchainEv(row)
    assert ev.chase_reference_basis == "post_trade" and ev.chase_reference_note is None


@pytest.mark.parametrize("key,leg", [("33dgwYSz", "raydium_launchlab"), ("3z92UxWr", "pump_amm")])
def test_the_watcher_route_valuation_stores_the_post_price(key, leg):
    r = ROWS[key]
    facts = sv.value_source_buy(r["tx"], r["owner"], sol_usd=(150.0, 1000.0), now=1000.0)
    assert facts["basis"] == "route_owner_quote_out" and facts["impact_leg"] == leg
    assert facts["source_impact_ratio"] == pytest.approx(r["expect_ratio"], rel=1e-12)
    assert facts["source_post_price"] == pytest.approx(facts["source_price"] * r["expect_ratio"], rel=1e-12)


def test_the_watcher_route_valuation_of_a_split_route_has_no_post_price():
    r = ROWS["5dqkeENM"]                                    # PumpSwap + DLMM
    facts = sv.value_source_buy(r["tx"], r["owner"], sol_usd=(150.0, 1000.0), now=1000.0)
    assert facts["basis"] == "route_owner_quote_out" and facts["source_price"]
    assert facts.get("source_post_price") is None and facts["impact_unknown"] == "no_whole_leg"


@pytest.mark.parametrize("note", [None, "routed_leg:raydium_launchlab; average includes upstream leg and router fees"])
def test_the_executor_records_the_reference_note_in_the_chase_substage(note):
    """Through execute_entry with the REAL EntryAttemptTrace (the first-buy-decimals harness): quote
    0.02 vs a post-trade reference of 0.018 passes the chase, and its substage carries the note."""
    from types import SimpleNamespace
    sys.path.insert(0, str(Path(__file__).parent))      # `tests.` resolves to a stray package here
    from test_memecoin_entry_chase_first_buy_decimals import MINT, _Clock, _Deps, m
    from sniper.execution import solana_swap_executor as ex
    from sniper.services.memecoin_entry_trace import EntryAttemptTrace
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint=MINT, source_user_id="u1", strategy="alpha_b", notional=10.0, reserved_at="t")
    c.execute("UPDATE memecoin_pending_entries SET source_price=? WHERE id=?", (0.01, pid))
    c.commit()
    ev = SimpleNamespace(fomo_event_id="event-note", source_user_id="u1", source_handle="alice",
                         signal_source="onchain_watch", network_id="solana", mint=MINT, ticker="M", side="buy",
                         their_price=0.01, their_market_cap=1_000_000.0, created_at="2026-08-19T00:00:00+00:00",
                         received_at_normalized="2026-08-19T00:00:00.900000+00:00", clock_domain="observed_real_utc")
    trace = EntryAttemptTrace.start(c, ev, "2026-08-19T00:00:01+00:00", _Clock())
    d = _Deps(6)
    ex.execute_entry(c, pending_id=pid, mint=MINT, usd_amount=10.0, strategy="alpha_b", source_user_id="u1",
                     now_iso="2026-08-19T00:00:00Z", quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn,
                     sign_fn=d.sign_fn, send_fn=d.send_fn, confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn,
                     token_fn=d.token_fn, impact_cap_pct=12.0, chase_max_pct=30.0, trace=trace,
                     chase_reference_price=0.018, chase_reference_note=note)
    trace.finish("filled", "filled")
    row = c.execute("SELECT trace_json FROM memecoin_entry_attempts WHERE attempt_id=?", (trace.attempt_id,)).fetchone()
    subs = {e["name"]: e for e in json.loads(row[0])["substage_events"]}
    md = subs["chase_quote_confirmation"]
    assert md["reference_basis"] == "post_trade" and md.get("reference_note") == note


@pytest.mark.parametrize("note", [None, "routed_leg:pump_amm; average includes upstream leg and router fees"])
def test_the_leg_hands_the_executor_the_reference_note(monkeypatch, tmp_path, note):
    from types import SimpleNamespace
    sys.path.insert(0, str(Path(__file__).parent))
    from test_memecoin_entry_drift import _conn
    from sniper.services import memecoin_arming
    from sniper.services.memecoin_real_leg import real_entry
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    trace = SimpleNamespace(attempt_id="attempt-note", mark=lambda *a, **k: True, mark_substage=lambda *a, **k: True)
    ev = SimpleNamespace(fomo_event_id="onchain:s", source_user_id="trader", source_handle="alice",
                         signal_source="onchain_watch", network_id="solana", mint="MINT", ticker="M", side="buy",
                         their_price=0.02, their_market_cap=1e6, created_at="2026-07-15T11:59:59+00:00",
                         chase_reference_price=0.021, chase_reference_note=note)
    seen = {}

    def execute_fn(conn, pid, mint, usd, now_iso, impact_cap_pct=None, trace=None,
                   chase_reference_price=None, chase_reference_note=None):
        seen.update(ref=chase_reference_price, note=chase_reference_note)
        return {"status": "aborted", "reason": "stub"}
    real_entry(_conn(), ev, day="2026-07-15", usdc_balance=300.0, sol_balance=1.0,
               now_iso="2026-07-15T12:00:00+00:00", trade_size=10.0, token_safety_fn=lambda _m: (True, "ok"),
               execute_fn=execute_fn, kill_path=str(tmp_path / "kill"), trace=trace)
    assert seen == {"ref": 0.021, "note": note}


def test_the_funded_binding_passes_the_note_to_the_executor(monkeypatch):
    from solders.keypair import Keypair
    from sniper.execution import solana_real_seams as S
    from sniper.execution import solana_swap_executor
    from sniper.execution.submission_transport import AttemptState
    from sniper.persistence import db_memecoin as m
    from test_memecoin_fast_submit_wiring import StubTransport
    seen = []

    def fake_execute_entry(conn, **kw):
        seen.append((kw.get("chase_reference_price"), kw.get("chase_reference_note")))
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
    ex["execute_fn"](conn, pid, "MINT", 10.0, "t", chase_reference_price=0.021, chase_reference_note="N",
        entry_min_slot=100, entry_max_slot=101, entry_head_fn=lambda: (100, time.monotonic()))
    assert seen == [(0.021, "N")]
    assert transport.calls == 0


def test_the_oversize_retry_keeps_the_note():
    import ast
    import inspect
    from sniper.execution import solana_swap_executor as ex
    tree = ast.parse(inspect.getsource(ex.execute_entry))
    inner = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "execute_entry"]
    assert inner and all("chase_reference_note" in {k.arg for k in call.keywords} for call in inner)


@pytest.mark.parametrize("key", ["33dgwYSz", "3z92UxWr"])
def test_the_watcher_binds_only_when_the_pool_paid_exactly_out(key):
    # Lambda's review 3: a PumpSwap or LaunchLab event names its pool, not its coin; with the receipt
    # in hand, the vault that paid the leg must have lost exactly `out` of the coin.
    import copy
    r = ROWS[key]
    tx = copy.deepcopy(r["tx"])
    ev = [e for e in sv.receipt_venue_events(tx["meta"]["logMessages"])
          if hasattr(e, "virtual_base") or isinstance(e, ss.RoutedPumpBuy)][0]
    out = (ev.real_base_after - ev.real_base_before) if hasattr(ev, "virtual_base") else dict(ev.fields)["token_amount"]
    vault = [b for b in tx["meta"]["postTokenBalances"] if b.get("mint") == r["mint"] and b.get("owner") != r["owner"]
             and any(p["accountIndex"] == b["accountIndex"] and int(p["uiTokenAmount"]["amount"])
                     - int(b["uiTokenAmount"]["amount"]) == out for p in tx["meta"]["preTokenBalances"])]
    assert len(vault) == 1, "the real receipt's vault paid exactly out"
    vault[0]["uiTokenAmount"]["amount"] = str(int(vault[0]["uiTokenAmount"]["amount"]) + 1)   # paid out - 1
    facts = sv.value_source_buy(tx, r["owner"], sol_usd=(150.0, 1000.0), now=1000.0)
    assert facts["basis"] == "route_owner_quote_out" and facts.get("source_post_price") is None
    assert facts["impact_unknown"] == "leg_unverified"


def test_a_clamped_reference_says_so_to_the_executor_and_through_the_retry(monkeypatch):
    # Lambda's review 6: the executor re-derives the basis from the price alone, so the clamp rides the note.
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "post_trade", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_CHASE_POST_TRADE_MAX_RATIO", 2.0, raising=False)
    row = {"mint": "m", "source_user_id": "u", "handle": "h", "network_id": "solana", "source_size_usd": 1.0,
           "source_price": 1e-4, "signature": "s", "detected_at": "2026-09-28T14:13:24+00:00",
           "source_valuation_json": json.dumps({"source_post_price": 5e-4})}
    ev = lane.OnchainEv(row)
    assert ev.chase_reference_basis == "post_trade_clamped"
    assert ev.chase_reference_note == "post_trade_clamped at 2.0x the average fill"
    assert lane.event_from_retry_payload(lane.retry_payload(ev)).chase_reference_note == ev.chase_reference_note
    row["source_valuation_json"] = json.dumps({"source_post_price": 5e-4, "impact_leg": "pump_amm"})
    assert lane.OnchainEv(row).chase_reference_note == (
        "routed_leg:pump_amm; average includes upstream leg and router fees; "
        "post_trade_clamped at 2.0x the average fill")
