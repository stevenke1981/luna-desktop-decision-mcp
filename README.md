# Luna Desktop Decision MCP

讓任何 MCP agent（Codex、ChatGPT 桌面版、Claude Code、小模型…）**快速又安全地操作 Windows 桌面**。
判斷交給 OpenAI **Decisions API**（`POST /v1/decisions`，模型 `gpt-6-luna`，約比 Responses API 快 10 倍、輸出不計費）。

Decisions 只回傳型別化答案（choice／predicate／score），不會回文字或座標。所以由伺服器把畫面轉成「可以選的東西」：

1. **UI Automation** 列出前景視窗的可點元素（名稱、類型、精確座標）——本機、0.3–0.6 秒、免費。
2. **先在本機比對名稱**；只有比對不出唯一結果時才請 Luna 在候選元素中選（choice）。
3. 看不到元素時改用**格線**：Luna 選欄、選列 → 放大再選 → 換算成座標。
4. 回應給 agent 的是**可直接呼叫的參數**（`next`）與**狀態碼**，能力較弱的 agent 照著做就行。

## 工具（預設 5 個）

| 工具 | 用途 | Luna 呼叫 |
|---|---|---|
| `focus_window(title_contains)` | 把指定視窗帶到最前面 | 0 |
| `observe()` | 列出前景視窗元素：`e12 button "Songs" (186,413)`；`include_image=true` 附標號截圖 | 0 |
| `locate(target)` | 用描述找元素（名稱比對 → Luna 選元素 → 格線） | 0–3 |
| `next_step(goal)` | Luna 選下一步；回傳 `decision`（act/wait/done/ask_user）與可直接呼叫的 `act` 參數 | 1 |
| `act(goal, action, element_id/obs 或 target 或 x/y, …)` | 一次完成：定位 → 安全判斷 → 執行 → 驗證 `expect` → 回傳新畫面元素；`plan_next=true` 同時給下一步 | 0–1（+定位） |

舊的低階工具（`judge_desktop_action`、`perform_desktop_action`、`check_screen`、`choose_desktop_action`、`capture_screen`）需設 `LUNA_LEGACY_TOOLS=1` 才會出現。

### `act` 的狀態碼

`done`、`expect_failed`、`needs_approval`（回傳 `approval_id`，使用者同意後用**同樣參數**加上它再呼叫）、`denied`（同一畫面上會一直拒絕，不要改寫重試）、`ambiguous`（附 `candidates`）、`not_found`、`stale`、`focus_lost`、`occluded`、`exec_disabled`。每個錯誤都有 `error.fix` 告訴 agent 下一步。

### 速度與安全的取捨

- **低風險步驟不判斷**（捲動、Esc/Tab/方向鍵、點一般連結／分頁／欄位、輸入不送出）→ 0 次 Luna。
- **高風險一律判斷**：名稱含 send/submit/delete/pay/buy/generate/install/送出/刪除/付款/購買/產生/安裝…、密碼欄、Enter 或送出、終端機視窗、網址列、格線或座標定位的未知目標。`LUNA_JUDGE=always` 可改成每步都判斷。
- 判斷用的動作描述由**伺服器依 UIA 名稱產生**，不採用 agent 的文字，避免換句話說繞過；被 `deny` 的動作在畫面未變前會直接拒絕。
- 執行前檢查前景視窗與滑鼠下的元素（UIA hit-test），被遮住或失焦就回 `occluded`／`focus_lost`，不會點錯視窗。
- 送給 Luna 的截圖只裁切目標視窗（不含 agent 自己的終端機），並以 JPEG 傳送。
- 畫面文字一律視為**不可信資料**：清除控制字元、截斷長度，並在回應中標記 `untrusted_text`。

