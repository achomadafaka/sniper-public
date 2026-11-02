"""Pre-entry Solana token-safety gate tests."""

import pytest

from sniper.execution import solana_token_safety as ts


_CLEAN = {"result": {"value": {
    "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "data": {"parsed": {"type": "mint", "info": {
        "freezeAuthority": None,
        "mintAuthority": None,
        "decimals": 6,
    }}},
}}}


def _http_returning(payload):
    def http(method, url, headers=None, body=None, timeout=10):
        return 200, payload, None
    return http


def _token_2022(*, older=300, newer=300, authority=None, delegate=None):
    extensions = [{
        "extension": "transferFeeConfig",
        "state": {
            "olderTransferFee": {
                "epoch": 1015,
                "maximumFee": 1_000_000_000_000_000_000,
                "transferFeeBasisPoints": older,
            },
            "newerTransferFee": {
                "epoch": 1015,
                "maximumFee": 1_000_000_000_000_000_000,
                "transferFeeBasisPoints": newer,
            },
            "transferFeeConfigAuthority": authority,
            "withdrawWithheldAuthority": "WithdrawAuth",
            "withheldAmount": 0,
        },
    }]
    if delegate is not None:
        extensions.append({
            "extension": "permanentDelegate",
            "state": {"delegate": delegate},
        })
    return {"result": {"value": {
        "owner": ts.TOKEN_2022_PROGRAM,
        "data": {"parsed": {"type": "mint", "info": {
            "freezeAuthority": None,
            "mintAuthority": None,
            "decimals": 9,
            "extensions": extensions,
        }}},
    }}}


def test_rejects_live_freeze_authority():
    payload = {"result": {"value": {
        "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "data": {"parsed": {"type": "mint", "info": {
            "freezeAuthority": "SomeAuth1111",
            "mintAuthority": None,
            "decimals": 6,
        }}},
    }}}
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300, http=_http_returning(payload))
    assert ok is False and "freeze" in reason


def test_rejects_live_mint_authority():
    payload = {"result": {"value": {
        "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "data": {"parsed": {"type": "mint", "info": {
            "freezeAuthority": None,
            "mintAuthority": "Minter11111",
            "decimals": 6,
        }}},
    }}}
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300, http=_http_returning(payload))
    assert ok is False and "mint" in reason


def test_accepts_immutable_transfer_fee_at_limit():
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300,
        http=_http_returning(_token_2022()),
    )
    assert ok is True
    assert "transfer_fee_bps=300" in reason
    assert "fee_limit_bps=300" in reason
    assert "fee_authority_immutable=true" in reason



def test_rejects_transfer_fee_over_limit():
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300,
        http=_http_returning(_token_2022(newer=301)),
    )
    assert ok is False
    assert "301 bps over 300 bps limit" in reason


# --- a live fee authority cannot reach us inside a hold (2026-09-06) ---------------------------
# A token-2022 fee change is written as `newerTransferFee` stamped with a FUTURE epoch; the older
# value applies until that boundary, and a Solana epoch is about 2 days. Our holds are minutes to
# hours. Measured that day across 30 refused mints: none had ever raised its fee, ten sit at 3% and
# one at 1%, and the two that triggered four refusals in three minutes (TOA, BUTT) share ONE
# authority -- a launchpad taking a creator fee.

def test_a_live_fee_authority_within_the_limit_is_admitted():
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300,
        http=_http_returning(_token_2022(older=300, newer=300, authority="FeeAuth")),
        allow_live_fee_authority=True,
    )
    assert ok is True
    # the ledger must be able to tell this apart from a genuinely immutable fee afterwards
    assert "fee_authority_immutable=false" in reason
    assert "transfer_fee_bps=300" in reason


def test_a_live_fee_authority_over_the_limit_is_still_refused():
    """The relaxation is about WHEN a fee can change, never about how high it already is."""
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300,
        http=_http_returning(_token_2022(older=301, newer=301, authority="FeeAuth")),
        allow_live_fee_authority=True,
    )
    assert ok is False
    assert "301 bps over 300 bps limit" in reason


