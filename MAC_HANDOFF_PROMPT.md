# Mac 端交接 Prompt（FH6 Telemetry）

把下面整段複製給 Mac 端工程師 / Agent 即可。

---

## Prompt（直接貼上）

你是 FH6 Telemetry 教練系統的 **Mac 端**負責人。Windows 端資料採集器已經完成並上線，倉庫：

https://github.com/zerogaluzu-bit/FH6UDP

標題／專案名：`FH6 UDP listener`

你的任務是實作 **正式 Mac HTTP receiver + 後續 Forza binary 解析／教練管線**。不要重做 Windows 採集器。

---

### 系統分工（必須遵守）

**Windows 端（已完成，不要改契約）：**

1. 接收 Forza Horizon 6 UDP Data Out
2. **不修改** UDP payload
3. 不解析 Forza telemetry fields
4. 以 lossless capture container 存本地
5. session 關閉後，用 HTTP multipart 上傳到 Mac
6. 上傳失敗會本地 queue + exponential backoff 重試；成功後**不刪**本地檔

**Mac 端（你負責）：**

1. 提供正式 HTTP receiver（Windows 的 `mock_http_receiver.py` 只供本機測試，不是正式實作）
2. 接收 `manifest.json` + `packets.bin`
3. 驗證完整性（尤其 sha256 / session_id）
4. 依原始 binary payload 自行解析 Forza telemetry
5. 之後才做 lap/sector、教練分析、視覺化等（可另開階段）

邊界：Windows = 採集與傳輸；Mac = 解析與教練。

---

### HTTP 上傳契約（Mac 必須實作）

**Endpoint（建議）：**

`POST /api/v1/captures`

Windows 會把完整 URL 配成例如：

`http://<mac-lan-ip>:8765/api/v1/captures`

**Request：`multipart/form-data`**

| Form field | Filename | Content |
|---|---|---|
| `manifest` | `manifest.json` | UTF-8 JSON |
| `capture` | `packets.bin` | raw binary |

**Request headers：**

| Header | 說明 |
|---|---|
| `Content-Type` | `multipart/form-data; boundary=...` |
| `X-Capture-Schema-Version` | 目前固定 `1` |
| `X-Session-ID` | 與 manifest 內 `session_id` 相同 |
| `Authorization` | 可選：`Bearer <token>`（僅當 Windows 設定了 auth env） |

**成功回應：HTTP 2xx + JSON**

```json
{
  "ok": true,
  "session_id": "<與上傳相同>",
  "sha256": "<packets.bin 的 SHA-256 hex>",
  "stored": true
}
```

Windows 端**只有**在以下全部成立時才標記 `uploaded`：

1. HTTP status 為 2xx
2. `ok === true`
3. `session_id` 與本地相同
4. `sha256` 與本地 `packets.bin` 相同
5. `stored === true`

**Idempotency（必須）：**

- 以 `session_id` 做幂等鍵
- 同一 `session_id` + 相同 `packets.bin` bytes 再次 POST：仍回成功（可加 `"duplicate": true`，但上面四個必要欄位仍要對）
- 同一 `session_id` 但內容不同：回 409 或明確錯誤，不要靜默覆蓋成錯誤資料

**失敗時：**

- 回非 2xx 或 JSON 不合約即可
- Windows 會保留本地檔並重試；不要假設只會傳一次

---

### `packets.bin` 二進位 schema（schema_version = 1）

檔案是多筆 record 串接；每筆對應一個原始 UDP datagram，**順序 = 接收順序**。

所有多位元整數：**little-endian**。  
固定 header：**45 bytes**。  
Python struct：`<4sHqqB16sHI`

| Offset | Size | Type | Field |
|---:|---:|---|---|
| 0 | 4 | bytes | magic = `FH6P` |
| 4 | 2 | uint16 | version = `1` |
| 6 | 8 | int64 | `recv_unix_us`（Unix epoch，微秒） |
| 14 | 8 | int64 | `mono_ns`（Windows `time.monotonic_ns()`） |
| 22 | 1 | uint8 | `addr_family`：`4`=IPv4，`6`=IPv6 |
| 23 | 16 | bytes | `src_addr`：IPv4 在 `[0:4]`（network order）其餘 0；IPv6 用滿 16 |
| 39 | 2 | uint16 | `src_port` |
| 41 | 4 | uint32 | `payload_len` |
| 45 | N | bytes | `payload` = **原始 Forza UDP datagram，未修改** |

重點：

- Mac 解析 Forza 時只用每筆 record 的 `payload`
- 不要假設 payload 是 JSON
- 不要截斷、重排、轉碼
- `recv_unix_us` / `mono_ns` 可用於時間軸；真正賽道時間仍以 Forza 欄位為準（由你解析）

---

### `manifest.json` 欄位

至少會有：

