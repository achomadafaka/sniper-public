# Wallet Ledger Slice 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the local DuckDB warehouse foundation for the wallet fact ledger.

**Architecture:** Keep this slice schema-only. No live fetch, no capture resume, no protocol behavior change. Use the existing `duckdb>=1.0` dependency and create one small wallet-ledger package with path helpers and idempotent schema creation for wallet, market-data, and index DuckDB files.

**Tech Stack:** Python, DuckDB, pytest.

---

## Files

- Create: `sniper/analysis/wallet_protocol/ledger/__init__.py`
- Create: `sniper/analysis/wallet_protocol/ledger/paths.py`
- Create: `sniper/analysis/wallet_protocol/ledger/schema.py`
- Create: `tests/test_wallet_ledger_schema.py`

## Task 1: Warehouse Path Helpers

- [ ] Write tests in `tests/test_wallet_ledger_schema.py` proving:
  - wallet addresses are lowercased and used as `<wallet>.duckdb`
  - `market_data.duckdb` and `index.duckdb` paths live under the warehouse root
  - unsafe wallet strings are rejected
- [ ] Run:

```bash
python3 -m pytest tests/test_wallet_ledger_schema.py -q
```

Expected: fails because `sniper.analysis.wallet_protocol.ledger` does not exist.

- [ ] Implement `paths.py` with:
  - `LedgerPaths` dataclass
  - `ledger_paths(root, wallet)`
  - strict wallet validation
- [ ] Re-run the focused test.

## Task 2: Idempotent Schema Creation

- [ ] Add tests proving:
  - `ensure_wallet_schema(conn)` creates the 11 wallet-local tables
  - `ensure_market_schema(conn)` creates the 4 shared market tables
  - `ensure_index_schema(conn)` creates the 2 index tables
  - calling all ensure functions twice leaves the same table set
  - primary key metadata exists for `wallet_events`, `market_price_points`, and `wallet_price_lookup_requests`
- [ ] Run:

```bash
python3 -m pytest tests/test_wallet_ledger_schema.py -q
```

Expected: fails because schema functions do not exist.

- [ ] Implement `schema.py` with pure DuckDB DDL helpers. No fetch code. No Supabase code.
- [ ] Re-run the focused test.

## Task 3: Hygiene

- [ ] Run:

```bash
git diff --check
python3 -m pytest tests/test_wallet_ledger_schema.py -q
```

- [ ] Leave the tree uncommitted for operator review unless explicitly told to commit.
