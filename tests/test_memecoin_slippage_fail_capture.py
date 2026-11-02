"""Slippage-failed Solana buys join the refusal capture as family `slippage_fail` (Alpha, 2026-09-30).

A buy that lands and reverts on its minimum-out is not a refusal, so no family matched it, and Gamma
graded those coins by hand from chain swaps. The operator is weighing a per-wallet slippage
exception for bot-heavy wallets (Degen, notdecu). The forward series of the failed coin, plus how far
short the fill fell, is the data for that decision.

Real terminal reasons, 2026-09-16..30 (memecoin_entry_attempts, terminal_status 'failed'):
  chain_error:BuySlippageBelowMinTokensOut                 12  pump curve 6042, slot lane
  chain_error:BuySlippageBelowMinBaseAmountOut             11  PumpSwap 6040, slot lane
  chain_error:slippage tolerance exceeded (Jupiter 0x1771)  3  named only when the program is JUP6
  chain_error:exceeds desired slippage limit                2  Raydium router's own log line
Recount 2026-09-16..10-07 (Lambda, for the CPMM addition; terminal_status 'failed', every row a BUY):
  chain_error:BuySlippageBelowMinTokensOut                 48  slot lane
  chain_error:BuySlippageBelowMinBaseAmountOut             46  slot lane
  chain_error:slippage tolerance exceeded (Jupiter 0x1771)  7  J7, on-chain, fallback and slot lanes
  chain_error:ExceededSlippage                              6  Raydium CPMM 6005, slot lane; all 6 landed
                                                               and reverted (receipts read 10-07:
                                                               Program CPMMoo8L... failed, 0x1775)
  chain_error:exceeds desired slippage limit                2  on-chain and J7 lanes
NOT matched: bare "chain_error:custom program error: 0x1771" (8 rows, 4 on the slot lane 09-26).
6001 is slippage only in Jupiter v6 (solana_real_seams._program_error_name), and a bare code names
no program, so it is not proven slippage.
"""
import json
import sqlite3

import pytest

from sniper.services import memecoin_refusal_capture as cap

SLIPPAGE_REASONS = (
    "chain_error:BuySlippageBelowMinTokensOut",
    "chain_error:BuySlippageBelowMinBaseAmountOut",
    "chain_error:slippage tolerance exceeded (Jupiter 0x1771)",
    "chain_error:exceeds desired slippage limit",
    "chain_error:ExceededSlippage",
)


@pytest.mark.parametrize("reason", SLIPPAGE_REASONS)
def test_real_slippage_failures_are_family_slippage_fail(reason):
    assert cap.classify_gate(reason) == "slippage_fail"
    assert cap.should_capture("slippage_fail")


@pytest.mark.parametrize("reason", [
    "chain_error:custom program error: 0x1771",           # program unknown: not proven slippage
    'chain_error:{"InstructionError":[0,{"Custom":38}]}',
    "chain_error:NotEnoughTokensToBuy",
    "chain_error:BondingCurveComplete",
    "chain_error:VolumeCapExceeded",
    "BuySlippageBelowMinTokensOut",                       # exact identity, never a substring
    "ExceededSlippage",
    "chain_error:ExceededSlippageX",
    "chain_error:BuySlippageBelowMinTokensOut (retry)",
])
def test_other_chain_errors_are_not_slippage_fail(reason):
    assert cap.classify_gate(reason) != "slippage_fail"


def test_slippage_fail_is_low_priority_and_sized():
    assert "slippage_fail" in cap.LOW_PRIORITY_FAMILIES
    assert "slippage_fail" in cap.SIZED_FAMILIES


# The slot lane's own substage, verbatim from attempt c2f5b06a (2026-09-30T06:50:47Z).
PUMP_SLIPPAGE = {"available_tokens_atomic": 2471353520672, "fee_lamports": 993000,
                 "landing_slot": 451888789, "min_tokens_atomic": 3337170587854,
                 "name": "slot_pump_slippage", "no_tokens_bought": True,
                 "reason": "price_moved_beyond_limit"}


def _trace(*events):
    return json.dumps({"substage_events": [{"name": "slot_price_limit", "min_tokens_atomic": 1},
                                           *events]})


def test_available_over_min_is_read_from_the_slot_lane_substage():
    ratio = cap.slippage_available_over_min(_trace(PUMP_SLIPPAGE))
    assert ratio == pytest.approx(2471353520672 / 3337170587854)


