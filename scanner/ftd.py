"""Follow-Through Day (FTD) 偵測：William O'Neil 的底部確認訊號。

規則取自 tradermonty/claude-trading-skills 的 ftd-detector（MIT 授權）之方法文件
（references/ftd_methodology.md、post_ftd_guide.md），依本專案的資料源重新實作：
  - 主指數：Nasdaq-100 (NDX) 的開高低收 + QQQ 成交量（NDX 指數本身沒有成交量）。
  - 確認指數：SPY（價格與成交量），只用於「雙指數確認」加分。
  - 每次執行都從歷史日線由前往後重算狀態機，不保存狀態檔，漏跑一天也不會錯位。

狀態流程：
  NO_SIGNAL → CORRECTION → RALLY_ATTEMPT(第1–3天) → FTD_WINDOW(第4–10天) → FTD_CONFIRMED
                   ↑______反彈失敗（跌破波段低點／第2–3天跌破第1天低點）______|
  FTD_WINDOW 逾 10 天無 FTD → RALLY_EXPIRED；FTD_CONFIRMED 收盤跌破 FTD 日低點 → FTD_INVALIDATED

與原方法的差異（刻意的）：
  - 不輸出建議持股水位，也不引用原文件中未附出處的失敗率統計。
  - 「FTD 後 5 日無賣壓 +10 分」要等滿 5 個交易日才給分，之前分數標示為「初步」。
"""
from typing import Any, Dict, List, Optional

import pandas as pd

DEFAULTS: Dict[str, Any] = {
    "min_correction_pct": 3.0,   # 自近期高點收盤回落幅度門檻（%）
    "high_lookback_days": 40,    # 找近期高點的回看交易日數
    "min_down_days": 3,          # 高點到低點之間至少幾個下跌日
    "ftd_day_start": 4,
    "ftd_day_end": 10,
    "ftd_min_gain_pct": 1.25,
    "distribution_drop_pct": 0.2,  # FTD 後賣壓日：跌幅 ≥ 0.2% 且量增
    "post_ftd_watch_days": 5,
    "dual_window_days": 5,       # 兩指數 FTD 相距幾個交易日內算雙重確認
    "max_signal_age_days": 60,   # FTD 確認後超過此日數，訊號視為已過時
}

STATE_LABEL = {
    "NO_SIGNAL": "無訊號（未達修正門檻）",
    "CORRECTION": "修正中（等待反彈第 1 天）",
    "RALLY_ATTEMPT": "反彈嘗試（第 1–3 天）",
    "FTD_WINDOW": "FTD 觀察窗（第 4–10 天）",
    "FTD_CONFIRMED": "FTD 已確認",
    "RALLY_EXPIRED": "反彈逾期（10 天內未出現 FTD）",
    "FTD_INVALIDATED": "FTD 已失效",
    "NO_DATA": "無資料",
}
_WATCH = ("NO_SIGNAL", "RALLY_EXPIRED", "FTD_INVALIDATED")


def _d(ts) -> str:
    return pd.Timestamp(ts).date().isoformat()


def _num(x) -> Optional[float]:
    return None if x is None or x != x else float(x)


