"""The J7 tracked lists ARE the addressable roster, so they must be stored, not counted.

J7 caps each account at 50 wallets and relays trades for those only. fomo is banned and the global
stream ended on 2026-09-11 (1,411 signalling wallets on 09-09, 49 on 09-11), so a roster row absent
from every tracked list can never produce a signal again. The runner already received the lists on
every connect and dropped them after reading `len()`.

Frame shape is taken from the recorded tap, data/j7_tap.jsonl:

    {"ev": "tracked_fomo", "payload": {"fomo_users": [{"fomo_user_id": "...", "handle": "...",
      "display_name": "...", "avatar": "...", "profile_url": "...", "added_at": "..."}],
      "limit": 50}}
    {"ev": "tracked_pump", "payload": {"pump_users": [], "limit": 50}}
"""
import sqlite3

import pytest

from sniper.feeds.j7_feed import _tracked_rows
from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


FOMO_FRAME = {"fomo_users": [
    {"fomo_user_id": "fb0c112f-45ba-54f7-b5ac-910c4e1ec6bb", "handle": "DegenCapitalLLC",
     "display_name": "DegenCapitalLLC",
     "avatar": "https://prod-fomo-profile-pics.s3.amazonaws.com/895f.jpg",
     "profile_url": "https://fomo.family/profile/DegenCapitalLLC",
     "added_at": "2026-09-01T10:00:00.000Z"},
    {"fomo_user_id": "u-2", "handle": "second", "display_name": "Second", "added_at": None},
], "limit": 50}


def test_the_recorded_frame_reduces_to_identity_rows():
    rows = _tracked_rows(FOMO_FRAME["fomo_users"], "fomo_user_id")
    assert [r["source_user_id"] for r in rows] == [
        "fb0c112f-45ba-54f7-b5ac-910c4e1ec6bb", "u-2"]
    assert rows[0]["handle"] == "DegenCapitalLLC"
    assert rows[0]["added_at"] == "2026-09-01T10:00:00.000Z"


def test_an_empty_pump_list_is_a_real_answer_and_a_missing_one_is_not():
    """The live tap carries `pump_users: []`. Reading that as "no data" would make a working
    account look unreachable; reading a MISSING frame as "empty" would clear a live list."""
    assert _tracked_rows([], "pump_user_id", "fomo_user_id") == []
    assert _tracked_rows(None, "pump_user_id", "fomo_user_id") is None
    assert _tracked_rows("not-a-list", "fomo_user_id") is None


def test_an_entry_with_no_id_is_dropped_but_some_entry_must_parse():
    """A blank id matches no roster row, so it is dropped — but a frame where NOTHING parses is
    UNKNOWN, not empty.

    CORRECTED 2026-09-22: this test previously asserted `[]` here, which is precisely the defect
    that reached production. Every entry failing to parse is a shape we do not understand, and
    calling that "J7 tracks nobody" marked 7 live pump wallets for retirement.
    """
    assert _tracked_rows([{"handle": "nameless"}, {"fomo_user_id": "", "handle": "empty"}],
                         "fomo_user_id") is None
    mixed = _tracked_rows([{"handle": "nameless"}, {"fomo_user_id": "u-2"}], "fomo_user_id")
    assert [r["source_user_id"] for r in mixed] == ["u-2"], "a readable entry still parses"


def test_the_stored_list_replaces_rather_than_merges():
    """REPLACE IS THE CONTRACT. The frame is the account's COMPLETE list, so a wallet J7 dropped
    must leave the table — a merge would keep retiring candidates alive forever."""
    c = _conn()
    m.replace_j7_tracked(c, 0, "fomo", _tracked_rows(FOMO_FRAME["fomo_users"], "fomo_user_id"),
                         seen_at="2026-09-22T17:00:00+00:00")
    assert m.j7_tracked_ids(c) == {"fb0c112f-45ba-54f7-b5ac-910c4e1ec6bb", "u-2"}
    m.replace_j7_tracked(c, 0, "fomo", [{"source_user_id": "u-2", "handle": "second"}],
                         seen_at="2026-09-22T17:05:00+00:00")
    assert m.j7_tracked_ids(c) == {"u-2"}


def test_a_missing_frame_leaves_the_stored_list_alone():
    """None is not an empty list. A socket that never sent a frame must not clear the roster."""
    c = _conn()
    m.replace_j7_tracked(c, 0, "fomo", [{"source_user_id": "u-1"}], seen_at="t1")
    assert m.replace_j7_tracked(c, 0, "fomo", None, seen_at="t2") is None
    assert m.j7_tracked_ids(c) == {"u-1"}