@pytest.mark.parametrize("trace", [
    None, "", "not json", json.dumps([]), _trace(),                       # the error carries no pair
    _trace({**PUMP_SLIPPAGE, "min_tokens_atomic": 0}),
    _trace({**PUMP_SLIPPAGE, "available_tokens_atomic": None}),
    _trace({**PUMP_SLIPPAGE, "available_tokens_atomic": "2471353520672"}),
    _trace({**PUMP_SLIPPAGE, "available_tokens_atomic": True}),
])
def test_available_over_min_is_unknown_when_the_error_does_not_carry_it(trace):
    assert cap.slippage_available_over_min(trace) is None


def _ledger(reason, trace):
    from sniper.persistence import db_memecoin as m
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = m.reserve_pending(conn, mint="MINT", source_user_id="u", strategy="alpha_b",
                            notional=101.42, reserved_at="2026-09-30T06:50:46+00:00",
                            attempt_id="A")
    conn.execute(
        "INSERT INTO memecoin_entry_attempts (attempt_id,mint,origin_event_id,terminal_reason,"
        "decision_started_at,network_id,terminal_status,pending_id,completed_at,trace_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("A", "MINT", "O", reason, "2026-09-30T06:50:46+00:00", "solana", "failed", pid,
         "2026-09-30T06:50:47+00:00", trace))
    return conn


def test_a_polled_slippage_failure_carries_its_size_and_ratio():
    from scripts.memecoin_refusal_capture_loop import poll_refusals
    conn = _ledger("chain_error:BuySlippageBelowMinTokensOut", _trace(PUMP_SLIPPAGE))
    [row], _ = poll_refusals(conn, since_iso="2026-09-30T06:50:00+00:00")
    assert row["gate"] == "slippage_fail"
    assert row["quote_input_usd"] == 101.42
    assert row["slippage_available_over_min"] == pytest.approx(2471353520672 / 3337170587854)


def test_a_jupiter_slippage_failure_polls_with_an_unknown_ratio():
    from scripts.memecoin_refusal_capture_loop import poll_refusals
    conn = _ledger("chain_error:slippage tolerance exceeded (Jupiter 0x1771)", _trace())
    [row], _ = poll_refusals(conn, since_iso="2026-09-30T06:50:00+00:00")
    assert row["gate"] == "slippage_fail" and row["slippage_available_over_min"] is None


def test_every_observation_of_the_capture_records_the_ratio(tmp_path):
    from sniper.persistence import db_memecoin_captures as store
    capture = cap.start({}, mint="MINT", origin_event_id="O", gate="slippage_fail",
                        refused_at="2026-09-30T06:50:46+00:00", horizon_s=810, cadence_s=25,
                        max_concurrent=6, now=0, now_epoch=1790000000, quote_input_usd=101.42,
                        slippage_available_over_min=0.7406)
    rows = []
    for _ in range(2):                                   # the buy that fixes the bag, then a sell
        rows.append(cap.quote_once(
            capture, http=lambda *_a, **_k: (200, {}, None), rpc_url="rpc", clip_usd=20,
            now_iso="2026-09-30T06:50:50+00:00", now_mono=0,
            quote_fn=lambda mint, usd, **_: {"outAmount": "100000000"},
            exit_fn=lambda mint, atoms, slip, **_: {"outAmount": "90000000"},
            decimals_fn=lambda *_a, **_k: 6))
        capture.tokens_atomic = capture.tokens_atomic or 100000000
    assert [r["slippage_available_over_min"] for r in rows] == [0.7406, 0.7406]
    conn = store.connect(tmp_path / "captures.db")
    store.record_observation(conn, rows[0])
    assert conn.execute("SELECT slippage_available_over_min FROM memecoin_refusal_captures"
                        ).fetchone()[0] == 0.7406


def test_an_existing_captures_table_gains_the_column(tmp_path):
    from sniper.persistence import db_memecoin_captures as store
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE memecoin_refusal_captures (id INTEGER PRIMARY KEY, capture_id TEXT "
                "NOT NULL, origin_event_id TEXT, network_id TEXT NOT NULL, mint TEXT NOT NULL, "
                "gate TEXT, refused_at TEXT NOT NULL, observed_at TEXT, age_s REAL, horizon_s REAL, "
                "quote_price REAL, quote_input_usd REAL, quote_route_target TEXT, quote_id TEXT, "
                "provider TEXT, endpoint_index INTEGER, latency_ms REAL, http_status INTEGER, "
                "quality TEXT, expected_obs INTEGER, outcome TEXT)")
    old.commit()
    old.close()
    conn = store.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_refusal_captures)")}
    assert "slippage_available_over_min" in cols
