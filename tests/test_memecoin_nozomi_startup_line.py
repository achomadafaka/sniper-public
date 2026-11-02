"""The Nozomi startup line names the lanes the provider serves (Alpha's LOW, 2026-10-09): with
MEMECOIN_NOZOMI_ALL_LANES on it said "slot-lane ... only"."""
import logging

from solders.keypair import Keypair


def _line(env, caplog):
    from test_memecoin_submission_backends import FakeClient, FakeSidecar
    from sniper.execution import submission_backends as B
    caplog.clear()
    try:
        B.build_solana_backend(keypair=Keypair(), custody_rpc_url="https://custody/rpc", env=env,
                               client_factory=FakeClient, tpu_sidecar=FakeSidecar())
    finally:
        B.close_all()
    return [r.getMessage() for r in caplog.records if "Nozomi A/B provider on" in r.getMessage()]


def test_the_startup_line_names_every_lane_only_when_all_lanes_is_on(caplog):
    caplog.set_level(logging.WARNING)
    base = {"MEMECOIN_SLOT_LANE_NOZOMI_AB": "1", "MEMECOIN_NOZOMI_API_KEY": "k-startup-line"}
    (slot,) = _line(base, caplog)
    assert "slot lane only" in slot and "every funded entry lane" not in slot
    (every,) = _line({**base, "MEMECOIN_NOZOMI_ALL_LANES": "1"}, caplog)
    assert "every funded entry lane" in every and "only" not in every
    assert "k-startup-line" not in slot + every
