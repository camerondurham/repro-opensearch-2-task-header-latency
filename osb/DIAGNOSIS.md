# Premature OSB completion: harness diagnosis, not engine evidence

The excluded 2026-11-05 attempt at `dadfd730` has a control-start with no
control-end, and a UBQ-start plus 60 polls with no UBQ-end. The last poll says
3,340/12,000 updated. Baseline OSB exited 64 at final revision accounting;
control OSB exited zero despite a failed load operation. Both resource cleanups
completed. Retained raw evidence: `results/osb/excluded-timeout/`.

Ranked hypotheses were: whole-runner timeout, native completed-by scheduling,
or engine task failure. The pinned [OSB AsyncExecutor source](https://github.com/opensearch-project/opensearch-benchmark/blob/445eb5a3188c1e3d3a3ca6bfe87757902ce5b10e/osbenchmark/worker_coordinator/worker_coordinator.py#L2400-L2440)
wraps `execute_single` in `asyncio.wait_for`: operation `request-timeout` overrides
the client timeout. The runner comprises *multiple* short HTTP requests plus
poll sleeps, so the 30s HTTP setting was inadvertently a 30s lifecycle setting.
Its timeout handler marks `success=false,error-type=timeout` without rethrowing
through the `on-error=abort` path. A one-iteration completed-by operation then
finishes; native parallel completion correctly tells other workers to finish.
The UBQ runner's cancellation handler cancels its own task. The later revision
failure therefore does not show the engine losing acknowledged updates.

The actual native-executor regression (real registered custom runner and native
request context, only time scaled for fast tests) failed before the fix:

```text
./osb/run.sh test
Client 0 request timed out after 0.03 s
FAIL: test_native_executor_completes_control_beyond_client_timeout
{'success': False, 'error-type': 'timeout'} != {'success': True}
```

Fix: keep transport timeout 30s, explicitly allow control duration+10s and UBQ
245s at the operation layer (UBQ's own deadline remains 240s). Live acceptance
now requires two correctness checks, complete ordered boundary events, correct
control duration and successful native metric records—not process exit alone.
The regression passed; negative tests still reject a missing end, truncated
control, partial update totals, task errors and executor timeout metadata.

Real native OSB boundary qualification crossed the original 30s boundary:
control 40.039s; baseline 43.174s/4,800 updates; cleanup 42.182s/4,800 updates.
All ended correctly and cleaned owned resources. The later full campaign has
both 100s controls and all eight complete 12,000-update UBQs. No engine code,
workload throughput, hypothesis, endpoint or practical-effect threshold was
changed to obtain these passes. The separate OSB progress-display float bug is
avoided by its official `--quiet` option; OSB scheduling code is unmodified.
