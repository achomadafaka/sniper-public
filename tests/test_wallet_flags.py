"""Wallet flags — favorite (pin + re-analyze) and soft-delete (blacklist). Wallet-keyed,
lowercased, idempotent; soft-delete is reversible."""
from __future__ import annotations

import sqlite3

from sniper.persistence import db_wallet_flags as DB

A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    DB.ensure_wallet_flags_tables(c)
    return c


def test_set_and_clear_favorite():
    c = _conn()
    DB.set_favorite(c, A, now=100)
    assert DB.is_favorite(c, A) is True
    assert A in DB.favorites(c)
    DB.clear_favorite(c, A, now=200)
    assert DB.is_favorite(c, A) is False
    assert A not in DB.favorites(c)


def test_favorite_is_case_insensitive_and_idempotent():
    c = _conn()
    DB.set_favorite(c, A.upper(), now=1)
    DB.set_favorite(c, A, now=2)
    assert DB.is_favorite(c, A) is True
    assert len(DB.favorites(c)) == 1


def test_soft_delete_and_restore():
    c = _conn()
    DB.soft_delete(c, A, now=100)
    assert DB.is_deleted(c, A) is True
    DB.restore(c, A, now=200)
    assert DB.is_deleted(c, A) is False


def test_flags_for_batch():
    c = _conn()
    DB.set_favorite(c, A, now=1)
    DB.soft_delete(c, B, now=2)
    f = DB.flags_for(c, [A, B, C])
    assert f[A]["favorite"] is True and f[A]["deleted"] is False
    assert f[B]["deleted"] is True
    assert C not in f  # no row -> absent


def test_favorite_and_delete_independent():
    c = _conn()
    DB.set_favorite(c, A, now=1)
    DB.soft_delete(c, A, now=2)
    assert DB.is_favorite(c, A) is True and DB.is_deleted(c, A) is True
