# Native OpenSearch Benchmark reproduction

The live experiment is OSB 2.5.0, pinned with hashes in `requirements.lock`.
It targets the published OpenSearch 2.19.0 source/image, not issue #23162 or an
unverified newer engine. Read [PROTOCOL.md](PROTOCOL.md) before interpreting it.

On Linux amd64, install Docker, Python, `patch`, and `uv`. Use a local Docker
daemon and an otherwise idle host with at least 8 logical CPUs, 12 GiB available
RAM and 8 GiB disk. The NixOS wrapper repairs its own inherited PATH only.

```sh
./osb/run.sh setup           # isolated Python 3.11.16; hash-locked dependencies
./osb/run.sh build           # stock + matched recompiled baseline/cleanup images
./osb/run.sh test
./osb/run.sh smoke           # stock, baseline, cleanup; tiny header/client checks
./osb/run.sh parallel-smoke  # three nodes: 40s control + 4,800-doc UBQ in both arms
./osb/run.sh campaign        # controls + four counterbalanced fresh-cluster pairs
python3 osb/verify.py        # all published evidence, offline; no Docker or pip
```

Each live command prints its unique `.local/osb/` output directory. A campaign
is bounded to ten units, each with 120 seconds readiness and at most 540 seconds
for OSB; typical runs are shorter. A failed run exits nonzero, records its receipt
and cleans its owned resources. Existing containers/jobs are never deleted or
stopped. The lock cooperates with the historical reproducer. No public ports:
engine HTTP ports bind only to loopback, with a unique private Docker bridge.
Data uses bounded ephemeral tmpfs; there are no persistent Docker data volumes.
If interrupted, inspect the receipt and exact owner labels before any retry.

The launcher captures executable inputs, the dependency lock, patch and protocol,
records the starting Git commit and source hashes, then executes a new interpreter
from that snapshot. Every unit verifies the same files and build manifest before
and after work. Editing checkout documentation or analysis does not alter a run.
The public campaign includes the executed snapshot, software inventory and hashes.
Build inputs (compiler script, Java sources and contract, patch, build script) are
retained separately and bound to the images. Snapshot cleanliness covers executed
inputs only; independent analysis/docs may change during measurement.
Hardware receipts read architecture, kernel, OS version and CPU model without a
hostname. Cleanup attempts all owned resources despite log/inspection failures;
failed ownership checks prohibit removal, and any cleanup error fails the run
while preserving its receipt. OSB's Thespian actors can detach with setsid/fork;
current cleanup separately verifies an inherited random owner marker, unit capture
path, Python executable, user and launch-time bound, then performs bounded
termination/rescans. Unknown or uninspectable live processes are never killed.
The real detached-actor regression also proves that a different owner survives.
A version-checked adapter selects OSB's process-private queue actor mode and
bypasses shared TCP actor discovery. There is no shared actor port to adopt or
shut down. Port probes were rejected in review because bootstrap can race another
service. Existing TCP-actor campaign evidence remains explicitly labelled; the
separate queue-actor campaign qualifies the final procedure.

The build uses the stock image's Temurin 21.0.6 compiler with `--release 11`,
compiles the pinned public `TransportSearchScrollAction.java` for both arms,
applies the existing `proposed-fix.patch` to the candidate, and substitutes only
that class in the stock server jar. It verifies all other jar payloads are equal.
The baseline is **recompiled**, not untouched stock; stock has its own smoke.
This is a targeted binary-compatible class build, not a full OpenSearch Gradle
distribution build or an execution of the whole upstream test suite. The included
Java contract executes the candidate helper against the stock libraries.

OSB's built-in bulk/search/refresh/merge operations and native parallel scheduler
own the workload. The three small custom operation areas are UBQ submission and
terminal accounting, duplicate-header/default-client observation, and final
document accounting. No old live runner is wrapped inside OSB. The observer
uses 1,024 maximum response headers solely to count the tiny fixture; a separate
request uses aiohttp 3.14.5 defaults (128 total headers). Stock's 128 resource
headers plus ordinary HTTP headers exceed that default. The resulting parser
exception's diagnostic 400 is not a server HTTP response status.

