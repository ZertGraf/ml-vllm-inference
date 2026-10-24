#!/usr/bin/env bash
# Builds the fallback zip of the committed repository state (no .git, no
# untracked or ignored files such as secrets.env, caches or model weights).
#   scripts/make-submission.sh
set -euo pipefail
cd "$(dirname "$0")/.."
name="Мясников Д. Команда исследования технологий машинного обучения"
out="submission/${name}.zip"
rm -f "${out}"
git archive --format=zip --prefix=ml-vllm-inference/ -o "${out}" HEAD
echo "wrote ${out} ($(du -h "${out}" | cut -f1)) from commit $(git rev-parse --short HEAD)"
