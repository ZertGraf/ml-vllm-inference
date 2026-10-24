# Verification log

Everything below was run by the author on the machine described in section 1, from a clean state
(`minikube delete -p vllm-test`, then the README quick start step by step). Output is trimmed
(long lines are cut, repeated progress lines removed) but not edited. Raw inputs are in the repo:
`scripts/verify-deployment.sh`, `docker/vllm/tests/`, `loadtest/results/`.

## 1. Environment

```text
date: 2026-10-24 08:17 UTC
docker: 29.7.2
Client Version: v1.36.1 Kustomize Version: v5.8.1 
minikube version: v1.39.0
Server Version: v1.37.0
node:
NAME        STATUS   ROLES           AGE   VERSION   INTERNAL-IP    EXTERNAL-IP   OS-IMAGE                         KERNEL-VERSION                             CONTAINER-RUNTIME
vllm-test   Ready    control-plane   19m   v1.37.0   192.168.49.2   <none>        Debian GNU/Linux 12 (bookworm)   6.6.87.2-microsoft-standard-WSL2 (amd64)   containerd://2.3.4
Capacity:
  cpu:                16
  memory:             7741632Ki
Allocatable:
  cpu:                16
  memory:             7741632Ki
model name	: AMD Ryzen 7 7735U with Radeon Graphics
avx2
16
              total        used        free      shared  buff/cache   available
Mem:           7560        4401         146          14        3014        2955
```

The Minikube node container was started with `--memory=4500mb --cpus=6`; Docker enforces that on
the node container (`docker inspect vllm-test`: Memory=4718592000, NanoCpus=6000000000), while the
kubelet reports the whole VM as capacity.

## 2. Summary

| # | Check | Result |
|---|---|---|
| I1 | vLLM image runs as non-root (`id`, `docker inspect` User) | pass: uid=10001, `User=10001:10001` |
| I2 | downloader image runs as non-root, separate Dockerfile | pass: uid=10001 |
| I3 | `pip check` clean in both images | pass |
| I4 | installed packages == pinned packages (both directions) | pass: 152 vLLM image, 15 downloader image; 4227 sha256 hashes in the vLLM requirements |
| I5 | vLLM version >= 0.27.0 | pass: 0.32.0 (torch 2.13.0+cpu) |
| I6 | unit tests of the logging filter / middlewares / guard run in the image | pass: 19 tests |
| D1 | `kubectl apply -k k8s/overlays/minikube` on a fresh cluster | pass |
| D2 | initContainer downloads the model into the PVC on first start | pass: logs below |
| D3 | pod Ready, no restarts | pass |
| D4 | `kubectl exec ... id` non-root; read-only rootfs; no capabilities; seccomp; no-new-privs | pass |
| A1 | no token -> 401, wrong token -> 401, correct token -> 200 | pass |
| A2 | routes outside `/v1` (`/invocations`, `/pause`, `/metrics`) -> 401 without token (open in stock vLLM) | pass |
| A3 | `/v1/models` and `/v1/chat/completions` return sensible answers | pass |
| P1 | privacy, default config: canaries in prompt, stop string, bad payloads, model name and echoed output; none in any log; request-fact lines present | pass |
| P2 | privacy, vLLM request/output logging + DEBUG forced ON via ConfigMap | pass (75 records suppressed by the filter) |
| P3 | negative control: same test without the privacy log config | fails as expected: prompt, JSON body, sampling params and output are printed |
| C1 | `kubectl delete pod` -> new pod skips the download | pass: "already present ... skipping download" |
| C2 | `rollout restart` with an unreachable `HF_ENDPOINT` | pass: skip + normal start + chat works |
| C3 | the vLLM container has `HF_HUB_OFFLINE=1`, no outbound connections | pass |
| C4 | node container SIGKILLed by a Docker Desktop restart (exit 137, not OOM); `minikube start` again: PVC data intact, download skipped, pod Ready again | pass |
| K1 | ConfigMap `max-model-len` 4096 -> 2048, restart, observe | pass: `/v1/models` reports 2048 |
| L1 | load test (README command), TTFT / latency / throughput | pass, section 8 |
| S1 | submission file and fallback zip, extracted and checked | pass, section 10 |

## 3. Images (I1-I6)

