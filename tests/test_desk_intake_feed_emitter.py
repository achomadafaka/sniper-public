"""Frozen intake feed selection at universe startup; all files and ledgers are fixtures."""
import json
import pytest

from sniper.persistence import db_memecoin_desk as D
from sniper.services import memecoin_desk_intake as I
from sniper.services import memecoin_desk_intake_book as B
from test_memecoin_desk_contract import policy
from test_memecoin_desk_intake_book import SOL, _ps_print, _ps_worker, printed
from test_memecoin_desk_pumpswap_candidates import MINT, NOW, SIG, event, facts


FEEDS = ("dexscreener", "fomo", "pumpfun")
ABSENT = object()


def cohort(root, *, cid="active", feeds=FEEDS, producer="U", intake=True):
    hard = {**policy(), "intake": {"version": "intake-v1", "mcap_safety": [5000, 50_000_000],
                                   "liq_min_quote_sol": 40}}
    if not intake:
        hard.pop("intake")
    elif feeds is not ABSENT:
        hard["intake"]["feeds"] = list(feeds) if isinstance(feeds, tuple) else feeds
    conn = D.connect(root / "memecoin_desk.db")
    D.register_cohort(conn, cohort_id=cid, producer=producer, prompt_version="desk-judge-v26",
                      policy={"hard_filter": hard}, created_at="2026-10-27T12:00:00+00:00")
    conn.close()


def sight(base, feed, *, at=NOW - 10, network="solana", offset=0, filt="active"):
    out = I.open_day(base / "intake", feed, at)
    I.record_poll(out, feed, filt, at,
                  [{"network_id": network, "chain_raw": SOL, "mint": MINT, "venue": "pumpswap", "raw": {}}],
                  offset_s=offset)
    out.close()


def start(root, monkeypatch, *, cid="active"):
    (root / "desk").mkdir(exist_ok=True)
    if cid is None:
        monkeypatch.delenv("MEMECOIN_DESK_U_COHORT_ID", raising=False)
    else:
        monkeypatch.setenv("MEMECOIN_DESK_U_COHORT_ID", cid)
    monkeypatch.setenv("MEMECOIN_DESK_INTAKE_EMIT", "1")
    return _ps_worker(root / "desk", monkeypatch)


@pytest.mark.parametrize("feed", FEEDS)
def test_each_frozen_feed_emits_with_its_own_filter_and_rank(tmp_path, monkeypatch, feed):
    cohort(tmp_path)
    w = start(tmp_path, monkeypatch)
    sight(w.base, feed, filt="raw_filter")
    w.intake_book.reload(NOW)
    [(oid, body)] = printed(w.intake_book, NOW)
    claim = json.loads(body)["intake"]
    assert set(w.intake_book.feeds) == set(FEEDS)
    assert (claim["feed"], claim["filter"], claim["provider_rank"], claim["rank_kind"]) == (feed, "raw_filter", 1, "list_position")
    assert w.intake_book.take_pending() == [(oid, body)]


def test_legacy_cohort_without_feeds_stays_pumpfun_only(tmp_path, monkeypatch):
    cohort(tmp_path, feeds=ABSENT)
    w = start(tmp_path, monkeypatch)
    for feed in FEEDS:
        sight(w.base, feed)
    w.intake_book.reload(NOW)
    rows = printed(w.intake_book, NOW)
    assert B.FEEDS == w.intake_book.feeds == ("pumpfun",)
    assert [json.loads(body)["intake"]["feed"] for _, body in rows] == ["pumpfun"]


def test_no_configured_cohort_preserves_legacy_without_creating_a_desk_db(tmp_path, monkeypatch):
    w = start(tmp_path, monkeypatch, cid=None)
    assert w.intake_book.feeds == ("pumpfun",)
    assert not (tmp_path / "memecoin_desk.db").exists()


@pytest.mark.parametrize("failure", ["missing", "missing_db", "unreadable", "wrong_producer", "no_intake", "invalid_json"])
def test_bad_configured_cohort_disables_only_intake(tmp_path, monkeypatch, caplog, failure):
    if failure == "unreadable":
        (tmp_path / "memecoin_desk.db").write_text("not a sqlite database")
    elif failure == "missing":
        cohort(tmp_path, cid="another-cohort")
    elif failure == "invalid_json":
        conn = D.connect(tmp_path / "memecoin_desk.db")
        conn.execute("INSERT INTO desk_cohorts(cohort_id,producer,prompt_version,policy_json,created_at) "
                     "VALUES('active','U','desk-judge-v26','not json','2026-10-27T12:00:00+00:00')")
        conn.commit()
        conn.close()
    elif failure != "missing_db":
        cohort(tmp_path, producer="H" if failure == "wrong_producer" else "U", intake=failure != "no_intake")
    w = start(tmp_path, monkeypatch)
    assert w.intake_book is None
    assert w.ps_capture is True and w.stop is False
    assert "intake_cohort_" in caplog.text
    assert not (w.base / "intake_emitter.db").exists()
    from sniper.services.memecoin_desk_safety import pumpswap_pool_address
    w.on_pumpswap(_ps_print(pumpswap_pool_address(MINT)), 10, "fixture-signature", 0, None, None, NOW, NOW)
    assert w.ps_hold_rows == 1


