"""Unit tests for vllm_hardening (stdlib only).

Run inside the image:
    docker run --rm --entrypoint python vllm-cpu:0.32.0 -m unittest discover -v \
        -s /opt/vllm_hardening/tests
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
import unittest

os.environ.setdefault("VLLM_API_KEY", "unit-test-token")

from vllm_hardening import logfilter  # noqa: E402
from vllm_hardening.guard import BearerAuthGuard  # noqa: E402
from vllm_hardening.logfilter import (  # noqa: E402
    CURRENT_REQUEST_ID,
    PrivacyFilter,
    PrivacyFormatter,
    SafeStreamHandler,
)
from vllm_hardening.stack import HardeningMiddleware  # noqa: E402

CANARY = "CANARY-SECRET-9f3a"
RID = "chatcmpl-0123456789abcdef0123456789abcdef"


def make_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    handler = SafeStreamHandler(buf)
    handler.setLevel(logging.INFO)
    handler.addFilter(PrivacyFilter())
    handler.setFormatter(
        PrivacyFormatter(fmt="%(levelname)s %(name)s %(message)s")
    )
    lg = logging.getLogger(name)
    lg.handlers = [handler]
    lg.setLevel(logging.DEBUG)  # even then DEBUG must not come out
    lg.propagate = False
    return lg, buf


class FilterTests(unittest.TestCase):
    def test_debug_is_dropped(self) -> None:
        lg, buf = make_logger("t.debug")
        lg.debug("prompt: %s", CANARY)
        self.assertEqual(buf.getvalue(), "")

    def test_request_logger_records_are_templated(self) -> None:
        lg, buf = make_logger("vllm.entrypoints.serve.utils.request_logger")
        lg.info("Received request %s: params: %s", RID, {"stop": [CANARY]})
        lg.info(
            "Generated response %s%s: output: %r, finish_reason: %s",
            RID, "", CANARY, "stop",
        )
        out = buf.getvalue()
        self.assertNotIn(CANARY, out)
        self.assertIn(RID, out)
        self.assertEqual(out.count("suppressed"), 2)

    def test_engine_dump_is_templated(self) -> None:
        lg, buf = make_logger("vllm.logging_utils.dump_input")
        lg.error("Dumping scheduler output for model execution: %s", CANARY)
        self.assertNotIn(CANARY, buf.getvalue())

    def test_args_reduced_inside_request_context(self) -> None:
        lg, buf = make_logger("vllm.v1.engine.async_llm")
        token = CURRENT_REQUEST_ID.set("req-aaaaaaaaaaaaaaaa")
        try:
            lg.info("Request %s failed (bad request): %s.", RID, ValueError(CANARY))
            lg.info("Request %s failed: %s.", RID, f"ValueError: {CANARY}")
        finally:
            CURRENT_REQUEST_ID.reset(token)
        out = buf.getvalue()
        self.assertNotIn(CANARY, out)
        self.assertEqual(out.count(RID), 2)

    def test_args_untouched_outside_request_context(self) -> None:
        lg, buf = make_logger("vllm.engine.startup")
        lg.info("Loading model %s", "/models/qwen")
        self.assertIn("/models/qwen", buf.getvalue())

    def _raise_and_log(self, lg: logging.Logger) -> None:
        try:
            raise ValueError(f"bad input {CANARY}")
        except ValueError:
            lg.exception("boom")

    def test_traceback_redacted_for_http_layer_logger(self) -> None:
        lg, buf = make_logger("uvicorn.error")
        self._raise_and_log(lg)
        out = buf.getvalue()
        self.assertNotIn(CANARY, out)
        self.assertIn("ValueError", out)
        self.assertIn("_raise_and_log", out)

    def test_traceback_redacted_inside_request_context(self) -> None:
        lg, buf = make_logger("some.other.logger")
        token = CURRENT_REQUEST_ID.set("req-aaaaaaaaaaaaaaaa")
        try:
            self._raise_and_log(lg)
        finally:
            CURRENT_REQUEST_ID.reset(token)
        self.assertNotIn(CANARY, buf.getvalue())

    def test_startup_traceback_keeps_message(self) -> None:
        # engine/worker start-up failures must stay debuggable
        lg, buf = make_logger("vllm.v1.worker.cpu_worker")
        self._raise_and_log(lg)
        self.assertIn(CANARY, buf.getvalue())

    def test_cached_exc_text_is_not_reused(self) -> None:
        lg, buf = make_logger("uvicorn.error")
        rec = lg.makeRecord(
            "uvicorn.error", logging.ERROR, __file__, 1, "x", (), None
        )
        rec.exc_text = f"Traceback ... ValueError: {CANARY}"
        lg.handle(rec)
        self.assertNotIn(CANARY, buf.getvalue())

    def test_uvicorn_access_query_string_removed(self) -> None:
        lg, buf = make_logger("uvicorn.access")
        lg.info(
            '%s - "%s %s HTTP/%s" %d',
            "10.0.0.1:1234", "GET", f"/v1/models?q={CANARY}", "1.1", 200,
        )
        out = buf.getvalue()
        self.assertNotIn(CANARY, out)
        self.assertIn("/v1/models", out)

    def test_handle_error_does_not_echo_message(self) -> None:
        lg, _ = make_logger("t.err")
        err = io.StringIO()
        old = sys.stderr
        sys.stderr = err
        try:
            lg.info("needs two args %s %s", CANARY)  # formatting error
        finally:
            sys.stderr = old
        self.assertNotIn(CANARY, err.getvalue())


async def run_asgi(app, method="POST", path="/v1/chat/completions", headers=None, body=b"{}"):
    sent: list[dict] = []
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("10.1.2.3", 5555),
    }
    queue = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def echo_app(payload: bytes, content_type: bytes):
    async def app(scope, receive, send):
        await receive()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", content_type)],
        })
        mid = len(payload) // 2
        await send({"type": "http.response.body", "body": payload[:mid], "more_body": True})
        await send({"type": "http.response.body", "body": payload[mid:]})
    return app


class MiddlewareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lg, self.buf = make_logger("vllm_hardening.request")
        self.auth = {"Authorization": "Bearer unit-test-token"}

    def test_guard_rejects_without_or_with_wrong_token(self) -> None:
        app = BearerAuthGuard(echo_app(b"{}", b"application/json"))
        for hdrs in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic x"}):
            sent = asyncio.run(run_asgi(app, path="/invocations", headers=hdrs))
            self.assertEqual(sent[0]["status"], 401)

    def test_guard_allows_token_and_public_health(self) -> None:
        app = BearerAuthGuard(echo_app(b"{}", b"application/json"))
        self.assertEqual(
            asyncio.run(run_asgi(app, path="/invocations", headers=self.auth))[0]["status"], 200)
        self.assertEqual(
            asyncio.run(run_asgi(app, method="GET", path="/health"))[0]["status"], 200)

    def test_guard_refuses_to_start_without_key(self) -> None:
        old = os.environ.pop("VLLM_API_KEY")
        try:
            with self.assertRaises(RuntimeError):
                BearerAuthGuard(echo_app(b"{}", b"application/json"))
        finally:
            os.environ["VLLM_API_KEY"] = old

    def test_fact_line_for_json_response(self) -> None:
        body = json.dumps({
            "id": RID, "model": "qwen-test",
            "choices": [{"message": {"content": CANARY}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        }).encode()
        app = HardeningMiddleware(echo_app(body, b"application/json"))
        req = json.dumps({"messages": [{"role": "user", "content": CANARY}]}).encode()
        sent = asyncio.run(run_asgi(app, headers=self.auth, body=req))
        self.assertEqual(sent[0]["status"], 200)
        self.assertIn(b"x-request-id", dict(sent[0]["headers"]))
        out = self.buf.getvalue()
        self.assertEqual(out.count("\n"), 1)
        self.assertNotIn(CANARY, out)
        for needle in ("method=POST", "route=/v1/chat/completions", "status=200",
                       f"engine_id={RID}", "model=qwen-test",
                       "prompt_tokens=7", "completion_tokens=3",
                       f"req_bytes={len(req)}", "client=10.1.2.3", "stream=false"):
            self.assertIn(needle, out)

    def test_fact_line_for_sse_response(self) -> None:
        chunks = [
            {"id": RID, "model": "qwen-test", "choices": [{"delta": {"content": CANARY}}]},
            {"id": RID, "model": "qwen-test", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
        ]
        payload = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        app = HardeningMiddleware(echo_app(payload.encode(), b"text/event-stream; charset=utf-8"))
        asyncio.run(run_asgi(app, headers=self.auth))
        out = self.buf.getvalue()
        self.assertNotIn(CANARY, out)
        self.assertIn("stream=true", out)
        self.assertIn("prompt_tokens=5 completion_tokens=2", out)

    def test_unauthorized_request_is_logged_with_status_401(self) -> None:
        app = HardeningMiddleware(echo_app(b"{}", b"application/json"))
        asyncio.run(run_asgi(app, path="/invocations"))
        out = self.buf.getvalue()
        self.assertIn("status=401", out)
        self.assertIn("route=/invocations", out)

    def test_health_is_not_logged(self) -> None:
        app = HardeningMiddleware(echo_app(b"{}", b"application/json"))
        asyncio.run(run_asgi(app, method="GET", path="/health"))
        self.assertEqual(self.buf.getvalue(), "")

    def test_exception_class_only(self) -> None:
        async def boom(scope, receive, send):
            raise RuntimeError(CANARY)

        app = HardeningMiddleware(boom)
        with self.assertRaises(RuntimeError):
            asyncio.run(run_asgi(app, headers=self.auth))
        out = self.buf.getvalue()
        self.assertNotIn(CANARY, out)
        self.assertIn("outcome=error:RuntimeError", out)


if __name__ == "__main__":
    logging.raiseExceptions = False
    unittest.main(verbosity=2)