```text
$ scripts/build-images.sh


$ docker run --rm --entrypoint id vllm-cpu:0.32.0
uid=10001(vllm) gid=10001(vllm) groups=10001(vllm)
$ docker inspect --format User/Entrypoint
User=10001:10001 Entrypoint=[/usr/local/bin/entrypoint.sh]
$ docker run --rm --entrypoint pip vllm-cpu:0.32.0 check
No broken requirements found.

$ docker run --rm --entrypoint id vllm-model-downloader:1.0.0
uid=10001(downloader) gid=10001(downloader) groups=10001(downloader)
$ docker inspect --format User/Entrypoint
User=10001:10001 Entrypoint=[python /app/download_model.py]
$ docker run --rm --entrypoint pip vllm-model-downloader:1.0.0 check
No broken requirements found.

$ vllm version
vllm 0.32.0 torch 2.13.0+cpu transformers 5.19.0

$ python scripts/check-pins.py docker/vllm/requirements.txt vllm-cpu:0.32.0
pinned packages: 152, installed packages: 153, differences: 0
$ python scripts/check-pins.py docker/downloader/requirements.txt vllm-model-downloader:1.0.0
pinned packages: 15, installed packages: 16, differences: 0

$ unit tests in the image
test_args_reduced_inside_request_context (test_hardening.FilterTests.test_args_reduced_inside_request_context) ... ok
test_args_untouched_outside_request_context (test_hardening.FilterTests.test_args_untouched_outside_request_context) ... ok
test_cached_exc_text_is_not_reused (test_hardening.FilterTests.test_cached_exc_text_is_not_reused) ... ok
test_debug_is_dropped (test_hardening.FilterTests.test_debug_is_dropped) ... ok
test_engine_dump_is_templated (test_hardening.FilterTests.test_engine_dump_is_templated) ... ok
test_handle_error_does_not_echo_message (test_hardening.FilterTests.test_handle_error_does_not_echo_message) ... ok
test_request_logger_records_are_templated (test_hardening.FilterTests.test_request_logger_records_are_templated) ... ok
test_startup_traceback_keeps_message (test_hardening.FilterTests.test_startup_traceback_keeps_message) ... ok
test_traceback_redacted_for_http_layer_logger (test_hardening.FilterTests.test_traceback_redacted_for_http_layer_logger) ... ok
test_traceback_redacted_inside_request_context (test_hardening.FilterTests.test_traceback_redacted_inside_request_context) ... ok
test_uvicorn_access_query_string_removed (test_hardening.FilterTests.test_uvicorn_access_query_string_removed) ... ok
test_exception_class_only (test_hardening.MiddlewareTests.test_exception_class_only) ... ok
test_fact_line_for_json_response (test_hardening.MiddlewareTests.test_fact_line_for_json_response) ... ok
test_fact_line_for_sse_response (test_hardening.MiddlewareTests.test_fact_line_for_sse_response) ... ok
test_guard_allows_token_and_public_health (test_hardening.MiddlewareTests.test_guard_allows_token_and_public_health) ... ok
test_guard_refuses_to_start_without_key (test_hardening.MiddlewareTests.test_guard_refuses_to_start_without_key) ... ok
test_guard_rejects_without_or_with_wrong_token (test_hardening.MiddlewareTests.test_guard_rejects_without_or_with_wrong_token) ... ok
test_health_is_not_logged (test_hardening.MiddlewareTests.test_health_is_not_logged) ... ok
test_unauthorized_request_is_logged_with_status_401 (test_hardening.MiddlewareTests.test_unauthorized_request_is_logged_with_status_401) ... ok

----------------------------------------------------------------------
Ran 19 tests in 0.008s

OK

$ requirement hash coverage
packages 152 sha256 hashes 4227
$ docker history sizes
docker: 'docker images' requires at most 1 argument

Usage:  docker images [OPTIONS] [REPOSITORY[:TAG]]

Run 'docker images --help' for more information
```

## 4. Deployment (D1-D4)

