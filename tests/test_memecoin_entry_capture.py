"""Entry feature capture, phase P1 (spec 2026-09-29-memecoin-entry-feature-capture-design.md).

One feature row per terminal entry attempt, written by its own process into its own database, read
from committed ledger rows and our own tape only (zero network in P1). The rules pinned here:
  * the funded entry path never imports or calls the capture;
  * only terminal attempts are captured, once each, and a lookback survives late commits;
  * every feature is a value with coverage 'ok', or null with a typed reason, never a guessed 0;
  * windows end at the decision: nothing after it is read, and our own trades are not order flow;
  * the blind spots the 09-29 study found are filled post hoc (consensus, repeat buy, drift parts).
"""
import ast
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sniper.persistence import db_entry_features as store
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_entry_capture as cap

ROOT = Path(__file__).resolve().parents[1]
MINT = "0x70d3e2fc00000000000000000000000000000000"
QUOTE = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
POOL = "0xc093bcee00000000000000000000000000000000"
SRC_TX = "0x" + "5" * 64
DECISION_TS = 1790695200                       # 2026-09-29T15:20:00Z
DECISION_ISO = "2026-09-29T15:20:00+00:00"


# ---------------------------------------------------------------------------- the ledger

def _ledger():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _attempt(c, attempt_id="a1", *, network="4663", mint=MINT, status="filled",
             reason=None, origin="onchain:4663:%s:%s" % (SRC_TX, MINT), source="w1",
             decision=DECISION_ISO, completed="2026-09-29T15:20:03+00:00",
             source_created="2026-09-29T15:19:58+00:00", classification=None, **cols):
    base = {"attempt_id": attempt_id, "origin_event_id": origin, "network_id": network,
            "mint": mint, "source_user_id": source, "decision_started_at": decision,
            "source_created_at": source_created, "terminal_status": status,
            "terminal_reason": reason if reason is not None else ("filled" if status else None),
            "completed_at": completed,
            "classification_json": json.dumps(classification or {"facts": {}}), **cols}
    keys = ",".join(base)
    c.execute(f"INSERT INTO memecoin_entry_attempts ({keys}) VALUES ({','.join('?' * len(base))})",
              tuple(base.values()))
    c.commit()


def _event(c, *, source="w1", mint=MINT, network="4663", size=100.0, created, side="buy", eid,
           received=None):
    c.execute("INSERT INTO memecoin_events (fomo_event_id, source_user_id, mint, side, "
              "their_size_usd, created_at, received_at, network_id) VALUES (?,?,?,?,?,?,?,?)",
              (eid, source, mint, side, size, created, received or created, network))
    c.commit()


def _close(c, *, source="w1", pnl, closed, mint="0x" + "9" * 40, network="4663"):
    c.execute("INSERT INTO memecoin_positions (source_user_id, mint, ticker, entry_price, "
              "entry_notional, is_open, opened_at, closed_at, realized_pnl, execution_mode, "
              "network_id, strategy) VALUES (?,?,?,?,?,0,?,?,?,'real',?,'alpha_b')",
              (source, mint, "X", 1e-6, 10.0, closed, closed, pnl, network))
    c.commit()


# ---------------------------------------------------------------------------- the tape

