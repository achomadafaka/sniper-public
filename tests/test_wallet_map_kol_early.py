"""Wallet Map, Axiom wallets that bought before our sources: counts on slice 3b's own denominator.

Only reads with status 'ok' count (3b's nofill/no_label rule); a wallet is matched by its Solana
address; the roster flag comes from the proven address tables; the baseline is every early trader.
"""
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "research"))
import wallet_map_kol_early as K           # noqa: E402


def _db(sql):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(sql)
    return c


LEDGER = """
CREATE TABLE memecoin_wallets (source_user_id TEXT, handle TEXT);
CREATE TABLE axiom_trader_directory (address TEXT, x_handle TEXT, chain TEXT, followers INTEGER, pnl_24h REAL);
CREATE TABLE memecoin_address_registry (source_user_id TEXT, network_id TEXT, address TEXT);
CREATE TABLE memecoin_wallet_addresses (source_user_id TEXT, network_id TEXT, address TEXT);
INSERT INTO memecoin_wallets VALUES ('u1', 'notdecu'), ('u2', 'cap'), ('u3', 'ours');
INSERT INTO axiom_trader_directory VALUES ('KOL1', 'kolone', 'solana', 10, 1.0), ('KOL2', 'koltwo', 'solana', NULL, NULL),
                                         ('EVM1', 'evm', 'evm', 1, 1.0), ('OURS', 'ourkol', 'solana', 1, 1.0);
INSERT INTO memecoin_address_registry VALUES ('u3', 'solana', 'OURS');
"""
GRAPH = """
CREATE TABLE memecoin_wallet_upstream_reads (signature TEXT, mint TEXT, source_user_id TEXT, status TEXT);
CREATE TABLE memecoin_wallet_early_buys (signature TEXT, trader TEXT, lead_s INTEGER);
INSERT INTO memecoin_wallet_upstream_reads VALUES ('s1', 'm1', 'u1', 'ok'), ('s2', 'm2', 'u1', 'ok'),
    ('s3', 'm3', 'u2', 'ok'), ('s4', 'm4', 'u2', 'our_fill');
INSERT INTO memecoin_wallet_early_buys VALUES ('s1', 'KOL1', 10), ('s2', 'KOL1', 30), ('s3', 'KOL1', 50),
    ('s4', 'KOL2', 5), ('s1', 'X', 1), ('s2', 'X', 2), ('s3', 'Y', 3), ('s1', 'OURS', 4);
"""


def test_counts_use_3b_ok_reads_and_name_the_sources():
    base, rows = K.build(_db(LEDGER), _db(GRAPH), {"KOL1": {"kol": 4}})
    by = {r["address"]: r for r in rows}
    assert set(by) == {"KOL1", "OURS"}            # KOL2's only early buy is on an our_fill read
    k1 = by["KOL1"]
    assert (k1["k_coins"], k1["lead_s_median"], k1["lead_s_min"]) == (3, 30, 10)
    assert k1["sources_preceded"] == {"notdecu": 2, "cap": 1}
    assert k1["axiom_7d_lists"] == {"kol": 4} and k1["on_roster"] is None
    assert by["OURS"]["on_roster"] == "ours"
    assert base == {"reads_ok": 3, "early_traders": 4, "early_traders_k_ge_2": 2, "early_traders_k_ge_3": 1,
                    "axiom_solana_addresses": 3, "axiom_seen_early": 2, "axiom_k_ge_2": 1, "axiom_k_ge_3": 1,
                    "axiom_on_roster": 1}


def test_the_7d_lists_come_from_the_solana_payloads_only(tmp_path):
    raw = tmp_path / "axiom_raw.db"
    c = sqlite3.connect(raw)
    c.executescript("CREATE TABLE axiom_raw_messages (source TEXT, body_sha TEXT, payload TEXT);"
                    "CREATE INDEX idx_axiom_raw_sha ON axiom_raw_messages(source, body_sha);")
    row = lambda a, rank: {"wallets": {"solana": {"address": a}}, "windows": {"7d": {"rank": rank}}}  # noqa: E731
    for src, chain, window, rows in (("kol", "sol", "7d", [row("A", 3)]), ("pump", "sol", "7d", [row("A", 9)]),
                                     ("kol", "sol", "24h", [row("B", 1)]), ("kol", "evm", "7d", [row("C", 1)])):
        c.execute("INSERT INTO axiom_raw_messages VALUES (?, 'sha', ?)",
                  ("https://api8.axiom.trade/smart-money-traders",
                   json.dumps({"source": src, "chain": chain, "window": window, "rows": rows})))
    c.commit()
    assert dict(K.axiom_lists(raw)) == {"A": {"kol": 3, "pump": 9}}


