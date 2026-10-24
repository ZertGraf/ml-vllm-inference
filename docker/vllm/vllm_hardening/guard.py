"""Deny-by-default Bearer authentication for the whole HTTP surface.

vLLM's built-in ``--api-key`` / ``VLLM_API_KEY`` protects only the ``/v1``,
``/v2``, ``/inference`` and ``/cohere`` prefixes. Other routes, notably
``/invocations`` (same inference capability as ``/v1``), ``/score``,
``/pooling``, ``/pause`` and ``/abort_requests`` stay open (see
https://docs.vllm.ai/en/latest/usage/security.html). This middleware closes
that gap: every request must carry ``Authorization: Bearer <VLLM_API_KEY>``
unless its path is in the public allow-list (default: ``/health``, needed by
the kubelet probes).

Fail-closed: the server refuses to start if ``VLLM_API_KEY`` is empty.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

_PUBLIC = frozenset(
    p.strip()
    for p in os.environ.get("VLLM_HARDENING_PUBLIC_PATHS", "/health").split(",")
    if p.strip()
)

_UNAUTHORIZED_BODY = b'{"error":{"message":"Unauthorized","type":"authentication_error","code":401}}'


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


class BearerAuthGuard:
    def __init__(self, app: Any) -> None:
        self.app = app
        keys = [k.strip() for k in os.environ.get("VLLM_API_KEY", "").split(",")]
        self._digests = [_digest(k) for k in keys if k]
        if not self._digests:
            raise RuntimeError(
                "vllm_hardening.guard: VLLM_API_KEY is empty; refusing to start "
                "an unauthenticated server"
            )

    def _authorized(self, scope: dict) -> bool:
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                parts = value.decode("latin-1").split(" ", 1)
                if len(parts) == 2 and parts[0].lower() == "bearer":
                    presented = _digest(parts[1].strip())
                    # compare against every key without early exit
                    ok = False
                    for expected in self._digests:
                        ok |= hmac.compare_digest(presented, expected)
                    return ok
        return False

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket") or scope.get("path") in _PUBLIC:
            await self.app(scope, receive, send)
            return
        if self._authorized(scope):
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_UNAUTHORIZED_BODY)).encode()),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})
