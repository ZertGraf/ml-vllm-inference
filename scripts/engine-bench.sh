#!/usr/bin/env bash
# Runs loadtest/engine_bench.py in the vLLM image with a Docker volume that
# already holds the model (see README, "Knob sweep"). Example:
#   DTYPE=float32 THREADS=0,2,4,6,8 scripts/engine-bench.sh
# Needs the volume filled once:
#   docker volume create vllm-dev-models
#   docker run --rm -v vllm-dev-models:/m alpine chown 10001:10001 /m
#   docker run --rm --user 10001:10001 -v vllm-dev-models:/models \
#     -e MODEL_REPO_ID=Qwen/Qwen2.5-0.5B-Instruct -e MODEL_LOCAL_DIR=/models/qwen2.5-0.5b-instruct \
#     -e MODEL_REVISION=7ae557604adf67be50417f59c2c2f167def9a775 vllm-model-downloader:1.0.0
set -euo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: do not rewrite /models etc.
cd "$(dirname "$0")/.."
docker volume create vllm-dev-jit >/dev/null
docker run --rm -m 5g --cpus "${CPUS:-6}" \
  --tmpfs /tmp:rw,exec,size=1g --tmpfs /dev/shm:size=1g \
  -v vllm-dev-models:/models:ro -v vllm-dev-jit:/cache \
  -v "$(pwd -W 2>/dev/null || pwd)/loadtest/engine_bench.py:/bench.py:ro" \
  -e MODEL_LOCAL_DIR=/models/qwen2.5-0.5b-instruct -e TRITON_CACHE_DIR=/cache/triton \
  -e VLLM_CPU_OMP_THREADS_BIND="${THREADS:-0,2,4,6,8}" -e DTYPE="${DTYPE:-bfloat16}" \
  -e MAX_NUM_SEQS="${MAX_NUM_SEQS:-16}" -e MAX_BATCHED_TOKENS="${MAX_BATCHED_TOKENS:-2048}" \
  -e VLLM_ENABLE_V1_MULTIPROCESSING=0 -e VLLM_USE_V2_MODEL_RUNNER=0 \
  --user 10001:10001 --entrypoint python "${VLLM_IMAGE:-vllm-cpu:0.32.0}" /bench.py 2>&1 | tr '\r' '\n' | grep -a "^RESULT"