```text
$ kubectl apply -k k8s/overlays/minikube
namespace/vllm created
configmap/vllm-env created
configmap/vllm-serve-config created
secret/vllm-secrets-54826bmkbh created
service/vllm created
persistentvolumeclaim/vllm-jit-cache created
persistentvolumeclaim/vllm-models created
deployment.apps/vllm created
$ kubectl -n vllm rollout status deploy/vllm --timeout=15m
Waiting for deployment "vllm" rollout to finish: 0 of 1 updated replicas are available...
deployment "vllm" successfully rolled out
$ kubectl -n vllm get pods,pvc
NAME                        READY   STATUS    RESTARTS   AGE
pod/vllm-7547c4fd7b-vjml6   1/1     Running   0          2m56s

NAME                                   STATUS   VOLUME                                     CAPACITY   ACCESS MODES   STORAGECLASS   VOLUMEATTRIBUTESCLASS   AGE
persistentvolumeclaim/vllm-jit-cache   Bound    pvc-d0f7ed26-dff3-4eb5-92f5-925089a3ac54   1Gi        RWO            standard       <unset>                 2m56s
persistentvolumeclaim/vllm-models      Bound    pvc-2a994193-93c4-4057-bf08-6f5ebd3ec4ca   5Gi        RWO            standard       <unset>                 2m56s
$ kubectl -n vllm logs deploy/vllm -c fetch-model
[download-model] 08:00:24 acquiring lock /models/qwen2.5-0.5b-instruct.lock
[download-model] 08:00:24 no complete copy of Qwen/Qwen2.5-0.5B-Instruct@7ae557604adf67be50417f59c2c2f167def9a775 in /models/qwen2.5-0.5b-instruct; token not provided (anonymous)
[download-model] 08:00:25 resolved Qwen/Qwen2.5-0.5B-Instruct@7ae557604adf67be50417f59c2c2f167def9a775 -> 7ae557604adf67be50417f59c2c2f167def9a775; 7 file(s)
[download-model] 08:00:25 downloading to /models/qwen2.5-0.5b-instruct.partial (resumable)
Warning: You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN to enable higher rate limits and faster downloads.
[download-model] 08:01:03 sha256 ok: model.safetensors
[download-model] 08:01:03 model ready in /models/qwen2.5-0.5b-instruct (999.6 MB)
```

```text
$ kubectl -n vllm exec deploy/vllm -c vllm -- id
uid=10001(vllm) gid=10001(vllm) groups=10001(vllm)
$ kubectl -n vllm exec deploy/vllm -c vllm -- sh -c "touch /usr/test || echo read-only root fs"
touch: cannot touch '/usr/test': Read-only file system
(root filesystem is read-only)
$ kubectl -n vllm exec deploy/vllm -c vllm -- sh -c "grep -E ^(Cap|NoNewPrivs|Seccomp) /proc/1/status"
CapEff:	0000000000000000
CapBnd:	0000000000000000
NoNewPrivs:	1
Seccomp:	2
$ pod securityContext (jsonpath)
{"fsGroup":10001,"fsGroupChangePolicy":"OnRootMismatch","runAsGroup":10001,"runAsNonRoot":true,"runAsUser":10001,"seccompProfile":{"type":"RuntimeDefault"}}
{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]},"readOnlyRootFilesystem":true}
{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]},"readOnlyRootFilesystem":true}
$ kubectl -n vllm get pod -o jsonpath=initContainer/container status
fetch-model exit=0 reason=Completed
vllm ready=true restarts=0
$ memory of the vllm container
memory.current=3800502272 memory.max=5368709120
anon 3557740544
file 188796928
$ kubectl -n vllm logs deploy/vllm -c vllm | head -n 8
[entrypoint] model dir : /models/qwen2.5-0.5b-instruct
[entrypoint] config    : /etc/vllm/serve.yaml
[entrypoint] uid/gid   : 10001/10001
(APIServer pid=10) INFO 10-24 08:01:17 [api_utils.py:347] vLLM server version 0.32.0, serving model /models/qwen2.5-0.5b-instruct
(APIServer pid=10) INFO 10-24 08:01:17 [api_utils.py:286] non-default args: {'model_tag': '/models/qwen2.5-0.5b-instruct', 'enable_force_include_usage': True, 'host': '0.0.0.0', 'disable_uvicorn_acces
(APIServer pid=10) INFO 10-24 08:01:28 [model.py:733] Resolved architecture: Qwen2ForCausalLM
(APIServer pid=10) INFO 10-24 08:01:28 [model.py:2152] Using max model len 4096
(APIServer pid=10) WARNING 10-24 08:01:28 [vllm.py:1703] Enforce eager set, disabling torch.compile, CUDAGraphs, and JIT kernel warmup. This is equivalent to setting -cc.mode=none -cc.cudagraph_mode=n
```

## 5. Authentication, inference, privacy (A1-A3, P1)

README step 5 (manual calls):

```text
$ kubectl -n vllm port-forward svc/vllm 18000:8000 &
(token length: 43)
$ curl -s http://127.0.0.1:18000/v1/models -H "Authorization: Bearer $VLLM_API_KEY" | jq .
{
  "object": "list",
  "data": [
    {
      "id": "qwen2.5-0.5b-instruct",
      "object": "model",
      "created": 1792829016,
      "owned_by": "vllm",
      "root": "/models/qwen2.5-0.5b-instruct",
      "parent": null,
      "max_model_len": 4096,
      "permission": [
        {
          "id": "modelperm-a6ab856bc234c39a",
          "object": "model_permission",
          "created": 1792829016,
          "allow_create_engine": false,
          "allow_sampling": true,
          "allow_logprobs": true,
          "allow_search_indices": false,
          "allow_view": true,
          "allow_fine_tuning": false,
          "organization": "*",
          "group": null,
          "is_blocking": false
        }
      ]
    }
  ]
}
$ curl ... /v1/chat/completions ... | jq -r .choices[0].message.content
Hello! How can I assist you today?
$ curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18000/v1/models   # no token
401
```

