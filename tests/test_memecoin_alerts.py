"""Item 20 — real-money operational alerts. Arming without these is flying blind. The Telegram
sender already works; this is the wiring + formatting. send_fn is injectable so tests assert
content without sending; every alert is best-effort (a send failure never raises into the loop)."""
from sniper.services import memecoin_alerts as al
import pytest


class _Sink:
    def __init__(self): self.msgs = []
    def __call__(self, text): self.msgs.append(text); return (True, 1.0)


def test_entry_alert_carries_key_fields():
    s = _Sink()
    al.alert_entry("MINT", 0.02, 10.0, "SIG123456", send_fn=s)
    assert "REAL-FILLED" in s.msgs[0] and "MINT" in s.msgs[0]
    assert "10.00" in s.msgs[0] and "SIG12345" in s.msgs[0]


def test_entry_alert_includes_copy_provenance_and_chain_link():
    s = _Sink()

    al.alert_entry(
        "0xa244d68ab295e37ec5f566ca5ecd32b660c7b126", 0.000205138, 70.11,
        "0x9178f7a8aa24eab0b8ca9163e411bb36cb6a2a94ffc6423f08347555c1f88bbb",
        ticker="WORLD", source_user_id="bb2ab76d-f022-53a1-820e-a5f46522576b",
        source_size_usd=999.187419, source_price=0.000235191,
        entry_lag_s=40.534614, policy="alpha_b", network_id="4663", send_fn=s,
    )

    assert "source: bb2ab76d-f022-53a1-820e-a5f46522576b bought $999 @ 0.000235191" in s.msgs[0]
    assert "feed lag: 40.5s" in s.msgs[0]
    assert "our fill: $70.11 @ 0.000205138 (-12.8%)" in s.msgs[0]
    assert "policy: alpha_b | chain: 4663" in s.msgs[0]
    assert ("https://robinhoodchain.blockscout.com/tx/"
            "0x9178f7a8aa24eab0b8ca9163e411bb36cb6a2a94ffc6423f08347555c1f88bbb") in s.msgs[0]


def test_entry_alert_attaches_origin_event_to_delivery_audit(monkeypatch):
    queued = []
    monkeypatch.setattr(
        "sniper.services.memecoin_telegram.enqueue_telegram",
        lambda text, **meta: queued.append((text, meta)) or True,
        raising=False,
    )

    al.alert_entry("MINT", 0.02, 10.0, "SIG", source_id="act-1")

    assert queued[0][1]["source_id"] == "act-1"


def test_entry_result_alert_names_every_operator_visible_refusal(monkeypatch):
    """Un-silences every kind: this pins the NAMING/detail contract for each refusal, which must
    hold regardless of the 2026-08-06 delivery policy that keeps routine declines off the phone.
    Which kinds actually send is covered by test_memecoin_real_alerts.py."""
    monkeypatch.setattr(al.cfg, "MEMECOIN_ALERT_SILENT_REFUSALS", frozenset())
    cases = [
        ({"status": "skipped_mcap", "mcap": 55_100_000, "reason": "over_ceiling"},
         "MCAP-SKIP", "$55.1M > $30.0M ceiling"),
        ({"status": "skipped_mcap", "mcap": None, "reason": "mcap_unresolved"},
         "MCAP-SKIP", "market cap unresolved"),
        ({"status": "aborted", "reason": "price impact 24.90% over 5.0% cap"},
         "SLIPPAGE-REFUSAL", "price impact 24.90% over 5.0% cap"),
        ({"status": "dropped_network", "network": "1"},
         "NETWORK-DROP", "network=1 not enabled"),
        ({"status": "evm_unarmed", "route": "evm:8453"},
         "NETWORK-DROP", "evm:8453 has no funded real context"),
        ({"status": "unsafe", "reason": "live mint authority (infinite-mint rug risk)"},
         "TOKEN-SAFETY-REFUSAL", "live mint authority"),
        ({"status": "route_failed", "reason":
          "configured sell venue returned no executable quote "
          "(route=direct_v3_single_hop; other venues not tested)"},
         "ENTRY-ROUTE-REFUSAL", "route=direct_v3_single_hop"),
        ({"status": "rejected", "reason": "max concurrent positions"},
         "RISK-REFUSAL", "max concurrent positions"),
        ({"status": "stale", "reason": "entry older than 120s"},
         "STALE-SKIP", "entry older than 120s"),
        ({"status": "disarmed"},
         "ENTRY-DISARMED", "real entries disabled or kill switch active"),
        ({"status": "skipped_mcap", "mcap": float("nan"), "reason": "mcap_unresolved"},
         "MCAP-SKIP", "market cap unresolved"),
    ]
    for result, kind, detail in cases:
        sink = _Sink()
        al.alert_entry_result(
            "MINT", result, ceiling=30_000_000, handle="alice", ticker="COIN", send_fn=sink,
        )
        assert len(sink.msgs) == 1
        assert kind in sink.msgs[0] and detail in sink.msgs[0]


