"""A signed de-risk that can never land must not wedge a funded position for 20 minutes.

Live on 2026-08-12, position 36517 (MOONDENG, solana): the runner signed a 50% de-risk at 17:59:24,
the transaction never landed, and its blockhash expired. `claim_position_close` refuses while
`derisk_sig` is set, so the position could not be exited AT ALL — not by the rails, not by the
operator's close button — and it sat wedged at +125% until it was reconciled by hand.

`DERISK_REARM_MIN_AGE_S` (20 min) is a PROXY for "the blockhash window is long gone". The exact
expiry is already on the intent as `last_valid_height`, and a Solana blockhash dies after ~150 slots
(~60 s), so the proxy waits about 20x longer than the fact it stands in for.
"""
import json

from sniper.services.memecoin_real_runner import derisk_provably_dead

SNAPSHOT = 234802.719352
LAST_VALID = 416911281


def _pos(**over):
    intent = {"last_valid_height": LAST_VALID, "balance_snapshot": SNAPSHOT}
    intent.update(over.pop("intent", {}))
    row = {"id": 36517, "mint": "CoYQmint", "exit_intent_json": json.dumps(intent)}
    row.update(over)
    return row


def test_past_the_window_with_the_full_bag_is_provably_dead():
    """Both facts hold: the chain is past last_valid_height AND nothing was sold."""
    assert derisk_provably_dead(
        _pos(), height_fn=lambda: LAST_VALID + 891, balance_fn=lambda m: SNAPSHOT) is True


def test_inside_the_window_is_never_decisive():
    """The sell may still land — re-arming here is the double-sell this guard exists to prevent."""
    assert derisk_provably_dead(
        _pos(), height_fn=lambda: LAST_VALID - 1, balance_fn=lambda m: SNAPSHOT) is False


def test_a_reduced_balance_means_the_sell_DID_land():
    """Expiry alone is not enough: a lagging node answers not_found for a sell that landed."""
    assert derisk_provably_dead(
        _pos(), height_fn=lambda: LAST_VALID + 891,
        balance_fn=lambda m: SNAPSHOT / 2) is False


def test_missing_or_unreadable_evidence_fails_closed():
    assert derisk_provably_dead(_pos(exit_intent_json=None),
                                height_fn=lambda: LAST_VALID + 9,
                                balance_fn=lambda m: SNAPSHOT) is False
    assert derisk_provably_dead(_pos(intent={"last_valid_height": None}),
                                height_fn=lambda: LAST_VALID + 9,
                                balance_fn=lambda m: SNAPSHOT) is False
    assert derisk_provably_dead(_pos(), height_fn=lambda: None,
                                balance_fn=lambda m: SNAPSHOT) is False
    assert derisk_provably_dead(_pos(), height_fn=lambda: LAST_VALID + 9,
                                balance_fn=lambda m: None) is False


def test_a_raising_probe_never_re_arms():
    def boom(*a):
        raise RuntimeError("rpc down")
    assert derisk_provably_dead(_pos(), height_fn=boom, balance_fn=lambda m: SNAPSHOT) is False
    assert derisk_provably_dead(_pos(), height_fn=lambda: LAST_VALID + 9, balance_fn=boom) is False