def _tape(swaps=(), *, blocks=None, price_observed=DECISION_TS - 30, resets=(), iso_ts=False):
    """swaps: (block, tx, quote_amount_signed, maker). Blocks every 2 s ending at the decision."""
    t = sqlite3.connect(":memory:")
    t.row_factory = sqlite3.Row
    t.executescript("""
        CREATE TABLE pool_ticks (id INTEGER PRIMARY KEY AUTOINCREMENT, chain_id TEXT, pool TEXT,
            kind TEXT, block_no INTEGER, tx_sig TEXT, log_index INTEGER, maker TEXT,
            amount0 TEXT, amount1 TEXT, observed_at TEXT, block_hash TEXT);
        CREATE TABLE pool_meta (chain_id TEXT, pool TEXT, token0 TEXT, token1 TEXT,
            quote_side INTEGER, resolved_at TEXT, token0_decimals INTEGER, token1_decimals INTEGER);
        CREATE TABLE block_times (chain_id TEXT, block_no INTEGER, block_ts INTEGER,
            fetched_at TEXT, block_hash TEXT, PRIMARY KEY (chain_id, block_no));
        CREATE TABLE onchain_pool_protocols (chain_id TEXT, pool TEXT, protocol TEXT, mint TEXT);
        CREATE TABLE volume_quote_prices (chain_id TEXT, token TEXT, usd REAL, observed REAL,
            PRIMARY KEY(chain_id, token));
        CREATE TABLE tick_gaps (id INTEGER PRIMARY KEY, chain_id TEXT NOT NULL,
            from_block INTEGER NOT NULL, to_block INTEGER NOT NULL, reason TEXT NOT NULL,
            detected_at TEXT NOT NULL, resolved_at TEXT);
        CREATE TABLE volume_feed_resets (chain_id TEXT, at TEXT, reason TEXT, window_s REAL);
    """)
    t.execute("INSERT INTO onchain_pool_protocols VALUES ('4663', ?, 'v4', ?)", (POOL, MINT))
    t.execute("INSERT INTO pool_meta VALUES ('4663', ?, ?, ?, 0, '', 18, 18)", (POOL, QUOTE, MINT))
    t.execute("INSERT INTO volume_quote_prices VALUES ('4663', ?, 2000.0, ?)", (QUOTE, price_observed))
    for block, ts in (blocks if blocks is not None
                      else [(1000 + i, DECISION_TS - 400 + 2 * i) for i in range(260)]):
        stamp = (datetime.fromtimestamp(ts, timezone.utc).isoformat() if iso_ts else ts)
        t.execute("INSERT INTO block_times VALUES ('4663', ?, ?, '', ?)", (block, stamp, "0xh%d" % block))
    for i, (block, tx, quote, maker) in enumerate(swaps):
        t.execute("INSERT INTO pool_ticks (chain_id, pool, kind, block_no, tx_sig, log_index, maker, "
                  "amount0, amount1, observed_at, block_hash) VALUES ('4663',?,?,?,?,?,?,?,?,?,?)",
                  (POOL, "v4_swap", block, tx, i, maker, str(int(quote * 10 ** 18)),
                   str(-int(abs(quote) * 10 ** 24)), "", "0xh%d" % block))
    for at in resets:
        t.execute("INSERT INTO volume_feed_resets VALUES ('4663', ?, 'reconnect', 300)", (at,))
    t.commit()
    return t


def _block_at(ts):
    return 1000 + (ts - (DECISION_TS - 400)) // 2


def _row(c, t=None, attempt_id="a1"):
    ledger_row = dict(c.execute("SELECT * FROM memecoin_entry_attempts WHERE attempt_id=?",
                                (attempt_id,)).fetchone())
    out = cap.capture_attempt(c, {"4663": t} if t is not None else {}, ledger_row,
                              now_iso="2026-09-29T15:20:10+00:00")
    return out, json.loads(out["features_json"]), json.loads(out["coverage_json"])


# ---------------------------------------------------------------------------- the fill path

ENTRY_PATH = ["sniper/services/memecoin_real_runner.py", "sniper/services/memecoin_real_leg.py",
              "sniper/services/memecoin_evm_leg.py", "sniper/execution/evm_swap_executor.py",
              "sniper/execution/evm_real_seams.py", "sniper/execution/solana_swap_executor.py",
              "sniper/execution/solana_real_seams.py", "sniper/services/memecoin_onchain_lane.py",
              "sniper/services/memecoin_slot_lane.py", "scripts/memecoin_runner_loop.py"]


@pytest.mark.parametrize("path", ENTRY_PATH)
def test_the_funded_entry_path_never_references_the_capture(path):
    tree = ast.parse((ROOT / path).read_text())
    names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any("entry_capture" in n or "db_entry_features" in n for n in names), path
    assert "memecoin_entry_features" not in (ROOT / path).read_text(), path


# ---------------------------------------------------------------------------- the cursor

def test_only_terminal_attempts_are_captured_once_each(tmp_path):
    c = _ledger()
    _attempt(c, "a1")
    _attempt(c, "a2", status=None, completed=None)          # still running: not a decision yet
    db = store.connect(tmp_path / "features.db")
    assert cap.run_once(c, {}, db, now_iso="2026-09-29T15:21:00+00:00") == 1
    assert cap.run_once(c, {}, db, now_iso="2026-09-29T15:21:05+00:00") == 0
    assert [r[0] for r in db.execute("SELECT attempt_id FROM entry_features")] == ["a1"]


