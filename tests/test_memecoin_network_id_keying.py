"""Chain-ambiguity hardening: the four mint-only memecoin tables get explicit network_id keying.

Spec:  docs/superpowers/specs/2026-07-15-memecoin-network-id-keying-design.md
Plan:  docs/superpowers/plans/2026-07-15-memecoin-network-id-keying.md

A 0x contract address is not a coin identity across EVM chains. These tests pin that the ledger
writer records the chain it already has, the backfill resolves chains unambiguous-only (never
coercing unknown to a default), and the research read stops mislabeling EVM mints as solana.
"""
import importlib.util
import pathlib
import sqlite3
from datetime import datetime, timedelta, timezone

import sniper.persistence.db_memecoin as m


def _mem_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _load_study():
    p = pathlib.Path("scripts/research/memecoin_pattern_study.py")
    spec = importlib.util.spec_from_file_location("mps_under_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- Task 1: quote_ledger schema -------------------------------------------------

def test_quote_ledger_has_network_id_column_and_index():
    c = _mem_db()
    cols = {r[1] for r in c.execute("PRAGMA table_info(memecoin_quote_ledger)")}
    assert "network_id" in cols
    idx = {r[1] for r in c.execute("PRAGMA index_list(memecoin_quote_ledger)")}
    assert "idx_mc_quotes_net_mint_ts" in idx


# --- Task 2: quote_ledger writer preserves the chain -----------------------------

def test_writer_preserves_network_id_for_tuple_keys_and_null_for_bare():
    c = _mem_db()
    # Distinct minutes so the mint-scoped UNIQUE(mint, ts_minute) keeps both chains' rows
    # (documents the residual: the unique constraint stays mint-scoped by design).
    assert m.record_quote_observations(
        c, {("8453", "0xSAME"): {"price": 1.0, "liquidity": 100.0}},
        "2026-07-15T20:00:05+00:00") == 1
    assert m.record_quote_observations(
        c, {("4663", "0xSAME"): {"price": 2.0, "liquidity": 200.0}},
        "2026-07-15T20:01:05+00:00") == 1
    assert m.record_quote_observations(
        c, {"SOLBARE": {"price": 3.0}}, "2026-07-15T20:02:05+00:00") == 1
    rows = {(r["network_id"], r["mint"]): r["price"]
            for r in c.execute("SELECT network_id, mint, price FROM memecoin_quote_ledger")}
    assert rows[("8453", "0xSAME")] == 1.0
    assert rows[("4663", "0xSAME")] == 2.0     # same mint, two chains, both distinguishable
    assert rows[(None, "SOLBARE")] == 3.0      # bare key -> NULL, never coerced to a default chain


def test_writer_records_per_row_source_not_a_single_scalar():
    """The dense quote ledger must record the TRUE per-row source. A DexScreener mark, a
    GeckoTerminal blind-chain mark, and a historical 0x-provenance mark can coexist. Historical
    provenance remains readable after provider retirement. A row without a source keeps the scalar
    fallback."""
    c = _mem_db()
    m.record_quote_observations(c, {
        ("solana", "SOLNATIVE"): {"price": 1.0, "source": "dexscreener"},
        ("4663", "0xGT"): {"price": 2.0, "source": "geckoterminal"},
        ("4663", "0xZX"): {"price": 3.0, "source": "zerox"},
        ("8453", "0xNOSRC"): {"price": 4.0},                     # no source -> scalar fallback
    }, "2026-07-15T20:00:05+00:00")
    rows = {r["mint"]: r["source"] for r in c.execute(
        "SELECT mint, source FROM memecoin_quote_ledger")}
    assert rows["SOLNATIVE"] == "dexscreener"
    assert rows["0xGT"] == "geckoterminal"
    assert rows["0xZX"] == "zerox"
    assert rows["0xNOSRC"] == "dexscreener"                     # default preserved for source-less rows


# --- Task 3: alert_latency + exec_latency ----------------------------------------

def test_alert_and_exec_latency_have_network_id():
    c = _mem_db()
    for t in ("memecoin_alert_latency", "memecoin_exec_latency"):
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
        assert "network_id" in cols, t


def test_record_alert_and_exec_persist_network_id():
    c = _mem_db()
    m.record_alert(c, {"fomo_event_id": "E1", "mint": "0xA", "network_id": "8453"})
    assert c.execute(
        "SELECT network_id FROM memecoin_alert_latency WHERE fomo_event_id='E1'"
    ).fetchone()[0] == "8453"
    m.record_exec_latency(c, mint="0xA", pending_id=1, status="filled", sig="s",
                          timing={}, at="2026-07-15T20:00:00+00:00", network_id="4663")
    assert c.execute(
        "SELECT network_id FROM memecoin_exec_latency WHERE mint='0xA'"
    ).fetchone()[0] == "4663"


def test_record_alert_without_network_id_is_null():
    c = _mem_db()
    m.record_alert(c, {"fomo_event_id": "E2", "mint": "SOL"})
    assert c.execute(
        "SELECT network_id FROM memecoin_alert_latency WHERE fomo_event_id='E2'"
    ).fetchone()[0] is None


# --- Task 4: research read no longer hardcodes solana ----------------------------

def test_legacy_quotes_does_not_mislabel_evm_mint_as_solana():
    c = _mem_db()
    t0 = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)
    m.record_quote_observations(c, {("8453", "0xEVM"): {"price": 1.0}}, t0.isoformat())
    m.record_quote_observations(c, {"SOLNATIVE": {"price": 2.0}}, t0.isoformat())
    mps = _load_study()
    evm = mps._legacy_quotes(c, "0xEVM", t0, t0 + timedelta(hours=1))
    assert evm and evm[0]["network_id"] == "8453"    # stored chain, NOT hardcoded solana
    sol = mps._legacy_quotes(c, "SOLNATIVE", t0, t0 + timedelta(hours=1))
    assert sol and sol[0]["network_id"] == "solana"  # non-0x inferred solana is unambiguous


