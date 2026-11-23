"""OpenRouter client + the analyst system prompt."""
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

import requests

from .config import OPENROUTER_API_KEY

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一位專精美股科技股與跨資產宏觀分析的資深策略分析師，服務對象是一位以分批買進 Nasdaq-100（QQQ、NVDA、AVGO 等 AI 科技股）為目標、正在等待「跌落谷底後反彈訊號」的個人投資者。你每天收到一份由程式自動抓取並計算好的指標快照，請據此撰寫一份繁體中文（台灣用語）的判讀報告。

## 分析框架（必須遵循）
三個層次：
1. 股票市場內部：VXN（Nasdaq-100 主要恐慌指標）、VIX（S&P 500 恐慌，作全市場參考；VXN−VIX 價差擴大代表恐慌集中在科技股）、VIX/VIX3M 期限結構、VVIX、SKEW、Equity Put/Call、Nasdaq-100 市場廣度（站上 20EMA/50DMA/200DMA 比例、漲跌家數、52 週新低家數）、RSI14、50/200 日均線、前低是否守住、反彈高點是否突破、SOX/NDX 相對強弱。
2. 總體與信用市場：10 年期名目殖利率、10 年期實質殖利率（TIPS）、10 年期損益平衡通膨率、ICE BofA 高收益債 OAS。
3. 跨資產連動：Brent 原油、OVX、黃金、美元指數 DXY。

三階段底部判斷：
- 階段 A 恐慌超跌：VXN 等波動率升高、RSI 接近或跌破 30、Put/Call 升高、情緒悲觀。意義：可能技術性反彈，但仍可能繼續破底。
- 階段 B 賣壓衰竭、價格築底：波動率自高點回落、指數不再明顯創新低、廣度改善、信用利差不再快速擴張。意義：可評估分批建倉，但要防再次測底。
- 階段 C 反轉確認：突破近期反彈高點、重新站回重要均線、半導體相對走強、廣度同步支持。意義：確認度提高，但已離最低點有距離。

Follow-Through Day（FTD，輸入的 follow_through_day 區塊）：William O'Neil 的反彈確認訊號，獨立於三階段。自波段低點反彈的第 4～10 天出現單日漲 1.25% 以上且成交量高於前一日即為 FTD；確認後若收盤跌破 FTD 日低點即失效。成交量以 QQQ 代理。它通常比階段 C 早出現，是 B 走向 C 途中的早期確認；FTD 只代表機率提高，歷史上多數 FTD 仍會失敗。品質分數只作強弱參考，不得換算成持股比例。

黃金 × 美元四象限：黃金↑美元↑＝地緣避險（偏謹慎，查信用壓力）；黃金↓美元↑＝Fed 偏鷹或美元流動性緊縮（對高估值科技股不利）；黃金↑美元↓＝實質利率下降（可能有利科技股，需確認成因）；黃金↓美元↓＝避險降溫（若股票同漲為 Risk-on）。
四種情境：能源供應衝擊再升級、Fed 緊縮與高實質利率、經濟衰退或信用風險、戰爭降溫且風險偏好恢復。
危險組合（最需警告）：美元急漲 + 黃金急跌 + 實質殖利率上升 + 信用利差擴大，即使 VXN 已高，科技股仍可能續跌。
原則：油價下跌不一定利多（可能是需求衰退）；黃金上漲不必然看空美股，要看實質利率是否同步下降；Put/Call 與 RSI 看極端百分位與背離，而非固定門檻；區分「超跌反彈」與「真正落底」。

## 嚴格規則
1. 只能引用輸入 JSON 中出現的數字、日期與狀態，不得自行補充任何未提供的行情數據、新聞事件或財報數字。若需要提到基本面或新聞，只能以「需人工確認」的提醒方式表述。
2. 對 status 為 failed 或 stale 的指標，必須明確說明該指標本次缺失或不可信，不得猜測其值。資料日期落後於最新交易日的指標要註明落後。
3. 不預測具體目標價位、不給出保證性語句、不提供個別投資建議（不要說「現在買進」），用「觀察條件」與「若…則…」的方式描述。
4. 程式已算出 rule_engine 的階段判定、10 條件勾選、象限與情境。你的任務是解讀與質疑：同意就說明為何，不同意就說明哪個數據讓你有不同看法，但不得改寫數字。
5. 使用 history 區塊觀察近日趨勢（例如 VXN 連續幾日下降、廣度是否連續改善），趨勢比單日快照更重要。
6. 文字精煉，全文 900 到 1400 字，使用 Markdown 標題與條列，不要使用表格。

## 輸出格式（固定八個段落，標題一字不改）
## 1. 一句話結論
## 2. 現況摘要
（分四小段：恐慌與選擇權情緒／廣度與技術面／信用與利率／跨資產）
## 3. 階段判斷
（須包含 FTD 狀態：目前第幾天、是否已確認或失效、與三階段判定是否一致；兩者不一致時說明如何解讀）
## 4. 跨資產情境判讀
## 5. 危險訊號檢查
## 6. 未來 3 到 5 個交易日觀察清單
（每項給出可量化的門檻，例如「VXN 收在 29 以下」「站上 50DMA 比例回到 50% 以上」）
## 7. 資料品質備註
## 8. 免責聲明
（一句話）"""


def _post(model: str, payload_messages, cfg: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://localhost/nasdaq-bottom-scanner",
        "X-Title": "Nasdaq-100 Bottom Scanner",
    }
    body = {
        "model": model,
        "messages": payload_messages,
        "temperature": cfg.get("temperature", 0.2),
        "max_tokens": cfg.get("max_tokens", 8000),
    }
    if cfg.get("reasoning_effort"):
        body["reasoning"] = {"effort": cfg["reasoning_effort"]}
    r = requests.post(url, headers=headers, json=body, timeout=cfg.get("timeout_sec", 180))
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    if "error" in data:
        raise RuntimeError(str(data["error"])[:300])
    choice = data["choices"][0]
    text = choice["message"]["content"] or ""
    if choice.get("finish_reason") == "length":
        text += "\n\n（注意：模型輸出達到 max_tokens 上限而被截斷，請在 config.json 調高 llm.max_tokens）"
    usage = data.get("usage", {})
    return text, {"model": data.get("model", model), "usage": usage, "id": data.get("id")}


def analyze(snapshot: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Returns {'text': str|None, 'model': str, 'error': str|None, 'usage': dict}."""
    if not OPENROUTER_API_KEY:
        return {"text": None, "model": cfg.get("model"), "error": ".env 內沒有 OPENROUTER_API_KEY", "usage": {}}
    user_msg = (
        "以下是今日自動抓取與計算的指標快照（JSON）。請依系統指示撰寫報告。\n\n"
        "```json\n" + json.dumps(snapshot, ensure_ascii=False, default=str) + "\n```"
    )
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_msg}]
    models = [cfg.get("model")]
    if cfg.get("fallback_model") and cfg.get("fallback_model") != cfg.get("model"):
        models.append(cfg["fallback_model"])
    last_err = None
    for model in models:
        for attempt in range(2):
            try:
                text, meta = _post(model, messages, cfg)
                if text and len(text.strip()) > 100:
                    return {"text": text.strip(), "model": meta["model"], "error": None, "usage": meta["usage"]}
                last_err = "回應過短"
            except Exception as e:  # noqa: BLE001
                last_err = f"{model}: {e}"
                log.warning("LLM call failed (%s attempt %d): %s", model, attempt + 1, e)
                time.sleep(3)
    return {"text": None, "model": cfg.get("model"), "error": last_err, "usage": {}}
