"""Content-free request logging as a pure ASGI middleware.

Writes exactly one INFO line per served request, e.g.

    request_id=req-3f1c0a9d2b7e4a65 method=POST route=/v1/chat/completions
    status=200 outcome=ok latency_ms=812.4 ttfb_ms=790.1 req_bytes=187
    resp_bytes=902 client=10.244.0.1 stream=false engine_id=chatcmpl-9c1e...
    model=qwen2.5-0.5b-instruct prompt_tokens=23 completion_tokens=41

Only metadata is logged. The request body is never read (only its size is
counted); from the response only ``id``, ``model`` and the ``usage`` counters
are extracted, from a bounded tail buffer, and are never logged verbatim. The
route is the matched route template (``/v1/models/{model_id}``) whenever
routing happened, so client-chosen text cannot reach the log through the path.
Requests rejected before routing (401) are logged
with their path restricted to ``[A-Za-z0-9/_.-]`` and cut at 64 characters; the
query string is never logged.

``outcome`` is ``ok``, ``client_disconnected`` (the client left before the
response was complete), ``aborted`` (the task was cancelled), ``incomplete`` or
``error:<ExceptionClass>`` (class name only).

Probe/metrics paths are not logged (``VLLM_HARDENING_QUIET_PATHS``, default
``/health,/metrics``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any

from .logfilter import CURRENT_REQUEST_ID

_log = logging.getLogger("vllm_hardening.request")

_QUIET = frozenset(
    p.strip()
    for p in os.environ.get("VLLM_HARDENING_QUIET_PATHS", "/health,/metrics").split(",")
    if p.strip()
)
_JSON_LIMIT = 1 << 20  # parse at most 1 MiB of a non-streaming JSON response
_TAIL_LIMIT = 8192  # tail window kept for SSE responses
_METHOD_RE = re.compile(r"^[A-Z]{1,10}$")
_IP_RE = re.compile(r"^[0-9a-fA-F:.]{3,45}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_PATH_BAD = re.compile(r"[^A-Za-z0-9/_.-]")


def _clean(value: Any, pattern: re.Pattern[str]) -> str:
    return value if isinstance(value, str) and pattern.match(value) else "-"


def _extract_meta(raw: bytes, sse: bool) -> dict[str, Any]:
    """Pull id/model/usage counters out of a JSON body or the tail of an SSE stream."""
    meta: dict[str, Any] = {}
    text = raw.decode("utf-8", errors="ignore")
    candidates: list[str]
    if sse:
        candidates = [
            line[5:].strip()
            for line in reversed(text.splitlines())
            if line.startswith("data:") and "[DONE]" not in line
        ]
    else:
        candidates = [text]
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        usage = obj.get("usage")
        if isinstance(usage, dict) or "id" in obj:
            meta["engine_id"] = _clean(obj.get("id"), _ID_RE)
            meta["model"] = _clean(obj.get("model"), _ID_RE)
            if isinstance(usage, dict):
                for key in ("prompt_tokens", "completion_tokens"):
                    val = usage.get(key)
                    if isinstance(val, int) and not isinstance(val, bool):
                        meta[key] = val
            break
    return meta


class RequestFactLogMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") in _QUIET:
            await self.app(scope, receive, send)
            return

        request_id = "req-" + uuid.uuid4().hex[:16]
        token = CURRENT_REQUEST_ID.set(request_id)
        t0 = time.perf_counter()
        state: dict[str, Any] = {
            "status": 0,
            "ttfb": None,
            "req_bytes": 0,
            "resp_bytes": 0,
            "sse": False,
            "json": False,
            "buf": bytearray(),
            "outcome": "ok",
            "complete": False,  # final response body chunk was sent
        }

        async def recv_wrapper() -> dict:
            message = await receive()
            if message["type"] == "http.request":
                state["req_bytes"] += len(message.get("body", b""))
            elif message["type"] == "http.disconnect" and not state["complete"]:
                state["outcome"] = "client_disconnected"
            return message

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                headers = {k.lower(): v for k, v in message.get("headers", [])}
                ctype = headers.get(b"content-type", b"").decode("latin-1")
                state["sse"] = ctype.startswith("text/event-stream")
                state["json"] = ctype.startswith("application/json")
                if b"x-request-id" not in headers:
                    message = dict(message)
                    message["headers"] = [
                        *message.get("headers", []),
                        (b"x-request-id", request_id.encode()),
                    ]
            elif message["type"] == "http.response.body":
                if not message.get("more_body", False):
                    state["complete"] = True
                body = message.get("body", b"")
                if body:
                    if state["ttfb"] is None:
                        state["ttfb"] = time.perf_counter() - t0
                    state["resp_bytes"] += len(body)
                    buf: bytearray = state["buf"]
                    if state["sse"]:
                        buf += body
                        if len(buf) > _TAIL_LIMIT:
                            del buf[: len(buf) - _TAIL_LIMIT]
                    elif state["json"] and len(buf) < _JSON_LIMIT:
                        buf += body
            await send(message)

        try:
            await self.app(scope, recv_wrapper, send_wrapper)
        except BaseException as exc:  # log the class only, never the message
            if isinstance(exc, asyncio.CancelledError):
                state["outcome"] = "aborted"  # server-side cancel, e.g. client went away
            else:
                state["outcome"] = "error:" + type(exc).__name__
            raise
        finally:
            CURRENT_REQUEST_ID.reset(token)
            if not state["complete"] and state["outcome"] == "ok":
                state["outcome"] = "incomplete"  # app returned without finishing the body
            try:
                self._emit(scope, request_id, t0, state)
            except Exception:  # logging must never break serving
                pass

    @staticmethod
    def _emit(scope: dict, request_id: str, t0: float, state: dict[str, Any]) -> None:
        # matched route template if routing happened, else the sanitised path
        # (requests rejected by the auth guard never reach the router)
        route = getattr(scope.get("route"), "path", None)
        if not (isinstance(route, str) and len(route) < 128):
            raw = scope.get("path")
            route = (
                _PATH_BAD.sub("?", raw[:64]) if isinstance(raw, str) else "<unmatched>"
            )
        client = scope.get("client")
        meta = _extract_meta(bytes(state["buf"]), state["sse"]) if state["buf"] else {}
        ttfb = state["ttfb"]
        fields = [
            f"request_id={request_id}",
            f"method={_clean(scope.get('method'), _METHOD_RE)}",
            f"route={route}",
            f"status={state['status']}",
            f"outcome={state['outcome']}",
            f"latency_ms={(time.perf_counter() - t0) * 1000:.1f}",
            f"ttfb_ms={'-' if ttfb is None else f'{ttfb * 1000:.1f}'}",
            f"req_bytes={state['req_bytes']}",
            f"resp_bytes={state['resp_bytes']}",
            f"client={_clean(client[0] if client else None, _IP_RE)}",
            f"stream={'true' if state['sse'] else 'false'}",
            f"engine_id={meta.get('engine_id', '-')}",
            f"model={meta.get('model', '-')}",
            f"prompt_tokens={meta.get('prompt_tokens', '-')}",
            f"completion_tokens={meta.get('completion_tokens', '-')}",
        ]
        _log.info(" ".join(fields))
