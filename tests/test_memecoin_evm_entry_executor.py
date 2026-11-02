"""Item 7a — EVM entry executor (S2-EVM), pure orchestration over injected seams. Mirrors the Solana
executor with two EVM-specific safety steps: an EXACT-AMOUNT approve() before the swap (never
unlimited), and a nonce write-ahead (the set_pending_sig analog stores tx hash + nonce BEFORE
broadcast so a crash is reconcilable and never double-submits). Fills price from the ERC-20 balance
delta, never the quote."""
import sqlite3
import threading
import time

import pytest

from sniper.execution import evm_swap_executor as evm_executor
from sniper.execution.evm_swap_executor import execute_entry_evm
from sniper.execution.submission_transport import sha256_hex
from sniper.persistence import db_memecoin as m


def _signed_raw(chain_id, nonce=1):
    """A real signed transaction: the EVM-08 intent fence never sends bytes without a chain id."""
    from eth_account import Account
    tx = {"type": 2, "chainId": chain_id, "nonce": nonce, "maxFeePerGas": 10,
          "maxPriorityFeePerGas": 0, "gas": 21000, "to": "0x" + "22" * 20, "value": 0, "data": b""}
    return "0x" + bytes(Account.sign_transaction(tx, b"\x03" * 32).raw_transaction).hex()


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _reserve(c, mint="0xtok", network_id="8453"):
    return m.reserve_pending(c, mint=mint, source_user_id="trader1", strategy="alpha_b",
                             notional=10.0, reserved_at="2026-07-09T00:00:00Z", network_id=network_id)


def _contract_reserve(c, *, pending_hash=None, post_peak=False, volume_v4_pool_id=None):
    attempt_id = "attempt-contract-4663"
    m.start_entry_attempt(
        c, attempt_id=attempt_id, decision_started_at="2026-07-09T00:00:00Z",
        network_id="4663", mint="0xtok",
    )
    contract = {
        "contract_version": 1,
        "identity": {"network_id": "4663", "mint": "0xtok"},
        "policy": {"policy_id": "micro_v1"},
    }
    if post_peak or volume_v4_pool_id:
        facts = {}
        if post_peak:
            facts.update({
                "peak_price": 4.0, "display_price": 2.0,
                "runup_pct": 100.0, "giveback_pct": 50.0,
            })
        if volume_v4_pool_id:
            facts["volume_v4_pool_id"] = volume_v4_pool_id
        contract["classification"] = {"facts": facts}
        if post_peak:
            contract["classification"]["momentum_phase"] = "post_peak"
    payload, digest = m.canonical_funded_json(contract)
    m.set_entry_attempt_funded_contract(
        c, attempt_id, funded_contract_json=payload, funded_contract_hash=digest,
        funded_contract_version=1,
    )
    pid = m.reserve_pending(
        c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
        notional=10.0, reserved_at="2026-07-09T00:00:00Z", network_id="4663",
        attempt_id=attempt_id, funded_contract_version=1,
        funded_contract_hash=pending_hash or digest, funded_contract_json=payload,
    )
    return pid, attempt_id


def _seq(vals):
    it = {"i": 0}

    def f(*_a, **_k):
        v = vals[min(it["i"], len(vals) - 1)]; it["i"] += 1
        return v
    return f


def _seams(*, quote=None, approve=True, confirm="confirmed", usdc=(100.0, 90.0), token=(0.0, 5.0),
           sign=("0xraw", "0xhash", 7), send=None):
    calls = {"approve": [], "sign": 0, "send": 0, "wrote": None}

    def approve_fn(spender, amount):
        calls["approve"].append((spender, amount)); return approve

    def sign_fn(tx):
        calls["sign"] += 1
        return sign

    def send_fn(raw):
        calls["send"] += 1
        return send

    return dict(
        quote_fn=lambda mint, usd: quote,
        approve_fn=approve_fn, sign_fn=sign_fn, send_fn=send_fn,
        confirm_fn=lambda h: confirm, usdc_fn=_seq(list(usdc)), token_fn=_seq(list(token)),
    ), calls


_QUOTE = {"sellAmount": "10000000", "transaction": {"to": "0xrouter", "data": "0xdead"},
          "issues": {"allowance": {"spender": "0xspender"}}}


@pytest.mark.parametrize('tampered', [False, True])
def test_proven_relay_wallet_minimum_can_reach_submission_under_the_copy_ceiling(monkeypatch, tampered):
    from test_memecoin_evm_relay_quote import _tenet_quote
    from sniper.execution import evm_relay_quote as relay
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT', 35.)
    quote, fixture = _tenet_quote()
    bound = relay.bind_relay_output_minimum(quote, guard_verified=True)
    if tampered:
        bound['transaction']['data'] = quote['transaction']['data']
    token = fixture['request']['buy']
    output = int(quote['buyAmount']) / 1e18
    c = _c()
    pid = m.reserve_pending(c, mint=token, source_user_id='trader1', strategy='alpha_b',
        notional=26., reserved_at='2026-10-06T20:30:00Z', network_id='4663', source_price=26. / output)
    deps, calls = _seams(quote=bound, usdc=(100., 74.), token=((0., 18), (output, 18)))
    result = execute_entry_evm(c, pending_id=pid, mint=token, network_id='4663', usd_amount=26.,
        strategy='alpha_b', source_user_id='trader1', now_iso='2026-10-06T20:30:01Z',
        sell_decimals=18, sell_token_usd=2600., **deps)
    assert result['status'] == ('skipped_chase' if tampered else 'filled')
    assert calls['sign'] == calls['send'] == (0 if tampered else 1)
    if tampered:
        assert calls['approve'] == []
    c.close()


@pytest.mark.parametrize('minimum', ['6944444', '6000000', None, 'relay'])
def test_hard_copy_ceiling_blocks_expected_or_signed_premium_before_approval(monkeypatch, minimum):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT', 35., raising=False)
    c = _c()
    pid = m.reserve_pending(c, mint='0xtok', source_user_id='trader1', strategy='alpha_b',
        notional=10., reserved_at='2026-10-06T19:07:01Z', network_id='56', source_price=1.)
    q = {**_QUOTE, 'buyAmount': '6944444' if minimum=='6944444' else '9000000'}
    if minimum == 'relay':
        q.update(minBuyAmount='8500000', route={'protocol':'relay_same_chain'})
    elif minimum is not None: q['minBuyAmount'] = minimum
    seams, calls = _seams(quote=q, token=((0., 6), (5., 6)))
    result = execute_entry_evm(c, pending_id=pid, mint='0xtok', network_id='56', usd_amount=10.,
        strategy='alpha_b', source_user_id='trader1', now_iso='t', **seams)
    assert result['status'] == 'skipped_chase'
    assert 'copy price' in result['reason']
    assert not calls['approve'] and calls['sign'] == calls['send'] == 0
    c.close()


def test_an_unsent_price_refusal_keeps_the_fast_lanes_wallet_snapshot(tmp_path, monkeypatch):
    from sniper.execution import exec_lock
    monkeypatch.setenv("MEMECOIN_REAL_QUOTE_OVER_SOURCE_MAX_PCT_4663", "30")
    c = _c()
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-07-09T00:00:00Z",
                            network_id="4663", source_price=1.0)
    seams, calls = _seams(quote={**_QUOTE, "buyAmount": "5000000"},
                          token=((0.0, 6), (5.0, 6)))
    execute = exec_lock.serialize_executor(execute_entry_evm, "evm-4663-test",
                                           lock_dir=str(tmp_path))
    result = execute(c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
                     strategy="alpha_b", source_user_id="trader1", now_iso="t", **seams)
    assert result["status"] == "skipped_chase"
    assert calls["approve"] == [] and calls["sign"] == calls["send"] == 0
    assert exec_lock.exec_generation("evm-4663-test", lock_dir=str(tmp_path)) == 0.0


class _TraceRecorder:
    def __init__(self):
        self.substages = []
        self.stages = {}
        self.submissions = []

    def mark_substage(self, name, **metadata):
        self.substages.append((name, metadata))
        return True

    def mark_submission(self, stage, _attempt, **_metadata):
        self.submissions.append((stage, _attempt))
        self.stages[stage] = "2026-07-09T00:00:01+00:00"

    def mark(self, stage):
        self.stages[stage] = "2026-07-09T00:00:01+00:00"

    def stage_completed_at(self, stage):
        return self.stages.get(stage)

    def reconciled_now(self):
        return "2026-07-09T00:00:01+00:00"


class _QuoteTimeTrace(_TraceRecorder):
    """Give each exact quote its own corrected receipt time."""
    def __init__(self, quote_times):
        super().__init__()
        self.quote_times = iter(quote_times)

    def mark_submission(self, stage, _attempt, **_metadata):
        if stage == "quote":
            self.stages[stage] = next(self.quote_times)
        else:
            self.stages[stage] = "2026-07-09T00:00:01+00:00"


def test_filled_opens_real_evm_position_and_writes_ahead():
    c = _c(); pid = _reserve(c)
    seams, calls = _seams(quote=_QUOTE)
    r = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
                          **seams)
    assert r["status"] == "filled"
    pos = m.get_position(c, r["position_id"])
    assert pos["execution_mode"] == "real" and pos["network_id"] == "8453"
    assert abs(pos["entry_price"] - (10.0 / 5.0)) < 1e-9      # chain delta: $10 spent / 5 tokens
    # exact-amount approval for exactly the sell amount, and the nonce/hash written AHEAD of send
    assert calls["approve"] == [("0xspender", 10000000)]
    prow = m.pending_row(c, pid)
    assert prow["tx_sig"] == "0xhash" and prow["nonce"] == 7 and prow["status"] == "confirmed"
    assert prow["signed_payload_b64"] == "0xraw"
    assert prow["signed_payload_sha256"] == sha256_hex("0xraw")
    fill = c.execute("SELECT tx_sig FROM memecoin_fills WHERE position_id=?", (r["position_id"],)).fetchone()
    assert fill["tx_sig"] == "0xhash"


def test_signed_evm_payload_is_durable_before_send():
    c = _c(); pid = _reserve(c)
    seams, _calls = _seams(quote=_QUOTE)

    def send(raw):
        row = m.pending_row(c, pid)
        assert row["signed_payload_b64"] == raw
        assert row["signed_payload_sha256"] == sha256_hex(raw)
        return "0xhash"

    seams["send_fn"] = send
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        **seams,
    )

    assert result["status"] == "filled"


def test_exact_evm_quote_over_the_source_cap_never_signs():
    c = _c()
    pid = m.reserve_pending(
        c, mint="0xtok", source_user_id="trader1", strategy="alpha_b", notional=10.0,
        reserved_at="2026-07-09T00:00:00Z", network_id="4663", source_price=1.0,
    )
    quote = {**_QUOTE, "buyAmount": "5000000"}
    seams, calls = _seams(quote=quote, token=((0.0, 6), (5.0, 6)))

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        chase_max_pct=50.0, **seams,
    )

    assert result["status"] == "skipped_chase"
    assert result["quote_chase_pct"] == pytest.approx(100.0)
    assert calls["approve"] == [] and calls["sign"] == 0 and calls["send"] == 0
    assert m.pending_totals(c) == (0, 0.0)


def test_entry_preflight_refusal_releases_before_wallet_mutation():
    c = _c(); pid = _reserve(c, network_id="4663")
    seams, calls = _seams(quote=_QUOTE, token=((0.0, 6), (5.0, 6)))
    seen = []

    def entry_preflight(quote):
        seen.append(quote)
        return False, "entry_simulation_transfer_failed: exact route token transfer failed"

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=entry_preflight, **seams,
    )

    assert result["status"] == "unsafe_gate"
    assert result["reason"].startswith("entry_simulation_transfer_failed")
    assert len(seen) == 1 and seen[0]["transaction"] == _QUOTE["transaction"]
    assert calls["approve"] == [] and calls["sign"] == 0 and calls["send"] == 0
    assert m.pending_totals(c) == (0, 0.0)


