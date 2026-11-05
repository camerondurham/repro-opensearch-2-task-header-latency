#!/usr/bin/env python3
"""Allowlist synthetic observations; leave all private logs outside version control."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import shutil

from analyze import analyze, rows


def publish(source, destination):
    report = analyze(source)  # Fail closed on incomplete evidence.
    destination.mkdir(parents=True, exist_ok=False)
    for name in ('order.json', 'build.json', 'PROTOCOL.md'):
        shutil.copyfile(source/name, destination/name)
    for name in report['runs']:
        unit, out = source/name, destination/name
        (out/'raw').mkdir(parents=True)
        for file in ('receipt.json', 'host.json'):
            shutil.copyfile(unit/file, out/file)
        shutil.copyfile(unit/'raw/events.jsonl', out/'raw/events.jsonl')
        raw = b''.join(p.read_bytes() for p in sorted((unit/'raw').glob('metrics-*.jsonl')))
        (out/'raw/metrics.jsonl.gz').write_bytes(gzip.compress(raw, mtime=0))
        provenance = {'projection': 'OSB metric allowlist at creation; exact concatenation, no deduplication or sampling',
                      'raw_concatenation_sha256': hashlib.sha256(raw).hexdigest(),
                      'source_metric_file_sha256': [hashlib.sha256(p.read_bytes()).hexdigest()
                                                    for p in sorted((unit/'raw').glob('metrics-*.jsonl'))]}
        (out/'provenance.json').write_text(json.dumps(provenance, indent=2)+'\n')
        (out/'workload').mkdir()
        for file in ('workload.json', 'index.json'):
            shutil.copyfile(unit/'workload'/file, out/'workload'/file)
    (destination/'report.json').write_text(json.dumps(report, indent=2, allow_nan=False)+'\n')
    hashes = {str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(destination.rglob('*')) if p.is_file()}
    (destination/'SHA256.json').write_text(json.dumps(hashes, indent=2)+'\n')
    if analyze(destination) != report:
        raise RuntimeError('Published projection changes recomputed report')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    publish(args.source, args.destination)
