"""A sell the node never admitted never moves the nonce, so proof one can never fire.

`evm_close_provably_dead` originally required the MINED nonce to be past the intent nonce. A
transaction refused before the mempool -- because `gasLimit * gasPrice` exceeds the sender balance
-- is never admitted, never mines, and never moves that nonce, so the claim was held forever and a
human had to free the bag. PRC 49252 (2026-09-03) is the case: latest == pending == 1335 == the
intent nonce, receipt null and hash unknown on all three endpoints, full bag held. It was pinned
through its own take-profit and closed 31 minutes later at -$7.46 against $122.12 projected.

The second proof releases on four facts, and the release is safe because the re-sell reuses that
same nonce: two transactions sharing one nonce can never both mine.
"""
import base64
import pathlib

import pytest

# Imported inside the helpers, never at module scope: importing the runner pulls in
# sniper.settings.memecoin, whose LI.FI activation check runs at import. At collection time that
# happens BEFORE the fixtures that set the env, and it poisoned five unrelated context-build
# tests in the same run while passing in isolation.

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "prc_49252_exit_payload.b64"
PRC_NONCE, PRC_SNAPSHOT = 1335, 3282664.3126828927
NATIVE_ETH = 0.00276                      # custody 0x898BE9c2…3A1d when PRC was signed


