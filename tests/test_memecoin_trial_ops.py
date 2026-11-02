"""sniper/services/memecoin_trial_ops: the one trial implementation behind the script and the Wallet
Map's buttons. A candidate's "Trial" adds it to the roster only after every check passed."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_trial_ops as ops

NOW = "2026-10-01T12:00:00+00:00"
ADDR = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
UID = "pump:" + ADDR


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMECOIN_REAL_TRIAL_CLIP_USD_SOLANA", "20")
    monkeypatch.setattr(ops, "GRAPH_PATH", str(tmp_path / "no-graph.db"))
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, address, network_id, mint, "
              "detected_at, created_at) VALUES ('s1', ?, ?, 'solana', 'M', '2026-09-30T12:00:00+00:00', "
              "'2026-09-30T12:00:00+00:00')", (UID, ADDR))
    c.commit()
    return c


def _wallets(c):
    return c.execute("SELECT COUNT(*) FROM memecoin_wallets").fetchone()[0]


def test_a_candidate_dry_run_writes_nothing_and_says_it_would_add(conn):
    out = ops.start(conn, "solana", uid=UID, handle="cand", confirm=False, now_iso=NOW, add_if_missing=True)
    assert out["ok"] and out["would_add"] and out["clip_usd"] == 20.0 and _wallets(conn) == 0


def test_a_candidate_trial_adds_then_starts_and_arms_as_the_given_actor(conn):
    out = ops.start(conn, "solana", uid=UID, handle="cand", confirm=True, now_iso=NOW, add_if_missing=True,
                    actor="operator:dashboard")
    assert out["ok"] and out["added"] and out["written"]
    assert m.wallet_armed_chains(conn, UID) == ["solana"] and m.open_trial(conn, UID, "solana")
    actor = conn.execute("SELECT actor FROM memecoin_wallet_approvals ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert actor == "operator:dashboard"


def test_a_refused_candidate_is_never_added(conn):
    conn.execute("DELETE FROM onchain_wallet_detections")
    out = ops.start(conn, "solana", uid=UID, handle="cand", confirm=True, now_iso=NOW, add_if_missing=True)
    assert not out["ok"] and "no on-chain buy" in out["reason"] and _wallets(conn) == 0


def test_an_arm_refused_after_the_add_says_the_wallet_stayed_on_the_roster(conn, monkeypatch):
    monkeypatch.setattr(m, "set_wallet_chain_armed", lambda *a, **k: {"ok": False, "reason": "gate"})
    out = ops.start(conn, "solana", uid=UID, handle="cand", confirm=True, now_iso=NOW, add_if_missing=True)
    assert not out["ok"] and out["added"] and _wallets(conn) == 1
    assert out["reason"] == "added to the roster (watch-only); arm refused, trial row deleted: gate"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_trial_arms").fetchone()[0] == 0


@pytest.mark.parametrize("uid, handle, why", [
    ("kyle", "k", "neither a Fomo uuid"), (UID, "", "needs a handle")])
def test_a_bad_new_wallet_is_refused_before_any_write(conn, uid, handle, why):
    out = ops.start(conn, "solana", uid=uid, handle=handle, confirm=True, now_iso=NOW, add_if_missing=True)
    assert not out["ok"] and why in out["reason"] and _wallets(conn) == 0


def test_without_add_a_wallet_off_the_roster_is_refused(conn):
    out = ops.start(conn, "solana", uid=UID, handle="cand", confirm=True, now_iso=NOW)
    assert not out["ok"] and out["reason"] == "not on the roster"


def test_a_lost_start_race_refuses_and_releases_the_write_lock(tmp_path, monkeypatch):
    """Gamma 2026-10-02: two starts past both checks; the second hit the partial UNIQUE index, the page
    read 'refused: HTTP 500', and the failed INSERT's implicit transaction kept the ledger's write lock,
    so the runner's writes waited on a web thread that might never commit."""
    monkeypatch.setenv("MEMECOIN_REAL_TRIAL_CLIP_USD_SOLANA", "20")
    monkeypatch.setattr(ops, "GRAPH_PATH", str(tmp_path / "no-graph.db"))
    path = str(tmp_path / "ledger.db")
    a = sqlite3.connect(path)
    a.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(a)
    a.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, address, network_id, mint, "
              "detected_at, created_at) VALUES ('s1', ?, ?, 'solana', 'M', ?, ?)", (UID, ADDR, NOW, NOW))
    a.commit()
    assert ops.start(a, "solana", uid=UID, handle="cand", confirm=True, now_iso=NOW, add_if_missing=True)["ok"]
    b = sqlite3.connect(path)
    b.row_factory = sqlite3.Row
    monkeypatch.setattr(m, "open_trial", lambda *x, **k: None)          # both starts read before either wrote
    monkeypatch.setattr(m, "wallet_armed_chains", lambda *x, **k: [])
    arms = []
    monkeypatch.setattr(m, "set_wallet_chain_armed", lambda *x, **k: arms.append(x) or {"ok": True})
    out = ops.start(b, "solana", uid=UID, handle="cand", confirm=True, now_iso=NOW)
    assert not out["ok"] and out["reason"] == "already has an open trial on solana" and arms == []
    assert not b.in_transaction
    c = sqlite3.connect(path, timeout=1)                                  # a third writer is not locked out
    c.execute("INSERT INTO memecoin_trial_verdicts (trial_id, checkpoint, checked_at, closes, verdict, detail_json) "
              "VALUES (99, '5', ?, 0, 'CONTINUE', '{}')", (NOW,))
    c.commit()
    assert a.execute("SELECT COUNT(*) FROM memecoin_trial_arms").fetchone()[0] == 1


