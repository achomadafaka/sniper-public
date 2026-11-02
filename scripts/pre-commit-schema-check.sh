#!/usr/bin/env bash
# Pre-commit hook: run the schema-ref validator AND keep index/*.json fresh.
#
# Two independent guards, both matching CI checks:
#   1. validate_schema_refs.py on staged .py / .md — catches unknown column names.
#      check_tracker_connections.py on the same files — catches a long-lived raw
#      sqlite3.connect on the funded ledger, which pins the WAL (1.4 GB, 2026-08-26).
#   2. brain_index.py generate — regenerates schema_registry + dependency_index; if EITHER
#      file changed as a result, the hook auto-stages them into the current commit so the
#      "index artifacts are stale" freshness gate in CI passes. Without this, every commit
#      that shifts the dependency map without a manual `brain_index.py generate` fails CI.
#
# Install:
#   cp scripts/pre-commit-schema-check.sh .git/hooks/pre-commit
#   chmod +x .git/hooks/pre-commit

set -euo pipefail

STAGED_FILES=()
while IFS= read -r -d '' file; do
    case "$file" in
        *.py|*.md) STAGED_FILES+=("$file") ;;
    esac
done < <(git diff --cached --name-only --diff-filter=ACM -z)

# --- 1. Schema-ref validator (only if there ARE staged .py/.md files) ---------------------
if [ "${#STAGED_FILES[@]}" -gt 0 ]; then
    echo "Schema reference check on staged files..."
    python3 scripts/validate_schema_refs.py --files "${STAGED_FILES[@]}"

    # A long-lived raw handle on sniper_tracker.db pins the WAL. Staged files only: this must
    # not fail a commit over code the author did not touch.
    echo "Funded-ledger connection check on staged files..."
    python3 scripts/check_tracker_connections.py --files "${STAGED_FILES[@]}"
fi

# --- 2. Index freshness (only when the commit touches .py — indexes are code-derived) -----
if git diff --cached --name-only --diff-filter=ACMRD | grep -qE '\.py$'; then
    # brain_index reads the working tree, not Git's staged index. If tracked Python WIP is
    # unstaged, regenerating now would auto-stage artifacts derived from code that is NOT in this
    # commit. Refuse instead of leaking another task's changes through generated JSON.
    UNSTAGED_PY=$(git diff --name-only --diff-filter=ACMRD | grep -E '\.py$' || true)
    if [ -n "$UNSTAGED_PY" ]; then
        echo "ERROR: cannot regenerate brain indexes with unstaged Python changes:" >&2
        echo "$UNSTAGED_PY" | sed 's/^/  /' >&2
        echo "Stage or stash those files, then retry the commit." >&2
        exit 1
    fi
    echo "Regenerating brain index (schema + dependency)..."
    python3 scripts/brain_index.py generate --core-only >/dev/null
    REGEN_DIFF=$(git diff --name-only -- index/schema_registry.json index/dependency_index.json || true)
    if [ -n "$REGEN_DIFF" ]; then
        echo "Index artifacts drifted — auto-staging into this commit:"
        echo "$REGEN_DIFF" | sed 's/^/  /'
        git add index/schema_registry.json index/dependency_index.json
    fi
fi
