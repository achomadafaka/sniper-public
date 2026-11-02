#!/usr/bin/env bash
# Nightly wrapper for scripts/compile_knowledge.py.
#
# Compiles knowledge/ pages from live DB state, then commits + pushes
# any changes so agents reading via git pull pick up the fresh pages.
#
# Installed as the ExecStart for sniper-knowledge-compile.service.
# Safe to run ad-hoc: on a clean tree it commits nothing.
#
# Non-zero exit only on compile failure — a no-op git commit or a stale
# git push (e.g. already up-to-date) is treated as success.

set -euo pipefail

REPO="/home/sniper/sniper"
PY="${REPO}/.venv/bin/python3"

cd "${REPO}"

# Compile — this mutates files under knowledge/ if anything has changed.
"${PY}" scripts/compile_knowledge.py

# Step 1: if knowledge/ is dirty, create a local commit scoped to it.
# Pathspec on `commit -- knowledge/` is load-bearing: commits ONLY knowledge/
# even if the working tree or index has unrelated changes. No reset needed —
# that would mutate operator staging intent.
if [[ -n "$(git status --porcelain knowledge/ 2>/dev/null || true)" ]]; then
    git add knowledge/
    git -c user.email="noreply@anthropic.com" \
        -c user.name="knowledge-compiler" \
        commit -m "knowledge: daily recompile $(date -u +%Y-%m-%dT%H:%MZ)" \
        -- knowledge/ \
        || echo "WARN: commit failed" >&2
else
    echo "knowledge/ clean — no new commit."
fi

# Step 2: publish any local commits (new or stranded from a prior failed push).
# This is keyed off "branch ahead of origin", NOT "knowledge/ dirty", so a
# commit that was created on a previous run but couldn't push still gets
# retried on subsequent runs. Rebase on top of origin/main so concurrent
# human pushes don't cause permanent divergence; --autostash protects any
# uncommitted modifications outside knowledge/. Conflicts abort cleanly.
git fetch --quiet origin main
if [[ -n "$(git rev-list origin/main..HEAD 2>/dev/null || true)" ]]; then
    if ! git rebase --autostash origin/main; then
        echo "WARN: rebase failed — leaving local commit for human review" >&2
        git rebase --abort 2>/dev/null || true
        exit 0
    fi
    if git push origin main; then
        # Touchfile for brain_health V7 — surfaces prolonged push failures.
        mkdir -p "${REPO}/logs"
        touch "${REPO}/logs/knowledge_compile.last_push_success"
    else
        echo "WARN: push failed (next run will retry)" >&2
    fi
else
    echo "in sync with origin — nothing to push."
    # No-op runs are still evidence the cron is alive. Touch the success
    # marker so brain_health doesn't false-positive on quiet days.
    mkdir -p "${REPO}/logs"
    touch "${REPO}/logs/knowledge_compile.last_push_success"
fi
