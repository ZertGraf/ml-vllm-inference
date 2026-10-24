#!/usr/bin/env bash
# Builds both images from the repo checkout.
#   scripts/build-images.sh
# Override the tags with VLLM_IMAGE / DOWNLOADER_IMAGE (keep them in sync with
# the `image:` fields in k8s/base/deployment.yaml).
set -euo pipefail
cd "$(dirname "$0")/.."
VLLM_IMAGE="${VLLM_IMAGE:-vllm-cpu:0.32.0}"
DOWNLOADER_IMAGE="${DOWNLOADER_IMAGE:-vllm-model-downloader:1.0.0}"

# --provenance=false keeps the result a plain single-platform image, which
# `minikube image load` handles without surprises.
docker build --provenance=false -t "${DOWNLOADER_IMAGE}" docker/downloader
docker build --provenance=false -t "${VLLM_IMAGE}" docker/vllm
