#!/usr/bin/env python3
"""Build a matched one-class intervention with the stock bundled compiler."""
import hashlib
import copy
import fcntl
import uuid
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import zipfile

ROOT = Path(os.environ.get('OSB_REPO_ROOT', Path(__file__).resolve().parents[1])).resolve()
OUT = ROOT / '.local/osb/build'
IMAGE = 'opensearchproject/opensearch@sha256:1f8b88245a6af61e7aa500afe0e87d43401e4b33140bb47230a919428ce3f7cb'
SOURCE = 'fd9a9d90df25bea1af2c6a85039692e815b894f5'
JAVA = 'server/src/main/java/org/opensearch/action/search/TransportSearchScrollAction.java'
CLASS = 'org/opensearch/action/search/TransportSearchScrollAction.class'
DOCKER = ['docker', '--context', 'default']


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(argv, **kw):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=600, **kw)
    if result.returncode:
        raise RuntimeError(f'Command failed ({result.returncode}): {argv}\n{result.stdout}\n{result.stderr}')
    return result.stdout


def main():
    if OUT.exists():
        raise SystemExit('Build directory exists; retain it and use the existing manifest or choose a fresh checkout.')
    OUT.mkdir(parents=True)
    run(DOCKER + ['pull', IMAGE])
    source = urllib.request.urlopen(f'https://raw.githubusercontent.com/opensearch-project/OpenSearch/{SOURCE}/{JAVA}', timeout=30).read()
    blob = hashlib.sha1(b'blob ' + str(len(source)).encode() + b'\0' + source).hexdigest()
    if blob != '4713d03c93bacd7a4f2564b00ffcd3a379957a92':
        raise RuntimeError('Pinned public source blob mismatch')
    for mode in ('baseline', 'candidate'):
        path = OUT / mode / JAVA
        path.parent.mkdir(parents=True)
        path.write_bytes(source)
    run(['patch', '-p1', '--input', str(ROOT / 'proposed-fix.patch')], cwd=OUT / 'candidate')
    shutil.copyfile(ROOT / 'osb/LifecycleContract.java', OUT / 'LifecycleContract.java')
    script = '''set -euo pipefail
JDK=/usr/share/opensearch/jdk/bin
LIB=/usr/share/opensearch/lib
"$JDK/java" -version 2> /work/jdk.txt
cp "$LIB/opensearch-2.19.0.jar" /work/stock.jar
for variant in baseline candidate; do
  mkdir /work/$variant/classes
  "$JDK/javac" --release 11 -proc:none -cp "$LIB/*" -d /work/$variant/classes /work/$variant/''' + JAVA + '''
done
"$JDK/javac" --release 11 -proc:none -cp "/work/candidate/classes:$LIB/*" -d /work/test-classes /work/LifecycleContract.java
"$JDK/java" -ea -cp "/work/test-classes:/work/candidate/classes:$LIB/*" org.opensearch.action.search.LifecycleContract > /work/contract.txt
'''
    (OUT / 'compile.sh').write_text(script)
    shutil.copyfile(__file__, OUT / 'build.py')
    shutil.copyfile(ROOT / 'proposed-fix.patch', OUT / 'proposed-fix.patch')
    inputs = ['build.py', 'compile.sh', 'LifecycleContract.java', 'proposed-fix.patch',
              'baseline/' + JAVA, 'candidate/' + JAVA]
    input_hashes = {name: sha(OUT/name) for name in inputs}
    # No server, networking, or privileges needed for this bounded compiler container.
    owner = 'osbth-build-' + uuid.uuid4().hex[:12]
    try:
        log = run(DOCKER + ['run', '--rm', '--name', owner, '--network', 'none', '--cpus', '2', '--memory', '2g',
                           '--memory-swap', '2g', '--user', '0', '--label', 'org.osb-task-header.build=' + owner,
                           '-v', f'{OUT}:/work', '--entrypoint', 'bash', IMAGE, '/work/compile.sh'])
    finally:
        for cid in run(DOCKER + ['ps', '-aq', '--filter', 'label=org.osb-task-header.build=' + owner]).split():
            node = json.loads(run(DOCKER + ['inspect', cid]))[0]
            if node['Name'] != '/' + owner or node['Config']['Labels'].get('org.osb-task-header.build') != owner:
                raise RuntimeError('Compiler container ownership mismatch; refusing removal')
            run(DOCKER + ['rm', '-f', cid])
    if any(sha(OUT/name) != digest for name, digest in input_hashes.items()):
        raise RuntimeError('Build inputs changed during compilation')
    (OUT / 'build.log').write_text(log)
    manifest = {'source_commit': SOURCE, 'source_blob': blob, 'stock_image': IMAGE,
                'patch_sha256': sha(ROOT / 'proposed-fix.patch'), 'jdk': (OUT / 'jdk.txt').read_text(),
                'compiler': 'javac --release 11 -proc:none', 'contract': (OUT / 'contract.txt').read_text(),
                'input_sha256': input_hashes, 'variants': {}}
    original = zipfile.ZipFile(OUT / 'stock.jar')
    for mode in ('baseline', 'candidate'):
        generated = list((OUT / mode / 'classes').rglob('*.class'))
        if [str(p.relative_to(OUT / mode / 'classes')) for p in generated] != [CLASS]:
            raise RuntimeError('Unexpected compiler outputs')
        jar = OUT / mode / 'opensearch-2.19.0.jar'
        with zipfile.ZipFile(jar, 'w') as dest:
            for entry in original.infolist():
                dest.writestr(copy.copy(entry), generated[0].read_bytes() if entry.filename == CLASS else original.read(entry))
        with zipfile.ZipFile(jar) as packed:
            changed = [e for e in original.namelist() if original.read(e) != packed.read(e)]
        if changed != [CLASS]:
            raise RuntimeError(f'Unexpected stock-to-{mode} payload changes: {changed}')
        (OUT / mode / 'Dockerfile').write_text(f'FROM {IMAGE}\nCOPY --chown=opensearch:opensearch opensearch-2.19.0.jar /usr/share/opensearch/lib/opensearch-2.19.0.jar\n')
        tag = 'osb-task-header:' + mode + '-' + sha(jar)[:12]
        run(DOCKER + ['build', '--network=none', '-t', tag, str(OUT / mode)])
        image_id = run(DOCKER + ['image', 'inspect', '--format', '{{.Id}}', tag]).strip()
        manifest['variants'][mode] = {'image_id': image_id, 'jar_sha256': sha(jar),
                                     'class_sha256': sha(generated[0]), 'source_sha256': sha(OUT / mode / JAVA),
                                     'changed_entries_from_stock': changed}
    with zipfile.ZipFile(OUT / 'baseline/opensearch-2.19.0.jar') as a, zipfile.ZipFile(OUT / 'candidate/opensearch-2.19.0.jar') as b:
        changed = [e for e in a.namelist() if a.read(e) != b.read(e)]
        if changed != [CLASS]:
            raise RuntimeError('Intervention includes unexpected jar entries')
        manifest['sole_intervention_entries'] = changed
    manifest['stock_jar_sha256'] = sha(OUT / 'stock.jar')
    (OUT / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    with open('/tmp/opensearch-regression-lab.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main()