def test_a_late_commit_behind_the_cursor_is_still_captured(tmp_path):
    c = _ledger()
    _attempt(c, "a1", completed="2026-09-29T15:20:03+00:00")
    db = store.connect(tmp_path / "features.db")
    cap.run_once(c, {}, db, now_iso="2026-09-29T15:21:00+00:00")
    _attempt(c, "a0", completed="2026-09-29T15:19:59+00:00")  # committed after, completed before
    assert cap.run_once(c, {}, db, now_iso="2026-09-29T15:21:05+00:00") == 1
    assert {r[0] for r in db.execute("SELECT attempt_id FROM entry_features")} == {"a0", "a1"}


def test_the_capture_ledger_connection_is_read_only(tmp_path):
    ledger = tmp_path / "ledger.db"
    c = sqlite3.connect(ledger)
    m.ensure_memecoin_tables(c)
    c.close()
    ro = cap.open_ledger(str(ledger))
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM memecoin_entry_attempts")


# ---------------------------------------------------------------------------- source context

def test_the_source_record_counts_only_closes_before_the_decision():
    c = _ledger()
    _close(c, pnl=+5.0, closed="2026-09-29T10:00:00+00:00")
    _close(c, pnl=-2.0, closed="2026-09-29T11:00:00+00:00")
    _close(c, pnl=+9.0, closed="2026-09-29T16:00:00+00:00")   # after the decision
    _attempt(c)
    _o, f, cov = _row(c)
    assert (f["src_prior_n"], f["src_prior_sum"], f["src_prior_wins"]) == (2, 3.0, 1)
    assert cov["src_prior_n"] == "ok"


def test_no_prior_closes_is_a_measured_zero():
    c = _ledger()
    _attempt(c)
    _o, f, cov = _row(c)
    assert f["src_prior_n"] == 0 and f["src_prior_sum"] is None and cov["src_prior_n"] == "ok"


def test_the_repeat_buy_and_size_context_come_from_history_before_the_event():
    c = _ledger()
    _event(c, eid="e1", size=50.0, created="2026-09-29T15:10:00+00:00")
    _event(c, eid="e2", size=150.0, created="2026-09-28T15:10:00+00:00", mint="0x" + "8" * 40)
    _event(c, eid="e3", size=999.0, created="2026-09-29T15:30:00+00:00")   # after: never counted
    _event(c, eid="src", size=300.0, created="2026-09-29T15:19:58+00:00")   # the copied trade
    _attempt(c, source_created="2026-09-29T15:19:58+00:00", origin="src")
    _o, f, cov = _row(c)
    assert f["src_prior_buys_mint"] == 1, "the copied trade itself is not a prior buy"
    assert f["src_size_usd"] == 300.0
    assert f["src_size_median_30d"] == 100.0 and f["src_size_over_median"] == 3.0
    assert cov["src_prior_buys_mint"] == "ok"


def test_an_unknown_source_time_leaves_history_features_unknown():
    c = _ledger()
    _attempt(c, source_created=None)
    _o, f, cov = _row(c)
    assert f["src_prior_buys_mint"] is None and cov["src_prior_buys_mint"] == "unavailable"


# ---------------------------------------------------------------------------- blind spots

def test_consensus_is_computed_even_when_the_runner_passed_none():
    c = _ledger()
    for i, who in enumerate(("w2", "w3")):
        _event(c, eid="c%d" % i, source=who, created="2026-09-29T15:%02d:00+00:00" % (5 + i))
    _attempt(c, classification={"facts": {"consensus_n_60m": None}})
    _o, f, cov = _row(c)
    assert f["consensus_n_60m"] == 2 and cov["consensus_n_60m"] == "ok"


def test_the_drift_components_are_kept_apart():
    c = _ledger()
    _attempt(c, source_drift_pct=4.0, execution_slippage_pct=1.5, total_copy_drift_pct=5.56)
    _o, f, _cov = _row(c)
    assert (f["source_drift_pct"], f["execution_slippage_pct"], f["total_copy_drift_pct"]) == (
        4.0, 1.5, 5.56)


# ---------------------------------------------------------------------------- EVM order flow

