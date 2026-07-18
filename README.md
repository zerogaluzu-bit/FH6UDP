# FH6 Telemetry — Windows UDP Collector

Windows-side data capture and transport for the FH6 Telemetry coaching system.

## 1. Purpose and responsibility boundary

**This project only:**

1. Receives Forza Horizon 6 UDP Data Out packets
2. Stores each raw UDP datagram losslessly on disk
3. Uploads completed capture sessions to a Mac HTTP receiver
4. Retries failed uploads from a local queue

**This project does not:**

- Parse Forza telemetry fields
- Detect laps/sectors
- Run coaching / DTW / baseline comparison
- Render dashboards or charts
- Implement the production Mac receiver

The Mac side owns binary packet parsing and all coaching logic. Windows must not mutate payload bytes.

## 2. Install

Requirements: **Python 3.11+** on Windows 10/11.

```powershell
cd C:\Users\Administrator\FH6UDP
python --version
# No pip packages required (stdlib only). See requirements.txt.
```

Optional: copy and edit config:

```powershell
copy config.example.json config.json
```

## 3. Forza Horizon 6 Data Out setup

In FH6 HUD / Telemetry settings (wording varies by build):

1. Enable **Data Out**
2. Set IP to the **Windows PC** running this collector (LAN IPv4)
3. Set port to **9999** (or the `--port` you choose)
4. Choose the packet format your Mac parser expects (Windows stores bytes as-is)
5. Start driving; packets should arrive while this listener is running

Ensure the Windows firewall allows inbound UDP on that port (see section 10).

## 4. CLI parameters

| Flag | Description | Default |
|------|-------------|---------|
| `--config PATH` | JSON config file; CLI overrides file values | none |
| `--host HOST` | UDP bind address | `0.0.0.0` |
| `--port PORT` | UDP bind port | `9999` |
| `--output-dir DIR` | Capture sessions root | `captures` |
| `--receiver-url URL` | Mac upload endpoint | none |
| `--auth-token-env NAME` | Env var holding Bearer token | none |
| `--duration SEC` | Auto-stop after N seconds | none (run until Ctrl+C) |
| `--no-upload` | Local capture only | off |
| `--http-timeout SEC` | HTTP timeout | `30` |
| `--verbose` | Debug logging (tokens never logged) | off |

Example:

```powershell
python udp_listener.py --port 9999 --receiver-url http://192.168.1.20:8765/api/v1/captures
```

Auth token (never put secrets in CLI or config values):

```powershell
$env:FH6_UPLOAD_TOKEN = "your-token-here"
python udp_listener.py --config config.json --auth-token-env FH6_UPLOAD_TOKEN
```

## 5. config.json example

See `config.example.json`:

```json
{
  "host": "0.0.0.0",
  "port": 9999,
  "output_dir": "captures",
  "receiver_url": "http://192.168.1.20:8765/api/v1/captures",
  "auth_token_env": "FH6_UPLOAD_TOKEN",
  "duration": null,
  "no_upload": false,
  "http_timeout": 30,
  "verbose": false
}
```

Do **not** store Mac IP secrets as hard-coded defaults in source. Do **not** put the Bearer token itself in JSON—only the environment variable name.

## 6. Capture binary schema (`packets.bin`)

Schema version **1**. All multi-byte integers are **little-endian**.

Each UDP datagram is one record, written in receive order:

| Offset | Size | Type | Field |
|-------:|-----:|------|-------|
| 0 | 4 | bytes | `magic` = `FH6P` (`0x46 0x48 0x36 0x50`) |
| 4 | 2 | uint16 | `version` = `1` |
| 6 | 8 | int64 | `recv_unix_us` — Unix epoch time in **microseconds** |
| 14 | 8 | int64 | `mono_ns` — `time.monotonic_ns()` at receive |
| 22 | 1 | uint8 | `addr_family` — `4` = IPv4, `6` = IPv6 |
| 23 | 16 | bytes | `src_addr` — IPv4 in bytes `[0:4]` (network order), remaining 12 zero; IPv6 full 16 octets |
| 39 | 2 | uint16 | `src_port` |
| 41 | 4 | uint32 | `payload_len` |
| 45 | N | bytes | `payload` — **raw UDP datagram bytes, unmodified** |

Fixed header size: **45 bytes**.

Python struct format: `<4sHqqB16sHI`.

Payload is never JSON-encoded, truncated, reordered, or field-parsed.

Session layout:

```text
captures/<session_id>/
  .capture_status    # recording | pending | uploading | uploaded | failed | local_only
  manifest.json
  packets.bin
```

## 7. Manifest schema (`manifest.json`)

