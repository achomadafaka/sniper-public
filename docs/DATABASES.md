# Database structure

The snapshot includes schema definitions and migrations, not production database files or private records. Sniper uses several stores; there is no single SQL dump that defines every subsystem.

| Store | Schema and access code |
| --- | --- |
| Core SQLite connection and shared persistence | `sniper/persistence/db_core.py`, `sniper/persistence/` |
| Memecoin positions, attempts, reservations, and execution evidence | `sniper/persistence/db_memecoin.py` and adjacent domain modules |
| Office records, append-only chains, memory, paper trials, and evaluation | `sniper/office/store.py`, `memory_store.py`, `training_store.py`, and domain-owned initializers |
| Hyperliquid research and execution state | `sniper/persistence/db_hyperliquid.py`, `sniper/hyperlive/real_store.py`, and adjacent stores |
| Wallet thesis materialization | `sniper/analysis/winning_wallet_thesis/db_store.py` |
| Brain/Postgres schema, functions, constraints, and access policies | `migrations/`, `setup_supabase_brain.sql`, `bot/brain_graph/store.py` |
| Other capture, watch, and research databases | Their owning modules under `sniper/office/`, `sniper/persistence/`, and `scripts/` |

Many SQLite schemas are created and upgraded by their owning Python module. Inspect that module before opening a store or applying DDL. The Supabase migrations are included verbatim from the source snapshot; do not apply every forward and rollback file indiscriminately.

The private repository also contains a file named `docs/current_schema.sql`, last changed in March 2026. It is omitted because it is a historical snapshot, not a complete current bootstrap schema. Current code-owned definitions and migrations are the relevant review surfaces.

A fresh end-to-end installation of every database has not been certified by the public focused test suite. Office storage behavior is exercised using temporary test databases; that does not prove complete production-schema reproducibility.
