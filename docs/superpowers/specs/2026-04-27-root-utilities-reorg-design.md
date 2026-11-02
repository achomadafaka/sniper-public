# Root Utilities Reorg Phase 3 Design

**Goal:** Continue reducing root-level Python clutter by moving the pure utility / settings / runtime / markets-helper layer into the `sniper.*` package. Phase 3 is utilities only — no service entrypoints, no domain-analysis modules.

**Decision:** Keep root compatibility shims during move commits. Migrate callers in cleanup commits. Verify once at the end before deploy. No mandatory soak after cleanup unless a runtime path is touched.

**Parent work:** Phase 1 moved DB helpers into `sniper.persistence`. Phase 2 moved `filters`, `serializers`, `paper_settings`, `web_settings` into `sniper.{filters, web.serializers, settings.paper, settings.web}` and deleted the four root shims. Phase 3 starts the utilities layer.

**Out of scope for Phase 3:**
- Service entrypoints (`alerter.py`, `stream_low_buys.py`, `paper_trader.py`, `web_server.py`, `strategy_runner.py`, `strategy_service.py`, `real_trader.py`, `sniper_mcp.py`, `dashboard.py`, `stink_bid_runner.py`, `hl_whale_poller.py`, `market_outcome_poller.py`, `stream_hyperliquid.py`, `stream_heartbeat.py`).
- Domain analysis modules (`bot_detector.py`, `entity_clusters.py`, `copy_viability.py`, `classify_accounts.py`, `analyze_users.py`, `agent_calibration.py`, `agent_pipeline.py`, `replay_engine.py`, `retrospective_analyzer.py`, `situation_model.py`, `world_context.py`, `finance_analyst.py`, `news_intel.py`, `mine_polymarket_patterns.py`, `discover_top_performers.py`, `brain.py`).
- DB layer (`db.py`, `db_discovery.py`).
- One-shot scripts/tools (`export_data.py`, `health_check.py`, `paper_status.py`, `sync.py`, `real_position_redeemer.py`, `backfill_polymarket_low_buys.py`, `ctf_redeemer.py`).
- `paper_limit_engine.py` (defer; touches paper trader execution path).

These get later phases.

---

## Scope

Move these 8 modules into `sniper.*`:

| Order | Current path | Target path | Caller count |
|---:|---|---|---:|
| 1 | `utils.py` | `sniper/utils.py` | 11 |
| 2 | `scoring_env_defaults.py` | `sniper/settings/scoring_env_defaults.py` | 2 |
| 3 | `instance_profile.py` | `sniper/settings/instance_profile.py` | 8 |
| 4 | `wallet_key_loader.py` | `sniper/runtime/wallet_key_loader.py` | 7 |
| 5 | `source_meta.py` | `sniper/markets/source_meta.py` | 3 |
| 6 | `content_normalization.py` | `sniper/markets/content_normalization.py` | 6 |
| 7 | `polymarket_api.py` | `sniper/markets/polymarket_api.py` | 29 |
| 8 | `patterns.py` | `sniper/markets/patterns.py` | 2 |

Create:

```text
sniper/runtime/__init__.py
sniper/markets/__init__.py
```

`sniper/settings/__init__.py` already exists from Phase 2.

**Order rationale:**
- `utils.py` first — no deps, used by patterns and serializers.
- `scoring_env_defaults`, `instance_profile`, `wallet_key_loader`, `source_meta`, `content_normalization` — independent leaves, any order.
- `polymarket_api` before `patterns` — `patterns.py` imports `from polymarket_api import fetch_user_trades`. Moving polymarket_api first means when patterns moves, its internal import gets rewritten to the canonical path directly.

User's original order swapped 7/8; this spec swaps them so polymarket_api precedes patterns.

---

## Module Notes

### `utils.py` (85 lines)
Pure type helpers: `_safe_float`, `_safe_int`, `_normalize_wallet`, `_normalize_tx_hash`, `_optional_float`, `_format_unix`, `_short_wallet`, `_parse_any_ts`. No deps. Move to `sniper/utils.py` (single module file, not a package), per user direction: "move utils.py here only if it is truly generic and small enough" — it is.

### `scoring_env_defaults.py` (69 lines)
Constant dict of canonical env-var defaults used by tests and replay harness. No deps. Move to `sniper/settings/scoring_env_defaults.py`.

### `instance_profile.py` (185 lines)
Frozen dataclass for per-VPS-instance profile loaded from env. No deps. Move to `sniper/settings/instance_profile.py`.

### `wallet_key_loader.py` (81 lines)
Resolves wallet private-key file path and loads signer metadata. No deps. Move to `sniper/runtime/wallet_key_loader.py`.

### `source_meta.py` (298 lines)
Static dict of news-source bias/region/lean/reliability metadata + alignment helpers. No runtime deps. Move to `sniper/markets/source_meta.py`.

### `content_normalization.py` (158 lines)
Title/section regex normalization helpers. No deps. Move to `sniper/markets/content_normalization.py`.

