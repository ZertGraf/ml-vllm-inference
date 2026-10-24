#!/usr/bin/env python3
"""Download a Hugging Face model snapshot into a persistent volume, exactly once.

Behaviour
---------
* If ``$MODEL_LOCAL_DIR`` already holds a complete copy of the requested model
  (completeness marker present, repo/revision match, every listed file has the
  recorded size) the script prints "already present, skipping download" and
  exits 0 *without touching the network*.
* Otherwise it resolves the revision to a commit sha, downloads the allowed
  files into ``$MODEL_LOCAL_DIR.partial`` (resumable), verifies the sha256 of
  every LFS file against the Hub metadata, writes the marker file and finally
  renames the directory into place. A pod killed mid-download therefore never
  leaves a half-written model that looks complete.
* A lock file on the volume serialises concurrent runs.
* ``HF_TOKEN`` is optional. Public models are downloaded anonymously when it is
  empty/unset (an invalid token makes the Hub answer 401 even for public repos,
  so an empty value must not be sent). The token is never printed.

Configuration (environment variables)
-------------------------------------
MODEL_REPO_ID         required, e.g. Qwen/Qwen2.5-0.5B-Instruct
MODEL_LOCAL_DIR       required, target directory on the volume
MODEL_REVISION        branch, tag or commit sha (default: main); a full sha is
                      recommended for reproducibility
MODEL_ALLOW_PATTERNS  comma separated globs (default: json, safetensors, txt,
                      model, tiktoken, jinja files)
HF_TOKEN              optional access token (needed for gated/private repos)
HF_ENDPOINT           optional mirror/endpoint override
DOWNLOAD_RETRIES      attempts for transient network errors (default: 5)
VERIFY_SHA256         "1" (default) / "0"

Exit codes: 0 ok, 1 download/verification failure, 2 configuration error.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

MARKER = ".download-complete.json"
DEFAULT_ALLOW = "*.json,*.safetensors,*.txt,*.model,*.tiktoken,*.jinja"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def log(msg: str) -> None:
    print(f"[download-model] {time.strftime('%H:%M:%S')} {msg}", flush=True)


@dataclass(frozen=True)
class Config:
    repo_id: str
    local_dir: Path
    revision: str
    allow_patterns: list[str]
    token: str | None
    endpoint: str | None
    retries: int
    verify: bool

    @staticmethod
    def from_env() -> Config:
        repo_id = os.environ.get("MODEL_REPO_ID", "").strip()
        local_dir = os.environ.get("MODEL_LOCAL_DIR", "").strip()
        if not repo_id or not local_dir:
            log("MODEL_REPO_ID and MODEL_LOCAL_DIR must be set")
            sys.exit(2)
        patterns = os.environ.get("MODEL_ALLOW_PATTERNS", DEFAULT_ALLOW)
        return Config(
            repo_id=repo_id,
            local_dir=Path(local_dir),
            revision=os.environ.get("MODEL_REVISION", "").strip() or "main",
            allow_patterns=[p.strip() for p in patterns.split(",") if p.strip()],
            token=(os.environ.get("HF_TOKEN", "").strip() or None),
            endpoint=(os.environ.get("HF_ENDPOINT", "").strip() or None),
            retries=int(os.environ.get("DOWNLOAD_RETRIES", "5")),
            verify=os.environ.get("VERIFY_SHA256", "1") != "0",
        )


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    with open(path, "w") as fh:
        log(f"acquiring lock {path}")
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def is_complete(cfg: Config) -> bool:
    """True if local_dir holds a finished download of the requested model."""
    marker = cfg.local_dir / MARKER
    if not marker.is_file():
        return False
    try:
        meta = json.loads(marker.read_text())
    except (OSError, ValueError):
        return False
    if meta.get("repo_id") != cfg.repo_id:
        return False
    if meta.get("requested_revision") != cfg.revision:
        return False
    files = meta.get("files")
    if not isinstance(files, dict) or not files:
        return False
    for rel, size in files.items():
        f = cfg.local_dir / rel
        if not f.is_file() or f.stat().st_size != size:
            return False
    return True


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class FatalDownloadError(Exception):
    """Errors that retrying cannot fix (auth, not found, bad checksum)."""


def download(cfg: Config) -> None:
    # imported lazily so that the "already present" path needs no network stack
    from huggingface_hub import HfApi, snapshot_download
    from huggingface_hub.errors import (
        GatedRepoError,
        HfHubHTTPError,
        RepositoryNotFoundError,
        RevisionNotFoundError,
    )
    from huggingface_hub.utils import filter_repo_objects

    partial = cfg.local_dir.with_name(cfg.local_dir.name + ".partial")
    api = HfApi(token=cfg.token, endpoint=cfg.endpoint)

    try:
        info = api.model_info(cfg.repo_id, revision=cfg.revision, files_metadata=True)
    except (GatedRepoError, RepositoryNotFoundError, RevisionNotFoundError) as exc:
        raise FatalDownloadError(
            f"cannot access {cfg.repo_id}@{cfg.revision}: {type(exc).__name__}. "
            "For gated/private repos set HF_TOKEN; for public repos leave it empty."
        ) from exc
    except HfHubHTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        if status in (401, 403, 404):
            raise FatalDownloadError(
                f"Hub answered HTTP {status} for {cfg.repo_id}@{cfg.revision} "
                "(an invalid HF_TOKEN gives 401 even for public repos)"
            ) from exc
        raise

    commit = info.sha
    wanted = list(
        filter_repo_objects(
            items=[s.rfilename for s in (info.siblings or [])],
            allow_patterns=cfg.allow_patterns,
        )
    )
    expected_sha = {
        s.rfilename: s.lfs.sha256
        for s in (info.siblings or [])
        if s.rfilename in wanted and getattr(s, "lfs", None) is not None
    }
    log(f"resolved {cfg.repo_id}@{cfg.revision} -> {commit}; {len(wanted)} file(s)")

    log(f"downloading to {partial} (resumable)")
    snapshot_download(
        repo_id=cfg.repo_id,
        revision=commit,
        local_dir=str(partial),
        allow_patterns=cfg.allow_patterns,
        token=cfg.token,
        endpoint=cfg.endpoint,
        max_workers=4,
    )
    shutil.rmtree(partial / ".cache", ignore_errors=True)

    if cfg.verify:
        for rel, want in expected_sha.items():
            got = sha256_of(partial / rel)
            if got != want:
                shutil.rmtree(partial, ignore_errors=True)
                raise FatalDownloadError(f"sha256 mismatch for {rel}")
            log(f"sha256 ok: {rel}")

    files = {
        str(p.relative_to(partial)): p.stat().st_size
        for p in sorted(partial.rglob("*"))
        if p.is_file()
    }
    missing = [w for w in wanted if w not in files]
    if missing:
        raise FatalDownloadError(f"files missing after download: {missing}")
    marker = {
        "repo_id": cfg.repo_id,
        "requested_revision": cfg.revision,
        "commit": commit,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
    }
    with open(partial / MARKER, "w") as fh:
        json.dump(marker, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())

    # atomic publish: a directory without the marker is never served
    if cfg.local_dir.exists():
        shutil.rmtree(cfg.local_dir)
    os.rename(partial, cfg.local_dir)
    log(f"model ready in {cfg.local_dir} ({sum(files.values()) / 1e6:.1f} MB)")


def main() -> int:
    cfg = Config.from_env()
    cfg.local_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = cfg.local_dir.with_name(cfg.local_dir.name + ".lock")

    with locked(lock_path):
        if is_complete(cfg):
            log(
                f"model {cfg.repo_id}@{cfg.revision} already present in "
                f"{cfg.local_dir}, skipping download"
            )
            return 0
        log(
            f"no complete copy of {cfg.repo_id}@{cfg.revision} in {cfg.local_dir}; "
            f"token {'provided' if cfg.token else 'not provided (anonymous)'}"
        )
        for attempt in range(1, cfg.retries + 1):
            try:
                download(cfg)
                return 0
            except FatalDownloadError as exc:
                log(f"FATAL: {exc}")
                return 1
            except Exception as exc:  # transient network/IO problems
                log(f"attempt {attempt}/{cfg.retries} failed: {type(exc).__name__}: {str(exc)[:300]}")
                if attempt == cfg.retries:
                    log("giving up")
                    return 1
                time.sleep(min(60, 2**attempt))
    return 1


if __name__ == "__main__":
    sys.exit(main())
