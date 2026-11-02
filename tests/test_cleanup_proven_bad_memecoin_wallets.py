import json
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from sniper.persistence import db_memecoin as m


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "cleanup_proven_bad_memecoin_wallets.py"


def _db(path, wallets):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    for uid, handle, pnl, real, paused in wallets:
        conn.execute(
            "INSERT INTO memecoin_wallets(source_user_id,handle,real_approved,paused,excluded) "
            "VALUES(?,?,?,?,0)", (uid, handle, int(real), int(paused)))
        for i in range(5):
            position_id = conn.execute(
                "INSERT INTO memecoin_positions(source_user_id,source_handle,mint,strategy,is_open,"
                "execution_mode,realized_pnl,opened_at,closed_at) VALUES(?,?,?,'alpha_b',0,'paper',?,?,?)",
                (uid, handle, f"{uid}-mint-{i}", float(pnl) / 5.0,
                 "2026-07-01T00:00:00+00:00", "2026-07-01T01:00:00+00:00")).lastrowid
            conn.execute(
                "INSERT INTO memecoin_fills(position_id,kind,qty,fill_source,created_at) "
                "VALUES(?,'entry',1,'route_quote','2026-07-01T00:00:00+00:00')",
                (position_id,),
            )
            conn.execute(
                "INSERT INTO memecoin_fills(position_id,kind,qty,fill_source,created_at) "
                "VALUES(?,'bracket_exit',1,'route_quote','2026-07-01T01:00:00+00:00')",
                (position_id,),
            )
    conn.commit()
    return conn


def _run(db_path, *args):
    env = os.environ.copy()
    env.update({
        "MEMECOIN_REAL_PROMOTE_MIN_CLOSES": "5",
        "MEMECOIN_REAL_PROMOTE_MIN_PNL_USD": "25",
    })
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db_path), *map(str, args)],
        cwd=ROOT, env=env, text=True, capture_output=True)


def _wallet(conn, uid):
    return dict(conn.execute(
        "SELECT source_user_id,paused,excluded,real_approved FROM memecoin_wallets "
        "WHERE source_user_id=?", (uid,)).fetchone())