### `polymarket_api.py` (1191 lines, 29 callers)
HTTP client for Polymarket Gamma + Data + Web APIs. Largest blast radius in this phase. No deps on other root utilities except `os`/`requests`. Move to `sniper/markets/polymarket_api.py`.

### `patterns.py` (285 lines)
Suspicious-pattern caches and trade-id helpers. Imports `polymarket_api.fetch_user_trades` and `utils._format_unix`/`_safe_int`/etc. Both deps already in `sniper.*` after step 7 + step 1. When moved, internal imports must be rewritten to canonical paths.

---

## Per-File Procedure

Each move = one commit. Do not batch.

For each module:

1. `git mv` to target path. For new sub-packages (`sniper/runtime/`, `sniper/markets/`), create the directory and an empty `__init__.py` first.
2. Write a new root shim at the original path (module-alias style):

```python
"""Compatibility shim for the moved <name> module.

Moved to sniper.<canonical>. Import from there going forward.
"""

import sys as _sys

from sniper.<parent> import <name> as _impl  # or `from sniper import <name> as _impl` for sniper/utils.py

_sys.modules[__name__] = _impl
```

3. Inside the moved module, rewrite imports of any other modules that have already been canonicalized in this phase. Specifically:
   - `patterns.py` → rewrite `from polymarket_api import` → `from sniper.markets.polymarket_api import`, `from utils import` → `from sniper.utils import`.
   - `serializers.py` already imports `from utils import` — that file is already in `sniper/web/`, so when `utils` moves, also rewrite `sniper/web/serializers.py`'s import to canonical. Treat that as part of the `utils` move commit.
4. Regenerate index: `python3 scripts/brain_index.py generate`.
5. Smoke import old + new paths.
6. Commit one move:

```bash
git add <root-shim> <new-path> sniper/<parent>/__init__.py index/
git commit -m "refactor(root): move <name> into sniper.<parent>"
```

**No per-commit pytest.** Per the new reorg policy, full pytest runs once at the end of the phase.

---

## Cleanup Commits (one per file)

After all 8 moves are committed:

For each module, one migration commit followed by one shim-deletion commit (Phase 2 pattern):

1. `rg`-survey direct callers; rewrite all `from <name> import` and `import <name>` to canonical paths.
2. Commit: `refactor(root): migrate <name> callers to sniper.<canonical>`.
3. `git rm <name>.py`; regenerate index.
4. Commit: `refactor(root): delete <name> root shim`.

---

## End-of-Phase Verification

Single batch before deploy:

```bash
python3 scripts/brain_index.py generate
python3 scripts/validate_schema_refs.py
```

Smoke imports:

```bash
python3 - <<'PY'
import importlib
for name in (
    "sniper.utils",
    "sniper.settings.scoring_env_defaults",
    "sniper.settings.instance_profile",
    "sniper.runtime.wallet_key_loader",
    "sniper.markets.source_meta",
    "sniper.markets.content_normalization",
    "sniper.markets.polymarket_api",
    "sniper.markets.patterns",
):
    importlib.import_module(name); print("ok", name)
PY
```

Sweep for residual root imports:

```bash
rg "^(from (utils|scoring_env_defaults|instance_profile|wallet_key_loader|source_meta|content_normalization|polymarket_api|patterns)\b|import (utils|scoring_env_defaults|instance_profile|wallet_key_loader|source_meta|content_normalization|polymarket_api|patterns)\b)" --type py
```

Expected: empty after caller migration.

Full pytest:

```bash
python3 -m pytest tests/ -q --deselect tests/test_paper_trader_world_boosts.py::TestPaperTraderWorldBoosts::test_non_sports_market_keeps_world_boost
```

---

## Deploy + Soak

Single deploy at end of phase. Per the new reorg policy: no mandatory full-cycle soak — utilities don't touch service entrypoints or runtime behavior. Watch journal for ImportError/ModuleNotFoundError on first request after restart; no extended hold required.

---

## Risks

| Risk | Mitigation |
|---|---|
| `polymarket_api.py` has 29 callers — wide blast radius | Root shim during move; canonical-path rewrite in cleanup commit. End-of-phase pytest catches failures. |
| `patterns.py` depends on already-moved `polymarket_api` and `utils` | Move polymarket_api before patterns; rewrite patterns' internal imports during the patterns move. |
| `utils.py` is consumed by `sniper/web/serializers.py` (already in package) | Rewrite `sniper/web/serializers.py`'s `from utils import` to `from sniper.utils import` as part of the utils move commit. |
| Test files that monkeypatch `polymarket_api.X` rely on a single canonical module identity | Module-alias shim (`sys.modules[__name__] = _impl`) preserves identity — same object as canonical. Verified pattern in Phase 2. |
| `scoring_env_defaults` reads `os` at import time only via dict literal — not env-time-sensitive | None needed; pure data. |

---

## Approval State

Spec drafted. Awaiting approval before move execution.
