"""The alert must show a market cap whenever one is obtainable — and must never let that
display value reach a funded decision.

2026-08-30. OMEGA (0x8338e93a…, 4663) filled and alerted as `mcap ~?` while DexScreener had it at
$52,800 the whole time. Every stored source was empty: feed event 0, `resolve_mint_mcap` None,
classification `effective_mcap` None (banded `assumed_nano`), `entry_mcap` NULL, no mark.
"""
import pytest

from sniper.services import memecoin_follow_activity as fa
from sniper.services.memecoin_telegram import _display_mcap


class _Ev:
    mint = "0x8338e93ab2fced3be98290e035594201989595ce"
    network_id = "4663"
    their_market_cap = 0.0
    ticker = "OMEGA"
    source_handle = "CryoPalmar"


def test_the_alert_renders_a_live_mcap_when_every_stored_source_is_empty(monkeypatch):
    monkeypatch.setattr(fa, "_live_display_mcap", lambda mint, net: 52800.0)
    assert _display_mcap(_Ev(), {"display_market_cap": 52800.0}) != "?"


def test_a_stored_value_is_preferred_over_a_provider_call(monkeypatch):
    """The stored number is the one the decision saw. A live fetch minutes later is a different
    moment, so it is a fallback and never an override."""
    called = []
    monkeypatch.setattr(fa, "_live_display_mcap",
                        lambda mint, net: called.append(1) or 999.0)
    assert _display_mcap(_Ev(), {"display_market_cap": 41000.0}) == _display_mcap(
        _Ev(), {"display_market_cap": 41000.0})
    assert not called, "a provider was called while a stored market cap was available"


def test_unknown_still_renders_honestly_rather_than_as_zero():
    assert _display_mcap(_Ev(), {"display_market_cap": None}) == "?"
    assert _display_mcap(_Ev(), {"display_market_cap": 0.0}) == "?"


def test_a_provider_failure_never_breaks_the_alert(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("provider down")
    monkeypatch.setattr("sniper.execution.solana_quote.dexscreener_stats", boom)
    assert fa._live_display_mcap("0xabc", "4663") is None


@pytest.mark.parametrize("stats", [None, {}, {"market_cap": None},
                                   {"market_cap": 0}, {"market_cap": -5}])
def test_a_missing_or_nonpositive_cap_is_unknown_not_a_number(monkeypatch, stats):
    monkeypatch.setattr("sniper.execution.solana_quote.dexscreener_stats",
                        lambda *_a, **_k: stats)
    assert fa._live_display_mcap("0xabc", "4663") is None


def test_no_call_without_a_mint_or_chain(monkeypatch):
    called = []
    monkeypatch.setattr("sniper.execution.solana_quote.dexscreener_stats",
                        lambda *_a, **_k: called.append(1) or {"market_cap": 1.0})
    assert fa._live_display_mcap(None, "4663") is None
    assert fa._live_display_mcap("0xabc", None) is None
    assert not called


def test_the_display_path_is_NOT_a_decision_input():
    """`entry_mcap` feeds funded_policy_for_arm -> resolve_policy, which picks the position's exit
    rails. If this helper ever appears on that path, a display convenience starts choosing when we
    sell — which is exactly what the 2026-08-30 band-source pin exists to prevent.

    Mutation check: call `_live_display_mcap` from anywhere under the funded entry/policy modules
    and this goes red.
    """
    import pathlib
    root = pathlib.Path("sniper")
    # memecoin_runner.py joined the allowlist 2026-09-11 (d2a1a9e5): `_display_mcap_lookup` runs in
    # the Telegram SENDER thread after the message is built, fills `context["display_market_cap"]`
    # and nothing else. The funded runner and the policy modules stay forbidden.
    allowed = {"memecoin_follow_activity.py", "memecoin_runner.py"}
    offenders = [
        str(p) for p in root.rglob("*.py")
        if p.name not in allowed
        and "_live_display_mcap" in p.read_text()
    ]
    assert not offenders, f"display-only mcap reached: {offenders}"
    runner = (root / "services" / "memecoin_runner.py").read_text()
    body = runner.split("def _display_mcap_lookup(")[1].split("\ndef ")[0]
    assert runner.count("_live_display_mcap") == body.count("_live_display_mcap"), \
        "the runner may use the display mcap only inside _display_mcap_lookup"