OSB 2.5.0 has a parallel progress-display failure (`float` not subscriptable).
The supported `--quiet` option avoids that display path. Installed OSB source is
unmodified; the explicit actor lifecycle adapter selects its existing queue mode
and bypasses shared TCP discovery. It does not patch the scheduler or HTTP timing.
`--enable-assertions` verifies exact search hits and timeout flags. The driver
disables transport retries, checks UBQ retries/failures, and rejects native
dropped-sample warnings. OSB's executor timeout path does not honor `on-error=abort`;
therefore successful raw operation records and complete lifecycle events are
mandatory before a live receipt can succeed. Custom lifecycle operations declare
an explicit whole-runner timeout, separate from the unchanged 30s HTTP timeout.
See the excluded setup attempt and the >30s boundary smoke.

`capture/sitecustomize.py` is a version-specific adapter for the native in-memory
metrics store: OSB's CSV report omits per-request observations. It saves an
allowlisted copy at creation, skips re-imported metrics during native store
transfer, and passes the original record through unchanged. It does not alter
queries, clocks, pacing, scheduling or samples. Latency includes time waiting for
the intended OSB schedule; service time is client-observed request time; `took`
is separately returned by OpenSearch. Timestamps are OSB processing-start epoch
milliseconds. Boundary checks use processing duration plus 1 ms rounding margin.

Raw events and metrics cover warmup, the entire parallel window and recovery.
OSB completed-by scheduling can leave an in-flight request at termination; the
report compares offered opportunities with observations, with an explicit
one-second boundary allowance. It does not claim exact accounting of every
unsent opportunity. A counterpart check rejects missing latency/service/processing
records; native log checks reject known sampling drops. No imputation or request
sample independence assumption is used.

The recorded results and all limits are in [the evidence report](../results/osb/REPORT.md).
`python3 osb/plot.py --verify` checks the full-duration plot without plotting dependencies;
regeneration instructions and the separate hash-locked plot environment are in the report.
The historical `reproduce.py` stays byte-identical for provenance/offline checks;
`run-reproduction.sh` now forwards live use to OSB rather than maintaining a second
public live shell procedure.

To recompute a private run: `python3 osb/analyze.py .local/osb/<run-directory>`.
To create an auditable public projection, use
`python3 osb/publish.py .local/osb/<run-directory> results/osb/<new-directory>`.
The exporter accepts only complete/clean runs, copies allowlisted files, losslessly
compresses metrics, hashes the files, and requires identical offline recomputation.
Private OSB/server logs and raw Docker inspection stay in ignored `.local/`.

Official references used for implementation:

- [Workload format](https://docs.opensearch.org/latest/benchmark/reference/workload-format/)
- [Summary metrics](https://docs.opensearch.org/latest/benchmark/reference/summary-report/)
- [OSB 2.5.0 source](https://github.com/opensearch-project/opensearch-benchmark/tree/445eb5a3188c1e3d3a3ca6bfe87757902ce5b10e)
- [Parallel loading/plugin API](https://github.com/opensearch-project/opensearch-benchmark/blob/445eb5a3188c1e3d3a3ca6bfe87757902ce5b10e/osbenchmark/workload/loader.py)
- [Native scheduling/timing](https://github.com/opensearch-project/opensearch-benchmark/blob/445eb5a3188c1e3d3a3ca6bfe87757902ce5b10e/osbenchmark/worker_coordinator/worker_coordinator.py)

Implementation, experiment operation, analysis and drafting were assisted by
GPT-6 Astra with xhigh reasoning, initially through Codex and then the
user-authorized Pi `openai/gpt-6-astra` route on the same NixOS environment. Measurements came from real local
OpenSearch/OSB processes; this disclosure does not make them independent evidence.
