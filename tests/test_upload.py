"""HTTP success, failure/retry, and idempotent upload tests."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile

from capture_format import (
    MANIFEST_FILENAME,
    PACKETS_FILENAME,
    UPLOAD_STATUS_FAILED,
    UPLOAD_STATUS_PENDING,
    UPLOAD_STATUS_UPLOADED,
    CaptureWriter,
    build_manifest,
    file_sha256,
    read_json,
    write_json,
    write_status,
)
from upload_queue import (
    UploadError,
    UploadQueueWorker,
    discover_uploadable_sessions,
    upload_session,
)


def _make_session(root: Path, session_id: str, payloads: list[bytes]) -> Path:
    session = root / session_id
    session.mkdir(parents=True, exist_ok=True)
    w = CaptureWriter(session)
    w.open()
    for i, payload in enumerate(payloads):
        w.write_datagram(payload, "127.0.0.1", 40000 + i, recv_unix_us=i + 1, mono_ns=i + 1)
    w.close()
    sha = file_sha256(session / PACKETS_FILENAME)
    manifest = build_manifest(
        session_id=session_id,
        created_at_utc="2026-01-01T00:00:00.000000Z",
        closed_at_utc="2026-01-01T00:00:01.000000Z",
        udp_bind_host="0.0.0.0",
        udp_port=9999,
        packet_count=w.packet_count,
        total_payload_bytes=w.total_payload_bytes,
        dropped_packet_count=None,
        hostname="testhost",
        python_version="3.12.0",
        capture_file_sha256=sha,
        upload_status=UPLOAD_STATUS_PENDING,
    )
    write_json(session / MANIFEST_FILENAME, manifest)
    write_status(session, UPLOAD_STATUS_PENDING)
    return session


class _UploadServer:
    def __init__(self, behavior: str = "ok"):
        self.behavior = behavior
        self.hits = 0
        self.stored: dict[str, str] = {}
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                parent.hits += 1
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                session_id = self.headers.get("X-Session-ID", "")

                if parent.behavior == "fail_once" and parent.hits == 1:
                    self._respond(503, {"ok": False, "error": "busy"})
                    return
                if parent.behavior == "always_fail":
                    self._respond(500, {"ok": False, "error": "boom"})
                    return
                if parent.behavior == "bad_sha":
                    self._respond(
                        200,
                        {
                            "ok": True,
                            "session_id": session_id,
                            "sha256": "0" * 64,
                            "stored": True,
                        },
                    )
                    return

                # Parse capture sha from local-like body: find packets content via boundary
                # Simpler: recompute by reading multipart capture field using upload_queue parser style
                from mock_http_receiver import parse_multipart

                fields = parse_multipart(self.headers.get("Content-Type", ""), body)
                import hashlib

                sha = hashlib.sha256(fields["capture"]).hexdigest()
                manifest = json.loads(fields["manifest"].decode("utf-8"))
                sid = manifest["session_id"]

                if sid in parent.stored and parent.stored[sid] == sha:
                    self._respond(
                        200,
                        {
                            "ok": True,
                            "session_id": sid,
                            "sha256": sha,
                            "stored": True,
                            "duplicate": True,
                        },
                    )
                    return

                parent.stored[sid] = sha
                self._respond(
                    200,
                    {"ok": True, "session_id": sid, "sha256": sha, "stored": True},
                )

            def _respond(self, code, obj):
                data = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v1/captures"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class UploadTests(unittest.TestCase):
    def test_http_success(self) -> None:
        server = _UploadServer("ok")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                session = _make_session(Path(tmp), "sess_ok", [b"aaa", b"bbb"])
                result = upload_session(session, server.url, timeout=5.0)
                self.assertTrue(result["ok"])
                self.assertEqual(result["session_id"], "sess_ok")
                self.assertTrue(result["stored"])
                manifest = read_json(session / MANIFEST_FILENAME)
                self.assertEqual(manifest["upload_status"], UPLOAD_STATUS_UPLOADED)
                self.assertEqual(result["sha256"], manifest["capture_file_sha256"])
                # Local data must remain
                self.assertTrue((session / PACKETS_FILENAME).is_file())
        finally:
            server.close()

    def test_http_failure_and_retry(self) -> None:
        server = _UploadServer("fail_once")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                session = _make_session(root, "sess_retry", [b"x"])
                with self.assertRaises(UploadError):
                    upload_session(session, server.url, timeout=5.0)

                worker = UploadQueueWorker(
                    root,
                    server.url,
                    timeout=5.0,
                    initial_backoff=0.01,
                    max_backoff=0.05,
                    max_attempts_per_cycle=5,
                    scan_interval=0.01,
                )
                # Clear backoff and force retry
                worker.enqueue_now(session)
                # process until success (fail_once means second hit works)
                ok = 0
                for _ in range(10):
                    ok += worker.process_once()
                    if ok:
                        break
                    # advance backoff artificially
                    worker._session_next_try["sess_retry"] = 0.0
                self.assertGreaterEqual(ok, 1)
                self.assertGreaterEqual(server.hits, 2)
                manifest = read_json(session / MANIFEST_FILENAME)
                self.assertEqual(manifest["upload_status"], UPLOAD_STATUS_UPLOADED)
        finally:
            server.close()

    def test_idempotent_duplicate_upload(self) -> None:
        """
        Re-uploading the same session_id with identical capture content must
        succeed again (Mac/mock may set duplicate=true). Windows client still
        requires ok/session_id/sha256/stored.
        """
        server = _UploadServer("ok")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                session = _make_session(Path(tmp), "sess_dup", [b"same"])
                r1 = upload_session(session, server.url, timeout=5.0)
                self.assertTrue(r1["ok"])
                # Reset local status to simulate restart retry of already-stored session
                write_status(session, UPLOAD_STATUS_PENDING)
                m = read_json(session / MANIFEST_FILENAME)
                m["upload_status"] = UPLOAD_STATUS_PENDING
                write_json(session / MANIFEST_FILENAME, m)
                r2 = upload_session(session, server.url, timeout=5.0)
                self.assertTrue(r2["ok"])
                self.assertEqual(r2["session_id"], "sess_dup")
                self.assertTrue(r2.get("duplicate") or r2["stored"])
                self.assertEqual(server.hits, 2)
        finally:
            server.close()

    def test_sha_mismatch_rejected(self) -> None:
        server = _UploadServer("bad_sha")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                session = _make_session(Path(tmp), "sess_badsha", [b"z"])
                with self.assertRaises(UploadError):
                    upload_session(session, server.url, timeout=5.0)
        finally:
            server.close()

    def test_discover_skips_recording_and_uploaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pending = _make_session(root, "a", [b"1"])
            uploaded = _make_session(root, "b", [b"2"])
            write_status(uploaded, UPLOAD_STATUS_UPLOADED)
            m = read_json(uploaded / MANIFEST_FILENAME)
            m["upload_status"] = UPLOAD_STATUS_UPLOADED
            write_json(uploaded / MANIFEST_FILENAME, m)
            recording = root / "c"
            recording.mkdir()
            (recording / PACKETS_FILENAME).write_bytes(b"")
            write_json(
                recording / MANIFEST_FILENAME,
                build_manifest(
                    session_id="c",
                    created_at_utc="t",
                    closed_at_utc=None,
                    udp_bind_host="0.0.0.0",
                    udp_port=9999,
                    packet_count=0,
                    total_payload_bytes=0,
                    dropped_packet_count=None,
                    hostname="h",
                    python_version="3",
                    capture_file_sha256=None,
                    upload_status="recording",
                ),
            )
            write_status(recording, "recording")
            found = discover_uploadable_sessions(root)
            self.assertEqual([p.name for p in found], ["a"])
            self.assertEqual(pending.name, "a")


if __name__ == "__main__":
    unittest.main()
