"""Tier-2 correctness fixes: empty-event-id collision (D), cross-process double-exit
claim (E), copy-failure reprocess (F), follow-sync truncated-page guard (G)."""
import sqlite3

from sniper.feeds.fomo_feed import normalize_feed_item
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_runner as run
from sniper.services.memecoin_follow_sync import sync_followed


# ---- D: id-less feed events must not collide on "" ----
def _raw(uid, mint, typ="large_buy", eid=None):
    r = {"type": typ, "userId": uid, "tokenAddress": mint,
         "createdAt": "2026-07-06T10:00:00Z",
         "body": {"userHandle": "h", "ticker": "T", "price": 0.01, "currentSizeUsd": 3000}}
    if eid is not None:
        r["id"] = eid
    return r


def test_idless_events_get_distinct_stable_ids():
    a = normalize_feed_item(_raw("u1", "MINTA"), "now", None)
    b = normalize_feed_item(_raw("u2", "MINTB"), "now", None)
    a2 = normalize_feed_item(_raw("u1", "MINTA"), "later", None)   # same trade re-delivered
    assert a.fomo_event_id and b.fomo_event_id
    assert a.fomo_event_id != b.fomo_event_id     # distinct trades -> distinct ids (no "" collide)
    assert a.fomo_event_id == a2.fomo_event_id    # same trade -> same id (dedup still holds)


# ---- E: claim_close is atomic (only one winner) ----
def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def test_claim_close_single_winner():
    c = _conn()
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint="M", ticker="T",
                          entry_price=1.0, entry_notional=100.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="t0")
    assert m.claim_close(c, pid) is True          # first caller wins
    assert m.claim_close(c, pid) is False         # second caller loses (already is_open=0)


# ---- F: a failed copy deletes the dedup row so it reprocesses ----
def test_run_once_reprocesses_event_on_copy_failure():
    c = _conn(); m.add_wallet(c, "u1", "alice")
    feed = {"responseObject": {"feed": [_raw("u1", "MINT", eid="E1")]}}

    def http(method, url, headers=None, body=None):
        return 200, feed, "Sun, 06 Jul 2026 10:00:01 GMT"

    class _Auth:
        access = "t"; refresh = "r"; ca_id = "c"
        def refresh_token(self, http=None): return "t"

    def boom(*a, **k):
        raise RuntimeError("transient copy failure")

    # copy raises -> event row must be deleted (not left as a committed silent drop).
    # run_once fans out via copy_event_to_arms, so patch THAT (imported inside the loop).
    import sniper.services.memecoin_runner as R
    import sniper.services.memecoin_copy_trader as CT
    orig = CT.copy_event_to_arms
    CT.copy_event_to_arms = boom
    try:
        R.run_once(c, _Auth(), 100.0, http=http, fill_fn=lambda *a, **k: (0.01, "j"))
    finally:
        CT.copy_event_to_arms = orig
    assert c.execute("SELECT COUNT(*) FROM memecoin_events WHERE fomo_event_id='E1'").fetchone()[0] == 0


# ---- G: follow-sync only pauses on a COMPLETE fetch ----
def _sync_conn():
    c = _conn()
    m.add_wallet(c, "f1", "keep", source="follow")
    m.add_wallet(c, "f2", "drop", source="follow")
    return c


def test_follow_sync_skips_pause_when_incomplete():
    c = _sync_conn()
    # incomplete fetch returns only f1 -> f2 must NOT be paused (could be an unfetched page)
    r = sync_followed(c, None, user_fn=lambda a: "me",
                      following_fn=lambda a, you: ([{"id": "f1", "userHandle": "keep"}], False))
    assert r["complete"] is False and r["paused"] == 0
    assert m.list_wallets(c)  # f2 still active
    f2 = [w for w in m.list_wallets(c) if w["source_user_id"] == "f2"][0]
    assert f2["paused"] == 0


def test_follow_sync_pauses_when_complete():
    c = _sync_conn()
    r = sync_followed(c, None, user_fn=lambda a: "me",
                      following_fn=lambda a, you: ([{"id": "f1", "userHandle": "keep"}], True))
    assert r["complete"] is True and r["paused"] == 1
    f2 = [w for w in m.list_wallets(c) if w["source_user_id"] == "f2"][0]
    assert f2["paused"] == 1


def test_follow_sync_bare_list_treated_complete_backward_compat():
    c = _sync_conn()
    r = sync_followed(c, None, user_fn=lambda a: "me",
                      following_fn=lambda a, you: [{"id": "f1", "userHandle": "keep"}])
    assert r["paused"] == 1    # bare list -> complete (existing injected-fn contract)