def test_an_empty_frame_does_clear_that_account():
    c = _conn()
    m.replace_j7_tracked(c, 0, "fomo", [{"source_user_id": "u-1"}], seen_at="t1")
    m.replace_j7_tracked(c, 0, "fomo", [], seen_at="t2")
    assert m.j7_tracked_ids(c) == set()


def test_account_replacement_cannot_inherit_complete_old_lists():
    from sniper.feeds.j7_feed import account_fingerprint
    c = _conn()
    old = account_fingerprint("old-secret")
    new = account_fingerprint("new-secret")
    for kind in ("fomo", "pump"):
        m.replace_j7_tracked(c, 0, kind, [{"source_user_id": "old-wallet"}],
                             seen_at="2026-09-24T12:00:00+00:00", account_fingerprint=old)
    assert m.j7_tracked_identity_matches(c, [old])
    assert not m.j7_tracked_identity_matches(c, [new])
    m.replace_j7_tracked(c, 0, "fomo", [], seen_at="2026-09-24T12:01:00+00:00",
                         account_fingerprint=new)
    assert not m.j7_tracked_identity_matches(c, [new])
    m.replace_j7_tracked(c, 0, "pump", [], seen_at="2026-09-24T12:01:00+00:00",
                         account_fingerprint=new)
    assert m.j7_tracked_identity_matches(c, [new])
    assert not m.j7_tracked_identity_matches(c, [old])
    assert not m.j7_tracked_identity_matches(c, [new, "missing-account"])
    assert not m.j7_tracked_identity_matches(c, [])


def test_legacy_tracked_rows_have_no_account_identity():
    c = _conn()
    for kind in ("fomo", "pump"):
        m.replace_j7_tracked(c, 0, kind, [], seen_at="2026-09-24T12:00:00+00:00")
    assert not m.j7_tracked_identity_matches(c, ["new-account"])


def test_accounts_and_kinds_do_not_overwrite_each_other():
    """Two accounts hold 50 each and pump is a separate roster; one key would hide 50 wallets."""
    c = _conn()
    m.replace_j7_tracked(c, 0, "fomo", [{"source_user_id": "a-0"}], seen_at="t")
    m.replace_j7_tracked(c, 1, "fomo", [{"source_user_id": "a-1"}], seen_at="t")
    m.replace_j7_tracked(c, 0, "pump", [{"source_user_id": "p-0"}], seen_at="t")
    assert m.j7_tracked_ids(c) == {"a-0", "a-1", "p-0"}
    m.replace_j7_tracked(c, 0, "fomo", [], seen_at="t2")
    assert m.j7_tracked_ids(c) == {"a-1", "p-0"}, "clearing one account touched another"


def test_seen_at_reports_the_OLDEST_list():
    """Codex review 2026-09-23 A3. "Absent from every list" needs EVERY list fresh, so the
    stamp the prune reads is the oldest one, and one dead account blocks the prune until it
    reconnects. The earlier newest-wins rule let one fresh account hide a stale one."""
    c = _conn()
    assert m.j7_tracked_seen_at(c) is None
    m.replace_j7_tracked(c, 0, "fomo", [{"source_user_id": "u-1"}],
                         seen_at="2026-09-22T17:00:00+00:00")
    m.replace_j7_tracked(c, 1, "fomo", [{"source_user_id": "u-2"}],
                         seen_at="2026-09-22T17:30:00+00:00")
    assert m.j7_tracked_seen_at(c) == "2026-09-22T17:00:00+00:00"
    # an EMPTY list is a real answer with its own stamp; it counts too
    m.replace_j7_tracked(c, 1, "pump", [], seen_at="2026-09-22T16:00:00+00:00")
    assert m.j7_tracked_seen_at(c) == "2026-09-22T16:00:00+00:00"


def test_a_list_is_stamped_when_its_frame_arrived_not_when_it_was_persisted():
    """Codex review 2026-09-23 A3: the heartbeat stamped every cached list with `now`, so a
    reconnecting account's stale list read as fresh. The frame's own arrival time travels with
    it, and an account that never delivered a frame in this process yields nothing."""
    from sniper.feeds.j7_feed import tracked_frames
    status = {"accounts": [
        {"tracked_users": [{"source_user_id": "a"}], "tracked_count": 1,
         "tracked_seen_at": "2026-09-23T15:00:00+00:00",
         "pump_tracked_users": None, "pump_tracked_count": 9, "pump_tracked_seen_at": None},
        {"tracked_users": [{"source_user_id": "cached"}], "tracked_count": 1,
         "tracked_seen_at": None, "pump_tracked_users": [], "pump_tracked_count": 0,
         "pump_tracked_seen_at": "2026-09-23T15:01:00+00:00"},
    ]}
    frames = list(tracked_frames(status))
    assert frames == [
        (0, "fomo", [{"source_user_id": "a"}], "2026-09-23T15:00:00+00:00", 1),
        (1, "pump", [], "2026-09-23T15:01:00+00:00", 0),
    ]


