import sqlite3

import scripts.screen_hl_smart_money as SCREEN
from sniper.hyperlive import intel_store as IS
from thread_local_time import patch_local  # noqa: E402  (2026-09-29)


def test_persisted_screen_runs_watch_lifecycle_and_activates_fallback(tmp_path, monkeypatch):
    db = str(tmp_path / "hl_intel.sqlite")
    wallet = "0x1111111111111111111111111111111111111111"
    row = {
        "wallet": wallet,
        "account_value": 100_000.0,
        "day": {"pnl": 1.0},
        "week": {"pnl": 1.0},
        "month": {"pnl": 1.0, "roi": 2.0},
        "all_time": {"pnl": 1.0},
    }
    verdict = {
        "verdict": "watch",
        "archetype": "directional",
        "archetype_pass": 1,
        "taker_frac": 1.0,
        "reason": "directional",
        "metrics": {
            "profit_factor": 3.0,
            "win_rate": 0.7,
            "n_closes": 100,
            "top_coin_pnl_frac": 0.2,
            "single_fill_frac": 0.1,
            "realized_gross": 1000.0,
        },
    }

    monkeypatch.setattr(SCREEN.HL, "fetch_leaderboard", lambda: [row])
    monkeypatch.setattr(SCREEN.HL, "fetch_user_fills", lambda _wallet: [{"coin": "BTC"}])
    monkeypatch.setattr(SCREEN.TI, "assess_trader", lambda _fills: verdict)

    assert SCREEN.main(["--persist", "--intel-db", db, "--candidates", "1"]) == 0

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    IS.ensure_schema(conn)
    targets = IS.get_executable_copy_targets(conn, now_ms=10)
    assert [t["wallet"] for t in targets] == [wallet]
    assert targets[0]["copy_tier"] == "watch_fallback"
    conn.close()


def test_fill_screen_retries_transient_fetch_error(tmp_path, monkeypatch):
    db = str(tmp_path / "hl_intel.sqlite")
    wallet = "0x2222222222222222222222222222222222222222"
    row = {
        "wallet": wallet,
        "account_value": 100_000.0,
        "day": {"pnl": 1.0},
        "week": {"pnl": 1.0},
        "month": {"pnl": 1.0, "roi": 2.0},
        "all_time": {"pnl": 1.0},
    }
    verdict = {
        "verdict": "watch",
        "archetype": "directional",
        "archetype_pass": 1,
        "taker_frac": 1.0,
        "reason": "directional",
        "metrics": {
            "profit_factor": 3.0,
            "win_rate": 0.7,
            "n_closes": 100,
            "top_coin_pnl_frac": 0.2,
            "single_fill_frac": 0.1,
            "realized_gross": 1000.0,
        },
    }
    calls = []
    sleeps = []

    def fetch_fills(w):
        calls.append(w)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return [{"coin": "BTC"}]

    monkeypatch.setattr(SCREEN.HL, "fetch_leaderboard", lambda: [row])
    monkeypatch.setattr(SCREEN.HL, "fetch_user_fills", fetch_fills)
    monkeypatch.setattr(SCREEN.TI, "assess_trader", lambda _fills: verdict)
    patch_local(monkeypatch, "sleep", lambda s: sleeps.append(s))

    assert SCREEN.main([
        "--persist", "--intel-db", db, "--candidates", "1",
        "--fill-retries", "1", "--fill-sleep", "0.25",
    ]) == 0

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    IS.ensure_schema(conn)
    targets = IS.get_executable_copy_targets(conn, now_ms=10)
    assert [t["wallet"] for t in targets] == [wallet]
    assert calls == [wallet, wallet]
    assert sleeps == [0.25]
    conn.close()


