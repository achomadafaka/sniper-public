"""MEMECOIN_PAPER_ARM_SAMPLE_DIVISOR takes 1 paper event in N, keyed on the origin.

Default 1 = every event, so the knob ships inert. The key is `origin_event_id` (the entry trace
reads it from `ev.fomo_event_id`), which gives two properties the A/B depends on:

  * every arm drops the SAME events, so the arms stay comparable on a common event set;
  * the decision is deterministic, so a replay of the same tape makes the same choice.
"""
import sqlite3

from sniper.feeds.fomo_feed import CopyEvent
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_copy_trader as ct
from sniper.settings import memecoin as cfg


ARMS = ["funded_clone", "profile_matched_v1"]


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "u1", "alice")
    return c


def _ev(eid, side="buy"):
    return CopyEvent("u1", "alice", "MINT", "PEPE", side, 0.005, 1e6, 100.0, True,
                     eid, "t", "t", 0.3, network_id="solana")


class _Quote:
    def __init__(self):
        self.calls = []

    def __call__(self, mint, network_id, size_usd):
        self.calls.append(mint)
        return 0.005, "jupiter"


def _run(conn, ev, quote=None):
    return ct.copy_event_to_arms(conn, ev, 100.0, "t", arms=ARMS,
                                 fill_fn=lambda *a, **k: (0.005, "jupiter"), quote_fn=quote)


def _taken(divisor, n=400):
    return [str(i) for i in range(n) if _sample(divisor, str(i))]


def _sample(divisor, key):
    import zlib
    return divisor <= 1 or zlib.crc32(key.encode()) % divisor == 0


def test_the_default_divisor_takes_every_event(monkeypatch):
    """Ship-inert. Divisor 1 must not skip a single event."""
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_SAMPLE_DIVISOR", 1)
    assert all(cfg.paper_arm_samples_event(str(i)) for i in range(500))
    out = _run(_conn(), _ev("e1"))
    assert "skipped_sampled_out" not in set(out.values())


def test_a_divisor_of_four_takes_about_a_quarter(monkeypatch):
    """A sampler that takes everything, or nothing, would pass a 'does it skip' test either way."""
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_SAMPLE_DIVISOR", 4)
    taken = [i for i in range(2000) if cfg.paper_arm_samples_event(str(i))]
    assert 0.20 < len(taken) / 2000 < 0.30, f"took {len(taken)} of 2000"


def test_the_decision_is_stable_across_calls(monkeypatch):
    """crc32, not hash(): PYTHONHASHSEED would make the subset differ between processes."""
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_SAMPLE_DIVISOR", 4)
    first = [cfg.paper_arm_samples_event(str(i)) for i in range(300)]
    second = [cfg.paper_arm_samples_event(str(i)) for i in range(300)]
    assert first == second
    # and it is the documented crc32 rule, not some other stable function
    assert first == [_sample(4, str(i)) for i in range(300)]


def test_a_sampled_out_event_never_buys_a_quote(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_SAMPLE_DIVISOR", 4)
    dropped = next(str(i) for i in range(500) if not cfg.paper_arm_samples_event(str(i)))
    quote = _Quote()
    out = _run(_conn(), _ev(dropped), quote=quote)
    assert out == {a: "skipped_sampled_out" for a in ARMS}
    assert quote.calls == [], "a sampled-out event bought a quote before refusing"


def test_every_arm_drops_the_same_events(monkeypatch):
    """The A/B property. A per-arm roll would decorrelate the arms and destroy the comparison."""
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_SAMPLE_DIVISOR", 4)
    c = _conn()
    for i in range(60):
        out = _run(c, _ev("ev-%d" % i))
        assert len(set(out.values())) == 1, f"arms disagreed on ev-{i}: {out}"


def test_an_origin_with_no_id_is_taken(monkeypatch):
    """A missing key cannot be sampled stably, and dropping the signal is the destructive answer."""
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_SAMPLE_DIVISOR", 4)
    assert cfg.paper_arm_samples_event(None) is True
    assert cfg.paper_arm_samples_event("") is True
    assert cfg.paper_arm_samples_event("   ") is True


def test_a_sell_is_never_sampled_out(monkeypatch):
    """A position the lane already opened must stay closable."""
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_SAMPLE_DIVISOR", 1)
    c = _conn()
    _run(c, _ev("open-1"))
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_SAMPLE_DIVISOR", 4)
    dropped = next(str(i) for i in range(500) if not cfg.paper_arm_samples_event(str(i)))
    out = _run(c, _ev(dropped, side="sell"))
    assert "skipped_sampled_out" not in set(out.values())
