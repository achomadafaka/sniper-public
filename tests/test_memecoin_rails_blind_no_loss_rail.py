"""RAILS-BLIND must not page a position whose loss rails are all disabled.

2026-08-14: the operator turned stop AND catastrophe off. GARYFREAK (pos#37881) then paged
RAILS-BLIND — 'stop/catastrophe are reading the nominal mid' — about rails that cannot fire. The
alert's whole premise is that a loss rail reads an optimistic price; with no loss rail, it is noise.
"""
from sniper.services import memecoin_real_runner as R


class _Cfg:
    MEMECOIN_REAL_CATASTROPHE_PCT = 0.0


def _pos(**over):
    base = {"id": 1, "network_id": "solana", "entry_price": 1e-4,
            "funded_contract_json": None, "assigned_policy_json": None}
    base.update(over)
    return base


def test_no_loss_rail_means_no_alert(monkeypatch):
    monkeypatch.setattr(R, "effective_real_thresholds", lambda pos, ov=None: (0.0, None, 0.0, None))
    from sniper.services import memecoin_funded_policy as fp
    monkeypatch.setattr(fp, "effective_funded_policy",
                        lambda pos, override=None: (None, {"catastrophe_pct": 0.0}))
    assert R._has_active_loss_rail(_pos(), None, _Cfg()) is False


def test_a_stop_keeps_the_alert(monkeypatch):
    monkeypatch.setattr(R, "effective_real_thresholds", lambda pos, ov=None: (0.0, None, 60.0, None))
    assert R._has_active_loss_rail(_pos(), None, _Cfg()) is True


def test_a_catastrophe_floor_keeps_the_alert(monkeypatch):
    monkeypatch.setattr(R, "effective_real_thresholds", lambda pos, ov=None: (0.0, None, 0.0, None))
    from sniper.services import memecoin_funded_policy as fp
    monkeypatch.setattr(fp, "effective_funded_policy",
                        lambda pos, override=None: (object(), {"catastrophe_pct": 80.0}))
    assert R._has_active_loss_rail(_pos(), None, _Cfg()) is True


def test_an_mcap_sl_keeps_the_alert(monkeypatch):
    monkeypatch.setattr(R, "effective_real_thresholds",
                        lambda pos, ov=None: (0.0, None, 0.0, 50000.0))
    assert R._has_active_loss_rail(_pos(), None, _Cfg()) is True


def test_a_cfg_catastrophe_keeps_the_alert(monkeypatch):
    monkeypatch.setattr(R, "effective_real_thresholds", lambda pos, ov=None: (0.0, None, 0.0, None))
    from sniper.services import memecoin_funded_policy as fp
    monkeypatch.setattr(fp, "effective_funded_policy",
                        lambda pos, override=None: (None, {"catastrophe_pct": 0.0}))

    class Cfg:
        MEMECOIN_REAL_CATASTROPHE_PCT = 80.0
    assert R._has_active_loss_rail(_pos(), None, Cfg()) is True


def test_failure_to_prove_keeps_the_alert(monkeypatch):
    def boom(pos, ov=None):
        raise RuntimeError("policy read failed")
    monkeypatch.setattr(R, "effective_real_thresholds", boom)
    assert R._has_active_loss_rail(_pos(), None, _Cfg()) is True   # fail LOUD