def _stopped(conn, verdict, mean_r=-0.32, closes=5):
    """A started trial the grader stopped (verdict row first, as the grader writes it)."""
    ops.start(conn, "solana", uid=UID, handle="cand", confirm=True, now_iso=NOW, add_if_missing=True)
    t = m.open_trial(conn, UID, "solana")
    if verdict:
        conn.execute("INSERT INTO memecoin_trial_verdicts (trial_id, checkpoint, checked_at, closes, mean_r, "
                     "verdict, action, detail_json) VALUES (?, '5', ?, ?, ?, ?, 'stopped', '{}')",
                     (t["id"], NOW, closes, mean_r, verdict))
    m.stop_trial(conn, t["id"], now_iso=NOW)
    return t


@pytest.mark.parametrize("verdict, label", [("DEMOTE", "DEMOTE"), ("SHADOW", "SHADOW"), (None, "STOPPED")])
def test_keep_armed_on_a_grader_stopped_trial_needs_the_confirm_to_name_the_stop(conn, verdict, label):
    """Gamma L1 on 8ae21ddfd: Keep armed on a stopped trial ended it at the FULL clip without saying it
    was stopped. Rule 3a0b141ee makes every verdict a recommendation to the operator, so the override
    is allowed, but only when the request names the stop it overrides."""
    _stopped(conn, verdict)
    for ack in (None, "PROMOTE"):
        out = ops.end(conn, "solana", uid=UID, keep_armed=True, disarm=False, confirm=True, now_iso=NOW,
                      ack_stop=ack)
        assert not out["ok"] and label in out["reason"] and out["stop"]["verdict"] == label
        assert m.open_trial(conn, UID, "solana") is not None
    dry = ops.end(conn, "solana", uid=UID, keep_armed=True, disarm=False, confirm=False, now_iso=NOW)
    assert not dry["ok"] and label in dry["reason"]                    # the dry run says it too
    out = ops.end(conn, "solana", uid=UID, keep_armed=True, disarm=False, confirm=True, now_iso=NOW,
                  ack_stop=label)
    assert out["ok"] and m.open_trial(conn, UID, "solana") is None and m.wallet_armed_chains(conn, UID) == ["solana"]


def test_a_stopped_trial_disarms_and_a_promote_stop_keeps_armed_without_an_ack(conn):
    _stopped(conn, "DEMOTE")
    assert ops.end(conn, "solana", uid=UID, keep_armed=False, disarm=True, confirm=True, now_iso=NOW)["ok"]
    conn.execute("DELETE FROM memecoin_trial_arms")
    conn.execute("DELETE FROM memecoin_trial_verdicts")
    _stopped(conn, "PROMOTE", mean_r=0.12, closes=10)
    out = ops.end(conn, "solana", uid=UID, keep_armed=True, disarm=False, confirm=True, now_iso=NOW)
    assert out["ok"] and out["stop"]["verdict"] == "PROMOTE"


def test_two_of_three_start_freezes_its_rule_and_fifty_dollar_cap(conn, monkeypatch):
    monkeypatch.setenv("MEMECOIN_REAL_TRIAL_CLIP_USD_SOLANA", "175")
    out = ops.start(conn, "solana", uid=UID, handle="cand", confirm=True, now_iso=NOW,
                    add_if_missing=True, rule="tp-two-of-three-v1", actor="operator:codex")
    assert out["ok"] and out["clip_usd"] == 50.0
    trial = m.open_trial(conn, UID, "solana")
    assert trial["rule"] == "tp-two-of-three-v1" and trial["clip_usd"] == 50.0
    audit = conn.execute("SELECT reason FROM memecoin_wallet_approvals ORDER BY id DESC LIMIT 1").fetchone()
    assert "tp-two-of-three-v1" in audit[0]


def test_two_of_three_dry_run_and_unknown_rule_never_write(conn):
    out = ops.start(conn, "solana", uid=UID, handle="cand", confirm=False, now_iso=NOW,
                    add_if_missing=True, rule="tp-two-of-three-v1")
    assert out["ok"] and out["clip_usd"] == 50.0 and _wallets(conn) == 0
    bad = ops.start(conn, "solana", uid=UID, handle="cand", confirm=True, now_iso=NOW,
                    add_if_missing=True, rule="typo")
    assert not bad["ok"] and "rule" in bad["reason"] and _wallets(conn) == 0

