#!/usr/bin/env python3
"""Own bounded containers; delegate all workload operations to real OSB."""
import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import os
import resource
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
import psutil

from build import IMAGE, ROOT, DOCKER, sha
from generate import generate

LABEL = 'org.osb-task-header.owner'


def command(args, timeout=60):
    p = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f'Command failed: {args}\n{p.stderr}')
    return p.stdout.strip()


def request(target, path):
    with urllib.request.urlopen(target + path, timeout=5) as response:
        return json.load(response)


def resource_sample():
    memory, swap = psutil.virtual_memory(), psutil.swap_memory()
    return dict(time=time.time(), cpu_percent=psutil.cpu_percent(), available_bytes=memory.available,
                swap_in=swap.sin, swap_out=swap.sout)


def preflight():
    endpoint = command(DOCKER + ['context', 'inspect', 'default', '--format', '{{.Endpoints.docker.Host}}'])
    if endpoint != 'unix:///var/run/docker.sock':
        raise RuntimeError('Only default local Docker socket is permitted')
    if command(DOCKER + ['ps', '-q']):
        raise RuntimeError('Performance prerequisite: existing running containers; leaving them untouched')
    if os.cpu_count() < 8 or psutil.virtual_memory().available < 12 * 2**30 or shutil.disk_usage(ROOT).free < 8 * 2**30:
        raise RuntimeError('Need 8 logical CPUs, 12 GiB available RAM and 8 GiB disk')
    if psutil.cpu_percent(interval=1) > 15:
        raise RuntimeError('Host busy before run (>15% CPU); no other jobs will be stopped')
    return dict(logical_cpus=os.cpu_count(), available_memory_bytes=psutil.virtual_memory().available,
                disk_free_bytes=shutil.disk_usage(ROOT).free, platform='Linux x86_64 / NixOS / WSL2',
                docker_endpoint='default local Unix socket')


