"""Proof lane: when a proven wallet exists, the top-N watch_fallback wallets stay
executable for PAPER copy so their exact-copy evidence keeps accruing toward a second
proven wallet. Without this, promotion freezes every fallback's copy record (found
2026-07-17: 0x1220's clean copy n froze at 15 the day 0xc9c7 went proven)."""
import time

from sniper.hyperlive import intel_store as IS

NOW = int(time.time() * 1000)


def _conn(tmp_path):
    c = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(c)
    return c


def _screened(c, wallet, *, archetype_pass=1, verdict="copyable", lifecycle="active"):
    c.execute("INSERT INTO hl_tracked_traders(wallet, verdict, archetype_pass, "
              "lifecycle_status, added_ts_ms, updated_ts_ms) VALUES (?,?,?,?,?,?)",
              (wallet, verdict, archetype_pass, lifecycle, NOW, NOW))
    c.commit()


def _target(c, wallet, *, tier, active):
    c.execute("INSERT INTO hl_copy_targets(wallet, active, copy_tier, added_ts_ms, "
              "updated_ts_ms) VALUES (?,?,?,?,?)", (wallet, active, tier, NOW, NOW))
    c.commit()


def _follows(c, wallet, n, mean_bps, *, coins=10):
    """n closed follows spread over `coins` coins × distinct days: passes concentration,
    gives a strong clustered t when mean is well above 0."""
    for i in range(n):
        coin = f"C{i % coins}"
        ts = NOW - (i // coins) * 86_400_000 - 1000
        net = mean_bps + (10 if i % 2 == 0 else -10)
        c.execute("INSERT INTO hl_follow_positions(wallet, coin, side, detect_ts_ms, "
                  "entry_mark, status, exit_ts_ms, net_bps) "
                  "VALUES (?,?,?,?,?, 'closed', ?, ?)",
                  (wallet, coin, "long", ts - 3600_000, 1.0, ts, net))
    c.commit()


def _setup_proven(c):
    """W0 passes phase-2 (n=30, positive, diversified, fresh) and is the active proven target."""
    _screened(c, "0xproven")
    _target(c, "0xproven", tier="proven", active=1)
    _follows(c, "0xproven", 30, 100.0)


def test_proof_lane_top_n_by_follow_mean(tmp_path):
    c = _conn(tmp_path)
    _setup_proven(c)
    for w, n, mean in (("0xw1", 10, 60.0), ("0xw2", 8, 30.0), ("0xw3", 6, 90.0)):
        _screened(c, w)
        _target(c, w, tier="watch_fallback", active=0)     # lifecycle-deactivated
        _follows(c, w, n, mean)
    out = IS.get_executable_copy_targets(c, now_ms=NOW, proof_lane_n=2)
    wallets = [t["wallet"] for t in out]
    assert wallets[0] == "0xproven"
    assert set(wallets[1:]) == {"0xw3", "0xw1"}            # top-2 by follow mean
    lane = [t for t in out if t["wallet"] != "0xproven"]
    assert all(t["copy_status"]["proof_lane"] for t in lane)
    assert all(t["copy_status"]["eligible"] for t in lane)
    assert all(t.get("copy_tier") == "watch_fallback" for t in lane)


def test_proof_lane_excludes_culled_thin_and_non_archetype(tmp_path):
    c = _conn(tmp_path)
    _setup_proven(c)
    _screened(c, "0xculled", lifecycle="culled")
    _target(c, "0xculled", tier="watch_fallback", active=0)
    _follows(c, "0xculled", 20, 500.0)
    _screened(c, "0xthin")
    _target(c, "0xthin", tier="watch_fallback", active=0)
    _follows(c, "0xthin", 3, 500.0)                        # n<5 evidence floor
    _screened(c, "0xreject", archetype_pass=0, verdict="reject")
    _target(c, "0xreject", tier="watch_fallback", active=0)
    _follows(c, "0xreject", 20, 500.0)
    out = IS.get_executable_copy_targets(c, now_ms=NOW, proof_lane_n=5)
    assert [t["wallet"] for t in out] == ["0xproven"]


def test_proof_lane_excludes_negative_mean(tmp_path):
    """Negative-follow-mean wallets are cull candidates, not promote candidates — they
    must not fill lane slots just because the quota has room (seen live 2026-07-17:
    0x450891… mean −171bps took a slot)."""
    c = _conn(tmp_path)
    _setup_proven(c)
    _screened(c, "0xneg")
    _target(c, "0xneg", tier="watch_fallback", active=0)
    _follows(c, "0xneg", 10, -50.0)
    out = IS.get_executable_copy_targets(c, now_ms=NOW, proof_lane_n=5)
    assert [t["wallet"] for t in out] == ["0xproven"]


def test_proof_lane_zero_disables(tmp_path):
    c = _conn(tmp_path)
    _setup_proven(c)
    _screened(c, "0xw1")
    _target(c, "0xw1", tier="watch_fallback", active=0)
    _follows(c, "0xw1", 10, 60.0)
    out = IS.get_executable_copy_targets(c, now_ms=NOW, proof_lane_n=0)
    assert [t["wallet"] for t in out] == ["0xproven"]


def test_proof_lane_env_default(tmp_path, monkeypatch):
    c = _conn(tmp_path)
    _setup_proven(c)
    _screened(c, "0xw1")
    _target(c, "0xw1", tier="watch_fallback", active=0)
    _follows(c, "0xw1", 10, 60.0)
    monkeypatch.setenv("HL_COPY_PROOF_LANE_N", "1")
    out = IS.get_executable_copy_targets(c, now_ms=NOW)
    assert {t["wallet"] for t in out} == {"0xproven", "0xw1"}
    monkeypatch.setenv("HL_COPY_PROOF_LANE_N", "0")
    out = IS.get_executable_copy_targets(c, now_ms=NOW)
    assert [t["wallet"] for t in out] == ["0xproven"]


def test_no_proven_no_lane_change(tmp_path):
    """Without a proven wallet the pre-existing fallback-basket behavior is untouched
    (lane only ADDS when proven_exists froze the fallbacks)."""
    c = _conn(tmp_path)
    _screened(c, "0xw1")
    _target(c, "0xw1", tier="watch_fallback", active=1)
    _follows(c, "0xw1", 10, 60.0)
    out = IS.get_executable_copy_targets(c, now_ms=NOW, proof_lane_n=5)
    assert [t["wallet"] for t in out] == ["0xw1"]
    assert "proof_lane" not in (out[0]["copy_status"].get("reason") or "")