def test_the_flow_window_ends_at_the_decision_and_excludes_our_own_trade():
    swaps = [(_block_at(DECISION_TS - 20), "0xb1", +1.0, "0xm1"),
             (_block_at(DECISION_TS - 10), SRC_TX, +0.5, "0xsrc"),
             (_block_at(DECISION_TS - 8), "0xour", +0.2, "0xme"),          # our own fill
             (_block_at(DECISION_TS - 4), "0xs1", -0.3, "0xm2"),
             (_block_at(DECISION_TS + 2), "0xlate", +9.0, "0xm3")]         # after the decision
    t = _tape(swaps)
    c = _ledger()
    _attempt(c, tx_sig="0xour")
    _o, f, cov = _row(c, t)
    assert cov["flow_60s"] == "ok"
    flow = f["flow_60s"]
    assert (flow["buy_n"], flow["sell_n"]) == (2, 1), "own fill and the late swap are not flow"
    assert flow["buy_usd"] == pytest.approx(3000.0) and flow["sell_usd"] == pytest.approx(600.0)
    assert flow["unique_makers"] == 3 and flow["largest_buy_usd"] == pytest.approx(2000.0)
    assert flow["source_buy_usd"] == pytest.approx(1000.0)
    assert f["flow_ex_source_60s"]["buy_n"] == 1


def test_a_swap_one_second_before_the_decision_counts_one_after_does_not():
    before = [(_block_at(DECISION_TS - 2), SRC_TX, +0.5, "0xsrc"),
              (_block_at(DECISION_TS), "0xat", +1.0, "0xm1")]
    after = before + [(_block_at(DECISION_TS + 2), "0xafter", +1.0, "0xm2")]
    c = _ledger()
    _attempt(c)
    assert _row(c, _tape(before))[1]["flow_30s"]["buy_n"] == 2
    assert _row(c, _tape(after))[1]["flow_30s"]["buy_n"] == 2


def test_a_tape_blind_to_the_source_trade_is_unavailable_not_quiet():
    t = _tape([(_block_at(DECISION_TS - 20), "0xb1", +1.0, "0xm1")])
    c = _ledger()
    _attempt(c)
    _o, f, cov = _row(c, t)
    assert f["flow_60s"] is None and cov["flow_60s"] == "unavailable:source_not_on_tape"


def test_a_feed_reset_inside_the_window_is_unavailable():
    t = _tape([(_block_at(DECISION_TS - 10), SRC_TX, +0.5, "0xsrc")],
              resets=("2026-09-29T15:19:40+00:00",))
    c = _ledger()
    _attempt(c)
    _o, f, cov = _row(c, t)
    assert f["flow_60s"] is None and cov["flow_60s"] == "unavailable:feed_reset"


def test_a_stale_quote_price_is_unavailable_not_zero():
    t = _tape([(_block_at(DECISION_TS - 10), SRC_TX, +0.5, "0xsrc")],
              price_observed=DECISION_TS - 3600)
    c = _ledger()
    _attempt(c)
    _o, f, cov = _row(c, t)
    assert f["flow_60s"] is None and cov["flow_60s"] == "unavailable:quote_price_stale"


def test_a_window_older_than_the_retained_tape_is_null_not_zero():
    # The tape holds only the last 40 s: block 1015 is DECISION_TS - 10 in this layout.
    t = _tape([(1015, SRC_TX, +0.5, "0xsrc")],
              blocks=[(1000 + i, DECISION_TS - 40 + 2 * i) for i in range(30)])
    c = _ledger()
    _attempt(c)
    _o, f, cov = _row(c, t)
    assert cov["flow_300s"] == "unavailable:tape_retention" and f["flow_300s"] is None
    assert cov["flow_30s"] == "ok"


def test_same_block_buyers_after_the_source():
    b = _block_at(DECISION_TS - 10)
    t = _tape([(b, "0xbefore", +0.1, "0xm0"), (b, SRC_TX, +0.5, "0xsrc"),
               (b, "0xa1", +0.2, "0xm1"), (b, "0xa2", +0.3, "0xm2"), (b + 1, "0xnext", +1.0, "0xm3")])
    c = _ledger()
    _attempt(c)
    _o, f, _cov = _row(c, t)
    assert f["same_block_buyers_after_source"] == 2


def test_solana_and_chains_without_a_tape_record_why():
    c = _ledger()
    _attempt(c, "s1", network="solana", mint="So1111", origin="onchain:" + "3" * 88)
    _attempt(c, "b1", network="56")
    assert _row(c, attempt_id="s1")[2]["flow_60s"] == "not_attempted:t1"
    assert _row(c, attempt_id="b1")[2]["flow_60s"] == "unsupported_chain"
    # the book at the decision is a T0 feature of every Solana row (Gamma on 752569c54)
    _o, f, cov = _row(c, attempt_id="s1")
    assert f["sol_real_open_at_decision"] == 0 and cov["sol_real_open_at_decision"] == "ok"
    assert _row(c, attempt_id="b1")[2]["sol_real_open_at_decision"] == "not_applicable:chain"


