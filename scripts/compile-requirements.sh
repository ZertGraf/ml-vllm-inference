#!/usr/bin/env bash
# Regenerates the fully pinned, hash-locked requirements.txt files.
#
# Resolution runs inside the very base image the Dockerfiles use, so markers
# (python 3.12, linux x86_64, glibc 2.41) are exactly the runtime ones.
# Usage: scripts/compile-requirements.sh [vllm|downloader|all]
set -euo pipefail

BASE_IMAGE="python:3.12-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704"
UV_VERSION="0.11.19"
TARGET="${1:-all}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

compile() {
  local dir="$1"; shift
  echo ">>> compiling docker/${dir}/requirements.in"
  MSYS_NO_PATHCONV=1 docker run --rm -v "${ROOT}/docker/${dir}:/work" -w /work "${BASE_IMAGE}" sh -c "
    pip install --quiet --no-cache-dir uv==${UV_VERSION} &&
    uv pip compile requirements.in --generate-hashes --python-version 3.12 \
      --no-header -o requirements.txt $*"
}

case "${TARGET}" in
  vllm|all)       compile vllm --torch-backend cpu --index-strategy unsafe-best-match ;;&
  downloader|all) compile downloader ;;
esac
