"""Tests for the selection CLI core (build_proposals / apply_proposals) + holdout freeze."""
import importlib
import os

from sniper.hyperlive import intel_store as IS


def _mod():
    return importlib.import_module("scripts.hl_copy_select")


def _db(tmp_path):
    c = IS.connect(os.path.join(str(tmp_path), "hl.sqlite"))
    IS.ensure_schema(c)
    return c


def test_build_proposals_only_targets_actionable(tmp_path):
    mod = _mod()
    wp = {
        "0xgood": [40, 50, 45, 60, 42, 55, 48, 52],     # promote
        "0xbad": [-200, -300, -250, -400, -180],         # cull
        "0xnottarget": [40, 50, 45, 60, 42, 55, 48, 52],  # promote-worthy but not a target
    }
    ranked, proposals = mod.build_proposals(wp, target_wallets={"0xgood", "0xbad"})
    by = {p["wallet"]: p["action"] for p in proposals}
    assert by == {"0xgood": "promote", "0xbad": "cull"}   # 0xnottarget excluded
    assert len(ranked) == 3


def test_apply_proposals_mutates_targets(tmp_path):
    mod = _mod()
    c = _db(tmp_path)
    IS.set_copy_target(c, "0xgood", active=True, copy_tier="watch_fallback", ts_ms=1)
    IS.set_copy_target(c, "0xbad", active=True, copy_tier="watch_fallback", ts_ms=1)
    proposals = [{"wallet": "0xgood", "action": "promote", "median": 48.0, "n": 8},
                 {"wallet": "0xbad", "action": "cull", "median": -250.0, "n": 5}]
    mod.apply_proposals(c, proposals, ts_ms=9999)
    good = c.execute("SELECT copy_tier, active FROM hl_copy_targets WHERE wallet='0xgood'").fetchone()
    bad = c.execute("SELECT copy_tier, active FROM hl_copy_targets WHERE wallet='0xbad'").fetchone()
    assert good["copy_tier"] == "proven" and good["active"] == 1
    assert bad["active"] == 0                              # culled = deactivated


def test_freeze_sets_holdout_boundary(tmp_path):
    mod = _mod()
    db = os.path.join(str(tmp_path), "hl.sqlite")
    IS.ensure_schema(IS.connect(db))
    rc = mod.main(["--db-path", db, "--freeze", "--now-ms", "123456"])
    assert rc == 0
    c = IS.connect(db)
    assert IS.holdout_freeze_ts(c) == 123456


def _seed_follows(conn, wallet, pnls):
    for i, bps in enumerate(pnls):
        fid = IS.open_follow(conn, {"wallet": wallet, "coin": "ETH", "side": "long",
                                    "entry_mark": 1.0}, ts_ms=(i + 1) * 1000)
        IS.close_follow(conn, fid, exit_mark=1.0, net_bps=bps, reason="mirror_exit", ts_ms=(i + 1) * 1000)


def test_main_apply_culls_only_not_promotes(tmp_path):
    """--apply deactivates cull candidates but must NOT force-promote (that would strand the
    wallet in limbo vs the phase-2 gate). Good promote-lead stays active + watch_fallback."""
    mod = _mod()
    db = str(tmp_path / "hl.sqlite")
    c = IS.connect(db)
    IS.ensure_schema(c)
    IS.set_copy_target(c, "0xgood", active=True, copy_tier="watch_fallback", ts_ms=1)
    IS.set_copy_target(c, "0xbad", active=True, copy_tier="watch_fallback", ts_ms=1)
    _seed_follows(c, "0xgood", [40, 50, 45, 60, 42, 55, 48, 52])       # promote lead (n=8)
    _seed_follows(c, "0xbad", [-200, -300, -250, -400, -180])          # cull (n=5)

    rc = mod.main(["--db-path", db, "--apply", "--now-ms", "9999"])
    assert rc == 0
    c2 = IS.connect(db)
    good = c2.execute("SELECT active, copy_tier FROM hl_copy_targets WHERE wallet='0xgood'").fetchone()
    bad = c2.execute("SELECT active FROM hl_copy_targets WHERE wallet='0xbad'").fetchone()
    assert bad["active"] == 0                                          # culled
    assert good["active"] == 1 and good["copy_tier"] == "watch_fallback"  # NOT force-promoted


def test_wallet_pnls_reads_follow_ledger_time_ordered(tmp_path):
    mod = _mod()
    c = _db(tmp_path)
    for i, bps in enumerate([10.0, -5.0, 20.0]):
        fid = IS.open_follow(c, {"wallet": "0xw", "coin": "ETH", "side": "long", "entry_mark": 1.0},
                             ts_ms=(i + 1) * 1000)
        IS.close_follow(c, fid, exit_mark=1.0, net_bps=bps, reason="mirror_exit", ts_ms=(i + 1) * 1000)
    wp = mod.wallet_pnls(c, source="follow")
    assert wp["0xw"] == [10.0, -5.0, 20.0]                 # time-ordered
