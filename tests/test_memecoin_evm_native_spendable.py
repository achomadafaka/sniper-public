"""Chain-1 unstall (Alpha 2026-10-08): two expired chain-1 swaps left their unwrapped funds as native
ETH, and sizing read WETH only, so every chain-1 entry refused "$0.00 clip". On a chain listed in
MEMECOIN_EVM_NATIVE_SPENDABLE_CHAINS, spare native above the full-book gas reserve is spendable."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_risk as risk
from sniper.settings import memecoin as cfg


@pytest.fixture
def ledger(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"1", "56"})
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING_BY_CHAIN", {})
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT_BY_CHAIN", {"1": 2, "56": 2})
    monkeypatch.setattr(cfg, "MEMECOIN_EQUITY_SLOT_TIERS_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EQUITY_SLOT_TIERS_ENABLED_BY_CHAIN", {})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_RESERVE_MIN_NATIVE_BY_CHAIN", {"1": .01, "56": .01})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MAX_CLIP_USD_BY_CHAIN", {})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MAX_CLIP_USD", 0)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_TIER_CLIP_USD_BY_CHAIN", {})
    monkeypatch.setenv("MEMECOIN_EVM_FIXED_CLIP_USD_1", "0")     # live: bankroll sizing on chain 1
    monkeypatch.setenv("MEMECOIN_EVM_NATIVE_SPENDABLE_CHAINS", "1")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    for name, value in (("ENTRY", .001), ("APPROVAL", .0001), ("EXIT", .001)):   # live chain-1 knobs
        monkeypatch.setenv("MEMECOIN_EVM_GAS_%s_NATIVE_1" % name, str(value))
        monkeypatch.setitem(getattr(cfg, "MEMECOIN_EVM_GAS_%s_NATIVE_BY_CHAIN" % name), "1", value)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    yield c
    c.close()


def ctx(c, *, weth=0.0, native=0.042, price=2500.0, chain="1"):
    return risk.build_evm_risk_context(c, usdc_balance=weth, native_balance=native,
                                       native_quote_price_usd=price, day="2026-10-08", network_id=chain)


def test_spare_native_funds_a_clip_when_weth_is_empty(ledger):
    got = ctx(ledger)
    spare = (0.042 - 0.01) * 2500                       # above the full-book gas reserve
    assert got.native_sizing_equity_usd == pytest.approx(spare)
    assert got.usdc_balance == pytest.approx(spare) and got.native_spendable
    assert got.trade_size_usd == pytest.approx(spare / 2) and got.trade_size_usd > 5
    decision = risk.evm_entry_allowed(got, mint="0x" + "ab" * 20, trade_size=got.trade_size_usd)
    assert decision.allowed, decision


@pytest.mark.parametrize("native, price", [(None, 2500.0), (0.042, None), (0.009, 2500.0)])
def test_unknown_native_or_price_or_native_inside_the_reserve_refuses_as_today(ledger, native, price):
    got = ctx(ledger, native=native, price=price)
    assert got.trade_size_usd == 0 and not got.native_spendable


def test_pending_notional_is_subtracted_once_from_the_combined_pool(ledger, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT_BY_CHAIN", {"1": 1})   # the clamp binds
    m.reserve_pending(ledger, mint="0xother", source_user_id="u", strategy="alpha_b", notional=20,
                      reserved_at="2026-10-08T00:00:00+00:00", network_id="1")
    before = ctx(ledger, weth=30.0, native=0.042)
    # An in-flight unwrap moved $20 of WETH to native: the free pool is unchanged, never doubled.
    after = ctx(ledger, weth=10.0, native=0.042 + 20 / 2500)
    assert after.trade_size_usd == pytest.approx(before.trade_size_usd)
    combined = 30.0 + (0.042 - 0.01) * 2500
    assert before.trade_size_usd == pytest.approx(combined - 20)      # the pending $20 once


def test_an_unlisted_chain_keeps_wrapped_cash_only(ledger, monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_NATIVE_SPENDABLE_CHAINS", "")
    got = ctx(ledger)
    assert got.trade_size_usd == 0 and got.usdc_balance == 0 and not got.native_spendable
    assert ctx(ledger, chain="56", weth=0.0).trade_size_usd == 0


def test_the_floor_refusal_names_the_combined_balance(ledger):
    got = ctx(ledger, native=0.0115)                     # spare $3.75: a clip under the $5 floor
    decision = risk.evm_entry_allowed(got, mint="0x" + "ab" * 20, trade_size=got.trade_size_usd)
    assert not decision.allowed and "WETH+spare native" in decision.reason, decision.reason


@pytest.fixture
def gas_book(ledger, monkeypatch):
    for name, value in (('gas_entry_native_for', .001),
                        ('gas_approval_native_for', .0001),
                        ('gas_exit_native_for', .001),
                        ('gas_reserve_min_native_for', 0.0)):
        monkeypatch.setattr(cfg, name, lambda chain, value=value: value)
    monkeypatch.setattr(cfg, 'MEMECOIN_GAS_RETRY_FACTOR', 2.0)
    return ledger


def reserve(c, chain='1', **kwargs):
    mint = '0x%040x' % (c.execute('SELECT count(*) FROM memecoin_pending_entries').fetchone()[0] + 1)
    return m.reserve_pending(c, mint=mint, source_user_id='source',
                             strategy='alpha_b', notional=51.77, network_id=chain,
                             reserved_at='2026-10-11T10:05:37+00:00', **kwargs)


def test_owned_unsigned_entry_is_counted_once_and_other_future_entries_stay_counted(gas_book):
    c = gas_book
    before = risk.required_entry_native(c, '1')
    own = reserve(c)
    assert before == pytest.approx(.0033)
    assert risk.required_entry_native(c, '1', own_pending_id=own) == pytest.approx(before)
    assert risk.required_entry_native(c, '1') == pytest.approx(.0055)
    reserve(c, signal_source='manual')
    assert risk.required_entry_native(c, '1', own_pending_id=own) == pytest.approx(.0055)
    assert m.pending_totals(c, network=' 1 ', own_pending_id=own) == (1, 51.77)
    assert m.pending_totals(c, own_pending_id=own) == (2, 103.54)  # global admission unchanged


@pytest.mark.parametrize('field,value', [
    ('tx_sig', '0xsigned'), ('nonce', 0), ('broadcast_at', 'now'),
    ('signed_payload_b64', 'raw'), ('signed_payload_sha256', 'sha'),
    ('submission_blockhash', 'hash'), ('submission_last_valid_height', 0),
    ('submission_backend', 'rpc'), ('submission_bound_at', 'now'),
    ('cleanup_tx_sig', '0xcleanup'), ('cleanup_nonce', 0),
    ('cleanup_amount_atomic', '0'), ('cleanup_status', 'reserved'),
    ('cleanup_signed_at', 'now'), ('cleanup_signed_payload_sha256', 'sha'),
])
def test_owned_signed_or_cleanup_bound_reservation_cannot_be_discounted(gas_book, field, value):
    own = reserve(gas_book)
    gas_book.execute(f'UPDATE memecoin_pending_entries SET {field}=? WHERE id=?', (value, own))
    gas_book.commit()
    assert risk.required_entry_native(gas_book, '1', own_pending_id=own) == pytest.approx(.0055)


@pytest.mark.parametrize('ownership', ['missing', 'wrong_chain', 'terminal', 'boolean'])
def test_invalid_owned_reservation_does_not_discount_another(gas_book, ownership):
    own = reserve(gas_book, chain='56' if ownership == 'wrong_chain' else '1')
    if ownership == 'terminal':
        m.release_pending(gas_book, own, 'confirmed_failed')
    other = reserve(gas_book)
    identity = 99999 if ownership == 'missing' else True if ownership == 'boolean' else own
    expected = .0077 if ownership in ('missing', 'boolean') else .0055
    assert risk.required_entry_native(gas_book, '1', own_pending_id=identity) == pytest.approx(expected)
    assert other != identity or ownership == 'boolean'


def test_owned_reservation_matching_open_position_never_discounts_other_pending(gas_book):
    own = reserve(gas_book, attempt_id='own-attempt')
    gas_book.execute("INSERT INTO memecoin_positions "
        "(mint,source_user_id,strategy,opened_at,execution_mode,is_open,network_id,attempt_id) "
        "VALUES ('0xown','source','alpha_b','2026-10-11T10:05:37Z','real',1,'1','own-attempt')")
    gas_book.commit()
    reserve(gas_book)
    assert m.pending_totals(gas_book, network='1', own_pending_id=own) == (1, 51.77)
    assert risk.required_entry_native(gas_book, '1', own_pending_id=own) == pytest.approx(.0077)


def test_owned_pending_aggregation_local_cost(gas_book):
    import statistics
    import time
    ids = [reserve(gas_book) for _ in range(32)]
    measurements = {'generic': [], 'owned': []}
    for _ in range(1000):
        for label in measurements:
            begin = time.perf_counter_ns()
            count, amount = m.pending_totals(gas_book, network='1',
                **({'own_pending_id': ids[0]} if label == 'owned' else {}))
            measurements[label].append((time.perf_counter_ns() - begin) / 1000)
            assert count == (31 if label == 'owned' else 32)
    for label, samples in measurements.items():
        print('pending aggregation', label, 'microseconds p50/p90:',
              statistics.median(samples), sorted(samples)[900])