def test_the_same_address_on_another_chain_is_another_coin():
    t = _tape([(_block_at(DECISION_TS - 10), SRC_TX, +0.5, "0xsrc")])
    c = _ledger()
    _attempt(c, network="8453")
    _o, f, cov = _row(c, t)                       # the 4663 tape must not answer for 8453
    assert f["flow_60s"] is None and cov["flow_60s"] in ("unavailable:tape_missing",
                                                         "unavailable:no_known_pool")


def test_the_live_tape_stores_block_time_as_iso_text():
    """The live tape writes block_ts as ISO text; a numeric-only read raised on every EVM row
    (found by the P1 dry run on the live store)."""
    t = _tape([(_block_at(DECISION_TS - 10), SRC_TX, +0.5, "0xsrc")], iso_ts=True)
    c = _ledger()
    _attempt(c)
    _o, f, cov = _row(c, t)
    assert cov["flow_60s"] == "ok" and f["flow_60s"]["buy_n"] == 1


def test_an_onchain_origin_takes_its_size_from_the_detection():
    c = _ledger()
    c.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, address, network_id, "
              "mint, source_size_usd, detected_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
              (SRC_TX, "w1", "0xw", "4663", MINT, 420.0, DECISION_ISO, DECISION_ISO))
    c.commit()
    _attempt(c)
    assert _row(c)[1]["src_size_usd"] == 420.0


def test_our_own_exit_in_the_window_is_not_order_flow():
    swaps = [(_block_at(DECISION_TS - 10), SRC_TX, +0.5, "0xsrc"),
             (_block_at(DECISION_TS - 6), "0xourexit", -0.4, "0xme")]
    c = _ledger()
    _attempt(c, "old", status="filled", position_id=7, completed="2026-09-29T15:00:00+00:00",
             decision="2026-09-29T14:59:00+00:00")
    c.execute("INSERT INTO memecoin_fills (position_id, kind, price, qty, notional, created_at, "
              "tx_sig) VALUES (7, 'bracket_exit', 1.0, 1.0, 1.0, ?, '0xourexit')", (DECISION_ISO,))
    c.commit()
    _attempt(c)
    assert _row(c, _tape(swaps))[1]["flow_60s"]["sell_n"] == 0


def test_a_buy_received_after_the_decision_is_not_counted():
    """The relay's created_at runs ahead of our receipt (4663: +23 s on 09-29). A buy stamped
    before the decision but received after it was not knowable at the decision."""
    c = _ledger()
    _event(c, eid="k1", source="w2", created="2026-09-29T15:05:00+00:00")
    _event(c, eid="k2", source="w3", created="2026-09-29T15:19:50+00:00",
           received="2026-09-29T15:20:30+00:00")                   # arrived after the decision
    _event(c, eid="k3", created="2026-09-29T15:10:00+00:00", received="2026-09-29T15:25:00+00:00")
    _attempt(c)
    _o, f, _cov = _row(c)
    assert f["consensus_n_60m"] == 1
    assert f["src_prior_buys_mint"] == 0


def test_the_runner_consensus_is_kept_beside_the_recompute():
    c = _ledger()
    _attempt(c, "a1", classification={"facts": {"consensus_n_60m": 3}})
    _attempt(c, "a2", classification={"facts": {}})
    assert _row(c, attempt_id="a1")[1]["consensus_n_60m_runner"] == 3
    assert _row(c, attempt_id="a2")[2]["consensus_n_60m_runner"] == "not_applicable:runner_did_not_compute"


def test_the_source_anchored_windows_and_the_copier_queue():
    src_t = DECISION_TS - 20
    swaps = [(_block_at(src_t - 50), "0xearly", +1.0, "0xm1"),
             (_block_at(src_t - 10), "0xpre", +0.5, "0xm2"),
             (_block_at(src_t), SRC_TX, +0.5, "0xsrc"),
             (_block_at(src_t), "0xsame", +0.4, "0xm5"),       # same block, after the source
             (_block_at(src_t + 6), "0xq1", +0.2, "0xm3"),
             (_block_at(src_t + 10), "0xq2", -0.1, "0xm4")]
    c = _ledger()
    _attempt(c)
    _o, f, cov = _row(c, _tape(swaps))
    assert f["source_anchor"] == "block_time"
    assert (f["pre_source_30s"]["buy_n"], f["pre_source_60s"]["buy_n"]) == (1, 2)
    assert (f["copier_queue"]["buy_n"], f["copier_queue"]["sell_n"]) == (1, 1), (
        "the queue starts at the next block; same-block buyers are counted on their own")
    assert f["same_block_buyers_after_source"] == 1


