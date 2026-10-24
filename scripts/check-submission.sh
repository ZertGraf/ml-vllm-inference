#!/usr/bin/env bash
# Extracts the submission zip into a temp dir and checks it: required files present,
# no .git / secrets / weights / caches, no copy of the local API token or an HF token.
#   scripts/check-submission.sh
set -uo pipefail
cd "$(dirname "$0")/.."
name="Мясников Д. Команда исследования технологий машинного обучения"
zip="submission/${name}.zip"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
fails=0
ok()   { echo "PASS  $*"; }
bad()  { echo "FAIL  $*"; fails=$((fails + 1)); }

[ -f "${zip}" ] || { echo "no ${zip}; run scripts/make-submission.sh"; exit 2; }
python -c "import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "${zip}" "${tmp}" || { echo "cannot extract"; exit 2; }
root="${tmp}/ml-vllm-inference"
echo "zip: $(du -h "${zip}" | cut -f1), $(find "${root}" -type f | wc -l) files"

for f in README.md VERIFICATION.md docker/vllm/Dockerfile docker/vllm/requirements.txt \
         docker/downloader/Dockerfile docker/downloader/requirements.txt docker/downloader/download_model.py \
         docker/vllm/logging_config.json docker/vllm/vllm_hardening/logfilter.py \
         k8s/base/kustomization.yaml k8s/base/deployment.yaml k8s/base/configmap-serve.yaml \
         k8s/overlays/minikube/kustomization.yaml k8s/overlays/minikube/secrets.env.example \
         loadtest/loadtest.py scripts/verify-deployment.sh; do
  [ -f "${root}/${f}" ] && ok "present: ${f}" || bad "missing: ${f}"
done
[ -f "${root}/submission/${name}.txt" ] && ok "present: submission/<name>.txt" || bad "missing: submission/<name>.txt"
grep -qE '^Repository: https://github.com/[^[:space:]]+$' "${root}/submission/${name}.txt" && ok "submission txt has the repository link" || bad "no repository link in the submission txt"

[ ! -e "${root}/.git" ] && ok "no .git directory" || bad ".git inside the zip"
if find "${root}" \( -name 'secrets.env' -o -name '*.safetensors' -o -name '*.gguf' -o -name '*.bin' -o -name '__pycache__' -o -name '*.pyc' -o -name '.env' \) | grep -q .; then
  bad "forbidden files:"; find "${root}" \( -name 'secrets.env' -o -name '*.safetensors' -o -name '*.gguf' -o -name '*.bin' -o -name '__pycache__' -o -name '*.pyc' -o -name '.env' \)
else ok "no secrets.env / weights / caches"; fi
if find "${root}" -type f -size +5M | grep -q .; then bad "file larger than 5 MB:"; find "${root}" -type f -size +5M; else ok "no file larger than 5 MB"; fi

local_secrets="k8s/overlays/minikube/secrets.env"
if [ -f "${local_secrets}" ]; then
  tok="$(grep '^VLLM_API_KEY=' "${local_secrets}" | cut -d= -f2-)"
  if [ -n "${tok}" ] && grep -rqF -- "${tok}" "${root}"; then bad "the local API token appears in the zip"; else ok "the local API token is not in the zip"; fi
fi
if grep -rEq 'hf_[A-Za-z0-9]{30,}' "${root}"; then bad "something that looks like a Hugging Face token"; else ok "no Hugging Face token pattern"; fi
echo
[ "${fails}" -eq 0 ] && echo "SUBMISSION CHECK PASSED" || echo "${fails} CHECK(S) FAILED"
exit "${fails}"
