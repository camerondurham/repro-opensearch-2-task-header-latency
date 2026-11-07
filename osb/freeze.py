#!/usr/bin/env python3
"""Capture first, then exec a fresh interpreter from those exact source bytes."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ('run.py', 'build.py', 'generate.py', 'workload.py', 'capture/sitecustomize.py',
          'PROTOCOL.md', 'requirements.lock', 'freeze.py', 'evidence.py', 'processes.py')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('smoke', 'parallel-smoke', 'campaign'):
        raise SystemExit('Use ./osb/run.sh {smoke|parallel-smoke|campaign}')
    mode = sys.argv[1]
    out = ROOT/'.local/osb'/(mode+'-'+time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
    code = out/'code'
    code.mkdir(parents=True, exist_ok=False)
    originals = {name: ROOT/'osb'/name for name in INPUTS}
    originals['proposed-fix.patch'] = ROOT/'proposed-fix.patch'
    for name, source in originals.items():
        (code/name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, code/name)
    hashes = {name: digest(code/name) for name in originals}
    if any(digest(path) != hashes[name] for name, path in originals.items()):
        raise RuntimeError('Executable inputs changed while capturing; no run started')
    manifest = {'start_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'worktree_status': subprocess.check_output(['git', 'status', '--porcelain', '--',
                     *['osb/' + name for name in INPUTS], 'proposed-fix.patch'], cwd=ROOT, text=True).splitlines(),
                'worktree_status_scope': 'executed snapshot inputs only; independent offline analysis/docs may change',
                'files': hashes, 'build_manifest_sha256': digest(ROOT/'.local/osb/build/manifest.json'),
                'execution': 'fresh interpreter executes code/run.py; workload and metrics hook loaded from this snapshot',
                'python': sys.version, 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    (out/'source-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    shutil.copyfile(ROOT/'.local/osb/build/manifest.json', out/'build.json')
    build = json.loads((out/'build.json').read_text())
    for name, expected in build.get('input_sha256', {}).items():
        source = ROOT/'.local/osb/build'/name
        if digest(source) != expected:
            raise RuntimeError('Build input changed: ' + name)
        target = out/'build-inputs'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    import osbenchmark
    package = Path(osbenchmark.__file__).parent
    software = {'distributions': dict(sorted((d.metadata['Name'], d.version) for d in importlib.metadata.distributions())),
                'osb_source_tag_commit': '445eb5a3188c1e3d3a3ca6bfe87757902ce5b10e',
                'critical_osb_files': {name: digest(package/name) for name in (
                    'metrics.py', 'worker_coordinator/worker_coordinator.py', 'worker_coordinator/runner.py',
                    'workload/loader.py', 'resources/workload-schema.json')}}
    (out/'software.json').write_text(json.dumps(software, indent=2)+'\n')
    env = dict(os.environ, OSB_REPO_ROOT=str(ROOT), OSB_RUN_ROOT=str(out))
    os.execve(sys.executable, [sys.executable, str(code/'run.py'), mode], env)


if __name__ == '__main__':
    main()
