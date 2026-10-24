"""Hardening helpers shipped with the vLLM image.

Modules:
    logfilter  - logging filter + formatter that keep prompts, completions and
                 exception messages out of stdout/stderr.
    reqlog     - ASGI middleware that logs one content-free line per request.
    guard      - ASGI middleware that requires the Bearer token on every route
                 except an explicit public allow-list (vLLM's --api-key only
                 covers /v1, /v2, /inference and /cohere).
"""

__all__ = ["logfilter", "reqlog", "guard"]
