#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Some NixOS application shells omit these installed profiles; process-local only.
if [[ -d /run/current-system/sw/bin ]]; then
  export PATH="$HOME/.nix-profile/bin:/run/current-system/sw/bin:$PATH"
fi
case "${1:-help}" in
  setup)
    command -v uv >/dev/null || { echo 'Missing uv: install uv using your platform package manager.' >&2; exit 2; }
    uv venv --python 3.11.16 .local/osb/venv311
    uv pip sync --python .local/osb/venv311/bin/python --require-hashes osb/requirements.lock
    ;;
  build)
    for tool in docker patch python3; do command -v "$tool" >/dev/null || { echo "Missing $tool" >&2; exit 2; }; done
    python3 osb/build.py
    ;;
  smoke|parallel-smoke|campaign)
    [[ -x .local/osb/venv311/bin/python ]] || { echo 'Run ./osb/run.sh setup first.' >&2; exit 2; }
    [[ -f .local/osb/build/manifest.json ]] || { echo 'Run ./osb/run.sh build first.' >&2; exit 2; }
    exec .local/osb/venv311/bin/python osb/freeze.py "$1"
    ;;
  test)
    exec .local/osb/venv311/bin/python -m unittest discover -s osb -v
    ;;
  *) echo 'Usage: ./osb/run.sh {setup|build|smoke|parallel-smoke|campaign|test}'; exit 2 ;;
esac
