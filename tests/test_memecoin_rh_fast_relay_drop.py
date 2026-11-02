"""The 4663 fast lane drops a Relay-route buy before it writes an attempt (2026-10-07, Alpha).

The lane's event takes its initiation proof from the receipt (`receipt_proof_version`). For a
Relay buy that is a relay-* envelope, which source_initiation_refusal never accepts: the lane
refused every Relay buy it was sent `wallet_initiation_unproved` (4 all-time, 10-06..10-07). Its
attempt row then made the on-chain lane skip the SAME transaction as `duplicate_of_feed` after the
watcher's origin verify committed it (already_attempted_from_another_lane counts rh_fast_lane). J7
had decided first in all 4, so none was lost, but a Relay buy where this lane beats J7 would be.
Now a relay-* receipt ends at the lane as `relay_origin_unproved` (its own event row, counted),
before any provider call, reservation or attempt row. The verified buy belongs to the on-chain
lane after the origin commit. A direct-wallet-paid buy is unchanged.
"""
import pytest

from test_memecoin_rh_fast_entry import _attempts, _go, _payload, entry  # noqa: F401


@pytest.mark.parametrize("version", ["relay-envelope-other-target-v1", "relay-envelope-linked-v1",
                                     "relay-kyber-linked-v1"])
def test_a_relay_route_buy_ends_at_the_lane_without_an_attempt(entry, version):
    built, sender, net, ledger = entry
    p = _payload()
    p["source_fill"] = dict(p["source_fill"], receipt_proof_version=version,
                            payment_basis="relay_receipt_outflow")
    rec = _go(built, p)
    assert rec["reason"] == "relay_origin_unproved", rec
    assert _attempts(ledger) == [], "no attempt row: the on-chain lane keeps the transaction"
    assert sender.raws == [] and net.calls == []
    assert ledger.execute("SELECT count(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def test_a_direct_wallet_paid_buy_still_reaches_the_hook(entry):
    built, sender, net, ledger = entry
    rec = _go(built, _payload())
    assert rec["reason"] != "relay_origin_unproved"
    assert [a["signal_source"] for a in _attempts(ledger)] == ["rh_fast_lane"]