def test_entry_preflight_requotes_min_out_before_a_wallet_mutation():
    c = _c(); pid = _reserve(c, network_id="4663")
    seams, calls = _seams(quote=None, token=((0.0, 6), (5.0, 6)))
    quotes = iter((
        {"sellAmount": "10000000", "buyAmount": "5", "transaction": {"to": "0xrouter", "data": "0xone"}, "issues": {}},
        {"sellAmount": "10000000", "buyAmount": "5", "transaction": {"to": "0xrouter", "data": "0xtwo"}, "issues": {}},
    ))
    slippages = []

    def quote_fn(_mint, _usd, *, slippage_bps=None):
        slippages.append(slippage_bps)
        return next(quotes)

    preflight = iter((
        (False, "entry_simulation_min_out: exact route minimum output reverted"),
        (True, "ok"),
    ))
    seams["quote_fn"] = quote_fn
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda _quote: next(preflight), retry_slippage_bps=800, **seams,
    )

    assert result["status"] == "filled"
    assert slippages == [None, 800]
    assert calls["approve"] == [] and calls["sign"] == 1 and calls["send"] == 1


def test_4663_parallel_safety_uses_retry_tolerance_on_first_quote():
    c = _c(); pid = _reserve(c, network_id="4663")
    seams, _calls = _seams(
        quote=None, approve={"pipelined": True}, token=((0.0, 6), (5.0, 6)))
    slippages = []

    def quote_fn(_mint, _usd, *, slippage_bps=None):
        slippages.append(slippage_bps)
        return _QUOTE

    seams["quote_fn"] = quote_fn
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        safety_gate_fn=lambda: (True, "ok"), retry_slippage_bps=800, **seams,
    )

    assert result["status"] == "filled"
    assert slippages == [800]


def test_entry_preflight_requotes_an_unknown_revert_once_at_normal_slippage():
    c = _c(); pid = _reserve(c, network_id="4663")
    seams, calls = _seams(quote=None, token=((0.0, 6), (0.0, 6), (5.0, 6)))
    quotes = iter((
        {"sellAmount": "10000000", "buyAmount": "5",
         "transaction": {"to": "0xrouter", "data": "0xone"}, "issues": {}},
        {"sellAmount": "10000000", "buyAmount": "5",
         "transaction": {"to": "0xrouter", "data": "0xtwo"}, "issues": {}},
    ))
    slippages = []

    def quote_fn(_mint, _usd, *, slippage_bps=None):
        slippages.append(slippage_bps)
        return next(quotes)

    preflight = iter((
        (False, "entry_simulation_reverted: exact route reverted; detail=0xdead"),
        (True, "ok"),
    ))
    seams["quote_fn"] = quote_fn
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda _quote: next(preflight), **seams,
    )

    assert result["status"] == "filled"
    assert slippages == [None, None]
    assert calls["sign"] == 1 and calls["send"] == 1


def test_repeated_unknown_entry_preflight_revert_stays_refused():
    c = _c(); pid = _reserve(c, network_id="4663")
    seams, calls = _seams(quote=None, token=((0.0, 6), (0.0, 6)))
    quotes = iter((
        {"sellAmount": "10000000", "buyAmount": "5",
         "transaction": {"to": "0xrouter", "data": "0xone"}, "issues": {}},
        {"sellAmount": "10000000", "buyAmount": "5",
         "transaction": {"to": "0xrouter", "data": "0xtwo"}, "issues": {}},
    ))
    slippages = []

    def quote_fn(_mint, _usd, *, slippage_bps=None):
        slippages.append(slippage_bps)
        return next(quotes)

    seams["quote_fn"] = quote_fn
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda _quote: (
            False, "entry_simulation_reverted: exact route reverted; detail=0xdead"),
        **seams,
    )

    assert result["status"] == "unsafe_gate"
    assert result["reason"].startswith("entry_simulation_reverted:")
    assert slippages == [None, None]
    assert calls["approve"] == [] and calls["sign"] == 0 and calls["send"] == 0
    assert m.pending_totals(c) == (0, 0.0)


def test_missing_entry_token_decimals_is_transient_and_never_signs():
    c = _c()
    pid = m.reserve_pending(
        c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
        notional=10.0, reserved_at="2026-07-09T00:00:00Z", network_id="4663",
    )
    seams, calls = _seams(quote={**_QUOTE, "buyAmount": "5000000"},
                          token=((0.0, None),))

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        **seams,
    )

    assert result["status"] == "aborted"
    assert result["reason"] == "token decimals unavailable"
    assert calls["approve"] == [] and calls["sign"] == 0 and calls["send"] == 0
    assert m.pending_totals(c) == (0, 0.0)


def test_v4_quote_from_another_pool_executes_when_the_route_is_valid():
    """A legacy provider-pool field does not reject a valid V4 route."""
    c = _c()
    provider_pool = "0x" + "11" * 32
    route_pool = "0x" + "22" * 32
    pid, _ = _contract_reserve(c, volume_v4_pool_id=provider_pool)
    quote = {**_QUOTE, "route": {"protocol": "uniswap_v4", "pool_id": route_pool}}
    seams, calls = _seams(quote=quote)

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        **seams,
    )

    assert result["status"] == "filled"
    assert calls["approve"] == [("0xspender", 10000000)]
    assert calls["sign"] == 1 and calls["send"] == 1


def test_v4_route_change_after_approval_executes_when_the_route_is_valid():
    """A fresh valid V4 route does not depend on a provider-pool field."""
    c = _c()
    provider_pool = "0x" + "11" * 32
    pid, _ = _contract_reserve(c, volume_v4_pool_id=provider_pool)
    matching = {**_QUOTE, "route": {"protocol": "uniswap_v4", "pool_id": provider_pool}}
    changed = {**_QUOTE, "route": {"protocol": "uniswap_v4", "pool_id": "0x" + "22" * 32}}
    seams, calls = _seams(quote=matching)
    seams["quote_fn"] = _seq([matching, changed])

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        **seams,
    )

    assert result["status"] == "filled"
    assert calls["approve"] == [("0xspender", 10000000)]
    assert calls["sign"] == 1 and calls["send"] == 1


def test_wallet_chase_cap_allows_a_final_quote_inside_the_event_window():
    """A 70% exact drift uses Koy's 75% cap only through the 60-second deadline."""
    c = _c()
    pid = m.reserve_pending(
        c, mint="0xtok", source_user_id="trader1", strategy="alpha_b", notional=10.0,
        reserved_at="2026-07-09T00:00:00Z", network_id="4663", source_price=1.0,
    )
    quote = {**_QUOTE, "buyAmount": "5882353", "issues": {}}
    seams, calls = _seams(quote=quote, token=((0.0, 6), (5.882353, 6)))

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:00Z",
        chase_max_pct=40.0, wallet_chase_cap_pct=75.0,
        wallet_chase_cap_expires_at="2026-07-09T00:01:00+00:00",
        trace=_QuoteTimeTrace(["2026-07-09T00:00:59+00:00"]), **seams,
    )

    assert result["status"] == "filled"
    assert calls["sign"] == 1


def test_wallet_chase_cap_expires_before_a_late_final_quote():
    """The same 70% quote falls back to the 40% chain cap after 60 seconds."""
    c = _c()
    pid = m.reserve_pending(
        c, mint="0xtok", source_user_id="trader1", strategy="alpha_b", notional=10.0,
        reserved_at="2026-07-09T00:00:00Z", network_id="4663", source_price=1.0,
    )
    quote = {**_QUOTE, "buyAmount": "5882353", "issues": {}}
    seams, calls = _seams(quote=quote, token=((0.0, 6), (5.882353, 6)))

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:00Z",
        chase_max_pct=40.0, wallet_chase_cap_pct=75.0,
        wallet_chase_cap_expires_at="2026-07-09T00:01:00+00:00",
        trace=_QuoteTimeTrace(["2026-07-09T00:01:01+00:00"]), **seams,
    )

    assert result["status"] == "skipped_chase"
    assert "cap 40%" in result["reason"]
    assert calls["sign"] == 0 and calls["send"] == 0


def test_wallet_chase_cap_is_rechecked_on_the_post_approval_quote():
    """A quote after the deadline cannot use the earlier 75% permission."""
    c = _c()
    pid = m.reserve_pending(
        c, mint="0xtok", source_user_id="trader1", strategy="alpha_b", notional=10.0,
        reserved_at="2026-07-09T00:00:00Z", network_id="4663", source_price=1.0,
    )
    quote = {**_QUOTE, "buyAmount": "5882353"}
    seams, calls = _seams(quote=quote, token=((0.0, 6), (0.0, 6)))

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:00Z",
        chase_max_pct=40.0, wallet_chase_cap_pct=75.0,
        wallet_chase_cap_expires_at="2026-07-09T00:01:00+00:00",
        trace=_QuoteTimeTrace([
            "2026-07-09T00:00:59+00:00", "2026-07-09T00:01:01+00:00",
        ]), **seams,
    )

    assert result["status"] == "skipped_chase"
    assert "cap 40%" in result["reason"]
    assert calls["sign"] == 0 and calls["send"] == 0


def test_pre_balance_reads_run_concurrently():
    c = _c(); pid = _reserve(c)
    usdc_started = threading.Event()
    token_started = threading.Event()
    usdc_calls = {"n": 0}
    token_calls = {"n": 0}

    def usdc_fn():
        usdc_calls["n"] += 1
        if usdc_calls["n"] == 1:
            usdc_started.set()
            assert token_started.wait(2.0)
            return 100.0
        return 90.0

    def token_fn(_mint):
        token_calls["n"] += 1
        if token_calls["n"] == 1:
            token_started.set()
            assert usdc_started.wait(2.0)
            return 0.0
        return 5.0

    seams, _calls = _seams(quote=_QUOTE)
    seams.update(usdc_fn=usdc_fn, token_fn=token_fn)
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        **seams,
    )
    assert result["status"] == "filled"


def test_post_balance_reads_run_concurrently():
    c = _c(); pid = _reserve(c)
    usdc_started = threading.Event()
    token_started = threading.Event()
    usdc_calls = {"n": 0}
    token_calls = {"n": 0}

    def usdc_fn():
        usdc_calls["n"] += 1
        if usdc_calls["n"] == 2:
            usdc_started.set()
            assert token_started.wait(2.0)
            return 90.0
        return 100.0

    def token_fn(_mint):
        token_calls["n"] += 1
        if token_calls["n"] == 2:
            token_started.set()
            assert usdc_started.wait(2.0)
            return 5.0
        return 0.0

    seams, _calls = _seams(quote=_QUOTE)
    seams.update(usdc_fn=usdc_fn, token_fn=token_fn)
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        **seams,
    )
    assert result["status"] == "filled"


def test_balance_reads_raise_usdc_error_without_waiting_for_token_timeout():
    token_started = threading.Event()
    token_release = threading.Event()

    def usdc_fn():
        assert token_started.wait(2.0)
        raise RuntimeError("usdc failed")

    def token_fn(_mint):
        token_started.set()
        token_release.wait(5.0)
        return 5.0

    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="usdc failed"):
            evm_executor._read_balances(usdc_fn, token_fn, "0xtok")
        assert time.monotonic() - started < 1.5
    finally:
        token_release.set()


def test_balance_reads_raise_token_error_after_usdc_result():
    def token_fn(_mint):
        raise RuntimeError("token failed")

    with pytest.raises(RuntimeError, match="token failed"):
        evm_executor._read_balances(lambda: 100.0, token_fn, "0xtok")


def test_4663_confirmed_copies_funded_contract_byte_for_byte_to_position():
    c = _c(); pid, attempt_id = _contract_reserve(c)
    seams, _calls = _seams(quote=_QUOTE)
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        **seams,
    )

    pending = dict(c.execute(
        "SELECT funded_contract_version,funded_contract_hash,funded_contract_json "
        "FROM memecoin_pending_entries WHERE id=?", (pid,),
    ).fetchone())
    attempt = dict(c.execute(
        "SELECT funded_contract_version,funded_contract_hash,funded_contract_json "
        "FROM memecoin_entry_attempts WHERE attempt_id=?", (attempt_id,),
    ).fetchone())
    position = dict(c.execute(
        "SELECT funded_contract_version,funded_contract_hash,funded_contract_json "
        "FROM memecoin_positions WHERE id=?", (result["position_id"],),
    ).fetchone())
    assert position == pending == attempt


