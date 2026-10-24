#!/bin/sh
# Entrypoint of the vLLM image.
#
# Everything tunable comes from outside the image:
#   * /etc/vllm/serve.yaml  - `vllm serve --config` file (ConfigMap in k8s)
#   * environment           - VLLM_CPU_OMP_THREADS_BIND, VLLM_USE_V2_MODEL_RUNNER, ...
#   * VLLM_API_KEY          - Bearer token clients must present (Secret in k8s)
# The weights are read from $MODEL_LOCAL_DIR on the volume; nothing is ever
# downloaded here (HF_HUB_OFFLINE=1 is baked into the image).
#
# The server runs as a child; a warm-up client (vllm_hardening.warmup) runs next
# to it and creates $WARMUP_DONE_FILE when the JIT kernels are compiled, which
# the readiness probe (ready.sh) waits for. SIGTERM is forwarded to the server.
set -eu

model_dir="${MODEL_LOCAL_DIR:?MODEL_LOCAL_DIR is not set}"
serve_config="${VLLM_SERVE_CONFIG:-/etc/vllm/serve.yaml}"
marker="${model_dir}/.download-complete.json"
export WARMUP_DONE_FILE="${WARMUP_DONE_FILE:-/tmp/warmup.done}"

log() { echo "[entrypoint] $*"; }

if [ ! -f "${marker}" ]; then
  log "ERROR: no complete model in ${model_dir} (missing ${marker##*/})."
  log "Run the downloader (initContainer / docker/downloader) first."
  exit 1
fi
: "${VLLM_API_KEY:?VLLM_API_KEY is not set; refusing to serve without authentication}"
if [ ! -f "${serve_config}" ]; then
  log "ERROR: serve config ${serve_config} not found"
  exit 1
fi

log "model dir : ${model_dir}"
log "config    : ${serve_config}"
log "uid/gid   : $(id -u)/$(id -g)"
rm -f "${WARMUP_DONE_FILE}"

# CLI arguments given to the container override values from the config file
vllm serve "${model_dir}" --config "${serve_config}" "$@" &
server=$!
trap 'kill -TERM "${server}" 2>/dev/null || true' TERM INT
python -m vllm_hardening.warmup &

rc=0
while kill -0 "${server}" 2>/dev/null; do
  wait "${server}" || rc=$?
done
exit "${rc}"
