"""scripts/memecoin_trial_arm.py: the operator starts and ends trials (spec 5.1, 5.6). Dry run by
default; --confirm writes. Driven through its own functions on the production schema."""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_trial_ops as ops

SPEC = importlib.util.spec_from_file_location(
    "memecoin_trial_arm_script",
    Path(__file__).resolve().parents[1] / "scripts" / "memecoin_trial_arm.py")
S = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S)
NOW = "2026-10-01T12:00:00+00:00"


ADDR = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
UID = "pump:" + ADDR                     # a pump.fun id names its own wallet: the slot lane watches it


def _detect(c, *, uid=UID, address=ADDR, network="solana", at="2026-09-30T12:00:00+00:00", sig="s1"):
    c.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, address, network_id, mint, "
              "detected_at, created_at) VALUES (?,?,?,?,?,?,?)", (sig, uid, address, network, "M", at, at))


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMECOIN_REAL_TRIAL_CLIP_USD_SOLANA", "20")
    monkeypatch.setattr(ops, "GRAPH_PATH", str(tmp_path / "no-graph.db"))   # no Wallet Map db here
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, UID, "cand")
    _detect(c)                                   # our own on-chain detection, 1 day before NOW
    c.commit()
    return c


def test_start_is_a_dry_run_unless_confirmed(conn):
    out = S.start(conn, "cand", "solana", confirm=False, now_iso=NOW)
    assert out["ok"] and out["written"] is False and out["clip_usd"] == 20.0
    assert m.open_trial(conn, UID, "solana") is None and m.wallet_armed_chains(conn, UID) == []


