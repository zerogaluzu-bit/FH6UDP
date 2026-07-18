#!/usr/bin/env python3
"""
Local mock HTTP receiver for Windows-side upload protocol smoke tests.

NOT the production Mac receiver. Implements the same multipart contract so
udp_listener.py can be tested end-to-end on one machine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


class State:
    def __init__(self, store_dir: Path, require_auth: str | None):
        self.store_dir = store_dir
        self.require_auth = require_auth
        self.seen_sessions: dict[str, str] = {}  # session_id -> sha256
        self.requests = 0
        self.fail_next = 0  # force N failures for retry testing


def parse_multipart(content_type: str, body: bytes) -> dict[str, bytes]:
    m = re.search(r"boundary=([^;]+)", content_type, flags=re.I)
    if not m:
        raise ValueError("missing multipart boundary")
    boundary = m.group(1).strip().strip('"').encode("ascii")
    delimiter = b"--" + boundary
    parts = body.split(delimiter)
    fields: dict[str, bytes] = {}
    for part in parts:
        if not part or part in (b"--", b"--\r\n", b"--\n"):
            continue
        if part.startswith(b"--"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        elif part.startswith(b"\n"):
            part = part[1:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        elif part.endswith(b"\n"):
            part = part[:-1]
        header_blob, _, data = part.partition(b"\r\n\r\n")
        if _ == b"":
            header_blob, _, data = part.partition(b"\n\n")
        headers = header_blob.decode("utf-8", errors="replace")
        name_m = re.search(r'name="([^"]+)"', headers, flags=re.I)
        if not name_m:
            continue
        fields[name_m.group(1)] = data
    return fields


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if urlparse(self.path).path == "/health":
                self._json(200, {"ok": True, "requests": state.requests})
                return
            self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path != "/api/v1/captures":
                self._json(404, {"ok": False, "error": "not found"})
                return

            state.requests += 1
            if state.fail_next > 0:
                state.fail_next -= 1
                self._json(503, {"ok": False, "error": "forced failure"})
                return

            if state.require_auth:
                auth = self.headers.get("Authorization", "")
                expected = f"Bearer {state.require_auth}"
                if auth != expected:
                    self._json(401, {"ok": False, "error": "unauthorized"})
                    return

            session_hdr = self.headers.get("X-Session-ID")
            schema_hdr = self.headers.get("X-Capture-Schema-Version")
            ctype = self.headers.get("Content-Type", "")
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)

            try:
                fields = parse_multipart(ctype, body)
            except Exception as e:
                self._json(400, {"ok": False, "error": f"multipart parse: {e}"})
                return

            if "manifest" not in fields or "capture" not in fields:
                self._json(400, {"ok": False, "error": "missing form fields"})
                return

            manifest_bytes = fields["manifest"]
            capture_bytes = fields["capture"]
            try:
                manifest = json.loads(manifest_bytes.decode("utf-8"))
            except Exception:
                self._json(400, {"ok": False, "error": "invalid manifest json"})
                return

            session_id = manifest.get("session_id")
            if not session_id:
                self._json(400, {"ok": False, "error": "missing session_id"})
                return
            if session_hdr and session_hdr != session_id:
                self._json(400, {"ok": False, "error": "X-Session-ID mismatch"})
                return

            sha = hashlib.sha256(capture_bytes).hexdigest()
            # Idempotency: same session_id + same sha => success again
            if session_id in state.seen_sessions:
                prev = state.seen_sessions[session_id]
                if prev == sha:
                    self._json(
                        200,
                        {
                            "ok": True,
                            "session_id": session_id,
                            "sha256": sha,
                            "stored": True,
                            "duplicate": True,
                        },
                    )
                    return
                self._json(
                    409,
                    {
                        "ok": False,
                        "error": "session_id exists with different sha256",
                        "session_id": session_id,
                    },
                )
                return

            dest = state.store_dir / session_id
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "manifest.json").write_bytes(manifest_bytes)
            (dest / "packets.bin").write_bytes(capture_bytes)
            state.seen_sessions[session_id] = sha

            self._json(
                200,
                {
                    "ok": True,
                    "session_id": session_id,
                    "sha256": sha,
                    "stored": True,
                    "schema_version_header": schema_hdr,
                },
            )

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock Mac HTTP capture receiver")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--store-dir",
        type=Path,
        default=Path("mock_receiver_store"),
        help="Where to write accepted captures",
    )
    parser.add_argument(
        "--require-token",
        default=None,
        help="If set, require Authorization: Bearer <token>",
    )
    parser.add_argument(
        "--fail-next",
        type=int,
        default=0,
        help="Force the next N POST requests to fail with 503",
    )
    args = parser.parse_args()

    args.store_dir.mkdir(parents=True, exist_ok=True)
    state = State(args.store_dir, args.require_token)
    state.fail_next = args.fail_next
    handler = make_handler(state)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(
        f"mock receiver listening on http://{args.host}:{args.port}/api/v1/captures",
        flush=True,
    )
    print(f"store_dir={args.store_dir.resolve()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
