"""The roster's display name must track fomo renames; its STATE must not.

`add_wallet(..., source="follow")` is `INSERT OR IGNORE` on purpose: a periodic follow sync must
never resurrect a paused wallet, un-exclude a buried one, or disturb funded authority. But the
same statement also discarded the handle, so `memecoin_wallets.handle` froze at whatever the
account was called when it was first added.

Live 2026-08-19: uid bfc5cd45… was added on 07-29 as `groguguy67`, and the feed has since
delivered its events under BOTH `groguguy67` (n=80, latest 17:31:20) and `ZonalItchyDeer` (n=35,
latest 17:20:23) — interleaved eleven minutes apart, same uid. Looking the wallet up by its
current name returned "not on roster", and the recommender (which reads the point-in-time
`memecoin_positions.source_handle`) disagreed with the Traders UI about what to call it.

`memecoin_positions.source_handle` staying frozen is CORRECT — it is an audit record of who we
copied at the time. Only the roster's display name follows the account.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m


UID = "bfc5cd45-9476-59e6-80cf-5c150b2610c1"


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _row(c, uid=UID):
    return dict(c.execute("SELECT * FROM memecoin_wallets WHERE source_user_id=?", (uid,)).fetchone())


def test_a_follow_sync_refreshes_a_renamed_handle():
    c = _conn()
    m.add_wallet(c, UID, "groguguy67", source="follow")
    m.add_wallet(c, UID, "ZonalItchyDeer", source="follow")
    assert _row(c)["handle"] == "ZonalItchyDeer"


def test_the_refresh_never_disturbs_state():
    """The whole reason this path is INSERT OR IGNORE. A rename must not re-arm, un-pause or
    un-exclude a wallet the operator put down."""
    c = _conn()
    m.add_wallet(c, UID, "groguguy67", source="follow")
    c.execute("UPDATE memecoin_wallets SET paused=1, excluded=1, real_approved=1, "
              "real_chains='solana', label='operator note', source='manual' "
              "WHERE source_user_id=?", (UID,))
    c.commit()

    m.add_wallet(c, UID, "ZonalItchyDeer", source="follow")

    r = _row(c)
    assert r["handle"] == "ZonalItchyDeer"      # only this moves
    assert r["paused"] == 1
    assert r["excluded"] == 1
    assert r["real_approved"] == 1
    assert r["real_chains"] == "solana"
    assert r["label"] == "operator note"
    assert r["source"] == "manual"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_handle_never_overwrites_a_real_name(blank):
    """The follow sync produces '' when fomo omits the display name (`.get('handle') or ''`).
    Blanking the roster row would reproduce the same not-on-roster failure it exists to fix."""
    c = _conn()
    m.add_wallet(c, UID, "groguguy67", source="follow")
    m.add_wallet(c, UID, blank, source="follow")
    assert _row(c)["handle"] == "groguguy67"


def test_an_unchanged_handle_is_a_no_op():
    c = _conn()
    m.add_wallet(c, UID, "groguguy67", source="follow")
    before = _row(c)
    m.add_wallet(c, UID, "groguguy67", source="follow")
    assert _row(c) == before


def test_a_new_wallet_still_inserts_normally():
    c = _conn()
    m.add_wallet(c, "new-uid", "freshwallet", source="follow")
    r = _row(c, "new-uid")
    assert r["handle"] == "freshwallet" and r["paused"] == 0 and r["source"] == "follow"


def test_the_manual_upsert_path_is_unchanged():
    c = _conn()
    m.add_wallet(c, UID, "groguguy67", source="follow")
    c.execute("UPDATE memecoin_wallets SET paused=1 WHERE source_user_id=?", (UID,))
    c.commit()
    m.add_wallet(c, UID, "ZonalItchyDeer", label="x")     # manual: claims + unpauses, as before
    r = _row(c)
    assert r["handle"] == "ZonalItchyDeer"
    assert r["paused"] == 0
    assert r["source"] == "manual"
