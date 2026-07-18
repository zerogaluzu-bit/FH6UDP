"""
Local upload queue with exponential backoff for completed capture sessions.

Uploads only closed sessions (status ready / pending / failed). Never uploads
an in-progress recording. Does not delete local captures after success.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional
from urllib.request import Request

from capture_format import (
    MANIFEST_FILENAME,
    PACKETS_FILENAME,
    SCHEMA_VERSION,
    UPLOAD_STATUS_FAILED,
    UPLOAD_STATUS_PENDING,
    UPLOAD_STATUS_UPLOADED,
    UPLOAD_STATUS_UPLOADING,
    file_sha256,
    read_json,
    read_status,
    write_json,
    write_status,
)

log = logging.getLogger("fh6.upload")


class UploadError(Exception):
    pass


def _redact_headers_for_log(headers: dict[str, str]) -> dict[str, str]:
    out = dict(headers)
    if "Authorization" in out:
        out["Authorization"] = "Bearer <redacted>"
    return out


def build_multipart_body(
    manifest_bytes: bytes,
    capture_bytes: bytes,
    boundary: Optional[str] = None,
) -> tuple[bytes, str]:
    """Build multipart/form-data with raw binary parts (no transfer encoding)."""
    bound = boundary or f"----fh6boundary{secrets.token_hex(16)}"
    crlf = b"\r\n"
    chunks: list[bytes] = []

    def add_part(name: str, filename: str, content_type: str, data: bytes) -> None:
        chunks.append(f"--{bound}".encode("ascii") + crlf)
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"'
            ).encode("ascii")
            + crlf
        )
        chunks.append(f"Content-Type: {content_type}".encode("ascii") + crlf)
        chunks.append(crlf)
        chunks.append(data)
        chunks.append(crlf)

    add_part("manifest", "manifest.json", "application/json", manifest_bytes)
    add_part("capture", "packets.bin", "application/octet-stream", capture_bytes)
    chunks.append(f"--{bound}--".encode("ascii") + crlf)
    body = b"".join(chunks)
    return body, f"multipart/form-data; boundary={bound}"


def upload_session(
    session_dir: Path,
    receiver_url: str,
    *,
    auth_token: Optional[str] = None,
    timeout: float = 30.0,
    opener: Optional[Callable] = None,
) -> dict:
    """
    POST multipart form to receiver_url. Validates response contract.
    Returns parsed JSON on success. Raises UploadError on failure.
    """
    session_dir = Path(session_dir)
    manifest_path = session_dir / MANIFEST_FILENAME
    packets_path = session_dir / PACKETS_FILENAME
    if not manifest_path.is_file() or not packets_path.is_file():
        raise UploadError(f"missing capture files in {session_dir}")

    manifest = read_json(manifest_path)
    session_id = manifest["session_id"]
    local_sha = manifest.get("capture_file_sha256") or file_sha256(packets_path)
    if manifest.get("capture_file_sha256") != local_sha:
        manifest["capture_file_sha256"] = local_sha
        write_json(manifest_path, manifest)

    manifest_bytes = manifest_path.read_bytes()
    capture_bytes = packets_path.read_bytes()
    body, content_type = build_multipart_body(manifest_bytes, capture_bytes)

    headers = {
        "Content-Type": content_type,
        "X-Capture-Schema-Version": str(SCHEMA_VERSION),
        "X-Session-ID": session_id,
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    log.info(
        "uploading session %s to %s headers=%s bytes=%d",
        session_id,
        receiver_url,
        _redact_headers_for_log(headers),
        len(body),
    )

    req = Request(receiver_url, data=body, headers=headers, method="POST")
    urlopen = opener or urllib.request.urlopen
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            resp_body = resp.read()
    except urllib.error.HTTPError as e:
        err_body = e.read() if hasattr(e, "read") else b""
        raise UploadError(f"HTTP {e.code}: {err_body[:500]!r}") from e
    except Exception as e:
        raise UploadError(f"network/upload error: {e}") from e

    if status < 200 or status >= 300:
        raise UploadError(f"non-2xx status: {status}")

    try:
        data = json.loads(resp_body.decode("utf-8"))
    except Exception as e:
        raise UploadError(f"invalid JSON response: {resp_body[:500]!r}") from e

    if not data.get("ok") is True:
        raise UploadError(f"response ok != true: {data}")
    if data.get("session_id") != session_id:
        raise UploadError(
            f"session_id mismatch: local={session_id} remote={data.get('session_id')}"
        )
    if data.get("sha256") != local_sha:
        raise UploadError(
            f"sha256 mismatch: local={local_sha} remote={data.get('sha256')}"
        )
    if data.get("stored") is not True:
        raise UploadError(f"stored != true: {data}")

    manifest["upload_status"] = UPLOAD_STATUS_UPLOADED
    write_json(manifest_path, manifest)
    write_status(session_dir, UPLOAD_STATUS_UPLOADED)
    log.info("upload succeeded for session %s", session_id)
    return data


def discover_uploadable_sessions(output_dir: Path) -> list[Path]:
    """Find sessions that are closed and not yet successfully uploaded."""
    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        return []
    result: list[Path] = []
    for child in sorted(output_dir.iterdir()):
        if not child.is_dir():
            continue
        status = read_status(child)
        manifest_path = child / MANIFEST_FILENAME
        packets_path = child / PACKETS_FILENAME
        if not packets_path.is_file() or not manifest_path.is_file():
            continue
        if status == "recording":
            continue
        if status == UPLOAD_STATUS_UPLOADED:
            continue
        try:
            manifest = read_json(manifest_path)
        except Exception:
            continue
        if manifest.get("upload_status") == UPLOAD_STATUS_UPLOADED:
            continue
        if status in (None, UPLOAD_STATUS_PENDING, UPLOAD_STATUS_FAILED, UPLOAD_STATUS_UPLOADING, "ready"):
            # "ready" is treated as pending for upload
            result.append(child)
    return result


class UploadQueueWorker:
    """
    Background worker: scans for pending sessions and uploads with backoff.
    Designed not to block the UDP capture loop forever on retries.
    """

    def __init__(
        self,
        output_dir: Path,
        receiver_url: str,
        *,
        auth_token_env: Optional[str] = None,
        timeout: float = 30.0,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
        max_attempts_per_cycle: int = 3,
        scan_interval: float = 2.0,
    ):
        self.output_dir = Path(output_dir)
        self.receiver_url = receiver_url
        self.auth_token_env = auth_token_env
        self.timeout = timeout
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff
        self.max_attempts_per_cycle = max_attempts_per_cycle
        self.scan_interval = scan_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._session_attempts: dict[str, int] = {}
        self._session_next_try: dict[str, float] = {}
        self._lock = threading.Lock()

    def _auth_token(self) -> Optional[str]:
        if not self.auth_token_env:
            return None
        token = os.environ.get(self.auth_token_env)
        return token if token else None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="fh6-upload-queue", daemon=True
        )
        self._thread.start()
        log.info("upload queue worker started")

    def stop(self, join_timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=join_timeout)
        log.info("upload queue worker stopped")

    def enqueue_now(self, session_dir: Path) -> None:
        """Mark session pending and clear backoff so it is tried soon."""
        session_dir = Path(session_dir)
        write_status(session_dir, UPLOAD_STATUS_PENDING)
        sid = session_dir.name
        with self._lock:
            self._session_attempts[sid] = 0
            self._session_next_try[sid] = 0.0

    def process_once(self) -> int:
        """Process up to max_attempts_per_cycle uploads. Returns success count."""
        if not self.receiver_url:
            return 0
        sessions = discover_uploadable_sessions(self.output_dir)
        successes = 0
        attempts = 0
        now = time.monotonic()
        for session_dir in sessions:
            if attempts >= self.max_attempts_per_cycle:
                break
            sid = session_dir.name
            with self._lock:
                next_try = self._session_next_try.get(sid, 0.0)
            if now < next_try:
                continue
            attempts += 1
            try:
                write_status(session_dir, UPLOAD_STATUS_UPLOADING)
                manifest = read_json(session_dir / MANIFEST_FILENAME)
                manifest["upload_status"] = UPLOAD_STATUS_UPLOADING
                write_json(session_dir / MANIFEST_FILENAME, manifest)
                upload_session(
                    session_dir,
                    self.receiver_url,
                    auth_token=self._auth_token(),
                    timeout=self.timeout,
                )
                successes += 1
                with self._lock:
                    self._session_attempts.pop(sid, None)
                    self._session_next_try.pop(sid, None)
            except Exception as e:
                log.warning("upload failed for %s: %s", sid, e)
                try:
                    manifest = read_json(session_dir / MANIFEST_FILENAME)
                    manifest["upload_status"] = UPLOAD_STATUS_FAILED
                    write_json(session_dir / MANIFEST_FILENAME, manifest)
                    write_status(session_dir, UPLOAD_STATUS_FAILED)
                except Exception:
                    pass
                with self._lock:
                    n = self._session_attempts.get(sid, 0) + 1
                    self._session_attempts[sid] = n
                    backoff = min(
                        self.max_backoff,
                        self.initial_backoff * (2 ** (n - 1)),
                    )
                    self._session_next_try[sid] = time.monotonic() + backoff
                    log.info("retry %s in %.1fs (attempt %d)", sid, backoff, n)
        return successes

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.process_once()
            except Exception as e:
                log.exception("upload queue cycle error: %s", e)
            self._stop.wait(self.scan_interval)
