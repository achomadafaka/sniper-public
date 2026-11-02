"""Full-only funded mirrors require liquidation proof, never a percentage rounded to one."""
import json

import pytest

from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg
from test_memecoin_real_runner import _c, _Ev, _insert_observation, _open_real
from test_memecoin_bsc_sniper_arm import mirror, db, _position, _sale
from test_memecoin_onchain_mirror_pending import world, _gas_hold
from sniper.persistence import db_memecoin as m


@pytest.fixture
def full_only(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 1.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_CHAINS", frozenset({"solana"}))
    monkeypatch.setattr(R, "_alert_exit_result", lambda *a, **k: None)
    monkeypatch.setattr(R, "check_wallet_demotion", lambda *a, **k: None)


@pytest.mark.parametrize("remaining", [None, 0.0, 0.01, 50.0])
@pytest.mark.parametrize("first_sell", [False, True])
@pytest.mark.parametrize("size_rule", [False, True])
def test_size_only_feed_and_first_sell_policy_cannot_prove_full_liquidation(
        full_only, monkeypatch, remaining, first_sell, size_rule):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_CHAINS",
                        frozenset({"solana"}) if size_rule else frozenset())
    c = _c()
    pid = _open_real(c, "MINT")
    if remaining is not None:
        _insert_observation(c, "sell", "u1", "2026-07-15T14:59:59+00:00", mint="MINT",
                            network_id="solana", side="sell", remaining_usd=remaining)
    from sniper.services import memecoin_funded_policy as policy
    monkeypatch.setattr(policy, "effective_funded_policy", lambda *a, **k: (
        object() if first_sell else None, {"mirror_on_sell": True}))
    monkeypatch.setattr(policy, "is_exit_trial_v4", lambda _: first_sell)
    calls = []
    result = R.real_mirror_on_sell(
        c, _Ev("MINT", side="sell", event_id="sell"),
        {"solana": {"exit_fn": lambda *a: calls.append(a) or {"status": "closed"}}},
        now_fn=lambda: "2026-07-15T15:00:00+00:00")
    assert calls == [] and result["status"] == "mirror_held"
    audit = c.execute("SELECT allowed FROM memecoin_exit_trigger_audit WHERE position_id=?",
                      (pid,)).fetchone()
    assert audit[0] == 0


@pytest.mark.parametrize("sold,pre,post", [
    (50, 100, 50), (99, 100, 1), (10**30 - 1, 10**30, 1),
    (100, 100, None), (101, 100, 0), (100, 100, "0.5"), (100, 100, False),
])
def test_evm_partial_or_unreadable_remainder_cannot_mirror(mirror, sold, pre, post):
    mirror.mp.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 1.0)
    _position(mirror.db)
    row = _sale(mirror.db, sold=sold, pre=pre)
    row["post_atomic"] = post
    result = mirror.run(row)
    assert mirror.exits == [] and result["status"] == "mirror_held"


def test_evm_exact_full_liquidation_exits_once(mirror):
    mirror.mp.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 1.0)
    pid = _position(mirror.db)
    row = _sale(mirror.db, sold=10**30, pre=10**30)
    assert mirror.run(row)["status"] == "closed"
    assert mirror.exits == [(pid, "mirror")]
    assert mirror.run(row)["status"] == "already_decided"
    detail = json.loads(mirror.db.execute(
        "SELECT detail FROM memecoin_exit_trigger_audit WHERE position_id=?", (pid,)).fetchone()[0])
    assert detail["post_atomic"] == "0"


def test_full_liquidation_proof_survives_an_unsigned_deferral(world):
    world.mp.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 1.0)
    pid = _position(world.c)
    row = _sale(world.c, sold=100, pre=100)
    debt = _gas_hold(world.c)
    assert world.run(row)["status"] == "mirror_pending"
    assert world.calls == []
    m.finish_evm_gas_topup(world.c, row_id=debt, status="confirmed")
    assert world.run(row)["status"] == "closed"
    assert world.calls == [pid]


def test_legacy_pending_mirror_without_zero_balance_proof_holds(world):
    pid = _position(world.c)
    row = _sale(world.c, sold=100, pre=100)
    debt = _gas_hold(world.c)
    assert world.run(row)["status"] == "mirror_pending"
    audit = world.c.execute("SELECT id,detail FROM memecoin_exit_trigger_audit").fetchone()
    detail = json.loads(audit["detail"])
    detail.pop("post_atomic")
    world.c.execute("UPDATE memecoin_exit_trigger_audit SET detail=? WHERE id=?",
                    (json.dumps(detail), audit["id"]))
    world.c.commit()
    world.mp.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 1.0)
    m.finish_evm_gas_topup(world.c, row_id=debt, status="confirmed")
    assert world.run(row)["status"] == "mirror_held"
    assert world.calls == [] and m.get_position(world.c, pid)["is_open"] == 1
