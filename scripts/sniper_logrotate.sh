#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEFAULT_STATE_ROOT="${REPO_ROOT}"
case "${REPO_ROOT}" in
  */.runtime/*/current|*/.runtime/*/releases/*)
    DEFAULT_STATE_ROOT="${REPO_ROOT%%/.runtime/*}"
    ;;
esac
SNIPER_ROOT="${SNIPER_LOG_ROOT:-${SNIPER_STATE_ROOT:-${DEFAULT_STATE_ROOT}}}"
STATE_DIR="${SNIPER_ROOT}/data/logrotate"
TEMPLATE="${REPO_ROOT}/config/sniper.logrotate"
CONFIG="${STATE_DIR}/sniper.conf"

mkdir -p "${STATE_DIR}"
sed "s|@SNIPER_ROOT@|${SNIPER_ROOT}|g" "${TEMPLATE}" > "${CONFIG}"
exec /usr/sbin/logrotate -s "${STATE_DIR}/status" "$@" "${CONFIG}"
