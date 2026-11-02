# Figure data and reproduction

This directory contains compact chart inputs derived from the retained stock reproduction and the saved common-paced v2 experiment. The large source archives are not copied here. Source archive IDs, exact member paths, and member SHA-256 values are retained in `mechanism-data.json`, `latency-data.json`, and `late-search-source-members.json`.

## Figures

- [`../../figures/accumulation.svg`](../../figures/accumulation.svg) separates raw stock response-header line counts, a source-supported carry-forward mechanism and proposed cleanup, and the separate instrumented diagnostic.
- [`../../figures/late-search-latency.svg`](../../figures/late-search-latency.svg) compares late-window p99 between paired baseline and cleanup arms. Labels are rounded for display; `latency-data.json` retains the reported six-decimal p99/difference bounds and percent reduction values.

Each has a PNG export beside the SVG. The SVG uses editable text and has no creation-date metadata.

## Experiment scope

The stock-count evidence is from the retained minimized stock OpenSearch 2.19.0 reproduction on 2026-10-25. It used one node, one primary shard, 128 documents, tracking enabled, and the pinned 2.19.0 image. Its memory limit was 2 GiB; this is specific to the stock reproduction and does not describe the v2 latency campaign. The measured response-header counts were 0 for ordinary search, 8 for UBQ scroll size 16, and 128 for both first and repeated UBQ scroll size 1. Each UBQ updated 128 documents.

The p99 comparison uses one tracking-on ABBA sequence: A1 baseline, B1 cleanup candidate, B2 cleanup candidate, A2 baseline. It used three nodes, 30 primary shards, 2,000,000 fixture documents, one slice, batch size 100, 216,000 requested updates (2,160 batches), a 60-second baseline and common pacing target of 225 acknowledged updates/s, a maximum task allowance of 1,100 seconds, and 120 seconds recovery. Each arm completed 216,000 updates. The timing jars differed in the scroll cleanup payload and shared the same pacing payload. This is a common-paced experimental control, not an unmodified-stock baseline.

The late window is the acknowledged-progress band 150,000–180,000 updates. Reported values are nearest-rank p99. A1/B1 is 31.175575 ms versus 9.500166 ms (21.675409 ms lower; 69.5268941%). A2/B2 is 24.674248 ms versus 9.386961–9.593277 ms (15.080971–15.287287 ms lower; at least 61.1202862%). B2 had 2,500 known probes out of 2,501 nominal late opportunities; its one missing slot is retained as unknown and produces conservative zero/infinity rank-padded bounds. The B2 p99 range is not a confidence interval. B2 also had one boundary-crossing slot in the separate early window. Probe rows within an arm are not independent replicates; one ABBA sequence provides two order pairs, not a population estimate or independent replication.

The full saved v2 analysis and independent raw verification report the primary gates and limitations. This compact figure package recomputes the late p99 and arithmetic bounds only. It does not rerun the full campaign verdict, rate/exposure gates, terminal accounting, build/diagnostic receipts, or safety contracts.

## Sanitized projection and provenance

`late-search-progress.csv` retains only `arm`, `request_start`, `response_end`, and `updated` from each arm's task-progress record; task identifiers, other counters, and response metadata are omitted. The records let the recomputation recover the progress-band window bounds.

`late-search-probes.jsonl` contains only probes scheduled in the late window's nominal slot set. Retained fields are `arm`, `thread`, `slot`, `scheduled`, `started`, `finished`, `latency_ms`, and `success`; error text is omitted. Expected slots use the frozen configuration of two probe threads at 10 probes/s, with thread offsets 0 and 0.5 slots. The projection contains 2,501 rows for A1, B1, and A2, and 2,500 for B2. The absent B2 expected slot remains absent; the recomputation derives it as missing from the denominator rather than inserting a synthetic observation. There are no errors or boundary exclusions in these late windows. The separate early B2 boundary crossing is documented in the source analysis, not included in the late-window projection.

