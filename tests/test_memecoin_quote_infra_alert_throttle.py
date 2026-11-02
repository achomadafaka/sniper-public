"""The INFRA/RECOVERED pair throttle: one page per provider flap window, never a stranded red.

2026-08-14: brand-new coins Jupiter had not indexed blipped the exact-quote provider one tick at a
time. Each blip paged twice (milestone-1 + recovery) — 257 of 380 Telegram sends in one hour —
and Telegram's rate limit queued the REAL alerts up to 95s behind the flaps.
"""
import pytest

from sniper.services import memecoin_alerts as A


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    A._quote_infra_state.clear()
    sent = []
    monkeypatch.setattr(A, "send_alert", lambda kind, msg, **kw: sent.append(kind) or True)
    yield sent
    A._quote_infra_state.clear()


def _infra(milestone, provider="solana:exact_quote"):
    return {"provider": provider, "failure": "rate_limit", "affected": 1,
            "max_streak": milestone, "milestone": milestone}


def _recovery(provider="solana:exact_quote"):
    return {"kind": "recovery", "provider": provider, "affected": 1, "max_streak": 1}


def test_a_flap_pair_never_pages(_clean):
    """Second pass (operator, 2026-08-14): milestone 1 is silent, full stop — every restart and
    every unindexed coin produces one. A real outage pages at milestone 5 within seconds."""
    sent = _clean
    A.alert_quote_infra_episode(_infra(1))
    A.alert_quote_infra_episode(_recovery())
    A.alert_quote_infra_episode(_infra(1))
    A.alert_quote_infra_episode(_recovery())
    assert sent == [], sent


def test_a_real_outage_still_pages_its_milestones_and_recovery(_clean):
    sent = _clean
    A.alert_quote_infra_episode(_infra(1))
    A.alert_quote_infra_episode(_infra(5))
    A.alert_quote_infra_episode(_infra(20))
    A.alert_quote_infra_episode(_recovery())
    assert sent == ["QUOTE-INFRA", "QUOTE-INFRA", "QUOTE-INFRA-RECOVERED"]


def test_escalation_pages_even_when_milestone_one_was_suppressed(_clean):
    sent = _clean
    A.alert_quote_infra_episode(_infra(1))
    A.alert_quote_infra_episode(_recovery())
    sent.clear()
    A.alert_quote_infra_episode(_infra(1))      # suppressed flap start
    A.alert_quote_infra_episode(_infra(5))      # but it turned REAL
    A.alert_quote_infra_episode(_recovery())    # paged episode -> recovery must send
    assert sent == ["QUOTE-INFRA", "QUOTE-INFRA-RECOVERED"]


def test_providers_do_not_share_state(_clean):
    sent = _clean
    A.alert_quote_infra_episode(_infra(5, "solana:exact_quote"))
    A.alert_quote_infra_episode(_infra(5, "4663:exact_quote"))
    assert sent == ["QUOTE-INFRA", "QUOTE-INFRA"]
