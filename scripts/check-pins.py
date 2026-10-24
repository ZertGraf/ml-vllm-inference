#!/usr/bin/env python3
"""Check that an image contains exactly the packages pinned in a requirements.txt.

    python scripts/check-pins.py docker/vllm/requirements.txt vllm-cpu:0.32.0

Compares `pip list` inside the image with the pins (name and version, both
directions). Exits non-zero on any difference. Needs docker on the host.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pinned(path: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip().rstrip("\\").strip()
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;]+)", line)
        if m:
            pins[norm(m.group(1))] = m.group(2)
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+) @ \S+/([^/\s]+?)-(\d[^-]*)-[^/\s]*\.whl", line)
        if m:  # direct URL wheels: name @ https://.../name-version-tags.whl
            pins[norm(m.group(1))] = m.group(3).replace("%2B", "+")
    return pins


def installed(image: str) -> dict[str, str]:
    out = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "pip", image, "list", "--format=json"],
        check=True, capture_output=True, text=True,
    ).stdout
    return {norm(p["name"]): p["version"] for p in json.loads(out)}


def main() -> int:
    want, have = pinned(sys.argv[1]), installed(sys.argv[2])
    # pip / its own tooling come from the base image, not from the pins
    base_tools = {"pip"}
    bad = 0
    for name, ver in sorted(want.items()):
        if have.get(name) != ver:
            print(f"MISMATCH {name}: pinned {ver}, installed {have.get(name)}")
            bad += 1
    for name, ver in sorted(have.items()):
        if name not in want and name not in base_tools:
            print(f"UNPINNED {name}=={ver} is installed but not pinned")
            bad += 1
    print(f"pinned packages: {len(want)}, installed packages: {len(have)}, differences: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