`scripts/verify-deployment.sh` on the default configuration:

```text
== authentication
PASS  GET /health without token (public, used by probes) -> HTTP 200
PASS  GET /v1/models without token -> HTTP 401
PASS  GET /v1/models with wrong token -> HTTP 401
PASS  GET /v1/models with correct token -> HTTP 200
      model: qwen2.5-0.5b-instruct max_model_len=4096
PASS  POST /invocations without token (open in stock vLLM) -> HTTP 401
PASS  POST /pause without token (open in stock vLLM) -> HTTP 401
PASS  GET /metrics without token -> HTTP 401
== inference
PASS  chat completion (non-stream) -> HTTP 200
      answer: CANARY-OUT-d6c1294625ab
      usage : {"prompt_tokens":74,"total_tokens":90,"completion_tokens":16,"prompt_tokens_details":null,"completion_tokens_details":null}
PASS  model echoed the output canary (canary check is not vacuous)
PASS  chat completion (stream) -> HTTP 200
PASS  stream terminated with [DONE]
== error paths (every payload carries a canary)
PASS  prompt longer than max-model-len -> HTTP 400
PASS  malformed JSON body -> HTTP 400
PASS  wrong field type -> HTTP 400
PASS  unknown model name -> HTTP 404
PASS  messages is a string, not a list -> HTTP 400
PASS  invalid json_schema -> HTTP 400
PASS  aborted stream sent (client disconnect)
== log scan (all containers, current and previous)
      scanned 126 log lines
PASS  no canary (prompt / output / stop / error payloads) in any log
PASS  generated answer text not in the logs
PASS  request-fact lines present (21)
      (APIServer pid=10) INFO 10-24 08:02:46 [reqlog.py:201] request_id=req-2f4f5b36e5094027 method=POST route=/v1/chat/completions status=200 outcome=ok latency_ms=52684.3 ttfb_ms=52684.0 req_bytes=142 resp_bytes=694 client=127.0.0.1 stream=false...
      (APIServer pid=10) INFO 10-24 08:02:50 [reqlog.py:201] request_id=req-cfe6e2738ec147ec method=POST route=/v1/chat/completions status=200 outcome=ok latency_ms=4062.4 ttfb_ms=4000.6 req_bytes=188 resp_bytes=1329 client=127.0.0.1 stream=true e...
      (APIServer pid=10) INFO 10-24 08:03:37 [reqlog.py:201] request_id=req-9c627d278b9b4b73 method=GET route=/v1/models status=401 outcome=ok latency_ms=0.1 ttfb_ms=0.1 req_bytes=0 resp_bytes=77 client=127.0.0.1 stream=false engine_id=- model=- p...
      (APIServer pid=10) INFO 10-24 08:03:50 [reqlog.py:201] request_id=req-73d6f744190d43af method=POST route=/v1/chat/completions status=200 outcome=client_disconnected latency_ms=2003.8 ttfb_ms=1043.0 req_bytes=246 resp_bytes=6077 client=127.0....

ALL CHECKS PASSED
```

### P2: vLLM request logging and DEBUG forced ON (`k8s/overlays/privacy-stress`)

