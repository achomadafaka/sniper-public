"""Frozen Solana entry-tip liability and retained snapshot regressions (F01)."""
import pytest
import time

from test_memecoin_slot_lane_sender_ab import conn  # noqa: F401 (temp SQLite fixture)


def test_slot_usdc_nozomi_tip_cannot_spend_exit_gas(conn, tmp_path, monkeypatch):
    from test_memecoin_slot_lane_nozomi_ab import _production_lane, _force_ids, _ids, BLOCKHASH
    from sniper.services import memecoin_slot_lane as lane, memecoin_risk as risk
    from sniper.settings import memecoin as cfg
    monkeypatch.setenv('MEMECOIN_NOZOMI_ALL_LANES', '0')
    monkeypatch.setenv('MEMECOIN_NOZOMI_ALL_B', '0')
    _force_ids(monkeypatch, [_ids()['B']])
    ln, buy, tpu, sender, nozomi = _production_lane(conn, tmp_path, monkeypatch)
    gas = risk.required_entry_sol(0, cfg.MEMECOIN_GAS_ENTRY_SOL,
                                 cfg.MEMECOIN_GAS_EXIT_SOL, cfg.MEMECOIN_GAS_RETRY_FACTOR)
    ln.snapshot_fn = lambda: lane.PrefetchSnapshot(sol_usd=150.0, usdc_balance=400.0,
        sol_balance=gas + 0.001, blockhash=BLOCKHASH, last_valid_height=1,
        observed_mono={k: time.monotonic() for k in ('sol_usd', 'balances', 'blockhash')})
    got = ln.handle(buy('audit-USDC-tip'))
    assert not tpu and not nozomi.calls, got


def test_usdc_pending_tip_is_reserved_once_and_legacy_rows_remain_valid(conn):
    from sniper.persistence import db_memecoin as db
    first = db.reserve_pending(conn, mint='audit-tip-one', source_user_id='uid', strategy='s',
        notional=25, reserved_at='2026-10-10T12:00:00+00:00', network_id='solana',
        funding_asset='usdc', funding_overhead_atomic=5_000_000)
    db.reserve_pending(conn, mint='audit-tip-legacy', source_user_id='uid', strategy='s',
        notional=25, reserved_at='2026-10-10T12:00:00+00:00', network_id='solana')
    assert db.pending_row(conn, first)['funding_overhead_atomic'] == '5000000'
    assert db.pending_funding_totals(conn, 'solana') == (50., 5_000_000)
    assert db.pending_funding_totals(conn, 'solana', snapshot_debits=(first,)) == (50., 5_000_000)


@pytest.mark.parametrize('all_lanes', ['0', '1'])
@pytest.mark.parametrize('native', [False, True])
def test_frozen_nozomi_tip_overrides_flag_and_is_charged_once(conn, tmp_path, monkeypatch, all_lanes, native):
    from test_memecoin_slot_lane_nozomi_ab import _production_lane, _force_ids, _ids, BLOCKHASH
    from sniper.services import memecoin_slot_lane as lane, memecoin_risk as risk
    from sniper.settings import memecoin as cfg
    from sniper.persistence import db_memecoin as db
    monkeypatch.setenv('MEMECOIN_NOZOMI_ALL_LANES', all_lanes)
    monkeypatch.setenv('MEMECOIN_NOZOMI_ALL_B', '0')
    _force_ids(monkeypatch, [_ids()['B']])
    ln, buy, sent, _, _ = _production_lane(conn, tmp_path, monkeypatch)
    if native:
        ln.sol_buffer_cap_usd, ln.shadow_clip_fn = 200., lambda: 50.
    ln.snapshot_fn = lambda: lane.PrefetchSnapshot(sol_usd=150., usdc_balance=400.,
        sol_balance=5., blockhash=BLOCKHASH, last_valid_height=1,
        observed_mono={k: time.monotonic() for k in ('sol_usd', 'balances', 'blockhash')})
    seen = []
    original = risk.memecoin_entry_allowed
    def observe(ctx, **kwargs):
        seen.append(ctx)
        return original(ctx, **kwargs)
    monkeypatch.setattr(risk, 'memecoin_entry_allowed', observe)
    got = ln.handle(buy('tip-flags'))
    assert sent, got
    ctx = seen[-1]
    row = dict(conn.execute('SELECT * FROM memecoin_pending_entries ORDER BY id DESC LIMIT 1').fetchone())
    assert row['funding_asset'] == ('sol' if native else 'usdc')
    assert int(row['funding_overhead_atomic']) == 5_000_000 + (lane.NATIVE_ATA_RENT_RESERVE_LAMPORTS if native else 0)
    assert ctx.gas_entry_sol == cfg.MEMECOIN_GAS_ENTRY_SOL + (0 if native else .005)


@pytest.mark.parametrize('network,asset,overhead', [('8453', 'usdc', 0), ('solana', None, 0),
                                                  ('solana', 'usdc', -1), ('solana', 'usdc', True)])
def test_usdc_tip_schema_does_not_widen_other_funding_shapes(conn, network, asset, overhead):
    from sniper.persistence import db_memecoin as db
    with pytest.raises(ValueError):
        db.reserve_pending(conn, mint='tip-bad', source_user_id='uid', strategy='s', notional=25,
            reserved_at='2026-10-10T12:00:00+00:00', network_id=network,
            funding_asset=asset, funding_overhead_atomic=overhead)
