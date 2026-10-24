#!/usr/bin/env python3
"""Closed-loop load test for an OpenAI-compatible vLLM endpoint.

Measures, per concurrency level and prompt profile, using streaming requests:

  TTFT       time from sending the request to the first generated token (ms)
  latency    time from sending the request to the end of the stream (ms)
  TPOT       mean time per output token after the first one (ms)
  out tok/s  generated tokens per second over the whole level (throughput)
  req/s      completed requests per second over the whole level

Every request gets a unique random prefix so the prefix cache never serves a
prompt from an earlier request (otherwise TTFT would be meaningless), and uses
`ignore_eos` + `max_tokens`, so every request generates exactly the same number
of tokens. Prompt/output token counts are taken from the server's `usage`
object, not estimated.

Example:
    python loadtest.py --base-url http://127.0.0.1:18000 --api-key "$VLLM_API_KEY" \
        --profile short:64:64 --profile long:1500:64 --concurrency 1,4,8 \
        --json-out results.json

Profile syntax: name:prompt_tokens:max_tokens
Requires: httpx (pip install httpx).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field

import httpx

WORDS = (
    "river market engine garden window copper signal forest harbor pattern lantern "
    "valley bridge compass ledger meadow orbit pencil quartz ribbon saddle timber "
    "umbrella velvet wagon yellow zephyr anchor beacon candle dagger ember falcon "
    "glacier hammer island jungle kettle ladder mirror needle oyster pillow quiver "
    "rocket shadow tunnel useful voyage willow xenon yonder zenith amber breeze"
).split()


@dataclass
class Sample:
    ok: bool
    ttft_ms: float = 0.0
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""


@dataclass
class LevelResult:
    profile: str
    concurrency: int
    requests: int
    ok: int
    failed: int
    wall_s: float
    req_per_s: float
    out_tok_per_s: float
    in_tok_per_s: float
    ttft_p50_ms: float
    ttft_p95_ms: float
    latency_p50_ms: float
    latency_p95_ms: float
    tpot_ms: float
    prompt_tokens_mean: float
    completion_tokens_mean: float
    errors: list[str] = field(default_factory=list)


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def make_prompt(n_words: int, rng: random.Random) -> str:
    # unique random head -> no shared prefix between requests (prefix cache miss)
    head = " ".join(rng.choices(WORDS, k=8)) + f" #{rng.getrandbits(48):x}."
    body = " ".join(rng.choices(WORDS, k=n_words))
    return f"{head} {body}\n\nWrite a continuation of the text above."


async def one_request(
    client: httpx.AsyncClient, url: str, headers: dict, model: str, n_words: int,
    max_tokens: int, rng: random.Random,
) -> Sample:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": make_prompt(n_words, rng)}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "ignore_eos": True,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.perf_counter()
    ttft = None
    usage: dict = {}
    chunks = 0
    try:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                await resp.aread()
                return Sample(False, error=f"HTTP {resp.status_code}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                obj = json.loads(data)
                if obj.get("usage"):
                    usage = obj["usage"]
                for ch in obj.get("choices", []):
                    if ch.get("delta", {}).get("content"):
                        chunks += 1
                        if ttft is None:
                            ttft = time.perf_counter() - t0
    except Exception as exc:  # noqa: BLE001 - report and continue
        return Sample(False, error=type(exc).__name__)
    lat = time.perf_counter() - t0
    if ttft is None:
        return Sample(False, error="no tokens")
    return Sample(
        True, ttft * 1000, lat * 1000,
        usage.get("prompt_tokens", 0), usage.get("completion_tokens", chunks),
    )


async def run_level(
    base_url: str, api_key: str, model: str, name: str, n_words: int, max_tokens: int,
    concurrency: int, n_requests: int, seed: int,
) -> LevelResult:
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    rng = random.Random(seed)
    remaining = n_requests
    samples: list[Sample] = []
    limits = httpx.Limits(max_connections=concurrency + 2)
    timeout = httpx.Timeout(900.0, connect=30.0)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        async def worker() -> None:
            nonlocal remaining
            while remaining > 0:
                remaining -= 1
                samples.append(
                    await one_request(client, url, headers, model, n_words, max_tokens, rng)
                )

        t0 = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        wall = time.perf_counter() - t0

    good = [s for s in samples if s.ok]
    ttfts = [s.ttft_ms for s in good]
    lats = [s.latency_ms for s in good]
    tpots = [
        (s.latency_ms - s.ttft_ms) / (s.completion_tokens - 1)
        for s in good if s.completion_tokens > 1
    ]
    out_tokens = sum(s.completion_tokens for s in good)
    in_tokens = sum(s.prompt_tokens for s in good)
    return LevelResult(
        profile=name, concurrency=concurrency, requests=len(samples), ok=len(good),
        failed=len(samples) - len(good), wall_s=wall,
        req_per_s=len(good) / wall if wall else 0.0,
        out_tok_per_s=out_tokens / wall if wall else 0.0,
        in_tok_per_s=in_tokens / wall if wall else 0.0,
        ttft_p50_ms=pct(ttfts, 50), ttft_p95_ms=pct(ttfts, 95),
        latency_p50_ms=pct(lats, 50), latency_p95_ms=pct(lats, 95),
        tpot_ms=statistics.fmean(tpots) if tpots else float("nan"),
        prompt_tokens_mean=statistics.fmean([s.prompt_tokens for s in good]) if good else 0.0,
        completion_tokens_mean=statistics.fmean([s.completion_tokens for s in good]) if good else 0.0,
        errors=sorted({s.error for s in samples if not s.ok}),
    )


async def calibrate(base_url: str, api_key: str, model: str, target_tokens: int) -> int:
    """Find how many random words give roughly `target_tokens` prompt tokens."""
    rng = random.Random(1)
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    words = max(4, target_tokens // 2)
    async with httpx.AsyncClient(timeout=httpx.Timeout(900.0)) as client:
        for _ in range(3):
            s = await one_request(client, url, headers, model, words, 1, rng)
            if not s.ok or s.prompt_tokens == 0:
                raise SystemExit(f"calibration request failed: {s.error or 'no usage'}")
            overhead = 30  # chat template + fixed instruction text
            per_word = max(0.5, (s.prompt_tokens - overhead) / words)
            new_words = max(1, int((target_tokens - overhead) / per_word))
            if abs(new_words - words) <= max(2, words // 50):
                return new_words
            words = new_words
    return words


def fmt(x: float, nd: int = 0) -> str:
    return "-" if x != x else f"{x:.{nd}f}"


def markdown(results: list[LevelResult]) -> str:
    rows = [
        "| profile | prompt tok | out tok | conc | reqs (ok/fail) | TTFT p50 (ms) | TTFT p95 (ms) "
        "| latency p50 (ms) | latency p95 (ms) | TPOT (ms) | req/s | out tok/s | in tok/s |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        rows.append(
            f"| {r.profile} | {r.prompt_tokens_mean:.0f} | {r.completion_tokens_mean:.0f} "
            f"| {r.concurrency} | {r.ok}/{r.failed} | {fmt(r.ttft_p50_ms)} | {fmt(r.ttft_p95_ms)} "
            f"| {fmt(r.latency_p50_ms)} | {fmt(r.latency_p95_ms)} | {fmt(r.tpot_ms, 1)} "
            f"| {fmt(r.req_per_s, 2)} | {fmt(r.out_tok_per_s, 1)} | {fmt(r.in_tok_per_s, 1)} |"
        )
    return "\n".join(rows)


async def main_async(args: argparse.Namespace) -> int:
    api_key = args.api_key or os.environ.get("VLLM_API_KEY", "")
    if not api_key:
        print("error: pass --api-key or set VLLM_API_KEY", file=sys.stderr)
        return 2
    levels = [int(c) for c in args.concurrency.split(",")]
    results: list[LevelResult] = []
    for spec in args.profile:
        name, ptoks, mtoks = spec.split(":")
        target, max_tokens = int(ptoks), int(mtoks)
        n_words = await calibrate(args.base_url, api_key, args.model, target)
        print(f"[{name}] ~{target} prompt tokens -> {n_words} words, {max_tokens} output tokens",
              file=sys.stderr)
        if args.warmup:
            await run_level(args.base_url, api_key, args.model, name, n_words,
                            max_tokens, 1, args.warmup, seed=0)
        for conc in levels:
            n_req = max(args.min_requests, conc * args.requests_per_worker)
            res = await run_level(args.base_url, api_key, args.model, name, n_words,
                                  max_tokens, conc, n_req, seed=conc)
            results.append(res)
            print(f"[{name}] conc={conc}: {res.ok}/{res.requests} ok, "
                  f"TTFT p50 {fmt(res.ttft_p50_ms)} ms, {fmt(res.out_tok_per_s, 1)} out tok/s",
                  file=sys.stderr)
    table = markdown(results)
    print(table)
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump({"args": {k: v for k, v in vars(args).items() if k != "api_key"},
                       "results": [asdict(r) for r in results], "markdown": table}, fh, indent=2)
    return 0 if all(r.failed == 0 for r in results) else 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:18000")
    p.add_argument("--api-key", default="", help="default: $VLLM_API_KEY")
    p.add_argument("--model", default="qwen2.5-0.5b-instruct", help="served model name")
    p.add_argument("--profile", action="append", required=True,
                   help="name:prompt_tokens:max_tokens (repeatable)")
    p.add_argument("--concurrency", default="1,4,8", help="comma separated levels")
    p.add_argument("--requests-per-worker", type=int, default=4,
                   help="requests per level = max(min-requests, concurrency * this)")
    p.add_argument("--min-requests", type=int, default=8)
    p.add_argument("--warmup", type=int, default=2, help="sequential warm-up requests per profile")
    p.add_argument("--json-out", default="")
    sys.exit(asyncio.run(main_async(p.parse_args())))


if __name__ == "__main__":
    main()
