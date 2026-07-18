"""Ctrl+C / duration graceful shutdown and end-to-end UDP smoke tests."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class GracefulShutdownTests(unittest.TestCase):
    def test_duration_stops_and_flushes_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            port = _free_udp_port()
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "udp_listener.py"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--output-dir",
                    str(out),
                    "--duration",
                    "1.5",
                    "--no-upload",
                ],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                _wait_listener_ready(proc, timeout=5.0)
                sent = 12
                _send_udp(port, sent, size=32)
                rc = proc.wait(timeout=10)
                output = proc.stdout.read() if proc.stdout else ""
                if proc.stdout:
                    proc.stdout.close()
                self.assertEqual(rc, 0, msg=output)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                if proc.stdout:
                    proc.stdout.close()

            sessions = [p for p in out.iterdir() if p.is_dir()]
            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
            self.assertIsNotNone(manifest["closed_at_utc"])
            self.assertEqual(manifest["packet_count"], sent)
            self.assertIsNotNone(manifest["capture_file_sha256"])
            self.assertEqual(manifest["upload_status"], "local_only")

            # verify tool
            v = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "verify_capture.py"),
                    str(session),
                    "--expect-packets",
                    str(sent),
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            self.assertEqual(v.returncode, 0, msg=v.stdout + v.stderr)

    def test_sigint_graceful_shutdown(self) -> None:
        """
        Reproducible Ctrl+C path without Windows CTRL_C_EVENT (which can kill
        the test runner). A child script installs the same handler semantics:
        set a stop flag, then flush/close the capture session.
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            script = Path(tmp) / "sigint_child.py"
            script.write_text(
                f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from udp_listener import CaptureSession

out = Path({str(out)!r})
session = CaptureSession(out, "127.0.0.1", 9999)
session.start()
session.record(b"hello-sigint", ("127.0.0.1", 12345))
session.record(b"world-sigint", ("127.0.0.1", 12346))
# Simulate the SIGINT handler's finally path
session_dir = session.close(upload_pending=False)
print("session closed", session_dir)
print("collector stopped")
""",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, str(script)],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
            self.assertIn("session closed", proc.stdout.lower())
            self.assertIn("collector stopped", proc.stdout.lower())

            sessions = [p for p in out.iterdir() if p.is_dir()]
            self.assertEqual(len(sessions), 1)
            manifest = json.loads(
                (sessions[0] / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["packet_count"], 2)
            self.assertIsNotNone(manifest["closed_at_utc"])
            self.assertIsNotNone(manifest["capture_file_sha256"])
            from capture_format import read_packets

            payloads = [p.payload for p in read_packets(sessions[0] / "packets.bin")]
            self.assertEqual(payloads, [b"hello-sigint", b"world-sigint"])

    def test_end_to_end_upload_with_mock_receiver(self) -> None:
        store = tempfile.mkdtemp()
        out = tempfile.mkdtemp()
        try:
            httpd, http_port = _start_simple_receiver(Path(store))
            udp_port = _free_udp_port()
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "udp_listener.py"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(udp_port),
                    "--output-dir",
                    out,
                    "--receiver-url",
                    f"http://127.0.0.1:{http_port}/api/v1/captures",
                    "--duration",
                    "2.0",
                ],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                _wait_listener_ready(proc, timeout=5.0)
                _send_udp(udp_port, 8, size=40)
                rc = proc.wait(timeout=15)
                output = proc.stdout.read() if proc.stdout else ""
                if proc.stdout:
                    proc.stdout.close()
                self.assertEqual(rc, 0, msg=output)
                self.assertIn("upload succeeded", output.lower())
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                if proc.stdout:
                    proc.stdout.close()
                httpd.shutdown()
                httpd.server_close()

            sessions = list(Path(out).iterdir())
            self.assertEqual(len(sessions), 1)
            manifest = json.loads(
                (sessions[0] / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["upload_status"], "uploaded")
            # Original retained
            self.assertTrue((sessions[0] / "packets.bin").is_file())
            remote = Path(store) / manifest["session_id"] / "packets.bin"
            self.assertTrue(remote.is_file())
            self.assertEqual(
                remote.read_bytes(),
                (sessions[0] / "packets.bin").read_bytes(),
            )
        finally:
            import shutil

            shutil.rmtree(store, ignore_errors=True)
            shutil.rmtree(out, ignore_errors=True)


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_listener_ready(proc: subprocess.Popen, timeout: float) -> None:
    deadline = time.time() + timeout
    started = time.time()
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"listener exited early: {out}")
        if time.time() - started >= 0.5:
            return
        time.sleep(0.05)


def _send_udp(port: int, count: int, size: int = 32) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for i in range(count):
            payload = i.to_bytes(4, "big") + bytes(max(0, size - 4))
            sock.sendto(payload, ("127.0.0.1", port))
            time.sleep(0.01)
    finally:
        sock.close()


def _start_simple_receiver(store_dir: Path):
    import hashlib

    from mock_http_receiver import parse_multipart

    stored: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            fields = parse_multipart(self.headers.get("Content-Type", ""), body)
            manifest = json.loads(fields["manifest"].decode("utf-8"))
            sid = manifest["session_id"]
            sha = hashlib.sha256(fields["capture"]).hexdigest()
            dest = store_dir / sid
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "manifest.json").write_bytes(fields["manifest"])
            (dest / "packets.bin").write_bytes(fields["capture"])
            stored[sid] = sha
            data = json.dumps(
                {"ok": True, "session_id": sid, "sha256": sha, "stored": True}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


if __name__ == "__main__":
    unittest.main()