def test_4663_confirmed_contract_mismatch_raises_and_keeps_reservation():
    c = _c(); pid, _attempt_id = _contract_reserve(c, pending_hash="0" * 64)
    seams, _calls = _seams(quote=_QUOTE)

    with pytest.raises(ValueError, match="funded contract"):
        execute_entry_evm(
            c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
            strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
            **seams,
        )

    assert m.pending_totals(c) == (1, 10.0)
    assert m.list_open_positions(c, mode="real") == []


def test_4663_exact_confirmed_post_peak_aborts_before_any_wallet_mutation():
    c = _c(); pid, _attempt_id = _contract_reserve(c, post_peak=True)
    quote = {
        **_QUOTE,
        "buyAmount": "5",
        "pre_transactions": [{"to": "0xweth", "data": "0x2e1a7d4d", "value": "0x0"}],
    }
    seams, calls = _seams(quote=quote, token=((0.0, 0), (5.0, 0)))
    trace = _TraceRecorder()

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        trace=trace, **seams,
    )

    assert result["status"] == "aborted"
    assert result["reason"] == "post_peak_confirmed"
    assert calls["approve"] == [] and calls["sign"] == 0 and calls["send"] == 0
    guard = [item for item in trace.substages if item[0] == "post_peak_confirmation"]
    assert guard and guard[0][1]["confirmed"] is True


def test_4663_post_peak_nomination_without_exact_price_keeps_existing_path():
    c = _c(); pid, _attempt_id = _contract_reserve(c, post_peak=True)
    seams, calls = _seams(quote=_QUOTE)

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        **seams,
    )

    assert result["status"] == "filled"
    assert calls["approve"] and calls["sign"] > 0 and calls["send"] == 1


def test_filled_entry_records_existing_call_substages_without_new_work():
    c = _c(); pid = _reserve(c)
    seams, _calls = _seams(quote=_QUOTE)
    trace = _TraceRecorder()

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        trace=trace, **seams,
    )

    assert result["status"] == "filled"
    # `evm.winner_refresh` is emitted on EVERY attempt from 2026-09-22 so the refresh outcome can
    # be counted from the ledger (ok / fresh / requote_none / requote_mismatch / no_requote). This
    # quote carries no `_requote`, so it records no_requote and costs no call -- the "without new
    # work" this test is named for is about CALLS, and the counts below still assert that.
    assert [name for name, _metadata in trace.substages] == [
        "evm.quote_initial", "evm.balance_pre", "chase_quote_confirmation", "evm.winner_refresh",
        "evm.approval", "evm.quote_post_approval", "evm.pre_transactions", "evm.swap_signing",
        "evm.broadcast", "evm.confirmation", "evm.balance_delta", "evm.persistence",
    ]


def test_trace_attempt_base_preserves_the_capacity_retry_history():
    c = _c(); pid = _reserve(c)
    seams, _calls = _seams(quote=_QUOTE)
    trace = _TraceRecorder()

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        trace=trace, trace_attempt_base=2, **seams,
    )

    assert result["status"] == "filled"
    assert {attempt for _stage, attempt in trace.submissions} == {2}
    assert {metadata["attempt_no"] for name, metadata in trace.substages
            if name.startswith("evm.")} == {2}


def test_no_approval_when_allowance_ok():
    c = _c(); pid = _reserve(c)
    q = {"sellAmount": "10000000", "transaction": {"to": "0xr"}, "issues": {"allowance": None}}
    seams, calls = _seams(quote=q)
    r = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="t", now_iso="t", **seams)
    assert r["status"] == "filled" and calls["approve"] == []   # already approved -> skip


def test_accepted_native_unwrap_pipelines_the_swap_without_a_receipt_wait():
    """GHOST: a confirmed unwrap before swap restores the 1.4 s native-route serial wait."""
    c = _c()
    pid = m.reserve_pending(
        c, mint="0xtok", source_user_id="trader1", strategy="alpha_b", notional=10.0,
        reserved_at="2026-07-09T00:00:00Z", network_id="4663",
    )
    pre = {"to": "0xweth", "data": "0x2e1a7d4d" + format(10, "064x"),
           "value": "0x0", "gas": "60000"}
    quote = {**_QUOTE, "pre_transactions": [pre]}
    seams, calls = _seams(quote=quote)
    confirmed = []
    seams["confirm_fn"] = lambda tx_hash: (confirmed.append(tx_hash) or "confirmed")
    pipelined = []

    def pipeline(pre_tx):
        pipelined.append(pre_tx)
        m.set_pending_broadcast(c, pid, "0xpre", 6, "2026-07-09T00:00:01Z")
        return {"status": "accepted", "tx_hash": "0xpre", "nonce": 6}

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        native_pre_pipeline_fn=pipeline, **seams,
    )

    assert result["status"] == "filled"
    assert pipelined == [pre]
    assert confirmed == ["0xhash"]
    assert calls["sign"] == 1 and calls["send"] == 1


def test_unavailable_native_pre_pipeline_keeps_the_sequential_unwrap_path():
    """GHOST: an unsupported unwrap must use the proved sequential path, not strand a reservation."""
    c = _c(); pid = _reserve(c)
    pre = {"to": "0xweth", "data": "0x2e1a7d4d" + format(10, "064x"),
           "value": "0x0", "gas": "60000"}
    quote = {**_QUOTE, "pre_transactions": [pre]}
    seams, calls = _seams(quote=quote)
    confirmations = []
    seams["confirm_fn"] = lambda tx_hash: (confirmations.append(tx_hash) or "confirmed")

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        native_pre_pipeline_fn=lambda _pre: None, **seams,
    )

    assert result["status"] == "filled"
    assert calls["sign"] == 2 and calls["send"] == 2
    assert confirmations == ["0xhash", "0xhash"]


def test_malformed_native_pre_pipeline_result_keeps_the_reservation():
    """GHOST: a bad pipeline seam must not crash after a signed native unwrap."""
    c = _c(); pid = _reserve(c)
    pre = {"to": "0xweth", "data": "0x2e1a7d4d" + format(10, "064x"),
           "value": "0x0", "gas": "60000"}
    seams, calls = _seams(quote={**_QUOTE, "pre_transactions": [pre]})

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        native_pre_pipeline_fn=lambda _pre: "bad result", **seams,
    )

    assert result["status"] == "unknown"
    assert calls["sign"] == 0 and calls["send"] == 0
    assert m.pending_totals(c) == (1, 10.0)


def test_approve_failure_aborts_and_releases():
    c = _c(); pid = _reserve(c)
    seams, calls = _seams(quote=_QUOTE, approve=False)
    r = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="t", now_iso="t", **seams)
    assert r["status"] == "aborted" and calls["send"] == 0
    assert m.pending_totals(c) == (0, 0.0)                       # reservation freed


def test_no_quote_aborts_and_releases():
    c = _c(); pid = _reserve(c)
    seams, _ = _seams(quote=None)
    r = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="t", now_iso="t", **seams)
    assert r["status"] == "aborted" and m.pending_totals(c) == (0, 0.0)


def test_failed_tx_releases():
    c = _c(); pid = _reserve(c)
    seams, _ = _seams(quote=_QUOTE, confirm="failed")
    r = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="t", now_iso="t", **seams)
    assert r["status"] == "failed" and m.pending_totals(c) == (0, 0.0)


def test_unknown_leaves_reserved_and_opens_nothing():
    c = _c(); pid = _reserve(c)
    seams, _ = _seams(quote=_QUOTE, confirm="unknown")
    r = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="t", now_iso="t", **seams)
    assert r["status"] == "unknown"
    assert m.pending_totals(c) == (1, 10.0)                      # still reserved (no double-submit)
    assert m.list_open_positions(c, mode="real") == []


def test_definite_broadcast_rejection_releases_without_waiting_for_confirmation():
    c = _c(); pid = _reserve(c)
    seams, calls = _seams(quote=_QUOTE, send=False)
    seams["confirm_fn"] = lambda _h: (_ for _ in ()).throw(
        AssertionError("a rejected tx was never submitted"))
    r = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
        strategy="alpha_b", source_user_id="t", now_iso="t", **seams)
    assert r["status"] == "failed" and r["reason"] == "broadcast_rejected"
    assert calls["send"] == 1 and m.pending_totals(c) == (0, 0.0)


def test_ambiguous_broadcast_still_checks_local_hash_before_preserving_reservation():
    c = _c(); pid = _reserve(c)
    seams, _ = _seams(quote=_QUOTE, send=None, confirm="unknown")
    r = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
        strategy="alpha_b", source_user_id="t", now_iso="t", **seams)
    assert r["status"] == "unknown"
    assert m.pending_totals(c) == (1, 10.0)


def test_confirmed_but_no_tokens_is_anomaly_not_a_fill():
    c = _c(); pid = _reserve(c)
    seams, _ = _seams(quote=_QUOTE, token=(0.0, 0.0))            # confirmed but got 0 tokens
    r = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="t", now_iso="t",
                          sleep_fn=lambda s: None, **seams)
    assert r["status"] == "anomaly"
    assert m.list_open_positions(c, mode="real") == []


def test_confirmed_lagged_token_read_rereads_before_anomaly():
    # a confirmed swap whose first post-balance read lags (delta 0) must re-read before
    # declaring anomaly — a real $5 Solana fill was booked 'anomaly' this exact way 2026-07-10.
    c = _c(); pid = _reserve(c)
    seams, _ = _seams(quote=_QUOTE, usdc=(100.0, 100.0, 90.0), token=(0.0, 0.0, 5.0))
    r = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="t", now_iso="t",
                          sleep_fn=lambda s: None, **seams)
    assert r["status"] == "filled"
    assert abs(r["fill_price"] - 2.0) < 1e-9                     # spent 10 / got 5
    assert len(m.list_open_positions(c, mode="real")) == 1


def test_confirmed_entry_uses_exact_receipt_transfer_after_balance_lag():
    c = _c(); pid = _reserve(c)
    seams, _ = _seams(
        quote=_QUOTE, usdc=(100.0, 90.0), token=(0.0, 0.0),
    )
    receipt_calls = []

    def receipt_token(tx_hash, mint):
        receipt_calls.append((tx_hash, mint))
        return 5.0, 18

    r = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
        strategy="alpha_b", source_user_id="t", now_iso="t",
        sleep_fn=lambda _s: None, receipt_token_fn=receipt_token, **seams,
    )

    assert r["status"] == "filled"
    assert receipt_calls == [("0xhash", "0xtok")]
    fill = c.execute(
        "SELECT qty,notional FROM memecoin_fills WHERE position_id=?",
        (r["position_id"],),
    ).fetchone()
    assert fill["qty"] == 5.0 and fill["notional"] == 10.0


def test_receipt_spend_fallback_converts_quote_units_to_usd():
    # 하루 (chain 56, 2026-09-23 11:32Z): the quote-balance read never moved, so `spent` fell
    # through to receipt_spent_fn, which answers in QUOTE-TOKEN units (0.0873 BNB). Booked
    # unconverted as $0.087, the ledger printed +$65.40 on a -$2.81 fill and the take-profit
    # fired 9 s later on a 775x phantom gain. The fallback must price the quote units in USD.
    c = _c(); pid = _reserve(c)
    seams, _ = _seams(quote=_QUOTE, usdc=(100.0, 100.0, 100.0, 100.0, 100.0), token=(0.0, 5.0))
    r = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
        strategy="alpha_b", source_user_id="t", now_iso="t",
        sleep_fn=lambda _s: None, sell_token_usd=1000.0,
        receipt_spent_fn=lambda _tx: 0.01, **seams,          # 0.01 BNB at $1,000 = $10
    )
    assert r["status"] == "filled"
    fill = c.execute(
        "SELECT qty,notional,price FROM memecoin_fills WHERE position_id=?",
        (r["position_id"],),
    ).fetchone()
    assert fill["qty"] == 5.0
    assert abs(fill["notional"] - 10.0) < 1e-9                  # USD, not 0.01 quote units
    assert abs(fill["price"] - 2.0) < 1e-9


