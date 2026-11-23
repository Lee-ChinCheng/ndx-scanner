# Nasdaq-100 築底訊號掃描器

每天自動抓取 concepts.docx 中列出的恐慌、廣度、信用/利率與跨資產指標，套用規則引擎
（10 條件追蹤表、階段 A/B/C、黃金×美元象限、四情境、危險組合），再交給 OpenRouter 上的
LLM 撰寫判讀，最後輸出 PDF 到 `output/nasdaq_<日>.pdf`（台灣日期的「日」，每月同日覆蓋）。

## 執行

```bash
.venv/bin/python main.py                 # 完整執行（約 3～5 分鐘）
.venv/bin/python main.py --no-llm        # 不呼叫 LLM
.venv/bin/python main.py --no-breadth    # 略過 100 檔成分股廣度計算（快很多）
.venv/bin/python main.py --date 2026-11-08   # 指定檔名用的台灣日期
./run.sh                                 # crontab 用的包裝（flock 防重疊，log 到 logs/cron.log）
```

排程（台灣時間週一到週五 20:30，美股開盤前 1～2 小時，前一交易日所有資料已齊）：

```
30 20 * * 1-5 /home/kk15/webspider/run.sh
```

## 設定

- `.env`：`OPENROUTER_API_KEY`（必要）、`FRED_API_KEY`（建議，免費申請）。
- `config.json`：`llm.model` 預設 `anthropic/claude-sonnet-5.5`，失敗時改用 `llm.fallback_model`；
  `reasoning_effort` 設 `low` 以節省推理 token；`report.chart_days` 趨勢圖天數。
- `config.json` 的 `ftd` 區塊：Follow-Through Day 偵測參數（修正門檻、FTD 窗口天數、最低漲幅等）；`ftd.enabled` 設 `false` 可關閉。

## 輸出

| 路徑 | 內容 |
|---|---|
| `output/nasdaq_<日>.pdf` | 報告（總覽、三階段路線圖、Follow-Through Day、10 條件表、指標總表含來源與更新時間、趨勢圖、LLM 判讀、歷史軌跡、來源明細） |
| `data/nasdaq_<日>.json` | 同一份資料的機器可讀快照 |
| `data/history.csv` | 每日一列的指標歷史，供趨勢判讀與日後回測 |
| `data/last_llm_input.json` | 最近一次送給 LLM 的完整輸入 |
| `data/cache/` | Put/Call 逐日快取、成分股名單快取、圖檔 |
| `logs/` | `scanner.log`（程式）與 `cron.log`（排程輸出） |

## 資料來源與備援

| 指標 | 主要來源 | 備援 |
|---|---|---|
| NDX、SOX、成分股收盤 | Nasdaq Data API | Yahoo Finance chart API |
| VIX、VXN、VIX3M、VVIX、SKEW、OVX | Cboe CDN 歷史 CSV | — |

VOLQ 已移除：公開來源自 2023-10 起全部停更，Nasdaq-100 恐慌面以 VXN 為主、VIX 為輔（含 VXN−VIX 價差）。

| Equity/Total/Index Put/Call | Cboe 每日市場統計 JSON（逐日快取累積百分位樣本） | — |
| HY OAS | FRED API（需 key）→ fredgraph.csv | 無（缺值時條件標「無資料」） |
| 10Y 名目 / 實質殖利率 | FRED | 美國財政部每日殖利率曲線 CSV |
| 10Y 損益平衡通膨 | FRED | 名目 − 實質 推算 |
| Brent、黃金、DXY | Yahoo Finance | ETF 代理：BNO、GLD、UUP（報告會明確標示「代理」） |
| AAII 多空調查 | aaii.com 官方 sentiment.xls（1987 年起完整歷史） | aaii.com/sentimentsurvey/sent_results 網頁表格（約一年） |
| CNN Fear & Greed | production.dataviz.cnn.io JSON（綜合分數 + 7 分項，250 日歷史） | — |
| 垃圾債-投資級利差（HY OAS 代理） | CNN Fear & Greed 的 junk_bond_demand 分項 | HY OAS 缺值時自動用於條件 4 與危險組合 |
| Nasdaq-100 市場廣度 | 成分股名單 + 各檔歷史價自行計算（非官方廣度） | — |
| FTD 用日線：NDX 開高低收、QQQ 成交量、SPY | Nasdaq Data API | 無（缺值時 FTD 章節標「無資料」，不影響其他章節） |

## Follow-Through Day（FTD）

`scanner/ftd.py` 實作 William O'Neil 的反彈確認訊號，獨立於三階段判定與 10 項條件：

- 狀態機：無訊號 → 修正（自 40 日內高點回落 3% 以上且至少 3 個下跌日）→ 反彈嘗試（第 1–3 天）→ FTD 觀察窗（第 4–10 天）→ FTD 確認。
- FTD 條件：第 4–10 天、單日漲 1.25% 以上、成交量高於前一日。確認後收盤跌破 FTD 日低點即失效。
- 價格用 NDX 指數，成交量用 QQQ 代理（指數沒有成交量）；SPY 另跑一次，只用於雙指數確認加分。
- 每次執行從兩年日線重算，不保存狀態檔。報告另列出近兩年同一規則回算的 FTD 紀錄。
- 規則依 [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills) 的 ftd-detector 方法文件（MIT 授權）重新實作；
  刻意不輸出原方法的建議持股水位，也不引用其未附出處的失敗率統計。

## Email 通知

每次報告 PDF 產出後，`scanner/notifier.py` 會透過 SMTP over SSL（預設 `smtp.gmail.com:465`）把 PDF 當附件寄給 `.env` 的 `NOTIFY_RECIPIENTS`（逗號分隔多人；未設定時才改讀 `config.json` 的 `notify.recipients`）。

- 帳密與收件人都放在 `.env`：`EVAS_EMAIL`（寄件人／登入帳號）、`EVAS_PASSWORD`（Gmail／Workspace 須用應用程式密碼）、`NOTIFY_RECIPIENTS`（收件人）。
- `.env` 已被 `.gitignore` 排除；請參考 `.env.example` 複製一份填入實際值。
- 關閉：`config.json` 設 `notify.enabled: false`，或單次執行加 `--no-email`。
- 寄信失敗只記到 log（`logs/scanner.log`），不會讓整次掃描失敗；連線錯誤會重試 3 次。
- 補寄舊報告：`.venv/bin/python -m scanner.notifier output/nasdaq_10.pdf [--to a@x.com,b@y.com]`