| Field | Type | Notes |
|-------|------|-------|
| `schema_version` | int | `1` |
| `session_id` | string | Unique per session |
| `created_at_utc` | string | ISO-8601 UTC |
| `closed_at_utc` | string\|null | Set when session closes |
| `udp_bind_host` | string | Bind host used |
| `udp_port` | int | Bind port used |
| `packet_count` | int | Records in `packets.bin` |
| `total_payload_bytes` | int | Sum of payload lengths |
| `dropped_packet_count` | int\|null | `null` if not reliably detectable; see `notes` |
| `hostname` | string | Machine hostname only |
| `python_version` | string | e.g. `3.12.10` |
| `application_version` | string | Collector version |
| `capture_file_sha256` | string\|null | SHA-256 hex of `packets.bin` after close |
| `upload_status` | string | `recording` / `pending` / `uploading` / `uploaded` / `failed` / `local_only` |
| `notes` | object | Optional; explains drop counter limits |

No usernames, API keys, or tokens are stored in the manifest.

## 8. HTTP request/response contract

Upload runs **only after** the session is closed and flushed (never while `packets.bin` is still being written).

`POST {receiver_url}` as `multipart/form-data`:

- field `manifest` → `manifest.json`
- field `capture` → `packets.bin`

Headers:

- `X-Capture-Schema-Version: 1`
- `X-Session-ID: <session_id>`
- `Authorization: Bearer <token>` — only if `--auth-token-env` is set and the env var is non-empty

Success response (HTTP 2xx) JSON:

```json
{
  "ok": true,
  "session_id": "<same as uploaded>",
  "sha256": "<sha256 of packets.bin>",
  "stored": true
}
```

Windows marks `uploaded` only when **all** are true:

1. HTTP status 2xx
2. `session_id` matches local session
3. `sha256` matches local `packets.bin`
4. `stored === true`

Local capture files are **never deleted** after a successful upload.

## 9. Local queue, retry, and recovery

- Closed sessions are marked `pending` and enter the upload queue
- A background worker uploads with **bounded exponential backoff** (starts at 1s, caps at 60s)
- Each cycle attempts a limited number of uploads so retries cannot block forever
- UDP receive errors / HTTP failures are logged; they must not crash the listener
- On startup, the worker scans `output_dir` for non-uploaded closed sessions and retries
- `session_id` is sent every time so the Mac can implement **idempotency** (duplicate POST of the same session+bytes should succeed without creating a second logical capture)

Status file `.capture_status` tracks lifecycle: write under `recording`, then `pending` after close.

## 10. Windows Firewall notes

If FH6 runs on another machine (or the same PC with strict rules), allow inbound UDP:

```powershell
# Example: allow UDP 9999 (run elevated PowerShell)
New-NetFirewallRule -DisplayName "FH6 UDP Collector" -Direction Inbound -Protocol UDP -LocalPort 9999 -Action Allow
```

Also confirm:

- Bind host `0.0.0.0` (default) listens on all IPv4 interfaces
- Game Data Out IP matches this PC’s LAN address
- No other process is bound to the same UDP port

## 11. Verify captures with `verify_capture.py`

```powershell
python verify_capture.py captures\<session_id>
python verify_capture.py captures\<session_id>\packets.bin --expect-packets 100
python verify_capture.py captures\<session_id> --json
```

Checks magic/version, sequential decode, payload lengths, file size integrity, and (if present) manifest counters / SHA-256. Does **not** parse Forza fields.

## 12. Local smoke test

Terminal A — mock HTTP receiver (protocol test only, not production Mac):

```powershell
python mock_http_receiver.py --host 127.0.0.1 --port 8765
```

Terminal B — collector:

```powershell
python udp_listener.py --host 127.0.0.1 --port 9999 --receiver-url http://127.0.0.1:8765/api/v1/captures --duration 5
```

Terminal C — fake UDP packets:

```powershell
python mock_udp_sender.py --host 127.0.0.1 --port 9999 --count 30
```

Then verify:

```powershell
python verify_capture.py captures\<latest-session-id>
```

Automated tests:

```powershell
python -m unittest discover -s tests -v
```

## 13. Known limitations

- One capture session per process run (start → Ctrl+C / `--duration` → close). No automatic lap splitting.
- OS UDP drops are not reliably exposed on all Windows setups; `dropped_packet_count` is usually `null`.
- IPv4 UDP socket only (`AF_INET`). IPv6 source addresses are supported in the container format if provided by the stack, but the listener binds IPv4.
- Upload requires a reachable `receiver_url`; without it, sessions stay local (`local_only` / pending depending on flags).
- `mock_http_receiver.py` is for Windows-side protocol testing only.
- Very high packet rates may still lose datagrams in the OS socket buffer before userspace reads them.

## Project files

| File | Role |
|------|------|
| `udp_listener.py` | Main Windows collector |
| `capture_format.py` | Binary container + manifest helpers |
| `upload_queue.py` | HTTP multipart upload + retry queue |
| `verify_capture.py` | Container verifier |
| `mock_udp_sender.py` | Fake UDP traffic for smoke tests |
| `mock_http_receiver.py` | Local upload protocol mock |
| `config.example.json` | Example configuration |
| `requirements.txt` | Dependency note (stdlib only) |
| `tests/` | Unit and integration tests |