def analyze(df: pd.DataFrame, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """由前往後走一遍日線，回傳最後一天的狀態與過程中的事件。
    df：index 為日期（遞增），欄位 open/high/low/close/volume（volume 可為 NaN）。"""
    p = {**DEFAULTS, **(cfg or {})}
    out: Dict[str, Any] = {"state": "NO_DATA", "events": [], "past_ftds": []}
    if df is None or len(df) < 60:
        out["error"] = "日線不足 60 筆"
        return out
    df = df.sort_index()
    C, H, L, V, idx = df["close"].values, df["high"].values, df["low"].values, df["volume"].values, df.index
    n = len(df)
    state, anchor = "NO_SIGNAL", 0
    sl_i: Optional[int] = None      # 波段低點（收盤最低）的位置
    corr: Dict[str, Any] = {}
    day1_i: Optional[int] = None
    day = 0
    resets = 0
    ftd: Optional[Dict[str, Any]] = None
    near_miss: List[Dict[str, Any]] = []
    events: List[Dict[str, str]] = []
    past: List[Dict[str, Any]] = []

    def ev(i: int, text: str) -> None:
        events.append({"date": _d(idx[i]), "text": text})

    def vol_up(i: int) -> Optional[bool]:
        if V[i] != V[i] or V[i - 1] != V[i - 1] or V[i - 1] <= 0:
            return None
        return bool(V[i] > V[i - 1])

    def try_correction(i: int) -> bool:
        lo = max(i - p["high_lookback_days"], anchor, 0)
        if i - lo < 2:
            return False
        hi_i = lo + int(C[lo:i].argmax())
        decline = (C[i] / C[hi_i] - 1) * 100
        if decline > -p["min_correction_pct"]:
            return False
        down = sum(1 for j in range(hi_i + 1, i + 1) if C[j] < C[j - 1])
        if down < p["min_down_days"]:
            return False
        corr.clear()
        corr.update(high_i=hi_i, high_date=_d(idx[hi_i]), high_close=float(C[hi_i]))
        ev(i, f"進入修正：自 {_d(idx[hi_i])} 高點收盤 {C[hi_i]:,.0f} 回落 {abs(decline):.1f}%")
        return True

    def close_ftd(i: int, outcome: str) -> None:
        if ftd is None:
            return
        rec = past[-1]
        rec["outcome"] = outcome
        rec["outcome_date"] = _d(idx[i])
        rec["days_held"] = i - ftd["i"]

    for i in range(1, n):
        c, pc = C[i], C[i - 1]
        if state == "FTD_CONFIRMED" and ftd is not None:
            since = i - ftd["i"]
            if c < ftd["low"]:
                ev(i, f"FTD 失效：收盤 {c:,.0f} 跌破 FTD 日低點 {ftd['low']:,.0f}")
                close_ftd(i, "失效")
                state, anchor = "FTD_INVALIDATED", 0
            else:
                if since <= p["post_ftd_watch_days"] and (c / pc - 1) * 100 <= -p["distribution_drop_pct"] and vol_up(i):
                    ftd["dist_days"].append(since)
                    ev(i, f"FTD 後第 {since} 天出現賣壓日（跌 {(c / pc - 1) * 100:.2f}% 且量增）")
                if since > p["max_signal_age_days"]:
                    ev(i, f"FTD 確認已滿 {p['max_signal_age_days']} 個交易日，回到常態監控")
                    close_ftd(i, "維持有效至期滿")
                    state, anchor, ftd = "NO_SIGNAL", 0, None
                continue
        if state in _WATCH:
            undercut = state == "RALLY_EXPIRED" and sl_i is not None and c < C[sl_i]
            if try_correction(i) or undercut:
                if undercut and not corr:
                    corr.update(high_i=sl_i, high_date=_d(idx[sl_i]), high_close=float(C[sl_i]))
                state, sl_i, day1_i, day, near_miss, ftd = "CORRECTION", i, None, 0, [], None
            continue
        if state == "CORRECTION":
            if c < C[sl_i]:
                sl_i = i
                continue
            rng = H[i] - L[i]
            if c > pc or (rng > 0 and (c - L[i]) / rng >= 0.5):
                state, day1_i, day, near_miss = "RALLY", i, 1, []
                ev(i, f"反彈第 1 天（波段低點 {_d(idx[sl_i])} 收盤 {C[sl_i]:,.0f}）")
            continue
        if state == "RALLY":
            if c < C[sl_i]:
                ev(i, f"反彈失敗：第 {day + 1} 天收盤 {c:,.0f} 跌破波段低點 {C[sl_i]:,.0f}，重新計算")
                state, sl_i, day1_i, day, resets = "CORRECTION", i, None, 0, resets + 1
                continue
            day += 1
            if day <= 3 and c < L[day1_i]:
                ev(i, f"反彈失敗：第 {day} 天收盤 {c:,.0f} 跌破第 1 天低點 {L[day1_i]:,.0f}，等待新的第 1 天")
                state, day1_i, day, resets = "CORRECTION", None, 0, resets + 1
                continue
            if p["ftd_day_start"] <= day <= p["ftd_day_end"]:
                gain = (c / pc - 1) * 100
                vu = vol_up(i)
                if gain >= p["ftd_min_gain_pct"] and vu:
                    prior = pd.Series(V[max(0, i - 50):i]).dropna()
                    avg50 = float(prior.mean()) if len(prior) else None
                    ftd = {"i": i, "date": _d(idx[i]), "day": day, "gain_pct": round(gain, 2), "close": float(c), "low": float(L[i]),
                           "volume": _num(V[i]), "prev_volume": _num(V[i - 1]), "avg50_volume": avg50,
                           "volume_above_avg50": (bool(V[i] > avg50) if avg50 else None), "dist_days": [],
                           "swing_low_date": _d(idx[sl_i]), "swing_low_close": float(C[sl_i])}
                    past.append({"date": ftd["date"], "day": day, "gain_pct": ftd["gain_pct"], "close": ftd["close"], "low": ftd["low"],
                                 "i": i, "outcome": "仍有效", "outcome_date": None, "days_held": None})
                    ev(i, f"FTD 確認：第 {day} 天上漲 {gain:.2f}% 且量增")
                    state = "FTD_CONFIRMED"
                    continue
                if gain >= p["ftd_min_gain_pct"]:
                    near_miss.append({"date": _d(idx[i]), "day": day, "gain_pct": round(gain, 2),
                                      "reason": "成交量未高於前一日" if vu is False else "缺成交量資料"})
            if day >= p["ftd_day_end"]:
                ev(i, f"反彈逾期：第 {day} 天仍未出現 FTD")
                state, anchor = "RALLY_EXPIRED", sl_i

    last = n - 1
    disp = state
    if state == "RALLY":
        disp = "RALLY_ATTEMPT" if day < p["ftd_day_start"] else "FTD_WINDOW"
    out.update(state=disp, state_label=STATE_LABEL[disp], last_date=_d(idx[last]), last_close=float(C[last]),
               events=events[-8:], near_miss=near_miss)
    # forward returns for the track record (20 個交易日後)
    for r in past:
        j = r.pop("i")
        r["fwd20_pct"] = round((C[j + 20] / C[j] - 1) * 100, 2) if j + 20 <= last else None
        r["max_dd_pct"] = round((C[j:min(last, j + 20) + 1].min() / C[j] - 1) * 100, 2)
    out["past_ftds"] = past[-6:]

    lo = max(last - p["high_lookback_days"], 0)
    hi_i = lo + int(C[lo:last + 1].argmax())
    out["drawdown_from_high"] = {"high_date": _d(idx[hi_i]), "high_close": float(C[hi_i]), "pct": round((C[last] / C[hi_i] - 1) * 100, 2)}

    if state in ("CORRECTION", "RALLY") or (state in ("FTD_CONFIRMED", "RALLY_EXPIRED") and sl_i is not None):
        if state == "FTD_CONFIRMED" and ftd:
            sl_date, sl_close = ftd["swing_low_date"], ftd["swing_low_close"]
        else:
            sl_date, sl_close = _d(idx[sl_i]), float(C[sl_i])
        out["correction"] = {"high_date": corr.get("high_date"), "high_close": corr.get("high_close"),
                             "swing_low_date": sl_date, "swing_low_close": sl_close,
                             "decline_pct": (round((sl_close / corr["high_close"] - 1) * 100, 2) if corr.get("high_close") else None),
                             "provisional": state == "CORRECTION"}
    if state == "RALLY" and day1_i is not None:
        days = []
        for k, j in enumerate(range(day1_i, last + 1), 1):
            g = (C[j] / C[j - 1] - 1) * 100
            vchg = (V[j] / V[j - 1] - 1) * 100 if V[j] == V[j] and V[j - 1] == V[j - 1] and V[j - 1] > 0 else None
            days.append({"day": k, "date": _d(idx[j]), "close": float(C[j]), "chg_pct": round(g, 2),
                         "vol_vs_prev_pct": None if vchg is None else round(vchg, 1)})
        out["rally"] = {"day": day, "day1_date": _d(idx[day1_i]), "day1_low": float(L[day1_i]), "days": days,
                        "days_left_in_window": max(p["ftd_day_end"] - day, 0)}
    if ftd is not None and state in ("FTD_CONFIRMED", "FTD_INVALIDATED"):
        f = {k: v for k, v in ftd.items() if k != "i"}
        f["days_since"] = last - ftd["i"]
        f["invalidation_level"] = ftd["low"]
        f["cushion_pct"] = round((C[last] / ftd["low"] - 1) * 100, 2)
        out["ftd"] = f

    # Power Trend：21EMA > 50SMA、50SMA 近 5 日上升、收盤 > 21EMA
    s = df["close"]
    ema21, sma50 = s.ewm(span=21, adjust=False).mean(), s.rolling(50).mean()
    checks = [("21 日 EMA 高於 50 日均線", bool(ema21.iloc[-1] > sma50.iloc[-1])),
              ("50 日均線近 5 日上升", bool(sma50.iloc[-1] > sma50.iloc[-6])),
              ("收盤高於 21 日 EMA", bool(C[last] > ema21.iloc[-1]))]
    out["power_trend"] = {"items": checks, "n_met": sum(1 for _, ok in checks if ok),
                          "ema21": round(float(ema21.iloc[-1]), 2), "sma50": round(float(sma50.iloc[-1]), 2)}
    out["last_volume"], out["prev_volume"] = _num(V[last]), _num(V[last - 1])
    return out


def quality_score(primary: Dict[str, Any], confirm: Optional[Dict[str, Any]], trading_index: pd.DatetimeIndex,
                  cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """0–100 品質分數（僅在主指數 FTD 已確認時計算）。"""
    p = {**DEFAULTS, **(cfg or {})}
    f = primary.get("ftd")
    if not f or primary.get("state") != "FTD_CONFIRMED":
        return None
    parts: List[Dict[str, Any]] = []
    base = 60 if f["day"] <= 7 else 50
    parts.append({"item": f"FTD 出現在第 {f['day']} 天（{'第 4–7 天主要窗口' if base == 60 else '第 8–10 天後段窗口'}）", "pts": base})
    g = f["gain_pct"]
    gp = 15 if g >= 2.0 else 10 if g >= 1.5 else 5
    parts.append({"item": f"當日漲幅 {g:+.2f}%", "pts": gp})
    va = f.get("volume_above_avg50")
    parts.append({"item": "成交量" + ("高於" if va else "未高於") + " 50 日均量", "pts": 10 if va else 0})
    dual, dual_txt = False, "確認指數無資料"
    if confirm and confirm.get("state") not in (None, "NO_DATA"):
        cf = confirm.get("ftd")
        if cf and confirm.get("state") == "FTD_CONFIRMED":
            pos = trading_index.searchsorted([pd.Timestamp(f["date"]), pd.Timestamp(cf["date"])])
            gap = abs(int(pos[0]) - int(pos[1]))
            dual = gap <= p["dual_window_days"]
            dual_txt = f"SPY 於 {cf['date']} 也出現 FTD（相距 {gap} 個交易日）"
        else:
            dual_txt = f"SPY 目前為「{confirm.get('state_label')}」，尚未同步確認"
    parts.append({"item": "雙指數確認：" + dual_txt, "pts": 15 if dual else 0})
    watch = p["post_ftd_watch_days"]
    monitored = min(f["days_since"], watch)
    dd = f.get("dist_days") or []
    final = monitored >= watch
    if dd:
        e = min(dd)
        hp = -30 if e <= 2 else -15 if e == 3 else -5
        parts.append({"item": f"FTD 後第 {e} 天出現賣壓日" + (f"（共 {len(dd)} 個）" if len(dd) > 1 else ""), "pts": hp})
    elif final:
        parts.append({"item": f"FTD 後 {watch} 個交易日無賣壓日", "pts": 10})
    else:
        parts.append({"item": f"FTD 後已觀察 {monitored}/{watch} 天，尚無賣壓日（滿 {watch} 天才給分）", "pts": 0})
    score = max(0, min(100, sum(x["pts"] for x in parts)))
    grade = "強" if score >= 80 else "中等" if score >= 60 else "弱" if score >= 40 else "無效"
    return {"score": score, "grade": grade, "final": final, "dual": dual, "parts": parts}


def cross_read(stage: str, state: str) -> str:
    """FTD 狀態與三階段判定不一致時的解讀。"""
    if state == "FTD_CONFIRMED":
        if stage == "C":
            return "FTD 與階段 C 同時成立：放量確認日與趨勢條件一致，兩套方法互相印證。"
        return (f"FTD 已確認但階段仍為 {stage if stage != 'N' else '未達恐慌門檻'}：FTD 是較早的訊號，通常在站回 50 日線之前出現。"
                "可視為「反彈獲得放量確認」，但趨勢條件（階段 C）尚未到位，仍須留意 FTD 失效價位。")
    if state == "FTD_INVALIDATED":
        return "最近一次 FTD 已失效：該次反彈確認作廢，需等待新的波段低點與新的反彈嘗試。"
    if stage == "C":
        return "階段 C 成立但沒有有效的 FTD：價格與廣度已轉強，但這段反彈缺少「第 4–10 天放量大漲」的確認日，屬於量能未確認的反轉。"
    if state in ("RALLY_ATTEMPT", "FTD_WINDOW"):
        return f"反彈正在進行、尚未出現 FTD：與階段 {stage if stage != 'N' else 'N（未達恐慌門檻）'} 的判定不衝突，FTD 是否出現可作為走向階段 C 之前的早期確認。"
    if state == "CORRECTION":
        return "仍在修正、尚未出現反彈第 1 天：FTD 方法目前沒有任何底部證據。"
    if state == "RALLY_EXPIRED":
        return "這段反彈在 10 天內沒有出現 FTD：依 O'Neil 方法，缺乏放量確認的反彈可靠度較低。"
    return "指數未達修正門檻，FTD 方法目前不適用。"


def evaluate(primary_df: pd.DataFrame, confirm_df: Optional[pd.DataFrame], stage: str,
             cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    p = {**DEFAULTS, **(cfg or {})}
    pri = analyze(primary_df, p)
    con = analyze(confirm_df, p) if confirm_df is not None and len(confirm_df) else {"state": "NO_DATA", "state_label": STATE_LABEL["NO_DATA"]}
    q = quality_score(pri, con, primary_df.sort_index().index, p) if pri.get("state") != "NO_DATA" else None
    st = pri.get("state", "NO_DATA")
    nxt = ""
    if st == "CORRECTION":
        nxt = "等待反彈第 1 天：收盤高於前一日，或收在當日高低區間的上半部。"
    elif st in ("RALLY_ATTEMPT", "FTD_WINDOW"):
        r = pri["rally"]
        nd = r["day"] + 1
        sl = pri["correction"]["swing_low_close"]
        if nd < p["ftd_day_start"]:
            nxt = (f"下一個交易日是第 {nd} 天，還不在 FTD 窗口（第 {p['ftd_day_start']}–{p['ftd_day_end']} 天）。"
                   f"第 2–3 天收盤不可跌破第 1 天低點 {r['day1_low']:,.0f}；任何一天收盤跌破波段低點 {sl:,.0f} 即反彈失敗。")
        else:
            lv = pri.get("last_volume")
            nxt = (f"下一個交易日是第 {nd} 天，在 FTD 窗口內：需上漲 {p['ftd_min_gain_pct']}% 以上"
                   f"（NDX 收在 {pri['last_close'] * (1 + p['ftd_min_gain_pct'] / 100):,.0f} 以上）"
                   + (f"，且 QQQ 成交量高於 {lv / 1e6:,.1f} 百萬股" if lv else "，且成交量高於前一日")
                   + f"。收盤跌破波段低點 {sl:,.0f} 即反彈失敗。")
    elif st == "FTD_CONFIRMED":
        f = pri["ftd"]
        nxt = f"FTD 有效中。失效條件：收盤跌破 FTD 日低點 {f['invalidation_level']:,.0f}（目前高出 {f['cushion_pct']:.1f}%）。"
    elif st == "RALLY_EXPIRED":
        nxt = "等待新的修正與波段低點，或指數創 40 日新高後回到常態。"
    elif st == "FTD_INVALIDATED":
        nxt = "等待新的波段低點與反彈第 1 天。"
    return {"params": p, "primary": pri, "confirm": con, "quality": q, "next_step": nxt, "cross_read": cross_read(stage, st),
            "state": st, "state_label": pri.get("state_label", STATE_LABEL["NO_DATA"])}
