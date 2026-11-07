#!/usr/bin/env python3
"""Standard-library offline evidence check; never starts a live workload."""
from pathlib import Path
import subprocess
import sys
from analyze import analyze, analyze_unit, inventory, load_json, need, verify_hashes, EXCLUDED_FILES
from publish import validate_metadata, publication_files


def verify_directory(path, *, legacy=False, expected_format=None):
    verify_hashes(path)
    report = analyze(path)
    if expected_format is not None:
        need(report['evidence_format'] == expected_format, 'Evidence format does not match declared role')
    validate_metadata(path, report)
    need(inventory(path) == publication_files(path, report), 'Unexpected published evidence inventory')
    saved = load_json(path/'report.json')
    if legacy:
        need(report['evidence_format'] in ('legacy-smoke', 'legacy-parallel-smoke'), 'Expected retained legacy evidence')
        # Do not rewrite historical reports or fabricate provenance. Their
        # original numeric results must still recompute exactly; the modern
        # analyzer adds only an explicit format/limitations envelope.
        additions = {'evidence_format', 'capture_limitation', 'provenance_limitation'}
        need({k: v for k, v in report.items() if k not in additions} == saved,
             'Recomputed legacy report differs: ' + path.name)
    else:
        need(not report['evidence_format'].startswith('legacy-'), 'New evidence cannot claim legacy exemption')
        need(report == saved, 'Recomputed report differs: ' + path.name)
    return report


def verify_excluded(path):
    verify_hashes(path)
    need(inventory(path) == EXCLUDED_FILES | {'SHA256.json'}, 'Unexpected excluded-timeout inventory')
    exclusion = load_json(path/'exclusion.json')
    need(exclusion['excluded'] is True and exclusion['start_commit'] == 'dadfd7302187308181dfd1c55645e3db02dd8891',
         'Timeout attempt must remain explicitly excluded')
    for name in ('control-before', 'pair1-baseline'):
        try:
            analyze_unit(path/name)
        except ValueError as exc:
            need(any(reason in str(exc) for reason in ('executor timeout', 'Truncated', 'Incomplete or unclean')),
                 'Excluded attempt failed for the wrong reason: ' + str(exc))
        else:
            raise ValueError('Excluded timeout was incorrectly accepted: ' + name)
    try:
        analyze(path)
    except (ValueError, FileNotFoundError):
        pass
    else:
        raise ValueError('Excluded timeout must not count as a campaign')


def verify_default_client_failure(path):
    verify_hashes(path)
    need(inventory(path) == {'SHA256.json', 'failure.json', 'receipt.json', 'events.jsonl',
                             'workload.json', 'workload.py'}, 'Unexpected original failure inventory')
    details = load_json(path/'failure.json')
    receipt = load_json(path/'receipt.json')
    need(details['benchmark_success'] is False and details['default_max_headers'] == 128 and
         receipt['execution_ok'] is False and receipt['osb_exitcode'] != 0,
         'Original client failure must remain a failure')


def main():
    root = Path(__file__).resolve().parents[1]
    formats = {'smoke': 'legacy-smoke', 'parallel-smoke': 'legacy-parallel-smoke',
               'boundary-smoke': 'boundary-smoke-pre-build-inputs', 'validated-smoke': 'smoke',
               'cleanup-smoke': 'smoke', 'campaign': 'campaign'}
    for directory, evidence_format in formats.items():
        report = verify_directory(root/'results/osb'/directory, legacy=directory in ('smoke', 'parallel-smoke'),
                                  expected_format=evidence_format)
        print('Verified OSB ' + directory + ' [' + report['evidence_format'] + ']')
    from plot import verify as verify_plot
    verify_plot(root/'results/osb/campaign', root/'figures')
    print('Verified full-duration plot observations and artifact provenance')
    verify_excluded(root/'results/osb/excluded-timeout')
    print('Verified timeout attempt remains rejected (not a campaign)')
    verify_default_client_failure(root/'results/osb/default-client-original-failure')
    print('Verified original default-client failure remains a failure')
    subprocess.run([sys.executable, 'reproduce.py', '--verify', 'results/stock'], cwd=root, check=True)
    subprocess.run([sys.executable, 'results/performance/recompute_late_p99.py'], cwd=root, check=True,
                   stdout=subprocess.DEVNULL)
    print('Verified historical stock and historical late-window evidence')


if __name__ == '__main__':
    main()
