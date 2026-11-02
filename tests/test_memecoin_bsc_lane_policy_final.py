"""The BSC fast lane's policy refusal of a source buy is final for the J7 path (73798, 2026-10-12).

The lane refused 0x105ed107... on 'mcap $262,855 over ceiling $250,000' from the source receipt;
the J7 copy of the same buy then passed on its own event mcap ($239,371) and lost 33.7%.
"""
import pytest

from sniper.services import memecoin_bsc_fast_lane as bsc

TX = "0x105ed1076867abf9e0d7a7950fef7357e209d5893534c1bb62334d9c621ebb29"
WALLET = "0x6feae02e6fd4f43387b2ef2b772a04e2039b0a0a"
MINT = "0x0699c83f715271da83c4d2191194d12d61417777"
MCAP = "hook:skipped_mcap:mcap $262,855 over ceiling $250,000 (over_ceiling)"


@pytest.fixture(autouse=True)
def _clean():
    bsc.clear_policy_decisions()
    yield
    bsc.clear_policy_decisions()


@pytest.mark.parametrize("reason,exact,wallet", [
    (MCAP, True, True),
    ("hook:skipped_chase:route quote is 1.93x the source price (cap 1.30x)", True, False),
    ("source_band:source buy $99 under absolute floor $197", True, False),
    ("hook:rejected:source buy $496 (0.422%) under mcap floor 0.50%", True, False),
    ("float_short", False, False), ("snapshot_predates_execution", False, False),
    ("lock_busy", False, False), ("reverted", False, False), ("filled", False, False),
    ("hook:disarmed:kill_switch_tripped", False, False),
    ("hook:rejected:one position per coin: another entry attempt was in progress", False, False),
    ("source_initiation_unknown:wallet_initiation_unproved", False, False)])
def test_only_policy_refusals_are_final_and_only_mcap_binds_a_tx_less_report(reason, exact, wallet):
    bsc.note_policy_decision(TX, MINT, "u1", reason, now=100.0)
    assert (bsc.policy_decision_for(tx_hash=TX, mint=MINT, source_user_id="u1", now=101.0)
            == (reason if exact else None))
    assert (bsc.policy_decision_for(tx_hash=None, mint=MINT, source_user_id="u1", now=101.0)
            == (reason if wallet else None))


def test_another_wallet_coin_or_transaction_and_an_old_decision_do_not_bind():
    bsc.note_policy_decision(TX, MINT, "u1", MCAP, now=100.0)
    assert bsc.policy_decision_for(tx_hash="0x" + "11" * 32, mint=MINT, source_user_id="u1", now=101.0) is None
    assert bsc.policy_decision_for(tx_hash=None, mint=MINT, source_user_id="u2", now=101.0) is None
    assert bsc.policy_decision_for(tx_hash=None, mint="0x" + "22" * 20, source_user_id="u1", now=101.0) is None
    assert bsc.policy_decision_for(tx_hash=TX, mint=MINT, source_user_id="u1",
                                   now=100.0 + bsc.POLICY_DECISION_TTL_S + 1) is None


def test_the_registry_is_bounded():
    for i in range(bsc.POLICY_DECISION_MAX + 5):
        bsc.note_policy_decision("0x%064x" % i, MINT, "u1", MCAP, now=100.0)
    assert bsc.policy_decision_for(tx_hash="0x%064x" % 0, mint=MINT, source_user_id="u1", now=101.0) is None
    assert bsc.policy_decision_for(tx_hash="0x%064x" % (bsc.POLICY_DECISION_MAX + 4), mint=MINT,
                                   source_user_id="u1", now=101.0) == MCAP


def test_the_live_lane_records_its_own_policy_refusal():
    live = object.__new__(bsc.BscLive)
    live.ctx = {}
    live._handle = lambda payload, recv_epoch: {"reason": MCAP, "tx_hash": None}
    payload = {"tx_hash": TX, "mint": MINT, "source_user_id": "u1", "route_kind": None}
    assert live.handle(payload, recv_epoch=100.0)["reason"] == MCAP
    assert bsc.policy_decision_for(tx_hash=TX, mint=MINT, source_user_id="u1") == MCAP


@pytest.mark.parametrize("event_id,noted,expected", [
    ("swap:raybot:%s:%s:%s" % (TX, WALLET, MINT), MCAP, "bsc_fast_lane_decided"),
    ("swap:4dc8041a-936d-4aaf-aabb-032a0364d345", MCAP, "bsc_fast_lane_decided"),
    ("swap:raybot:%s:%s:%s" % (TX, WALLET, MINT), None, "fixture_admitted"),
    ("swap:raybot:%s:%s:%s" % (TX, WALLET, MINT), "float_short", "fixture_admitted")])
def test_a_j7_copy_of_a_lane_refused_buy_is_refused_before_any_reservation(monkeypatch, event_id, noted, expected):
    from sniper.services import memecoin_real_runner as runner
    from test_memecoin_entry_identity import NOW, _arm, _approve, _conn, _event
    ev = _event(network_id="56", mint=MINT, source="follow_activity", event_id=event_id, market_cap=239_371.)
    ev.their_price = 1.
    conn = _conn(); _arm(monkeypatch)
    monkeypatch.setattr(runner.cfg, "MEMECOIN_EVM_ALLOWLIST", {"4663", "8453", "56"})
    _approve(conn, chains=("56",))
    monkeypatch.setattr(runner.m, "wallet_real_cohort_start", lambda *a, **k: None)  # fixture clock
    if noted:
        bsc.note_policy_decision(TX, MINT, "u1", noted)
    reached = []
    monkeypatch.setattr(runner, "real_entry_hook", lambda *a, **k: reached.append(k) or {"status": "fixture_admitted"})
    ctx = dict(usdc_fn=lambda: 100., native_fn=lambda: 1., token_safety_fn=lambda *a, **k: (True, None),
               execute_fn=lambda *a: None)
    hook = runner.make_real_hook(None, {"56": ctx}, now_fn=lambda: NOW, trade_size=30., max_age_s=45)
    result = hook.raw(conn, ev)
    assert result.get("reason", result["status"]).split(":")[0] == expected, result
    assert bool(reached) == (expected == "fixture_admitted")
    assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0