```text
$ scripts/verify-deployment.sh   (privacy-stress overlay active: vLLM request/output logging ON, DEBUG)
== authentication
PASS  GET /health without token (public, used by probes) -> HTTP 200
PASS  GET /v1/models without token -> HTTP 401
PASS  GET /v1/models with wrong token -> HTTP 401
PASS  GET /v1/models with correct token -> HTTP 200
      model: qwen2.5-0.5b-instruct max_model_len=4096
PASS  POST /invocations without token (open in stock vLLM) -> HTTP 401
PASS  POST /pause without token (open in stock vLLM) -> HTTP 401
PASS  GET /metrics without token -> HTTP 401
== inference
PASS  chat completion (non-stream) -> HTTP 200
      answer: CANARY-OUT-c385915f5c4e
      usage : {"prompt_tokens":76,"total_tokens":93,"completion_tokens":17,"prompt_tokens_details":null,"completion_tokens_details":null}
PASS  model echoed the output canary (canary check is not vacuous)
PASS  chat completion (stream) -> HTTP 200
PASS  stream terminated with [DONE]
== error paths (every payload carries a canary)
PASS  prompt longer than max-model-len -> HTTP 400
PASS  malformed JSON body -> HTTP 400
PASS  wrong field type -> HTTP 400
PASS  unknown model name -> HTTP 404
PASS  messages is a string, not a list -> HTTP 400
PASS  invalid json_schema -> HTTP 400
PASS  aborted stream sent (client disconnect)
== log scan (all containers, current and previous)
      scanned 202 log lines
PASS  no canary (prompt / output / stop / error payloads) in any log
PASS  generated answer text not in the logs
PASS  request-fact lines present (18)
      (APIServer pid=10) INFO 10-24 08:11:10 [reqlog.py:201] request_id=req-e0105e1d4a3746e7 method=POST route=/v1/chat/completions status=200 outcome=ok latency_ms=1720.2 ttfb_ms=1719.8 req_bytes=142 resp_bytes=694 client=127.0.0.1 stream=false engi
      (APIServer pid=10) INFO 10-24 08:11:10 [reqlog.py:201] request_id=req-57bd4125205f43b4 method=POST route=/v1/chat/completions status=200 outcome=ok latency_ms=684.9 ttfb_ms=628.6 req_bytes=188 resp_bytes=1329 client=127.0.0.1 stream=true engine
      (APIServer pid=10) INFO 10-24 08:14:06 [reqlog.py:201] request_id=req-e4f3dd8e37d6450e method=GET route=/v1/models status=401 outcome=ok latency_ms=0.2 ttfb_ms=0.1 req_bytes=0 resp_bytes=77 client=127.0.0.1 stream=false engine_id=- model=- prom
      (APIServer pid=10) INFO 10-24 08:14:15 [reqlog.py:201] request_id=req-89a4d59eb6254850 method=POST route=/v1/chat/completions status=200 outcome=client_disconnected latency_ms=2001.9 ttfb_ms=865.3 req_bytes=246 resp_bytes=7430 client=127.0.0.1 

ALL CHECKS PASSED
$ number of records suppressed by the filter
75
$ sample suppressed lines
(APIServer pid=10) INFO 10-24 08:11:10 [request_logger.py:108] content-bearing log record suppressed (source=request_logger request=chatcmpl-87ef4863edcb3651)
(APIServer pid=10) INFO 10-24 08:11:10 [request_logger.py:65] content-bearing log record suppressed (source=request_logger request=chatcmpl-950f34afbb5207f5)
(APIServer pid=10) INFO 10-24 08:11:10 [request_logger.py:108] content-bearing log record suppressed (source=request_logger request=chatcmpl-950f34afbb5207f5)
$ sample sanitized error-path line
(APIServer pid=10) ERROR 10-24 08:14:13 [serving.py:274] Error with model <ErrorResponse>
```

### P3: negative control (privacy log config removed, stress overlay still on)

```text
$ kubectl -n vllm set env deploy/vllm VLLM_LOGGING_CONFIG_PATH=    # negative control: stock vLLM logging, stress overlay still active
deployment.apps/vllm env updated
deployment "vllm" successfully rolled out
$ scripts/verify-deployment.sh   (expected: FAIL in the log scan)
== log scan (all containers, current and previous)
      scanned 831 log lines
FAIL  a canary string appears in the logs:
526:(APIServer pid=10) DEBUG 10-24 08:15:49 [entrypoints/.../utils/request_logger.py:74] Request 9bb4557aefb4545d JSON body: {"messages":[{"content":"Remember this tag: CANARY-PROMPT-42f3e78628a7. Now repeat exactly this text and nothing el
527:(APIServer pid=10) DEBUG 10-24 08:15:49 [entrypoints/.../utils/request_logger.py:55] Request chatcmpl-9bb4557aefb4545d details: prompt: '<|im_start|>system\nYou are Qwen, created by Alibaba Cloud. You are a helpful assistant.<|im_end|>\
528:(APIServer pid=10) INFO 10-24 08:15:49 [entrypoints/.../utils/request_logger.py:65] Received request chatcmpl-9bb4557aefb4545d: params: SamplingParams(n=1, presence_penalty=0.0, frequency_penalty=0.0, repetition_penalty=1.1, temperature
573:(APIServer pid=10) INFO 10-24 08:15:51 [entrypoints/.../utils/request_logger.py:108] Generated response chatcmpl-9bb4557aefb4545d: output: 'CANARY-OUT-42f3e78628a7', finish_reason: stop
574:(APIServer pid=10) DEBUG 10-24 08:15:51 [entrypoints/.../utils/request_logger.py:74] Request 8271e0fdd7392180 JSON body: {"messages":[{"content":"Count from one to five. Tag CANARY-PROMPT-42f3e78628a7-stream","role":"user"}],"model":"qw
FAIL  generated answer text appears in the logs
FAIL  too few request-fact lines (0)

3 CHECK(S) FAILED
$ restore: remove the env override and the stress overlay
deployment.apps/vllm env updated
configmap/vllm-env configured
configmap/vllm-serve-config configured
deployment "vllm" successfully rolled out
$ effective config restored
max-model-len: 4096
enable-log-requests: false
INFO
```