def test_start_confirmed_writes_the_trial_then_arms_the_chain(conn):
    out = S.start(conn, "cand", "solana", confirm=True, now_iso=NOW)
    trial = m.open_trial(conn, UID, "solana")
    assert out["ok"] and trial["clip_usd"] == 20.0 and trial["rule"] == "3a0b141ee"
    assert m.wallet_armed_chains(conn, UID) == ["solana"]
    reason = conn.execute("SELECT reason FROM memecoin_wallet_approvals ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "trial 3a0b141ee #%d" % trial["id"] in reason


@pytest.mark.parametrize("case,expect", [
    ("no_clip", "no trial clip"), ("excluded", "excluded"), ("open", "already has open trial"),
    ("armed", "already armed"), ("no_onchain_buys", "no on-chain buy on solana in 7 days")])
def test_start_refuses(conn, monkeypatch, case, expect):
    if case == "no_clip":
        monkeypatch.delenv("MEMECOIN_REAL_TRIAL_CLIP_USD_SOLANA")
    elif case == "excluded":
        conn.execute("UPDATE memecoin_wallets SET excluded=1 WHERE source_user_id=?", (UID,))
    elif case == "open":
        S.start(conn, "cand", "solana", confirm=True, now_iso=NOW)
    elif case == "armed":
        m.set_wallet_chain_armed(conn, UID, "solana", True, force=True)
    elif case == "no_onchain_buys":
        conn.execute("DELETE FROM onchain_wallet_detections")
    out = S.start(conn, "cand", "solana", confirm=True, now_iso=NOW)
    assert not out["ok"] and expect in out["reason"], out


def test_a_refused_arm_deletes_the_trial_row(conn, monkeypatch):
    monkeypatch.setattr(m, "set_wallet_chain_armed", lambda *a, **k: {"ok": False, "reason": "gate"})
    out = S.start(conn, "cand", "solana", confirm=True, now_iso=NOW)
    assert not out["ok"] and "gate" in out["reason"]
    assert conn.execute("SELECT COUNT(*) FROM memecoin_trial_arms").fetchone()[0] == 0


def test_end_needs_exactly_one_of_keep_armed_or_disarm(conn):
    S.start(conn, "cand", "solana", confirm=True, now_iso=NOW)
    for keep, disarm in ((False, False), (True, True)):
        out = S.end(conn, "cand", "solana", keep_armed=keep, disarm=disarm, confirm=True, now_iso=NOW)
        assert not out["ok"] and "exactly one" in out["reason"]
    assert m.open_trial(conn, UID, "solana") is not None


@pytest.mark.parametrize("disarm,chains", [(True, []), (False, ["solana"])])
def test_end_confirmed(conn, disarm, chains):
    S.start(conn, "cand", "solana", confirm=True, now_iso=NOW)
    assert S.end(conn, "cand", "solana", keep_armed=not disarm, disarm=disarm, confirm=False,
                 now_iso=NOW)["written"] is False
    out = S.end(conn, "cand", "solana", keep_armed=not disarm, disarm=disarm, confirm=True, now_iso=NOW)
    assert out["ok"] and m.open_trial(conn, UID, "solana") is None
    assert m.wallet_armed_chains(conn, UID) == chains


def test_the_refusal_names_the_env_var_the_code_reads(conn, monkeypatch):
    monkeypatch.delenv("MEMECOIN_REAL_TRIAL_CLIP_USD_SOLANA")
    out = S.start(conn, "cand", "solana", confirm=False, now_iso=NOW)
    assert "MEMECOIN_REAL_TRIAL_CLIP_USD_SOLANA" in out["reason"]


def test_a_ledger_without_the_trial_tables_refuses_with_a_reason():
    """Before the runner restarts on the trial release, the tables do not exist yet."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE memecoin_wallets (source_user_id TEXT, handle TEXT, excluded INTEGER, real_chains TEXT)")
    for fn in (lambda: S.start(c, "cand", "solana", confirm=False, now_iso=NOW),
               lambda: S.end(c, "cand", "solana", keep_armed=True, disarm=False, confirm=False, now_iso=NOW)):
        out = fn()
        assert not out["ok"] and "trial tables" in out["reason"]
    rows, reason = S.status(c)                    # never an empty list that reads as "no trials"
    assert rows == [] and "trial tables" in reason


# ---- operator 2026-10-02 (via Alpha): J7 is out of the wallet path. A trial starts on OUR OWN
# on-chain evidence of recent buys, never on J7 deliveries (memecoin_events) ----

def _graph(path, rows=(), slots=()):
    g = sqlite3.connect(path)
    g.executescript("CREATE TABLE memecoin_wallet_early_buys (signature TEXT, trader TEXT, lead_s INTEGER);"
                    "CREATE TABLE memecoin_wallet_upstream_reads (signature TEXT PRIMARY KEY, status TEXT, t0 INTEGER);"
                    "CREATE TABLE memecoin_wallet_slot_reads (signature TEXT PRIMARY KEY, status TEXT, "
                    "detected_at TEXT, companions TEXT);")
    for sig, trader, lead, t0 in rows:
        g.execute("INSERT INTO memecoin_wallet_early_buys VALUES (?,?,?)", (sig, trader, lead))
        g.execute("INSERT INTO memecoin_wallet_upstream_reads VALUES (?,?,?)", (sig, "ok", t0))
    for sig, at, traders in slots:
        g.execute("INSERT INTO memecoin_wallet_slot_reads VALUES (?,?,?,?)",
                  (sig, "ok", at, __import__("json").dumps([{"trader": t, "index": 1} for t in traders])))
    g.commit()
    g.close()


def test_a_j7_delivery_alone_no_longer_starts_a_trial(conn):
    conn.execute("DELETE FROM onchain_wallet_detections")
    conn.execute("INSERT INTO memecoin_events (fomo_event_id, source_user_id, mint, side, created_at, "
                 "network_id) VALUES ('e1', ?, 'M', 'buy', '2026-09-30T12:00:00.000Z', 'solana')", (UID,))
    out = S.start(conn, "cand", "solana", confirm=False, now_iso=NOW)
    assert not out["ok"] and "no on-chain buy on solana in 7 days" in out["reason"]


def test_a_candidate_found_on_our_own_tape_can_start(conn, monkeypatch, tmp_path):
    from datetime import datetime
    conn.execute("DELETE FROM onchain_wallet_detections")
    t0 = int(datetime.fromisoformat(NOW).timestamp()) - 2 * 86400
    path = str(tmp_path / "graph.db")
    _graph(path, rows=[("sig1", ADDR, 12, t0)])                 # it bought 12 s before a best wallet, 2 d ago
    monkeypatch.setattr(ops, "GRAPH_PATH", path)
    out = S.start(conn, "cand", "solana", confirm=False, now_iso=NOW)
    assert out["ok"] and out["tape_buys_7d"] == 1 and out["detections_7d"] == 0, out
    _graph(str(tmp_path / "g2.db"), slots=[("s9", "2026-09-29T00:00:00+00:00", ["other", ADDR])])
    monkeypatch.setattr(ops, "GRAPH_PATH", str(tmp_path / "g2.db"))   # a buy in the same slot as one of our sources
    assert S.start(conn, "cand", "solana", confirm=False, now_iso=NOW)["tape_buys_7d"] == 1


def test_old_tape_evidence_still_refuses(conn, monkeypatch, tmp_path):
    from datetime import datetime
    conn.execute("DELETE FROM onchain_wallet_detections")
    _detect(conn, at="2026-09-20T12:00:00+00:00", sig="old")         # 11 days before NOW
    t0 = int(datetime.fromisoformat(NOW).timestamp()) - 9 * 86400
    _graph(str(tmp_path / "graph.db"), rows=[("sig1", ADDR, 12, t0)],
           slots=[("s9", "2026-09-20T00:00:00+00:00", [ADDR])])
    monkeypatch.setattr(ops, "GRAPH_PATH", str(tmp_path / "graph.db"))
    out = S.start(conn, "cand", "solana", confirm=False, now_iso=NOW)
    assert not out["ok"] and "no on-chain buy on solana in 7 days" in out["reason"]
    assert out["detections_7d"] == 0 and out["tape_buys_7d"] == 0


def test_a_wallet_our_detection_cannot_see_is_refused_by_name(conn):
    m.add_wallet(conn, "87f77c24-44e0-5696-baaf-dfe732083954", "fomo_only")    # a Fomo uuid, no address
    out = S.start(conn, "fomo_only", "solana", confirm=False, now_iso=NOW)
    assert not out["ok"] and "no proven solana address" in out["reason"] and out["addresses"] == 0


def test_an_evm_wallet_starts_on_a_detection_at_its_proven_address(monkeypatch):
    monkeypatch.setenv("MEMECOIN_REAL_TRIAL_CLIP_USD_4663", "12")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    uid, addr = "87f77c24-44e0-5696-baaf-dfe732083954", "0x" + "ab" * 20
    m.add_wallet(c, uid, "evmcand")
    monkeypatch.setattr(ops, "evm_addresses", lambda conn, u, chain: {addr} if u == uid and chain == "4663" else set())
    assert not S.start(c, "evmcand", "4663", confirm=False, now_iso=NOW)["ok"]       # no detection yet
    _detect(c, uid=uid, address=addr, network="4663")
    out = S.start(c, "evmcand", "4663", confirm=False, now_iso=NOW)
    assert out["ok"] and out["detections_7d"] == 1 and out["tape_buys_7d"] is None, out


def test_a_refuted_pump_address_is_no_proven_address(conn):
    m.record_address_evidence(conn, source_user_id=UID, network_id="solana", address=ADDR, status="refuted",
                              evidence_kind="test", evidence_json="{}", handle="cand",
                              now_iso=NOW, actor="test")
    out = S.start(conn, "cand", "solana", confirm=False, now_iso=NOW)
    assert not out["ok"] and "no proven solana address" in out["reason"]
