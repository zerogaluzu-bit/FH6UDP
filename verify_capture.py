#!/usr/bin/env python3
"""
Verify a capture session container (packets.bin + optional manifest.json).

Validates binary schema, packet count, payload lengths, and file integrity.
Does NOT parse Forza Horizon telemetry fields.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from capture_format import (
    MANIFEST_FILENAME,
    PACKETS_FILENAME,
    read_json,
    read_packets,
    verify_packets_file,
)


def verify_session(path: Path, *, expect_packets: int | None = None) -> dict:
    path = Path(path)
    if path.is_dir():
        packets_path = path / PACKETS_FILENAME
        manifest_path = path / MANIFEST_FILENAME
    else:
        packets_path = path
        manifest_path = path.parent / MANIFEST_FILENAME

    if not packets_path.is_file():
        raise FileNotFoundError(f"packets file not found: {packets_path}")

    result = verify_packets_file(packets_path)

    # Extra sequential read for first/last summary
    first = last = None
    for i, pkt in enumerate(read_packets(packets_path)):
        if i == 0:
            first = {
                "source_ip": pkt.source_ip,
                "source_port": pkt.source_port,
                "payload_len": pkt.payload_len,
                "recv_unix_us": pkt.recv_unix_us,
            }
        last = {
            "source_ip": pkt.source_ip,
            "source_port": pkt.source_port,
            "payload_len": pkt.payload_len,
            "recv_unix_us": pkt.recv_unix_us,
        }
    result["first_packet"] = first
    result["last_packet"] = last

    if expect_packets is not None and result["packet_count"] != expect_packets:
        raise ValueError(
            f"packet_count mismatch: expected {expect_packets}, got {result['packet_count']}"
        )

    if manifest_path.is_file():
        manifest = read_json(manifest_path)
        result["manifest"] = {
            "session_id": manifest.get("session_id"),
            "packet_count": manifest.get("packet_count"),
            "total_payload_bytes": manifest.get("total_payload_bytes"),
            "capture_file_sha256": manifest.get("capture_file_sha256"),
            "upload_status": manifest.get("upload_status"),
        }
        mismatches = []
        if manifest.get("packet_count") != result["packet_count"]:
            mismatches.append("packet_count")
        if manifest.get("total_payload_bytes") != result["total_payload_bytes"]:
            mismatches.append("total_payload_bytes")
        sha = manifest.get("capture_file_sha256")
        if sha and sha != result["sha256"]:
            mismatches.append("capture_file_sha256")
        result["manifest_matches"] = len(mismatches) == 0
        result["manifest_mismatches"] = mismatches
        if mismatches:
            raise ValueError(f"manifest mismatch fields: {mismatches}")
    else:
        result["manifest"] = None
        result["manifest_matches"] = None

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify FH6 capture container")
    parser.add_argument(
        "path",
        type=Path,
        help="Path to session directory or packets.bin",
    )
    parser.add_argument(
        "--expect-packets",
        type=int,
        default=None,
        help="Fail if packet count differs",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print result as JSON",
    )
    args = parser.parse_args(argv)

    try:
        result = verify_session(args.path, expect_packets=args.expect_packets)
    except Exception as e:
        print(f"VERIFY FAILED: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("VERIFY OK")
        print(f"  path:                 {result['path']}")
        print(f"  schema_version:       {result['schema_version']}")
        print(f"  file_size:            {result['file_size']}")
        print(f"  packet_count:         {result['packet_count']}")
        print(f"  total_payload_bytes:  {result['total_payload_bytes']}")
        print(f"  sha256:               {result['sha256']}")
        if result.get("manifest"):
            print(f"  session_id:           {result['manifest']['session_id']}")
            print(f"  upload_status:        {result['manifest']['upload_status']}")
            print(f"  manifest_matches:     {result['manifest_matches']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
