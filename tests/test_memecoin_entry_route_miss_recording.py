"""Entry-quote-ladder misses leave evidence behind (2026-09-06).

PUNK (DegenCapitalLLC, mint 0x3bcbd768...) aborted `no_route_all_venues`. Its classification had
already resolved the real pool (fee=0, hook=0xe5e702..., cached 33 minutes before the buy) as
`liquidity_class: deep`, and the retry lane fired repeatedly over the next 36+ seconds before
giving up -- but `entry_quote_probe_fn` (evm_real_seams.py) classified the miss and then threw the
per-rung ladder trace away, keeping only the one-word code. There was nothing left to say whether
the real pool was even tried, or why it came back empty. Same defect class HOOD named in 2026-08:
"NOTHING recorded whether those misses were the token, the route or the provider."

This does not fix PUNK's specific miss -- there is no evidence left to diagnose it from, which is
exactly the problem. It makes the NEXT one answerable: every miss (a classified no-route, a
route-capacity refusal, or a raised provider exception) now writes one row to
memecoin_entry_route_misses with the full rung ladder, so a repeat can be told apart as "the real
pool was blind" vs "the real pool was tried and came back empty" vs "a different rung entirely".
"""
import json
import sqlite3

import pytest
from eth_account import Account

from sniper.execution import evm_real_seams as seams
from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg

MINT = "0x18e674231a58c239dc7daedcffe15ec3a24cff5c"


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "route_miss_test.db")
    conn = sqlite3.connect(db_path)
    m.ensure_memecoin_tables(conn)
    conn.close()
    monkeypatch.setenv("DB_PATH", db_path)
    return db_path


def _no_route_executor(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False)
    return seams.build_real_evm_executor(
        acct=Account.from_key("0x" + "42" * 32), chain_id="4663",
        rpc_url="http://rpc", venue_quote_fn=lambda *_a, **_k: None,
        price_fn=lambda: 2500.0, usdc_fn=lambda: 100.0,
        token_fn=lambda _mint: (0.0, 18),
    )


def _rows(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM memecoin_entry_route_misses ORDER BY id")]
    c.close()
    return rows


def test_a_no_route_miss_persists_the_full_rung_ladder(monkeypatch, isolated_db):
    ex = _no_route_executor(monkeypatch)

    result = ex["entry_quote_probe_fn"](MINT, 31.49)

    assert result == "no_route"
    rows = _rows(isolated_db)
    assert len(rows) == 1
    assert rows[0]["mint"] == MINT
    assert rows[0]["network_id"] == "4663"
    assert rows[0]["code"] == "no_route"
    rungs = json.loads(rows[0]["rungs_json"])
    assert rungs, "the per-rung ladder trace must not be empty on a no-route miss"
    assert {r["rung"] for r in rungs} >= {"v3"}


def test_a_hit_records_nothing(monkeypatch, isolated_db):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False)
    # Not what this test is about: a fake {"buyAmount": "777"} quote has no real sellAmount, so
    # the measured-ladder round-trip check (evm_v4_multihop.round_trip_within_budget) reads it as
    # a ~-100% round trip and refuses it as unmeasured/over-budget -- same as
    # test_memecoin_evm_quote_miss_attribution.py:864 disables it for the same reason.
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", False, raising=False)
    ex = seams.build_real_evm_executor(
        acct=Account.from_key("0x" + "42" * 32), chain_id="4663",
        rpc_url="http://rpc",
        venue_quote_fn=lambda *_a, **_k: {"buyAmount": "777"},
        price_fn=lambda: 2500.0, usdc_fn=lambda: 100.0,
        token_fn=lambda _mint: (0.0, 18),
    )

    result = ex["entry_quote_probe_fn"](MINT, 31.49)

    assert result is None
    assert _rows(isolated_db) == []


def test_a_raised_provider_exception_also_leaves_a_row(monkeypatch, isolated_db):
    def _raises(*_a, **_k):
        raise RuntimeError("provider transport blew up")

    ex = seams.build_real_evm_executor(
        acct=Account.from_key("0x" + "42" * 32), chain_id="4663",
        rpc_url="http://rpc", venue_quote_fn=_raises,
        price_fn=lambda: 2500.0, usdc_fn=lambda: 100.0,
        token_fn=lambda _mint: (0.0, 18),
    )
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False)

    result = ex["entry_quote_probe_fn"](MINT, 31.49)

    assert result == "provider_rpc_transport"
    rows = _rows(isolated_db)
    assert len(rows) == 1
    assert rows[0]["code"] == "provider_rpc_transport"


def test_two_retries_on_the_same_mint_leave_two_rows_not_one(monkeypatch, isolated_db):
    """Append-only, one row per attempt: the pattern ACROSS attempts (same rung blind every time
    vs. a different one each time) is itself evidence, so a repeat must not collapse into a
    single upserted row."""
    ex = _no_route_executor(monkeypatch)

    ex["entry_quote_probe_fn"](MINT, 31.49)
    ex["entry_quote_probe_fn"](MINT, 31.49)

    assert len(_rows(isolated_db)) == 2


def test_a_write_failure_never_raises_out_of_the_quote_probe(monkeypatch, isolated_db):
    """The write is diagnostic, not load-bearing. A DB that cannot take it must not turn a
    classified miss into an unhandled exception on the trading path."""
    import sniper.persistence.db_core as db_core

    def _boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db_core, "connection_scope", _boom)
    ex = _no_route_executor(monkeypatch)

    result = ex["entry_quote_probe_fn"](MINT, 31.49)

    assert result == "no_route"


def test_a_chain_may_take_its_own_round_trip_budget(monkeypatch):
    """PONZI (7ae4df5b, 2026-09-13): -14.2% on 4663 against the shared 10% budget. Funded 4663
    closes at -10..-15% made +$157 over 13 copied trades, so 4663 takes 0.6 x TP = 15%."""
    from sniper.execution.evm_v4_multihop import round_trip_within_budget
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_TP_FRAC", 0.4)
    monkeypatch.setenv("MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_TP_FRAC_4663", "0.6")
    assert round_trip_within_budget(-14.2167, 25.0, chain="4663")
    assert not round_trip_within_budget(-15.5, 25.0, chain="4663")
    assert not round_trip_within_budget(-14.2167, 25.0, chain="56"), "other chains keep 10%"
    assert not round_trip_within_budget(-14.2167, 25.0), "no chain = the shared budget"
    monkeypatch.setenv("MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_TP_FRAC_4663", "junk")
    assert not round_trip_within_budget(-14.2167, 25.0, chain="4663"), "unparseable = no override"