def test_without_a_source_tx_the_windows_anchor_on_our_receipt_and_say_so():
    c = _ledger()
    _attempt(c, origin="swap:9f1c2e2a-uuid", observed_at="2026-09-29T15:19:50+00:00")
    _o, f, _cov = _row(c, _tape([(_block_at(DECISION_TS - 5), "0xq", +0.1, "0xm")]))
    assert f["source_anchor"] == "received_at" and f["copier_queue"]["buy_n"] == 1


def test_no_read_transaction_is_left_open(tmp_path):
    """A read transaction held open pins WAL checkpoints on the funded ledger (review LOW 7)."""
    c = _ledger()
    t = _tape([(_block_at(DECISION_TS - 10), SRC_TX, +0.5, "0xsrc")])
    _attempt(c)
    db = store.connect(tmp_path / "features.db")
    cap.run_once(c, {"4663": t}, db, now_iso="2026-09-29T15:21:00+00:00")
    assert not c.in_transaction and not t.in_transaction and not db.in_transaction


def test_a_lower_rowid_that_completes_second_is_captured(tmp_path):
    """Review MED 1: attempts are inserted at decision start and completed later; fills are the
    slow ones. The cursor follows completion, not insertion."""
    c = _ledger()
    c.execute("INSERT INTO memecoin_entry_attempts (attempt_id, network_id, mint, source_user_id, "
              "decision_started_at) VALUES ('slow', 'solana', 'Mslow', 'w1', ?)", (DECISION_ISO,))
    c.commit()
    _attempt(c, "fast", network="solana", mint="Mfast", completed="2026-09-29T15:20:02+00:00")
    db = store.connect(tmp_path / "features.db")
    cap.run_once(c, {}, db, now_iso="2026-09-29T15:20:05+00:00")
    c.execute("UPDATE memecoin_entry_attempts SET terminal_status='filled', terminal_reason='filled', "
              "completed_at='2026-09-29T15:20:09+00:00' WHERE attempt_id='slow'")
    c.commit()
    cap.run_once(c, {}, db, now_iso="2026-09-29T15:20:12+00:00")
    assert {r[0] for r in db.execute("SELECT attempt_id FROM entry_features")} == {"fast", "slow"}


# ---------------------------------------------------------------------------- Gamma review, 2026-09-29

