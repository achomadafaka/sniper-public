"""Slice 6 Task 2 — SupabaseHttp injection surface.

A single explicit-constructor REST adapter (`RestSupabaseHttp`) plus a
matching in-memory stub (`StubSupabaseHttp`). Output writers depend on
the `SupabaseHttp` protocol, never on `bot.supabase_helper`,
`urllib.request`, `requests`, or `httpx` — that decoupling is the
analogue of Slice 5's "no live imports" invariant for the writer layer.

`RestSupabaseHttp` does NOT read any `os.environ` global at construction
time. The `url` and `key` arguments are always passed explicitly.
Calibration env vars (`CALIBRATION_LIVE_SUPABASE_URL` /
`CALIBRATION_LIVE_SUPABASE_KEY`) are read only by the live test
fixtures (Task 9), which construct `RestSupabaseHttp(url=..., key=...)`
from those values and inject the instance into the writers under test.
This avoids the global-coupling failure mode that v1 of the Slice 6
plan tripped over (Codex round-1 #3).

Three operations on the protocol surface:

* ``insert_ignore(table, rows, *, natural_key_columns)`` — POST with
  ``Prefer: resolution=ignore-duplicates,return=representation``. No
  ``merge-duplicates`` header anywhere in the slice (D1).
* ``patch(table, *, where, set_columns)`` — PATCH with PostgREST
  ``?col=eq.value`` filters. Used only by ``mark_superseded(...)``
  and the artifact ``paths`` enrichment.
* ``select(table, *, where, columns=None)`` — GET with ``?col=eq.value``
  filters and optional projection.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol


class HTTPTransport(Protocol):
    """Tiny transport hook for `RestSupabaseHttp`.

    Lets tests inject an in-process fake without touching the network.
    The real production transport is the lambda installed by the
    constructor when no override is supplied.
    """

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> tuple[int, bytes]:
        ...


class SupabaseHttp(Protocol):
    """The minimal surface every Slice 6 writer depends on."""

    def insert_ignore(
        self,
        table: str,
        rows: list[dict],
        *,
        natural_key_columns: tuple[str, ...],
    ) -> int:
        ...

    def patch(
        self,
        table: str,
        *,
        where: dict,
        set_columns: dict,
    ) -> int:
        ...

    def select(
        self,
        table: str,
        *,
        where: dict,
        columns: tuple[str, ...] | None = None,
    ) -> list[dict]:
        ...


# ---------------------------------------------------------------------------
# Stub implementation — used by every offline test
# ---------------------------------------------------------------------------


@dataclass
class StubSupabaseHttp:
    """In-memory `SupabaseHttp` for offline tests.

    Records every request to ``calls`` (a flat append-only log; second
    pass over the same natural key DOES grow the list) and maintains a
    per-table row store at ``rows_by_table`` so reads work.

    The idempotency contract Slice 6 enforces is "writer return value
    is `0` AND `rows_by_table[table]` length is unchanged" — `calls`
    growing on the second pass is expected.

    Partial-unique-index enforcement
    --------------------------------
    Tests can register partial unique indexes via
    ``partial_unique_indexes`` to mirror production DB constraints.
    Each entry is ``(table, columns, where_filter)``:

        StubSupabaseHttp(partial_unique_indexes=[
            ("wallet_flipper_tag_grain",
             ("wallet", "grain", "tag"),
             {"status": "active"}),
        ])

    On ``insert_ignore``, a row is rejected (NOT inserted, NOT counted
    in the return value) when both the new row and at least one
    existing row in the store match ``where_filter`` AND have equal
    values across ``columns``. This mirrors PostgreSQL's
    ``INSERT ... ON CONFLICT DO NOTHING`` semantics under a partial
    unique index. Without enforcement, tests can silently accept bug
    orderings that production would silently swallow.
    """

    rows_by_table: dict[str, list[dict]] = field(default_factory=dict)
    calls: list[tuple[str, str, dict]] = field(default_factory=list)
    # Mirrors the migration's partial unique indexes. Tests pass these
    # explicitly so the constraint is opt-in (most stub callers don't
    # need it).
    partial_unique_indexes: list[
        tuple[str, tuple[str, ...], dict[str, object]]
    ] = field(default_factory=list)

    def _store(self, table: str) -> list[dict]:
        return self.rows_by_table.setdefault(table, [])

    def _partial_unique_conflict(
        self, table: str, candidate: dict, existing_rows: list[dict],
    ) -> bool:
        """Return True if `candidate` would violate any registered
        partial unique index against `existing_rows`. Both the
        candidate and the existing row must satisfy the index's
        `where_filter` for the partial index to apply."""
        for (idx_table, columns, where_filter) in self.partial_unique_indexes:
            if idx_table != table:
                continue
            # Candidate must match the partial-index filter, else this
            # index doesn't gate it.
            if not all(
                candidate.get(k) == v for k, v in where_filter.items()
            ):
                continue
            key = tuple(candidate.get(c) for c in columns)
            for existing in existing_rows:
                if not all(
                    existing.get(k) == v for k, v in where_filter.items()
                ):
                    continue
                if tuple(existing.get(c) for c in columns) == key:
                    return True
        return False

    def insert_ignore(
        self,
        table: str,
        rows: list[dict],
        *,
        natural_key_columns: tuple[str, ...],
    ) -> int:
        store = self._store(table)
        inserted = 0
        for row in rows:
            self.calls.append(("insert_ignore", table, dict(row)))
            key = tuple(row.get(col) for col in natural_key_columns)
            already_by_natural_key = any(
                tuple(existing.get(col) for col in natural_key_columns) == key
                for existing in store
            )
            if already_by_natural_key:
                continue
            # Mirror DB-level partial unique enforcement: even if the
            # natural key is fresh, an existing active row matching a
            # registered partial unique index blocks the insert.
            if self._partial_unique_conflict(table, row, store):
                continue
            store.append(dict(row))
            inserted += 1
        return inserted

    def patch(
        self,
        table: str,
        *,
        where: dict,
        set_columns: dict,
    ) -> int:
        self.calls.append(
            (
                "patch",
                table,
                {"where": dict(where), "set": dict(set_columns)},
            )
        )
        store = self._store(table)
        touched = 0
        for row in store:
            if all(row.get(k) == v for k, v in where.items()):
                row.update(set_columns)
                touched += 1
        return touched

    def select(
        self,
        table: str,
        *,
        where: dict,
        columns: tuple[str, ...] | None = None,
    ) -> list[dict]:
        self.calls.append(
            (
                "select",
                table,
                {
                    "where": dict(where),
                    "columns": list(columns) if columns is not None else None,
                },
            )
        )
        store = self._store(table)
        matched = [
            dict(row)
            for row in store
            if all(row.get(k) == v for k, v in where.items())
        ]
        if columns is not None:
            matched = [
                {col: row.get(col) for col in columns} for row in matched
            ]
        return matched


# ---------------------------------------------------------------------------
# REST implementation — explicit url/key, optional injectable transport
# ---------------------------------------------------------------------------


def _default_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, bytes]:
    """Real-network transport. Wraps urllib.request directly.

    Imported and used only here. Keeping it as a private module-level
    function (not a class method) makes the urllib import explicit and
    easy to grep for from offline-import audits.
    """
    request = urllib.request.Request(  # noqa: S310 — explicit Supabase REST
        url=url,
        method=method,
        headers=dict(headers),
        data=body,
    )
    try:
        with urllib.request.urlopen(request, timeout=30.0) as response:  # noqa: S310
            status = response.getcode()
            payload = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = exc.read() if exc.fp is not None else b""
    return status, payload


class RestSupabaseHttp:
    """Explicit-constructor PostgREST adapter.

    `url` and `key` are passed in directly. No `os.environ` reads at
    construction or at request time. The `transport` hook lets tests
    inject an in-process fake.
    """

    def __init__(
        self,
        *,
        url: str,
        key: str,
        transport: HTTPTransport | None = None,
    ) -> None:
        if not url:
            raise ValueError("RestSupabaseHttp requires a non-empty `url`")
        if not key:
            raise ValueError("RestSupabaseHttp requires a non-empty `key`")
        self._base_url = url.rstrip("/")
        self._key = key
        self._transport = transport or _default_transport

    def _headers(self, *, prefer: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def _table_url(self, table: str) -> str:
        return f"{self._base_url}/rest/v1/{table}"

    @staticmethod
    def _filter_query(where: Mapping[str, Any]) -> str:
        # PostgREST `?col=eq.value` filters. Values are URL-encoded.
        if not where:
            return ""
        parts = [
            f"{urllib.parse.quote(str(k), safe='')}=eq.{urllib.parse.quote(str(v), safe='')}"
            for k, v in where.items()
        ]
        return "?" + "&".join(parts)

    def insert_ignore(
        self,
        table: str,
        rows: list[dict],
        *,
        natural_key_columns: tuple[str, ...],
    ) -> int:
        """POST rows with ignore-duplicates.

        `natural_key_columns` is informational on this adapter (the
        conflict resolution is server-side); it is part of the protocol
        signature so the stub can use it for in-memory dedup. Slice 6
        writers always pass it.
        """
        if not rows:
            return 0
        body = json.dumps(rows, separators=(",", ":")).encode("utf-8")
        headers = self._headers(
            prefer="resolution=ignore-duplicates,return=representation"
        )
        status, payload = self._transport(
            "POST", self._table_url(table), headers, body
        )
        if status >= 400:
            raise RuntimeError(
                f"insert_ignore {table}: HTTP {status} {payload!r}"
            )
        if not payload:
            return 0
        try:
            inserted = json.loads(payload)
        except (TypeError, ValueError):
            return 0
        return len(inserted) if isinstance(inserted, list) else 0

    def patch(
        self,
        table: str,
        *,
        where: dict,
        set_columns: dict,
    ) -> int:
        if not where:
            raise ValueError("patch requires a non-empty `where` filter")
        body = json.dumps(set_columns, separators=(",", ":")).encode("utf-8")
        headers = self._headers(prefer="return=representation")
        url = f"{self._table_url(table)}{self._filter_query(where)}"
        status, payload = self._transport("PATCH", url, headers, body)
        if status >= 400:
            raise RuntimeError(f"patch {table}: HTTP {status} {payload!r}")
        try:
            updated = json.loads(payload) if payload else []
        except (TypeError, ValueError):
            updated = []
        return len(updated) if isinstance(updated, list) else 0

    def select(
        self,
        table: str,
        *,
        where: dict,
        columns: tuple[str, ...] | None = None,
    ) -> list[dict]:
        query = self._filter_query(where)
        if columns is not None:
            sep = "&" if query else "?"
            query += f"{sep}select={','.join(columns)}"
        url = f"{self._table_url(table)}{query}"
        headers = self._headers()
        status, payload = self._transport("GET", url, headers, None)
        if status >= 400:
            raise RuntimeError(f"select {table}: HTTP {status} {payload!r}")
        try:
            rows = json.loads(payload) if payload else []
        except (TypeError, ValueError):
            rows = []
        return rows if isinstance(rows, list) else []


__all__ = [
    "HTTPTransport",
    "RestSupabaseHttp",
    "StubSupabaseHttp",
    "SupabaseHttp",
]