# --- approval pipelining (2026-07-23): the approval wait was p50 2.8s of a 9.2s 4663 entry. A
#     pipelined approve broadcasts WITHOUT waiting for its receipt; the swap signs immediately at
#     nonce+1 (nonce order guarantees the approval mines first). The post-approval re-quote exists
#     only because of the wait, so pipelined mode skips it — the initial quote + post-peak check
#     (which run BEFORE the first mutation) remain the funded-contract gate. ---

def _pipelined_seams(**kw):
    seams, calls = _seams(**kw)
    quote_calls = {"n": 0}
    base_quote = seams["quote_fn"]

    def counting_quote(mint, usd):
        quote_calls["n"] += 1
        return base_quote(mint, usd)

    def pipelined_approve(spender, amount):
        calls["approve"].append((spender, amount))
        return {"pipelined": True, "nonce": 6, "tx_hash": "0xapprovehash"}

    seams["quote_fn"] = counting_quote
    seams["approve_fn"] = pipelined_approve
    return seams, calls, quote_calls


def test_pipelined_approval_skips_requote_and_fills():
    c = _c(); pid = _reserve(c)
    seams, calls, quote_calls = _pipelined_seams(quote=_QUOTE)
    trace = _TraceRecorder()
    r = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="t", now_iso="t", trace=trace, **seams)
    assert r["status"] == "filled"
    assert quote_calls["n"] == 1                        # NO post-approval re-quote
    assert calls["approve"] == [("0xspender", 10000000)]
    approval = [meta for name, meta in trace.substages if name == "evm.approval"]
    assert approval and approval[0]["outcome"] == "broadcast"
    assert "evm.quote_post_approval" not in [name for name, _ in trace.substages]


def test_pipelined_approval_post_peak_checked_once_on_initial_quote():
    # pipelined mode has no post-approval revision; the initial exact-quote post-peak check is the
    # funded-contract gate and must still fire (and still abort when confirmed).
    c = _c(); pid, _attempt_id = _contract_reserve(c, post_peak=True)
    quote = {**_QUOTE, "buyAmount": "5"}
    seams, calls, _qc = _pipelined_seams(quote=quote, token=((0.0, 0), (5.0, 0)))
    trace = _TraceRecorder()
    r = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="t", now_iso="t", trace=trace, **seams)
    assert r["status"] == "aborted" and r["reason"] == "post_peak_confirmed"
    assert calls["approve"] == [] and calls["send"] == 0
    guards = [meta for name, meta in trace.substages if name == "post_peak_confirmation"]
    assert len(guards) == 1 and guards[0]["confirmed"] is True


def test_pipelined_approval_broadcast_failure_still_aborts_and_releases():
    c = _c(); pid = _reserve(c)
    seams, calls = _seams(quote=_QUOTE, approve=False)   # approve_fn returns False either mode
    r = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="t", now_iso="t", **seams)
    assert r["status"] == "aborted" and calls["send"] == 0
    assert m.pending_totals(c) == (0, 0.0)


# --- parallel token-safety gate (2026-07-23): safety (p50 1.7s on 4663) is read-only and runs
#     concurrently with the quote; the executor must JOIN + CHECK it before the first wallet
#     mutation (approval / pre-tx / signing). Unsafe -> release + unsafe_gate, nothing mutated. ---

def test_safety_gate_unsafe_releases_and_never_mutates():
    c = _c(); pid = _reserve(c)
    seams, calls = _seams(quote=_QUOTE)
    r = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="t", now_iso="t",
                          safety_gate_fn=lambda: (False, "sell simulation failed: revert"), **seams)
    assert r["status"] == "unsafe_gate"
    assert r["reason"] == "sell simulation failed: revert"
    assert calls["approve"] == [] and calls["sign"] == 0 and calls["send"] == 0
    assert m.pending_totals(c) == (0, 0.0)               # reservation freed
    assert m.list_open_positions(c, mode="real") == []


def test_safety_gate_runs_after_quote_before_approval_and_fill_proceeds():
    c = _c(); pid = _reserve(c)
    order = []
    seams, _calls = _seams(quote=_QUOTE)
    base_quote = seams["quote_fn"]
    base_approve = seams["approve_fn"]
    seams["quote_fn"] = lambda mint, usd: (order.append("quote"), base_quote(mint, usd))[1]
    seams["approve_fn"] = lambda s, a: (order.append("approve"), base_approve(s, a))[1]

    def gate():
        order.append("gate")
        return (True, "ok")

    r = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="t", now_iso="t",
                          safety_gate_fn=gate, **seams)
    assert r["status"] == "filled"
    assert order.index("quote") < order.index("gate") < order.index("approve")


def test_safety_gate_called_once_across_minout_retry():
    c = _c(); pid = _reserve(c)
    gate_calls = {"n": 0}

    def gate():
        gate_calls["n"] += 1
        return (True, "ok")

    seams, _calls = _seams(quote=_QUOTE, usdc=(100.0, 100.0, 90.0), token=(0.0, 0.0, 5.0))
    confirms = iter(["failed", "confirmed"])
    signed = iter([("0xraw1", "0xhash1", 7), ("0xraw2", "0xhash2", 8)])
    seams["confirm_fn"] = lambda _h: next(confirms)
    seams["sign_fn"] = lambda _tx: next(signed)
    r = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="t", now_iso="t",
                          safety_gate_fn=gate, sleep_fn=lambda s: None,
                          revert_reason_fn=lambda _h: ("min_out", "Too little received"), **seams)
    assert r["status"] == "filled"
    assert gate_calls["n"] == 1
    row = m.pending_row(c, pid)
    assert row["tx_sig"] == "0xhash2"
    assert row["signed_payload_b64"] == "0xraw2"


def test_a_quote_45_percent_over_the_source_is_refused_on_4663_before_any_mutation(monkeypatch):
    """TWIN (57097, 2026-09-13): +45% over picadura passed the chase gate and the premium floor.
    MEMECOIN_REAL_QUOTE_OVER_SOURCE_MAX_PCT_4663=30 refuses it before approve/sign/send."""
    monkeypatch.setenv("MEMECOIN_REAL_QUOTE_OVER_SOURCE_MAX_PCT_4663", "30")
    c = _c()
    pid = m.reserve_pending(
        c, mint="0xtok", source_user_id="trader1", strategy="alpha_b", notional=10.0,
        reserved_at="2026-07-09T00:00:00Z", network_id="4663", source_price=1.0,
    )
    quote = {**_QUOTE, "buyAmount": "6896552"}          # 10 USD / 6.896552 tokens = $1.45
    seams, calls = _seams(quote=quote, token=((0.0, 6), (5.0, 6)))
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        chase_max_pct=50.0, **seams,
    )
    assert result["status"] == "skipped_chase" and "cap 30%" in result["reason"], result
    assert calls["approve"] == [] and calls["sign"] == 0 and calls["send"] == 0
    assert m.pending_totals(c) == (0, 0.0)


def test_exact_quote_safety_runs_again_after_approval_before_swap():
    c = _c(); pid = _reserve(c, network_id="5042")
    first = {**_QUOTE, "buyAmount": "100"}
    fresh = {**_QUOTE, "buyAmount": "200"}
    quotes = iter((first, fresh))
    seams, calls = _seams(quote=first, token=((0.0, 6), (5.0, 6)))
    seams["quote_fn"] = lambda *a, **kw: next(quotes)
    checked = []

    def quote_safety(quote):
        checked.append(int(quote["buyAmount"]))
        return (True, "ok") if checked[-1] == 100 else (False, "exit_unquotable_at_clip")

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="5042", usd_amount=10,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        quote_safety_fn=quote_safety, **seams,
    )
    assert checked == [100, 200]
    assert result["status"] == "unsafe_gate"
    assert calls["sign"] == calls["send"] == 0
    assert m.pending_totals(c) == (0, 0.0)


def test_a_confirmed_unwrap_with_no_visible_balance_never_broadcasts_and_is_rewrapped():
    """PAWSINU 0xbcc948e7 (2026-09-17 21:17:56Z): the unwrap confirmed and the swap, broadcast 3 ms
    later, was rejected — the admitting node had not yet seen the unwrap's block. The swap must not
    go out, and the landed native ETH must still be rewrapped."""
    c = _c()
    pid = m.reserve_pending(
        c, mint="0xtok", source_user_id="trader1", strategy="alpha_b", notional=10.0,
        reserved_at="2026-07-09T00:00:00Z", network_id="4663",
    )
    pre = {"to": "0xweth", "data": "0x2e1a7d4d" + format(10, "064x"),
           "value": "0x0", "gas": "60000"}
    quote = {**_QUOTE, "pre_transactions": [pre]}
    seams, calls = _seams(quote=quote)
    rewrapped = []

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        native_pre_pipeline_fn=lambda _pre: {"status": "not_visible", "tx_hash": "0xpre",
                                             "nonce": 6},
        cleanup_native_fn=lambda amount: (rewrapped.append(amount) or "confirmed"),
        **seams,
    )

    assert calls["send"] == 0, "the swap must never be broadcast"
    assert rewrapped == [10], "the confirmed unwrap must be rewrapped"
    # ⚠️ NOT "failed" (2026-09-23, LINTCHA 9021c685). The receipt confirmed, so the unwrap HAPPENED
    # -- the chain says success at block 70750681 -- and only a pool member's balance view lagged.
    # All 5 pre-transactions this system called ambiguous or failed in the 30 days to 2026-09-23
    # succeeded on chain; not one was a real failure, so the row must not assert one.
    assert result["status"] == "unknown"
    assert result["reason"] == "native pre-transaction ambiguous"
    assert "failed" not in result["reason"]


def test_an_unconfirmed_pre_transaction_is_not_rewrapped():
    """The other half of the distinction: with NO receipt the ETH is not known to exist, so it must
    not be rewrapped -- rewrapping a balance we do not have would spend real native gas on a
    transaction that reverts."""
    c = _c()
    pid = m.reserve_pending(
        c, mint="0xtok", source_user_id="trader1", strategy="alpha_b", notional=10.0,
        reserved_at="2026-07-09T00:00:00Z", network_id="4663",
    )
    pre = {"to": "0xweth", "data": "0x2e1a7d4d" + format(10, "064x"),
           "value": "0x0", "gas": "60000"}
    quote = {**_QUOTE, "pre_transactions": [pre]}
    seams, calls = _seams(quote=quote)
    rewrapped = []

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        native_pre_pipeline_fn=lambda _pre: {"status": "unknown", "tx_hash": "0xpre", "nonce": 6},
        cleanup_native_fn=lambda amount: (rewrapped.append(amount) or "confirmed"),
        **seams,
    )

    assert calls["send"] == 0
    assert rewrapped == [], "no receipt, no proof the ETH exists, no rewrap"
    assert result["status"] == "unknown"
    assert result["reason"] == "native pre-transaction ambiguous"


def _rejecting_rpc(message):
    """A node that positively rejects the raw transaction, in `_rpc`'s own answer shape."""
    def rpc(_url, _method, _params, http=None):
        return {"error": {"code": -32000, "message": message}}
    return rpc


def _reserve_4663(c):
    return m.reserve_pending(
        c, mint="0xtok", source_user_id="trader1", strategy="alpha_b", notional=10.0,
        reserved_at="2026-07-09T00:00:00Z", network_id="4663")


def test_a_rejected_broadcast_keeps_the_nodes_own_words(monkeypatch):
    """A bare `broadcast_rejected` cannot say whether the nonce, the gas or the balance refused the
    swap, and those have different repairs (PAWSINU, 2026-09-17).

    E8, 2026-09-23: this test used to HAND-SET the thread-local and never clear it, so it both
    asserted the defect and was the writer that leaked a message into the next test on the worker.
    It now drives the real `eth_send_raw`, which is the only code that has the node's words.
    """
    from sniper.execution import evm_real_seams as seams_mod
    c = _c(); pid = _reserve_4663(c)
    monkeypatch.setattr(seams_mod, "_rpc",
                        _rejecting_rpc("insufficient funds for gas * price + value"))
    seams, _calls = _seams(quote=_QUOTE, sign=(_signed_raw(4663), "0xhash", 7))
    seams["send_fn"] = lambda raw: seams_mod.eth_send_raw(raw, "http://rpc.invalid")

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z", **seams,
    )

    assert result["status"] == "failed"
    assert result["reason"] == "broadcast_rejected: insufficient funds for gas * price + value"


