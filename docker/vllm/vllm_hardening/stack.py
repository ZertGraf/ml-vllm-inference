"""Single entry point for ``vllm serve --middleware``.

``--middleware`` can be passed repeatedly on the command line but not from a
``--config`` YAML file (a list becomes ``--middleware a b``), so both
middlewares are composed here. Order, outermost first:

    RequestFactLogMiddleware -> BearerAuthGuard -> (vLLM's own middlewares/app)

The fact logger therefore also records requests the guard rejects with 401.
"""

from __future__ import annotations

from typing import Any

from .guard import BearerAuthGuard
from .reqlog import RequestFactLogMiddleware


class HardeningMiddleware:
    def __init__(self, app: Any) -> None:
        self._app = RequestFactLogMiddleware(BearerAuthGuard(app))

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        await self._app(scope, receive, send)
