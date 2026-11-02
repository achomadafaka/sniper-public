from sniper.services.memecoin_alerts import alert_quote_infra_episode, quote_infra_episode_events


def _change(pid, before, after, *, provider="solana:jupiter", failure="rpc_transport"):
    return {
        "position_id": pid, "provider": provider, "before": before, "after": after,
        "failure": failure,
    }


def test_provider_episode_alerts_only_at_start_and_milestones():
    assert quote_infra_episode_events([_change(1, 0, 1), _change(2, 0, 1)]) == ({
        "kind": "outage", "provider": "solana:jupiter", "failure": "rpc_transport",
        "affected": 2, "max_streak": 1, "milestone": 1,
    },)
    assert quote_infra_episode_events([_change(1, 1, 2)]) == ()
    assert quote_infra_episode_events([_change(1, 4, 5)]) == ({
        "kind": "outage", "provider": "solana:jupiter", "failure": "rpc_transport",
        "affected": 1, "max_streak": 5, "milestone": 5,
    },)
    assert quote_infra_episode_events([_change(1, 19, 20)]) == ({
        "kind": "outage", "provider": "solana:jupiter", "failure": "rpc_transport",
        "affected": 1, "max_streak": 20, "milestone": 20,
    },)
    assert quote_infra_episode_events([_change(1, 6, 7)]) == ()


def test_provider_recovery_requires_no_remaining_failure_in_group():
    assert quote_infra_episode_events([_change(1, 7, 0, failure=None)]) == ({
        "kind": "recovery", "provider": "solana:jupiter", "affected": 1,
        "max_streak": 7,
    },)
    assert quote_infra_episode_events([
        _change(1, 7, 0, failure=None), _change(2, 7, 8),
    ]) == ()


def test_provider_groups_and_token_failures_do_not_cross_contaminate():
    events = quote_infra_episode_events([
        _change(1, 0, 1, provider="solana:jupiter"),
        _change(2, 0, 1, provider="4663:rpc"),
        _change(3, 0, 0, provider="solana:jupiter", failure="no_route"),
    ])
    assert [event["provider"] for event in events] == ["4663:rpc", "solana:jupiter"]
    assert all(event["affected"] == 1 for event in events)


def test_episode_alert_copy_names_provider_and_preserves_token_counter_truth():
    sent = []
    alert_quote_infra_episode({
        "kind": "outage", "provider": "solana:jupiter", "failure": "rpc_transport",
        "affected": 3, "max_streak": 5, "milestone": 5,
    }, send_fn=lambda text: sent.append(text) or (True, 0.0))
    assert sent == [
        "🔴 memecoin QUOTE-INFRA | provider=solana:jupiter failure=rpc_transport affected=3 "
        "max_streak=5 milestone=5; token unsellable counters unchanged"]


def test_provider_recovery_is_green_and_does_not_claim_token_recovery():
    # A recovery only sends for a PAGED episode (flap throttle, 2026-08-14): a fully-suppressed
    # flap suppresses its own recovery too. Seed the paged INFRA first, then assert the recovery.
    from sniper.services import memecoin_alerts as A
    A._quote_infra_state.clear()
    sent = []
    alert_quote_infra_episode({
        "provider": "solana:jupiter", "failure": "rate_limit", "affected": 1,
        "max_streak": 5, "milestone": 5,
    }, send_fn=lambda text: (True, 0.0))
    alert_quote_infra_episode({
        "kind": "recovery", "provider": "solana:jupiter", "affected": 1,
        "max_streak": 1,
    }, send_fn=lambda text: sent.append(text) or (True, 0.0))

    assert sent == [
        "🟢 memecoin QUOTE-INFRA-RECOVERED | provider=solana:jupiter recovered affected=1 "
        "prior_max_streak=1; token unsellable counters unchanged"]


def test_exact_quote_episode_names_failed_boundary_without_splitting_recovery_group():
    changes = [
        _change(1, 1, 0, provider="solana:exact_quote", failure=None),
        {**_change(2, 0, 1, provider="solana:exact_quote"),
         "boundary": "solana:jupiter"},
    ]

    assert quote_infra_episode_events(changes) == ({
        "kind": "outage", "provider": "solana:exact_quote",
        "boundary": "solana:jupiter", "failure": "rpc_transport",
        "affected": 1, "max_streak": 1, "milestone": 1,
    },)

    # Milestone-1 pages are silent (operator, 2026-08-14) — the event REDUCTION above is what this
    # test protects; verify the boundary-carrying message text at a paging milestone instead.
    from sniper.services import memecoin_alerts as A
    A._quote_infra_state.clear()
    sent = []
    alert_quote_infra_episode(
        {**quote_infra_episode_events(changes)[0], "milestone": 5, "max_streak": 5},
        send_fn=lambda text: sent.append(text) or (True, 0.0),
    )
    assert sent == [
        "🔴 memecoin QUOTE-INFRA | provider=solana:exact_quote "
        "boundary=solana:jupiter failure=rpc_transport affected=1 max_streak=5 milestone=5; "
        "token unsellable counters unchanged"]
