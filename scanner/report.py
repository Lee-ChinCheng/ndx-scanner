"""PDF report built with reportlab. Latin text uses DejaVu Sans; CJK runs are wrapped in a
Droid Sans Fallback font because the installed Noto CJK fonts are CFF-based (unsupported)."""
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from .config import TZ_TW, TZ_US

log = logging.getLogger(__name__)

_FONT_LATIN = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_LATIN_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT_CJK = "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"
_registered = False
_CJK_RE = re.compile(r"([⺀-⿟　-ヿ㐀-䶿一-鿿豈-﫿＀-￯]+)")


def _register_fonts() -> None:
    global _registered
    if _registered:
        return
    pdfmetrics.registerFont(TTFont("Latin", _FONT_LATIN))
    pdfmetrics.registerFont(TTFont("Latin-Bold", _FONT_LATIN_B))
    pdfmetrics.registerFont(TTFont("CJK", _FONT_CJK))
    pdfmetrics.registerFontFamily("Latin", normal="Latin", bold="Latin-Bold", italic="Latin", boldItalic="Latin-Bold")
    pdfmetrics.registerFontFamily("CJK", normal="CJK", bold="CJK", italic="CJK", boldItalic="CJK")
    _registered = True


def T(text: Any, bold: bool = False) -> str:
    """Escape text and wrap CJK runs in the CJK font. Accepts simple **bold** markdown."""
    s = "" if text is None else str(text)
    s = escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = _CJK_RE.sub(r'<font name="CJK">\1</font>', s)
    return f"<b>{s}</b>" if bold else s


UP_COLOR, DOWN_COLOR = "#1e8449", "#c0392b"


def _change_cell(ch: Any) -> str:
    """「變化」欄著色：上升綠色、下降紅色，零或缺值維持預設色。"""
    txt = T(ch)
    try:
        v = float(str(ch).replace("%", "").replace("+", ""))
    except (TypeError, ValueError):
        return txt
    if v > 0:
        return f'<font color="{UP_COLOR}">{txt}</font>'
    if v < 0:
        return f'<font color="{DOWN_COLOR}">{txt}</font>'
    return txt


