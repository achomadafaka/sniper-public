"""Phase 1.2 — `bot.supabase_helper`-backed SupabaseHttp adapter.

`bot.supabase_helper._request` is the project's canonical PostgREST
client; it picks up `SUPABASE_URL` and `SUPABASE_(SERVICE|ANON)_KEY`
from env / `.env`. This adapter wraps that helper in the
`SupabaseHttp` Protocol the Slice 6 writers expect, so
`persist_flipper_tag_grains` can run against the live Supabase project
without depending on a separate auth path.

Mostly a thin shim — exists to:
  1. avoid duplicating auth/HTTP across writers, and
  2. give run_wallet_protocol.py a one-line `from_env_or_dotenv()`
     constructor that handles the `.env` fallback when the env vars
     aren't pre-loaded.
"""
from __future__ import annotations

import os
import urllib.parse
from pathlib import Path
from typing import Any


class BotSupabaseAdapter:
    """`SupabaseHttp` Protocol implementation backed by `bot.supabase_helper`."""

    def __init__(self) -> None:
        # Lazy import — the helper module reads SUPABASE_URL/SUPABASE_KEY
        # at import time. We want callers to be able to pre-load .env
        # before construction without forcing them to import the helper.
        from bot import supabase_helper

        # Refresh the module-level URL/KEY in case .env was just loaded.
        # The helper reads these as module globals, so we need to
        # re-read os.environ AND patch the module's globals.
        supabase_helper.SUPABASE_URL = os.getenv("SUPABASE_URL", "") or supabase_helper.SUPABASE_URL
        supabase_helper.SUPABASE_KEY = (
            os.getenv("SUPABASE_SERVICE_KEY")
            or os.getenv("SUPABASE_ANON_KEY", "")
            or supabase_helper.SUPABASE_KEY
        )
        if not supabase_helper.SUPABASE_URL or not supabase_helper.SUPABASE_KEY:
            raise RuntimeError(
                "BotSupabaseAdapter: SUPABASE_URL and SUPABASE_(SERVICE|ANON)_KEY "
                "must be set in environment or .env"
            )
        self._helper = supabase_helper

    # ------------------------------------------------------------------
    # Convenience factories
    # ------------------------------------------------------------------

    @classmethod
    def from_env_or_dotenv(cls, dotenv_path: str | Path = ".env") -> "BotSupabaseAdapter":
        """Construct after loading `.env` if present and env vars are unset.

        Idempotent — does not overwrite already-set env vars.
        """
        p = Path(dotenv_path)
        if p.is_file():
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k.startswith("SUPABASE_") and not os.environ.get(k):
                    os.environ[k] = v
        return cls()

    # ------------------------------------------------------------------
    # SupabaseHttp Protocol surface
    # ------------------------------------------------------------------

    def insert_ignore(
        self,
        table: str,
        rows: list[dict],
        *,
        natural_key_columns: tuple[str, ...],
    ) -> int:
        """Insert rows, ignoring duplicates on natural_key_columns.

        Uses PostgREST's "Prefer: resolution=ignore-duplicates" with the
        upsert path, which requires a unique constraint on the natural
        key. Returns the count of newly-inserted rows.
        """
        if not rows:
            return 0
        # Compute on-conflict columns from the natural key
        on_conflict = ",".join(natural_key_columns)
        headers = {
            "Prefer": "resolution=ignore-duplicates,return=representation",
        }
        path = f"{table}?on_conflict={on_conflict}"
        result = self._helper._request("POST", path, body=rows, extra_headers=headers)
        # PostgREST returns the rows that were actually written; the
        # ignored ones are absent. So len(result) is the inserted count.
        return len(result) if isinstance(result, list) else 0

    def patch(
        self,
        table: str,
        *,
        where: dict,
        set_columns: dict,
    ) -> int:
        """PATCH rows matching `where`, setting `set_columns`. Returns count."""
        if not where:
            raise ValueError("BotSupabaseAdapter.patch: empty `where` is unsafe")
        filter_parts = [f"{k}=eq.{_quote(v)}" for k, v in where.items()]
        path = f"{table}?{'&'.join(filter_parts)}"
        headers = {"Prefer": "return=representation"}
        result = self._helper._request("PATCH", path, body=set_columns, extra_headers=headers)
        return len(result) if isinstance(result, list) else 0

    # PostgREST defaults `db-max-rows` to 1000. A single GET that exceeds
    # this cap silently truncates the response with no error — readers
    # see only the first 1000 rows. `wallet_flipper_tag_grain` is
    # already growing per-wallet (Phase 1.2 + #4 plan); once total
    # active rows cross 1000 the cached FH4 baseline will undercount
    # peers silently (Codex deep-review finding #5).
    _PAGE_SIZE: int = 1000
    # Upper bound on cumulative rows fetched per `select` call. Defensive
    # cap: a query that legitimately matches more than 100k rows is
    # almost certainly a missing `where` clause. Raise rather than
    # OOM. Override via `select(... max_rows=...)`.
    _DEFAULT_MAX_ROWS: int = 100_000

    # PostgREST operator whitelist. Keep this conservative — adding new
    # operators here lets callers express server-side filters, but each
    # one is a surface where a sloppy caller can produce a malformed
    # query that silently returns zero rows.
    #
    # `is` and `in` are deliberately EXCLUDED for now. PostgREST needs
    # `is.null` / `is.true` (unquoted bareword) and `in.(a,b,c)` (paren-
    # wrapped CSV) which the generic `op.<urlencoded value>` serializer
    # below cannot produce safely. Add them back with proper formatting
    # + tests when a caller actually needs them.
    _ALLOWED_WHERE_OPS: frozenset[str] = frozenset({
        "eq", "neq", "gt", "gte", "lt", "lte",
        "like", "ilike",
    })

    def select(
        self,
        table: str,
        *,
        where: dict,
        columns: tuple[str, ...] | None = None,
        max_rows: int | None = None,
        where_ops: dict[str, tuple[str, Any]] | None = None,
    ) -> list[dict]:
        """Read rows matching `where` with automatic Range-header
        pagination. Returns the full result list (possibly empty).

        PostgREST caps a single response at `db-max-rows` (defaults to
        1000). This method walks `Range: 0-999`, `Range: 1000-1999`,
        … until either:
          * a page returns fewer than `_PAGE_SIZE` rows (natural end), or
          * cumulative row count would exceed `max_rows`
            (`_DEFAULT_MAX_ROWS` if not set), in which case it raises
            rather than silently truncating.

        The natural-end check is critical: without it, a server with a
        larger cap (say 10_000) would not be paginated correctly here,
        but we'd never know — it would just return one big page. With
        the natural-end check, we stop when we see we've gotten a
        non-full page.

        `where_ops` lets callers express non-equality filters (gte, lte,
        gt, lt, neq, like, ilike, is, in) without bypassing this method.
        Format: `{"col": (op, value)}`. Operator value is URL-encoded.
        Equality-only callers can leave it as None.
        """
        select_clause = ",".join(columns) if columns else "*"
        filter_parts = [f"{k}=eq.{_quote(v)}" for k, v in where.items()]
        for col, op_value in (where_ops or {}).items():
            op, value = op_value
            if op not in self._ALLOWED_WHERE_OPS:
                raise ValueError(
                    f"unsupported PostgREST operator {op!r} for column {col!r}; "
                    f"allowed: {sorted(self._ALLOWED_WHERE_OPS)}"
                )
            filter_parts.append(f"{col}={op}.{urllib.parse.quote(str(value), safe='')}")
        path = f"{table}?select={select_clause}"
        if filter_parts:
            path += "&" + "&".join(filter_parts)

        cap = max_rows if max_rows is not None else self._DEFAULT_MAX_ROWS
        results: list[dict] = []
        offset = 0
        while True:
            range_end = offset + self._PAGE_SIZE - 1
            headers = {"Range": f"{offset}-{range_end}"}
            page = self._helper._request("GET", path, extra_headers=headers)
            if not isinstance(page, list):
                break
            results.extend(page)
            if len(page) < self._PAGE_SIZE:
                # Natural end — server returned a partial page.
                break
            if len(results) >= cap:
                raise RuntimeError(
                    f"BotSupabaseAdapter.select exceeded max_rows={cap} "
                    f"on table {table!r} (cumulative {len(results)} rows). "
                    f"Tighten the `where` clause or pass an explicit "
                    f"`max_rows=` if this is intentional."
                )
            offset += self._PAGE_SIZE
        return results


def _quote(v: Any) -> str:
    """PostgREST filter-value encoding. Booleans → 'true'/'false'."""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)
