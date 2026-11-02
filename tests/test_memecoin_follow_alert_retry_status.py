"""A transient fresh-pool miss is not a verdict while the re-quote lane still owns it.

HOODLAND, 2026-09-08 on chain 4663: the follow-lane alert said `entry: ABORTED · reserved` at
14:15:41 with `no_route_all_venues`. The retry lane re-quoted and FILLED at 14:16:17, 36 seconds
later, and the position closed `real_tp` for +$11.03. No correction was ever sent, so the operator
read a refusal for a trade that had already profited and asked for it to be fixed.

`_alert_entry_result` in the runner already returned early for exactly this case — "it will fill
silently or page ONCE on give-up". The follow-lane alert is a SECOND sender that never learned the
rule, which is why the suppression looked correct in review and still paged in production.

The fix reports `retrying`, not silence: the operator should still see that the trader bought, and
that our decision is open rather than settled.
"""
import pytest

from sniper.services.memecoin_follow_activity import _retry_pending_status as S

TRANSIENT = {"status": "aborted", "reason": "no_route_all_venues"}
REAL_REFUSAL = {"status": "rejected", "reason": "coin already ran +57% past their entry"}


@pytest.fixture(autouse=True)
def _lane_on(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_WINDOW_S", 360.0, raising=False)


def test_the_hoodland_case_reads_as_retrying_not_aborted():
    assert S("aborted", TRANSIENT) == "retrying"


def test_a_real_refusal_keeps_its_own_word():
    """A chase refusal is final. Calling it `retrying` would be the same lie in the other
    direction — the operator would wait for a fill that is never coming."""
    assert S("rejected", REAL_REFUSAL) == "rejected"


def test_a_fill_is_never_rewritten():
    assert S("filled", {"status": "filled"}) == "filled"


def test_pending_stays_pending():
    assert S(None, None) == "pending"


def test_nothing_is_rewritten_when_the_lane_is_off(monkeypatch):
    """With no retry lane the one-shot page IS the operator's only signal, so `aborted` is true."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_WINDOW_S", 0.0, raising=False)
    assert S("aborted", TRANSIENT) == "aborted"


def test_a_broken_read_never_breaks_the_alert(monkeypatch):
    """This is alert cosmetics on the funded path; it must degrade to the plain status."""
    import sniper.services.memecoin_real_runner as rr
    monkeypatch.setattr(rr, "_is_transient_entry_miss",
                        lambda _r: (_ for _ in ()).throw(RuntimeError("boom")))
    assert S("aborted", TRANSIENT) == "aborted"


def test_the_follow_sender_actually_uses_it():
    """The bug was a second sender that did not know the rule; pin that it now does."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "sniper" / "services" / "memecoin_follow_activity.py").read_text()
    assert '"entry_status": _retry_pending_status(' in src