def test_the_same_wallet_on_two_accounts_is_one_id():
    c = _conn()
    m.replace_j7_tracked(c, 0, "fomo", [{"source_user_id": "dup"}], seen_at="t")
    m.replace_j7_tracked(c, 1, "fomo", [{"source_user_id": "dup"}], seen_at="t")
    assert m.j7_tracked_ids(c) == {"dup"}


# --- the 2026-09-22 pump regression ---------------------------------------------------------------
#
# L1 shipped with ONE guessed pump id key. A live 9-entry tracked_pump frame matched none of them,
# _tracked_rows returned [], and "we could not read this list" was stored as "J7 tracks nobody on
# pump". The prune then listed 7 ACTIVE pump wallets as untracked — Thepennyflippe with 202 events
# in 7 days and two funded closes among them. Retiring those would have cut live signal sources.


def test_entries_that_parse_to_nothing_are_UNKNOWN_not_empty():
    """THE DEFECT, pinned. A list that arrives and cannot be read is not an empty roster."""
    assert _tracked_rows([{"mystery_key": "abc"}], "pump_user_id") is None
    assert _tracked_rows([{"pump_user_id": ""}], "pump_user_id") is None
    # and a genuinely empty frame is still empty, which is a real answer
    assert _tracked_rows([], "pump_user_id") == []


@pytest.mark.parametrize("key", ["pump_user_id", "user_id", "wallet", "address", "id"])
def test_a_pump_entry_is_read_from_any_plausible_id_key(key):
    """We have never captured a non-empty tracked_pump frame, so one guessed key was the bug."""
    from sniper.feeds.j7_feed import _PUMP_ID_KEYS, _pump_roster_id
    rows = _tracked_rows([{key: "So1Wa11et"}], *_PUMP_ID_KEYS, normalise=_pump_roster_id)
    assert rows == [{"source_user_id": "pump:So1Wa11et", "handle": None,
                     "display_name": None, "added_at": None}]


def test_the_pump_id_matches_the_roster_form():
    """pump_event_to_feed_item writes userId = f"pump:{wallet}", so a bare address matches no
    roster row. The roster is what the prune compares against."""
    from sniper.feeds.j7_feed import _pump_roster_id
    assert _pump_roster_id("So1Wa11et") == "pump:So1Wa11et"
    assert _pump_roster_id("pump:So1Wa11et") == "pump:So1Wa11et", "must not double-prefix"


def test_an_unreadable_kind_refuses_the_prune_instead_of_retiring_its_wallets():
    """THE FAIL-CLOSED HALF. Even as UNKNOWN, absent pump rows make every pump wallet look
    untracked. The reported count is what makes the gap visible."""
    c = _conn()
    m.add_wallet(c, "u1", "alice")
    m.add_wallet(c, "pump:So1Wa11et", "pennyflipper")
    m.replace_j7_tracked(c, 0, "fomo", [{"source_user_id": "u1"}], seen_at="t", reported_count=1)
    # the feed said 9 pump wallets and we could parse none of them
    m.replace_j7_tracked(c, 0, "pump", None, seen_at="t", reported_count=9)

    gaps = m.j7_tracked_incomplete(c)
    assert [(g["kind"], g["reported_count"], g["stored_count"]) for g in gaps] == [("pump", 9, None)]
    assert m.wallets_not_tracked(c) == [], "an unreadable kind must retire nobody"


def test_a_complete_pair_of_lists_prunes_normally():
    """The guard must not wedge the prune shut once both kinds read."""
    c = _conn()
    m.add_wallet(c, "u1", "alice")
    m.add_wallet(c, "pump:So1Wa11et", "pennyflipper")
    m.add_wallet(c, "u-gone", "retiree")
    m.replace_j7_tracked(c, 0, "fomo", [{"source_user_id": "u1"}], seen_at="t", reported_count=1)
    m.replace_j7_tracked(c, 0, "pump", [{"source_user_id": "pump:So1Wa11et"}],
                         seen_at="t", reported_count=1)
    assert m.j7_tracked_incomplete(c) == []
    assert [r["source_user_id"] for r in m.wallets_not_tracked(c)] == ["u-gone"]