def test_screen_can_skip_existing_wallets_and_throttle_between_candidates(tmp_path, monkeypatch):
    db = str(tmp_path / "hl_intel.sqlite")
    existing = "0x3333333333333333333333333333333333333333"
    new_a = "0x4444444444444444444444444444444444444444"
    new_b = "0x5555555555555555555555555555555555555555"

    conn = IS.connect(db)
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, existing, {
        "verdict": "watch",
        "archetype": "directional",
        "archetype_pass": 1,
        "profit_factor": 2.0,
        "win_rate": 0.5,
        "n_closes": 100,
        "month_roi": 3.0,
        "reason": "already screened",
    }, ts_ms=1)
    conn.close()

    def row(wallet, roi):
        return {
            "wallet": wallet,
            "account_value": 100_000.0,
            "day": {"pnl": 1.0},
            "week": {"pnl": 1.0},
            "month": {"pnl": 1.0, "roi": roi},
            "all_time": {"pnl": 1.0},
        }

    verdict = {
        "verdict": "watch",
        "archetype": "directional",
        "archetype_pass": 1,
        "taker_frac": 1.0,
        "reason": "directional",
        "metrics": {
            "profit_factor": 3.0,
            "win_rate": 0.7,
            "n_closes": 100,
            "top_coin_pnl_frac": 0.2,
            "single_fill_frac": 0.1,
            "realized_gross": 1000.0,
        },
    }
    calls = []
    sleeps = []

    monkeypatch.setattr(SCREEN.HL, "fetch_leaderboard", lambda: [
        row(existing, 3.0), row(new_a, 2.0), row(new_b, 1.0),
    ])
    monkeypatch.setattr(SCREEN.HL, "fetch_user_fills", lambda w: calls.append(w) or [{"coin": "BTC"}])
    monkeypatch.setattr(SCREEN.TI, "assess_trader", lambda _fills: verdict)
    patch_local(monkeypatch, "sleep", lambda s: sleeps.append(s))

    assert SCREEN.main([
        "--persist", "--intel-db", db, "--candidates", "2",
        "--skip-existing", "--fill-delay", "0.5",
    ]) == 0

    assert calls == [new_a, new_b]
    assert sleeps == [0.5]


def test_screen_refresh_tracked_rescreens_known_wallet_below_topn(tmp_path, monkeypatch):
    # A tracked archetype-pass wallet that rotated out of the top-N is re-screened when
    # --refresh-tracked is set, so it never ages into a staleness cull.
    db = str(tmp_path / "hl_intel.sqlite")
    top = "0x7777777777777777777777777777777777777777"
    tracked = "0x8888888888888888888888888888888888888888"

    conn = IS.connect(db)
    IS.ensure_schema(conn)
    IS.upsert_tracked(conn, tracked, {
        "verdict": "watch", "archetype": "directional", "archetype_pass": 1,
        "profit_factor": 2.0, "win_rate": 0.6, "n_closes": 100, "month_roi": 1.0,
    }, ts_ms=1)
    conn.close()

    def row(wallet, roi):
        return {"wallet": wallet, "account_value": 100_000.0, "day": {"pnl": 1.0},
                "week": {"pnl": 1.0}, "month": {"pnl": 1.0, "roi": roi}, "all_time": {"pnl": 1.0}}

    verdict = {
        "verdict": "watch", "archetype": "directional", "archetype_pass": 1, "taker_frac": 1.0,
        "reason": "directional",
        "metrics": {"profit_factor": 3.0, "win_rate": 0.7, "n_closes": 100,
                    "top_coin_pnl_frac": 0.2, "single_fill_frac": 0.1, "realized_gross": 1000.0},
    }
    calls = []

    # both are in the leaderboard; --candidates 1 keeps only `top` as a prefiltered candidate.
    monkeypatch.setattr(SCREEN.HL, "fetch_leaderboard", lambda: [row(top, 5.0), row(tracked, 1.0)])
    monkeypatch.setattr(SCREEN.HL, "fetch_user_fills", lambda w: calls.append(w) or [{"coin": "BTC"}])
    monkeypatch.setattr(SCREEN.TI, "assess_trader", lambda _fills: verdict)

    assert SCREEN.main([
        "--persist", "--intel-db", db, "--candidates", "1", "--refresh-tracked",
    ]) == 0

    assert top in calls
    assert tracked in calls  # re-screened despite being below the top-N cut


