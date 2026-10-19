"""Demo application for the S1 "pod keeps restarting" scenario.

A small HTTP service whose failure modes are driven entirely by its deployment
manifest, so that every fault in the demo is introduced by a manifest change:

- ``APP_MODE`` must be one of ``VALID_MODES``; any other value is logged and the
  process exits with status 2 before serving anything.
- ``ALLOC_MB`` mebibytes are allocated and touched at startup and held for the
  lifetime of the process, so a memory limit below that amount gets the
  container OOM-killed.
- ``GET /healthz`` is the liveness and readiness endpoint; every other path
  except ``/`` returns 404, so a probe pointed elsewhere fails.

Only the standard library is used.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType

VALID_MODES = ("readonly", "standard")
EXIT_INVALID_CONFIG = 2
PAGE_SIZE = 4096


def log(message: str) -> None:
    """Write one log line to stdout."""
    print(message, flush=True)


def allocate(mebibytes: int) -> bytearray:
    """Allocate ``mebibytes`` MiB and write to every page so it becomes resident."""
    buffer = bytearray(mebibytes * 1024 * 1024)
    for offset in range(0, len(buffer), PAGE_SIZE):
        buffer[offset] = 1
    return buffer


class Handler(BaseHTTPRequestHandler):
    """Serves the health endpoint and a status document."""

    mode = ""
    allocated_mb = 0

    def do_GET(self) -> None:
        """Handle GET requests."""
        if self.path == "/healthz":
            self._reply(200, "text/plain", b"ok\n")
        elif self.path == "/":
            body = json.dumps({"mode": self.mode, "allocated_mb": self.allocated_mb})
            self._reply(200, "application/json", body.encode() + b"\n")
        else:
            self._reply(404, "text/plain", b"not found\n")

    def log_message(self, format: str, *args: object) -> None:
        """Keep probe requests out of the log; only startup and errors are logged."""

    def _reply(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    """Validate the configuration, allocate memory and serve until SIGTERM."""
    mode = os.environ.get("APP_MODE", "standard")
    if mode not in VALID_MODES:
        print(
            f'ERROR invalid APP_MODE "{mode}"; expected one of: {", ".join(VALID_MODES)}',
            file=sys.stderr,
            flush=True,
        )
        return EXIT_INVALID_CONFIG

    allocated_mb = int(os.environ.get("ALLOC_MB", "0"))
    port = int(os.environ.get("PORT", "8080"))
    log(f"starting s1-app mode={mode}")
    _memory = allocate(allocated_mb)
    log(f"allocated {allocated_mb} MiB")

    Handler.mode = mode
    Handler.allocated_mb = allocated_mb
    server = ThreadingHTTPServer(("", port), Handler)

    def stop(signum: int, frame: FrameType | None) -> None:
        log("received SIGTERM, shutting down")
        threading.Thread(target=server.shutdown).start()

    signal.signal(signal.SIGTERM, stop)
    log(f"listening on port {port}")
    server.serve_forever()
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
