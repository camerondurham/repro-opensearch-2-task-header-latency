#!/usr/bin/env python3
"""Recompute late progress-window p99s from staged, sanitized telemetry."""
from __future__ import annotations
import csv, json, math
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

HERE=Path(__file__).resolve().parent
PROGRESS_BAND=(150_000,180_000)
THREADS=2
PROBES_PER_THREAD_PER_SECOND=10

def expected_slots(start,end):
    rate=Fraction(PROBES_PER_THREAD_PER_SECOND)
    out=set()
    for thread in range(THREADS):
        offset=Fraction(thread,THREADS)
        first=max(0,math.ceil(Fraction(str(start))*rate-offset))
        stop=max(0,math.ceil(Fraction(str(end))*rate-offset))
        out.update((thread,slot) for slot in range(first,stop))
    return out

def nearest_rank(values,rank):
    return sorted(values)[rank]

def require(condition,message):
    if not condition:
        raise ValueError(message)

def recompute(arm,progress,probes):
    rows=progress[arm]
    lo_i=next((i for i,r in enumerate(rows) if r['updated']>=PROGRESS_BAND[0]),None)
    hi_i=next((i for i,r in enumerate(rows) if lo_i is not None and i>=lo_i and r['updated']>=PROGRESS_BAND[1]),None)
    if lo_i is None or hi_i is None or hi_i==lo_i:
        raise ValueError(f'{arm}: progress window is not bracketed')
    first,last=rows[lo_i],rows[hi_i-1]
    start,end=first['response_end'],last['request_start']
    slots=expected_slots(start,end)
    selected=[r for r in probes[arm] if (r['thread'],r['slot']) in slots]
    keys=[(r['thread'],r['slot']) for r in selected]
    if len(set(keys))!=len(keys): raise ValueError(f'{arm}: duplicate slot')
    bounded=[r for r in selected if start<=r['started']<end and r['finished']<=end]
    errors=sum(not r['success'] for r in selected)
    successes=[r['latency_ms'] for r in bounded if r['success']]
    missing=len(slots)-len(selected)
    boundary=len(selected)-len(bounded)
    unknown=len(slots)-len(successes)
    rank=math.ceil(.99*len(slots))-1
    lo=nearest_rank(successes+[0]*unknown,rank)
    upper_values=successes+[math.inf]*unknown
    hi=nearest_rank(upper_values,rank)
    return {'arm':arm,'progress_window_s':[start,end], 'progress_observations':[first['updated'],last['updated']],
            'nominal':len(slots),'attempts':len(selected),'missing':missing,'boundary_excluded':boundary,
            'errors':errors,'known':len(successes),'p99_lower_ms':lo,
            'p99_upper_ms':hi if math.isfinite(hi) else None}

progress=defaultdict(list)
with (HERE/'late-search-progress.csv').open() as f:
    for r in csv.DictReader(f):
        progress[r['arm']].append({'request_start':float(r['request_start']),
                                   'response_end':float(r['response_end']),
                                   'updated':int(r['updated'])})
probes=defaultdict(list)
with (HERE/'late-search-probes.jsonl').open() as f:
    for line in f:
        r=json.loads(line); probes[r['arm']].append(r)
results={arm:recompute(arm,progress,probes) for arm in ('A1','B1','B2','A2')}
expected=json.loads((HERE/'latency-data.json').read_text())['pairs']
for pair in expected:
    base=results[pair['baseline_arm']]
    cleanup=results[pair['cleanup_arm']]
    require(round(base['p99_lower_ms'],6)==pair['baseline_p99_ms']['lower'],
            f"{pair['pair']}: recomputed baseline p99 does not match the declared six-decimal value")
    require(round(cleanup['p99_lower_ms'],6)==pair['cleanup_p99_ms']['lower'],
            f"{pair['pair']}: recomputed cleanup lower p99 does not match the declared value")
    upper_matches=(cleanup['p99_upper_ms'] is None if pair['cleanup_p99_ms']['upper'] is None else
                   round(cleanup['p99_upper_ms'],6)==pair['cleanup_p99_ms']['upper'])
    require(upper_matches,f"{pair['pair']}: recomputed cleanup upper p99 does not match the declared value")
    reduction_lo=base['p99_lower_ms']-cleanup['p99_upper_ms']
    reduction_hi=base['p99_lower_ms']-cleanup['p99_lower_ms']
    require(round(reduction_lo,6)==pair['absolute_reduction_ms']['lower'],
            f"{pair['pair']}: recomputed lower reduction does not match the declared bound")
    require(round(reduction_hi,6)==pair['absolute_reduction_ms']['upper'],
            f"{pair['pair']}: recomputed upper reduction does not match the declared bound")
    percent_lower=reduction_lo/base['p99_lower_ms']*100
    require(round(percent_lower,7)==pair['reduction_percent']['lower'],
            f"{pair['pair']}: recomputed percentage lower bound does not match the declared value")
    results[pair['pair']]={'baseline_p99_ms':base['p99_lower_ms'],
                           'cleanup_p99_ms':[cleanup['p99_lower_ms'],cleanup['p99_upper_ms']],
                           'absolute_reduction_ms':[reduction_lo,reduction_hi],
                           'reduction_percent_lower_bound':percent_lower}
print(json.dumps({'recomputed_arms':results,'method':'nearest rank, ceil(0.99*N)-1; unknown observations padded with 0 for lower and infinity for upper'},indent=2))
