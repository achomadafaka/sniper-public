"""Everything live, no control halves (operator order 2026-10-09 via Alpha: "everything turned on live,
not shadow or off").

MEMECOIN_NOZOMI_ALL_B forces the Nozomi arm B on every listed-wallet send, on the slot lane and (with
MEMECOIN_NOZOMI_ALL_LANES) on every other lane, under its own rule name so these sends never mix with
the A/B's. MEMECOIN_BSC_BUILDER_ALL does the same for the 56 builder bundle. The guards, fallbacks,
bundle expiry and gas reserve are unchanged; only the arm is forced.
"""
import base64
import hashlib

import pytest
from solders.transaction import VersionedTransaction

from sniper.execution import bsc_builder_ab as B
from sniper.execution import solana_sender_ab as S
from test_memecoin_slot_lane_sender_ab import _arm_by_hand, _ids, _substages, conn  # noqa: F401
from test_memecoin_slot_lane_nozomi_ab import _production_lane
from test_memecoin_bsc_builder_ab import _run as _bsc_run


@pytest.fixture(autouse=True)
def _knobs(monkeypatch):
    for name in ("MEMECOIN_NOZOMI_ALL_B", "MEMECOIN_NOZOMI_ALL_LANES", "MEMECOIN_BSC_BUILDER_ALL",
                 "MEMECOIN_BSC_BUILDER_AB"):
        monkeypatch.delenv(name, raising=False)
    B._reset_marks()
    yield
    B._reset_marks()


def test_nozomi_all_b_is_arm_b_under_its_own_rule_with_the_ab_tip_account():
    for aid in ["%032x" % i for i in range(32)]:
        got = S.assign_nozomi_all(aid)
        digest = hashlib.sha256(aid.encode()).digest()
        assert got["arm"] == "B" and got["rule"] == "nozomi-all-b-v1" != S.NOZOMI_RULE
        assert got["tip_lamports"] == 5_000_000
        assert got["tip_account"] == S.NOZOMI_TIP_ACCOUNTS[digest[4] % 17]
        assert got["attempt_hash"] == digest[:4].hex()


def test_all_b_forces_the_non_slot_lane_arm_and_its_gas_reserve(monkeypatch):
    from sniper.settings import memecoin as cfg
    ids = _ids()
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_NOZOMI_AB_WALLETS", frozenset({"w1"}))
    monkeypatch.setenv("MEMECOIN_NOZOMI_ALL_LANES", "1")
    assert S.nozomi_entry_arm(ids["A"], "w1")["arm"] == "A"
    monkeypatch.setenv("MEMECOIN_NOZOMI_ALL_B", "1")
    arm = S.nozomi_entry_arm(ids["A"], "w1")
    assert (arm["arm"], arm["rule"]) == ("B", "nozomi-all-b-v1")
    assert S.nozomi_gas_extra_sol(ids["A"], "w1") == pytest.approx(0.005)
    assert S.nozomi_entry_arm(ids["A"], "w2") is None              # the wallet list still scopes it


def test_all_b_tips_an_arm_a_slot_lane_send_with_nozomi(conn, tmp_path, monkeypatch):
    ids = _ids()
    monkeypatch.setenv("MEMECOIN_NOZOMI_ALL_B", "1")
    from test_memecoin_slot_lane_sender_ab import _force_ids
    _force_ids(monkeypatch, [ids["A"]])
    ln, buy, tpu, sender_http, nozomi_http = _production_lane(conn, tmp_path, monkeypatch)
    assert ln.handle(buy("SigAllB"))["status"] == "failed"
    (sent,) = tpu
    (_m, _u, body), = nozomi_http.calls
    assert body["params"][0] == sent and sender_http.calls == []
    msg = VersionedTransaction.from_bytes(base64.b64decode(sent)).message
    assert S.tip_transfers(msg, S._NOZOMI_SET) == [(S.assign_nozomi_all(ids["A"])["tip_account"],
                                                   5_000_000)]
    stages = _substages(conn, ids["A"])
    assert (stages["nozomi_ab"]["arm"], stages["nozomi_ab"]["rule"]) == ("B", "nozomi-all-b-v1")
    assert stages["nozomi_ab_send"]["nozomi_state"] == "accepted"


def test_bsc_builder_all_is_arm_b_under_its_own_rule_and_turns_the_providers_on():
    aid = _ids()["A"]
    assert B.assign(aid, env={"MEMECOIN_BSC_BUILDER_ALL": "1"}) == {
        "arm": "B", "rule": "bsc-builder-all-b-v1",
        "attempt_hash": hashlib.sha256(aid.encode()).digest()[:4].hex()}
    assert B.knob_on({"MEMECOIN_BSC_BUILDER_ALL": "1"})
    assert [p.provider_id for p in B.providers(lambda *a, **k: None,
                                               env={"MEMECOIN_BSC_BUILDER_ALL": "1"})] == \
        ["56:puissant_bundle"]


def test_bsc_builder_all_bundles_an_arm_a_entry(conn, monkeypatch):
    ids = _ids()
    monkeypatch.setenv("MEMECOIN_BSC_BUILDER_ALL", "1")
    trace, custody, bundle_http, heads, signed = _bsc_run(conn, monkeypatch, ids["A"])
    _unwrap_raw, swap_raw = custody
    (_m, _url, _h, body), = bundle_http.calls
    assert body["params"][0]["txs"] == [swap_raw]
    ab = trace.stages["bsc_builder_ab"]
    assert (ab["arm"], ab["rule"]) == ("B", "bsc-builder-all-b-v1")
