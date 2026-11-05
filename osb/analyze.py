#!/usr/bin/env python3
"""Recompute run-level evidence from retained OSB observations (stdlib only)."""
import argparse
import hashlib
import gzip
import json
import math
from pathlib import Path
import statistics


def need(ok, message):
    if not ok:
        raise ValueError(message)


def rows(path):
    text = gzip.decompress(path.read_bytes()).decode() if path.suffix == '.gz' else path.read_text()
    return [json.loads(line) for line in text.splitlines()]


def p99(values):
    need(bool(values) and all(math.isfinite(v) and v >= 0 for v in values), 'Invalid/absent samples')
    return sorted(values)[math.ceil(.99 * len(values)) - 1]


def paired_interval(effects):
    need(len(effects) == 4, 'Exactly four predeclared pairs required')
    mean = statistics.mean(effects)
    width = 3.182446305 * statistics.stdev(effects) / math.sqrt(len(effects))
    return [math.exp(mean - width), math.exp(mean + width)]


def analyze_unit(unit):
    receipt = json.loads((unit / 'receipt.json').read_text())
    need(receipt['execution_ok'] and receipt['clean'] and not receipt['errors'] and receipt['osb_exitcode'] == 0,
         'Incomplete or unclean run: ' + unit.name)
    need(receipt['osb_version'] == '2.5.0', 'Unexpected OSB version')
    events = rows(unit / 'raw/events.jsonl')
    correctness = [e for e in events if e['kind'] == 'correctness']
    docs = receipt['documents']
    expected = 512 if receipt['smoke'] else 0 if receipt['control'] else docs
    need([e['revision_sum'] for e in correctness] == [0, expected] and
         all(e['documents'] == docs for e in correctness), 'Incorrect final corpus/revision total')
    metrics = [r for p in sorted((unit / 'raw').glob('metrics*.jsonl*')) for r in rows(p)]
    need(bool(metrics), 'Missing OSB metrics')
    # Keep every observation, including coincident equal values; never deduplicate samples.
    need(all(math.isfinite(r['value']) and r['value'] >= 0 for r in metrics), 'Nonfinite/negative metric')
    errors = sum(r.get('meta', {}).get('success') is False for r in metrics if r['name'] == 'service_time')
    timeouts = sum(r.get('meta', {}).get('timed_out') is True for r in metrics if r['name'] == 'service_time')
    search_rows = [r for r in metrics if r.get('operation') == 'search' and r['name'] == 'service_time']
    need(search_rows and all(r['meta'].get('hits') == docs and r['meta'].get('hits_relation') == 'eq'
                            for r in search_rows), 'Missing/incorrect search hit accounting')
    summary = dict(variant=receipt['variant'], smoke=receipt['smoke'], control=receipt['control'],
                   documents=docs, revision_sum=expected, errors=errors, timeouts=timeouts)
    if receipt['smoke']:
        h = [e for e in events if e['kind'] == 'headers']
        counts = [0, 0, 0, 0] if receipt['variant'] == 'candidate' else [0, 8, 128, 128]
        need([e['count'] for e in h] == counts and all(e['status'] == 200 for e in h), 'Header reproduction mismatch')
        need([len(e['resource_headers']) for e in h] == counts, 'Raw header count mismatch')
        need(all(e['opaque'] == 'osb-synthetic-' + e['case'] for e in h), 'Unrelated header lost')
        default = [e for e in events if e['kind'] == 'default-client']
        expected_outcome = 'accepted' if receipt['variant'] == 'candidate' else 'client-parser-rejection'
        need(len(default) == 1 and default[0]['outcome'] == expected_outcome, 'Default-client comparison missing')
        if expected_outcome == 'client-parser-rejection':
            need(default[0]['server_status'] is None and default[0]['message'] == 'Too many headers received',
                 'Client parser failure mislabeled as server response')
        for kind, status, opaque in [('scroll-failure', 400, 'osb-failure'), ('expired-scroll', 404, 'osb-expired')]:
            found = [e for e in events if e['kind'] == kind]
            need(len(found) == 1 and found[0]['status'] == status and found[0]['opaque'] == opaque
                 and not found[0]['headers'], 'Scroll failure contract')
        need(errors == timeouts == 0, 'Smoke request errors')
        return dict(summary, header_counts=counts, default_client=expected_outcome)
    prefix = 'control' if receipt['control'] else 'ubq'
    starts, ends = ([e for e in events if e['kind'] == prefix + '-' + s] for s in ('start', 'end'))
    need(len(starts) == len(ends) == 1, 'Missing/duplicate load boundaries')
    start, end = starts[0]['time'], ends[0]['time']
    duration = end - start
    need(0 < duration <= 241, 'Invalid load duration')
    summary['duration_seconds'] = duration
    if receipt['control']:
        lower, upper = start + .75 * duration, start + .95 * duration
    else:
        body = ends[0]['response']
        need(body.get('failures') == [] and body.get('timed_out') is False and
             body.get('total') == body.get('updated') == docs and
             all(body.get(k) == 0 for k in ('deleted', 'noops', 'version_conflicts')) and
             body.get('retries') == dict(bulk=0, search=0), 'Invalid UBQ terminal accounting')
        progress = [e for e in events if e['kind'] == 'progress']
        need(progress and all(a['updated'] <= b['updated'] and a['time'] < b['request_start']
                              for a, b in zip(progress, progress[1:])), 'Nonmonotonic progress')
        need(progress[-1]['completed'] and progress[-1]['updated'] == docs, 'Missing terminal progress')
        lower = next(e['time'] for e in progress if e['updated'] >= .75 * docs)
        upper = max(e['request_start'] for e in progress if e['updated'] <= .95 * docs)
        summary.update(ubq_rate=docs/duration, batches=body['batches'])
        if receipt.get('quick'):
            need(errors == timeouts == 0, 'Parallel smoke request errors')
            need(any(r.get('task') == 'concurrent-search' for r in metrics), 'Missing parallel OSB search')
            return dict(summary, quick=True)
    need(lower < upper, 'Empty progress window')
    groups = {}
    for row in metrics:
        if row.get('task') != 'concurrent-search' or row.get('sample-type') != 'normal' or row['name'] == 'throughput':
            continue
        key = (row['@timestamp'], row['relative-time-ms'])
        # Original processing timestamp is high-resolution relative time, even if epoch is rounded.
        group = groups.setdefault(key, {})
        need(row['name'] not in group, 'Duplicate metric key: capture must not double-count requests')
        group[row['name']] = row
    need(groups and all(set(g) == {'service_time', 'latency', 'processing_time'} for g in groups.values()),
         'Missing per-request metric counterpart')
    late, excluded, full = [], 0, []
    for (timestamp, _), group in sorted(groups.items()):
        t = timestamp / 1000
        finish = t + group['processing_time']['value']/1000 + .001
        if start <= t <= end:
            full.append(group)
        if t - .001 >= lower and finish <= upper:
            late.append(group)
        elif lower - group['processing_time']['value']/1000 <= t <= upper + .001:
            excluded += 1
    need(late and full, 'Missing late/full observations')
    offered = receipt['config']['search_rate']
    achieved = len(full)/duration
    host = json.loads((unit / 'host.json').read_text())
    need(len(host) >= duration and all(math.isfinite(s['cpu_percent']) for s in host), 'Missing host sampling')
    contaminated = sum(s['cpu_percent'] > 100*8/receipt['preflight']['logical_cpus'] for s in host) > .1*len(host)
    swapped = host[-1]['swap_in'] != host[0]['swap_in'] or host[-1]['swap_out'] != host[0]['swap_out']
    failures = []
    if errors or timeouts: failures.append('request errors/timeouts')
    if len(late) < 500: failures.append('fewer than 500 late samples')
    if abs(len(full)-offered*duration) > .05*offered*duration + offered: failures.append('offered/achieved search load mismatch')
    if contaminated: failures.append('host CPU contention')
    if swapped: failures.append('host swapping')
    if any(s.get('unrelated_containers', 0) for s in host): failures.append('unrelated container started')
    return dict(summary, offered_search_rate=offered, achieved_search_rate=achieved,
                search_samples=len(full), late_samples=len(late), boundary_exclusions=excluded,
                late_start=lower-start, late_end=upper-start,
                late_service_p99_ms=p99([g['service_time']['value'] for g in late]),
                late_latency_p99_ms=p99([g['latency']['value'] for g in late]),
                full_service_p99_ms=p99([g['service_time']['value'] for g in full]),
                gates=failures, missing_metric_counterparts=0,
                nominal_search_opportunities=offered*duration,
                observed_minus_nominal=len(full)-offered*duration)