實測（Chrome + Flow Music）：`observe` 0.56 秒、`next_step` 1.3 秒（1 次 Luna）、名稱可直接比對的點擊 0 次 Luna。

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
| `LUNA_ALLOW_EXECUTE` | 未設 | 設 `1` 才會真的動滑鼠鍵盤（未設時 `act` 回 `exec_disabled`，且不呼叫 Luna） |
| `LUNA_HTTP_ALLOW_EXECUTE` | 未設 | HTTP 模式下再額外設 `1` 才可執行 |
| `LUNA_JUDGE` | `risky` | `always` = 每步都判斷 |
| `LUNA_CONFIRM_POLICY` | `approve` | 舊工具用；`never` = `confirm` 一律不執行 |
| `LUNA_LEGACY_TOOLS` | 未設 | `1` = 也提供舊的低階工具 |
| `LUNA_LOCATE_MIN_CONFIDENCE` / `LUNA_LOCATE_MIN_MARGIN` | `0.6` / `0.25` | Luna 選元素時的最低機率與領先差距，不足則回 `ambiguous` |
| `LUNA_MCP_TOKEN` | — | HTTP 模式必填，≥ 24 字元隨機字串 |
| `LUNA_SCREENSHOT_MAX_WIDTH` | `1600` | 截圖縮放寬度 |
| `LUNA_IMAGE_FORMAT` / `LUNA_IMAGE_DETAIL` | `JPEG` / `high` | 送給 Luna 的圖片格式與細節 |
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
env = { LUNA_ALLOW_EXECUTE = "1" }     # 要讓 agent 實際操作時才開
```

### Claude Code — stdio

```powershell
claude mcp add luna-desktop -s user -e OPENAI_API_KEY=$env:OPENAI_API_KEY -e LUNA_ALLOW_EXECUTE=1 -- C:\path\to\luna-desktop-decision-mcp\.venv\Scripts\python.exe C:\path\to\luna-desktop-decision-mcp\server.py
```

### ChatGPT 桌面版 — Streamable HTTP

ChatGPT 自訂連接器（設定 → Apps & Connectors → Advanced → Developer mode）只接受**遠端 HTTPS** MCP URL：

```powershell
$env:LUNA_MCP_TOKEN = -join ((48..57 + 97..122) | Get-Random -Count 40 | % {[char]$_})
.venv\Scripts\python server.py --http --port 8765
cloudflared tunnel --url http://127.0.0.1:8765     # 或 ngrok http 8765
```

連接器 URL 填 `https://<通道網域>/mcp?token=<LUNA_MCP_TOKEN>`（也接受 `Authorization: Bearer`）。
⚠️ 這等於把「看你螢幕」的能力公開到網路上：token 要保密、用完就關通道；沒有必要不要開 `LUNA_HTTP_ALLOW_EXECUTE`。

## 參考範例

- [`examples/AGENTS.md`](examples/AGENTS.md)：可直接貼給 agent 的使用規則、操作迴圈與呼叫範例。
- [`examples/demo_client.py`](examples/demo_client.py)：用 Python MCP client 跑一次 `focus_window → observe → locate → next_step → act`。

一個典型的回合（給能力較弱的 agent）：

```text
next_step(goal="打開 Songs 清單")
→ decision "act", suggestions[0].call = act(obs="o4", action="click", element_id="e59")
act(obs="o4", goal=…, action="click", element_id="e59", expect="A list of songs is shown", plan_next=true)
→ status "done"（或 expect_failed + decision "wait"），elements = 新畫面，next = 下一步可呼叫的參數
```

## 注意事項

- 需要 Luna 時會把**目標視窗的截圖**送到 OpenAI 並計費（實測一次判斷約 1,700 input tokens，$0.10 / 1M）。畫面上有機密時不要使用。
- 判斷結果是機率性的建議，不能取代人工確認；高風險操作請讓使用者核准（`approval_id`）。
- 只支援主螢幕。需要中止時把滑鼠甩到螢幕角落（PyAutoGUI failsafe）。
- 以系統管理員身分執行的視窗（UAC 提升權限）無法被一般權限的行程操作；遠端桌面視窗內的元素 UIA 看不到，只能用格線。
- 原生下拉選單、右鍵選單是獨立視窗，目前不會列入 `observe`（網頁內的選單可以）。
