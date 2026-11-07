#!/usr/bin/env python3
"""Full retained search timeline; descriptive non-overlapping bins, not replicates."""
import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
from analyze import analyze, rows, verify_hashes, need, p99

ROOT = Path(__file__).resolve().parents[1]


def series(root):
    report = analyze(root)
    result = {}
    for name in report['runs']:
        unit = root/name
        events = rows(unit/'raw/events.jsonl')
        start = next(e['time'] for e in events if e['kind'] in ('ubq-start', 'control-start'))
        bins = defaultdict(list)
        for row in rows(unit/'raw/metrics.jsonl.gz'):
            if row['operation'] == 'search' and row['name'] == 'service_time':
                # Keep phases separate; don't bridge OSB join-point gaps or
                # mislabel warmup samples as measurements. Every search is used.
                key = (row['task'], row['sample-type'], math.floor((row['@timestamp']/1000-start)/5))
                bins[key].append(row)
        result[name] = [dict(task=task, sample_type=sample, bin_start=5*index,
            first=min(r['@timestamp']/1000-start for r in values),
            last=max(r['@timestamp']/1000-start for r in values), count=len(values),
            service_p99_ms=p99([r['value'] for r in values]))
            for (task, sample, index), values in sorted(bins.items(), key=lambda item: (item[0][2], item[0][:2]))]
    return result


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render(root, destination):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    need(matplotlib.__version__ == '3.10.8', 'Install plot-requirements.lock')
    verify_hashes(root)
    data, report = series(root), analyze(root)
    plt.rcParams.update({'svg.hashsalt': 'osb-task-header-full-duration-v1', 'font.size': 10})
    fig, axes = plt.subplots(5, 1, figsize=(12, 12), sharex=True, sharey=True, layout='constrained')
    colors = {'baseline': '#b04435', 'candidate': '#087d88'}
    for i, ax in enumerate(axes):
        names = [f'pair{i+1}-baseline', f'pair{i+1}-candidate'] if i < 4 else ['control-before', 'control-after']
        for j, name in enumerate(names):
            run = report['runs'][name]
            color = colors[run['variant']] if i < 4 else ['#58478f', '#779143'][j]
            for phase, marker in [('warmup-search', '^'), ('concurrent-search', 'o'), ('recovery-search', 's')]:
                points = [p for p in data[name] if p['task'] == phase]
                ax.scatter([(p['first']+p['last'])/2 for p in points], [p['service_p99_ms'] for p in points],
                           c=color, marker=marker, s=23, alpha=.85,
                           label=name + f' ({run["duration_seconds"]:.1f}s)' if phase == 'concurrent-search' else None)
            ax.axvspan(run['late_start'], run['late_end'], color=color, alpha=.055)
            ax.axvline(run['duration_seconds'], color=color, alpha=.35, linestyle=':')
        ax.axvline(0, color='#666666', linewidth=.8)
        ax.set_yscale('log')
        ax.grid(axis='y', which='both', alpha=.16)
        ax.set_ylabel('5s-bin p99 (ms)\nlog scale')
        ax.set_title(f'Pair {i+1} — ' + ('baseline then cleanup' if i % 2 == 0 else 'cleanup then baseline')
                     if i < 4 else 'Search-only controls — before and after all pairs', loc='left', fontsize=11)
        ax.legend(loc='upper left', fontsize=9, ncol=2)
    axes[-1].set_xlabel('Observed time relative to load start (seconds); each unit has its own clock origin')
    fig.suptitle('Native OSB: entire retained search timeline, not an interpolated ramp\n'
                 'Triangles: warmup phase · Circles: parallel phase · Squares: recovery\n'
                 'Points are nearest-rank p99 in non-overlapping 5s bins; partial bins retained. '
                 'Shading: predeclared late windows.', fontsize=12)
    destination.mkdir(parents=True, exist_ok=True)
    for suffix in ('svg', 'png'):
        fig.savefig(destination/('osb-full-duration.'+suffix), dpi=140,
                    metadata={'Date': None} if suffix == 'svg' else {})
    plt.close(fig)
    (destination/'osb-full-duration.json').write_text(json.dumps(data, indent=2, allow_nan=False)+'\n')
    provenance = dict(plot_script_sha256=sha(Path(__file__)), campaign_manifest_sha256=sha(root/'SHA256.json'),
        matplotlib=matplotlib.__version__, bin_seconds=5,
        files={f'osb-full-duration.{suffix}': sha(destination/f'osb-full-duration.{suffix}')
               for suffix in ('svg', 'png', 'json')})
    (destination/'osb-full-duration-provenance.json').write_text(json.dumps(provenance, indent=2)+'\n')


def verify(root, destination):
    meta = json.loads((destination/'osb-full-duration-provenance.json').read_text())
    need(meta['plot_script_sha256'] == sha(Path(__file__)) and
         meta['campaign_manifest_sha256'] == sha(root/'SHA256.json'), 'Plot source provenance mismatch')
    for name, digest in meta['files'].items():
        need(name in {f'osb-full-duration.{s}' for s in ('svg', 'png', 'json')} and sha(destination/name) == digest,
             'Plot artifact mismatch')
    need(series(root) == json.loads((destination/'osb-full-duration.json').read_text()), 'Plot observations differ')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    (verify if args.verify else render)(ROOT/'results/osb/campaign', ROOT/'figures')