def test_the_old_refusal_is_one_setting_away():
    """`MEMECOIN_SOLANA_ALLOW_LIVE_TRANSFER_FEE_AUTHORITY=0` restores the previous gate exactly."""
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300,
        http=_http_returning(_token_2022(authority="FeeAuth")),
        allow_live_fee_authority=False,
    )
    assert ok is False
    assert reason == "live token-2022 transfer-fee authority"


def test_a_permanent_delegate_is_refused_even_with_the_relaxation_on():
    """A permanent delegate needs no epoch: it can take the balance now. That rug is not this one."""
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300,
        http=_http_returning(_token_2022(authority="FeeAuth", delegate="Dele")),
        allow_live_fee_authority=True,
    )
    assert ok is False
    assert "permanent delegate" in reason


def test_freeze_and_mint_authority_are_untouched_by_the_relaxation():
    for field in ("freezeAuthority", "mintAuthority"):
        payload = _token_2022(authority="FeeAuth")
        payload["result"]["value"]["data"]["parsed"]["info"][field] = "Live1111"
        ok, reason = ts.token_safety_ok(
            "M", "rpc", max_transfer_fee_bps=300, http=_http_returning(payload),
            allow_live_fee_authority=True)
        assert ok is False, field
        assert ("freeze" in reason) or ("mint authority" in reason)


def test_an_unreadable_fee_schedule_still_fails_closed_with_the_relaxation_on():
    payload = _token_2022(authority="FeeAuth")
    payload["result"]["value"]["data"]["parsed"]["info"]["extensions"][0]["state"].pop(
        "newerTransferFee")
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300, http=_http_returning(payload),
        allow_live_fee_authority=True)
    assert ok is False and "unreadable" in reason


def test_rejects_permanent_delegate():
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300,
        http=_http_returning(_token_2022(delegate="Delegate")),
    )
    assert ok is False
    assert reason == "live token-2022 permanent delegate"


@pytest.mark.parametrize(("older", "newer"), [
    (None, 300),
    (300, None),
    ("bad", 300),
    (-1, 300),
    (300, 10_001),
])
def test_rejects_malformed_transfer_fee_schedule(older, newer):
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300,
        http=_http_returning(_token_2022(older=older, newer=newer)),
    )
    assert ok is False
    assert reason == "token-2022 transfer-fee schedule unreadable (fail-closed)"


def test_accepts_clean_token():
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300, http=_http_returning(_CLEAN))
    assert ok is True and reason == "ok"


# FOUR WAYS A READ CAN FAIL, AND THEY ARE NOT ONE (MCAT, solana, 2026-09-23T21:27:24Z, attempt
# 760c3fe9). `token_safety_ok` wrapped the RPC read AND the parse in one `except Exception`, so a
# transport failure, a JSON-RPC error body, an account the node cannot see yet and a genuinely
# unparseable mint all rendered as "mint account unreadable (fail-closed)" — a TOKEN verdict for
# three events that say nothing about the token. MCAT was clean: no mint authority, no freeze
# authority, no permanent delegate, a 300 bps fee against a 300 bps limit, and a live fee authority
# that MEMECOIN_SOLANA_ALLOW_LIVE_TRANSFER_FEE_AUTHORITY admits. We should have filled it.
#
# A NULL ACCOUNT IS ALWAYS OUR NODE BEHIND, never a missing mint: this check runs only because we
# are copying a BUY in that coin, so the mint provably exists or the source's transaction could not
# have executed. That is why it needs no age threshold to be called transient.
#
# None of these admits anything. All four still refuse; three of them become re-askable by the
# existing retry lane, which runs every gate again.


def test_a_transport_failure_is_named_as_one():
    def http(method, url, headers=None, body=None, timeout=10):
        raise RuntimeError("rpc down")

    ok, reason = ts.token_safety_ok("M", "rpc", max_transfer_fee_bps=300, http=http)
    assert ok is False
    assert reason == "mint read failed (rpc) — fail-closed"