```json
{
  "schema_version": 1,
  "session_id": "20260718T071634Z_4c0fbf3f",
  "created_at_utc": "...",
  "closed_at_utc": "...",
  "udp_bind_host": "0.0.0.0",
  "udp_port": 9999,
  "packet_count": 1234,
  "total_payload_bytes": 567890,
  "dropped_packet_count": null,
  "hostname": "WINDOWS-PC",
  "python_version": "3.12.x",
  "application_version": "1.0.0",
  "capture_file_sha256": "<hex>",
  "upload_status": "pending",
  "notes": {
    "dropped_packet_count_note": "..."
  }
}
```

說明：

- `dropped_packet_count` 常常是 `null`（Windows UDP 不一定能可靠讀 OS drop counter）
- `capture_file_sha256` 是整個 `packets.bin` 的 SHA-256
- Mac 應自行重算 sha256，並與 manifest / 回應欄位一致
- 不要期待 manifest 含 username / API key

---

### Session 行為（Windows 現況）

- 程式啟動 = 開一個 capture session
- Ctrl+C / GUI Stop / `--duration` = close session
- **不會**自動依圈數切 session
- 只上傳已關閉的 session（不會邊收 UDP 邊傳同一個未完成檔）
- 本地目錄：`captures/<session_id>/{manifest.json,packets.bin}`

---

### Mac 端建議實作順序

**Phase A — Receiver（先做這個，才能對接 Windows）**

1. HTTP server 聽 LAN（例如 `:8765`）
2. 實作 `POST /api/v1/captures`
3. 解析 multipart，取出 `manifest` / `capture`
4. 驗證 headers、`session_id`、sha256
5. 持久化存放（建議 `store/<session_id>/`）
6. 幂等處理
7. 可選 Bearer token 驗證
8. 用 Windows 倉庫內工具對測：
   - `python mock_udp_sender.py ...`（Windows）
   - 或直接拿一份真實 `captures/<id>/` 上傳

**Phase B — Parse Forza payload**

1. 依 FH6 Data Out 格式解析 `payload` bytes
2. 產出你自己的 frame/telemetry 結構（不要要求 Windows 改格式）
3. 用 `recv_unix_us` 或 Forza 時間戳建立時間軸

**Phase C — 教練系統（後續）**

- lap/sector detection
- baseline comparison / DTW
- dashboard / AI coach
- 這些都不要回灌進 Windows 採集契約

---

### 對接驗收清單（Mac receiver）

- [ ] 從 Windows GUI/CLI 上傳成功，Windows manifest `upload_status` 變 `uploaded`
- [ ] 回應 `sha256` 與檔案一致
- [ ] 重複上傳同一 session 不會炸、也不產生錯誤重複資料
- [ ] 故意回錯 sha256 / 非 2xx 時，Windows 會重試且本地檔仍在
- [ ] 能從 `packets.bin` 還原每一個 datagram 的 payload 與順序
- [ ] 不依賴 Windows 幫你解析 Forza fields

---

### Windows 端怎麼連你

使用者在 Windows 上會這樣設（範例）：

```text
Receiver URL = http://<你的Mac區網IP>:8765/api/v1/captures
Auth env name = FH6_UPLOAD_TOKEN   (可選)
```

GUI：

```powershell
python udp_listener_gui.py
```

CLI：

```powershell
python udp_listener.py --port 9999 --receiver-url http://<mac-ip>:8765/api/v1/captures
```

請確保：

- Mac 防火牆放行該 TCP port
- 兩台在同一區網
- 回傳嚴格符合上述 JSON 契約

---

### 參考實作（僅供協議對照，非正式 Mac 產品）

Windows 倉庫內：

- `mock_http_receiver.py`：本機協議 mock（可看成功/幂等行為）
- `upload_queue.py`：Windows 實際上傳與驗收邏輯
- `capture_format.py`：`packets.bin` encode/decode
- `verify_capture.py`：container 驗證（不解析 Forza）
- `README.md`：完整契約文件

正式 Mac receiver 請用你選定的 Swift/Vapor/Node/Python 等實作，但 **wire contract 必須相容**。

---

### 明確不要做的事

- 不要要求 Windows 改成 JSONL telemetry frames
- 不要在上傳前要求 Windows 解析 Forza
- 不要在成功後要求 Windows 刪本地原始檔
- 不要打破 multipart field 名稱（必須是 `manifest` + `capture`）
- 不要回傳缺欄位的「成功」JSON（Windows 會當失敗並重試）

---

### 你完成 Phase A 後應回報

1. Receiver listen address / port
2. 是否啟用 auth
3. 儲存路徑與幂等策略
4. 用 Windows 實機或 mock 的對測結果
5. 下一步 Forza parser 計劃（packet format / 欄位來源）

現在請從 **Phase A：正式 Mac HTTP receiver** 開始實作。