The original v2 source archive SHA-256 is `12c1459c95c653ab276165551d9e9c81e1a9091c2952dc7e4854b572f686b79f`. All 12 original per-arm progress/probe member names and byte hashes are in `late-search-source-members.json`. The original analysis member is `raw/experiments/scroll-lifecycle-v2/results/scroll-lifecycle-v2-20261021T061647Z-d4c6511c/completed-work-analysis.json`, SHA-256 `afcf89479ddc1b72faa1388e68eac9e3b0966f1598dbea591bc9d3185a2b7889`. The independent raw verification member and hash are listed in `latency-data.json`.

The stock archive is the retained minimized-stock evidence archive with SHA-256 `3a7478fbf718d447c09bec76b8e3e155b9843d5795203c90f6665b8c21a0ef98`. It is identified as `minimized-stock-20261025T053542Z/raw-evidence.tar.gz`; the four raw header member hashes are listed in `mechanism-data.json`. The proposed cleanup and source-mechanism records trace to `lifecycle.patch` and `SOURCE_FINDING.md` archive members in the v2 build archive; their hashes are also recorded in `mechanism-data.json`.

## Reproduce

The p99 recomputation uses only the Python standard library (validated with Python 3.12). Figure regeneration additionally requires Matplotlib 3.10.8 or compatible. With that plotting dependency installed, neither command needs network access, archive extraction, or a new workload. To install the optional plotting dependency in an isolated environment, run `python3 -m venv /tmp/opensearch-figure-venv` and `/tmp/opensearch-figure-venv/bin/pip install matplotlib==3.10.8`, then use that environment’s Python for plotting.

From the repository root:

```bash
python3 results/performance/recompute_late_p99.py
python3 results/performance/plot_figures.py
```

The p99 recomputation derives the progress-window boundaries from the staged progress rows, enumerates all nominal probe slots, classifies missing/unknown probes, and applies nearest-rank `ceil(0.99 × N) − 1` with zero/infinity padding. It checks the recomputed six-decimal values and reduction bounds against the recorded analysis values using explicit exceptions, including under `python -O`. Both normal and optimized runs were compared and matched; a temporary altered expected p99 was rejected with `ValueError` in both modes.

The plotted values and generated artifacts are traceable by SHA-256:

| File | SHA-256 |
|---|---|
| `mechanism-data.json` | `3fdd029aade5baa0bc73a922c0c90858773a3fd4429ef1efc36a8c4946b884c9` |
| `latency-data.json` | `c39ccd9ccea273f3e5542fc39fdf9bc378089d671eeb224ecb17324b47f73c96` |
| `late-search-progress.csv` | `18f421520295aa76b2db025bc6d3e8e8c4e038f516b7919ade3eb0f4a61093eb` |
| `late-search-probes.jsonl` | `ceceb7433c290187deac22f75ee33cf2e2ff6393dd00615d9e8100e470f1214e` |
| `late-search-source-members.json` | `e4003de713b024a1bcf03e5cb3c3bc4434716eded88241947ae5e791e69c859a` |
| `plot_figures.py` | `92c3cd5972cfd3e7ec8a20c258c80f0fde95cde1ec65168074bcda73328cb221` |
| `recompute_late_p99.py` | `0b0dfd5dcbc98afd68b1faf15db98b73e7957dc1e52a2a8ce6483c7a754c49fd` |
| `../../figures/accumulation.svg` | `93e74a256894e848cc344a5d09ddc7fb77604cef379aeed766a601d4984d3138` |
| `../../figures/accumulation.png` | `5caa4603ff0d5514182be1a76a643faf813cacae0f89c73453ed253aeefd097a` |
| `../../figures/late-search-latency.svg` | `dc9ed7de2a38c231cda508445ea5d03d06ebdbe75be5450d7722213a16b16be5` |
| `../../figures/late-search-latency.png` | `fb802cc841617f216128a9c8fced435a1b650cbe36ff07ac2e13d48b80554680` |