def run_unit(root, name, variant, smoke, control, quick=False):
    pre = preflight()
    unit = root / name
    unit.mkdir()
    private = unit / 'private'
    private.mkdir()
    capture = unit / 'raw'
    capture.mkdir()
    manifest = json.loads((ROOT / '.local/osb/build/manifest.json').read_text())
    image = IMAGE if variant == 'stock' else manifest['variants'][variant]['image_id']
    owner = 'osbth-' + uuid.uuid4().hex[:12]
    network = owner + '-net'
    containers, child, network_created = [], None, False
    receipt = dict(name=name, variant=variant, smoke=smoke, control=control, quick=quick, preflight=pre,
                   image=image, start=time.time(), nodes=1 if smoke else 3, errors=[], clean=False,
                   osb_version=importlib.metadata.version('opensearch-benchmark'), python=sys.version.split()[0],
                   limits=dict(node_cpus=2, node_memory_bytes=3*2**30, heap_bytes=2**30, data_bytes=512*2**20),
                   config=dict(batch=10, ubq_rate=120, search_rate=50, search_clients=2,
                               warmup=2 if smoke or quick else 20, deadline=240, recovery=2 if smoke or quick else 10),
                   sources={str(p.relative_to(ROOT)): sha(p) for p in sorted((ROOT / 'osb').rglob('*'))
                            if p.is_file() and '__pycache__' not in str(p)})
    receipt.update(generate(unit / 'workload', smoke=smoke, candidate=variant == 'candidate', control=control, quick=quick))
    try:
        command(DOCKER + ['network', 'create', '--label', LABEL + '=' + owner, network])
        network_created = True
        for i in range(receipt['nodes']):
            cname = owner + '-n' + str(i)
            args = DOCKER + ['run', '-d', '--pull', 'never', '--name', cname, '--hostname', 'n' + str(i),
                '--label', LABEL + '=' + owner, '--network', network, '--network-alias', 'n' + str(i),
                '--cpus', '2', '--memory', '3g', '--memory-swap', '3g', '--ulimit', 'nofile=65536:65536',
                '--cpuset-cpus', f'{i*2},{i*2+1}',
                '--mount', 'type=tmpfs,destination=/usr/share/opensearch/data,tmpfs-size=536870912,tmpfs-mode=1777',
                '-p', '127.0.0.1::9200']
            env = {'cluster.name': owner, 'node.name': 'n' + str(i), 'network.host': '0.0.0.0',
                   'node.processors': '2', 'OPENSEARCH_JAVA_OPTS': '-Xms1g -Xmx1g',
                   'DISABLE_INSTALL_DEMO_CONFIG': 'true', 'DISABLE_SECURITY_PLUGIN': 'true',
                   'task_resource_tracking.enabled': 'true'}
            env.update({'discovery.type': 'single-node'} if smoke else
                       {'discovery.seed_hosts': 'n0,n1,n2', 'cluster.initial_cluster_manager_nodes': 'n0,n1,n2'})
            for key, value in env.items():
                args += ['-e', key + '=' + value]
            args += [image]
            # Retain launch intent before mutation, so ambiguous launches are found by exact label in finally.
            with (private / 'commands.jsonl').open('a') as out:
                out.write(json.dumps(args) + '\n')
            containers.append(command(args))
        inspection = json.loads(command(DOCKER + ['inspect', *containers]))
        (private / 'containers.json').write_text(json.dumps(inspection, indent=2))
        target = 'http://127.0.0.1:' + inspection[0]['NetworkSettings']['Ports']['9200/tcp'][0]['HostPort']
        receipt['engine_image_ids'] = [n['Image'] for n in inspection]
        deadline = time.monotonic() + 120
        while True:
            try:
                health = request(target, f'/_cluster/health?wait_for_nodes={receipt["nodes"]}&wait_for_status=green&timeout=1s')
                if health['cluster_name'] != owner:
                    raise RuntimeError('Unexpected target identity')
                if not health['timed_out'] and health['number_of_nodes'] == receipt['nodes']:
                    break
            except (OSError, ValueError):
                pass
            if time.monotonic() > deadline:
                raise TimeoutError('Cluster readiness deadline')
            time.sleep(1)
        identity = request(target, '/')
        if identity['version']['build_hash'] != manifest['source_commit']:
            raise RuntimeError('Stock runtime build hash differs from pinned source')
        receipt['engine_version'] = identity['version']
        def stats(stage):
            data = request(target, '/fixture/_stats?filter_path=_all') if stage == 'after' else {}
            nodes = request(target, '/_nodes/stats/jvm,indices?filter_path=nodes.*.jvm.gc,nodes.*.indices.merges,nodes.*.indices.refresh')
            return dict(indices=data, nodes=list(nodes['nodes'].values()))
        receipt['before_stats'] = stats('before')
        home = private / 'home'
        home.mkdir()
        env = dict(os.environ, HOME=str(home), BENCHMARK_HOME=str(home),
                   OSB_CAPTURE=str(capture), OSB_TARGET=target,
                   PYTHONPATH=str(ROOT / 'osb/capture'), OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
        exe = str(Path(sys.executable).parent / 'opensearch-benchmark')
        argv = [exe, 'run', '--pipeline=benchmark-only', '--offline', '--enable-assertions',
                '--workload-path=' + str(unit / 'workload'), '--target-hosts=' + target.removeprefix('http://'),
                '--on-error=abort', '--test-run-id=' + str(uuid.uuid4()),
                '--client-options=timeout:30,max_retries:0,retry_on_timeout:false',
                '--results-format=csv', '--results-file=' + str(private / 'summary.csv')]
        receipt['osb_command'] = [a.replace(str(ROOT), '$REPO').replace(target.removeprefix('http://'), '$OWNED_LOOPBACK') for a in argv]
        with (private / 'osb.log').open('w') as out:
            def driver_limits():
                os.sched_setaffinity(0, {6, 7})
                resource.setrlimit(resource.RLIMIT_AS, (8*2**30, 8*2**30))
            child = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, env=env,
                                     start_new_session=True, preexec_fn=driver_limits)
            end = time.monotonic() + 540
            samples = []
            while child.poll() is None:
                sample = resource_sample()
                if len(samples) % 10 == 0:
                    running = command(DOCKER + ['ps', '-q', '--no-trunc']).split()
                    sample['unrelated_containers'] = len(set(running) - set(containers))
                samples.append(sample)
                if time.monotonic() > end:
                    raise TimeoutError('OSB exceeded 540-second unit bound')
                time.sleep(1)
            receipt['osb_exitcode'] = child.returncode
            (unit / 'host.json').write_text(json.dumps(samples))
            if child.returncode or (capture / 'capture-error').exists():
                raise RuntimeError('OSB failed; inspect private/osb.log')
        if not list(capture.glob('metrics-*.jsonl')):
            raise RuntimeError('OSB raw capture absent')
        receipt['after_stats'] = stats('after')
        receipt['execution_ok'] = True
    except BaseException as exc:
        receipt['execution_ok'] = False
        receipt['errors'].append(type(exc).__name__ + ': ' + str(exc))
        raise
    finally:
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(10)
        owned = command(DOCKER + ['ps', '-aq', '--filter', 'label=' + LABEL + '=' + owner]).split()
        for cid in owned:
            inspected = json.loads(command(DOCKER + ['inspect', cid]))[0]
            if inspected['Config']['Labels'].get(LABEL) != owner or not inspected['Name'].startswith('/' + owner + '-n'):
                raise RuntimeError('Cleanup owner mismatch')
            (private / (inspected['Name'][1:] + '.log')).write_text(command(DOCKER + ['logs', cid]))
            command(DOCKER + ['rm', '-f', cid])
        if network_created:
            inspected = json.loads(command(DOCKER + ['network', 'inspect', network]))[0]
            if inspected['Labels'].get(LABEL) != owner or inspected['Containers']:
                raise RuntimeError('Network owner mismatch or unexpected attachment')
            command(DOCKER + ['network', 'rm', network])
        receipt['clean'] = not command(DOCKER + ['ps', '-aq', '--filter', 'label=' + LABEL + '=' + owner])
        receipt['end'] = time.time()
        (unit / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(name + ': completed and cleaned', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['smoke', 'parallel-smoke', 'campaign'])
    args = parser.parse_args()
    if importlib.metadata.version('opensearch-benchmark') != '2.5.0':
        raise SystemExit('Install pinned requirements with Python 3.11 first')
    # Cooperate with the existing reproduction lock; never kill an existing runner.
    with open('/tmp/opensearch-regression-lab.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        root = ROOT / '.local/osb' / (args.mode + '-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
        root.mkdir()
        print(root, flush=True)
        shutil.copyfile(ROOT / '.local/osb/build/manifest.json', root / 'build.json')
        shutil.copyfile(ROOT / 'osb/PROTOCOL.md', root / 'PROTOCOL.md')
        order = [('parallel-baseline', 'baseline', False, False), ('parallel-candidate', 'candidate', False, False)] if args.mode == 'parallel-smoke' else [('stock', 'stock', True, False), ('baseline', 'baseline', True, False),
                 ('candidate', 'candidate', True, False)] if args.mode == 'smoke' else [
                     ('control-before', 'baseline', False, True),
                     *[(f'pair{pair}-{variant}', variant, False, False) for pair in range(1, 5)
                       for variant in (('baseline', 'candidate') if pair % 2 else ('candidate', 'baseline'))],
                     ('control-after', 'baseline', False, True)]
        (root / 'order.json').write_text(json.dumps(order))
        for name, variant, smoke, control in order:
            run_unit(root, name, variant, smoke, control, quick=args.mode == 'parallel-smoke')


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: sys.exit('Terminated; cleaning owned resources'))
    main()