# --- Task 5: unambiguous-only resolver + batched backfill ------------------------

def test_resolve_ledger_network_unambiguous_only():
    c = _mem_db()
    c.execute("INSERT INTO memecoin_events(fomo_event_id,mint,network_id) VALUES('e1','0xEVM','8453')")
    c.commit()
    assert m.resolve_ledger_network(c, "SOLNATIVE") == "solana"   # non-0x unambiguous
    assert m.resolve_ledger_network(c, "0xEVM") == "8453"         # 0x resolved from feed events
    assert m.resolve_ledger_network(c, "0xGHOST") is None         # 0x, feed silent -> NULL


def test_resolve_ledger_network_conflicting_chains_stays_null():
    c = _mem_db()
    c.execute("INSERT INTO memecoin_events(fomo_event_id,mint,network_id) VALUES('a','0xC','8453')")
    c.execute("INSERT INTO memecoin_events(fomo_event_id,mint,network_id) VALUES('b','0xC','4663')")
    c.commit()
    assert m.resolve_ledger_network(c, "0xC") is None             # ambiguous -> refuse


def test_backfill_quote_ledger_fills_unambiguous_leaves_rest_null():
    c = _mem_db()
    c.execute("INSERT INTO memecoin_events(fomo_event_id,mint,network_id) VALUES('e1','0xEVM','8453')")
    c.commit()
    m.record_quote_observations(c, {"SOLNATIVE": {"price": 1.0}}, "2026-07-15T20:00:05+00:00")
    m.record_quote_observations(c, {"0xEVM": {"price": 1.0}}, "2026-07-15T20:00:05+00:00")
    m.record_quote_observations(c, {"0xGHOST": {"price": 1.0}}, "2026-07-15T20:00:05+00:00")
    stats = m.backfill_quote_ledger_network(c, batch=2)          # batch<rows exercises the watermark
    got = {r[0]: r[1] for r in c.execute("SELECT mint, network_id FROM memecoin_quote_ledger")}
    assert got["SOLNATIVE"] == "solana"
    assert got["0xEVM"] == "8453"
    assert got["0xGHOST"] is None
    assert stats["resolved"] == 2 and stats["left_null"] == 1


def test_backfill_is_idempotent():
    c = _mem_db()
    m.record_quote_observations(c, {"SOLNATIVE": {"price": 1.0}}, "2026-07-15T20:00:05+00:00")
    first = m.backfill_quote_ledger_network(c)
    second = m.backfill_quote_ledger_network(c)
    assert first["resolved"] == 1
    assert second["scanned"] == 0 and second["resolved"] == 0    # nothing left NULL to touch