def _styles():
    base = dict(fontName="Latin", leading=13, alignment=TA_LEFT)
    return {
        "title": ParagraphStyle("title", fontSize=18, leading=24, textColor=colors.HexColor("#1b2a4a"), **{k: v for k, v in base.items() if k != "leading"}),
        "sub": ParagraphStyle("sub", fontSize=9, textColor=colors.HexColor("#555555"), **base),
        "h1": ParagraphStyle("h1", fontSize=13, leading=18, spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#1b2a4a"), fontName="Latin-Bold"),
        "h2": ParagraphStyle("h2", fontSize=10.5, leading=15, spaceBefore=7, spaceAfter=2, fontName="Latin-Bold", textColor=colors.HexColor("#2457a6")),
        "body": ParagraphStyle("body", fontSize=9, **base),
        "small": ParagraphStyle("small", fontSize=7.4, leading=9.6, fontName="Latin"),
        "cell": ParagraphStyle("cell", fontSize=7.2, leading=9.2, fontName="Latin"),
        "cellb": ParagraphStyle("cellb", fontSize=7.2, leading=9.2, fontName="Latin-Bold"),
        "bullet": ParagraphStyle("bullet", fontSize=9, leading=13, leftIndent=10, bulletIndent=2, fontName="Latin"),
        "box": ParagraphStyle("box", fontSize=9.5, leading=14, fontName="Latin"),
    }


def _fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    return f"{dt.astimezone(TZ_TW):%m/%d %H:%M} 台北 / {dt.astimezone(TZ_US):%m/%d %H:%M} 美東"


STATUS_LABEL = {"ok": "正常", "stale": "可疑/停更", "failed": "失敗", "fallback": "備援源"}
STATUS_COLOR = {"ok": "#2e8b57", "stale": "#d9822b", "failed": "#c0392b", "fallback": "#8a6d1f"}


def _md_to_flowables(md: str, st) -> List[Any]:
    out: List[Any] = []
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            out.append(Spacer(1, 3))
            continue
        if line.startswith("## "):
            out.append(Paragraph(T(line[3:].strip()), st["h2"]))
        elif line.startswith("# "):
            out.append(Paragraph(T(line[2:].strip()), st["h2"]))
        elif line.startswith("### "):
            out.append(Paragraph(T(line[4:].strip(), bold=True), st["body"]))
        elif re.match(r"^\s*[-*•]\s+", line):
            out.append(Paragraph(T(re.sub(r"^\s*[-*•]\s+", "", line)), st["bullet"], bulletText="•"))
        elif re.match(r"^\s*\d+[.)]\s+", line):
            m = re.match(r"^\s*(\d+)[.)]\s+(.*)", line)
            out.append(Paragraph(T(m.group(2)), st["bullet"], bulletText=f"{m.group(1)}."))
        else:
            out.append(Paragraph(T(line.strip("（）() ") if line.strip().startswith("（") and line.strip().endswith("）") and len(line) < 40 else line), st["body"]))
    return out


def build_pdf(path: Path, ctx: Dict[str, Any]) -> None:
    """ctx keys: title, now_tw, now_us, session_date, filename, scoring, rows (indicator rows),
    charts (paths), llm (dict), failures (list), data_notes (list), model_name, history_rows."""
    _register_fonts()
    st = _styles()
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm, title=ctx["title"], author="nasdaq-bottom-scanner")
    W = A4[0] - 28 * mm
    story: List[Any] = []
    sc = ctx["scoring"]

    # ---- header
    story.append(Paragraph(T(ctx["title"]), st["title"]))
    story.append(Paragraph(T(
        f"報告產生時間：台灣 {ctx['now_tw']:%Y-%m-%d %H:%M} (UTC+8)　｜　美東 {ctx['now_us']:%Y-%m-%d %H:%M} ({ctx['now_us']:%Z})"), st["sub"]))
    story.append(Paragraph(T(
        f"對應美股交易日（Nasdaq-100 最後收盤日）：{ctx['session_date']}　｜　檔名：{ctx['filename']}　｜　LLM：{ctx.get('model_name') or '未使用'}"), st["sub"]))
    story.append(Spacer(1, 6))

    # ---- overview box
    stage_color = {"A": "#c0392b", "B": "#d9822b", "C": "#2e8b57", "N": "#8a8f98"}[sc["stage"]]
    stage_name = {"A": "階段 A　恐慌超跌", "B": "階段 B　賣壓衰竭 / 築底", "C": "階段 C　反轉確認", "N": "未達恐慌門檻"}[sc["stage"]]
    danger = sc["danger"]
    danger_txt = ("已觸發：" if danger["triggered"] else f"未觸發（符合 {danger['n_hits']}/4 項）：") + (
        "、".join(danger["hits"]) if danger["hits"] else "無")
    quad = sc.get("quadrant")
    quad_txt = f"{quad['label']}　→ {quad['driver']}；{quad['read']}（{quad['detail']}）" if quad else "黃金或 DXY 資料缺失"
    ov = [
        [Paragraph(T("程式判定階段", bold=True), st["cell"]),
         Paragraph(f'<font color="{stage_color}">{T(stage_name, bold=True)}</font>　{T(sc["stage_text"])}', st["box"])],
        [Paragraph(T("10 條件勾選", bold=True), st["cell"]),
         Paragraph(T(f"{sc['n_pass']} / {sc['n_auto']} 項可判定條件成立"
                     + (f"；{sc.get('n_nodata', 0)} 項因資料缺失無法判定" if sc.get('n_nodata') else "")
                     + "；另 1 項基本面需人工判定"), st["box"])],
        [Paragraph(T("黃金 × 美元象限", bold=True), st["cell"]), Paragraph(T(quad_txt), st["box"])],
        [Paragraph(T("情境辨識", bold=True), st["cell"]), Paragraph(T("；".join(sc["scenarios"])), st["box"])],
        [Paragraph(T("危險組合", bold=True), st["cell"]),
         Paragraph(f'<font color="{"#c0392b" if danger["triggered"] else "#333333"}">{T(danger_txt)}</font>', st["box"])],
    ]
    t = Table(ov, colWidths=[30 * mm, W - 30 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f3f6fb")),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#c9d3e3")),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#dde4ef")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t)
    story.append(Spacer(1, 4))
    for r in sc["stage_reasons"]:
        story.append(Paragraph(T(r), st["small"], bulletText="·"))

    # ---- stage roadmap (concepts.docx: A -> B -> C)
    story.append(Paragraph(T("一、築底三階段路線圖（大局觀）"), st["h1"]))
    story.append(Paragraph(T("底部通常依序經歷三個階段。目標不是猜中最低的一天，而是辨認市場走到哪一段、下一段需要看到什麼證據。"
                             "「超跌反彈」與「真正落底」不同：恐慌後可能反彈 5～10% 再創新低（熊市反彈）。"), st["body"]))
    story.append(Spacer(1, 3))
    stage_defs = [
        ("A", "恐慌超跌", "VXN、VIX 升高；RSI 接近或跌破 30；Put/Call 升高；投資人情緒轉向悲觀",
         "可能出現技術性反彈，但仍可能繼續破底，不宜視為底部已成"),
        ("B", "賣壓衰竭、價格築底", "波動率自高點回落；Nasdaq-100 不再明顯創新低；市場廣度改善；信用利差不再快速擴張",
         "可開始評估分批建倉，但仍要防範再次測底"),
        ("C", "反轉確認", "突破近期反彈高點；重新站回重要均線；半導體相對走強；成交量與廣度同步支持",
         "趨勢確認度提高，但通常已離最低點有一段距離"),
    ]
    checks = sc.get("stage_checks", {})
    cur = sc["stage"]
    head = [Paragraph(T(h, bold=True), st["cell"]) for h in ("階段", "市場特徵", "意義", "程式進入條件與今日檢核", "今日位置")]
    rows, hl = [head], []
    for i, (code, name, feat, meaning) in enumerate(stage_defs, 1):
        ck = checks.get(code, {})
        lines = [T(f"（{ck.get('mode', '')}）")]
        for crit, ok, ev in ck.get("items", []):
            mark = '<font color="#2e8b57">✔</font>' if ok else '<font color="#c0392b">✘</font>'
            lines.append(f"{mark} {T(crit + '：' + ev)}")
        here = code == cur
        pos = '<font color="%s">%s</font>' % (stage_color, T("◀ 目前所在", bold=True)) if here else ""
        if not here and cur in ("A", "B") and code > cur and code == chr(ord(cur) + 1):
            pos = T("下一個目標")
        rows.append([Paragraph(T(f"階段 {code}", bold=True) + "<br/>" + T(name), st["cell"]), Paragraph(T(feat), st["cell"]),
                     Paragraph(T(meaning), st["cell"]), Paragraph("<br/>".join(lines), st["cell"]), Paragraph(pos, st["cell"])])
        if here:
            hl.append(i)
    t = Table(rows, colWidths=[22 * mm, 42 * mm, 34 * mm, W - 116 * mm, 18 * mm], repeatRows=1)
    style = _grid()
    for i in hl:
        style.add("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fff4e5"))
        style.add("BOX", (0, i), (-1, i), 1.0, colors.HexColor(stage_color))
    t.setStyle(style)
    story.append(t)
    story.append(Spacer(1, 2))
    flow = {"N": "目前未達恐慌門檻，市場不在 A→B→C 的築底流程內（偏中性或一般回檔）。",
            "A": "目前位於 A：已出現恐慌超跌，下一步要觀察 B 的三項條件是否同時成立。",
            "B": "目前位於 B：賣壓已有衰竭跡象，下一步要觀察 C 的四項條件是否同時成立。",
            "C": "目前位於 C：反轉已獲確認，後續重點轉為趨勢能否延續，以及是否回落到 B 之前的狀態。"}[cur]
    story.append(Paragraph(T(flow), st["small"]))
    story.append(Paragraph(T("判定順序為 C → B → A：條件較嚴格的階段優先；三者皆不成立時顯示「未達恐慌門檻」。"), st["small"]))
    story.append(Paragraph(T("B 與 C 之間另有一個較早的確認訊號 Follow-Through Day（放量確認日），獨立判定，見下一節。"), st["small"]))

    # ---- Follow-Through Day（獨立訊號，不影響階段判定與 10 項條件）
    story.extend(_ftd_section(ctx.get("ftd"), sc["stage"], st, W))

    # ---- 10 conditions
    story.append(Paragraph(T("三、科技股築底條件追蹤表（程式自動判定）"), st["h1"]))
    head = [Paragraph(T(h, bold=True), st["cell"]) for h in ("#", "條件", "分類", "狀態", "今日依據", "判定規則")]
    rows = [head]
    for i, c in enumerate(sc["conditions"], 1):
        lab = {"pass": "✔ 成立", "fail": "✘ 未成立", "manual": "人工", "nodata": "無資料"}[c["status"]]
        col = {"pass": "#2e8b57", "fail": "#c0392b", "manual": "#8a8f98", "nodata": "#d9822b"}[c["status"]]
        rows.append([Paragraph(str(i), st["cell"]), Paragraph(T(c["name"]), st["cell"]), Paragraph(T(c["group"]), st["cell"]),
                     Paragraph(f'<font color="{col}">{T(lab)}</font>', st["cellb"]),
                     Paragraph(T(c["evidence"]), st["cell"]), Paragraph(T(c["rule"]), st["cell"])])
    t = Table(rows, colWidths=[6 * mm, 40 * mm, 18 * mm, 15 * mm, 60 * mm, W - 139 * mm], repeatRows=1)
    t.setStyle(_grid())
    story.append(t)

    # ---- indicator table
    story.append(Paragraph(T("四、指標總表（數值、來源、資料更新時間）"), st["h1"]))
    story.append(Paragraph(T("「來源更新」為資料源回報的 HTTP Last-Modified；來源未提供時，改列資料源最後一筆觀測日。「抓取時間」為本程式實際下載時刻。"), st["small"]))
    head = [Paragraph(T(h, bold=True), st["cell"]) for h in ("分類", "指標", "數值", "前值", "變化", "來源", "來源更新 / 抓取時間", "狀態")]
    rows = [head]
    for r in ctx["rows"]:
        col = STATUS_COLOR.get(r["status"], "#333333")
        src = f'<link href="{escape(r["source_url"])}"><u>{T(r["source_name"])}</u></link>' if r.get("source_url") else T(r["source_name"])
        lm = r.get("last_modified_str")
        if not lm or lm == "—":
            lm = f"資料日 {r['data_date']}" if r.get("data_date") and r["data_date"] != "—" else "—"
        upd = (T(lm) + "<br/>" + T(r.get("fetched_at_str") or "—"))
        rows.append([Paragraph(T(r["group"]), st["cell"]), Paragraph(T(r["name"]), st["cell"]),
                     Paragraph(T(r["value_str"]), st["cellb"]), Paragraph(T(r["prev_str"]), st["cell"]),
                     Paragraph(_change_cell(r["change_str"]), st["cell"]),
                     Paragraph(src, st["cell"]), Paragraph(upd, st["small"]),
                     Paragraph(f'<font color="{col}">{T(STATUS_LABEL.get(r["status"], r["status"]))}</font>', st["cell"])])
    t = Table(rows, colWidths=[16 * mm, 40 * mm, 17 * mm, 15 * mm, 16 * mm, 22 * mm, W - 139 * mm, 13 * mm], repeatRows=1)
    t.setStyle(_grid())
    story.append(t)
    notes = [r for r in ctx["rows"] if r.get("note")]
    if notes:
        story.append(Spacer(1, 3))
        for r in notes:
            story.append(Paragraph(T(f"{r['name']}：{r['note']}"), st["small"], bulletText="·"))

    # ---- charts
    if ctx.get("charts"):
        story.append(Paragraph(T("五、趨勢圖（近 %d 個交易日）" % ctx.get("chart_days", 120)), st["h1"]))
        imgs = []
        for p in ctx["charts"]:
            img = Image(str(p))
            ratio = img.imageHeight / img.imageWidth
            img.drawWidth = (W - 6 * mm) / 2
            img.drawHeight = img.drawWidth * ratio
            imgs.append(img)
        grid = [imgs[i:i + 2] for i in range(0, len(imgs), 2)]
        for row in grid:
            while len(row) < 2:
                row.append("")
        t = Table(grid, colWidths=[(W) / 2, (W) / 2])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 1), ("RIGHTPADDING", (0, 0), (-1, -1), 1)]))
        story.append(t)

    # ---- LLM analysis
    story.append(PageBreak())
    llm = ctx.get("llm") or {}
    story.append(Paragraph(T(f"六、LLM 綜合判讀（模型：{llm.get('model') or ctx.get('model_name') or '—'}）"), st["h1"]))
    if llm.get("text"):
        story.extend(_md_to_flowables(llm["text"], st))
        u = llm.get("usage") or {}
        if u:
            story.append(Spacer(1, 4))
            story.append(Paragraph(T(f"tokens：輸入 {u.get('prompt_tokens','—')}、輸出 {u.get('completion_tokens','—')}"), st["small"]))
    else:
        story.append(Paragraph(T(f"本次 LLM 分析未能完成：{llm.get('error') or '未知原因'}。以上程式判定結果不受影響。"), st["body"]))

    # ---- history table
    hist = ctx.get("history_rows") or []
    if hist:
        story.append(Paragraph(T("七、近日歷史軌跡（每日一列，取自 data/history.csv）"), st["h1"]))
        cols = [("date_tw", "台灣日期"), ("session_date", "交易日"), ("ndx_close", "NDX"), ("ndx_rsi14", "RSI"), ("vxn", "VXN"),
                ("breadth_pct50", "%>50DMA"), ("pcr_equity", "PCR"), ("hy_oas", "HY OAS"), ("real10", "10Y實質"),
                ("dxy", "DXY"), ("gold", "黃金"), ("brent", "Brent"), ("fng_score", "F&G"), ("stage", "階段"), ("n_pass", "條件"), ("ftd_state", "FTD")]
        rows = [[Paragraph(T(h, bold=True), st["cell"]) for _, h in cols]]
        for h in hist[-15:]:
            rows.append([Paragraph(T(_fmt_hist(h.get(k))), st["cell"]) for k, _ in cols])
        t = Table(rows, repeatRows=1)
        t.setStyle(_grid())
        story.append(t)

    # ---- appendix: sources
    story.append(Paragraph(T("八、資料來源明細與失敗清單"), st["h1"]))
    head = [Paragraph(T(h, bold=True), st["cell"]) for h in ("指標", "完整網址", "狀態 / 備註")]
    rows = [head]
    for r in ctx["rows"]:
        rows.append([Paragraph(T(r["name"]), st["cell"]),
                     Paragraph(f'<link href="{escape(r["source_url"])}">{escape(r["source_url"])}</link>', st["small"]),
                     Paragraph(T(f"{STATUS_LABEL.get(r['status'], r['status'])}" + (f"：{r['note']}" if r.get("note") else "")), st["small"])])
    t = Table(rows, colWidths=[40 * mm, 95 * mm, W - 135 * mm], repeatRows=1)
    t.setStyle(_grid())
    story.append(t)
    story.append(Spacer(1, 6))
    story.append(Paragraph(T("免責聲明：本報告由程式自動抓取公開資料並以規則與大型語言模型產生，僅供個人研究與觀察條件整理之用，不構成任何投資建議；資料可能有延遲、缺漏或錯誤，請自行核實。"), st["small"]))

    doc.build(story)