def test_a_transport_rejection_does_not_inherit_an_earlier_sends_words(monkeypatch):
    """E8, 2026-09-23, and this is the funded DEFAULT path. `_last_send_rejection` is written in
    one place, the classified-marker branch of `eth_send_raw`. With a submission transport
    configured -- `auto_free` on every funded chain -- the executor's `send_fn` is
    `make_evm_send_fn`, and `aggregate_to_seam` returns False for an EXPLICIT_REJECTED without
    ever touching that thread-local. The rejected entry then read whatever message the WORKER
    THREAD last set: an earlier entry, a different coin, possibly hours before.

    Absent is the correct answer there. We do not have the node's words on that path."""
    from sniper.execution import evm_real_seams as seams_mod
    monkeypatch.setattr(seams_mod, "_rpc", _rejecting_rpc("nonce too low"))
    assert seams_mod.eth_send_raw(_signed_raw(4663), "http://rpc.invalid") is False
    monkeypatch.undo()

    c = _c(); pid = _reserve_4663(c)
    seams, _calls = _seams(quote=_QUOTE, send=False)      # the transport shape: False, no words
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z", **seams,
    )
    assert result["status"] == "failed"
    assert result["reason"] == "broadcast_rejected", result["reason"]


def test_the_nodes_words_are_read_once_and_cannot_describe_a_later_send(monkeypatch):
    """Clearing on write alone still lets ONE later send inherit a message if it never reaches
    `eth_send_raw`. The consumer takes the message rather than peeking at it."""
    from sniper.execution import evm_real_seams as seams_mod
    c = _c(); pid = _reserve_4663(c)
    monkeypatch.setattr(seams_mod, "_rpc", _rejecting_rpc("intrinsic gas too low"))
    seams, _calls = _seams(quote=_QUOTE, sign=(_signed_raw(4663), "0xhash", 7))
    seams["send_fn"] = lambda raw: seams_mod.eth_send_raw(raw, "http://rpc.invalid")
    first = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z", **seams)
    assert first["reason"] == "broadcast_rejected: intrinsic gas too low", first
    assert seams_mod.last_send_rejection() is None, "the consumer must take it, not peek"


def test_an_ambiguous_send_clears_the_previous_rejection(monkeypatch):
    """`eth_send_raw`'s own transport-failure path must not leave an earlier rejection standing:
    the next caller would read a message describing a send that is not this one."""
    from sniper.execution import evm_real_seams as seams_mod
    monkeypatch.setattr(seams_mod, "_rpc", _rejecting_rpc("invalid sender"))
    assert seams_mod.eth_send_raw(_signed_raw(4663), "http://rpc.invalid") is False
    assert seams_mod.last_send_rejection() == "invalid sender"

    def boom(*_a, **_k):
        raise OSError("node unreachable")
    monkeypatch.setattr(seams_mod, "_rpc", boom)
    assert seams_mod.eth_send_raw(_signed_raw(4663), "http://rpc.invalid") is None
    assert seams_mod.last_send_rejection() is None


def test_a_preflight_revert_falls_through_to_the_next_ladder_hit():
    """MUSEFUND e31953ff / AGRIPPA e3fd121c (2026-09-22): the multihop winner reverted in
    simulation while relay had an in-budget quote and the trader's own fill went through Relay."""
    c = _c(); pid = _reserve(c, network_id="4663")
    seams, calls = _seams(quote=None, token=((0.0, 6), (5.0, 6)))
    relay_quote = {"sellAmount": "10000000", "buyAmount": "5",
                   "transaction": {"to": "0xrelay", "data": "0xrelay"}, "issues": {}}
    winner = {"sellAmount": "10000000", "buyAmount": "5",
              "transaction": {"to": "0xrouter", "data": "0xhooked"}, "issues": {},
              "_sniper_winner": "v4_multihop",
              "_alternates": [{"rung": "relay", "quote": relay_quote}]}
    quotes = []

    def quote_fn(_mint, _usd, *, slippage_bps=None):
        quotes.append(slippage_bps)
        return winner

    preflights = []

    def preflight(quote):
        preflights.append(quote["transaction"]["data"])
        if quote["transaction"]["data"] == "0xhooked":
            return (False, "entry_simulation_reverted: exact route reverted; detail=Call failed")
        return (True, "ok")

    signed = []
    seams["quote_fn"] = quote_fn
    seams["sign_fn"] = lambda tx: (signed.append(tx), ("0xraw", "0xhash", 7))[1]
    trace = _TraceRecorder()
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=preflight, trace=trace, **seams,
    )

    assert result["status"] == "filled", result
    assert preflights == ["0xhooked", "0xrelay"]           # the winner, then the runner-up
    assert quotes == [None]                                 # the ladder ran once
    assert calls["send"] == 1
    assert signed and signed[0]["to"] == "0xrelay"
    fallback = [m for n, m in trace.substages if n == "evm.entry_preflight_fallback"]
    assert fallback and fallback[0]["fallback_to"] == "relay" \
        and fallback[0]["fallback_from"] == "v4_multihop"


def test_every_hit_reverting_is_still_refused():
    c = _c(); pid = _reserve(c, network_id="4663")
    seams, calls = _seams(quote=None, token=((0.0, 6), (0.0, 6), (0.0, 6)))
    relay_quote = {"sellAmount": "10000000", "buyAmount": "5",
                   "transaction": {"to": "0xrelay", "data": "0xrelay"}, "issues": {}}
    winner = {"sellAmount": "10000000", "buyAmount": "5",
              "transaction": {"to": "0xrouter", "data": "0xhooked"}, "issues": {},
              "_sniper_winner": "v4_multihop",
              "_alternates": [{"rung": "relay", "quote": relay_quote}]}
    seams["quote_fn"] = lambda _mint, _usd, *, slippage_bps=None: winner
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda _q: (
            False, "entry_simulation_reverted: exact route reverted; detail=Call failed"),
        **seams,
    )
    assert result["status"] == "unsafe_gate"
    assert result["reason"].startswith("entry_simulation_reverted:")
    assert calls["sign"] == 0 and calls["send"] == 0


def test_an_unprovable_simulation_also_falls_through_to_the_next_hit():
    """A node that returns junk payload (0x0) is not a revert; once classified as rpc_unknown the
    winner still cannot be signed, and the runner-up with its own passing preflight fills."""
    c = _c(); pid = _reserve(c, network_id="4663")
    seams, calls = _seams(quote=None, token=((0.0, 6), (5.0, 6)))
    relay_quote = {"sellAmount": "10000000", "buyAmount": "5",
                   "transaction": {"to": "0xrelay", "data": "0xrelay"}, "issues": {}}
    winner = {"sellAmount": "10000000", "buyAmount": "5",
              "transaction": {"to": "0xrouter", "data": "0xhooked"}, "issues": {},
              "_sniper_winner": "v4_multihop",
              "_alternates": [{"rung": "relay", "quote": relay_quote}]}
    seams["quote_fn"] = lambda _mint, _usd, *, slippage_bps=None: winner
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda q: (
            (False, "entry_simulation_rpc_unknown: exact route simulation unavailable")
            if q["transaction"]["data"] == "0xhooked" else (True, "ok")),
        **seams,
    )
    assert result["status"] == "filled", result
    assert calls["sign"] == 1 and calls["send"] == 1


# ---------------------------------------------------------------------------------------------
# 2026-09-22: the winner is re-asked before the preflight simulates it. `row` 0xb30fcc1d passed its
# preflight at 21:40:21.35 and the chain reverted the transaction two seconds later, 115,763 gas
# for nothing. The lagging custody node was half of that staleness; the ladder's own 1.8-3.0 s is
# the other half, and the minimum is computed at the top of it.

def _aged_quote(age_s, requote, *, data="0xone", observed_at="2026-07-09T00:00:01+00:00"):
    """A winner's quote that the ladder handed back `age_s` seconds ago.

    TWO STAMPS, ONE REPRESENTATION EACH (E2, 2026-09-23). `_sniper_quote_monotonic_at` is the
    process-local float the staleness check reads; `_sniper_quote_observed_at` is ISO UTC and is
    what the chase window, attribution and everything persisted read. They used to be one key
    holding whichever type the writer happened to use.
    """
    import time as _t
    return {"sellAmount": "10000000", "buyAmount": "5",
            "transaction": {"to": "0xrouter", "data": data}, "issues": {},
            "_sniper_winner": "v4", "_sniper_quote_monotonic_at": _t.perf_counter() - age_s,
            "_sniper_quote_observed_at": observed_at,
            "_requote": requote}


def test_a_ladder_stamp_no_longer_closes_the_wallet_chase_window():
    """E2, 2026-09-23, and this one is money. `_effective_chase_cap` parses
    `_sniper_quote_observed_at` with `fromisoformat` to decide whether the quote falls inside the
    source event's window. The ladder wrote a monotonic FLOAT into that key, the parse raised, and
    the function returned the BASE cap -- so a wallet-scoped cap silently never applied to a
    single ladder quote. The two readings now live in two fields and neither can shadow the
    other."""
    def run(quote_extra):
        c = _c()
        pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                                notional=10.0, reserved_at="2026-07-09T00:00:00Z",
                                network_id="4663", source_price=1.0)
        quote = {"sellAmount": "10000000", "buyAmount": "5882353",
                 "transaction": {"to": "0xrouter", "data": "0xdead"}, "issues": {}}
        quote.update(quote_extra)
        seams, _calls = _seams(quote=quote, token=((0.0, 6), (5.882353, 6)))
        return execute_entry_evm(
            c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
            strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
            chase_max_pct=40.0, wallet_chase_cap_pct=75.0,
            wallet_chase_cap_expires_at="2026-07-09T00:01:00+00:00",
            trace=_QuoteTimeTrace(["2026-07-09T00:00:59+00:00"]), **seams)

    import time as _t
    plain = run({})
    ladder = run({"_sniper_quote_observed_at": "2026-07-09T00:00:30+00:00",
                  "_sniper_quote_monotonic_at": _t.perf_counter()})
    assert plain["status"] == "filled", plain
    assert ladder["status"] == "filled", ladder


def test_a_refreshed_quote_cannot_inherit_the_original_quotes_chase_window():
    """E1 recheck, 2026-09-23, and it is a money gate. `_refresh_stale_winner` binds the
    REPLACEMENT's own observation instant, and the caller then re-ran the price gates with the
    ORIGINAL `quote_observed_at`. `_effective_chase_cap` grants the wallet's scoped cap only while
    the observation falls inside the source event's window, so a replacement taken after that
    window closed was still judged under the wallet cap.

    The boundary: the initial quote is observed at 00:00:59, the wallet's 75% allowance expires at
    00:01:00, the base cap is 40%, and the refresh arrives at 00:01:02 at +70%. Under the expired
    allowance it must refuse at the BASE cap, before any signature. The assertion on the signer is
    the point -- refusing after signing would still be the defect.

    The post-approval requote at the other refresh site already adopted its instant
    (`quote_observed_at = fresh_observed_at`); this site did not.
    """
    class _LaterTrace(_QuoteTimeTrace):
        def reconciled_now(self):
            return "2026-07-09T00:01:02+00:00"

    c = _c()
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-07-09T00:00:00Z",
                            network_id="4663", source_price=1.0)
    fresh = {"sellAmount": "10000000", "buyAmount": "5882353",
             "transaction": {"to": "0xrouter", "data": "0xfresh"}, "issues": {}}
    stale = _aged_quote(5.0, lambda slippage_bps=None: fresh,
                        observed_at="2026-07-09T00:00:59+00:00")
    stale["buyAmount"] = "10000000"          # the original priced at source; the refresh is +70%
    seams, calls = _seams(quote=stale, token=((0.0, 6), (5.882353, 6)))
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        chase_max_pct=40.0, wallet_chase_cap_pct=75.0,
        wallet_chase_cap_expires_at="2026-07-09T00:01:00+00:00",
        trace=_LaterTrace(["2026-07-09T00:00:59+00:00"]), **seams)
    assert result["status"] == "skipped_chase", result
    assert calls["sign"] == 0, calls


