#!/bin/sh
# Readiness probe: the warm-up must have finished and the API must answer /health.
[ -f "${WARMUP_DONE_FILE:-/tmp/warmup.done}" ] || exit 1
exec python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
