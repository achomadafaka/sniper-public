"""WO-6 — predictive-rug wallet graph. Deployer/LP resolution + activity flag, degrade-loudly."""
from sniper.analysis.memecoin_wallet_graph import deployer_activity, resolve_watch_set


def _t(block_no, maker):
    return {"block_no": block_no, "maker": maker}


def test_resolves_deployer_and_top_lps_excluding_router():
    ticks = [{**_t(5, "DEP"), "role": "deployer"}, _t(6, "ROUTER"),
             {**_t(7, "LP1"), "kind": "lp_add"}, {**_t(8, "LP1"), "kind": "lp_add"},
             {**_t(9, "LP2"), "kind": "lp_add"}, {**_t(10, "ROUTER"), "kind": "lp_add"},
             {**_t(11, "LP3"), "kind": "lp_add"}]
    ws = resolve_watch_set("POOL", ticks, top_lps=3, router_addrs={"ROUTER"})
    assert ws["resolvable"] is True
    assert ws["deployer"] == "DEP"                      # earliest by block_no, router excluded
    assert ws["lp_providers"][0] == "LP1"              # most frequent non-deployer
    assert set(ws["lp_providers"]) == {"LP1", "LP2", "LP3"}


def test_unresolvable_pool_degrades_loudly():
    ws = resolve_watch_set("POOL", [{"block_no": 1, "maker": None}], router_addrs=set())
    assert ws["resolvable"] is False and ws["deployer"] is None
    assert any("unresolvable" in g for g in ws["data_gaps"])   # explicit gap, never a phantom claim


def test_sig_fetch_failure_surfaces_a_gap_not_a_claim():
    def boom(_pool):
        raise RuntimeError("rpc down")
    ws = resolve_watch_set("POOL", [_t(5, "ACTOR")], sig_fetch=boom)
    assert any("sig_fetch failed" in g for g in ws["data_gaps"])
    assert ws["deployer"] is None                       # a swap actor is not creation identity


def test_sig_fetch_receives_pool_and_requires_creation_role():
    seen = []
    ws = resolve_watch_set(
        "POOL-7", [_t(5, "ACTOR")],
        sig_fetch=lambda pool: seen.append(pool) or [{"block_no": 1, "maker": "DEP",
                                                       "role": "deployer"}])
    assert seen == ["POOL-7"]
    assert ws["resolvable"] is True and ws["deployer"] == "DEP"


def test_deployer_activity_flags_watched_wallet():
    ws = {"resolvable": True, "deployer": "DEP", "lp_providers": ["LP1", "LP2"]}
    assert deployer_activity(ws, [_t(20, "LP1"), _t(21, "RANDO")]) == {"active": True, "wallets": ["LP1"]}
    assert deployer_activity(ws, [_t(20, "RANDO")]) == {"active": False, "wallets": []}


def test_deployer_activity_never_claims_on_unresolvable():
    assert deployer_activity({"resolvable": False}, [_t(1, "ANY")]) == {"active": False, "wallets": []}


# ── report logic: router detection + resolvability (synthetic DB) ──

def test_report_router_detection_and_resolvability(tmp_path):
    import sqlite3
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks as t
    import scripts.research.rug_precedence_report as RP
    path = str(tmp_path / "t.db")
    c = sqlite3.connect(path); t.ensure_tick_tables(c); ensure_feature_tables(c)
    # ROUTER makes swaps in many pools; DEP_i seeds each pool first
    for p in range(20):
        c.execute("INSERT INTO pool_ticks(chain_id,pool,kind,block_no,maker,amount0,amount1,observed_at) "
                  "VALUES('4663',?,'v2_swap',?,?, '1','1','t')", (f"pool{p}", 1, f"DEP{p}"))
        c.execute("INSERT INTO pool_ticks(chain_id,pool,kind,block_no,maker,amount0,amount1,observed_at) "
                  "VALUES('4663',?,'v2_swap',?,'ROUTER', '1','1','t')", (f"pool{p}", 2))
    c.commit(); c.close()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    router_set, total = RP.routers(conn, "4663", 0.05)     # ROUTER in all 20 pools -> router
    resolvable, sampled = RP.resolvability(conn, "4663", router_set)
    conn.close()
    assert "ROUTER" in router_set and total == 20
    assert resolvable == 20 and sampled == 20              # each pool's earliest is a genuine DEP