def test_the_staleness_check_reads_the_monotonic_field_not_the_persisted_one():
    """The two stamps answer different questions and must not be interchangeable. A quote whose
    MONOTONIC stamp is stale gets refreshed even though its ISO stamp is recent."""
    c = _c(); pid = _reserve(c, network_id="4663")
    fresh = {"sellAmount": "10000000", "buyAmount": "6",
             "transaction": {"to": "0xrouter", "data": "0xfresh"}, "issues": {}}
    seams, _calls = _seams(quote=_aged_quote(5.0, lambda slippage_bps=None: fresh),
                           token=((0.0, 6), (5.0, 6)))
    seen = []
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda q: seen.append(q) or (True, "ok"), **seams,
    )
    assert result["status"] == "filled"
    assert seen[0]["transaction"]["data"] == "0xfresh", seen


def test_a_quote_with_no_monotonic_stamp_is_never_re_asked():
    """A capacity-reused quote carries the ISO stamp and NO monotonic one, so its age cannot be
    computed in this process and it proceeds unrefreshed. That is today's behaviour, reached by a
    `float()` that raised on an ISO string; it is now an explicit absent-value branch. Making the
    reuse path refreshable adds a provider call to the entry path and is held separately."""
    c = _c(); pid = _reserve(c, network_id="4663")
    asked = []
    quote = {"sellAmount": "10000000", "buyAmount": "5000000", "issues": {},
             "transaction": {"to": "0xrouter", "data": "0xold"},
             "_sniper_quote_observed_at": "2026-07-08T00:00:00+00:00",
             "_sniper_capacity_quote": True,
             "_requote": lambda slippage_bps=None: asked.append(1)}
    seams, _calls = _seams(quote=quote, token=((0.0, 6), (5.0, 6)))
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda _q: (True, "ok"), **seams,
    )
    assert result["status"] == "filled" and asked == []


def test_the_persisted_quote_stamp_is_always_an_instant_an_age_can_be_read_from():
    """`quote_age_ms` parses `quote_observed_at` with `fromisoformat`, so a float there is not a
    late age -- it is no age at all, on every ladder-quoted entry."""
    from sniper.execution.entry_attribution import quote_age_ms
    import time as _t
    c = _c(); pid = _reserve(c, network_id="4663")
    ladder_shaped = {"sellAmount": "10000000", "buyAmount": "5000000", "issues": {},
                     "transaction": {"to": "0xrouter", "data": "0xdead"},
                     "_sniper_winner": "v4",
                     "_sniper_quote_monotonic_at": _t.perf_counter()}
    seams, _calls = _seams(quote=ladder_shaped, token=((0.0, 6), (5.0, 6)))
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01+00:00",
        entry_preflight_fn=lambda _q: (True, "ok"), **seams)
    assert result["status"] == "filled", result
    observed = result["quote_observed_at"]
    assert isinstance(observed, str), observed
    age = quote_age_ms(_TraceRecorder(), "confirmed", quote_observed_at=observed,
                       confirmation_observed_at="2026-07-09T00:00:03+00:00")
    assert age == pytest.approx(2000.0), age


def test_a_stale_winner_is_re_asked_before_the_preflight_simulates_it():
    """THE PROPERTY. The quote the preflight sees must be the refreshed one."""
    c = _c(); pid = _reserve(c, network_id="4663")
    fresh = {"sellAmount": "10000000", "buyAmount": "6",
             "transaction": {"to": "0xrouter", "data": "0xfresh"}, "issues": {}}
    seams, calls = _seams(quote=_aged_quote(5.0, lambda slippage_bps=None: fresh),
                          token=((0.0, 6), (5.0, 6)))
    seen = []
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda q: seen.append(q) or (True, "ok"), **seams,
    )
    assert result["status"] == "filled"
    assert seen and seen[0]["transaction"]["data"] == "0xfresh", "the preflight saw a stale quote"


def test_a_fresh_winner_is_not_re_asked():
    """Inside the tolerance the refresh must cost nothing -- no extra quote round trip."""
    c = _c(); pid = _reserve(c, network_id="4663")
    asked = []
    seams, calls = _seams(quote=_aged_quote(0.05, lambda slippage_bps=None: asked.append(1)),
                          token=((0.0, 6), (5.0, 6)))
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda _q: (True, "ok"), **seams,
    )
    assert result["status"] == "filled"
    assert asked == [], "a quote inside the age tolerance must not be re-asked"


def test_a_winner_that_stops_quoting_proceeds_on_the_original():
    """Operator's call: one immediate second attempt, then carry on and let the preflight decide.
    The preflight now reads the freshest member, so a route that is truly gone reverts there and
    the fallback takes the next alternate with NO gas spent."""
    c = _c(); pid = _reserve(c, network_id="4663")
    tries = []

    def requote(slippage_bps=None):
        tries.append(1)
        return None
    seams, calls = _seams(quote=_aged_quote(5.0, requote), token=((0.0, 6), (5.0, 6)))
    seen = []
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda q: seen.append(q) or (True, "ok"), **seams,
    )
    assert result["status"] == "filled"
    assert len(tries) == 2, "one immediate second attempt, then proceed"
    assert seen[0]["transaction"]["data"] == "0xone", "it must proceed on the ORIGINAL quote"


def test_a_refresh_that_changes_the_spender_is_discarded():
    """A refresh that changed venue would invalidate the approval this entry relies on. Same rule
    _requote_matches enforces after approval."""
    c = _c(); pid = _reserve(c, network_id="4663")
    other_venue = {"sellAmount": "10000000", "buyAmount": "9",
                   "transaction": {"to": "0xDIFFERENT", "data": "0xelsewhere"}, "issues": {}}
    seams, calls = _seams(quote=_aged_quote(5.0, lambda slippage_bps=None: other_venue),
                          token=((0.0, 6), (5.0, 6)))
    seen = []
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda q: seen.append(q) or (True, "ok"), **seams,
    )
    assert result["status"] == "filled"
    assert seen[0]["transaction"]["to"] == "0xrouter", "a venue flip must never be adopted"


def test_a_refresh_that_changes_the_sell_amount_is_discarded():
    c = _c(); pid = _reserve(c, network_id="4663")
    wrong_size = {"sellAmount": "99999999", "buyAmount": "9",
                  "transaction": {"to": "0xrouter", "data": "0xbigger"}, "issues": {}}
    seams, calls = _seams(quote=_aged_quote(5.0, lambda slippage_bps=None: wrong_size),
                          token=((0.0, 6), (5.0, 6)))
    seen = []
    execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda q: seen.append(q) or (True, "ok"), **seams,
    )
    assert seen[0]["transaction"]["data"] == "0xone"


def test_a_quote_with_no_requote_is_left_alone():
    """Every quote predating this change, and every exit quote, has no `_requote`."""
    c = _c(); pid = _reserve(c, network_id="4663")
    seams, calls = _seams(quote=_QUOTE, token=((0.0, 6), (5.0, 6)))
    seen = []
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda q: seen.append(q) or (True, "ok"), **seams,
    )
    assert result["status"] == "filled" and seen[0]["transaction"] == _QUOTE["transaction"]


def test_a_refreshed_winner_keeps_the_alternates_the_ladder_already_paid_for():
    """E3, 2026-09-23. A refresh is an `only_rung` pass, so the replacement carries no
    `_alternates` by construction -- `evm_real_seams` only builds that list when a second rung
    completed. Dropping the original's list disabled the MUSEFUND/AGRIPPA fall-through for
    exactly the entries that took a refresh: the refreshed winner reverts, a known in-budget
    runner-up sits unused, and the attempt refuses `unsafe_gate`."""
    c = _c(); pid = _reserve(c, network_id="4663")
    relay_quote = {"sellAmount": "10000000", "buyAmount": "5000000",
                   "transaction": {"to": "0xrouter", "data": "0xrelay"}, "issues": {}}
    fresh = {"sellAmount": "10000000", "buyAmount": "5000000",
             "transaction": {"to": "0xrouter", "data": "0xfresh"}, "issues": {}}
    stale = _aged_quote(5.0, lambda slippage_bps=None: fresh)
    stale["_sniper_winner"] = "v4_multihop"
    stale["_alternates"] = [{"rung": "relay", "quote": relay_quote}]
    seams, calls = _seams(quote=stale, token=((0.0, 6), (5.0, 6)))
    seen = []

    def preflight(quote):
        data = quote["transaction"]["data"]
        seen.append(data)
        return (True, "ok") if data == "0xrelay" else (
            False, "entry_simulation_reverted: exact route reverted; detail=Call failed")

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=preflight, **seams,
    )
    assert result["status"] == "filled", result
    assert seen == ["0xfresh", "0xrelay"], seen
    assert calls["sign"] == 1


def test_a_refresh_invents_no_alternates_when_the_ladder_found_none():
    """The carry is a carry. A single-hit ladder must still refuse on a reverting winner rather
    than acquire a fallback it never quoted."""
    c = _c(); pid = _reserve(c, network_id="4663")
    fresh = {"sellAmount": "10000000", "buyAmount": "5000000",
             "transaction": {"to": "0xrouter", "data": "0xfresh"}, "issues": {}}
    seams, calls = _seams(quote=_aged_quote(5.0, lambda slippage_bps=None: fresh),
                          token=((0.0, 6), (5.0, 6)))
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda _q: (
            False, "entry_simulation_reverted: exact route reverted; detail=Call failed"),
        **seams,
    )
    assert result["status"] == "unsafe_gate", result
    assert calls["sign"] == 0 and calls["send"] == 0


def test_a_refreshed_winner_is_re_gated_before_it_can_be_signed():
    """E1, 2026-09-23. The refresh replaces the quote AFTER the price gates decided, so the gates
    had judged a price the entry no longer executes at. Matching sell amount and spender says the
    approval still covers the swap; it says nothing about what the swap now costs.

    Here the ladder's winner buys 10 tokens for $10 (source price $1, no chase) and the refresh
    comes back at 5 tokens for the same $10 -- the coin at +100% against a 30% chase cap. It must
    refuse without signing, exactly as it does when that expensive quote arrives first."""
    c = _c()
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-07-09T00:00:00Z",
                            network_id="4663", source_price=1.0)
    expensive = {"sellAmount": "10000000", "buyAmount": "5000000",
                 "transaction": {"to": "0xrouter", "data": "0xfresh"}, "issues": {}}
    cheap = _aged_quote(5.0, lambda slippage_bps=None: expensive)
    cheap["buyAmount"] = "10000000"
    seams, calls = _seams(quote=cheap, token=((0.0, 6), (5.0, 6)))
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        chase_max_pct=30.0, entry_preflight_fn=lambda _q: (True, "ok"), **seams,
    )
    assert result["status"] == "skipped_chase", result
    assert calls["sign"] == 0, "the refreshed price must be gated before any signing"


def test_the_same_expensive_quote_refuses_whether_it_arrives_first_or_on_refresh():
    """The control for the test above: the refusal must not depend on WHEN the price arrived."""
    expensive = {"sellAmount": "10000000", "buyAmount": "5000000",
                 "transaction": {"to": "0xrouter", "data": "0xfresh"}, "issues": {}}

    def run(quote):
        c = _c()
        pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                                notional=10.0, reserved_at="2026-07-09T00:00:00Z",
                                network_id="4663", source_price=1.0)
        seams, calls = _seams(quote=quote, token=((0.0, 6), (5.0, 6)))
        result = execute_entry_evm(
            c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
            strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
            chase_max_pct=30.0, entry_preflight_fn=lambda _q: (True, "ok"), **seams,
        )
        return result, calls

    refreshed_in = _aged_quote(5.0, lambda slippage_bps=None: expensive)
    refreshed_in["buyAmount"] = "10000000"
    late, late_calls = run(refreshed_in)
    first, first_calls = run(dict(expensive))
    assert late["status"] == first["status"] == "skipped_chase", (late, first)
    assert late_calls["sign"] == first_calls["sign"] == 0
    assert late["quote_chase_pct"] == pytest.approx(first["quote_chase_pct"]), (late, first)