def test_a_json_rpc_error_body_is_the_node_rejecting_us():
    """A 200 carrying an `error` object, which is where a rate limit lands."""
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300,
        http=_http_returning({"jsonrpc": "2.0", "id": 1,
                              "error": {"code": -32429, "message": "Too many requests"}}))
    assert ok is False
    assert reason == "mint read rejected by node (fail-closed)"


def test_a_null_account_is_our_node_behind_not_a_bad_token():
    """MCAT's own shape. The reach value is `value: null` with a well-formed envelope around it:
    give the envelope an account and the branch is unreachable."""
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300,
        http=_http_returning({"jsonrpc": "2.0", "id": 1, "result": {"value": None}}))
    assert ok is False
    assert reason == "mint account not visible yet (fail-closed)"


def test_an_account_we_cannot_parse_stays_terminal():
    """The one case that IS about the token, so it must not join the transient set."""
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300,
        http=_http_returning({"result": {"value": {"owner": "x", "data": ["base64blob", "base64"]}}}))
    assert ok is False
    assert reason == "mint account unparseable (fail-closed)"


def test_the_three_read_failures_re_ask_and_the_parse_failure_does_not():
    from sniper.services import memecoin_real_runner as R
    for reason in ("mint read failed (rpc) — fail-closed",
                   "mint read rejected by node (fail-closed)",
                   "mint account not visible yet (fail-closed)"):
        assert R._is_transient_entry_miss({"status": "unsafe", "reason": reason}), reason
    assert not R._is_transient_entry_miss(
        {"status": "unsafe", "reason": "mint account unparseable (fail-closed)"})


def test_mcat_itself_passes_on_its_real_mint_state():
    """Replayed from the chain, 2026-09-23: this is the coin the old catch refused."""
    mcat = {"result": {"value": {
        "owner": ts.TOKEN_2022_PROGRAM,
        "data": {"parsed": {"info": {
            "mintAuthority": None, "freezeAuthority": None,
            "supply": "1000000000000000", "decimals": 6,
            "extensions": [{"extension": "transferFeeConfig", "state": {
                "newerTransferFee": {"epoch": 1041, "maximumFee": 1000000000000000,
                                     "transferFeeBasisPoints": 300},
                "olderTransferFee": {"epoch": 1041, "maximumFee": 1000000000000000,
                                     "transferFeeBasisPoints": 300},
                "transferFeeConfigAuthority": "WLHv2UAZm6z4KyaaELi5pjdbJh6RESMva1Rnn8pJVVh",
                "withdrawWithheldAuthority": "5KXDF6QnqhBj72hDtJNkkpFaQVUfbFXNybMsp3DiK6tD",
                "withheldAmount": 3538343392670}}]}}}}}}
    ok, reason = ts.token_safety_ok(
        "ESqDYnhwXTkeyiWs9HFvyykWWuxT2yz8VEZCs8fEU7Ra", "rpc", max_transfer_fee_bps=300,
        http=_http_returning(mcat), allow_live_fee_authority=True)
    assert ok is True, reason
    assert "transfer_fee_bps=300" in reason and "fee_authority_immutable=false" in reason


def test_the_mint_is_read_at_confirmed_not_the_finalized_default():
    # 2026-09-26 23:37Z, attempt 92dcc8ef: the slot-lane fallback asked for a seconds-old pump mint
    # 0.9 s after the chain failure and got null, because getAccountInfo with no commitment answers
    # at FINALIZED (~32 slots behind). The on-chain lane has no retry, so that was its only shot.
    import json
    bodies = []

    def http(method, url, body=None, **_kw):
        bodies.append(json.loads(body))
        return 200, {"result": {"value": None}}, None

    ts.read_mint_account("MintX", "https://rpc", http)
    assert bodies[0]["params"][1] == {"encoding": "jsonParsed", "commitment": "confirmed"}
    bodies.clear()
    try:
        ts._mint_account("MintX", "https://rpc", http)
    except (KeyError, TypeError):
        pass
    assert bodies[0]["params"][1] == {"encoding": "jsonParsed", "commitment": "confirmed"}