def analyze(root):
    order = json.loads((root / 'order.json').read_text())
    units = {name: analyze_unit(root / name) for name, *_ in order}
    result = {'runs': units}
    if all(u['smoke'] or u.get('quick') for u in units.values()):
        return result
    pairs = []
    for pair in range(1, 5):
        a, b = (units[f'pair{pair}-{v}'] for v in ('baseline', 'candidate'))
        ratio = b['late_service_p99_ms'] / a['late_service_p99_ms']
        failures = a['gates'] + b['gates']
        rate_ratio = b['ubq_rate']/a['ubq_rate']
        if not .9 <= rate_ratio <= 1.1:
            failures.append('UBQ achieved-rate mismatch >10%')
        pairs.append(dict(pair=pair, candidate_over_baseline=ratio, reduction_percent=100*(1-ratio),
                          difference_ms=a['late_service_p99_ms']-b['late_service_p99_ms'],
                          ubq_rate_ratio=rate_ratio, gates=failures))
    interval = paired_interval([math.log(p['candidate_over_baseline']) for p in pairs])
    ratio = math.exp(statistics.mean(math.log(p['candidate_over_baseline']) for p in pairs))
    delta = statistics.mean(p['difference_ms'] for p in pairs)
    qualified = all(not p['gates'] for p in pairs) and all(not u['gates'] for u in units.values())
    result.update(pairs=pairs, geometric_mean_ratio=ratio, ratio_95pct_t_interval=interval,
                  mean_difference_ms=delta, performance_gates_pass=qualified,
                  practically_meaningful_effect_supported=qualified and ratio <= .8 and delta >= 2 and interval[1] < 1,
                  inference='Four fresh-cluster pairs on one host; t interval assumes independent approximately normal log effects.')
    return result


def verify_hashes(root):
    manifest = root / 'SHA256.json'
    if manifest.exists():
        for name, expected in json.loads(manifest.read_text()).items():
            need(hashlib.sha256((root/name).read_bytes()).hexdigest() == expected, 'Evidence checksum mismatch: ' + name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    verify_hashes(args.directory)
    print(json.dumps(analyze(args.directory), indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