def test_a_refresh_that_is_not_adopted_is_gated_exactly_once():
    """The re-gate must hang off the REPLACEMENT, not off the refresh attempt. A requote that
    never replaces the quote (none, raising, wrong venue, wrong size) leaves the single initial
    decision untouched -- otherwise every failure mode of the refresh changes gate behaviour."""
    c = _c(); pid = _reserve(c, network_id="4663")
    seams, _calls = _seams(quote=_aged_quote(5.0, lambda slippage_bps=None: None),
                           token=((0.0, 6), (5.0, 6)))
    trace = _TraceRecorder()
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda _q: (True, "ok"), trace=trace, **seams,
    )
    assert result["status"] == "filled"
    revisions = [md.get("revision") for name, md in trace.substages
                 if name in ("execution_premium", "post_peak_confirmation")]
    assert "refresh" not in revisions, revisions


def test_the_refresh_re_gate_adds_no_second_chase_trace_line():
    """`scripts/research/chase_cap_prospective.py` reads the FIRST chase event and
    `test_the_trace_records_every_substage_without_new_work` asserts the exact substage sequence,
    so the re-gate re-checks the chase inline without emitting a second line -- the same choice
    the post-approval requote block already makes."""
    c = _c()
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-07-09T00:00:00Z",
                            network_id="4663", source_price=1.0)
    fresh = {"sellAmount": "10000000", "buyAmount": "9000000",
             "transaction": {"to": "0xrouter", "data": "0xfresh"}, "issues": {}}
    quote = _aged_quote(5.0, lambda slippage_bps=None: fresh)
    quote["buyAmount"] = "10000000"
    seams, _calls = _seams(quote=quote, token=((0.0, 6), (5.0, 6)))
    trace = _TraceRecorder()
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        chase_max_pct=30.0, entry_preflight_fn=lambda _q: (True, "ok"), trace=trace, **seams,
    )
    assert result["status"] == "filled", result
    chase = [name for name, _md in trace.substages if name == "chase_quote_confirmation"]
    assert len(chase) == 1, trace.substages


def test_a_raising_requote_never_breaks_the_entry():
    c = _c(); pid = _reserve(c, network_id="4663")

    def boom(slippage_bps=None):
        raise RuntimeError("aggregator down")
    seams, calls = _seams(quote=_aged_quote(5.0, boom), token=((0.0, 6), (5.0, 6)))
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda _q: (True, "ok"), **seams,
    )
    assert result["status"] == "filled"


def test_a_min_out_retry_re_asks_the_WINNER_not_the_ladder():
    """2026-09-22. A min-out revert means the minimum was computed on state the pool has left, so
    the retry needs a fresh quote at a wider slippage -- not a fresh VENUE. Re-running the whole
    ladder spends the 1.8-3.0 s that made the quote stale, which is what the retry escapes."""
    c = _c(); pid = _reserve(c, network_id="4663")
    ladder_calls, requote_slips = [], []
    retry = {"sellAmount": "10000000", "buyAmount": "7",
             "transaction": {"to": "0xrouter", "data": "0xretry"}, "issues": {}}

    def requote(slippage_bps=None):
        requote_slips.append(slippage_bps)
        return retry

    first = _aged_quote(0.0, requote)

    def quote_fn(_mint, _usd, *, slippage_bps=None):
        ladder_calls.append(slippage_bps)
        return first
    seams, calls = _seams(quote=None, token=((0.0, 6), (5.0, 6)))
    seams["quote_fn"] = quote_fn
    preflight = iter(((False, "entry_simulation_min_out: exact route minimum output reverted"),
                      (True, "ok")))
    seen = []
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda q: seen.append(q) or next(preflight),
        retry_slippage_bps=800, **seams,
    )
    assert result["status"] == "filled"
    assert requote_slips == [800], "the retry must re-ask the winner at the retry slippage"
    assert len(ladder_calls) == 1, "the ladder must run ONCE, not again for the retry"
    assert seen[1]["transaction"]["data"] == "0xretry"


def test_a_min_out_retry_falls_back_to_the_ladder_when_the_winner_will_not_answer():
    """This can only save a ladder call; it can never be the reason an entry fails."""
    c = _c(); pid = _reserve(c, network_id="4663")
    ladder_calls = []
    second = {"sellAmount": "10000000", "buyAmount": "7",
              "transaction": {"to": "0xrouter", "data": "0xladder"}, "issues": {}}

    def quote_fn(_mint, _usd, *, slippage_bps=None):
        ladder_calls.append(slippage_bps)
        return _aged_quote(0.0, lambda slippage_bps=None: None) if not ladder_calls[1:] else second
    seams, calls = _seams(quote=None, token=((0.0, 6), (5.0, 6)))
    seams["quote_fn"] = quote_fn
    preflight = iter(((False, "entry_simulation_min_out: exact route minimum output reverted"),
                      (True, "ok")))
    seen = []
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda q: seen.append(q) or next(preflight),
        retry_slippage_bps=800, **seams,
    )
    assert result["status"] == "filled"
    assert ladder_calls == [None, 800], "the ladder still runs when the winner will not answer"
    assert seen[1]["transaction"]["data"] == "0xladder"


def test_a_min_out_retry_refuses_a_winner_that_answers_a_different_venue():
    c = _c(); pid = _reserve(c, network_id="4663")
    ladder_calls = []
    elsewhere = {"sellAmount": "10000000", "buyAmount": "7",
                 "transaction": {"to": "0xOTHER", "data": "0xelsewhere"}, "issues": {}}
    good = {"sellAmount": "10000000", "buyAmount": "7",
            "transaction": {"to": "0xrouter", "data": "0xladder"}, "issues": {}}

    def quote_fn(_mint, _usd, *, slippage_bps=None):
        ladder_calls.append(slippage_bps)
        return (_aged_quote(0.0, lambda slippage_bps=None: elsewhere)
                if not ladder_calls[1:] else good)
    seams, calls = _seams(quote=None, token=((0.0, 6), (5.0, 6)))
    seams["quote_fn"] = quote_fn
    preflight = iter(((False, "entry_simulation_min_out: exact route minimum output reverted"),
                      (True, "ok")))
    seen = []
    execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda q: seen.append(q) or next(preflight),
        retry_slippage_bps=800, **seams,
    )
    assert seen[1]["transaction"]["to"] == "0xrouter", "a venue flip must not be adopted"
    assert ladder_calls == [None, 800]


# --- executed pool (2026-09-22): the pool a fill actually touched, from our own receipt ---

def _receipt_with_v4_swap(chain="8453"):
    from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, V4_SWAP
    pool_id = "0x" + "77" * 32
    return {"status": "0x1", "logs": [
        {"address": V4_POOL_MANAGERS[chain], "topics": [V4_SWAP, pool_id, "0x" + "00" * 32], "data": "0x"},
    ]}, pool_id


def test_a_filled_entry_stamps_the_pool_its_receipt_names():
    from sniper.services.memecoin_executed_pool import SOURCE_V4
    c = _c(); pid = _reserve(c)
    seams, _ = _seams(quote=_QUOTE, usdc=(100.0, 90.0), token=(0.0, 5.0))
    receipt, pool_id = _receipt_with_v4_swap()
    asked = []
    trace = _Trace() if "_Trace" in globals() else None
    r = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="t", now_iso="t", sleep_fn=lambda _s: None,
                          receipt_fn=lambda h: asked.append(h) or receipt, **seams)
    assert r["status"] == "filled" and asked == ["0xhash"]
    row = c.execute("SELECT executed_pool, executed_pool_source FROM memecoin_positions WHERE id=?",
                    (r["position_id"],)).fetchone()
    assert tuple(row) == (pool_id, SOURCE_V4)


def test_a_split_fill_stamps_the_pool_that_delivered_most_of_the_coin():
    """Alpha 2026-10-17 (9177657c): the executor hands the coin to the receipt decode, so a split
    route records its main pool, not the last V3 swap."""
    from sniper.services.memecoin_executed_pool import SOURCE_V3, TRANSFER_TOPIC, V3_SWAP
    main_pool, side_pool, coin = "0x" + "a1" * 20, "0x" + "a2" * 20, "0x" + "70" * 20

    def sent(pool, amount):
        return {"address": coin, "topics": [TRANSFER_TOPIC, "0x" + "0" * 24 + pool[2:],
                                            "0x" + "0" * 24 + "99" * 20], "data": "0x%064x" % amount}
    receipt = {"status": "0x1", "logs": [
        sent(main_pool, 92), {"address": main_pool, "topics": [V3_SWAP, "0x1", "0x2"], "data": "0x"},
        sent(side_pool, 8), {"address": side_pool, "topics": [V3_SWAP, "0x1", "0x2"], "data": "0x"}]}
    c = _c(); pid = _reserve(c, mint=coin)
    seams, _ = _seams(quote=_QUOTE, usdc=(100.0, 90.0), token=(0.0, 5.0))
    r = execute_entry_evm(c, pending_id=pid, mint=coin, network_id="8453", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="t", now_iso="t", sleep_fn=lambda _s: None,
                          receipt_fn=lambda h: receipt, **seams)
    assert r["status"] == "filled"
    row = c.execute("SELECT executed_pool, executed_pool_source FROM memecoin_positions WHERE id=?",
                    (r["position_id"],)).fetchone()
    assert tuple(row) == (main_pool, SOURCE_V3)


def test_a_receipt_read_that_fails_never_touches_the_fill():
    c = _c(); pid = _reserve(c)
    seams, _ = _seams(quote=_QUOTE, usdc=(100.0, 90.0), token=(0.0, 5.0))

    def boom(_h):
        raise ConnectionError("rpc down")
    r = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="t", now_iso="t", sleep_fn=lambda _s: None,
                          receipt_fn=boom, **seams)
    assert r["status"] == "filled"
    row = c.execute("SELECT executed_pool FROM memecoin_positions WHERE id=?", (r["position_id"],)).fetchone()
    assert row[0] is None


# --- the receipt recovery booked the swap first (Ink double booking, 2026-09-30) -----------------


@pytest.mark.parametrize("closed", [True, False], ids=["recovered-and-closed", "recovered-open"])
def test_a_swap_the_receipt_recovery_already_booked_is_never_booked_again(closed):
    """recover_confirmed_ink_entry can book a landed swap while this executor is still in its
    confirmation wait. Once that position CLOSED, the open-position unique index no longer stops
    a second position for the same swap, and a failed confirm_pending only warns. The executor
    finds the fill for its own attempt and returns that position instead."""
    c = _c()
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-07-09T00:00:00Z", network_id="8453",
                            attempt_id="attempt-dup")
    seams, calls = _seams(quote=_QUOTE)
    booked = {}

    def recovery_books_first(tx_hash):
        """What the receipt recovery commits during the executor's confirmation wait."""
        position = m.open_position(
            c, source_user_id="trader1", source_handle="(real)", mint="0xtok", ticker="",
            entry_price=2.0, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=None,
            their_size_usd=None, opened_at="2026-07-09T00:00:01Z", strategy="alpha_b",
            execution_mode="real",
            network_id="8453", attempt_id="attempt-dup", commit=False)
        booked["fill"] = m.insert_fill(
            c, position_id=position, kind="entry", price=2.0, qty=5.0, notional=10.0,
            fill_source="chain", fraction=1.0, created_at="2026-07-09T00:00:01Z",
            tx_sig=tx_hash.upper().replace("0X", "0x"), attempt_id="attempt-dup",
            network_id="8453", commit=False)
        assert m.confirm_pending(c, pid, commit=False)
        c.commit()
        if closed:
            m.close_position(c, position, "2026-07-09T00:00:02Z", -1.0, "stop")
        booked["position"] = position
        return "confirmed"
    seams["confirm_fn"] = recovery_books_first
    r = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                          strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
                          **seams)
    assert r["status"] == "filled" and r["booked_by"] == "receipt_recovery"
    assert r["position_id"] == booked["position"] and r["fill_id"] == booked["fill"]
    assert r["attempt_id"] == "attempt-dup" and r["tx_hash"] == "0xhash"
    assert c.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE kind='entry'").fetchone()[0] == 1
    assert m.pending_row(c, pid)["status"] == "confirmed"
    assert not c.in_transaction                         # the booking transaction was rolled back