def test_exit_alert_shows_reason_and_pnl():
    s = _Sink()
    al.alert_exit("MINT", -3.5, "b_timecap", "SIGabcdef", send_fn=s)
    assert "EXIT" in s.msgs[0] and "b_timecap" in s.msgs[0] and "-3.50" in s.msgs[0]


@pytest.mark.parametrize('reason', ['token_balance_unreadable', 'usdc_balance_unreadable', None])
def test_unreadable_exit_balance_does_not_page_every_attempt(monkeypatch, reason):
    from sniper.services import memecoin_real_runner as runner
    queued = []
    monkeypatch.setattr('sniper.services.memecoin_telegram.enqueue_telegram',
                        lambda text, **meta: queued.append((text, meta)) or True)
    for _ in range(3):
        runner._alert_exit_result('MINT', 74106, 'real_stop',
                                  {'status': 'balance_unreadable', 'reason': reason})
    assert queued == []


def test_zero_exit_balance_never_claims_the_sell_ladder_was_exhausted(monkeypatch):
    from sniper.services import memecoin_real_runner as runner
    queued = []
    monkeypatch.setattr('sniper.services.memecoin_telegram.enqueue_telegram',
                        lambda text, **meta: queued.append((text, meta)) or True)
    runner._alert_exit_result('MINT', 74106, 'real_stop',
                              {'status': 'stuck', 'reason': 'token_balance_zero'},
                              handle='owner', ticker='COIN')
    assert len(queued) == 1
    text, meta = queued[0]
    assert meta['kind'] == 'operational:EXIT-DEFERRED'
    assert 'token balance is zero' in text and 'pos#74106' in text and 'COIN (owner)' in text
    assert 'STUCK-BAG' not in text and 'ladder exhausted' not in text
    assert 'no sale proven' in text and 'reconciliation' in text


def test_stuck_exit_alert_preserves_its_actual_route_failure(monkeypatch):
    from sniper.services import memecoin_real_runner as runner
    queued = []
    monkeypatch.setattr('sniper.services.memecoin_telegram.enqueue_telegram',
                        lambda text, **meta: queued.append((text, meta)) or True)
    runner._alert_exit_result('MINT', 74106, 'real_stop',
                              {'status': 'stuck', 'failure_type': 'route_capacity'})
    text, meta = queued[0]
    assert meta['kind'] == 'operational:STUCK-BAG'
    assert 'route_capacity' in text


def test_held_close_claim_does_not_issue_another_exit_alert(monkeypatch):
    from sniper.services import memecoin_real_runner as runner
    queued = []
    monkeypatch.setattr('sniper.services.memecoin_telegram.enqueue_telegram',
                        lambda text, **meta: queued.append((text, meta)) or True)
    runner._alert_exit_result('MINT', 74106, 'real_stop',
                              {'status': 'already_closing', 'sig': 'SIG123456789'})
    assert queued == []


@pytest.mark.parametrize('reason,sig_field,kind,action', [
    ('real_tp', 'sig', 'CLOSE-UNKNOWN', 'full close'),
    ('mirror', 'tx_hash', 'CLOSE-UNKNOWN', 'full close'),
    ('source_not_landed', 'sig', 'CLOSE-UNKNOWN', 'full close'),
    ('derisk', 'sig', 'DERISK-UNKNOWN', 'derisk'),
])
def test_unknown_exit_reports_its_action_and_keeps_the_owned_close_alert(
        monkeypatch, caplog, reason, sig_field, kind, action):
    from sniper.services import memecoin_real_runner as runner
    queued = []
    monkeypatch.setattr('sniper.services.memecoin_telegram.notification_lifecycle_active', lambda: True)
    monkeypatch.setattr('sniper.services.memecoin_telegram.enqueue_telegram',
                        lambda text, **meta: queued.append((text, meta)) or True)
    runner._alert_exit_result('MINT', 73945, reason, {'status': 'unknown', sig_field: 'SIG123456789'},
                              handle='owner', ticker='COIN')
    assert len(queued) == 1
    text, meta = queued[0]
    assert meta['kind'] == 'operational:' + kind
    assert 'SIG12345' in text and 'COIN (owner)' in text
    assert action + ' confirm UNKNOWN' in caplog.text
    if reason == 'derisk':
        assert 'partial-sell' in text and 're-arms' in text
    else:
        assert 'full sell' in text and 'close claim stays held' in text
        assert 'partial' not in text and 're-arms' not in text


