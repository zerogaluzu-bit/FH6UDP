"""Container encode/decode, payload integrity, and manifest SHA-256 tests."""

from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from pathlib import Path

from capture_format import (
    HEADER_SIZE,
    CaptureWriter,
    build_manifest,
    encode_packet,
    file_sha256,
    read_packets,
    verify_packets_file,
    write_json,
)


class CaptureFormatTests(unittest.TestCase):
    def test_round_trip_encode_decode(self) -> None:
        payloads = [
            b"",
            b"\x00\x01\x02",
            bytes(range(256)),
            b"\xff" * 311,  # typical-ish Forza-sized blob, still opaque
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "packets.bin"
            writer = CaptureWriter(Path(tmp))
            writer.open()
            expected = []
            for i, payload in enumerate(payloads):
                recv_us = 1_700_000_000_000_000 + i
                mono = 1000 + i * 10
                ip = "192.168.1.50"
                port = 50000 + i
                writer.write_datagram(
                    payload,
                    ip,
                    port,
                    recv_unix_us=recv_us,
                    mono_ns=mono,
                )
                expected.append((recv_us, mono, ip, port, payload))
            writer.close()

            decoded = list(read_packets(path))
            self.assertEqual(len(decoded), len(expected))
            for got, exp in zip(decoded, expected):
                self.assertEqual(got.recv_unix_us, exp[0])
                self.assertEqual(got.mono_ns, exp[1])
                self.assertEqual(got.source_ip, exp[2])
                self.assertEqual(got.source_port, exp[3])
                self.assertEqual(got.payload, exp[4])

            info = verify_packets_file(path)
            self.assertTrue(info["ok"])
            self.assertEqual(info["packet_count"], len(payloads))
            self.assertEqual(
                info["total_payload_bytes"], sum(len(p) for p in payloads)
            )

    def test_payload_bytes_integrity_no_mutation(self) -> None:
        # Include bytes that would break if JSON-escaped or UTF-8 decoded
        payload = bytes([0, 10, 13, 34, 92, 127, 128, 255]) + b'{"fake":true}'
        record = encode_packet(
            recv_unix_us=1234567890123456,
            mono_ns=999,
            source_ip="10.0.0.8",
            source_port=4242,
            payload=payload,
        )
        self.assertEqual(record[:4], b"FH6P")
        self.assertEqual(len(record), HEADER_SIZE + len(payload))
        self.assertEqual(record[HEADER_SIZE:], payload)

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "packets.bin"
            p.write_bytes(record + encode_packet(1, 2, "127.0.0.1", 9, b"abc"))
            pkts = list(read_packets(p))
            self.assertEqual(pkts[0].payload, payload)
            self.assertEqual(pkts[1].payload, b"abc")

    def test_ipv6_address_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            w = CaptureWriter(Path(tmp))
            w.open()
            w.write_datagram(b"x", "2001:db8::1", 9999, recv_unix_us=1, mono_ns=2)
            w.close()
            pkt = next(read_packets(Path(tmp) / "packets.bin"))
            self.assertEqual(pkt.source_ip, "2001:db8::1")
            self.assertEqual(pkt.payload, b"x")

    def test_manifest_sha256_matches_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            w = CaptureWriter(session)
            w.open()
            for i in range(5):
                w.write_datagram(f"p{i}".encode(), "127.0.0.1", 1000 + i)
            w.close()
            packets = session / "packets.bin"
            sha = file_sha256(packets)
            # Independent hash
            independent = hashlib.sha256(packets.read_bytes()).hexdigest()
            self.assertEqual(sha, independent)

            manifest = build_manifest(
                session_id="test_session",
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
                upload_status="pending",
            )
            write_json(session / "manifest.json", manifest)
            self.assertEqual(manifest["capture_file_sha256"], independent)
            self.assertEqual(manifest["packet_count"], 5)

    def test_truncated_file_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "packets.bin"
            good = encode_packet(1, 2, "1.2.3.4", 5, b"hello")
            p.write_bytes(good[:-2])  # truncate payload
            with self.assertRaises(ValueError):
                verify_packets_file(p)


if __name__ == "__main__":
    unittest.main()
