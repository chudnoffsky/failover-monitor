#!/usr/bin/env bash
set -euo pipefail

echo "============================================================"
echo " запуск приложения мониторинга / failover"
echo "============================================================"
echo " адреса (CHECK_ADDRESSES) : ${CHECK_ADDRESSES:-<не задано>}"
echo " URL проверки (CHECK_URL) : ${CHECK_URL:-/health}"
echo " интервал (CHECK_INTERVAL): ${CHECK_INTERVAL:-5} c"
echo " порог неудач (FAIL_*)    : ${FAIL_THRESHOLD:-3}"
echo " файл состояния           : ${STATE_FILE:-/data/state.json}"
echo "============================================================"

if [[ -z "${CHECK_ADDRESSES:-}" ]]; then
  echo "ERROR: переменная CHECK_ADDRESSES не задана. завершение...(" >&2
  exit 1
fi

exec python3 /app/monitor.py