# Stock reproduction evidence

This directory contains the saved bounded run from 2026-10-25 of the canonical standalone reproducer against stock OpenSearch 2.19.0. The reproducer's repository revision was `f832e64c870034206a674acb51bab61808d2f522`; OpenSearch source review is pinned to `fd9a9d90df25bea1af2c6a85039692e815b894f5`. It is a symptom reproduction, not a performance benchmark. The run observed `TASK_RESOURCE_USAGE` response-header occurrence counts of 0 for ordinary search and 8 / 128 / 128 for update-by-query scroll sizes 16 / 1 / 1. Each update-by-query updated 128 documents; the revision sums were 0 initially and 128 / 256 / 384 afterward. The cleanup receipt records a clean removal.

Verify offline from the public tree with:

```sh
python3 reproduce.py --verify results/stock
```

The verifier reads the retained raw HTTP requests, responses, image/container geometry, launch and cleanup ownership records, a command lifecycle projection, and the saved container log. It returned `verified: true` with counts 0 / 8 / 128 / 128. This is the reproducer's shared validator, not an independent attestation. `evidence-manifest.json` records published file hashes and the source sealed archive hash.

The basis is the reviewed October 25 sealed qualification archive (`74,407` bytes; SHA-256 `3a7478fbf718d447c09bec76b8e3e155b9843d5795203c90f6665b8c21a0ef98`). Its original extracted run passed offline verification before the documented projection. Successful raw HTTP requests and measurement responses are unchanged; `nodes.body` is a documented field projection. All verifier-checked ownership and measurement values are retained. Selected Docker inspection and node fields were projected to remove host-only metadata; the full lifecycle command stream was reduced to Docker records. See `evidence-manifest.json` for field-level omissions and hashes. This directory has 47 files: the 45 verifier inputs, this report, and the manifest. The projection is not byte-identical to the sealed archive and should not be treated as an authenticated signature.

The qualification retained nine failed startup readiness attempts before readiness succeeded. Their 27 `ready-*` files and curl command records are omitted here because they do not contribute to the verifier's acceptance; the qualification report retains that fact. No claim of zero startup failures is made.

The original image inspection records `linux/amd64` and a Docker-reported image size of 1,443,902,696 bytes (about 1.45 GB); these non-sensitive fields are retained in `image.inspect.json`. Allow additional Docker disk and host-memory headroom.