def _rlp(item):
    if isinstance(item, int):
        item = b"" if item == 0 else item.to_bytes((item.bit_length() + 7) // 8, "big")
    if len(item) == 1 and item[0] < 0x80:
        return item
    if len(item) < 56:
        return bytes([0x80 + len(item)]) + item
    length = len(item).to_bytes((len(item).bit_length() + 7) // 8, "big")
    return bytes([0xb7 + len(length)]) + length + item


def _signed_legacy(nonce, gas_price, gas_limit, value=0):
    """A minimal signed legacy tx with real field positions — enough for the cost decoder."""
    body = b"".join([_rlp(nonce), _rlp(gas_price), _rlp(gas_limit), _rlp(b"\x11" * 20),
                     _rlp(value), _rlp(b""), _rlp(27), _rlp(b"\x22" * 32), _rlp(b"\x33" * 32)])
    payload = (bytes([0xf7 + len(len(body).to_bytes((len(body).bit_length() + 7) // 8, "big"))])
               + len(body).to_bytes((len(body).bit_length() + 7) // 8, "big") + body
               if len(body) >= 56 else bytes([0xc0 + len(body)]) + body)
    return base64.b64encode(("0x" + payload.hex()).encode()).decode()


def _decode(payload):
    from sniper.execution.evm_real_seams import decode_signed_tx_cost
    return decode_signed_tx_cost(payload)


def _dead(pos, **kw):
    from sniper.services.memecoin_real_runner import evm_close_provably_dead
    return evm_close_provably_dead(pos, **kw)


def _pos(payload_b64, *, nonce=PRC_NONCE, snapshot=PRC_SNAPSHOT,
         submitted_at="2026-09-03T15:18:51+00:00"):
    import json
    return {"id": 49252, "mint": "0x1beae5e9cee0201c52c6e47935c91f3aaad28a69",
            "close_submitted_at": submitted_at,
            "exit_intent_json": json.dumps({"nonce": nonce, "balance_snapshot": snapshot,
                                            "signed_payload_b64": payload_b64})}


def _call(pos, *, mined=PRC_NONCE, pending=PRC_NONCE, bag=PRC_SNAPSHOT, native=NATIVE_ETH,
          now="2026-09-03T15:25:00+00:00"):
    return _dead(
        pos, confirmed_nonce_fn=lambda: mined, balance_fn=lambda _m: bag,
        pending_nonce_fn=lambda: pending, native_balance_fn=lambda: native, now_iso=now)


# ---------------------------------------------------------------- the decoder, on real bytes

def test_the_real_prc_payload_decodes_to_the_measured_cost():
    cost = _decode(FIXTURE.read_text().strip())
    assert cost["nonce"] == PRC_NONCE
    assert cost["gas_limit"] == 1_000_000
    # 1,000,000 x 4.798 gwei = 0.004798 ETH against 0.00276 held: refused before the mempool.
    assert round(cost["worst_case_wei"] / 1e18, 6) == 0.004798
    assert cost["worst_case_wei"] / 1e18 > NATIVE_ETH


def test_an_undecodable_payload_never_raises():
    assert _decode("not base64 at all") is None
    assert _decode(None) is None


# ---------------------------------------------------------------- (a) the PRC case must release

def test_prc_releases_and_names_the_gas_cause():
    verdict = _call(_pos(FIXTURE.read_text().strip()))
    assert verdict, "PRC 49252 must release: nonce free, nothing pending, full bag"
    assert "never_admitted_gas" in verdict
    assert "1335" in verdict


# ---------------------------------------------------------------- (e) affordable == other cause

def test_an_affordable_sell_is_labelled_evicted_not_gas():
    """The 2026-09-02 ASS/RIZZLER cluster needed ~0.0006 ETH against 0.00276 held — affordable, so
    it is the evicted-or-dropped class and must not be blamed on gas."""
    payload = _signed_legacy(PRC_NONCE, 658_000_000, 900_000)      # 0.000592 ETH
    assert _decode(payload)["worst_case_wei"] / 1e18 < NATIVE_ETH
    verdict = _call(_pos(payload))
    assert verdict and "evicted_unmined" in verdict


# ---------------------------------------------------------------- (b)(c)(d) must NOT release

def test_a_node_still_holding_the_sell_blocks_release():
    """pending > latest: something of ours is queued and may yet mine."""
    assert _call(_pos(FIXTURE.read_text().strip()), pending=PRC_NONCE + 1) is None


def test_a_young_submission_blocks_release():
    """Inside the window where a slow node's not_found may simply be a receipt not yet visible."""
    assert _call(_pos(FIXTURE.read_text().strip()),
                 now="2026-09-03T15:19:20+00:00") is None          # 29s old


def test_a_short_bag_blocks_release():
    """A landed partial reduced the bag: this sell is not provably absent."""
    assert _call(_pos(FIXTURE.read_text().strip()), bag=PRC_SNAPSHOT * 0.5) is None


def test_an_unreadable_submitted_stamp_blocks_release():
    assert _call(_pos(FIXTURE.read_text().strip(), submitted_at=None)) is None


# ---------------------------------------------------------------- fail-closed on every read

@pytest.mark.parametrize("kind", ["pending", "balance", "mined"])
def test_a_failed_read_never_releases(kind):
    def boom(*a, **k):
        raise RuntimeError("EVM RPC provider HTTP failure")
    pos = _pos(FIXTURE.read_text().strip())
    kwargs = dict(confirmed_nonce_fn=lambda: PRC_NONCE, balance_fn=lambda _m: PRC_SNAPSHOT,
                  pending_nonce_fn=lambda: PRC_NONCE, native_balance_fn=lambda: NATIVE_ETH,
                  now_iso="2026-09-03T15:25:00+00:00")
    kwargs[{"pending": "pending_nonce_fn", "balance": "balance_fn",
            "mined": "confirmed_nonce_fn"}[kind]] = boom
    assert _dead(pos, **kwargs) is None


def test_an_unbound_pending_nonce_fn_holds_the_claim():
    """A context without the new binding must behave exactly as before, not release blindly."""
    pos = _pos(FIXTURE.read_text().strip())
    assert _dead(
        pos, confirmed_nonce_fn=lambda: PRC_NONCE, balance_fn=lambda _m: PRC_SNAPSHOT) is None


def test_an_unreadable_native_balance_still_releases_as_evicted():
    """The cause LABEL is best-effort; it must never decide whether the claim is released."""
    verdict = _call(_pos(FIXTURE.read_text().strip()), native=None)
    assert verdict and "evicted_unmined" in verdict


# ---------------------------------------------------------------- proof one still works

def test_the_original_spent_nonce_proof_is_unchanged():
    verdict = _call(_pos(FIXTURE.read_text().strip()), mined=PRC_NONCE + 3, pending=PRC_NONCE + 3)
    assert verdict and "spent by another transaction" in verdict


def test_a_spent_nonce_with_a_reduced_bag_does_not_release():
    assert _call(_pos(FIXTURE.read_text().strip()), mined=PRC_NONCE + 3,
                 pending=PRC_NONCE + 3, bag=1.0) is None