def test_a_fill_the_receipt_recovery_booked_is_not_announced_as_a_new_entry(monkeypatch):
    """Gamma LOW on d9acb6959: "filled $X" for a swap booked earlier (maybe already closed) would
    announce an entry that is not happening. A fill this executor booked is still announced."""
    from sniper.services import memecoin_real_runner as R
    from sniper.services import memecoin_telegram as T
    sent = []
    monkeypatch.setattr(T, "notification_lifecycle_active", lambda: False)
    monkeypatch.setattr(R.alerts, "alert_entry", lambda *a, **k: sent.append(a))
    booked = {"status": "filled", "position_id": 1, "fill_price": 2.0, "notional": 10.0,
              "tx_hash": "0xhash"}
    R._alert_entry_result("0xtok", 10.0, dict(booked, booked_by="receipt_recovery"))
    assert sent == []
    R._alert_entry_result("0xtok", 10.0, booked)
    assert len(sent) == 1


def test_the_attempt_lookup_matches_the_swap_hash_and_nothing_else():
    c = _c()
    position = m.open_position(
        c, source_user_id="u", source_handle="(real)", mint="0xtok", ticker="", entry_price=1.0,
        entry_notional=1.0, entry_fill_source="chain", entry_lag_s=None, their_size_usd=None,
        opened_at="2026-07-09T00:00:01Z",
        strategy="alpha_b", execution_mode="real", network_id="8453", attempt_id="a1")
    m.insert_fill(c, position_id=position, kind="entry", price=1.0, qty=1.0, notional=1.0,
                  fill_source="chain", fraction=1.0, created_at="2026-07-09T00:00:01Z",
                  tx_sig="0xABC", attempt_id="a1")
    assert m.entry_fill_for_attempt(c, "a1", "0xabc")["position_id"] == position
    assert m.entry_fill_for_attempt(c, "a1", "0xother") is None      # a retried swap's new hash
    assert m.entry_fill_for_attempt(c, "a2", "0xabc") is None
    assert m.entry_fill_for_attempt(c, None, "0xabc") is None
    assert m.entry_fill_for_attempt(c, "a1", None) is None


# --- tax-aware entry minimum (2026-09-30, operator: "make the BSC tax aware") --------------------

PANCAKE_V2_ROUTER = "0x10ed43c718714eb63d5aa57b78b54704e256024e"
TAX_WBNB, TAX_COIN = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", "0x" + "77" * 20


def _tax_pool():
    from sniper.services import memecoin_evm_post_trade as pt
    factory, init_hash = pt.V2_FACTORIES[("56", PANCAKE_V2_ROUTER)]
    return pt._v2_pair(factory, init_hash, TAX_WBNB, TAX_COIN)


def _v2_buy_quote(slippage_bps=300):
    """The PRODUCTION V2 fee-on-transfer builder quoting WBNB -> coin on PancakeSwap V2."""
    from sniper.execution.evm_v2_exit_quote import dex_v2_exit_quote

    def call_fn(to, data):
        return "0x" + "".join(format(x, "064x") for x in (32, 2, 10_000_000, 5_000_000))
    return dex_v2_exit_quote("56", TAX_WBNB, TAX_COIN, 10_000_000, "0x" + "11" * 20,
                             slippage_bps=slippage_bps, cfg_router=PANCAKE_V2_ROUTER,
                             connector_tokens=(), tax_buffer_bps=0, call_fn=call_fn,
                             now_fn=lambda: 1_000)


def _tax_entry(monkeypatch, *, chains=("56",), post=None, preflight=None):
    from sniper.services.memecoin_evm_post_trade import EvmChaseReference
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_TAX_AWARE_MIN_OUT_CHAINS", frozenset(chains))
    c = _c()
    pid = m.reserve_pending(c, mint=TAX_COIN, source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-07-09T00:00:00Z", network_id="56")
    seen, trace = [], _TraceRecorder()
    verdicts = iter(preflight or [(True, "ok")])

    def entry_preflight_fn(quote):
        seen.append(dict(quote))
        return next(verdicts, (True, "ok"))
    post = {"ratio": 1.05, "pool": _tax_pool(), "venue": "v2", "buy_tax": 0.03} if post is None else post
    seams, _calls = _seams(quote=None)
    seams["quote_fn"] = lambda mint, usd, slippage_bps=300: _v2_buy_quote(slippage_bps)
    signed = []
    seams["sign_fn"] = lambda tx: (signed.append(tx), ("0xraw", "0xhash", 7))[1]
    r = execute_entry_evm(
        c, pending_id=pid, mint=TAX_COIN, network_id="56", usd_amount=10.0, strategy="alpha_b",
        source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=entry_preflight_fn, retry_slippage_bps=800,
        chase_reference=EvmChaseReference("56", post=post), trace=trace, **seams)
    marks = [md for name, md in trace.substages if name == "evm.buy_tax_min_out"]
    return r, seen, marks, signed


def test_a_measured_buy_tax_lowers_the_signed_minimum_on_the_taxed_pair(monkeypatch):
    """BINF (2026-09-30): a 3% buy tax ate the whole 3% first-attempt minimum. The source's own
    receipt decode measured it on the pair our V2 route buys from: min = quote x (1 - 3% - 3%)."""
    r, seen, marks, signed = _tax_entry(monkeypatch)
    assert r["status"] == "filled"
    assert [int(tx["data"][74:138], 16) for tx in signed] == [4_700_000]   # the bytes we sign
    assert [(md["outcome"], md["tax_bps"], md["min_after"]) for md in marks] == [
        ("applied", 300, "4700000")] * 2
    # 4,850,000 - 3% of 5,000,000, and the post-approval preflight does not take it again
    assert [q["minBuyAmount"] for q in seen] == ["4700000", "4700000"]
    assert int(seen[0]["transaction"]["data"][74:138], 16) == 4_700_000


def test_the_min_out_retry_adds_the_same_tax_to_its_eight_percent(monkeypatch):
    r, seen, _marks, _signed = _tax_entry(monkeypatch, preflight=[
        (False, "entry_simulation_min_out: exact route minimum output reverted"), (True, "ok")])
    # first attempt, the 8% retry, then the post-approval preflight of that same retry quote:
    # the tax is taken once per quote, never twice.
    assert [q["minBuyAmount"] for q in seen] == ["4700000", "4450000", "4450000"]
    assert r["status"] == "filled"


@pytest.mark.parametrize("case", ["chain_off", "other_pool", "no_tax", "zero_tax"])
def test_every_other_case_keeps_todays_minimum(monkeypatch, case):
    """Alpha: pool-bound. A pair the source did not buy through, an unknown or zero tax, or a chain
    not switched on: the minimum the rung built, byte for byte."""
    post = {"ratio": 1.05, "pool": _tax_pool(), "venue": "v2", "buy_tax": 0.03}
    kw = {}
    if case == "chain_off":
        kw["chains"] = ()
    elif case == "other_pool":
        post["pool"] = "0x" + "ee" * 20
    elif case == "no_tax":
        post.pop("buy_tax")
    else:
        post["buy_tax"] = 0.0
    r, seen, marks, _signed = _tax_entry(monkeypatch, post=post, **kw)
    assert r["status"] == "filled" and seen[0]["minBuyAmount"] == "4850000"
    assert [md["outcome"] for md in marks] == (["pool_mismatch"] * 2 if case == "other_pool" else [])
    assert seen[0]["transaction"]["data"] == _v2_buy_quote()["transaction"]["data"]


def test_a_tax_above_the_cap_lowers_the_minimum_by_the_cap_only(monkeypatch):
    post = {"ratio": 1.05, "pool": _tax_pool(), "venue": "v2", "buy_tax": 0.40}
    r, seen, _marks, _signed = _tax_entry(monkeypatch, post=post)
    assert seen[0]["minBuyAmount"] == str(4_850_000 - 500_000)      # 10% cap of 5,000,000


@pytest.mark.parametrize('missing', ['token', 'spend', 'both'])
def test_confirmed_receipt_books_lagged_balances_without_sleep(missing):
    c = _c(); pid = _reserve(c, network_id='4663')
    seams, calls = _seams(quote=_QUOTE,
                          usdc=(100.0, 100.0 if missing in ('spend', 'both') else 90.0),
                          token=(0.0, 0.0 if missing in ('token', 'both') else 5.0))
    sleeps = []
    result = execute_entry_evm(
        c, pending_id=pid, mint='0xtok', network_id='4663', usd_amount=10.0,
        strategy='alpha_b', source_user_id='t', now_iso='t', sleep_fn=sleeps.append,
        receipt_token_fn=lambda h, mint: (5.0, 18), receipt_spent_fn=lambda h: 0.01,
        sell_token_usd=1000.0, **seams)
    assert result['status'] == 'filled'
    assert sleeps == []
    fill = c.execute('select qty,notional from memecoin_fills where position_id=?',
                     (result['position_id'],)).fetchone()
    assert fill['qty'] == 5.0 and fill['notional'] == 10.0
    assert calls['send'] == 1


def test_unreadable_receipt_keeps_bounded_balance_recovery():
    c = _c(); pid = _reserve(c, network_id='4663')
    seams, _ = _seams(quote=_QUOTE, token=(0.0, 0.0, 5.0))
    sleeps = []
    result = execute_entry_evm(
        c, pending_id=pid, mint='0xtok', network_id='4663', usd_amount=10.0,
        strategy='alpha_b', source_user_id='t', now_iso='t', sleep_fn=sleeps.append,
        receipt_token_fn=lambda h, mint: None, **seams)
    assert result['status'] == 'filled' and sleeps == [1.0]


@pytest.mark.parametrize("fresh_buy, expected", [("6000000", "filled"), ("1000000", "skipped_chase"),
                                                    ("signed_floor", "skipped_chase"),
                                                    ("missing", "filled"), ("wrong_size", "filled")])
def test_base_rechecks_quote_after_confirmed_unwrap_before_swap(fresh_buy, expected, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "perf_counter", lambda: clock[0])
    c = _c(); pid = _reserve(c)
    c.execute("UPDATE memecoin_pending_entries SET source_price=2.0 WHERE id=?", (pid,))
    c.commit()
    fresh = {"sellAmount": "10000000", "buyAmount": fresh_buy if fresh_buy.isdigit() else "6000000",
             "minBuyAmount": "5000000",
             "transaction": {"to": "0xrouter", "data": "0xfresh"}, "issues": {}}
    if fresh_buy == "signed_floor":
        from sniper.settings import memecoin as cfg
        monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.)
        fresh["minBuyAmount"] = "1000000"
    elif fresh_buy == "missing":
        fresh = None
    elif fresh_buy == "wrong_size":
        fresh["sellAmount"] = "11000000"
    q = _aged_quote(0.0, lambda slippage_bps=None: fresh)
    q["buyAmount"] = "5000000"
    q["minBuyAmount"] = "4000000"
    q["pre_transactions"] = [{"to": "0xweth", "data": "0x2e1a7d4d" + format(10, "064x"),
                              "value": "0x0"}]
    seams, calls = _seams(quote=q, token=((0.0, 6), (5.0, 6)))
    original_confirm = seams["confirm_fn"]
    def confirm(h):
        # The unwrap has made the quote old; no wall-clock sleep in the regression.
        clock[0] += 5
        return original_confirm(h)
    seams["confirm_fn"] = confirm
    signed = []
    original_sign = seams["sign_fn"]
    seams["sign_fn"] = lambda tx: signed.append(tx) or original_sign(tx)
    checks = []
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        chase_reference={"price": 2.0, "basis": "source_avg"}, chase_max_pct=35,
        entry_preflight_fn=lambda x: checks.append(x["transaction"]["data"]) or (True, "ok"),
        **seams)
    assert result["status"] == expected, result
    if expected == "filled":
        want = "0xone" if fresh_buy in {"missing", "wrong_size"} else "0xfresh"
        assert signed[-1]["data"] == want
        assert checks[-1] == want
    else:
        assert len(signed) == 1, "only the unwrap, never the over-cap swap"
