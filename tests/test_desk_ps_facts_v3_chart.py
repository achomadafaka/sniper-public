"""pumpswap-facts-v3 (cohort D, Gamma shape, Alpha operator principle): chart figures come from OBSERVED prints
whenever prints_n >= 2; the pool's own losses and unattributed loss receipts are counted beside them and never
null a field. Null only on prints_lt_2 or a failed loss read. v1/v2 unchanged."""
import sqlite3

from sniper.services import memecoin_desk_ps_facts as F
from test_desk_ps_model_facts import NOW, ROWS, facts, tape


def gap(path):
    """Break the chain before the -10 s print (after a buy): a proven own-pool lost swap (post != next pre)."""
    with sqlite3.connect(path) as db:
        db.execute("UPDATE swaps_ps SET pre_quote_reserve_atomic='1' WHERE signature='fixture4'")


def test_an_own_loss_never_nulls_the_observed_chart_and_is_counted(tmp_path):
    c, path = tape(tmp_path)
    gap(path)
    v3 = facts(c, tmp_path, version=F.VERSION3)
    w = v3["chart"]["windows"]["60s"]
    assert v3["chart"]["state"] == "known" and w["null_reason"] is None
    assert w["own_pool_losses"] >= 1 and w["lower_bound"] is True
    assert w["range"] is not None and w["trend"] is not None and w["breakout"] is not None
    assert v3["structure"]["breakout"]["60s"]["state"] == "ok"
    assert v3["structure"]["breakout"]["60s"]["own_pool_losses"] >= 1
    v2 = facts(c, tmp_path, version=F.VERSION)
    assert v2["structure"]["breakout"]["60s"]["state"] == "unknown"        # v2 keeps its veto
    assert "chart" not in v2


def test_fewer_than_two_prints_is_the_only_tape_null(tmp_path):
    c, _ = tape(tmp_path, [(-100, 1.0, 1, 10, "old"), (-5, 2.0, 1, 10, "late")])
    w = facts(c, tmp_path, version=F.VERSION3)["chart"]["windows"]
    assert w["15s"]["null_reason"] == "prints_lt_2" and w["15s"]["prints_n"] == 1 and w["15s"]["range"] is None
    assert w["300s"]["null_reason"] is None and w["300s"]["prints_n"] == 2


def test_a_failed_loss_read_is_unknown_never_zero(tmp_path, monkeypatch):
    c, _ = tape(tmp_path)

    def boom(*a, **k):
        raise F.L.LossUnreadable("loss:row_limit")
    monkeypatch.setattr(F.L, "losses", boom)
    chart = facts(c, tmp_path, version=F.VERSION3)["chart"]
    assert chart == {"state": "unknown", "reason": "loss_read_failed", "definition_sha256": F.CHART_DEFINITION_SHA256,
                     "as_of": NOW}


def test_validator_pins_the_chart_shape(tmp_path):
    import copy
    import pytest
    c, _ = tape(tmp_path)
    good = facts(c, tmp_path, version=F.VERSION3)
    for poison in (lambda f: f["chart"]["windows"]["60s"].update(extra=1),
                   lambda f: f["chart"]["windows"]["15s"].update(breakout=True),
                   lambda f: f["chart"].update(definition_sha256="0" * 64),
                   lambda f: f["chart"]["windows"]["60s"].update(null_reason="own_pool_loss"),
                   lambda f: f.pop("chart")):
        bad = copy.deepcopy(good)
        poison(bad)
        with pytest.raises(ValueError):
            F.validate(bad)
    assert F.validate(good) == good


