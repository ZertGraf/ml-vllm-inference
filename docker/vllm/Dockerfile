# syntax=docker/dockerfile:1.7
#
# Rootless vLLM 0.32.0 image for CPU-only inference.
#
# * base image pinned by digest (python:3.12-slim-trixie, glibc 2.41; the
#   official 0.32.0 CPU wheel needs manylinux_2_39 or newer)
# * every Python dependency, including transitive ones, is pinned with hashes in
#   requirements.txt and installed with --require-hashes --no-deps
# * vLLM itself is the official prebuilt CPU wheel of the GitHub release
#   (listed in requirements.txt with its sha256). It is not compiled here, but
#   0.32 JIT-compiles Triton-CPU kernels at run time, so gcc/g++ are installed
#   (the official vLLM CPU image ships a compiler for the same reason)
# * runs as UID/GID 10001, nothing needs root at runtime, the root filesystem
#   may be read-only (all writable paths are under /tmp)
# * privacy logging + auth guard are shipped as the `vllm_hardening` package
#   and activated through VLLM_LOGGING_CONFIG_PATH / --middleware
#
# Build context: docker/vllm
#   docker build -t vllm-cpu:0.32.0 docker/vllm

FROM python:3.12-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Python dependencies: exact versions + sha256 for every file.
# --extra-index-url is only the source of the +cpu torch wheel; the hashes
# decide what is accepted.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --require-hashes --no-deps \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r /tmp/requirements.txt \
    && pip check \
    && rm /tmp/requirements.txt

# System packages (versions come from the Debian suite of the pinned base image
# and are not hash-pinned; only the Python side is):
#   libnuma1            NUMA-aware thread binding of the CPU backend
#   gcc g++ libc6-dev   Triton-CPU compiles its kernels on first use; runs as the
#                       unprivileged user, caches in $TRITON_CACHE_DIR (/tmp)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libnuma1 gcc g++ libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# unprivileged runtime user: no login shell, no home directory
RUN groupadd --gid 10001 vllm \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp \
       --shell /usr/sbin/nologin vllm

# privacy logging, request-fact logging and auth guard (root-owned, read-only)
COPY --chown=root:root --chmod=0755 vllm_hardening /opt/vllm_hardening/vllm_hardening
COPY --chown=root:root --chmod=0444 logging_config.json /opt/vllm_hardening/logging_config.json
COPY --chown=root:root --chmod=0555 entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chown=root:root --chmod=0555 ready.sh /usr/local/bin/ready.sh
COPY --chown=root:root --chmod=0555 tests /opt/vllm_hardening/tests

ENV PYTHONPATH=/opt/vllm_hardening \
    VLLM_LOGGING_CONFIG_PATH=/opt/vllm_hardening/logging_config.json \
    VLLM_CONFIGURE_LOGGING=1 \
    VLLM_NO_USAGE_STATS=1 \
    VLLM_DO_NOT_TRACK=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HOME=/tmp \
    HF_HOME=/tmp/hf \
    VLLM_CACHE_ROOT=/tmp/vllm-cache \
    XDG_CACHE_HOME=/tmp/.cache \
    TORCHINDUCTOR_CACHE_DIR=/tmp/torchinductor \
    TRITON_CACHE_DIR=/tmp/triton \
    MODEL_LOCAL_DIR=/models/model

# compile all modules now so that a read-only filesystem never needs to write .pyc
RUN python -m compileall -q /opt/vllm_hardening >/dev/null

USER 10001:10001
WORKDIR /tmp
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD []
