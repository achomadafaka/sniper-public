#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SNIPER_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_DIR="${SNIPER_BACKUP_DIR:-${SNIPER_ROOT}/data/backups/daily}"

exec python3 "${SCRIPT_DIR}/daily_backup.py" \
  --db "${SNIPER_ROOT}/sniper_tracker.db" \
  --backup-dir "${BACKUP_DIR}" \
  "$@"