def test_screen_refresh_tracked_rescreens_wallet_off_leaderboard(tmp_path, monkeypatch):
    # A tracked archetype-pass wallet that dropped off the leaderboard entirely is still
    # re-screened from its fills (synthesized candidate from stored fields), so the stale-cull
    # exemption never leaves it followed on a permanently-frozen verdict.
    db = str(tmp_path / "hl_intel.sqlite")
    top = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    gone = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    conn = IS.connect(db)
    IS.ensure_schema(conn)
    seed = {"verdict": "watch", "archetype": "directional", "archetype_pass": 1,
            "profit_factor": 2.0, "win_rate": 0.6, "n_closes": 100, "month_roi": 2.0}
    IS.upsert_tracked(conn, gone, seed, ts_ms=1)
    IS.upsert_screened(conn, gone, {**seed, "account_value": 80_000.0, "top_coin_frac": 0.2,
                                    "single_fill_frac": 0.1, "reason": "directional"}, ts_ms=1)
    conn.close()

    def row(wallet, roi):
        return {"wallet": wallet, "account_value": 100_000.0, "day": {"pnl": 1.0},
                "week": {"pnl": 1.0}, "month": {"pnl": 1.0, "roi": roi}, "all_time": {"pnl": 1.0}}

    verdict = {
        "verdict": "watch", "archetype": "directional", "archetype_pass": 1, "taker_frac": 1.0,
        "reason": "directional",
        "metrics": {"profit_factor": 3.0, "win_rate": 0.7, "n_closes": 100,
                    "top_coin_pnl_frac": 0.2, "single_fill_frac": 0.1, "realized_gross": 1000.0},
    }
    calls = []

    monkeypatch.setattr(SCREEN.HL, "fetch_leaderboard", lambda: [row(top, 5.0)])  # `gone` absent
    monkeypatch.setattr(SCREEN.HL, "fetch_user_fills", lambda w: calls.append(w) or [{"coin": "BTC"}])
    monkeypatch.setattr(SCREEN.TI, "assess_trader", lambda _fills: verdict)

    assert SCREEN.main([
        "--persist", "--intel-db", db, "--candidates", "1", "--refresh-tracked",
    ]) == 0

    assert gone in calls  # re-screened from fills despite being off the leaderboard


def test_screen_refresh_tracked_skips_forward_evidence_culled(tmp_path, monkeypatch):
    # A wallet culled for negative forward evidence is NOT re-screened (avoids churn / re-fetch of
    # wallets we removed for cause). Only active + stale-culled wallets are refreshed.
    db = str(tmp_path / "hl_intel.sqlite")
    top = "0xcccccccccccccccccccccccccccccccccccccccc"
    bad = "0xdddddddddddddddddddddddddddddddddddddddd"

    conn = IS.connect(db)
    IS.ensure_schema(conn)
    seed = {"verdict": "watch", "archetype": "directional", "archetype_pass": 1,
            "profit_factor": 2.0, "win_rate": 0.6, "n_closes": 100, "month_roi": 1.0}
    IS.upsert_tracked(conn, bad, seed, ts_ms=1)
    IS.upsert_screened(conn, bad, {**seed, "account_value": 90_000.0, "top_coin_frac": 0.2,
                                   "single_fill_frac": 0.1, "reason": "directional"}, ts_ms=1)
    IS.set_tracked_lifecycle(conn, bad, status="culled",
                             reason="16 closed follows mean -252.31bps <= -25", ts_ms=2)
    conn.close()

    def row(wallet, roi):
        return {"wallet": wallet, "account_value": 100_000.0, "day": {"pnl": 1.0},
                "week": {"pnl": 1.0}, "month": {"pnl": 1.0, "roi": roi}, "all_time": {"pnl": 1.0}}

    verdict = {
        "verdict": "watch", "archetype": "directional", "archetype_pass": 1, "taker_frac": 1.0,
        "reason": "directional",
        "metrics": {"profit_factor": 3.0, "win_rate": 0.7, "n_closes": 100,
                    "top_coin_pnl_frac": 0.2, "single_fill_frac": 0.1, "realized_gross": 1000.0},
    }
    calls = []

    # `bad` is in the leaderboard but below the top-N cut; refresh must NOT pull it back in.
    monkeypatch.setattr(SCREEN.HL, "fetch_leaderboard", lambda: [row(top, 5.0), row(bad, 1.0)])
    monkeypatch.setattr(SCREEN.HL, "fetch_user_fills", lambda w: calls.append(w) or [{"coin": "BTC"}])
    monkeypatch.setattr(SCREEN.TI, "assess_trader", lambda _fills: verdict)

    assert SCREEN.main([
        "--persist", "--intel-db", db, "--candidates", "1", "--refresh-tracked",
    ]) == 0

    assert top in calls
    assert bad not in calls  # culled for forward evidence -> excluded from refresh