## 6. Model cache and offline start (C1-C3)

```text
$ kubectl -n vllm delete pod -l app.kubernetes.io/name=vllm
pod "vllm-7547c4fd7b-vjml6" deleted from vllm namespace
deployment "vllm" successfully rolled out
(ready after 81 s)
$ kubectl -n vllm logs deploy/vllm -c fetch-model
[download-model] 08:04:03 acquiring lock /models/qwen2.5-0.5b-instruct.lock
[download-model] 08:04:03 model Qwen/Qwen2.5-0.5B-Instruct@7ae557604adf67be50417f59c2c2f167def9a775 already present in /models/qwen2.5-0.5b-instruct, skipping download
$ kubectl -n vllm logs deploy/vllm -c vllm | grep -E "warmup|Loading weights|Application startup"
(Worker pid=127) INFO 10-24 08:04:49 [default_loader.py:501] Loading weights took 0.82 seconds
(APIServer pid=10) INFO 10-24 08:04:52 [on.py:62] Application startup complete.
[warmup] 08:04:52 server healthy; warming up model qwen2.5-0.5b-instruct
[warmup] 08:04:54 step 'greedy' done in 1.7s
[warmup] 08:04:54 step 'sampled+stream' done in 0.7s
[warmup] 08:05:19 step 'long prefill' done in 24.0s
[warmup] 08:05:19 warm-up complete; ready
$ kubectl -n vllm get pod (age/restarts)
NAME                    READY   STATUS    RESTARTS   AGE
vllm-7547c4fd7b-gsh78   1/1     Running   0          81s
```

```text
$ kubectl -n vllm patch configmap vllm-env --type merge -p {"data":{"HF_ENDPOINT":"http://127.0.0.1:9"}}   # unreachable endpoint
configmap/vllm-env patched
$ kubectl -n vllm rollout restart deploy/vllm
deployment.apps/vllm restarted
deployment "vllm" successfully rolled out
(ready after 82 s)
$ kubectl -n vllm get pod -o jsonpath env HF_ENDPOINT of the initContainer
HF_ENDPOINT=http://127.0.0.1:9 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
$ kubectl -n vllm logs deploy/vllm -c fetch-model
[download-model] 08:05:35 acquiring lock /models/qwen2.5-0.5b-instruct.lock
[download-model] 08:05:35 model Qwen/Qwen2.5-0.5B-Instruct@7ae557604adf67be50417f59c2c2f167def9a775 already present in /models/qwen2.5-0.5b-instruct, skipping download
$ chat request after the restart
Two common and widely recognized colors are red and blue.
$ restore: kubectl apply -k k8s/overlays/minikube (drops HF_ENDPOINT)
["HF_ENDPOINT","MODEL_LOCAL_DIR","MODEL_REPO_ID","MODEL_REVISION","TRITON_CACHE_DIR","VLLM_CPU_OMP_THREADS_BIND","VLLM_LOGGING_LEVEL","VLLM_USE_V2_MODEL_RUNNER"]
```

```text
$ outbound connections of the vllm container (/proc/net/tcp, state ESTABLISHED, remote address decoded)
established connections: 0
non-loopback remotes: []
$ remove the test key again
configmap/vllm-env patched
["MODEL_LOCAL_DIR","MODEL_REPO_ID","MODEL_REVISION","TRITON_CACHE_DIR","VLLM_CPU_OMP_THREADS_BIND","VLLM_LOGGING_LEVEL","VLLM_USE_V2_MODEL_RUNNER"]
```

Hard restart of the whole cluster (happened during the session, recorded as found):

