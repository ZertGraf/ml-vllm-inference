# syntax=docker/dockerfile:1.7
#
# Lightweight, rootless image for the initContainer that downloads model
# weights from Hugging Face into the PVC. Only huggingface_hub and its
# (hash-pinned) dependencies are installed; no vLLM, no torch.
#
# Build context: docker/downloader
#   docker build -t vllm-model-downloader:1.0.0 docker/downloader

# python:3.12-slim-trixie, pinned by digest (re-pin deliberately, see README)
FROM python:3.12-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# every package (including transitive ones) is pinned and hash-checked
COPY requirements.txt /tmp/requirements.txt
RUN pip install --require-hashes --no-deps -r /tmp/requirements.txt \
    && pip check \
    && rm /tmp/requirements.txt

# unprivileged runtime user; no login shell, no home directory
RUN groupadd --gid 10001 downloader \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp \
       --shell /usr/sbin/nologin downloader

COPY --chown=root:root --chmod=0555 download_model.py /app/download_model.py

# the root filesystem can be read-only; huggingface_hub keeps its scratch data in /tmp
ENV HOME=/tmp \
    HF_HOME=/tmp/hf \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_DISABLE_PROGRESS_BARS=1

USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python", "/app/download_model.py"]
