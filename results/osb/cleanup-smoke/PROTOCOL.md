# Protocol frozen before live measurements

This campaign tests OpenSearch **2.19.0**, source
`fd9a9d90df25bea1af2c6a85039692e815b894f5`, against the existing proposed
scroll cleanup. It does not test issue #23162 (keyword sorting/max clauses),
or claim that every current OpenSearch 2 release has the same behavior.

Three questions are separate:

1. **Lifecycle:** untouched stock repeats the published 128-document result:
   search=0 headers, UBQ batch 16=8, batch 1=128, repeated batch 1=128.
   Cleanup should remove those scroll resource headers while preserving exact
   document/revision accounting and unrelated `X-Opaque-Id`. A mismatch falsifies
   the expected reproduction. Counts alone do not establish performance harm.
   After a stock integration attempt exposed aiohttp 3.14.5's default 128-header
   cap, the inspection reader uses a bounded 1,024-header cap. A separate fourth
   UBQ retains client defaults: stock/baseline should trigger the client parser's
   `Too many headers received`, cleanup should be accepted. The exception's
   diagnostic status 400 is **not a server HTTP 400**. Final revision sum is 512,
   including the request whose response the client could not parse. This is
   compatibility evidence, not a latency endpoint.
2. **Performance:** four fresh-cluster baseline/candidate pairs, alternating AB,
   BA, AB, BA, test whether cleanup reduces concurrent-search late-progress p99
   service time by at least **20% and 2 ms**. Late means 75–95% acknowledged UBQ
   progress; p99 uses nearest rank. Report each run, paired effects, and a 95%
   Student t interval over the four paired log ratios (df=3), not over requests.
   A practical-effect claim requires both thresholds and an interval excluding
   no improvement. With four pairs, inference remains weak and host-specific.
   Failure to meet these criteria is inconclusive/unsupported, not proof of no cost.
3. **Safety:** successful UBQ accounting, terminal callback success/failure,
   cancellation delivered to that callback, context capture, unrelated headers,
   and live malformed/expired scroll failure. Passing is bounded safety coverage,
   not plugin compatibility, resource accounting equivalence, or cancellation
   paths that bypass the callback.

Timing uses three nodes on one NixOS/WSL2 machine, three primary shards, no
replicas, 12,000 deterministic documents, batch size 10, one UBQ/slice, native
`requests_per_second=120`, tracking enabled, 2 search clients at **50 total
requests/s**, 20 seconds search warmup plus one baseline measurement second
(OSB requires a positive measurement period), a bounded 240-second UBQ window, and
10 seconds recovery. Each node gets 2 CPUs, 3 GiB memory/no extra swap, 1 GiB
heap and 512 MiB ephemeral data. Nodes use CPU sets 0–1, 2–3 and 4–5;
the OSB driver uses 6–7 and an 8 GiB per-process virtual-address bound.
The corpus includes explicit `_id` values `0..11999` so shard placement is
deterministic across fresh clusters. An initial control using automatic IDs was
stopped before paired timing and excluded; the document-ID correction changes
the fixture implementation, not the endpoints or effect thresholds.
No worker pacing modification. Two baseline
search-only controls bracket the timing pairs for 100 seconds each. A stock
smoke and matched-build smoke precede timing and are not performance replicates.

A second setup attempt (`dadfd730`, 2026-11-05) is excluded: OSB 2.5.0's
executor wrapped the entire custom runner in the client's 30-second timeout.
Its timeout handler recorded failure but did not honor `--on-error=abort` there;
the truncated control exited zero and the cancelled UBQ failed final accounting.
Explicit operation-level `request-timeout` now bounds the control at duration+10s
and UBQ at 245s (its own deadline remains 240s); HTTP transport timeout stays 30s.
A native-executor regression failed before this correction and passes after it.
Before timing, a three-node boundary smoke requires a complete 40s control and
4,800-document UBQs exceeding 35s. Live receipts now require complete lifecycle
and successful raw operation records, not merely process exit zero. Neither the
performance endpoints nor the practical-effect thresholds changed.

OSB owns bulk ingestion, search scheduling, warmup, parallel completion, and
measurement. A custom UBQ runner submits/polls the missing operation, enforces
terminal correctness, and records progress. A separate tiny header operation
uses aiohttp because OSB's client hides duplicate HTTP response headers. A narrow
metrics-store hook retains original OSB per-request latency/service-time records;
it does not schedule requests. Native OSB latency includes scheduler queue delay;
service time is client-observed request time, not server `took`.
Use OSB's official `--quiet` option: the 2.5.0 progress display attempts to index
a float at parallel completion (`update_progress_message`); quiet mode bypasses
that display path without patching OSB or changing scheduling/measurement.

Each fresh cluster uses disabled automatic refresh, explicit refresh after load,
one initial force merge, warmup and a final refresh for revision checks. Preserve
full-duration raw observations, warmup/recovery, GC/merge/refresh counters and
host CPU/memory samples. No fabricated intermediate observations. Record offered
and achieved search throughput, UBQ duration/progress/rate, errors and timeouts.
Reject performance attribution if pair UBQ rates differ by >10%, observed search
load differs by >5% from offered load (allow 1 second at boundaries), any request
errors/missing capture occurs, late windows have <500 observations, host swap
in/out occurs, unrelated containers run, or host CPU use exceeds the six engine
CPUs plus two driver CPUs for >10% of samples. Windows crossing uncertain
progress boundaries are excluded and counted, never imputed.

No concurrent builds during timing. Check resources before each run; abort
contaminated campaigns without touching unrelated jobs. Containers, network,
files and cleanup are restricted to this campaign's unique owner. OSB runs in
its own process group and home; cleanup follows errors/timeouts/signals. Publish
only allowlisted synthetic evidence, checksums and software/resource provenance;
keep raw tool logs and machine identities under ignored `.local/`.
