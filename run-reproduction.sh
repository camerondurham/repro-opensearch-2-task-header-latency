#!/usr/bin/env bash
# Prepare the pinned stock image, then delegate all workload/safety checks.
set -euo pipefail
if [[ $# -gt 0 ]]; then
  if [[ $# == 1 && ( $1 == --help || $1 == -h ) ]]; then
    echo 'Usage: ./run-reproduction.sh (creates results/run-<unique-id>)'
    exit 0
  fi
  echo 'No arguments supported; use ./reproduce.py --help for the standalone interface.' >&2
  exit 2
fi
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
for command in python3 curl docker; do
  command -v "$command" >/dev/null || { echo "Missing prerequisite: $command" >&2; exit 1; }
done
python3 - <<'PY'
import os, sys
if sys.platform != 'linux' or sys.version_info < (3, 10):
    raise SystemExit('Requires Linux and Python 3.10 or newer')
if any(k in os.environ for k in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')):
    raise SystemExit('Docker endpoint/context/TLS overrides must be unset; use the default local daemon')
PY
export PUBLIC_REPRO_DOCKER
PUBLIC_REPRO_DOCKER=$(command -v docker)
"$PUBLIC_REPRO_DOCKER" --context default context inspect default | python3 -c '
import json,sys
v=json.load(sys.stdin)
if len(v)!=1 or v[0]["Endpoints"]["docker"]["Host"]!="unix:///var/run/docker.sock":
    raise SystemExit("Requires the default local Unix-socket Docker daemon")'
if [[ -n $("$PUBLIC_REPRO_DOCKER" --context default ps -aq) ]]; then
  echo 'Use an idle Docker daemon: existing containers (including stopped) are refused.' >&2
  exit 1
fi
image='opensearchproject/opensearch@sha256:1f8b88245a6af61e7aa500afe0e87d43401e4b33140bb47230a919428ce3f7cb'
if ! "$PUBLIC_REPRO_DOCKER" --context default image inspect "$image" >/dev/null 2>&1; then
  "$PUBLIC_REPRO_DOCKER" --context default pull "$image"
fi
# Keep pulls on the user's normal proxy/auth route. This offline workload needs
# exact environment variables, so isolate only client-side container injection.
public_repro_tmp=$(mktemp -d)
trap 'rm -rf -- "$public_repro_tmp"' EXIT
export PUBLIC_REPRO_DOCKER_CONFIG="$public_repro_tmp/config"
python3 - <<'PY'
import json, os, shutil
from pathlib import Path
source=Path(os.environ.get('DOCKER_CONFIG', str(Path.home()/'.docker')))
target=Path(os.environ['PUBLIC_REPRO_DOCKER_CONFIG'])
if source.exists():
    shutil.copytree(source, target)
else:
    target.mkdir(mode=0o700)
config=target/'config.json'
data=json.loads(config.read_text()) if config.exists() else {}
data.pop('proxies', None)
config.write_text(json.dumps(data))
config.chmod(0o600)
PY
mkdir "$public_repro_tmp/bin"
cat > "$public_repro_tmp/bin/docker" <<'SH'
#!/bin/sh
exec "$PUBLIC_REPRO_DOCKER" --config "$PUBLIC_REPRO_DOCKER_CONFIG" "$@"
SH
chmod 700 "$public_repro_tmp/bin/docker"
public_repro_output="results/run-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
echo "Evidence directory: $public_repro_output"
PATH="$public_repro_tmp/bin:$PATH" python3 reproduce.py --output "$public_repro_output"
python3 reproduce.py --verify "$public_repro_output"