def _ftd_section(fd: Optional[Dict[str, Any]], stage: str, st, W) -> list:
    """二、Follow-Through Day：O'Neil 的放量確認日，定位在階段 B 與 C 之間的早期確認訊號。"""
    out: list = [Paragraph(T("二、Follow-Through Day（FTD）反彈確認訊號"), st["h1"])]
    if not fd:
        return out + [Paragraph(T("本次未啟用 FTD 偵測（config.json 的 ftd.enabled 為 false）。"), st["body"])]
    out.append(Paragraph(T(
        "FTD 是 William O'Neil 用來確認「修正後的反彈是否有大資金進場」的訊號：指數自波段低點反彈的第 4～10 天，"
        "出現單日上漲 1.25% 以上且成交量高於前一日。它是獨立於上方三階段的另一套方法，通常比階段 C 更早出現，"
        "可視為 B 走向 C 途中的早期確認；它不改變階段判定，也不計入 10 項條件。FTD 只代表機率提高，確認後仍可能失效。"), st["body"]))
    out.append(Spacer(1, 3))
    if fd.get("error") or fd.get("state") == "NO_DATA":
        return out + [Paragraph(f'<font color="#c0392b">{T(fd.get("error") or "無資料")}</font>', st["body"])]
    pri, con, q = fd["primary"], fd.get("confirm") or {}, fd.get("quality")
    state = fd["state"]
    col = {"FTD_CONFIRMED": "#2e8b57", "FTD_WINDOW": "#2457a6", "RALLY_ATTEMPT": "#2457a6", "CORRECTION": "#d9822b",
           "FTD_INVALIDATED": "#c0392b", "RALLY_EXPIRED": "#c0392b"}.get(state, "#555555")
    kv = [("目前狀態", f'<font color="{col}">{T(fd["state_label"], bold=True)}</font>' + T(f"（Nasdaq-100，資料至 {pri.get('last_date')}）"))]
    cor = pri.get("correction")
    if cor:
        txt = (f"高點 {cor.get('high_date')} 收盤 {cor.get('high_close') or 0:,.0f} → 波段低點 {cor['swing_low_date']} 收盤 {cor['swing_low_close']:,.0f}"
               + (f"（{cor['decline_pct']:.1f}%）" if cor.get("decline_pct") is not None else "")
               + ("；低點為暫定，尚未出現反彈第 1 天" if cor.get("provisional") else ""))
        kv.append(("修正與波段低點", T(txt)))
    else:
        dd = pri.get("drawdown_from_high") or {}
        kv.append(("距 40 日高點", T(f"{dd.get('pct', 0):.1f}%（高點 {dd.get('high_date')} 收盤 {dd.get('high_close', 0):,.0f}）")))
    r = pri.get("rally")
    if r:
        kv.append(("反彈進度", T(f"第 {r['day']} 天（第 1 天為 {r['day1_date']}，當日低點 {r['day1_low']:,.0f}）；FTD 窗口還剩 {r['days_left_in_window']} 個交易日")))
    f = pri.get("ftd")
    if f:
        kv.append(("FTD 內容", T(f"{f['date']}（反彈第 {f['day']} 天）上漲 {f['gain_pct']:+.2f}%，QQQ 成交量 {f['volume'] / 1e6:,.1f} 百萬股"
                                 f"（前一日 {f['prev_volume'] / 1e6:,.1f}）；至今 {f['days_since']} 個交易日；"
                                 f"失效價位：收盤跌破 {f['invalidation_level']:,.0f}（目前距離 {f['cushion_pct']:+.1f}%）")))
    if q:
        parts = "<br/>".join(T(f"{x['pts']:+d}　{x['item']}") for x in q["parts"])
        kv.append(("品質分數", T(f"{q['score']} / 100（{q['grade']}，{'已定案' if q['final'] else '初步，FTD 後滿 5 個交易日才定案'}）", bold=True) + "<br/>" + parts))
    nm = pri.get("near_miss") or []
    if nm:
        kv.append(("差一點的日子", T("；".join(f"{x['date']} 第 {x['day']} 天漲 {x['gain_pct']:+.2f}%，但{x['reason']}" for x in nm))))
    pt = pri.get("power_trend")
    if pt:
        marks = "　".join(('<font color="#2e8b57">✔</font> ' if ok else '<font color="#c0392b">✘</font> ') + T(name) for name, ok in pt["items"])
        kv.append(("Power Trend", T(f"{pt['n_met']}/3　") + marks))
    if con.get("state") and con["state"] != "NO_DATA":
        cr = con.get("rally")
        kv.append(("S&P 500 (SPY)", T(con.get("state_label", "—") + (f"，第 {cr['day']} 天" if cr else "")
                                      + (f"；FTD 日 {con['ftd']['date']}" if con.get("ftd") else ""))))
    else:
        kv.append(("S&P 500 (SPY)", T("無資料，本次無法判斷雙指數確認")))
    if fd.get("next_step"):
        kv.append(("接下來看什麼", T(fd["next_step"])))
    kv.append(("與三階段對照", T(fd.get("cross_read", ""))))
    t = Table([[Paragraph(T(k, bold=True), st["cell"]), Paragraph(v, st["cell"])] for k, v in kv], colWidths=[30 * mm, W - 30 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f3f6fb")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#b7c3d9")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d5dce8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    out.append(t)

    if r and r.get("days"):
        out.append(Paragraph(T("本次反彈逐日紀錄"), st["h2"]))
        p = fd.get("params", {})
        lo_d, hi_d, mg = p.get("ftd_day_start", 4), p.get("ftd_day_end", 10), p.get("ftd_min_gain_pct", 1.25)
        rows = [[Paragraph(T(h, bold=True), st["cell"]) for h in ("天數", "日期", "NDX 收盤", "漲跌", "QQQ 量 vs 前一日", "判定")]]
        for d in r["days"]:
            v = d.get("vol_vs_prev_pct")
            if d["day"] < lo_d:
                verdict = "第 1 天（反彈起點）" if d["day"] == 1 else "未進入 FTD 窗口"
            elif d["chg_pct"] >= mg and v is not None and v > 0:
                verdict = "符合 FTD"
            elif d["chg_pct"] >= mg:
                verdict = "漲幅達標但量未增"
            else:
                verdict = f"漲幅未達 {mg}%"
            rows.append([Paragraph(str(d["day"]), st["cell"]), Paragraph(d["date"], st["cell"]), Paragraph(f"{d['close']:,.0f}", st["cell"]),
                         Paragraph(_change_cell(f"{d['chg_pct']:+.2f}%"), st["cell"]),
                         Paragraph(_change_cell(f"{v:+.1f}%") if v is not None else "—", st["cell"]), Paragraph(T(verdict), st["cell"])])
        t = Table(rows, colWidths=[14 * mm, 26 * mm, 26 * mm, 22 * mm, 36 * mm, W - 124 * mm], repeatRows=1)
        t.setStyle(_grid())
        out.append(t)

    past = pri.get("past_ftds") or []
    if past:
        out.append(Paragraph(T("近兩年 Nasdaq-100 的 FTD 紀錄（同一套規則回算）"), st["h2"]))
        rows = [[Paragraph(T(h, bold=True), st["cell"]) for h in ("FTD 日期", "反彈第幾天", "當日漲幅", "結果", "結果日期", "20 日後漲跌", "20 日內最大回落")]]
        for x in past:
            oc = {"失效": "#c0392b", "仍有效": "#2e8b57"}.get(x["outcome"], "#333333")
            rows.append([Paragraph(x["date"], st["cell"]), Paragraph(str(x["day"]), st["cell"]), Paragraph(f"{x['gain_pct']:+.2f}%", st["cell"]),
                         Paragraph(f'<font color="{oc}">{T(x["outcome"])}</font>' + (T(f"（{x['days_held']} 個交易日後）") if x.get("days_held") and x["outcome"] == "失效" else ""), st["cell"]),
                         Paragraph(x.get("outcome_date") or "—", st["cell"]),
                         Paragraph(_change_cell(f"{x['fwd20_pct']:+.2f}%") if x.get("fwd20_pct") is not None else T("未滿 20 日"), st["cell"]),
                         Paragraph(f"{x['max_dd_pct']:.2f}%", st["cell"])])
        t = Table(rows, colWidths=[24 * mm, 20 * mm, 20 * mm, W - 136 * mm, 24 * mm, 24 * mm, 24 * mm], repeatRows=1)
        t.setStyle(_grid())
        out.append(t)
        out.append(Paragraph(T("樣本只有兩年、筆數很少，僅供了解這套規則過去的表現，不能當成勝率依據。"), st["small"]))

    evs = pri.get("events") or []
    if evs:
        out.append(Paragraph(T("狀態機近期事件"), st["h2"]))
        for e in evs:
            out.append(Paragraph(T(f"{e['date']}　{e['text']}"), st["small"], bulletText="·"))
    out.append(Spacer(1, 2))
    out.append(Paragraph(T(
        "說明：價格用 Nasdaq-100 指數，成交量以 QQQ 代理（指數本身沒有成交量；O'Neil 原法用交易所總成交量，ETF 量在選擇權結算日可能失真）。"
        "修正門檻為自 40 個交易日內高點收盤回落 3% 以上且至少 3 個下跌日。品質分數滿分 100：出現時點、漲幅、是否高於 50 日均量、"
        "SPY 是否同步確認、FTD 後 5 日有無賣壓日。規則依 tradermonty/claude-trading-skills 的 ftd-detector 方法文件重新實作，"
        "不含原方法的建議持股水位。"), st["small"]))
    return out


def _fmt_hist(v) -> str:
    if v is None or (isinstance(v, float) and v != v):
        return "—"
    if isinstance(v, float):
        return f"{v:.2f}" if abs(v) < 1000 else f"{v:,.0f}"
    return str(v)


def _grid() -> TableStyle:
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef7")),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#8fa3c4")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d5dce8")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#b7c3d9")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafd")]),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ])
