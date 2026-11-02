"""Negative evidence needs a trusted witness; positive evidence must stay available.

Peer review 2026-08-25, in two rounds.

Round 1 found the hole: `getSignatureStatuses` is in RotatingSolanaRpcHttp's rotatable set
(solana_rpc_pool.py:52) and the funded Solana leg installs that pool, so on a -32429 a recovery read
silently moves to another endpoint. `searchTransactionHistory=true` only searches the history the
ANSWERING node kept, and a null is what releases a reservation. Two nodes each answering null is not
the evidence the two-spaced-reads rule collects — that rule guards one lagging node re-answering, not
a change of witness.

Round 2 found that pinning EVERY verdict fixed that at too coarse a granularity. confirmed, failed
and pending are POSITIVE evidence: the answering node saw the transaction. Those drive orphan
detection, and an orphan is funds already at risk. Pinning them meant a custody outage stopped all
recovery — and stuck reservations correlate with flaky submission rather than being independent of
it. So the pool keeps serving status, and only a not_found is re-read through the pinned client.
"""
from sniper.execution.solana_real_seams import build_real_solana_executor
from sniper.services import memecoin_real_runner as R


class _Keypair:
    def pubkey(self):
        return "PubkeyForTest1111111111111111111111111111111"


def _resp(value):
    # the http seam contract is (status, parsed_json, raw) -- see solana_real_seams._rpc_call
    return 200, {"jsonrpc": "2.0",
                 "result": {"context": {"slot": 441_682_717}, "value": [value]}, "id": 1}, None


_RATE_LIMITED = (429, {"jsonrpc": "2.0",
                       "error": {"code": -32429, "message": "rate limited"}, "id": 1}, None)
_LANDED = {"err": None, "confirmationStatus": "confirmed"}


def _executor(http, proof_http):
    return build_real_solana_executor(
        keypair=_Keypair(), rpc_url="https://custody.example/rpc",
        http=http, proof_http=proof_http, usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0)


def test_the_pooled_read_serves_status_and_keeps_its_failover():
    """`confirm_once_fn` must NOT be pinned: orphan detection has to survive a custody outage."""
    seen = []

    def pooled(method, url, **kw):
        seen.append("pooled")
        return _resp(_LANDED)

    ex = _executor(pooled, lambda *a, **k: _RATE_LIMITED)
    assert ex["confirm_once_fn"]("SigLanded") == "confirmed"
    assert seen == ["pooled"]


def test_the_proof_read_uses_the_unrotated_client():
    seen = []
    ex = _executor(lambda *a, **k: (seen.append("pooled"), _resp(None))[1],
                   lambda *a, **k: (seen.append("direct"), _resp(None))[1])
    assert ex["confirm_proof_once_fn"]("SigPinned") == "not_found"
    assert seen == ["direct"], seen


def test_the_proof_read_falls_back_when_no_pool_is_installed():
    """Single-endpoint deploys install no pool, so nothing rotates and http IS the direct client."""
    seen = []
    ex = _executor(lambda *a, **k: (seen.append("only"), _resp(None))[1], None)
    assert ex["confirm_proof_once_fn"]("SigPinned") == "not_found"
    assert seen == ["only"]


def test_a_rate_limited_proof_read_is_unavailable_not_not_found():
    """Fail-closed: an endpoint that refuses must never look like an absent signature."""
    ex = _executor(lambda *a, **k: _resp(None), lambda *a, **k: _RATE_LIMITED)
    assert ex["confirm_proof_once_fn"]("SigPinned") == "unavailable"


# --- the recovery-level contract -------------------------------------------------------------

def _recover(confirm, proof, **kw):
    calls = {"confirm": 0, "proof": 0}

    def confirm_for(_n, _s):
        calls["confirm"] += 1
        return confirm

    def confirm_proof_for(_n, _s):
        calls["proof"] += 1
        return proof

    return calls, confirm_for, confirm_proof_for


def test_only_not_found_pays_for_the_pinned_reread():
    """One extra RPC call on the release path only; every other verdict costs nothing."""
    import sqlite3
    from sniper.persistence import db_memecoin as m
    for verdict in ("confirmed", "failed", "pending", "unavailable"):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        m.ensure_memecoin_tables(c)
        pid = m.reserve_pending(c, mint="MintA", source_user_id="u1", strategy="alpha_b",
                                notional=23.0, reserved_at="2026-08-25T17:00:00+00:00",
                                network_id="solana")
        c.execute("UPDATE memecoin_pending_entries SET tx_sig='Sig1', broadcast_at=?, "
                  "submission_last_valid_height=1 WHERE id=?",
                  ("2026-08-25T17:00:00+00:00", pid))
        c.commit()
        calls, cf, cpf = _recover(verdict, "not_found")
        R.recover_stale_reservations(c, confirm_for=cf, confirm_proof_for=cpf,
                                     now_iso="2026-08-25T17:20:00+00:00", max_age_s=600.0,
                                     height_for=lambda _n: 10, expiry_proof_s=0.0)
        assert calls["proof"] == 0, f"{verdict} should not pay for the pinned read"


def test_a_pinned_answer_overrides_a_pooled_not_found():
    """The pinned client is the witness of record for negative evidence, in both directions."""
    import sqlite3
    from sniper.persistence import db_memecoin as m
    for proof, expect_release in (("not_found", True), ("unavailable", False),
                                  ("pending", False), ("confirmed", False)):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        m.ensure_memecoin_tables(c)
        pid = m.reserve_pending(c, mint="MintB", source_user_id="u1", strategy="alpha_b",
                                notional=23.0, reserved_at="2026-08-25T17:00:00+00:00",
                                network_id="solana")
        c.execute("UPDATE memecoin_pending_entries SET tx_sig='Sig2', broadcast_at=?, "
                  "submission_last_valid_height=1 WHERE id=?",
                  ("2026-08-25T17:00:00+00:00", pid))
        c.commit()
        _calls, cf, cpf = _recover("not_found", proof)
        for now in ("2026-08-25T17:11:00+00:00", "2026-08-25T17:13:00+00:00",
                    "2026-08-25T17:25:00+00:00"):
            R.recover_stale_reservations(c, confirm_for=cf, confirm_proof_for=cpf, now_iso=now,
                                         max_age_s=600.0, height_for=lambda _n: 10,
                                         expiry_proof_s=90.0)
        status = c.execute("SELECT status FROM memecoin_pending_entries WHERE id=?",
                           (pid,)).fetchone()[0]
        assert (status == "failed") is expect_release, (proof, status)


def test_no_proof_router_leaves_the_pooled_answer_untouched():
    """EVM venues supply no pinned read; the old behaviour must be byte-identical."""
    import sqlite3
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="MintC", source_user_id="u1", strategy="alpha_b",
                            notional=23.0, reserved_at="2026-08-25T17:00:00+00:00",
                            network_id="solana")
    c.execute("UPDATE memecoin_pending_entries SET tx_sig='Sig3', broadcast_at=?, "
              "submission_last_valid_height=1 WHERE id=?",
              ("2026-08-25T17:00:00+00:00", pid))
    c.commit()
    _calls, cf, _cpf = _recover("not_found", None)
    for now in ("2026-08-25T17:11:00+00:00", "2026-08-25T17:13:00+00:00"):
        R.recover_stale_reservations(c, confirm_for=cf, confirm_proof_for=None, now_iso=now,
                                     max_age_s=600.0, height_for=lambda _n: 10,
                                     expiry_proof_s=90.0)
    assert c.execute("SELECT status FROM memecoin_pending_entries WHERE id=?",
                     (pid,)).fetchone()[0] == "failed"