def test_unattributed_receipts_are_counted_and_never_null_a_field(tmp_path, monkeypatch):
    c, _ = tape(tmp_path)

    def losses(*a, receipt_details=None, **k):
        if receipt_details is not None:
            receipt_details += [("u", NOW - 30, "unknown_loss_receipts"), ("g", NOW - 20, "global_loss_receipts"),
                                ("g2", NOW - 200, "global_loss_receipts")]
        return [NOW - 30, NOW - 20, NOW - 200]
    monkeypatch.setattr(F.L, "losses", losses)
    w = facts(c, tmp_path, version=F.VERSION3)["chart"]["windows"]
    assert (w["60s"]["unattributed_loss_receipts"], w["300s"]["unattributed_loss_receipts"]) == (2, 3)
    assert w["60s"]["own_pool_losses"] == 0 and w["60s"]["range"] is not None and w["60s"]["lower_bound"] is True


def test_unattributed_loss_receipts_counts_each_signature_cause_once(tmp_path, monkeypatch):
    c, _ = tape(tmp_path)

    def losses(*a, receipt_details=None, **k):
        if receipt_details is not None:
            receipt_details += [("u", NOW - 30, "unknown_loss_receipts"), ("u", NOW - 29, "unknown_loss_receipts"),
                                ("u", NOW - 28, "global_loss_receipts")]
        return [NOW - 30, NOW - 29, NOW - 28]
    monkeypatch.setattr(F.L, "losses", losses)
    assert facts(c, tmp_path, version=F.VERSION3)["chart"]["windows"]["60s"]["unattributed_loss_receipts"] == 2


# ---- Zeta witnesses on 34500cf3f, D Amendment 12 5dd4a5e25 (degenerate math) and Gamma's validator invariants

def _w(tmp_path, rows, key="60s"):
    c, _ = tape(tmp_path, rows)
    return facts(c, tmp_path, version=F.VERSION3)["chart"]["windows"][key]


def test_two_fresh_prints_have_no_breakout_reference(tmp_path):
    w = _w(tmp_path, [(-5, 1.0, 1, 10, "a"), (-1, 2.0, 1, 10, "b")])
    assert w["breakout"] is None and w["field_unknown"] == {"breakout": "no_reference"} and w["null_reason"] is None
    assert w["range"] is not None and w["return_pct"] is not None


def test_two_equal_prices_have_a_real_zero_range_and_no_position(tmp_path):
    w = _w(tmp_path, [(-50, 1.0, 1, 10, "a"), (-5, 1.0, 1, 10, "b")])
    assert w["range"] == 0 and w["pos_in_range"] is None and w["field_unknown"]["pos_in_range"] == "zero_range"


def test_two_prints_at_one_timestamp_have_no_trend(tmp_path):
    w = _w(tmp_path, [(-5, 1.0, 1, 10, "a"), (-5, 2.0, 1, 10, "b")])
    assert w["prints_n"] == 2 and w["trend"] is None and w["field_unknown"]["trend"] == "same_timestamp"


def _good(tmp_path):
    c, _ = tape(tmp_path)
    return facts(c, tmp_path, version=F.VERSION3)


import pytest  # noqa: E402


@pytest.mark.parametrize("poison", [
    lambda w: w.update(prints_n=None), lambda w: w.update(own_pool_losses=None), lambda w: w.update(prints_n=True),
    lambda w: w.update(unattributed_loss_receipts=1.0), lambda w: w.update(partial="not_a_boolean"),
    lambda w: w.update(lower_bound=[]), lambda w: w.update(own_pool_losses=1, lower_bound=False),
    lambda w: w.update(prints_n=0, null_reason=None), lambda w: w.update(prints_n=5, null_reason="prints_lt_2"),
    lambda w: w.update(range=None), lambda w: w.update(return_pct=None), lambda w: w.update(trend="up"),
    lambda w: w.update(trend=None),                                                     # null without a reason
    lambda w: w["field_unknown"].update(range="zero_range"),                            # reason on a non-null field
    lambda w: w["field_unknown"].update(trend="made_up"),
])
def test_the_chart_validator_refuses_every_witness(tmp_path, poison):
    import copy
    good = _good(tmp_path)
    bad = copy.deepcopy(good)
    poison(bad["chart"]["windows"]["60s"])
    with pytest.raises(ValueError):
        F.validate(bad)
    assert F.validate(good) == good
