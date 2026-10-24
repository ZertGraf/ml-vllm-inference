# vLLM on Kubernetes (CPU only): rootless, private, reproducible

Test task for the ML technology research team (Data Engineer, intern): an LLM inference
service built on **vLLM 0.32.0** (CPU backend) and **Qwen2.5-0.5B-Instruct**, deployed to
**Minikube** with `kubectl apply -k`.

Everything in this README was run on the machine described under
[Test environment](#test-environment); `VERIFICATION.md` has the raw evidence for every check.

| Requirement | Where / how | Status |
|---|---|---|
| Minikube / k8s, CPU-only inference, vLLM >= 0.27.0 | vLLM **0.32.0** (latest stable on PyPI, 2026-10-18), CPU wheel, no GPU anywhere | verified |
| Own rootless Dockerfile, pinned deps | [`docker/vllm/Dockerfile`](docker/vllm/Dockerfile): UID 10001, base image pinned by digest, 152 packages pinned **with sha256** in [`requirements.txt`](docker/vllm/requirements.txt), `pip install --require-hashes --no-deps` | verified |
| Logger must not print prompts/answers, but must log the fact of each request | [`docker/vllm/vllm_hardening/`](docker/vllm/vllm_hardening) + [`logging_config.json`](docker/vllm/logging_config.json), see [Privacy logging](#privacy-logging) | verified with canary strings, including with vLLM request logging forced ON and a negative control |
| PVC cache: download on first start, local load afterwards | PVC `vllm-models`, [`download_model.py`](docker/downloader/download_model.py), see [Model cache](#model-cache-pvc) | verified (pod delete, rollout restart, unreachable HF endpoint) |
| Secrets: HF token + API Bearer token | kustomize `secretGenerator` from a git-ignored `secrets.env`, see [Secrets](#secrets) | verified |
| Parameters via ConfigMap, no rebuild | `vllm-serve-config` (`vllm serve --config`) + `vllm-env`, see [Configuration](#configuration-configmap) | verified (`max-model-len` change) |
| Bonus 1: kustomize | `k8s/base`, `k8s/overlays/minikube`, `k8s/overlays/privacy-stress` | verified |
| Bonus 2: separate `initContainer` image | [`docker/downloader/Dockerfile`](docker/downloader/Dockerfile): python slim + `huggingface_hub`, UID 10001, 15 hash-pinned packages | verified |
| Bonus 3: load test + analysis | [`loadtest/loadtest.py`](loadtest/loadtest.py), results and analysis in [Load testing](#load-testing) | verified |

## Contents

1. [Architecture](#architecture)
2. [Choices: vLLM version, model, dtype](#choices-vllm-version-model-dtype)
3. [Repository layout](#repository-layout)
4. [Quick start (Minikube)](#quick-start-minikube)
5. [Configuration (ConfigMap)](#configuration-configmap)
6. [Secrets](#secrets)
7. [Model cache (PVC)](#model-cache-pvc)
8. [Privacy logging](#privacy-logging)
9. [Security hardening](#security-hardening)
10. [Reproducibility](#reproducibility)
11. [Load testing](#load-testing)
12. [Findings about vLLM's CPU backend](#findings-about-vllms-cpu-backend)
13. [Known limitations](#known-limitations)
14. [Clean-up](#clean-up)

## Architecture

```
                         kubectl apply -k k8s/overlays/minikube
                                        |
   Namespace vllm (Pod Security "restricted")
   +-----------------------------------------------------------------------------+
   | Deployment vllm (1 replica, strategy Recreate)                              |
   |                                                                             |
   |  initContainer fetch-model            container vllm                        |
   |  image vllm-model-downloader          image vllm-cpu:0.32.0                 |
   |  UID 10001, read-only rootfs          UID 10001, read-only rootfs           |
   |  HF_TOKEN (Secret, optional)          VLLM_API_KEY (Secret)                 |
   |  downloads once -> PVC                HF_HUB_OFFLINE=1, reads PVC read-only |
   |        |                                   ^   ^                            |
   |        v                                   |   |  /etc/vllm/serve.yaml      |
   |  PVC vllm-models  ------------------------+   +- ConfigMap vllm-serve-config|
   |  PVC vllm-jit-cache (Triton kernels)          +- ConfigMap vllm-env (envFrom)|
   +-----------------------------------------------------------------------------+
                  Service vllm :8000  --->  port-forward / Ingress
```

Inside the `vllm` container the entrypoint starts `vllm serve` with
`--middleware vllm_hardening.stack.HardeningMiddleware` and a warm-up client next to it:

```
client -> [request-fact logger] -> [Bearer guard: deny by default] -> vLLM's own auth -> API
```

## Choices: vLLM version, model, dtype

**vLLM 0.32.0.** The task asks for >= 0.27.0. PyPI and GitHub (checked 2026-10-23) have
0.27.0 through 0.32.0; 0.32.0 (2026-10-18) is the newest stable release and includes the
security fixes listed in its release notes (chat-template resource exhaustion, restricted
default CORS, ...). Installation follows the official CPU guide for that exact version
([docs](https://docs.vllm.ai/en/v0.32.0/getting_started/installation/cpu/)): the **prebuilt
x86_64 CPU wheel** attached to the GitHub release
(`vllm-0.32.0+cpu-cp38-abi3-manylinux_2_39_x86_64.whl`), with `torch==2.13.0+cpu`. The wheel
needs glibc >= 2.39, hence Debian 13 (`python:3.12-slim-trixie`) as the base. Building from
source is not needed. The host CPU has AVX2 only (no AVX-512): the guide lists AVX2 as
supported with "limited features".

**Qwen/Qwen2.5-0.5B-Instruct.** An `instruct` model that is not gated and fits the budget: the
Docker VM has 7.4 GB RAM shared with other workloads, the Minikube node gets 4.5 GB, and the
service needs about 3.3 GB resident (bf16 weights 1 GB, API server 0.8 GB, engine core 0.5 GB,
KV cache, runtime). The 1.5B model from the task text would need about 2 GB more for the weights alone. The model
revision is pinned to a commit sha (`7ae557604adf67be50417f59c2c2f167def9a775`) in
[`configmap-env.yaml`](k8s/base/configmap-env.yaml). Any other model is a ConfigMap change
(`MODEL_REPO_ID`, `MODEL_REVISION`, `MODEL_LOCAL_DIR`); gated models additionally need `HF_TOKEN`.

**dtype `bfloat16`** (default of the deployment) because it halves memory. On this CPU
(AVX2, no AVX512-BF16) it is **not** the fastest: `float32` has 3.7x the prefill speed and 2.5x
the batched decode throughput, at twice the weight memory and a 40% slower single stream (see
[Knob sweep](#knob-sweep-offline-engine)). With more RAM, set `dtype: float32`.

## Repository layout

```
docker/vllm/                 vLLM image: Dockerfile, requirements.in/.txt (hashes), entrypoint.sh,
                             ready.sh, logging_config.json, vllm_hardening/ (python package), tests/
docker/downloader/           initContainer image: Dockerfile, requirements.in/.txt (hashes), download_model.py
k8s/base/                    Namespace, PVCs, ConfigMaps, Deployment, Service, kustomization
k8s/overlays/minikube/       secretGenerator (secrets.env.example), local images, node-sized resources
k8s/overlays/privacy-stress/ test-only overlay: vLLM content logging + DEBUG forced on
loadtest/                    loadtest.py (HTTP, streaming), engine_bench.py (offline), results/*.json
scripts/                     build-images.sh, make-secrets.sh, verify-deployment.sh, compile-requirements.sh,
                             check-pins.py, engine-bench.sh, make-submission.sh
VERIFICATION.md              every check that was run, with commands and observed output
```

## Quick start (Minikube)

Prerequisites: Docker, `minikube` (tested: v1.39.0), `kubectl` with built-in kustomize (tested: v1.36.1), `curl`, `jq`.
Commands are POSIX shell (Linux, macOS, WSL, Git Bash). About 6 GB of free disk and 4.5 GB of
free RAM for the cluster.

```bash
# 1. cluster (docker driver). Dedicated profile, 6 CPUs, 4.5 GB
minikube start -p vllm-test --driver=docker --cpus=6 --memory=4500mb

# 2. build both images locally (~5 min the first time) and load them into the cluster
scripts/build-images.sh
minikube -p vllm-test image load vllm-model-downloader:1.0.0
minikube -p vllm-test image load vllm-cpu:0.32.0

# 3. secrets: writes k8s/overlays/minikube/secrets.env (git-ignored) with a random API token.
#    HF_TOKEN stays empty: Qwen2.5-0.5B-Instruct is public (HF_TOKEN=hf_xxx scripts/make-secrets.sh for gated models)
scripts/make-secrets.sh

# 4. deploy (kustomize)
kubectl apply -k k8s/overlays/minikube
kubectl -n vllm rollout status deploy/vllm --timeout=15m   # first start: download + JIT warm-up, ~3-4 min
kubectl -n vllm logs deploy/vllm -c fetch-model            # the download, from the initContainer

# 5. use it
kubectl -n vllm port-forward svc/vllm 18000:8000 &
export VLLM_API_KEY="$(kubectl -n vllm get secret -l app.kubernetes.io/part-of=vllm-inference \
  -o jsonpath='{.items[0].data.VLLM_API_KEY}' | base64 -d)"
curl -s http://127.0.0.1:18000/v1/models -H "Authorization: Bearer $VLLM_API_KEY" | jq .
curl -s http://127.0.0.1:18000/v1/chat/completions \
  -H "Authorization: Bearer $VLLM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5-0.5b-instruct","messages":[{"role":"user","content":"Say hello in five words."}],"max_tokens":32}' | jq -r '.choices[0].message.content'
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18000/v1/models                      # 401 without a token

# 6. automated checks: auth, inference, error paths, privacy (canary strings in logs)
scripts/verify-deployment.sh
```

Git Bash on Windows: if a path such as `/models` gets rewritten, prefix the command with
`MSYS_NO_PATHCONV=1`. Ports used for local access are 18000-18099.

## Configuration (ConfigMap)

Two ConfigMaps in [`k8s/base`](k8s/base):

* `vllm-serve-config` holds [`serve.yaml`](k8s/base/configmap-serve.yaml), mounted at
  `/etc/vllm/serve.yaml` and passed as `vllm serve <model-dir> --config /etc/vllm/serve.yaml`.
  Keys are `vllm serve` option names: `dtype`, `max-model-len`, `max-num-seqs`,
  `max-num-batched-tokens`, `kv-cache-memory-bytes`, `enforce-eager`, `middleware`, ...
* `vllm-env` is injected with `envFrom` into both containers: model repo/revision/directory,
  `VLLM_CPU_OMP_THREADS_BIND`, `VLLM_USE_V2_MODEL_RUNNER`, `TRITON_CACHE_DIR`, `VLLM_LOGGING_LEVEL`,
  optional `HF_ENDPOINT`.

Change a value and restart the pod; the image is not rebuilt:

```bash
sed -i 's/max-model-len: 4096/max-model-len: 2048/' k8s/base/configmap-serve.yaml
kubectl apply -k k8s/overlays/minikube
kubectl -n vllm rollout restart deploy/vllm && kubectl -n vllm rollout status deploy/vllm --timeout=10m
curl -s http://127.0.0.1:18000/v1/models -H "Authorization: Bearer $VLLM_API_KEY" | jq '.data[0].max_model_len'   # 2048
git checkout k8s/base/configmap-serve.yaml                                                                  # undo
```

(`kubectl edit configmap vllm-serve-config -n vllm` + `rollout restart` works the same way.
The port-forward must be restarted after a pod restart.)

## Secrets

Real values are never committed. `k8s/overlays/minikube/kustomization.yaml` generates the Secret
`vllm-secrets-<hash>` from `secrets.env` (git-ignored; format in
[`secrets.env.example`](k8s/overlays/minikube/secrets.env.example)):

| Key | Used by | Purpose |
|---|---|---|
| `HF_TOKEN` | initContainer only (`optional: true`) | Hugging Face token for gated/private models. **Empty = anonymous download**; an empty value is never sent to the Hub. |
| `VLLM_API_KEY` | `vllm` container | Bearer token every client must send. The container refuses to start without it. |

The kustomize hash suffix makes a changed token roll the pod. The main container never sees the
HF token and has no network need (`HF_HUB_OFFLINE=1`).

## Model cache (PVC)

[`download_model.py`](docker/downloader/download_model.py) runs in the `fetch-model`
initContainer (it is also a normal CLI script: `MODEL_REPO_ID=... MODEL_LOCAL_DIR=... python download_model.py`).

* **First start:** resolves the revision to a commit, downloads into `<dir>.partial` on the PVC
  (resumable), verifies the sha256 of every LFS file against the Hub metadata, writes the
  completeness marker `.download-complete.json` (repo, revision, commit, file sizes) and
  `rename()`s the directory into place. A pod killed mid-download never leaves something that
  looks complete. A lock file serialises concurrent runs.
* **Later starts:** marker present, repo/revision match, file sizes match: the script prints
  `already present ..., skipping download` and exits without importing the HTTP stack.
* The `vllm` container mounts the PVC **read-only**, runs with `HF_HUB_OFFLINE=1` and
  `TRANSFORMERS_OFFLINE=1`, and loads from the local path. The entrypoint refuses to start when the
  marker is missing.
* `fsGroup: 10001` makes the volume writable for the unprivileged downloader. The Deployment uses
  `strategy: Recreate` because the volume is `ReadWriteOnce`.

A second small PVC, `vllm-jit-cache`, keeps the compiled Triton kernels (see
[Findings](#findings-about-vllms-cpu-backend)); restarts then skip a ~1 minute compile.

## Privacy logging

Requirement: prompts and generations must not reach stdout/stderr; the fact of each request must.
vLLM 0.32.0 has several places that can print user content: `--enable-log-requests` and
`--enable-log-outputs` (prompt, sampling parameters incl. stop strings and grammars, output text;
DEBUG adds the JSON body and token ids), the engine crash dump (prompt token ids), exception
messages that echo input (pydantic validation errors, "prompt too long"), and third-party logs.
So the protection is layered instead of relying on a flag staying off:

| Layer | File | What it does |
|---|---|---|
| 1. handler-level filter | [`logfilter.py`](docker/vllm/vllm_hardening/logfilter.py) `PrivacyFilter` | Attached to the **handlers** in [`logging_config.json`](docker/vllm/logging_config.json) (`VLLM_LOGGING_CONFIG_PATH`), so it sees every record of every logger (vllm, uvicorn, root, libraries). Drops everything below INFO regardless of `VLLM_LOGGING_LEVEL`; replaces records of vLLM's request logger and of the engine crash dump with `content-bearing log record suppressed (source=... request=<id>)`; while a request is being served reduces log arguments to request ids, numbers and type names; strips query strings from uvicorn access lines. Fails closed. |
| 2. formatter | `PrivacyFormatter` | For the HTTP layer and request context, tracebacks are printed as frames + exception *class names*, never messages. Start-up/engine errors keep full messages (an operator needs them and they cannot contain user input). `VLLM_HARDENING_REDACT_ALL_EXCEPTIONS=1` redacts everywhere. |
| 3. handler | `SafeStreamHandler` | `logging` normally prints message and arguments to stderr when formatting fails; this handler prints a content-free line instead. |
| 4. fact log | [`reqlog.py`](docker/vllm/vllm_hardening/reqlog.py) | Pure ASGI middleware, one line per request: id, method, route template, status, outcome, latency, time to first byte, request/response sizes, client IP, stream flag, engine request id, model, prompt/completion token counts. The request body is not read (only its size); from the response only `id`, `model` and the `usage` counters are extracted. `/health` and `/metrics` are not logged. |

Example line (real output):

```
(APIServer pid=10) INFO 10-24 07:08:03 [reqlog.py:188] request_id=req-1aaed0497e114d4a method=POST route=/v1/chat/completions status=200 outcome=ok latency_ms=1077.4 ttfb_ms=988.6 req_bytes=188 resp_bytes=1329 client=127.0.0.1 stream=true engine_id=chatcmpl-9c66dba4b53d696d model=qwen2.5-0.5b-instruct prompt_tokens=24 completion_tokens=16
```

Evidence ([`VERIFICATION.md`](VERIFICATION.md)): `scripts/verify-deployment.sh` sends unique
canary strings in the prompt, in `stop`, in malformed/invalid payloads and in the model name, asks
the model to echo a canary, aborts a stream, and then greps the logs of all containers (and
`--previous`). It passes with the default config **and** with
`k8s/overlays/privacy-stress` (vLLM request/output logging and DEBUG forced on through the
ConfigMaps; the filter suppressed 67 records in that run). As a **negative control**, the same
test with the privacy log config removed (`VLLM_LOGGING_CONFIG_PATH=`) fails: stock vLLM prints
the JSON body, the rendered prompt, the sampling parameters and the output text.

Limits: this controls what the logging system and the middleware write. It cannot stop a process
from `print()`-ing user data or a native library from writing to the file descriptors directly;
nothing in vLLM 0.32.0's request path does, which the canary tests confirm. The warm-up client
prints its own content-free progress lines.

## Security hardening

Pod (`k8s/base/deployment.yaml`, namespace labelled Pod Security `restricted`):
`runAsNonRoot`, UID/GID 10001, `fsGroup`, `seccompProfile: RuntimeDefault`,
`allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `readOnlyRootFilesystem: true`
for both containers (writable: `/tmp` emptyDir, `/dev/shm` Memory emptyDir, the JIT cache PVC),
`automountServiceAccountToken: false`, no service links, startup/readiness/liveness probes,
requests/limits. Images: base pinned by digest, hash-checked dependencies, non-root `USER`, no
shell login for the user, no secrets in layers.

**`--api-key` does not protect the whole API.** vLLM's own docs
([security](https://docs.vllm.ai/en/v0.32.0/usage/security/)) list routes outside `/v1` that stay
open when `--api-key`/`VLLM_API_KEY` is set: `/invocations` (same inference as `/v1`), `/score`,
`/rerank`, `/pooling`, `/classify` and the operational `/pause`, `/abort_requests`, ... A Secret
holding the key is therefore not enough. `vllm_hardening.guard.BearerAuthGuard` closes the gap:
every request needs `Authorization: Bearer <VLLM_API_KEY>` except `/health` (kubelet probes). The
server refuses to start without a key. Checked: `/invocations`, `/pause` and `/metrics` answer 401
without a token. Further advice from the vLLM docs still applies (network policy, TLS at the
ingress); the NetworkPolicy is not included because Minikube's default CNI does not enforce it.

## Reproducibility

* Base image: `python:3.12-slim-trixie@sha256:a0f57064...b86704` in both Dockerfiles.
* `requirements.txt` files are generated, not hand-written: `scripts/compile-requirements.sh`
  runs `uv pip compile --generate-hashes` inside the base image (same platform, Python 3.12,
  torch from the official CPU index). They contain every transitive dependency, including `vllm`
  and the Triton-CPU wheel as hashed URLs.
* Installation uses `--require-hashes --no-deps`, followed by `pip check`.
* `scripts/check-pins.py docker/vllm/requirements.txt vllm-cpu:0.32.0` compares `pip list` inside
  the image with the pins in both directions.
* Not pinned by hash: the four Debian packages (`libnuma1 gcc g++ libc6-dev`) installed with apt.

## Load testing

Test environment: AMD Ryzen 7 7735U (8 cores / 16 threads, AVX2, no AVX-512), Windows 11,
Docker Desktop 29 (WSL2 VM: 16 vCPU, 7.4 GB RAM shared with other workloads), Minikube node
6 CPUs / 4.5 GB. Pod: bf16, `max-num-seqs 8`, `max-num-batched-tokens 2048`, 256 MiB KV cache
(21,760 tokens), 5 OpenMP threads pinned to physical cores `0,2,4,6,8`, model runner V1, eager mode.
The client ran on the host through `kubectl port-forward`.

Run it yourself (with the port-forward from the quick start; `pip install httpx` first):

```bash
python loadtest/loadtest.py --base-url http://127.0.0.1:18000 --api-key "$VLLM_API_KEY" \
  --profile short:64:64 --profile long:1024:32 --concurrency 1,2,4,8 \
  --requests-per-worker 3 --min-requests 6 --warmup 2 --json-out loadtest/results/baseline.json
```

`loadtest.py` is a closed-loop generator: `concurrency` workers each send streaming chat requests
back to back. Every prompt starts with a unique random prefix (no prefix-cache hits) and uses
`ignore_eos`, so every request generates exactly `max_tokens`. TTFT is the time to the first
generated token, latency the time to the end of the stream, TPOT the mean time per token after
the first; token counts come from the server's `usage`, not from estimates. Profile syntax is
`name:prompt_tokens:max_tokens`.

### Results (HTTP endpoint, bf16, as deployed)

| profile | prompt tok | out tok | conc | reqs (ok/fail) | TTFT p50 (ms) | TTFT p95 (ms) | latency p50 (ms) | latency p95 (ms) | TPOT (ms) | req/s | out tok/s | in tok/s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| short | 70 | 64 | 1 | 6/0 | 1476 | 1586 | 5915 | 6282 | 70.8 | 0.17 | 10.8 | 11.7 |
| short | 71 | 64 | 2 | 6/0 | 2168 | 3190 | 8671 | 9535 | 103.0 | 0.23 | 14.6 | 16.2 |
| short | 69 | 64 | 4 | 12/0 | 5304 | 5509 | 14619 | 14683 | 159.8 | 0.28 | 17.7 | 19.2 |
| short | 70 | 64 | 8 | 24/0 | 10850 | 10993 | 28758 | 29302 | 301.5 | 0.28 | 17.9 | 19.5 |
| long | 1022 | 32 | 1 | 6/0 | 19568 | 20826 | 21703 | 24399 | 82.4 | 0.05 | 1.6 | 49.7 |
| long | 1031 | 32 | 2 | 6/0 | 28434 | 38699 | 40343 | 42672 | 404.4 | 0.05 | 1.6 | 50.2 |
| long | 1030 | 32 | 4 | 12/0 | 58258 | 78081 | 81133 | 101576 | 1022.5 | 0.05 | 1.6 | 50.2 |
| long | 1022 | 32 | 8 | 24/0 | 81986 | 132812 | 159716 | 215168 | 2740.6 | 0.05 | 1.6 | 50.2 |

n = 6-24 requests per row, so p95 is approximate. Throughput columns are over the whole level
(wall time), `in tok/s` counts prompt tokens.

Reading the table: the engine is compute-bound. Prompt processing runs at a constant ~50 tokens/s
and generation saturates at ~18 tokens/s whatever the concurrency, so extra concurrency only
queues: TTFT of the long profile grows from 19.6 s (1 client) to 82 s (8 clients) at an
unchanged 0.05 req/s.

### Knob sweep (offline engine)

To see which knobs matter, [`loadtest/engine_bench.py`](loadtest/engine_bench.py)
(`scripts/engine-bench.sh`) loads the same model in the same image and measures prefill speed
(one 833-token prompt) and decode throughput for N concurrent 64-token generations
(`max-num-seqs 16`, 6 CPUs, no HTTP layer, so numbers are higher than the table above):

| dtype | OpenMP threads (physical cores) | prefill tok/s (833-token prompt) | decode tok/s, batch 1 | batch 4 | batch 8 | batch 16 |
|---|---:|---:|---:|---:|---:|---:|
| bfloat16 | 5 | 68.5 | 12.6 | 23.6 | 28.8 | 29.5 |
| bfloat16 | 7 | 66.3 | 14.0 | 26.2 | 29.3 | 30.3 |
| float32 | 5 | 252.6 | 7.7 | 28.4 | 47.2 | 74.6 |
| float32 | 7 | 278.6 | 8.1 | 29.3 | 49.7 | 79.6 |

### Analysis: which parameters to change

Sources: vLLM 0.32.0 docs, [Optimization and Tuning](https://docs.vllm.ai/en/v0.32.0/configuration/optimization/)
and the [CPU guide](https://docs.vllm.ai/en/v0.32.0/getting_started/installation/cpu/)
(sections "How to decide `VLLM_CPU_OMP_THREADS_BIND`", "How to do performance tuning for vLLM CPU"),
plus the measurements above.

**A. Many requests, few tokens (high request rate, short prompts and answers).** The goal is a
big batch per step and cheap per-request overhead.

* `max-num-seqs` up. Per the CPU guide it "has more impact on output token performance"; a larger
  batch gives higher throughput, a smaller one lower latency. Measured with fp32: 7.7 tok/s at
  batch 1, 28 at 4, 47 at 8, **75 at 16**. The deployment caps it at 8 to fit the memory budget.
  Size the KV cache for it (`kv-cache-memory-bytes`, 12 KiB per token for this model, or
  `VLLM_CPU_KVCACHE_SPACE` in whole GiB); otherwise requests are preempted and recomputed
  (docs: "Preemption").
* `dtype`: on this AVX2 CPU `float32` is the throughput choice (see the sweep: bf16 saturates at
  30 tok/s because it is emulated, fp32 keeps scaling with the batch). On AVX-512-BF16/AMX
  hardware keep `bfloat16`.
* `max-num-batched-tokens` stays moderate (2048, the online default of the CPU guide): the docs
  say smaller values give better inter-token latency, larger values better TTFT; short prompts
  never fill the budget anyway.
* Threads: one OpenMP thread per **physical** core (`VLLM_CPU_OMP_THREADS_BIND=0,2,4,...`, the
  CPU guide's recommendation for SMT hosts); going from 5 to 7 threads gave +5..10% on fp32
  here (prefill and batched decode). Leave a core for the API server and engine core.
* `--api-server-count N` once the single API process (tokenising, SSE streaming) is the limit
  (docs: "Parallel Processing"; the 0.32.0 release notes report +43-85% req/s on very short
  requests). It needs spare cores and is not useful on a 6-core node.
* Keep prefix caching (default) for requests that share a system prompt.

**B. Fewer requests, huge contexts.** The work is dominated by prefill, which is compute-bound;
KV memory and scheduling matter more than batch size.

* `dtype float32` on non-AVX-512-BF16 CPUs: prefill 68 -> 253-279 tok/s (3.7-4.2x). This is
  the biggest single lever measured; it needs ~1 GB more RAM.
* `max-model-len` at least the longest context, and `kv-cache-memory-bytes` large enough for
  `concurrent sequences x context` (this model: 12 KiB per token, i.e. 384 MiB for one 32k-token
  sequence). A too small cache leads to preemption/recompute.
* `max-num-seqs` down (2-4): the table shows that concurrency does not add throughput for long
  prompts, only queueing delay and KV pressure. Add replicas instead of concurrency.
* `max-num-batched-tokens` (the prefill chunk size) up, e.g. 4096-8192 where activation memory
  allows. Docs ("Chunked Prefill"): higher values give better TTFT because more prefill tokens go
  into one step, smaller values protect the latency of concurrent decodes; for optimal
  throughput the docs suggest values above 8192. Chunked prefill itself stays on (V1 default) so
  one long prompt does not freeze other users. Not measured here, so this is a docs-based
  recommendation.
* Prefix caching (default on) pays off when many requests share one long document/system prompt.
* `--block-size` a multiple of 32 (CPU guide), weight-only quantisation (AWQ/GPTQ, W8A8 INT8 are
  supported on x86 CPU per the guide) when the model is large enough for decode to be
  bandwidth-bound; not useful for a 0.5B model.
* More physical cores, or tensor/data parallelism across NUMA nodes on multi-socket hosts
  (CPU guide).

## Findings about vLLM's CPU backend

Things that are not obvious from the docs and cost time; each was reproduced.

1. **vLLM 0.32.0 compiles Triton-CPU kernels at run time and needs `gcc`/`g++`.** Without a
   compiler the first request fails with `Failed to find C compiler`. The image therefore ships
   `gcc g++ libc6-dev` (the official vLLM CPU image also contains a compiler). The compiled
   `.so` files are loaded from `TRITON_CACHE_DIR`, so that directory must be on a filesystem
   mounted **without** `noexec` (a plain `docker run --tmpfs /tmp` is `noexec` and fails with
   `failed to map segment from shared object`; Kubernetes emptyDir/PVC volumes are fine).
2. **First request after start takes ~1 minute** (kernel compilation). The entrypoint runs a
   warm-up client and the readiness probe waits for it; `vllm-jit-cache` makes restarts fast
   (warm-up of the second start: 2.2 s instead of 57 s for the first request).
3. **Model runner V2 is the default and did not work here:** its Triton-CPU sampler kernel was
   still compiling after 2 minutes on this AVX2 CPU. `VLLM_USE_V2_MODEL_RUNNER=0` (V1 runner)
   compiles in about a minute and then runs at full speed.
4. **Memory pre-flight counts page cache.** `memory.max - memory.current` must exceed the KV
   cache size at start-up, and `memory.current` includes reclaimable page cache. With a 4 GiB
   limit and a cold cache the start failed with
   `Available memory on node 0 (0.19/4.0 GiB) ... is less than requested memory for kv (0.25 GiB)`
   although only ~2.9 GiB was really in use. The Deployment therefore uses a 5 GiB limit
   (3 GiB request); resident memory is ~3.3 GiB.
5. **bf16 on AVX2 is slow** (oneDNN cannot build the bf16 matmul and torch falls back),
   see the sweep. `fp32` is 4x faster in prefill.
6. `enforce-eager` (no torch.compile/inductor) is used: it avoids a long compile with another
   C++ toolchain step and extra memory; vLLM also skips its JIT warm-up in that mode, which is why
   the warm-up client exists.

## Known limitations

* CPU throughput is low by design (see the table); the numbers are for a laptop-class CPU under
  Docker/WSL2, not a server.
* The Minikube node is smaller than the pod's memory limit on purpose (explained in finding 4);
  on a real cluster set requests/limits for your node size.
* `VLLM_CPU_OMP_THREADS_BIND=0,2,4,6,8` assumes the 8-core/16-thread layout above; use `auto` or
  adjust on other hardware.
* One replica only: the model cache and JIT cache PVCs are `ReadWriteOnce`.
* The privacy filter protects the logging system and the middleware. It is not a data-loss
  prevention tool for traces, metrics or a debugger attached to the container.
* The first start downloads ~1 GB from Hugging Face and needs internet access; no later start does.
* vLLM's usage statistics are disabled (`VLLM_NO_USAGE_STATS=1`, `VLLM_DO_NOT_TRACK=1`).

## Clean-up

```bash
kubectl delete -k k8s/overlays/minikube      # keeps nothing but the PVC data inside the cluster
minikube stop -p vllm-test                     # or: minikube delete -p vllm-test
```