def _iso_at(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def test_a_full_page_inside_the_lookback_does_not_stop_the_cursor(tmp_path):
    """MED 1: 250 attempts in 8 min, then 5 more. A single 200-row read stuck the cursor on its
    last row and never captured anything after it (200 of 255)."""
    c = _ledger()
    for i in range(250):
        _attempt(c, "p%03d" % i, network="solana", mint="M%d" % i, origin="swap:%d" % i,
                 completed=_iso_at(DECISION_TS + 2 * i))
    db = store.connect(tmp_path / "features.db")
    now = DECISION_TS + 500
    for k in range(3):
        cap.run_once(c, {}, db, now_iso=_iso_at(now + 5 * k))
    for i in range(5):
        _attempt(c, "late%d" % i, network="solana", mint="L%d" % i, origin="swap:late%d" % i,
                 completed=_iso_at(now + 20 + i))
    cap.run_once(c, {}, db, now_iso=_iso_at(now + 30))
    assert db.execute("SELECT COUNT(*) FROM entry_features").fetchone()[0] == 255


def test_equal_completion_stamps_across_a_page_boundary_are_all_captured(tmp_path):
    c = _ledger()
    for i in range(5):
        _attempt(c, "e%d" % i, network="solana", mint="E%d" % i, origin="swap:e%d" % i,
                 completed="2026-09-29T15:20:03+00:00")
    db = store.connect(tmp_path / "features.db")
    assert cap.run_once(c, {}, db, now_iso="2026-09-29T15:20:05+00:00", page=2) == 5


def test_a_tape_that_has_not_reached_the_decision_is_retried_not_written(tmp_path):
    """MED 2: the tape trails the chain by 1-2 s, and a fresh EVM attempt is read within seconds.
    A read then wrote a permanent 'tape_retention' for flow the tape was about to show."""
    blocks = [(1000 + i, DECISION_TS - 400 + 2 * i) for i in range(200)]    # head: decision - 2 s
    t = _tape([(_block_at(DECISION_TS - 10), SRC_TX, +0.5, "0xsrc")], blocks=blocks)
    c = _ledger()
    _attempt(c, completed="2026-09-29T15:20:00.200000+00:00")
    db = store.connect(tmp_path / "features.db")
    stats = {}
    assert cap.run_once(c, {"4663": t}, db, now_iso="2026-09-29T15:20:01+00:00", stats=stats) == 0
    assert stats == {"deferred": 1}
    # A block in the decision's own second does not prove that second is complete.
    t.execute("INSERT INTO block_times VALUES ('4663', 1200, ?, '', '0xh1200')", (DECISION_TS,))
    t.commit()
    assert cap.run_once(c, {"4663": t}, db, now_iso="2026-09-29T15:20:02+00:00") == 0
    t.execute("INSERT INTO block_times VALUES ('4663', 1201, ?, '', '0xh1201')", (DECISION_TS + 1,))
    t.commit()
    assert cap.run_once(c, {"4663": t}, db, now_iso="2026-09-29T15:20:03+00:00") == 1
    cov = json.loads(db.execute("SELECT coverage_json FROM entry_features").fetchone()[0])
    assert cov["flow_30s"] == "ok" and cov["copier_queue"] == "ok"


def test_a_tape_still_behind_after_the_wait_says_so(tmp_path):
    blocks = [(1000 + i, DECISION_TS - 400 + 2 * i) for i in range(200)]
    t = _tape([(_block_at(DECISION_TS - 10), SRC_TX, +0.5, "0xsrc")], blocks=blocks)
    c = _ledger()
    _attempt(c, completed="2026-09-29T15:20:00.200000+00:00")
    db = store.connect(tmp_path / "features.db")
    late = _iso_at(DECISION_TS + cap.TAPE_WAIT_S + 1)
    assert cap.run_once(c, {"4663": t}, db, now_iso=late) == 1
    cov = json.loads(db.execute("SELECT coverage_json FROM entry_features").fetchone()[0])
    assert cov["flow_30s"] == "unavailable:tape_behind"


def test_the_wait_ends_inside_the_lookback():
    """A waiting attempt is re-read only while it is inside the lookback; past it, it gets no row."""
    assert cap.TAPE_WAIT_S + 60 < cap.LOOKBACK_S


def test_a_tape_chain_without_its_tape_says_tape_missing():
    """LOW 3: a missing tape store is not an unsupported chain."""
    c = _ledger()
    _attempt(c)
    assert _row(c)[2]["flow_60s"] == "unavailable:tape_missing"


def test_the_loop_reads_its_settings_from_the_state_root(tmp_path):
    """MED 3: a pinned release has no .env. Loading from the code directory started the loop on
    library defaults, so Base V2 pools dropped out of the tape's coverage. Fresh interpreter: the
    loader runs at import."""
    import os
    import subprocess
    import sys
    # .env spells DB_PATH relative, as the live one does; the ledger must still be the state root's.
    (tmp_path / ".env").write_text("ENTRY_CAPTURE_ENV_PROBE=from_state_root\n"
                                   "DB_PATH=sniper_tracker.db\n")
    env = {k: v for k, v in os.environ.items() if k not in ("ENTRY_CAPTURE_ENV_PROBE", "DB_PATH")}
    env["SNIPER_STATE_ROOT"] = str(tmp_path)
    code = ("import importlib.util, json, os; "
            "spec = importlib.util.spec_from_file_location('loop', %r); "
            "loop = importlib.util.module_from_spec(spec); spec.loader.exec_module(loop); "
            "print(json.dumps([os.environ.get('ENTRY_CAPTURE_ENV_PROBE'), str(loop.ledger_path())]))"
            % str(ROOT / "scripts" / "memecoin_entry_capture_loop.py"))
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(ROOT), capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    probe, ledger = json.loads(out.stdout.strip().splitlines()[-1])
    assert probe == "from_state_root"
    assert ledger == str((tmp_path / "sniper_tracker.db").resolve())
