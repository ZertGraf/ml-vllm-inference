"""Warm-up client run by the entrypoint next to the server.

vLLM 0.32 on CPU JIT-compiles its Triton-CPU kernels the first time a request
needs them (about a minute on a laptop-class CPU, see README). Without this
step the first real user would pay for it, and the pod would already be
"Ready". The entrypoint therefore runs this script in the background: it waits
for /health, sends a few fixed, content-free requests that touch the code paths
used in production (greedy and sampled decoding, streaming, a multi-chunk
prefill) and then creates $WARMUP_DONE_FILE, which the readiness probe checks.

Compiled kernels are cached in $TRITON_CACHE_DIR; when that directory is on a
persistent volume the warm-up takes seconds after the first start.

Environment: VLLM_API_KEY (required), WARMUP_PORT (8000), WARMUP_TIMEOUT_S (1200),
WARMUP_DONE_FILE (/tmp/warmup.done), WARMUP_ENABLED ("1").
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

PORT = int(os.environ.get("WARMUP_PORT", "8000"))
BASE = f"http://127.0.0.1:{PORT}"
DONE = os.environ.get("WARMUP_DONE_FILE", "/tmp/warmup.done")
TIMEOUT_S = float(os.environ.get("WARMUP_TIMEOUT_S", "1200"))
KEY = os.environ.get("VLLM_API_KEY", "")


def log(msg: str) -> None:
    print(f"[warmup] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def call(path: str, body: dict | None = None, timeout: float = 900.0) -> bytes:
    req = urllib.request.Request(
        BASE + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def main() -> int:
    if os.environ.get("WARMUP_ENABLED", "1") != "1":
        open(DONE, "w").close()
        log("disabled; marked ready")
        return 0
    deadline = time.time() + TIMEOUT_S
    while True:
        try:
            urllib.request.urlopen(BASE + "/health", timeout=3).read()
            break
        except Exception:
            if time.time() > deadline:
                log("server did not become healthy in time")
                return 1
            time.sleep(2)
    model = json.loads(call("/v1/models"))["data"][0]["id"]
    log(f"server healthy; warming up model {model}")

    filler = " ".join(["warm-up"] * 700)  # a prompt longer than one prefill chunk
    steps = [
        ("greedy", {"max_tokens": 8, "temperature": 0}),
        ("sampled+stream", {"max_tokens": 16, "temperature": 0.7, "top_p": 0.9,
                            "top_k": 20, "stream": True}),
        ("long prefill", {"max_tokens": 4, "temperature": 0,
                          "messages_content": filler}),
    ]
    for name, extra in steps:
        t0 = time.time()
        extra = dict(extra)
        content = extra.pop("messages_content", "Reply with one short word.")
        body = {"model": model, "messages": [{"role": "user", "content": content}], **extra}
        try:
            call("/v1/chat/completions", body)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"step '{name}' failed: {type(exc).__name__}")
            return 1
        log(f"step '{name}' done in {time.time() - t0:.1f}s")
    open(DONE, "w").close()
    log("warm-up complete; ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