# ------------------------------------------------------------- the pre-registered group test
import random                                   # noqa: E402

DAY = 86400


def _units(n, *, kol, win, p_null=0.3, n_early=None, launch=None, day=None):
    out = []
    for i in range(n):
        out.append({"signature": "s%d" % i, "source_user_id": "u%d" % (i % 3), "t0": 1_790_000_000 + (day(i) if day else i % 5) * DAY,
                    "launch": launch(i) if launch else i % 2, "win": win(i), "p_null": p_null,
                    "n_early": n_early(i) if n_early else i % 9, "read_at": "2026-10-01T00:00:00",
                    "kols": {"K%d" % (i % 4)} if kol(i) else set()})
    return out


def test_a_planted_kol_effect_passes():
    rng = random.Random(1)
    wins = [rng.random() < 0.3 for _ in range(150)]
    units = _units(150, kol=lambda i: i % 3 == 0, win=lambda i: 1 if i % 3 == 0 else int(wins[i]))
    out = K.group_test(units, n_perm=2000)
    assert out["kol_units"] == 50 and out["S"] > 0 and out["p"] <= 0.05
    assert out["loo_S"] > 0 and out["loo_p"] <= 0.10 and out["win_days"] >= 3
    assert out["verdict"] == "pass"


def test_crowding_alone_never_passes():
    """Busy coins win and KOLs sit only on busy coins: within a stratum the label carries nothing."""
    units = _units(150, n_early=lambda i: i % 3 * 10, kol=lambda i: i % 3 == 2 and i % 5 != 0,
                   win=lambda i: 1 if i % 3 == 2 else 0)
    out = K.group_test(units, n_perm=2000)
    assert out["S"] > 0 and out["p"] > 0.05 and out["verdict"].startswith("not shown")


def test_fewer_than_30_kol_units_reports_counts_only():
    out = K.group_test(_units(100, kol=lambda i: i < 20, win=lambda i: 1), n_perm=10)
    assert out["verdict"].startswith("not shown: fewer than 30") and "p" not in out


def test_the_permutation_is_seeded():
    units = _units(90, kol=lambda i: i % 3 == 0, win=lambda i: i % 2)
    assert K.group_test(units, n_perm=500)["p"] == K.group_test(units, n_perm=500)["p"]


def test_the_a1_sensitivity_line_takes_each_reads_a1_label_and_recomputes_the_nulls():
    """Amendment A1b: A1b is the primary; --fx-band a1 relabels a read where A1 decided otherwise."""
    import wallet_upstream as up
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    up.ensure_schema(c)
    alt = json.dumps(dict.fromkeys(up.LABEL_COLS) | {"status": "no_label", "label_reason": "other_quote"})
    rows = [("k%d" % i, i % 2, 120.0, None, None, None) for i in range(12)]          # A1 and A1b agree
    rows += [("moved", 1, 117.6, None, 117.6, alt)]                                   # A1 rejected, A1b kept
    for sig, win, rate, rejected, a1, other in rows:
        c.execute("INSERT INTO memecoin_wallet_upstream_reads (signature, source_user_id, origin, t0, launch, "
                  "status, win, fx_rate, fx_rejected, fx_a1_rejected, fx_alt_label, p_null, method_version, read_at) "
                  "VALUES (?, 'u1', 'detection', 1, 0, 'ok', ?, ?, ?, ?, ?, 0.5, ?, 't')",
                  (sig, win, rate, rejected, a1, other, up.UPSTREAM_METHOD))
    primary = K.test_units(c, set())
    sens = K.test_units(c, set(), fx_band="a1")
    assert "moved" in {u["signature"] for u in primary}
    assert "moved" not in {u["signature"] for u in sens} and len(sens) == 12      # no_label under A1
    assert all(u["p_null"] != 0.5 for u in sens)                                    # recomputed on the A1 view
    assert K.a1b_pending(c) == 0
    # Gamma M1: a row still waiting for its A1b re-read never enters the primary, and the test refuses
    c.execute("INSERT INTO memecoin_wallet_upstream_reads (signature, source_user_id, origin, t0, launch, status, "
              "win, fx_rate, p_null, method_version, read_at) VALUES ('waiting', 'u1', 'detection', 1, 0, 'ok', 1, "
              "NULL, 0.5, ?, 't')", (up.UPSTREAM_METHOD_A1,))
    assert "waiting" not in {u["signature"] for u in K.test_units(c, set())}
    assert K.a1b_pending(c) == 1
