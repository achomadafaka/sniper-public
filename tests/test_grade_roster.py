"""The scheduled profiler's pure composition (build_grade): Q75 from activity + archetype-led profile."""
from __future__ import annotations
import importlib
GR = importlib.import_module("scripts.grade_roster")

ARCH = {"classification": {"archetype": "directional_discretionary", "confidence": "high",
                           "copy_policy": "validated_copy"},
        "classification_layer2": {"archetype": "thesis_specialist"},
        "domain_tags": {"primary_tag": "NBA"}}
THESIS = {"shuffle_baseline": {"obs_over_expected": 2.4},
          "edge_clusters": [{"label": "big NBA bets", "pnl": 50000, "is_loss_center": 0}]}

def _buy(usd, px=0.5, cid="0xc", oidx=0, slug="nba-lal-bos"):
    return {"type": "TRADE", "side": "BUY", "conditionId": cid, "outcomeIndex": oidx,
            "usdcSize": usd, "size": usd/px, "price": px, "eventSlug": slug, "title": "x", "timestamp": 1}

def test_build_grade_composes_q75_and_profile():
    acts = [_buy(10000, cid="0x1"), _buy(40000, cid="0x2"), _buy(30000, cid="0x3"), _buy(20000, cid="0x4")]
    g = GR.build_grade("0x" + "A" * 40, thesis=THESIS, archetype_report=ARCH, activity=acts, analyzed_ts=7)
    assert g["wallet"] == "0x" + "a" * 40 and g["analyzed_ts"] == 7
    assert g["archetype"] == "directional_discretionary" and g["directional_profile"] is True
    assert g["in_sample_edge_read"] == "profitable_unproven"  # in-sample ratio is never "provable"
    # nearest-rank Q75 of [10k,20k,30k,40k] = 30k — NOT the max (the old off-by-one bug returned 40k)
    assert g["q75_usdc"] == 30000.0

def test_build_grade_q75_counts_non_sports_positions():
    # REGRESSION: Q75 used to be sports-only, so a politics/crypto wallet got q75=0 and its conviction
    # gate was silently disabled. Now it counts all-market positions.
    acts = [_buy(5000, cid="0xp1", slug="us-house-control-2026"),
            _buy(15000, cid="0xp2", slug="trump-cabinet-pick"),
            _buy(25000, cid="0xp3", slug="btc-100k-2026"),
            _buy(35000, cid="0xp4", slug="fed-rate-hike")]
    g = GR.build_grade("0x" + "C" * 40, thesis={}, archetype_report=ARCH, activity=acts)
    assert g["q75_usdc"] == 25000.0

def test_build_grade_empty_activity_q75_zero():
    g = GR.build_grade("0x" + "b" * 40, thesis={}, archetype_report={}, activity=[])
    assert g["q75_usdc"] == 0.0 and g["description"]


def test_main_gate_skips_wallets_without_thesis(monkeypatch, capsys):
    import sqlite3

    import db
    from sniper.persistence import db_wallet_signals as signals

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    signals.ensure_wallet_signals_tables(conn)
    monkeypatch.setattr(db, "get_connection", lambda: conn)
    monkeypatch.setattr(GR, "_stored_thesis_wallets", lambda: {})       # nobody analyzed
    called = []
    monkeypatch.setattr("scripts.wallet_archetype_extractor.extract_wallet_archetype",
                        lambda w, **k: called.append(w) or {})
    rc = GR.main(["0x" + "e" * 40])
    assert rc == 0 and called == []                                     # never profiled the un-analyzed wallet
    assert "SKIPPED" in capsys.readouterr().err


def test_main_prunes_existing_profiles_without_thesis(monkeypatch):
    import sqlite3
    import db
    from sniper.persistence import db_wallet_signals as D

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    D.ensure_wallet_signals_tables(conn)
    keep = "0x" + "a" * 40
    drop = "0x" + "d" * 40
    for w in (keep, drop):
        D.upsert_profile(conn, {"wallet": w, "archetype": "directional_discretionary",
                                "copy_policy": "validated_copy", "directional_profile": True,
                                "description": "old"}, now_ts=1)
    monkeypatch.setattr(db, "get_connection", lambda: conn)
    monkeypatch.setattr(GR, "_stored_thesis_wallets", lambda: {keep: {"wallet": keep, "run_ts": 7}})
    monkeypatch.setattr("scripts.wallet_archetype_extractor.extract_wallet_archetype", lambda w, **k: ARCH)

    assert GR.main([]) == 0
    assert set(D.all_profiles(conn).keys()) == {keep}
