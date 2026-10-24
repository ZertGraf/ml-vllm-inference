"""Privacy-preserving logging for vLLM.

Goal: nothing the user typed (prompts, stop strings, schemas, ...) and nothing
the model generated may reach stdout/stderr, while the fact of every request is
still logged (see ``reqlog.py``).

Defence in depth, from the outside in:

1. Handler-level filter (``PrivacyFilter``). A filter attached to the *handler*
   sees every record that is about to be written, whichever logger produced it
   (vllm, uvicorn, fastapi, third-party libraries, root). It
     - drops everything below INFO (vLLM's DEBUG branches dump prompt text and
       token ids), whatever ``VLLM_LOGGING_LEVEL`` says;
     - replaces records from content-bearing loggers (vLLM's ``RequestLogger``
       behind ``--enable-log-requests`` / ``--enable-log-outputs`` and the
       engine crash dump) with a fixed template that keeps only the request id;
     - for records emitted while an HTTP request is being served, reduces
       arguments to request ids, numbers and type names, so a message such as
       "Request %s failed (bad request): %s" cannot carry user text;
     - strips the query string from uvicorn access lines.
2. Formatter (``PrivacyFormatter``). Exception *messages* often echo the input
   (pydantic validation errors, JSON decode errors, "prompt too long ...").
   For records produced while serving a request, or by the HTTP layer
   (uvicorn/starlette/fastapi/vllm.entrypoints), tracebacks are rendered as
   frames + exception type names only. Elsewhere (start-up, model loading,
   engine workers) the full message is kept: it is what an operator needs to
   debug a failing start and it cannot contain a user's input. Set
   ``VLLM_HARDENING_REDACT_ALL_EXCEPTIONS=1`` to redact everywhere.
3. Handler (``SafeStreamHandler``). ``logging`` reports formatting failures to
   stderr together with the offending message and arguments; that path is
   replaced with a content-free line.

The filter fails closed: if it hits an unexpected error the record is replaced
by a generic notice instead of being passed through.
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
import sys
import traceback
from types import TracebackType
from typing import Any

try:  # vLLM's own formatter keeps the familiar line layout and continuation lines
    from vllm.logging_utils import NewLineFormatter as _BaseFormatter
except Exception:  # pragma: no cover - lets the unit tests run without vLLM
    _BaseFormatter = logging.Formatter  # type: ignore[assignment,misc]

# Set by RequestFactLogMiddleware to the request id while a request is served.
CURRENT_REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vllm_hardening_request_id", default=None
)

# Loggers whose records are content-bearing by design.
CONTENT_LOGGERS: tuple[str, ...] = (
    # --enable-log-requests / --enable-log-outputs: prompt, params, output text
    "vllm.entrypoints.serve.utils.request_logger",
    # engine crash dump: scheduler output contains prompt token ids
    "vllm.logging_utils.dump_input",
)

# Loggers that are the product of this package: their lines are built from
# controlled values only, so they are exempt from argument sanitising.
TRUSTED_LOGGERS: tuple[str, ...] = ("vllm_hardening.",)

# Loggers of the HTTP layer: exceptions they log may echo request data.
HTTP_LAYER_LOGGERS: tuple[str, ...] = (
    "uvicorn",
    "starlette",
    "fastapi",
    "vllm.entrypoints",
    "vllm.v1.engine.async_llm",
    "vllm.renderers",
)
REDACT_ALL_EXCEPTIONS = os.environ.get("VLLM_HARDENING_REDACT_ALL_EXCEPTIONS") == "1"

# vLLM / OpenAI style ids: chatcmpl-<hex>, cmpl-<hex>-0, req-<hex>, ...
_REQUEST_ID_RE = re.compile(r"^[a-z][a-z0-9]*-[0-9a-f]{12,}(?:-[0-9]+)?$")


def _safe_arg(arg: Any) -> Any:
    """Reduce a log argument to something that cannot carry user text."""
    if arg is None or isinstance(arg, (bool, int, float)):
        return arg
    if isinstance(arg, str):
        if _REQUEST_ID_RE.match(arg):
            return arg
        return f"<str len={len(arg)}>"
    if isinstance(arg, BaseException):
        return f"<{type(arg).__name__}>"
    return f"<{type(arg).__name__}>"


def _first_request_id(args: Any) -> str:
    if isinstance(args, tuple):
        for a in args:
            if isinstance(a, str) and _REQUEST_ID_RE.match(a):
                return a
    return "-"


class PrivacyFilter(logging.Filter):
    """Attach to *handlers*; see the module docstring."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            return self._filter(record)
        except Exception:
            record.msg = "log record suppressed (privacy filter error)"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            return True

    def _filter(self, record: logging.LogRecord) -> bool:
        # 1. nothing below INFO, whatever the configured level is
        if record.levelno < logging.INFO:
            return False

        # never reuse a cached traceback text rendered by another formatter
        record.exc_text = None
        # the format string uses this attribute, which vLLM's record factory adds
        if not hasattr(record, "vllm_process_name"):
            record.vllm_process_name = record.processName

        name = record.name
        in_request = CURRENT_REQUEST_ID.get() is not None
        record.redact_exc = (
            REDACT_ALL_EXCEPTIONS or in_request or name.startswith(HTTP_LAYER_LOGGERS)
        )

        # 2. content-bearing loggers -> fixed template
        if name.startswith(CONTENT_LOGGERS):
            rid = _first_request_id(record.args)
            record.msg = "content-bearing log record suppressed (source=%s request=%s)"
            record.args = (name.rsplit(".", 1)[-1], rid)
            return True

        # 3. uvicorn access line: keep client, method, path, status; no query
        if name == "uvicorn.access" and isinstance(record.args, tuple):
            if len(record.args) == 5 and isinstance(record.args[2], str):
                args = list(record.args)
                args[2] = args[2].split("?", 1)[0]
                record.args = tuple(args)
            return True

        # 4. records produced while a request is being served
        if name.startswith(TRUSTED_LOGGERS):
            return True
        if in_request:
            if isinstance(record.args, tuple):
                record.args = tuple(_safe_arg(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: _safe_arg(v) for k, v in record.args.items()}
        return True


class PrivacyFormatter(_BaseFormatter):
    """Renders tracebacks without exception messages where the filter asked for it."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        # fail closed: records that never passed the filter are redacted
        if getattr(record, "redact_exc", True) and record.exc_info:
            record.exc_text = self._redacted_traceback(record.exc_info)
        return super().format(record)

    @staticmethod
    def _redacted_traceback(
        ei: tuple[type[BaseException], BaseException, TracebackType | None]
        | tuple[None, None, None],
    ) -> str:
        etype, evalue, tb = ei
        if etype is None or evalue is None:
            return "exception (details redacted)"
        lines = ["Traceback (most recent call last) [exception messages redacted]:"]
        lines.extend(s.rstrip("\n") for s in traceback.format_tb(tb))
        chain: list[str] = []
        cur: BaseException | None = evalue
        seen: set[int] = set()
        while cur is not None and id(cur) not in seen and len(chain) < 5:
            seen.add(id(cur))
            t = type(cur)
            chain.append(f"{t.__module__}.{t.__qualname__}")
            cur = cur.__cause__ or cur.__context__
        lines.append("exception chain (outermost first): " + " <- ".join(chain))
        return "\n".join(lines)


class SafeStreamHandler(logging.StreamHandler):
    """StreamHandler whose error path does not echo the message or arguments."""

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        try:
            sys.stderr.write(
                f"logging error in logger {record.name!r} (details suppressed)\n"
            )
        except Exception:
            pass
