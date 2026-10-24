#!/usr/bin/env bash
# Creates k8s/overlays/minikube/secrets.env (git-ignored) with a random API token.
# An existing file is never overwritten.
#   HF_TOKEN=hf_xxx scripts/make-secrets.sh     # for gated models
#   scripts/make-secrets.sh                       # public model, anonymous download
set -euo pipefail
target="$(cd "$(dirname "$0")/.." && pwd)/k8s/overlays/minikube/secrets.env"
if [ -e "${target}" ]; then
  echo "${target} already exists; leaving it untouched"
  exit 0
fi
token="$(head -c 32 /dev/urandom | base64 | tr -d '=+/\n' | cut -c1-43)"
umask 077
printf 'HF_TOKEN=%s\nVLLM_API_KEY=%s\n' "${HF_TOKEN:-}" "${token}" > "${target}"
echo "wrote ${target} (API token generated, not shown)"