def test_a_wallet_on_the_pump_list_only_counts_as_tracked():
    """EITHER list makes a wallet tracked. This is the union the prune depends on."""
    c = _conn()
    m.add_wallet(c, "pump:So1Wa11et", "pennyflipper")
    m.replace_j7_tracked(c, 0, "fomo", [], seen_at="t", reported_count=0)
    m.replace_j7_tracked(c, 0, "pump", [{"source_user_id": "pump:So1Wa11et"}],
                         seen_at="t", reported_count=1)
    assert m.wallets_not_tracked(c) == []


# --- a list that never arrived at all (Codex recheck 2026-09-23, A3) ----------------------------
#
# `tracked_frames` yields only lists that carry a frame, so an account that has never delivered one
# produced NO row anywhere and completeness had nothing to compare. Account 0 fresh and complete,
# account 1 silent: the prune read "every list agrees" and nominated account 1's wallets. The
# expected inventory is knowable -- `status["accounts"]` carries one entry per configured token
# whether or not that socket ever spoke -- so the silence is recorded as its own state.

def test_the_expected_inventory_names_a_list_that_never_sent_a_frame():
    from sniper.feeds.j7_feed import expected_lists, tracked_frames
    status = {"accounts": [
        {"tracked_users": [], "tracked_count": 0, "tracked_seen_at": "t0",
         "pump_tracked_users": [], "pump_tracked_count": 0, "pump_tracked_seen_at": "t0"},
        {},                                   # configured, connected or not, never delivered
    ]}
    assert sorted((i, k, seen) for i, k, seen in expected_lists(status)) == [
        (0, "fomo", True), (0, "pump", True), (1, "fomo", False), (1, "pump", False)]
    # the frame reader is unchanged: it still yields only what actually arrived
    assert sorted((i, k) for i, k, _r, _s, _c in tracked_frames(status)) == [(0, "fomo"), (0, "pump")]


def test_a_list_that_never_reported_refuses_the_prune():
    c = _conn()
    m.add_wallet(c, "u1", "alice")
    m.add_wallet(c, "u2", "bob")              # belongs to the account that never spoke
    m.replace_j7_tracked(c, 0, "fomo", [{"source_user_id": "u1"}], seen_at="t", reported_count=1)
    m.replace_j7_tracked(c, 0, "pump", [], seen_at="t", reported_count=0)
    assert m.j7_tracked_incomplete(c) == []            # account 0 alone looks complete
    m.mark_j7_list_unseen(c, 1, "fomo", at="t")
    m.mark_j7_list_unseen(c, 1, "pump", at="t")
    gaps = m.j7_tracked_incomplete(c)
    assert [(g["account_index"], g["kind"]) for g in gaps] == [(1, "fomo"), (1, "pump")]
    assert all(g["stored_count"] is None and g["reported_count"] is None for g in gaps)


def test_an_unseen_marker_is_never_read_as_a_fresh_observation():
    """The two readers must point in OPPOSITE directions: `j7_tracked_incomplete` includes the
    marker so the prune refuses, `j7_tracked_seen_at` excludes it so its stamp cannot pass as
    evidence. Reading it as freshness would hide the gap; reading it as the oldest stamp would
    wedge the prune shut forever."""
    c = _conn()
    m.replace_j7_tracked(c, 0, "fomo", [{"source_user_id": "u1"}],
                         seen_at="2026-09-23T20:00:00+00:00", reported_count=1)
    m.mark_j7_list_unseen(c, 1, "fomo", at="2026-01-01T00:00:00+00:00")
    assert m.j7_tracked_seen_at(c) == "2026-09-23T20:00:00+00:00"
    assert [g["kind"] for g in m.j7_tracked_incomplete(c)] == ["fomo"]


def test_a_frame_clears_the_unseen_marker_without_operator_action():
    c = _conn()
    m.mark_j7_list_unseen(c, 1, "fomo", at="t0")
    assert m.j7_tracked_incomplete(c)
    m.replace_j7_tracked(c, 1, "fomo", [{"source_user_id": "u9"}], seen_at="t1", reported_count=1)
    assert m.j7_tracked_incomplete(c) == []
    assert m.j7_tracked_seen_at(c) == "t1"


def test_the_account_fingerprint_is_a_short_identity_not_a_credential_digest():
    """Alpha's ruling, 2026-09-24: 16 hex chars (64 bits) identifies the account; the full sha256
    would be a stable fingerprint of the J7 token itself, readable by anyone reading the ledger."""
    import string
    from sniper.feeds.j7_feed import account_fingerprint
    fp = account_fingerprint("a-long-j7-bearer-token")
    assert len(fp) == 16 and set(fp) <= set(string.hexdigits.lower())
    assert account_fingerprint("a") != account_fingerprint("b")
    assert account_fingerprint("same") == account_fingerprint("same")
