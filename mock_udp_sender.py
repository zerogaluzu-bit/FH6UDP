#!/usr/bin/env python3
"""
Send fake UDP packets for local smoke tests. Not Forza telemetry.
"""

from __future__ import annotations

import argparse
import socket
import time


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock FH6 UDP sender for smoke tests")
    parser.add_argument("--host", default="127.0.0.1", help="Target host")
    parser.add_argument("--port", type=int, default=9999, help="Target UDP port")
    parser.add_argument("--count", type=int, default=20, help="Number of packets")
    parser.add_argument("--interval", type=float, default=0.02, help="Seconds between packets")
    parser.add_argument(
        "--payload-prefix",
        default="MOCK",
        help="ASCII prefix; remaining bytes are a zero-padded index",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=64,
        help="Payload size in bytes (minimum 8)",
    )
    args = parser.parse_args()

    size = max(8, args.size)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for i in range(args.count):
            body = f"{args.payload_prefix}:{i:08d}".encode("ascii")
            if len(body) < size:
                payload = body + bytes(size - len(body))
            else:
                payload = body[:size]
            # Embed index as big-endian uint32 at end for integrity checks
            payload = payload[:-4] + i.to_bytes(4, "big")
            sock.sendto(payload, (args.host, args.port))
            print(f"sent {i + 1}/{args.count} len={len(payload)} -> {args.host}:{args.port}")
            if args.interval > 0 and i + 1 < args.count:
                time.sleep(args.interval)
    finally:
        sock.close()
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
