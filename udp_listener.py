#!/usr/bin/env python3
"""
FH6 Telemetry Windows collector: receive UDP Data Out, save raw datagrams,
and upload completed capture sessions to a Mac HTTP receiver.

Does not parse Forza telemetry fields. Does not implement coaching logic.
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import select
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Any, Optional

from capture_format import (
    MANIFEST_FILENAME,
    PACKETS_FILENAME,
    UPLOAD_STATUS_PENDING,
    UPLOAD_STATUS_RECORDING,
    CaptureWriter,
    APPLICATION_VERSION,
    build_manifest,
    file_sha256,
    new_session_id,
    utc_now_iso,
    write_json,
    write_status,
)
from upload_queue import UploadQueueWorker

log = logging.getLogger("fh6.listener")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 9999
DEFAULT_OUTPUT_DIR = "captures"
DEFAULT_HTTP_TIMEOUT = 30.0
RECV_BUFSIZE = 65535


def load_config(path: Optional[Path]) -> dict[str, Any]:
    if path is None:
        return {}
    if not path.is_file():
        raise SystemExit(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise SystemExit("config.json must be a JSON object")
    return data


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FH6 UDP Data Out collector (Windows capture + HTTP upload)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.json (CLI flags override config values)",
    )
    parser.add_argument("--host", default=None, help=f"UDP bind host (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=None, help=f"UDP bind port (default {DEFAULT_PORT})")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=f"Directory for capture sessions (default {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--receiver-url",
        default=None,
        help="HTTP POST URL for completed captures (e.g. http://HOST:8765/api/v1/captures)",
    )
    parser.add_argument(
        "--auth-token-env",
        default=None,
        help="Name of environment variable holding Bearer token (never pass token on CLI)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Stop after N seconds (optional; otherwise run until Ctrl+C)",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        default=False,
        help="Disable HTTP upload (local capture only)",
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=None,
        help=f"HTTP upload timeout seconds (default {DEFAULT_HTTP_TIMEOUT})",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Enable debug logging (auth tokens are never logged)",
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)

    def pick(cli_val, key, default):
        if cli_val is not None and cli_val is not False:
            return cli_val
        if key in cfg:
            return cfg[key]
        return default

    # store_true for --no-upload: if flag set True use it; else config; else False
    no_upload = True if args.no_upload else bool(cfg.get("no_upload", False))
    verbose = True if args.verbose else bool(cfg.get("verbose", False))

    return argparse.Namespace(
        host=pick(args.host, "host", DEFAULT_HOST),
        port=int(pick(args.port, "port", DEFAULT_PORT)),
        output_dir=Path(pick(args.output_dir, "output_dir", DEFAULT_OUTPUT_DIR)),
        receiver_url=pick(args.receiver_url, "receiver_url", None),
        auth_token_env=pick(args.auth_token_env, "auth_token_env", None),
        duration=pick(args.duration, "duration", None),
        no_upload=no_upload,
        http_timeout=float(pick(args.http_timeout, "http_timeout", DEFAULT_HTTP_TIMEOUT)),
        verbose=verbose,
        config=args.config,
    )


class CaptureSession:
    def __init__(
        self,
        output_dir: Path,
        udp_bind_host: str,
        udp_port: int,
    ):
        self.session_id = new_session_id()
        self.session_dir = Path(output_dir) / self.session_id
        self.udp_bind_host = udp_bind_host
        self.udp_port = udp_port
        self.created_at_utc = utc_now_iso()
        self.writer = CaptureWriter(self.session_dir)
        self._notes = {
            "dropped_packet_count_note": (
                "UDP sockets do not expose a reliable OS-level drop counter "
                "on all Windows configurations; dropped_packet_count is null "
                "unless an application-level drop is recorded."
            )
        }
        self.app_dropped = 0

    def start(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        write_status(self.session_dir, UPLOAD_STATUS_RECORDING)
        self.writer.open()
        manifest = build_manifest(
            session_id=self.session_id,
            created_at_utc=self.created_at_utc,
            closed_at_utc=None,
            udp_bind_host=self.udp_bind_host,
            udp_port=self.udp_port,
            packet_count=0,
            total_payload_bytes=0,
            dropped_packet_count=None,
            hostname=platform.node(),
            python_version=platform.python_version(),
            capture_file_sha256=None,
            upload_status=UPLOAD_STATUS_RECORDING,
            notes=self._notes,
        )
        write_json(self.session_dir / MANIFEST_FILENAME, manifest)
        log.info("session started: %s -> %s", self.session_id, self.session_dir)

    def record(self, data: bytes, addr: tuple[str, int]) -> None:
        self.writer.write_datagram(data, addr[0], addr[1])

    def close(self, upload_pending: bool = True) -> Path:
        self.writer.flush()
        self.writer.close()
        closed_at = utc_now_iso()
        sha = None
        packets_path = self.session_dir / PACKETS_FILENAME
        if packets_path.is_file():
            sha = file_sha256(packets_path)
        dropped = self.app_dropped if self.app_dropped > 0 else None
        upload_status = UPLOAD_STATUS_PENDING if upload_pending else "local_only"
        manifest = build_manifest(
            session_id=self.session_id,
            created_at_utc=self.created_at_utc,
            closed_at_utc=closed_at,
            udp_bind_host=self.udp_bind_host,
            udp_port=self.udp_port,
            packet_count=self.writer.packet_count,
            total_payload_bytes=self.writer.total_payload_bytes,
            dropped_packet_count=dropped,
            hostname=platform.node(),
            python_version=platform.python_version(),
            capture_file_sha256=sha,
            upload_status=upload_status,
            notes=self._notes,
        )
        write_json(self.session_dir / MANIFEST_FILENAME, manifest)
        write_status(self.session_dir, upload_status)
        log.info(
            "session closed: %s packets=%d payload_bytes=%d sha256=%s",
            self.session_id,
            self.writer.packet_count,
            self.writer.total_payload_bytes,
            sha,
        )
        return self.session_dir


def run_listener(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    log.info(
        "FH6 UDP collector v%s starting (Python %s, %s)",
        APPLICATION_VERSION,
        platform.python_version(),
        platform.platform(),
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)

    upload_enabled = (not args.no_upload) and bool(args.receiver_url)
    worker: Optional[UploadQueueWorker] = None
    if upload_enabled:
        worker = UploadQueueWorker(
            args.output_dir,
            args.receiver_url,
            auth_token_env=args.auth_token_env,
            timeout=args.http_timeout,
        )
        worker.start()
        # Retry any leftover sessions from previous runs
        try:
            worker.process_once()
        except Exception as e:
            log.warning("startup queue scan failed (continuing): %s", e)
    elif not args.no_upload and not args.receiver_url:
        log.warning("no --receiver-url set; capture will stay local (use --no-upload to silence)")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.host, args.port))
    except OSError as e:
        log.error("failed to bind UDP %s:%s: %s", args.host, args.port, e)
        if worker:
            worker.stop()
        return 1
    sock.setblocking(False)
    log.info("UDP listening on %s:%s", args.host, args.port)

    session = CaptureSession(args.output_dir, args.host, args.port)
    session.start()

    stop = {"flag": False}

    def request_stop(*_args) -> None:
        if not stop["flag"]:
            log.info("stop requested; flushing capture...")
        stop["flag"] = True

    # Windows: SIGINT works for Ctrl+C; SIGTERM may not exist on all builds
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, request_stop)
        except Exception:
            pass

    deadline = None
    if args.duration is not None:
        deadline = time.monotonic() + float(args.duration)
        log.info("will stop after %.3f seconds", float(args.duration))

    packets_this_session = 0
    try:
        while not stop["flag"]:
            if deadline is not None and time.monotonic() >= deadline:
                log.info("duration elapsed")
                break
            # Short select timeout so Ctrl+C / duration are responsive
            try:
                readable, _, _ = select.select([sock], [], [], 0.25)
            except (InterruptedError, OSError):
                if stop["flag"]:
                    break
                continue
            if not readable:
                continue
            try:
                data, addr = sock.recvfrom(RECV_BUFSIZE)
            except BlockingIOError:
                continue
            except OSError as e:
                if stop["flag"]:
                    break
                log.warning("recvfrom error (continuing): %s", e)
                continue
            try:
                session.record(data, addr)
                packets_this_session += 1
                if packets_this_session == 1 or packets_this_session % 500 == 0:
                    log.debug(
                        "recorded %d packets (last from %s:%s len=%d)",
                        packets_this_session,
                        addr[0],
                        addr[1],
                        len(data),
                    )
            except Exception as e:
                session.app_dropped += 1
                log.warning("failed to write packet (continuing): %s", e)
    finally:
        try:
            sock.close()
        except Exception:
            pass
        session_dir = session.close(upload_pending=upload_enabled)
        if worker:
            worker.enqueue_now(session_dir)
            # Give the queue a short chance to upload before exit
            try:
                worker.process_once()
            except Exception as e:
                log.warning("final upload attempt failed: %s", e)
            worker.stop()
        log.info("collector stopped")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    return run_listener(args)


if __name__ == "__main__":
    sys.exit(main())
