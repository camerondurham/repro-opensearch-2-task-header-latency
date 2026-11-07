# `TASK_RESOURCE_USAGE` response headers in update-by-query scrolls

In stock OpenSearch 2.19.0, a 128-document update-by-query returned 8 `TASK_RESOURCE_USAGE` header lines with scroll size 16 and 128 lines with scroll size 1. `TASK_RESOURCE_USAGE` is JSON task metadata containing task IDs and CPU-time and memory-usage values; it is not a document count or total resource accounting ([producer source](https://github.com/opensearch-project/OpenSearch/blob/fd9a9d90df25bea1af2c6a85039692e815b894f5/server/src/main/java/org/opensearch/tasks/TaskResourceTrackingService.java#L283-L325)). In the tested path, smaller scroll batches returned more entries, and an instrumented diagnostic observed earlier distinct entries carried into later scroll continuations. The stock counts and diagnostic are separate evidence. The stock requests and responses are retained in [`results/stock/`](results/stock/).

## Reproduce with OpenSearch Benchmark

**Canonical live experiment:** pinned **OSB 2.5.0**, not an ad hoc runner wrapped
in OSB. Start with the [evidence report](results/osb/REPORT.md),
[predeclared protocol](osb/PROTOCOL.md), and [operator guide](osb/README.md).
The new campaign supports a bounded performance effect; it is separate from the
historical stock counts and common-paced campaign below.

```sh
git clone https://github.com/camerondurham/repro-opensearch-2-task-header-latency.git
cd repro-opensearch-2-task-header-latency
# Before this draft merges, check out its feature branch:
git switch feat/osb-controlled-reproduction
./osb/run.sh setup           # hash-locked Python 3.11.16 / OSB dependencies
./osb/run.sh build           # pinned stock + matched baseline/cleanup class builds
./osb/run.sh test
./osb/run.sh smoke           # untouched stock + both builds; correctness/client checks
./osb/run.sh parallel-smoke  # three-node >30s lifecycle qualification
./osb/run.sh campaign        # four AB/BA/AB/BA pairs + bracketing search-only controls
python3 osb/verify.py        # published evidence only; no network, Docker or pip
```

Requires Linux amd64, Docker's default local Unix socket, Python, `patch`, `uv`,
at least 8 logical CPUs, 12 GiB available RAM and 8 GiB free disk; use an otherwise
idle host. Limits, owned resources, cleanup, and maximum durations are explicit in
the operator guide. The tested environment was NixOS under WSL2. No production
workload or private archive is needed. `./run-reproduction.sh` is now a small
compatibility entry point to OSB smoke; it accepts `parallel-smoke` or `campaign`.

The byte-identical historical [`reproduce.py`](reproduce.py) is retained for
provenance and offline verification, not as the canonical live procedure:

```sh
python3 reproduce.py --verify results/stock
python3 results/performance/recompute_late_p99.py
```

These checks recompute retained observations; they are not independent replication
or signed attestations.

## Stock result

| Request | Header lines | Updated documents | Batches | Revision sum after request |
|---|---:|---:|---:|---:|
| Ordinary search | 0 | — | — | 0 (initial) |
| Update-by-query, scroll 16 | 8 | 128 | 8 | 128 |
| Update-by-query, scroll 1 | 128 | 128 | 128 | 256 |
| Same update-by-query, scroll 1 | 128 | 128 | 128 | 384 |

Each update-by-query completed 128 updates without reported failures. These are literal response-header occurrences at request completion. They do not measure header bytes, latency, task resource totals, or growth across distinct update-by-query requests. The raw evidence and its projection notes are in [`results/stock/REPORT.md`](results/stock/REPORT.md) and [`results/stock/evidence-manifest.json`](results/stock/evidence-manifest.json).

## Why scroll continuations can carry earlier entries

The producer updates the current response-header key in [`TaskResourceTrackingService`](https://github.com/opensearch-project/OpenSearch/blob/fd9a9d90df25bea1af2c6a85039692e815b894f5/server/src/main/java/org/opensearch/tasks/TaskResourceTrackingService.java#L283-L325); it does not blindly append every measurement. A saved [`ThreadContext`](https://github.com/opensearch-project/OpenSearch/blob/fd9a9d90df25bea1af2c6a85039692e815b894f5/server/src/main/java/org/opensearch/common/util/concurrent/ThreadContext.java#L322-L365) preserves response headers, and restoration merges saved and incoming values, retaining distinct strings ([merge implementation](https://github.com/opensearch-project/OpenSearch/blob/fd9a9d90df25bea1af2c6a85039692e815b894f5/server/src/main/java/org/opensearch/common/util/concurrent/ThreadContext.java#L790-L808)). In the inspected source, transport captures a restorable response context ([`TransportService`](https://github.com/opensearch-project/OpenSearch/blob/fd9a9d90df25bea1af2c6a85039692e815b894f5/server/src/main/java/org/opensearch/transport/TransportService.java#L978-L979)); update-by-query requests further scrolls after each asynchronous response ([`ScrollableHitSource`](https://github.com/opensearch-project/OpenSearch/blob/fd9a9d90df25bea1af2c6a85039692e815b894f5/server/src/main/java/org/opensearch/index/reindex/ScrollableHitSource.java#L120-L135)). Ordinary search has request-end/failure cleanup through [`SearchTaskRequestOperationsListener`](https://github.com/opensearch-project/OpenSearch/blob/fd9a9d90df25bea1af2c6a85039692e815b894f5/server/src/main/java/org/opensearch/action/search/SearchTaskRequestOperationsListener.java#L27-L37); the inspected [`SearchScrollAsyncAction`](https://github.com/opensearch-project/OpenSearch/blob/fd9a9d90df25bea1af2c6a85039692e815b894f5/server/src/main/java/org/opensearch/action/search/SearchScrollAsyncAction.java#L288-L309) has no equivalent cleanup.

A separate instrumented diagnostic sampled the response context at `doStartNextScroll`, immediately before `client.searchScroll`. Across 128 successive next-scroll observations, the baseline held 0 through 127 distinct values; the cleanup candidate held zero. The increasing counts are consistent with earlier distinct entries being carried forward, as the source mechanism predicts. This supports the explanation in the tested path; the hook does not directly inspect every continuation capture or transport hop. The figure combines this diagnostic with the stock counts and source-supported lifecycle path:

![Stock header counts, instrumented next-scroll observations, and proposed cleanup boundary](figures/accumulation.svg)

Carrying more distinct header values can increase context copying, serialization, allocation, and transport work, which may affect concurrent latency. No profiler or isolated wire-byte attribution measured that mediation.

## New native OSB results (2026-11-07)

The stock and rebuilt-baseline smoke each returned **0/8/128/128** resource
header lines; the cleanup returned **0/0/0/0**. Stock/baseline exceeded the pinned
aiohttp 3.14.5 client's default 128-total-header limit; cleanup was accepted.
Every smoke finished with 128 documents and revision sum 512, including the fourth
request whose stock response the default client could not parse. That parser's
diagnostic 400 is **not a server HTTP 400**.

Four fresh-cluster, three-node matched pairs at 50 offered searches/s produced
late-progress p99 **service-time** reductions of **56.3%, 53.3%, 39.2%, 21.5%**.
The geometric-mean candidate/baseline ratio was **0.559**, with a run-level 95%
t interval **[0.365, 0.856]**; mean absolute reduction **6.41 ms**. The predeclared
20%/2 ms endpoint and validation gates passed. All eight UBQs verified 12,000
updates each with zero reported failures/retries/timeouts. The candidate achieved
about 4% more UBQ throughput despite the same native throttle setting; this is
within the declared 10% matching tolerance, not perfectly identical pacing.

![Entire observed native OSB search timeline, including warmup and recovery](figures/osb-full-duration.svg)

These are actual full-duration observations, not invented interpolation. Each
point is a descriptive 5-second-bin p99, **not** a replicate or the primary
late-window p99. The primary endpoint is recomputed from individual requests.
There is no established monotonic latency ramp in these plots. Baseline late p99
declined across pairs; one host, four pairs and approximate t-interval assumptions
limit generalization. See [every run, source/image pins, raw files, exclusions,
and uncertainty](results/osb/REPORT.md).

## Proposed cleanup and historical performance evidence

[`proposed-fix.patch`](proposed-fix.patch) removes only `TASK_RESOURCE_USAGE` before terminal listener delegation. This lets downstream code run after cleanup and capture the next continuation without the observed history. It does not change the producer, generic `ThreadContext`, worker pacing, or other response headers. It remains a proposal; external consumers, successful distributed phases, and cancellation paths that bypass the terminal callback need maintainer review.

The historical stock reproduction, instrumented diagnostic, and historical performance campaign are separate evidence, and are distinct from the native OSB campaign above. The historical campaign compared stock scroll transport with the cleanup candidate while both used the same experimental worker-pacing control. It was one common-paced A1/B1/B2/A2 ABBA sequence, not a stock latency baseline or independent replication.

![Late-progress p99 for one common-paced ABBA campaign](figures/late-search-latency.svg)

The historical figure and retained projection cover **late windows only**, not a full-duration latency ramp. Late-progress p99 fell 69.5% in A1/B1 and at least 61.1% in A2/B2. Recompute the saved late-window values offline with `python3 results/performance/recompute_late_p99.py`; see [`results/performance/`](results/performance/) for data and method. This is conditional intervention evidence from one campaign, not a population estimate, confidence interval, proof that cleanup is the dominant cause, or a production-wide speedup. The historical unpaced experiment remains inconclusive. Equal updated-document totals establish document work, not resource-accounting invariance or universal plugin/cancellation safety.

## Source pin, license, and support

Source inspection uses OpenSearch commit [`fd9a9d90df25bea1af2c6a85039692e815b894f5`](https://github.com/opensearch-project/OpenSearch/tree/fd9a9d90df25bea1af2c6a85039692e815b894f5), including ordinary-search changes [#16532](https://github.com/opensearch-project/OpenSearch/pull/16532) and [#16536](https://github.com/opensearch-project/OpenSearch/pull/16536). See [`SUPPORT.md`](SUPPORT.md) for maintainer questions about the header contract and target branch. This extraction is provided under [Apache License 2.0](LICENSE.txt); see [`NOTICE.txt`](NOTICE.txt).

AI disclosure: implementation, operation, review, analysis and drafting were
assisted by GPT-6 Astra with xhigh reasoning, initially through Codex and then
through the user-authorized Pi `openai/gpt-6-astra` route. Builds and measurements
ran on the same NixOS environment, not a Mac or substituted machine.
