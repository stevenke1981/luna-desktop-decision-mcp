# Luna Desktop Decision MCP

讓任何 MCP agent（Codex、ChatGPT 桌面版、Claude Code、Cursor…）在操作桌面**之前**，先用 OpenAI **Decisions API**（`POST /v1/decisions`，模型 `gpt-6-luna`）判斷這個操作該不該做。

流程：擷取主螢幕 → 在目標座標畫紅色十字 → 送 Decisions 問一組型別化問題（choice / predicate / score）→ 本地依門檻算出 `allow` / `confirm` / `deny`。Decisions 不回傳理由，所以原始機率會一併回傳以供稽核。

## 工具

| 工具 | 用途 |
|---|---|
| `capture_screen` | 主螢幕截圖（含 `scale`：screen = image × scale） |
| `judge_desktop_action` | 判斷單一操作：`allow` / `confirm`（先問使用者）/ `deny` |
| `choose_desktop_action` | 從多個候選操作中選最佳下一步（或 `none`） |
| `check_screen` | 對目前畫面評估是非條件（如「另存新檔對話框已開啟」），用來驗證操作結果 |
| `perform_desktop_action` | 先判斷，`allow` 才執行；`confirm` 需 `user_approved=true`；`deny` 永不執行。預設關閉 |

操作格式（與 OpenAI computer-use 動作相近）：

```json
{"type": "click", "x": 960, "y": 540, "button": "left", "description": "工具列的「儲存」按鈕"}
{"type": "type", "text": "你好"}            // 非 ASCII 走剪貼簿貼上
{"type": "keypress", "keys": ["CTRL", "S"]}
{"type": "drag", "path": [[100, 100], [400, 300]]}
{"type": "scroll", "x": 800, "y": 500, "scroll_y": 5}
```

座標預設是主螢幕實體像素（`coordinate_space: "screen"`）；若用 `capture_screen` 圖上的座標，傳 `coordinate_space: "image"`。

### 判斷問題與門檻

- `verdict`（choice：allow / confirm / deny）、`goal_aligned`、`irreversible`、`sensitive`、`blocking_dialog`、`target_visible`（僅指標類操作）、`risk`（score：low→critical）
- `deny` 機率 ≥ `LUNA_DENY_THRESHOLD`（0.5）或模型拒答 → **deny**
- verdict 非 allow、信心 < 0.5、不可逆 ≥ 0.4、敏感 ≥ 0.4、阻擋對話框 ≥ 0.6、目標對不上 < 0.5、目標不在 < 0.5、risk ≥ high → **confirm**
- 其餘 → **allow**

所有門檻都可用環境變數調整（見 `decisions.py` 的 `Thresholds`）。

## 安裝

```powershell
git clone https://github.com/stevenke1981/luna-desktop-decision-mcp.git
cd luna-desktop-decision-mcp
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python tests.py      # 離線測試，不呼叫 API、不動滑鼠
```

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `OPENAI_API_KEY` | — | 必填 |
| `LUNA_ALLOW_EXECUTE` | 未設 | 設 `1` 才允許 `perform_desktop_action` 真的動滑鼠鍵盤 |
| `LUNA_HTTP_ALLOW_EXECUTE` | 未設 | HTTP 模式下再額外設 `1` 才可執行 |
| `LUNA_CONFIRM_POLICY` | `approve` | `never` = `confirm` 判定一律不執行，即使 agent 聲稱已核准 |
| `LUNA_MCP_TOKEN` | — | HTTP 模式必填，≥ 24 字元隨機字串 |
| `LUNA_SCREENSHOT_MAX_WIDTH` | `1600` | 截圖縮放寬度（影響 token 成本） |
| `LUNA_IMAGE_DETAIL` | `high` | `low` / `high` / `auto` / `original` |
| `LUNA_AUDIT_LOG` | `~/.luna-desktop-decision/audit.jsonl` | `off` 關閉；輸入文字只記長度 |

## 接到各 agent

### Codex（CLI / IDE / Codex app）— stdio

`%USERPROFILE%\.codex\config.toml`：

```toml
[mcp_servers.luna-desktop]
command = 'C:\path\to\luna-desktop-decision-mcp\.venv\Scripts\python.exe'
args = ['C:\path\to\luna-desktop-decision-mcp\server.py']
env_vars = ["OPENAI_API_KEY"]          # 從父行程轉送；或改用 env = { OPENAI_API_KEY = "..." }
tool_timeout_sec = 60
# env = { LUNA_ALLOW_EXECUTE = "1" }   # 要讓 Codex 直接執行時才開
```

或：`codex mcp add luna-desktop -- C:\path\to\luna-desktop-decision-mcp\.venv\Scripts\python.exe C:\path\to\luna-desktop-decision-mcp\server.py`

### Claude Code — stdio

```powershell
claude mcp add luna-desktop -s user -e OPENAI_API_KEY=$env:OPENAI_API_KEY -- C:\path\to\luna-desktop-decision-mcp\.venv\Scripts\python.exe C:\path\to\luna-desktop-decision-mcp\server.py
```

### ChatGPT 桌面版 — Streamable HTTP

ChatGPT 的自訂連接器（設定 → Apps & Connectors → Advanced → Developer mode）只接受**遠端 HTTPS** MCP URL，所以要開 HTTP 模式並用通道公開：

```powershell
$env:LUNA_MCP_TOKEN = -join ((48..57 + 97..122) | Get-Random -Count 40 | % {[char]$_})
.venv\Scripts\python server.py --http --port 8765
cloudflared tunnel --url http://127.0.0.1:8765     # 或 ngrok http 8765
```

連接器 URL 填 `https://<通道網域>/mcp?token=<LUNA_MCP_TOKEN>`，驗證方式選 No authentication（token 在 URL 中；也接受 `Authorization: Bearer`）。
⚠️ 這等於把「看你螢幕」的能力公開到網路上：token 要保密、用完就關通道，HTTP 模式下建議**不要**開 `LUNA_HTTP_ALLOW_EXECUTE`，只用判斷工具。

### 其他 MCP 客戶端

stdio：`python server.py`；HTTP：`python server.py --http` → `http://127.0.0.1:8765/mcp`（需 token）。

## 建議給 agent 的指示

> 每次要點擊、輸入或按鍵操作桌面前，先呼叫 `judge_desktop_action`（附上 `description`）。`deny` 不要做；`confirm` 先向使用者說明並取得同意；做完後用 `check_screen` 驗證結果。

## 注意事項

- 每次判斷都會把截圖送到 OpenAI，會計費：實測一次判斷約 1,700 input tokens（$0.10 / 1M，輸出不計費）。畫面上有機密時請不要使用。
- 判斷結果只是機率性建議，不能取代人工確認；`user_approved` 由 agent 填寫，無法驗證，高風險環境請設 `LUNA_CONFIRM_POLICY=never`。
- 只支援主螢幕；需要中止時把滑鼠甩到螢幕角落（PyAutoGUI failsafe）。
- 以系統管理員身分執行的視窗（UAC 提升權限）無法被一般權限的行程操作。