```text
$ docker inspect vllm-test (killed by the Docker Desktop restart)
ExitCode=137 OOMKilled=false (SIGKILL when Docker Desktop restarted)
$ minikube start -p vllm-test   # existing profile
* Включенные дополнения: default-storageclass, storage-provisioner
* Готово! kubectl настроен для использования кластера "vllm-test" и "default" п�
exit=0
$ kubectl -n vllm get pods,pvc
NAME                        READY   STATUS    RESTARTS      AGE
pod/vllm-648b747db7-r6rl5   1/1     Running   1 (92s ago)   3h44m

NAME                                   STATUS   VOLUME                                     CAPACITY   ACCESS MODES   STORAGECLASS   VOLUMEATTRIBUTESCLASS   AGE
persistentvolumeclaim/vllm-jit-cache   Bound    pvc-d0f7ed26-dff3-4eb5-92f5-925089a3ac54   1Gi        RWO            standard       <unset>                 4h
persistentvolumeclaim/vllm-models      Bound    pvc-2a994193-93c4-4057-bf08-6f5ebd3ec4ca   5Gi        RWO            standard       <unset>                 4h
$ kubectl -n vllm logs deploy/vllm -c fetch-model
[download-model] 11:59:15 acquiring lock /models/qwen2.5-0.5b-instruct.lock
[download-model] 11:59:15 model Qwen/Qwen2.5-0.5B-Instruct@7ae557604adf67be50417f59c2c2f167def9a775 already present in /models/qwen2.5-0.5b-instruct, skipping download
$ kubectl -n vllm logs deploy/vllm -c vllm | grep warmup
[warmup] 11:59:59 server healthy; warming up model qwen2.5-0.5b-instruct
[warmup] 12:00:01 step 'greedy' done in 2.0s
[warmup] 12:00:02 step 'sampled+stream' done in 0.8s
[warmup] 12:00:26 step 'long prefill' done in 24.2s
[warmup] 12:00:26 warm-up complete; ready
```

## 7. ConfigMap change without rebuilding (K1)

```text
$ sed -i s/max-model-len: 4096/max-model-len: 2048/ k8s/base/configmap-serve.yaml
 k8s/base/configmap-serve.yaml | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)
$ kubectl apply -k k8s/overlays/minikube
configmap/vllm-serve-config configured
$ kubectl -n vllm rollout restart deploy/vllm && rollout status
deployment.apps/vllm restarted
deployment "vllm" successfully rolled out
(ready after 82 s)
$ curl -s .../v1/models | jq .data[0].max_model_len
2048
$ git checkout k8s/base/configmap-serve.yaml  (undo)
Updated 1 path from the index
configmap/vllm-serve-config configured
deployment "vllm" successfully rolled out
```

## 8. Load test (L1)

Command (README, "Load testing"), through `kubectl port-forward`:

```text
python loadtest/loadtest.py --base-url http://127.0.0.1:18000 --api-key "$VLLM_API_KEY"   --profile short:64:64 --profile long:1024:32 --concurrency 1,2,4,8   --requests-per-worker 3 --min-requests 6 --warmup 2 --json-out loadtest/results/baseline.json
```

**Run 1** (`loadtest/results/baseline.json`; the final image and manifests on the first deployment, before the
clean-state run of sections 3-7). All 8 levels completed:

```text
[short] ~64 prompt tokens -> 8 words, 64 output tokens
[short] conc=1: 6/6 ok, TTFT p50 1476 ms, 10.8 out tok/s
[short] conc=2: 6/6 ok, TTFT p50 2168 ms, 14.6 out tok/s
[short] conc=4: 12/12 ok, TTFT p50 5304 ms, 17.7 out tok/s
[short] conc=8: 24/24 ok, TTFT p50 10850 ms, 17.9 out tok/s
[long] ~1024 prompt tokens -> 830 words, 32 output tokens
[long] conc=1: 6/6 ok, TTFT p50 19568 ms, 1.6 out tok/s
[long] conc=2: 6/6 ok, TTFT p50 28434 ms, 1.6 out tok/s
[long] conc=4: 12/12 ok, TTFT p50 58258 ms, 1.6 out tok/s
[long] conc=8: 24/24 ok, TTFT p50 81986 ms, 1.6 out tok/s
exit=0
```

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

**Run 2** (`loadtest/results/run2-rebuilt-cluster.json`; after `minikube delete`, rebuild and redeploy from the
README). The node container had been killed once by a Docker Desktop restart (section 6, C4) and the cluster
restarted; the run's progress lines, exactly as printed:

