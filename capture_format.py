"""
FH6 capture container: lossless UDP datagram recording.

packets.bin record layout (little-endian), schema version 1:

  Offset  Size  Type     Field
  ------  ----  -------  -----------------------------------------------
  0       4     bytes    magic = b'FH6P'
  4       2     uint16   version = 1
  6       8     int64    recv_unix_us  (Unix epoch, microsecond precision)
  14      8     int64    mono_ns       (time.monotonic_ns())
  22      1     uint8    addr_family   (4 = IPv4, 6 = IPv6)
  23      16    bytes    src_addr      (IPv4 in [0:4] rest 0; IPv6 full 16)
  39      2     uint16   src_port
  41      4     uint32   payload_len
  45      N     bytes    payload       (raw UDP datagram, unmodified)

Fixed header size: 45 bytes. Byte order: little-endian for all multi-byte integers.
Addresses are stored in network byte order (big-endian octets), zero-padded for IPv4.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Optional

SCHEMA_VERSION = 1
APPLICATION_VERSION = "1.0.0"
MAGIC = b"FH6P"
HEADER_STRUCT = struct.Struct("<4sHqqB16sHI")  # 45 bytes
HEADER_SIZE = HEADER_STRUCT.size
assert HEADER_SIZE == 45

UPLOAD_STATUS_RECORDING = "recording"
UPLOAD_STATUS_PENDING = "pending"
UPLOAD_STATUS_UPLOADING = "uploading"
UPLOAD_STATUS_UPLOADED = "uploaded"
UPLOAD_STATUS_FAILED = "failed"

STATUS_FILENAME = ".capture_status"
MANIFEST_FILENAME = "manifest.json"
PACKETS_FILENAME = "packets.bin"


@dataclass(frozen=True)
class PacketRecord:
    recv_unix_us: int
    mono_ns: int
    source_ip: str
    source_port: int
    payload: bytes

    @property
    def payload_len(self) -> int:
        return len(self.payload)


def _pack_address(source_ip: str) -> tuple[int, bytes]:
    addr = ipaddress.ip_address(source_ip)
    if isinstance(addr, ipaddress.IPv4Address):
        return 4, addr.packed + bytes(12)
    return 6, addr.packed


def _unpack_address(addr_family: int, raw: bytes) -> str:
    if addr_family == 4:
        return str(ipaddress.IPv4Address(raw[:4]))
    if addr_family == 6:
        return str(ipaddress.IPv6Address(raw[:16]))
    raise ValueError(f"unsupported addr_family: {addr_family}")


def encode_packet(
    recv_unix_us: int,
    mono_ns: int,
    source_ip: str,
    source_port: int,
    payload: bytes,
) -> bytes:
    if source_port < 0 or source_port > 65535:
        raise ValueError(f"invalid source_port: {source_port}")
    if len(payload) > 0xFFFFFFFF:
        raise ValueError("payload too large")
    family, packed_addr = _pack_address(source_ip)
    header = HEADER_STRUCT.pack(
        MAGIC,
        SCHEMA_VERSION,
        int(recv_unix_us),
        int(mono_ns),
        family,
        packed_addr,
        int(source_port),
        len(payload),
    )
    return header + payload


def decode_packet_header(header: bytes) -> tuple[int, int, str, int, int]:
    if len(header) != HEADER_SIZE:
        raise ValueError(f"header must be {HEADER_SIZE} bytes, got {len(header)}")
    magic, version, recv_unix_us, mono_ns, family, src_addr, src_port, payload_len = (
        HEADER_STRUCT.unpack(header)
    )
    if magic != MAGIC:
        raise ValueError(f"bad magic: {magic!r}")
    if version != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema version: {version}")
    source_ip = _unpack_address(family, src_addr)
    return recv_unix_us, mono_ns, source_ip, src_port, payload_len


def read_packets(path: Path | str) -> Iterator[PacketRecord]:
    path = Path(path)
    with path.open("rb") as fh:
        yield from iter_packets(fh)


def iter_packets(fh: BinaryIO) -> Iterator[PacketRecord]:
    while True:
        header = fh.read(HEADER_SIZE)
        if not header:
            return
        if len(header) < HEADER_SIZE:
            raise ValueError(
                f"truncated packets.bin: incomplete header ({len(header)} bytes)"
            )
        recv_unix_us, mono_ns, source_ip, src_port, payload_len = decode_packet_header(
            header
        )
        payload = fh.read(payload_len)
        if len(payload) != payload_len:
            raise ValueError(
                f"truncated packets.bin: expected payload {payload_len} bytes, "
                f"got {len(payload)}"
            )
        yield PacketRecord(
            recv_unix_us=recv_unix_us,
            mono_ns=mono_ns,
            source_ip=source_ip,
            source_port=src_port,
            payload=payload,
        )


def verify_packets_file(path: Path | str) -> dict:
    """Validate packets.bin integrity. Does not parse Forza telemetry fields."""
    path = Path(path)
    packet_count = 0
    total_payload_bytes = 0
    file_size = path.stat().st_size
    consumed = 0
    for pkt in read_packets(path):
        packet_count += 1
        total_payload_bytes += pkt.payload_len
        consumed += HEADER_SIZE + pkt.payload_len
    if consumed != file_size:
        raise ValueError(
            f"size mismatch: consumed {consumed} bytes, file size {file_size}"
        )
    return {
        "ok": True,
        "path": str(path),
        "file_size": file_size,
        "packet_count": packet_count,
        "total_payload_bytes": total_payload_bytes,
        "schema_version": SCHEMA_VERSION,
        "sha256": file_sha256(path),
    }


def file_sha256(path: Path | str) -> str:
    path = Path(path)
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def unix_us_now() -> int:
    return time.time_ns() // 1_000


def new_session_id() -> str:
    # Compact UTC-based id + short random suffix for uniqueness
    import secrets
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_{secrets.token_hex(4)}"


def utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def write_json(path: Path | str, data: dict) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path | str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_status(session_dir: Path, status: str) -> None:
    (session_dir / STATUS_FILENAME).write_text(status + "\n", encoding="utf-8")


def read_status(session_dir: Path) -> Optional[str]:
    status_path = session_dir / STATUS_FILENAME
    if not status_path.exists():
        return None
    return status_path.read_text(encoding="utf-8").strip()


def build_manifest(
    *,
    session_id: str,
    created_at_utc: str,
    closed_at_utc: Optional[str],
    udp_bind_host: str,
    udp_port: int,
    packet_count: int,
    total_payload_bytes: int,
    dropped_packet_count: Optional[int],
    hostname: str,
    python_version: str,
    capture_file_sha256: Optional[str],
    upload_status: str,
    notes: Optional[dict] = None,
) -> dict:
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "created_at_utc": created_at_utc,
        "closed_at_utc": closed_at_utc,
        "udp_bind_host": udp_bind_host,
        "udp_port": udp_port,
        "packet_count": packet_count,
        "total_payload_bytes": total_payload_bytes,
        "dropped_packet_count": dropped_packet_count,
        "hostname": hostname,
        "python_version": python_version,
        "application_version": APPLICATION_VERSION,
        "capture_file_sha256": capture_file_sha256,
        "upload_status": upload_status,
    }
    if notes is not None:
        manifest["notes"] = notes
    return manifest


class CaptureWriter:
    """Append-only packets.bin writer for one session."""

    def __init__(self, session_dir: Path):
        self.session_dir = Path(session_dir)
        self.packets_path = self.session_dir / PACKETS_FILENAME
        self._fh: Optional[BinaryIO] = None
        self.packet_count = 0
        self.total_payload_bytes = 0

    def open(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._fh = self.packets_path.open("ab")

    def write_datagram(
        self,
        payload: bytes,
        source_ip: str,
        source_port: int,
        recv_unix_us: Optional[int] = None,
        mono_ns: Optional[int] = None,
    ) -> None:
        if self._fh is None:
            raise RuntimeError("CaptureWriter is not open")
        record = encode_packet(
            recv_unix_us if recv_unix_us is not None else unix_us_now(),
            mono_ns if mono_ns is not None else time.monotonic_ns(),
            source_ip,
            source_port,
            payload,
        )
        self._fh.write(record)
        self.packet_count += 1
        self.total_payload_bytes += len(payload)

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None