@pytest.mark.parametrize("feeds", [[], ["pumpfun", "unknown"], ["pumpfun", "pumpfun"], "pumpfun", None])
def test_invalid_frozen_feed_policy_never_falls_back(tmp_path, monkeypatch, caplog, feeds):
    cohort(tmp_path, feeds=feeds)
    w = start(tmp_path, monkeypatch)
    assert w.intake_book is None
    assert "intake_cohort_" in caplog.text
    assert not (w.base / "intake_emitter.db").exists()


def test_selection_is_frozen_until_restart_and_a_changed_cid_fails_closed(tmp_path, monkeypatch):
    cohort(tmp_path)
    w = start(tmp_path, monkeypatch)
    (tmp_path / "memecoin_desk.db").rename(tmp_path / "prior-cohort.db")
    monkeypatch.setenv("MEMECOIN_DESK_U_COHORT_ID", "missing-next-cohort")
    sight(w.base, "fomo")
    w.intake_book.reload(NOW)
    assert len(printed(w.intake_book, NOW)) == 1
    assert start(tmp_path, monkeypatch, cid="missing-next-cohort").intake_book is None


@pytest.mark.parametrize("feed", FEEDS)
@pytest.mark.parametrize("bad", ["other_chain", "stale", "future", "no_clock"])
def test_wrong_chain_stale_and_unknown_clock_do_not_emit(tmp_path, monkeypatch, feed, bad):
    cohort(tmp_path)
    w = start(tmp_path, monkeypatch)
    sight(w.base, feed, network="base" if bad == "other_chain" else "solana",
          at=NOW - 300 if bad == "stale" else NOW + 10 if bad == "future" else NOW - 10,
          offset=None if bad == "no_clock" else 0)
    w.intake_book.reload(NOW)
    assert w.intake_book.listed == {} and printed(w.intake_book, NOW) == []


@pytest.mark.parametrize("feed", FEEDS)
def test_a_print_received_before_the_sighting_waits_for_a_later_print(tmp_path, feed):
    sight(tmp_path, feed, at=NOW - 1)
    b = B.IntakeBook(intake_dir=tmp_path / "intake", db_path=tmp_path / "emitter.db", feeds=FEEDS)
    b.reload(NOW)
    mapping, metadata = facts()
    assert b.on_print(event=event(), mapping=mapping, metadata=metadata, slot=10, signature=SIG, ordinal=0,
                      tx_index=None, now=NOW, received_at=NOW - 10, sol_usd=100, sol_usd_at=NOW - 11) == []
    assert len(printed(b, NOW + 1)) == 1


def test_cooldown_is_one_hour_per_feed_and_mint(tmp_path, monkeypatch):
    cohort(tmp_path)
    w = start(tmp_path, monkeypatch)
    for feed in FEEDS:
        sight(w.base, feed)
    w.intake_book.reload(NOW)
    first = printed(w.intake_book, NOW)
    assert {json.loads(body)["intake"]["feed"] for _, body in first} == set(FEEDS)
    for oid, _ in first:
        w.intake_book.committed(oid)
    for feed in FEEDS:
        sight(w.base, feed, at=NOW + 1)
    w.intake_book.reload(NOW + 2)
    assert printed(w.intake_book, NOW + 3) == []
    for feed in FEEDS:
        sight(w.base, feed, at=NOW + 3600)
    w.intake_book.reload(NOW + 3601)
    assert len(printed(w.intake_book, NOW + 3602)) == 3


def test_pending_replay_keeps_bytes_and_excludes_disallowed_feeds(tmp_path):
    base = tmp_path / "desk"
    for feed in FEEDS:
        sight(base, feed)
    b = B.IntakeBook(intake_dir=base / "intake", db_path=tmp_path / "emitter.db", feeds=FEEDS)
    b.reload(NOW)
    pending = printed(b, NOW)
    assert len(pending) == 3
    b.db.close()
    legacy = B.IntakeBook(intake_dir=base / "intake", db_path=tmp_path / "emitter.db")
    assert legacy.take_pending() == [(oid, body) for oid, body in pending if json.loads(body)["intake"]["feed"] == "pumpfun"]
    legacy.db.close()
    restored = B.IntakeBook(intake_dir=base / "intake", db_path=tmp_path / "emitter.db", feeds=FEEDS)
    assert restored.take_pending() == pending


def test_a_row_claiming_another_feed_cannot_bypass_the_frozen_allowlist(tmp_path):
    out = I.open_day(tmp_path / "intake", "pumpfun", NOW - 10)
    I.record_poll(out, "fomo", "active", NOW - 10,
                  [{"network_id": "solana", "chain_raw": SOL, "mint": MINT, "venue": "pumpswap", "raw": {}}], offset_s=0)
    out.close()
    b = B.IntakeBook(intake_dir=tmp_path / "intake", db_path=tmp_path / "emitter.db")
    b.reload(NOW)
    assert b.listed == {} and printed(b, NOW) == []
    assert b.stats["reload_errors"] == 1


@pytest.mark.parametrize("feeds", [[], ["pumpfun", "unknown"], ["pumpfun", "pumpfun"], "pumpfun", None])
def test_bad_direct_feed_selection_refuses_before_opening_the_emitter(tmp_path, feeds):
    with pytest.raises(ValueError, match="intake_feeds"):
        B.IntakeBook(intake_dir=tmp_path / "intake", db_path=tmp_path / "emitter.db", feeds=feeds)
    assert not (tmp_path / "emitter.db").exists()