def _digest(*uids):
    raw = json.dumps(list(uids), separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def test_dry_run_uses_honest_metric_and_never_mutates(tmp_path):
    path = tmp_path / "ledger.db"
    conn = _db(path, [
        ("bad", "bad-watch", -30.0, False, False),
        ("good", "good-watch", 30.0, False, False),
        ("fomo-bad", "fomo-bad", -100.0, False, False),
        ("real-bad", "real-bad", -30.0, True, False),
        ("paused-bad", "paused-bad", -30.0, False, True),
    ])
    fomo_position = conn.execute(
        "SELECT id FROM memecoin_positions WHERE source_user_id='fomo-bad' ORDER BY id LIMIT 1"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO memecoin_fills(position_id,kind,fill_source,created_at) "
        "VALUES(?,'entry','fomo','2026-07-01T00:00:00+00:00')", (fomo_position,))
    conn.commit()

    result = _run(path)

    assert result.returncode == 0, result.stderr
    assert "bad-watch" in result.stdout
    assert "good-watch" not in result.stdout
    assert "fomo-bad" not in result.stdout
    assert "real-bad" not in result.stdout
    assert "paused-bad" not in result.stdout
    assert "TOTAL: 1" in result.stdout
    assert f"TARGET-DIGEST: {_digest('bad')}" in result.stdout
    assert _wallet(conn, "bad")["excluded"] == 0
    assert not list(tmp_path.glob("ledger.db.bak-provenbad-*"))


def test_missing_db_path_fails_without_creating_file(tmp_path):
    path = tmp_path / "missing.db"

    result = _run(path)

    assert result.returncode != 0
    assert "database does not exist" in result.stderr
    assert not path.exists()


def test_apply_requires_exact_expected_count_before_backup(tmp_path):
    path = tmp_path / "ledger.db"
    conn = _db(path, [("bad", "bad-watch", -30.0, False, False)])

    missing = _run(path, "--apply")
    missing_digest = _run(path, "--apply", "--expect-count", "1")
    mismatch = _run(
        path, "--apply", "--expect-count", "1", "--expect-digest", "0" * 64)

    assert missing.returncode != 0
    assert missing_digest.returncode != 0
    assert mismatch.returncode != 0
    assert "--apply requires --expect-count" in missing.stderr
    assert "--apply requires --expect-digest" in missing_digest.stderr
    assert "target digest mismatch" in mismatch.stderr
    assert _wallet(conn, "bad")["excluded"] == 0
    assert not list(tmp_path.glob("ledger.db.bak-provenbad-*"))


def test_exact_apply_backs_up_manifests_and_excludes_atomically(tmp_path):
    path = tmp_path / "ledger.db"
    conn = _db(path, [
        ("bad", "bad-watch", -30.0, False, False),
        ("real-bad", "real-bad", -40.0, True, False),
    ])
    conn.execute("UPDATE memecoin_wallets SET real_chains='solana' WHERE source_user_id='bad'")
    conn.commit()

    result = _run(
        path, "--apply", "--expect-count", "1", "--expect-digest", _digest("bad"))

    assert result.returncode == 0, result.stderr
    assert _wallet(conn, "bad") == {
        "source_user_id": "bad", "paused": 1, "excluded": 1, "real_approved": 0}
    assert conn.execute(
        "SELECT real_chains FROM memecoin_wallets WHERE source_user_id='bad'"
    ).fetchone()[0] is None
    assert _wallet(conn, "real-bad")["excluded"] == 0
    backups = list(tmp_path.glob("ledger.db.bak-provenbad-*.db"))
    manifests = list(tmp_path.glob("ledger.db.bak-provenbad-*.json"))
    assert len(backups) == len(manifests) == 1
    backup = sqlite3.connect(backups[0])
    assert backup.execute(
        "SELECT excluded FROM memecoin_wallets WHERE source_user_id='bad'").fetchone()[0] == 0
    manifest = json.loads(manifests[0].read_text())
    assert manifest["status"] == "applied"
    assert manifest["target_digest"] == _digest("bad")
    assert manifest["targets"] == [{
        "source_user_id": "bad", "handle": "bad-watch", "n_closes": 5, "pnl": -30.0}]


def test_apply_failure_rolls_back_every_target(tmp_path):
    path = tmp_path / "ledger.db"
    conn = _db(path, [
        ("bad1", "bad-one", -30.0, False, False),
        ("bad2", "bad-two", -40.0, False, False),
    ])
    conn.execute(
        "CREATE TRIGGER reject_bad2 BEFORE UPDATE OF excluded ON memecoin_wallets "
        "WHEN NEW.source_user_id='bad2' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    conn.commit()

    result = _run(
        path, "--apply", "--expect-count", "2", "--expect-digest", _digest("bad1", "bad2"))

    assert result.returncode != 0
    assert _wallet(conn, "bad1")["excluded"] == 0
    assert _wallet(conn, "bad2")["excluded"] == 0
    manifests = list(tmp_path.glob("ledger.db.bak-provenbad-*.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text())["status"] == "planned"


# --- reinstate mode: repairing one sweep that used the wrong metric ---

SWEEP = "cleanup-2026-07-27"


def _exclude(conn, uid, *, actor=SWEEP, blended_pnl=-900.0):
    """Mark a wallet as swept: excluded+paused, with an audit row citing the blended number."""
    conn.execute("UPDATE memecoin_wallets SET excluded=1,paused=1 WHERE source_user_id=?", (uid,))
    if actor:
        conn.execute(
            "INSERT INTO memecoin_wallet_approvals(source_user_id,action,actor,reason,copy_pnl,"
            "copy_closes,forced,created_at) VALUES(?,'revoke',?,?,?,?,0,?)",
            (uid, actor, "honest alpha_b closed PnL<0 (churn cleanup)", blended_pnl, 40,
             "2026-07-27T04:16:00+00:00"))
    conn.commit()


def test_reinstate_only_frees_wallets_the_honest_metric_does_not_convict(tmp_path):
    path = tmp_path / "ledger.db"
    conn = _db(path, [
        ("mild", "mild-loser", -10.0, False, False),      # honest -10 -> NOT proven bad
        ("proven", "proven-loser", -60.0, False, False),  # honest -60 -> proven bad
        ("other", "other-sweep", -10.0, False, False),    # excluded by someone else
    ])
    for uid in ("mild", "proven"):
        _exclude(conn, uid)
    _exclude(conn, "other", actor="operator-2026-07-01")

    dry = _run(path, "--reinstate-actor", SWEEP)
    assert dry.returncode == 0, dry.stderr
    assert "REINSTATE" in dry.stdout and "TOTAL: 1" in dry.stdout
    assert _wallet(conn, "mild")["excluded"] == 1                     # dry run mutates nothing

    result = _run(path, "--reinstate-actor", SWEEP, "--apply",
                  "--expect-count", "1", "--expect-digest", _digest("mild"))
    assert result.returncode == 0, result.stderr
    assert _wallet(conn, "mild") == {
        "source_user_id": "mild", "paused": 0, "excluded": 0, "real_approved": 0}
    assert _wallet(conn, "proven")["excluded"] == 1                   # honest metric still convicts
    assert _wallet(conn, "other")["excluded"] == 1                    # permanence outside the sweep

    audit = conn.execute(
        "SELECT action,actor,copy_pnl,copy_closes FROM memecoin_wallet_approvals "
        "WHERE source_user_id='mild' AND action='reinstate'").fetchone()
    assert audit["actor"] == f"reinstate-{SWEEP}"
    assert (audit["copy_pnl"], audit["copy_closes"]) == (-10.0, 5)    # HONEST snapshot, not blended
    manifests = list(tmp_path.glob("ledger.db.bak-reinstate-*.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text())
    assert manifest["mode"] == "reinstate" and manifest["reinstate_actor"] == SWEEP


def test_reinstate_never_grants_real_approval(tmp_path):
    path = tmp_path / "ledger.db"
    conn = _db(path, [("mild", "mild-loser", -10.0, False, False)])
    _exclude(conn, "mild")
    conn.execute("UPDATE memecoin_wallets SET real_chains='4663' WHERE source_user_id='mild'")
    conn.commit()

    result = _run(path, "--reinstate-actor", SWEEP, "--apply",
                  "--expect-count", "1", "--expect-digest", _digest("mild"))

    assert result.returncode == 0, result.stderr
    assert _wallet(conn, "mild")["real_approved"] == 0
    assert conn.execute(
        "SELECT real_chains FROM memecoin_wallets WHERE source_user_id='mild'"
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_wallet_approvals WHERE source_user_id='mild' "
        "AND action='approve'").fetchone()[0] == 0
