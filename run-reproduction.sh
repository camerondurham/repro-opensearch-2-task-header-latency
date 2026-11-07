#!/usr/bin/env bash
# Compatibility entry point: canonical live work is now native OSB.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
  echo 'Native OSB usage: ./run-reproduction.sh [smoke|parallel-smoke|campaign]'
  echo 'First run ./osb/run.sh setup and ./osb/run.sh build; offline: python3 osb/verify.py'
  exit 0
fi
if [[ $# -gt 1 ]]; then
  echo 'Expected at most one OSB mode.' >&2
  exit 2
fi
case "${1:-smoke}" in
  smoke|parallel-smoke|campaign) exec ./osb/run.sh "${1:-smoke}" ;;
  *) echo 'Expected smoke, parallel-smoke, or campaign.' >&2; exit 2 ;;
esac
