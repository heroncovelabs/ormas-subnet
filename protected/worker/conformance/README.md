<!-- MIT. The public worker directory is the source, not a deployment mirror. -->
# Check your worker locally

Use **Python 3.12 with Unicode 15.0.0** and a running Docker Engine. The intake refuses older Python or a different Unicode database before running any container: older databases miss path controls such as U+0890. Alternatively run inside the Dockerfile's pinned Python image, with a Docker CLI and daemon access provided; the bare Python image has no Docker CLI. Bind fixture paths must also be accessible to that daemon.

From `protected/worker/`, run these three commands (replace the image/tag and choose the byte cap yourself):

```sh
cd /path/to/ormas-subnet/protected/worker
docker build -t my-worker -f /path/to/your/Dockerfile /path/to/your/build-context
python3.12 conformance/conformance.py my-worker --max-patch-bytes "$PATCH_CAP_BYTES"
```

For the reference worker, the build command is `docker build -t my-worker .`.
`PATCH_CAP_BYTES` is the shell operator's positive artifact-byte cap, **not a published default**. It is required and applies to patch and offer artifacts. Optionally add `--wall-time-seconds "$WORKER_WALL_SECONDS"` with your operator's invocation wall-time; without it this local harness imposes no runtime cap. It stops/removes a surviving worker on timeout or interruption. Before removing each Linux bind fixture, a root container from the pinned base runs `chmod -R a+rwX /work` to restore access to uid-65534-owned files. Cleanup failure reports `FAIL cleanup_failed` and retains the fixture rather than passing.

Each invocation has fresh writable `/work` for uid 65534. **Solve** gets source-only `repo/`, flat `packet.json`, `.home/`, `.tmp/`. **Offer** gets sanitized `offer-request.json`, `.home/`, `.tmp/`, not source or a solve packet. The harness runs `docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 --network none` with the contract environment. **No network** substitutes for proxy-only networking; no provider call is possible. Workers needing a provider may decline these local fixtures.

Checks cover solve/offer exits, regular bounded artifacts, patch scope/refusals and strict offer JSON. Three negative-control workers write a symlink, out-of-scope diff and forbidden mode; each must be refused. They run on the reference Dockerfile's pinned Python base, so your image need not contain Python. Docker may pull that base. The harness prints one `PASS`/`FAIL <code>` line per check and exits 0 only when all pass. Container stdout/stderr is discarded; never put secrets in CLI arguments or fixture files.

**Parity note:** the standalone text-parser and scope-helper functions are AST-identical to `worker_patch`/`local_runner`, with a crafted parity corpus. The public delivery wrapper additionally refuses COPY as **`copy_unsupported` (harness-only)**: `worker_patch` still accepts COPY text, but `worker_apply.apply_patch` refuses `apply_scope_mismatch` because the source is unchanged. Renames are supported when both paths are in scope. Runtime codes `python_version_mismatch` and `unicode_version_mismatch`, plus regular-file reader diagnostics, are also local-harness codes, not live gateway outcomes. See [the contract](../CONTRACT.md) for the live archive/worker outcomes.

Passing does **not** qualify live proxy isolation, resource quotas, attestation, general diff applicability or task acceptance. The shell checks original authority, applies on a fresh checkout and runs its verifier. Your strategy belongs inside your image; the reference strategy only exercises the boundary.