def test_evm_route_capacity_partial_unknown_keeps_the_partial_alert(monkeypatch, caplog):
    from sniper.execution.evm_exit_executor import execute_exit_evm, execute_partial_exit_evm
    from sniper.services import memecoin_real_runner as runner
    from test_memecoin_evm_exit_executor import _c, _pos, _seams, _partial_seams
    c = _c()
    try:
        pos = _pos(c)
        full, _ = _seams(quote=None)
        partial, _ = _partial_seams(confirm='pending')
        result = execute_exit_evm(
            c, pos, 'real_trail', 't', slippage_ladder=[3], **full,
            quote_miss_fn=lambda _slip: {'code': 'route_capacity'},
            partial_exit_fn=lambda fraction: execute_partial_exit_evm(
                c, pos, fraction, 't', slippage_ladder=[.03], sleep_fn=lambda _: None, **partial))
        assert result['status'] == 'unknown' and result['fallback_fraction'] == .5
        row = c.execute('SELECT close_claim_id,derisk_sig FROM memecoin_positions').fetchone()
        assert row['close_claim_id'] is None and row['derisk_sig'] == result['sig']
        queued = []
        monkeypatch.setattr('sniper.services.memecoin_telegram.enqueue_telegram',
                            lambda text, **meta: queued.append((text, meta)) or True)
        runner._alert_exit_result(pos['mint'], pos['id'], 'real_trail', result)
        assert queued[0][1]['kind'] == 'operational:DERISK-UNKNOWN'
        assert 'partial-sell' in queued[0][0] and 're-arms' in queued[0][0]
        assert 'close claim stays held' not in queued[0][0]
        assert 'derisk confirm UNKNOWN' in caplog.text
    finally:
        c.close()


@pytest.mark.parametrize('fallback,fraction', [
    ('route_capacity', None), ('route_capacity', 0.), ('route_capacity', 1.),
    ('route_capacity', True), ('route_capacity', float('nan')), ('route_capacity', float('inf')),
    ('route_capacity', '.5'), ('other', .5),
])
def test_invalid_partial_fallback_metadata_does_not_invent_a_partial(monkeypatch, fallback, fraction):
    from sniper.services import memecoin_real_runner as runner
    kinds = []
    monkeypatch.setattr(al, 'send_alert', lambda kind, text, **kw: kinds.append(kind))
    runner._alert_exit_result('MINT', 1, 'real_tp', {
        'status': 'unknown', 'sig': 'SIG', 'fallback_reason': fallback, 'fallback_fraction': fraction})
    assert kinds == ['CLOSE-UNKNOWN']


def test_stuck_bag_and_daily_loss_and_kill():
    s = _Sink()
    al.alert_stuck_bag("MINT", 7, send_fn=s)
    al.alert_daily_loss("2026-07-09", -30.0, send_fn=s)
    al.alert_kill_switch(send_fn=s)
    assert "STUCK-BAG" in s.msgs[0] and "EXIT" not in s.msgs[0]
    assert "DAILY-LOSS" in s.msgs[1] and "2026-07-09" in s.msgs[1]
    assert "KILL-SWITCH" in s.msgs[2]


def test_alert_never_raises_on_send_failure():
    def boom(text): raise RuntimeError("telegram down")
    # returns a (False, _) tuple instead of raising -> can't break the trade loop
    ok, _ = al.alert_gas_low(0.001, 0.006, send_fn=boom)
    assert ok is False


def test_default_operational_alert_is_enqueued_not_sent_inline(monkeypatch):
    queued = []
    monkeypatch.setattr(
        "sniper.services.memecoin_telegram.enqueue_telegram",
        lambda text, **meta: queued.append((text, meta)) or True,
        raising=False,
    )

    ok, elapsed_ms = al.alert_gas_low(0.001, 0.006)

    assert ok is True
    assert elapsed_ms == 0.0
    assert len(queued) == 1
    assert "GAS-LOW" in queued[0][0]
    assert queued[0][1]["kind"] == "operational:GAS-LOW"
