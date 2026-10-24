#!/usr/bin/env python3
"""Offline engine micro-benchmark: how much do individual vLLM knobs matter on this CPU?

Runs inside the vLLM image (no HTTP, no Kubernetes), loads the model once with the
settings given in the environment and reports

  prefill tok/s   one long prompt, max_tokens=1
  decode tok/s    N concurrent short prompts, 64 new tokens each, N in BATCHES

It exists to back the tuning analysis in the README with measurements; the
official numbers (TTFT, latency, throughput through the real endpoint) come from
loadtest.py. Use scripts/engine-bench.sh to run it in Docker.

Environment: MODEL_LOCAL_DIR (required), DTYPE (bfloat16), MAX_NUM_SEQS (16),
MAX_BATCHED_TOKENS (2048), LONG_WORDS (800), BATCHES ("1,4,8,16").
"""

from __future__ import annotations

import json
import os
import random
import time

from vllm import LLM, SamplingParams

WORDS = (
    "river market engine garden window copper signal forest harbor pattern lantern "
    "valley bridge compass ledger meadow orbit pencil quartz ribbon saddle timber"
).split()


def main() -> None:
    dtype = os.environ.get("DTYPE", "bfloat16")
    max_seqs = int(os.environ.get("MAX_NUM_SEQS", "16"))
    max_batched = int(os.environ.get("MAX_BATCHED_TOKENS", "2048"))
    long_words = int(os.environ.get("LONG_WORDS", "800"))
    batches = [int(b) for b in os.environ.get("BATCHES", "1,4,8,16").split(",")]

    llm = LLM(
        model=os.environ["MODEL_LOCAL_DIR"],
        dtype=dtype,
        max_model_len=4096,
        max_num_seqs=max_seqs,
        max_num_batched_tokens=max_batched,
        compilation_config={"mode": 0},
        kv_cache_memory_bytes=268435456,
        enable_prefix_caching=False,  # every measurement must really compute
    )
    rng = random.Random(7)

    def prompt(n_words: int) -> str:
        return " ".join(rng.choices(WORDS, k=n_words))

    decode = SamplingParams(max_tokens=64, temperature=0, ignore_eos=True)
    llm.generate([prompt(20)], decode, use_tqdm=False)  # JIT warm-up

    out: dict = {"dtype": dtype, "max_num_seqs": max_seqs, "max_num_batched_tokens": max_batched,
                 "omp_threads_bind": os.environ.get("VLLM_CPU_OMP_THREADS_BIND")}
    p = prompt(long_words)
    t = time.perf_counter()
    res = llm.generate([p], SamplingParams(max_tokens=1, temperature=0), use_tqdm=False)
    dt = time.perf_counter() - t
    n = len(res[0].prompt_token_ids)
    out["prefill_prompt_tokens"] = n
    out["prefill_tok_s"] = round(n / dt, 1)

    out["decode_tok_s"] = {}
    for b in batches:
        prompts = [prompt(20) for _ in range(b)]
        t = time.perf_counter()
        res = llm.generate(prompts, decode, use_tqdm=False)
        dt = time.perf_counter() - t
        toks = sum(len(r.outputs[0].token_ids) for r in res)
        out["decode_tok_s"][str(b)] = round(toks / dt, 1)
    print("RESULT " + json.dumps(out), flush=True)


if __name__ == "__main__":
    main()
