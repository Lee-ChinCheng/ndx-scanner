"""Deterministic rule engine implementing the concepts.docx framework:
10-condition checklist, stage A/B/C, gold x dollar quadrant, four cross-asset scenarios,
and the 'danger combination' warning. Works on the flat metrics dict built in main.py."""
from typing import Any, Dict, List, Optional


def _f(m: Dict[str, Any], k: str) -> Optional[float]:
    v = m.get(k)
    return None if v is None or v != v else float(v)


def _fmt(v: Optional[float], nd: int = 2, suffix: str = "") -> str:
    return "—" if v is None else f"{v:.{nd}f}{suffix}"


def evaluate(m: Dict[str, Any]) -> Dict[str, Any]:
    conds: List[Dict[str, Any]] = []

    def add(name, group, ok: Optional[bool], evidence: str, rule: str):
        if ok is None:
            status = "manual" if rule == "人工判定" else "nodata"
        else:
            status = "pass" if ok else "fail"
        conds.append({"name": name, "group": group, "status": status,
                      "evidence": evidence, "rule": rule})

    # 1 VXN cooling
    vxn, vxn_hi10, vxn_c5 = _f(m, "vxn"), _f(m, "vxn_10d_high"), _f(m, "vxn_chg5")
    ok = None if vxn is None or vxn_hi10 is None else (vxn <= 0.85 * vxn_hi10 and (vxn_c5 is None or vxn_c5 < 0))
    add("VXN 恐慌逐步降溫", "股票波動率", ok,
        f"VXN {_fmt(vxn)}，10 日高 {_fmt(vxn_hi10)}（距高點 {_fmt(m.get('vxn_from_10dhigh_pct'),1,'%')}），5 日變化 {_fmt(vxn_c5)}",
        "VXN 低於 10 日高點 15% 以上，且 5 日變化為負")

    # 2 breadth improving
    p50, p50c5, p20c3 = _f(m, "breadth_pct50"), _f(m, "breadth_pct50_chg5"), _f(m, "breadth_pct20_chg3")
    ok = None if p50 is None or p50c5 is None else (p50c5 > 0 and (p20c3 is None or p20c3 > 0))
    add("Nasdaq 市場廣度持續改善", "股票廣度", ok,
        f"站上 50DMA {_fmt(p50,1,'%')}（5 日變化 {_fmt(p50c5,1,'pp')}），站上 20EMA 3 日變化 {_fmt(p20c3,1,'pp')}",
        "站上 50DMA 比例高於 5 日前，且站上 20EMA 比例高於 3 日前")

    # 3 price: no new low + breaking rebound high
    newlow = m.get("ndx_newlow_recent")
    brk = m.get("ndx_break_rebound_high")
    ok = None if newlow is None or brk is None else (not newlow and bool(brk))
    add("Nasdaq 價格不再創低，開始突破反彈高點", "價格確認", ok,
        f"收盤 {_fmt(_f(m,'ndx_close'),0)}；近 3 日{'有' if newlow else '無'}創 20 日新低；"
        f"{'已' if brk else '未'}突破前 5 日最高收盤 {_fmt(_f(m,'ndx_rebound_high'),0)}",
        "近 3 日未創 20 日收盤新低，且收盤高於前 5 個交易日的最高收盤")

    # 4 HY OAS converging
    oas, oas5 = _f(m, "hy_oas"), _f(m, "hy_oas_chg5")
    if oas is not None and oas5 is not None:
        add("HY OAS 信用利差收斂", "信用市場", oas5 <= 0,
            f"HY OAS {_fmt(oas)}%，5 日變化 {_fmt(oas5)} pp，20 日變化 {_fmt(_f(m,'hy_oas_chg20'))} pp",
            "5 日變化小於等於 0")
    else:
        js, js5 = _f(m, "junk_spread"), _f(m, "junk_spread_chg5")
        ok = None if js is None or js5 is None else (js5 <= 0)
        oas5 = js5  # used below by scenarios / danger combination as the credit-spread proxy
        add("HY OAS 信用利差收斂", "信用市場", ok,
            f"HY OAS 缺值，改用 CNN 垃圾債-投資級利差 {_fmt(js,3)} pp，5 日變化 {_fmt(js5,3)} pp（代理）",
            "5 日變化小於等於 0（代理：CNN junk bond demand）")

    # 5 real yield stops surging
    r5, r1 = _f(m, "real10_chg5"), _f(m, "real10_chg1")
    ok = None if r5 is None else (r5 <= 0.10 and (r1 is None or r1 <= 0.05))
    add("10 年期實質殖利率停止急升", "利率市場", ok,
        f"實質殖利率 {_fmt(_f(m,'real10'))}%，1 日 {_fmt(r1)} pp，5 日 {_fmt(r5)} pp",
        "5 日變化不超過 +0.10 pp，且 1 日變化不超過 +0.05 pp")

    # 6 DXY not surging
    d5 = _f(m, "dxy_chg5_pct")
    ok = None if d5 is None else (d5 <= 1.0)
    add("DXY 不再快速上漲", "匯率市場", ok,
        f"DXY {_fmt(_f(m,'dxy'))}，5 日 {_fmt(d5,2,'%')}", "5 日漲幅不超過 +1.0%")

    # 7 Brent & OVX stable together
    b5, o5 = _f(m, "brent_chg5_pct"), _f(m, "ovx_chg5")
    ok = None if b5 is None or o5 is None else (abs(b5) <= 5.0 and o5 <= 0)
    add("Brent 油價與 OVX 同步穩定", "能源市場", ok,
        f"Brent {_fmt(_f(m,'brent'))}（5 日 {_fmt(b5,2,'%')}），OVX {_fmt(_f(m,'ovx'))}（5 日 {_fmt(o5)}）",
        "Brent 5 日漲跌幅在 ±5% 內，且 OVX 5 日變化小於等於 0")

    # 8 gold not signalling rapidly expanding hedging pressure
    g5 = _f(m, "gold_chg5_pct")
    ok = None if g5 is None else (g5 <= 3.0)
    add("黃金走勢不再顯示避險壓力快速擴大", "黃金市場", ok,
        f"黃金 {_fmt(_f(m,'gold'))}，5 日 {_fmt(g5,2,'%')}", "5 日漲幅不超過 +3%")

    # 9 SOX relative strength
    rs10 = _f(m, "sox_ndx_ratio_chg10_pct")
    ok = None if rs10 is None else (rs10 > 0)
    add("SOX 半導體相對 Nasdaq 轉強", "科技股領漲", ok,
        f"SOX/NDX 比值 {_fmt(_f(m,'sox_ndx_ratio'),4)}，10 日變化 {_fmt(rs10,2,'%')}", "比值高於 10 日前")

    # 10 fundamentals: manual
    add("企業盈利預估未持續惡化", "基本面", None,
        "無免費即時資料源，由 LLM 定性提醒並請人工確認財報季盈利修正方向", "人工判定")

    n_pass = sum(1 for c in conds if c["status"] == "pass")
    n_auto = sum(1 for c in conds if c["status"] in ("pass", "fail"))
    n_nodata = sum(1 for c in conds if c["status"] == "nodata")

    # ---- stage
    rsi14, vxn_pct = _f(m, "ndx_rsi14"), _f(m, "vxn_pctile_1y")
    panic = (rsi14 is not None and rsi14 < 35) or (vxn_pct is not None and vxn_pct >= 80) or (vxn is not None and vxn >= 30)
    vxn_off = _f(m, "vxn_from_10dhigh_pct")
    nolow5 = m.get("ndx_newlow_5d") is False
    stage_b = (vxn_off is not None and vxn_off <= -15) and nolow5 and (conds[1]["status"] == "pass" or conds[3]["status"] == "pass")
    close, sma50 = _f(m, "ndx_close"), _f(m, "ndx_sma50")
    hi15 = _f(m, "ndx_high15_prior")
    stage_c = (close is not None and hi15 is not None and close > hi15) and (sma50 is not None and close > sma50) \
        and conds[8]["status"] == "pass" and conds[1]["status"] == "pass"
    if stage_c:
        stage, stage_text = "C", "反轉確認：突破近期反彈高點、站回 50 日均線，半導體相對轉強且廣度改善"
    elif stage_b:
        stage, stage_text = "B", "賣壓衰竭、價格築底：波動率自高點回落、未再創低，廣度或信用利差開始改善"
    elif panic:
        stage, stage_text = "A", "恐慌超跌：波動率偏高或 RSI 偏低，可能出現技術性反彈，但仍可能再破底"
    else:
        stage, stage_text = "N", "未達恐慌門檻：市場未處於文件定義的 A/B/C 築底階段（偏中性或正常回檔）"
    stage_reasons = [
        f"RSI14 {_fmt(rsi14,1)}；VXN {_fmt(vxn)}（一年百分位 {_fmt(vxn_pct,0)}）",
        f"VXN 距 10 日高點 {_fmt(vxn_off,1,'%')}；近 5 日{'未' if nolow5 else '有'}創 20 日新低",
        f"收盤 {_fmt(close,0)} vs 前 15 日最高收盤 {_fmt(hi15,0)}、50 日均線 {_fmt(sma50,0)}、200 日均線 {_fmt(_f(m,'ndx_sma200'),0)}",
    ]

    # ---- per-stage entry checks (same rules as above, exposed for the report's stage roadmap)
    breadth_ok, credit_ok = conds[1]["status"] == "pass", conds[3]["status"] == "pass"
    stage_checks = {
        "A": {"mode": "任一成立", "items": [
            ("RSI14 < 35", rsi14 is not None and rsi14 < 35, f"RSI14 {_fmt(rsi14,1)}"),
            ("VXN 一年百分位 ≥ 80", vxn_pct is not None and vxn_pct >= 80, f"百分位 {_fmt(vxn_pct,0)}"),
            ("VXN ≥ 30", vxn is not None and vxn >= 30, f"VXN {_fmt(vxn)}"),
        ]},
        "B": {"mode": "全部成立", "items": [
            ("VXN 自 10 日高點回落 15% 以上", vxn_off is not None and vxn_off <= -15, f"距高點 {_fmt(vxn_off,1,'%')}"),
            ("近 5 日未創 20 日收盤新低", nolow5, "近 5 日" + ("未創新低" if nolow5 else "有創新低")),
            ("廣度改善或信用利差收斂（條件 2 或 4）", breadth_ok or credit_ok,
             f"廣度{'✔' if breadth_ok else '✘'}、信用{'✔' if credit_ok else '✘'}"),
        ]},
        "C": {"mode": "全部成立", "items": [
            ("收盤突破前 15 日最高收盤", close is not None and hi15 is not None and close > hi15, f"{_fmt(close,0)} vs {_fmt(hi15,0)}"),
            ("收盤站上 50 日均線", close is not None and sma50 is not None and close > sma50, f"{_fmt(close,0)} vs {_fmt(sma50,0)}"),
            ("SOX 相對 NDX 轉強（條件 9）", conds[8]["status"] == "pass", conds[8]["evidence"][:40]),
            ("市場廣度改善（條件 2）", breadth_ok, conds[1]["evidence"][:40]),
        ]},
    }

    # ---- gold x dollar quadrant (5-day direction)
    gq = None if g5 is None or d5 is None else (g5 >= 0, d5 >= 0)
    quadrants = {
        (True, True): ("黃金↑ 美元↑", "地緣避險、全球資金尋求安全資產", "偏謹慎，需檢查信用壓力"),
        (False, True): ("黃金↓ 美元↑", "Fed 偏鷹、實質利率上升或美元流動性緊縮", "對高估值科技股較不利"),
        (True, False): ("黃金↑ 美元↓", "美元走弱、實質利率下降、黃金受益", "可能有利科技股，但需確認成因"),
        (False, False): ("黃金↓ 美元↓", "避險需求降溫、資金轉往風險資產", "若股票上漲，可視為 Risk-on 訊號"),
    }
    quadrant = None
    if gq is not None:
        q = quadrants[gq]
        quadrant = {"label": q[0], "driver": q[1], "read": q[2],
                    "detail": f"黃金 5 日 {_fmt(g5,2,'%')}，DXY 5 日 {_fmt(d5,2,'%')}"}

    # ---- scenarios
    vxn_c5v = vxn_c5 if vxn_c5 is not None else 0.0
    ndx5 = _f(m, "ndx_chg5_pct")
    y5 = _f(m, "y10_chg5")
    scenarios = []
    if b5 is not None and o5 is not None and b5 > 5 and o5 > 0 and vxn_c5v > 0:
        scenarios.append("能源供應衝擊再度升級（Brent↑ OVX↑ VXN↑）")
    if r5 is not None and d5 is not None and r5 > 0.10 and d5 > 0 and (g5 is None or g5 < 0):
        scenarios.append("Fed 緊縮與高實質利率（實質利率↑ 美元↑ 黃金↓）")
    if oas5 is not None and oas5 > 0.15 and (b5 is None or b5 < 0) and (y5 is None or y5 < 0):
        scenarios.append("經濟衰退或信用風險（利差擴大、油價與殖利率下滑）")
    if b5 is not None and o5 is not None and b5 < 0 and o5 < 0 and vxn_c5v < 0 and (ndx5 is None or ndx5 > 0):
        scenarios.append("戰爭降溫且風險偏好恢復（Brent↓ OVX↓ VXN↓ Nasdaq↑）")
    if not scenarios:
        scenarios.append("無明確單一情境主導（混合訊號）")

    # ---- danger combination
    danger_parts = {
        "美元急漲（5 日 > +1%）": d5 is not None and d5 > 1.0,
        "黃金急跌（5 日 < -2%）": g5 is not None and g5 < -2.0,
        "實質殖利率上升（5 日 > +0.10 pp）": r5 is not None and r5 > 0.10,
        "信用利差擴大（5 日 > 0）": oas5 is not None and oas5 > 0,
    }
    danger_hits = [k for k, v in danger_parts.items() if v]
    danger = {"triggered": len(danger_hits) == 4, "hits": danger_hits, "n_hits": len(danger_hits)}

    return {
        "conditions": conds, "n_pass": n_pass, "n_auto": n_auto, "n_nodata": n_nodata,
        "stage": stage, "stage_text": stage_text, "stage_reasons": stage_reasons, "stage_checks": stage_checks,
        "quadrant": quadrant, "scenarios": scenarios, "danger": danger,
    }
