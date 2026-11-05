#!/usr/bin/env python3
"""One standard-library offline evidence check; never starts a live workload."""
import json
from pathlib import Path
import subprocess
import sys
from analyze import analyze, need, verify_hashes

root = Path(__file__).resolve().parents[1]
for directory in ('smoke', 'parallel-smoke', 'campaign'):
    path = root / 'results/osb' / directory
    verify_hashes(path)
    report = analyze(path)
    need(report == json.loads((path/'report.json').read_text()), 'Recomputed report differs: ' + directory)
    print('Verified OSB ' + directory)
failure = root/'results/osb/default-client-original-failure'
verify_hashes(failure)
details = json.loads((failure/'failure.json').read_text())
need(details['benchmark_success'] is False and details['default_max_headers'] == 128,
     'Original client failure must remain a failure')
subprocess.run([sys.executable, 'reproduce.py', '--verify', 'results/stock'], cwd=root, check=True)
subprocess.run([sys.executable, 'results/performance/recompute_late_p99.py'], cwd=root, check=True,
               stdout=subprocess.DEVNULL)
print('Verified historical stock and historical late-window evidence')