```text
[short] ~64 prompt tokens -> 8 words, 64 output tokens
[short] conc=1: 6/6 ok, TTFT p50 1284 ms, 12.2 out tok/s
[short] conc=2: 6/6 ok, TTFT p50 2269 ms, 12.1 out tok/s
[short] conc=4: 12/12 ok, TTFT p50 3888 ms, 16.6 out tok/s
[short] conc=8: 24/24 ok, TTFT p50 9774 ms, 19.7 out tok/s
[long] ~1024 prompt tokens -> 830 words, 32 output tokens
[long] conc=1: 6/6 ok, TTFT p50 18255 ms, 1.7 out tok/s
[long] conc=2: 6/6 ok, TTFT p50 27352 ms, 1.6 out tok/s
[long] conc=4: 12/12 ok, TTFT p50 54467 ms, 1.6 out tok/s
[long] conc=8: 0/24 ok, TTFT p50 - ms, 0.0 out tok/s
exit=1
```

The last level (long prompts, 8 clients) failed with `ConnectError`/`RemoteProtocolError` because Docker Desktop
itself became unavailable on the host while it was running (the `docker` pipe disappeared; the cause was not
established). The other seven levels agree with run 1 within about 15% (TTFT p50, short/1: 1284 vs 1476 ms;
long/1: 18255 vs 19568 ms; out tok/s, short/8: 19.7 vs 17.9).

Offline engine knob sweep (`scripts/engine-bench.sh`, `loadtest/results/engine_sweep.jsonl`):

```text
{"dtype": "bfloat16", "max_num_seqs": 16, "max_num_batched_tokens": 2048, "omp_threads_bind": "0,2,4,6,8", "prefill_prompt_tokens": 833, "prefill_tok_s": 68.5, "decode_tok_s": {"1": 12.6, "4": 23.6, "8": 28.8, "16": 29.5}}
{"dtype": "float32", "max_num_seqs": 16, "max_num_batched_tokens": 2048, "omp_threads_bind": "0,2,4,6,8", "prefill_prompt_tokens": 833, "prefill_tok_s": 252.6, "decode_tok_s": {"1": 7.7, "4": 28.4, "8": 47.2, "16": 74.6}}
{"dtype": "bfloat16", "max_num_seqs": 16, "max_num_batched_tokens": 2048, "omp_threads_bind": "0,2,4,6,8,10,12", "prefill_prompt_tokens": 833, "prefill_tok_s": 66.3, "decode_tok_s": {"1": 14.0, "4": 26.2, "8": 29.3, "16": 30.3}}
{"dtype": "float32", "max_num_seqs": 16, "max_num_batched_tokens": 2048, "omp_threads_bind": "0,2,4,6,8,10,12", "prefill_prompt_tokens": 833, "prefill_tok_s": 278.6, "decode_tok_s": {"1": 8.1, "4": 29.3, "8": 49.7, "16": 79.6}}
```

## 9. Development-time findings (observed, reproduced before they went into the README)

These were found while building the image; the quoted lines are the real messages.

* Stock vLLM logging leaks content (P3 above) and vLLM's `--api-key` leaves `/invocations` etc. open
  (documented by vLLM, closed by `BearerAuthGuard`, checked in A2).
* No C compiler in the image: first request fails, engine dies:
  `RuntimeError: Failed to find C compiler. Please specify via CC environment variable ...`
  (triton/runtime/build.py, called from `vllm/v1/worker/gpu/buffer_utils.py`). Fix: `gcc g++ libc6-dev`.
* `docker run --tmpfs /tmp` is `noexec`: `ImportError: /tmp/triton/.../__triton_cpu_launcher.so: failed to map segment from shared object`.
  Fix: `--tmpfs /tmp:rw,exec` in plain Docker; Kubernetes volumes are not affected.
* Model runner V2 (default): a `faulthandler` dump after 120 s shows the process still inside
  `triton/backends/cpu/compiler.py make_asm` (kernel `gumbel_sample`); with `VLLM_USE_V2_MODEL_RUNNER=0`
  the first generation takes 56-70 s and later ones 1.2 s per 16 tokens.
* Start-up memory pre-flight with a cold page cache and a 4 GiB limit:
  `ValueError: Available memory on node 0 (0.19/4.0 GiB) on kv cache allocation is less than requested memory for kv (0.25 GiB).`
  Fix: 5 GiB limit (resident memory is 3.3-3.6 GiB).
* `Failed to create oneDNN linear, fallback to torch linear` for bf16 on the AVX2-only CPU (explains the sweep).

## 10. Submission package (S1)

`scripts/make-submission.sh` builds `submission/<name>.zip` with `git archive` (committed files only: no `.git`, no
ignored or untracked files such as `secrets.env`). `scripts/check-submission.sh` extracts it into a temp dir and
checks it. Output of that check (run on the zip built from the commit preceding the final one; the final zip differs
only by this section):

```text
(pending)
```
