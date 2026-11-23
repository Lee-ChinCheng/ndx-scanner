#!/usr/bin/env python3
"""Nasdaq-100 bottom-signal scanner: fetch indicators -> rule engine -> LLM -> PDF report.

Usage:  .venv/bin/python main.py [--no-llm] [--no-breadth] [--no-email] [--date YYYY-MM-DD]
Output: output/nasdaq_<day>.pdf (Taiwan date), data/nasdaq_<day>.json, data/history.csv
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

import pandas as pd

from scanner import charts, fetchers, ftd, history, indicators as ind, llm, notifier, scoring
from scanner.config import (CHART_DIR, DATA_DIR, LOG_DIR, OUTPUT_DIR, TZ_TW, TZ_US, ensure_dirs, load_config)
from scanner.report import build_pdf

log = logging.getLogger("scanner")


def setup_logging() -> None:
    ensure_dirs()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = RotatingFileHandler(LOG_DIR / "scanner.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers = [sh, fh]
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)


def _r(v: Optional[float], nd: int = 2) -> Optional[float]:
    return None if v is None or v != v else round(float(v), nd)


def _fmt(v: Optional[float], nd: int = 2, suffix: str = "") -> str:
    return "—" if v is None or v != v else f"{v:,.{nd}f}{suffix}"


def _fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return f"{dt.astimezone(TZ_TW):%m/%d %H:%M} 台北 · {dt.astimezone(TZ_US):%m/%d %H:%M} 美東"


def make_row(rec: fetchers.Fetched, value: Optional[float] = None, prev: Optional[float] = None, nd: int = 2,
             unit: str = "", change_pct: bool = False, name: Optional[str] = None, group: Optional[str] = None,
             note: Optional[str] = None, status: Optional[str] = None) -> Dict[str, Any]:
    v = rec.last if value is None else value
    p = rec.prev if prev is None else prev
    if v is None or p is None:
        ch = "—"
    elif change_pct:
        ch = f"{(v / p - 1) * 100:+.2f}%" if p else "—"
    else:
        ch = f"{v - p:+.{nd}f}"
    lm = rec.last_modified
    return {
        "key": rec.key, "group": group or rec.group, "name": name or rec.name,
        "value": _r(v, 4), "prev": _r(p, 4),
        "value_str": _fmt(v, nd, unit), "prev_str": _fmt(p, nd, unit), "change_str": ch,
        "data_date": rec.last_date.isoformat() if rec.last_date else "—",
        "source_name": rec.source_name, "source_url": rec.source_url,
        "last_modified": lm.isoformat() if lm else None, "last_modified_str": _fmt_dt(lm) if lm else "—",
        "fetched_at": rec.fetched_at.isoformat() if rec.fetched_at else None, "fetched_at_str": _fmt_dt(rec.fetched_at),
        "status": status or rec.status, "note": note if note is not None else rec.note,
    }


def run(args) -> int:
    cfg = load_config()
    fcfg = cfg["fetch"]
    t0 = time.time()
    now_utc = datetime.now(timezone.utc)
    now_tw, now_us = now_utc.astimezone(TZ_TW), now_utc.astimezone(TZ_US)
    run_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else now_tw.date()
    fname = f"{cfg['report']['filename_prefix']}{run_date.day}"
    log.info("=== run start %s (TW) / %s (US) -> %s.pdf", now_tw.strftime("%F %T"), now_us.strftime("%F %T %Z"), fname)

    # ------------------------------------------------------------------ fetch
    R: Dict[str, fetchers.Fetched] = {}
    R["ndx"] = fetchers.price_history("ndx", "Nasdaq-100 指數", "價格/技術", "^NDX", nasdaq_symbol="NDX", nasdaq_assetclass="index")
    R["sox"] = fetchers.price_history("sox", "費城半導體 SOX", "價格/技術", "^SOX", nasdaq_symbol="SOX", nasdaq_assetclass="index")
    R["vxn"] = fetchers.cboe_history("vxn", "VXN", "VXN (Nasdaq-100 波動率指數)", "Nasdaq 恐慌")
    # VIX 衡量的是 S&P 500 的 30 天隱含波動率，並非直接對應 Nasdaq-100，
    # 但它是全市場恐慌的基準，可與 VXN 比較，判斷恐慌是集中在科技股還是全面擴散。
    R["vix"] = fetchers.cboe_history("vix", "VIX", "VIX (S&P 500 波動率指數)", "S&P 500 恐慌（參考）")
    R["vix3m"] = fetchers.cboe_history("vix3m", "VIX3M", "VIX3M (3 個月)", "Nasdaq 恐慌")
    R["vvix"] = fetchers.cboe_history("vvix", "VVIX", "VVIX (VIX 的波動率)", "尾端風險")
    R["skew"] = fetchers.cboe_history("skew", "SKEW", "SKEW 偏斜指數", "尾端風險")
    R["ovx"] = fetchers.cboe_history("ovx", "OVX", "OVX 原油波動率指數", "跨資產 · 能源")
    # VOLQ（Nasdaq-100 ATM 隱含波動率）已從報告移除：經 2026-11-08 實測，公開來源全部停更。
    #   Nasdaq Data API 最後成交時間停在 2023-10-05，每日回傳固定值 20.21；
    #   Nasdaq Global Index Watch 歷史匯出回傳空內容；Yahoo Finance 無此代碼（404）；
    #   TradingView 搜尋不到；Cboe 不發布此指數（403）；CME 網站拒絕程式存取。
    #   Nasdaq-100 的恐慌面改以 VXN 為主、VIX 為輔。
    R["pcr"] = fetchers.cboe_put_call(backfill_days=fcfg["pcr_backfill_days"])
    R["hy_oas"] = fetchers.fred_series("hy_oas", "BAMLH0A0HYM2", "高收益債 OAS (ICE BofA US HY)", "信用/利率")
    R["y10"] = fetchers.fred_with_treasury_fallback("y10", "DGS10", "nominal", "美國 10 年期公債殖利率", "信用/利率")
    R["real10"] = fetchers.fred_with_treasury_fallback("real10", "DFII10", "real", "美國 10 年期實質殖利率 (TIPS)", "信用/利率")
    R["bei10"] = fetchers.fred_series("bei10", "T10YIE", "10 年期損益平衡通膨率", "信用/利率")
    if not R["bei10"].ok and R["y10"].ok and R["real10"].ok:
        s = (R["y10"].series - R["real10"].series).dropna()
        R["bei10"].series, R["bei10"].status = s, "fallback"
        R["bei10"].source_name, R["bei10"].source_url = R["y10"].source_name, R["y10"].source_url
        R["bei10"].last_modified, R["bei10"].fetched_at = R["y10"].last_modified, R["y10"].fetched_at
        R["bei10"].note = "FRED 無法連線，改以財政部名目殖利率減實質殖利率推算"
    R["brent"] = fetchers.price_history("brent", "Brent 原油期貨 (近月)", "跨資產 · 能源", "BZ=F", prefer="yahoo",
                                        proxy={"symbol": "BNO", "assetclass": "etf", "label": "BNO (United States Brent Oil Fund)"})
    R["gold"] = fetchers.price_history("gold", "黃金期貨 COMEX (近月)", "跨資產 · 黃金", "GC=F", prefer="yahoo",
                                       proxy={"symbol": "GLD", "assetclass": "etf", "label": "GLD (SPDR Gold Shares)"})
    R["dxy"] = fetchers.price_history("dxy", "美元指數 DXY", "跨資產 · 美元", "DX-Y.NYB", prefer="yahoo",
                                      proxy={"symbol": "UUP", "assetclass": "etf", "label": "UUP (Invesco DB US Dollar Bullish)"})
    R["aaii"] = fetchers.aaii_sentiment_any()
    # Follow-Through Day 偵測用的日線：NDX 開高低收 + QQQ 成交量（NDX 指數沒有成交量），SPY 作雙指數確認。
    ftd_on = cfg.get("ftd", {}).get("enabled", True)
    if ftd_on:
        R["ndx_ohlc"] = fetchers.nasdaq_ohlcv("ndx_ohlc", "NDX", "Nasdaq-100 日線（開高低收）", "FTD 訊號", assetclass="index")
        R["qqq"] = fetchers.nasdaq_ohlcv("qqq", "QQQ", "QQQ 成交量（NDX 量能代理）", "FTD 訊號", assetclass="etf")
        R["spy"] = fetchers.nasdaq_ohlcv("spy", "SPY", "SPY (S&P 500 ETF，雙指數確認)", "FTD 訊號", assetclass="etf")
    R["fng"] = fetchers.cnn_fear_greed()

    # breadth
    breadth_df = pd.DataFrame()
    breadth_meta: Dict[str, Any] = {}
    if not args.no_breadth:
        try:
            cons = fetchers.nasdaq_constituents(cache_days=fcfg["constituents_cache_days"])
            closes = fetchers.constituent_closes(cons["symbols"], threads=fcfg["breadth_threads"], pause=fcfg["breadth_pause_sec"])
            breadth_df = ind.breadth_table(closes, days=160)
            breadth_meta = {"n_symbols": len(cons["symbols"]), "n_fetched": len(closes), "as_of": cons.get("as_of"),
                            "source_url": cons.get("source_url", "https://api.nasdaq.com/api/quote/list-type/nasdaq100"),
                            "from_cache": cons.get("from_cache"), "note": cons.get("note", "")}
        except Exception as e:  # noqa: BLE001
            log.exception("breadth failed: %s", e)
            breadth_meta = {"error": str(e)[:200]}
    log.info("fetch done in %.0fs", time.time() - t0)

    # ------------------------------------------------------------------ metrics
    m: Dict[str, Any] = {}
    ndx = R["ndx"].series if R["ndx"].ok else pd.Series(dtype=float)
    if len(ndx) > 200:
        rsi = ind.rsi(ndx, 14)
        s50, s200 = ind.sma(ndx, 50), ind.sma(ndx, 200)
        m.update(ndx_close=_r(ndx.iloc[-1]), ndx_chg1_pct=_r(ind.pct_change_n(ndx, 1)), ndx_chg5_pct=_r(ind.pct_change_n(ndx, 5)),
                 ndx_rsi14=_r(rsi.iloc[-1]), ndx_rsi14_prev=_r(rsi.iloc[-2]), ndx_sma50=_r(s50.iloc[-1]), ndx_sma200=_r(s200.iloc[-1]),
                 ndx_dist200_pct=_r((ndx.iloc[-1] / s200.iloc[-1] - 1) * 100), ndx_dist50_pct=_r((ndx.iloc[-1] / s50.iloc[-1] - 1) * 100),
                 ndx_low20=_r(ind.rolling_min_n(ndx, 20)), ndx_high52w=_r(ind.rolling_max_n(ndx, 252)),
                 ndx_dd_52w_pct=_r((ndx.iloc[-1] / ind.rolling_max_n(ndx, 252) - 1) * 100),
                 ndx_rebound_high=_r(ind.rolling_max_n(ndx, 5, exclude_last=True)),
                 ndx_high15_prior=_r(ind.rolling_max_n(ndx, 15, exclude_last=True)))
        m["ndx_break_rebound_high"] = bool(ndx.iloc[-1] > m["ndx_rebound_high"])
        # new 20-day closing low within the last 3 / 5 sessions?
        roll_min_prior = ndx.rolling(20).min().shift(1)
        is_new_low = ndx < roll_min_prior
        m["ndx_newlow_recent"] = bool(is_new_low.tail(3).any())
        m["ndx_newlow_5d"] = bool(is_new_low.tail(5).any())
        m["ndx_low20_date"] = ndx.tail(20).idxmin().date().isoformat()
    for k, key in (("vxn", "vxn"), ("vix", "vix"), ("vvix", "vvix"), ("skew", "skew"), ("ovx", "ovx")):
        rec = R[k]
        if rec.ok:
            s = rec.series
            m[key] = _r(s.iloc[-1]); m[f"{key}_chg1"] = _r(ind.diff_n(s, 1)); m[f"{key}_chg5"] = _r(ind.diff_n(s, 5))
            m[f"{key}_pctile_1y"] = _r(ind.percentile_rank(s, 252), 0)
    if R["vxn"].ok:
        s = R["vxn"].series
        m["vxn_10d_high"] = _r(ind.rolling_max_n(s, 10)); m["vxn_from_10dhigh_pct"] = _r((s.iloc[-1] / m["vxn_10d_high"] - 1) * 100)
        m["vxn_20d_high"] = _r(ind.rolling_max_n(s, 20))
    if R["vix"].ok and R["vix3m"].ok:
        j = pd.concat([R["vix"].series, R["vix3m"].series], axis=1, join="inner").dropna()
        ratio = j.iloc[:, 0] / j.iloc[:, 1]
        m["vix3m"] = _r(R["vix3m"].last); m["vix_ratio"] = _r(ratio.iloc[-1], 3); m["vix_ratio_prev"] = _r(ratio.iloc[-2], 3)
        m["vix_ratio_chg5"] = _r(ind.diff_n(ratio, 5), 3)
        R["vix_ratio_series"] = ratio
    if R["vxn"].ok and R["vix"].ok:
        j = pd.concat([R["vxn"].series, R["vix"].series], axis=1, join="inner").dropna()
        spread = j.iloc[:, 0] - j.iloc[:, 1]
        m["vxn_vix_spread"] = _r(spread.iloc[-1]); m["vxn_vix_spread_chg5"] = _r(ind.diff_n(spread, 5))
        m["vxn_vix_spread_pctile_1y"] = _r(ind.percentile_rank(spread, 252), 0)
    if R["pcr"].ok:
        s = R["pcr"].series
        m["pcr_equity"] = _r(s.iloc[-1]); m["pcr_equity_5d"] = _r(s.tail(5).mean()); m["pcr_equity_10d"] = _r(s.tail(10).mean())
        m["pcr_equity_pctile"] = _r(ind.percentile_rank(s, 252), 0); m["pcr_equity_window_days"] = int(len(s))
        if "total" in R["pcr"].extra:
            m["pcr_total"] = _r(R["pcr"].extra["total"].iloc[-1]); m["pcr_index"] = _r(R["pcr"].extra["index"].iloc[-1])
    if R["hy_oas"].ok:
        s = R["hy_oas"].series
        m.update(hy_oas=_r(s.iloc[-1]), hy_oas_chg1=_r(ind.diff_n(s, 1)), hy_oas_chg5=_r(ind.diff_n(s, 5)), hy_oas_chg20=_r(ind.diff_n(s, 20)),
                 hy_oas_pctile_1y=_r(ind.percentile_rank(s, 252), 0), hy_oas_20d_high=_r(ind.rolling_max_n(s, 20)))
    if R["y10"].ok:
        s = R["y10"].series; m.update(y10=_r(s.iloc[-1]), y10_chg1=_r(ind.diff_n(s, 1)), y10_chg5=_r(ind.diff_n(s, 5)))
    if R["real10"].ok:
        s = R["real10"].series; m.update(real10=_r(s.iloc[-1]), real10_chg1=_r(ind.diff_n(s, 1)), real10_chg5=_r(ind.diff_n(s, 5)), real10_chg20=_r(ind.diff_n(s, 20)))
    if R["bei10"].ok:
        s = R["bei10"].series; m.update(bei10=_r(s.iloc[-1]), bei10_chg5=_r(ind.diff_n(s, 5)))
    for k in ("brent", "gold", "dxy"):
        if R[k].ok:
            s = R[k].series
            m[k] = _r(s.iloc[-1]); m[f"{k}_chg1_pct"] = _r(ind.pct_change_n(s, 1)); m[f"{k}_chg5_pct"] = _r(ind.pct_change_n(s, 5))
            m[f"{k}_chg20_pct"] = _r(ind.pct_change_n(s, 20))
    if R["sox"].ok and len(ndx):
        j = pd.concat([R["sox"].series, ndx], axis=1, join="inner").dropna()
        ratio = j.iloc[:, 0] / j.iloc[:, 1]
        m["sox_close"] = _r(R["sox"].last); m["sox_chg1_pct"] = _r(ind.pct_change_n(R["sox"].series, 1))
        m["sox_ndx_ratio"] = _r(ratio.iloc[-1], 4); m["sox_ndx_ratio_chg10_pct"] = _r(ind.pct_change_n(ratio, 10)); m["sox_ndx_ratio_chg20_pct"] = _r(ind.pct_change_n(ratio, 20))
        R["sox_ndx_series"] = ratio
    if len(breadth_df):
        b = breadth_df
        m.update(breadth_pct20ema=_r(b["pct_above_20ema"].iloc[-1], 1), breadth_pct50=_r(b["pct_above_50sma"].iloc[-1], 1),
                 breadth_pct200=_r(b["pct_above_200sma"].iloc[-1], 1), breadth_pct50_prev=_r(b["pct_above_50sma"].iloc[-2], 1),
                 breadth_pct50_chg5=_r(ind.diff_n(b["pct_above_50sma"], 5), 1), breadth_pct20_chg3=_r(ind.diff_n(b["pct_above_20ema"], 3), 1),
                 breadth_pct200_chg5=_r(ind.diff_n(b["pct_above_200sma"], 5), 1),
                 advancers=int(b["advancers"].iloc[-1]), decliners=int(b["decliners"].iloc[-1]), new_52w_lows=int(b["new_52w_lows"].iloc[-1]),
                 new_52w_lows_prev5_max=int(b["new_52w_lows"].tail(6).iloc[:-1].max()), breadth_n=int(b["n"].iloc[-1]),
                 breadth_date=b.index[-1].date().isoformat())
    if R["aaii"].ok:
        m.update(aaii_bear=_r(R["aaii"].last, 1), aaii_bull=_r(R["aaii"].extra["bull"].iloc[-1], 1),
                 aaii_neutral=_r(R["aaii"].extra["neutral"].iloc[-1], 1), aaii_date=R["aaii"].last_date.isoformat())
        m["aaii_spread"] = _r(m["aaii_bull"] - m["aaii_bear"], 1)
        m["aaii_bear_pctile_1y"] = _r(ind.percentile_rank(R["aaii"].series, 52), 0)

    if R["fng"].ok:
        s = R["fng"].series
        comps = R["fng"].extra.get("components", {})
        m.update(fng_score=_r(s.iloc[-1], 1), fng_prev=_r(s.iloc[-2], 1) if len(s) > 1 else None, fng_chg5=_r(ind.diff_n(s, 5), 1),
                 fng_1w_ago=_r(R["fng"].extra.get("previous_1_week"), 1), fng_1m_ago=_r(R["fng"].extra.get("previous_1_month"), 1),
                 fng_rating=R["fng"].extra.get("rating"), fng_pctile_1y=_r(ind.percentile_rank(s, 250), 0),
                 fng_components={k: {"score": _r(v["score"], 1), "rating": v["rating"], "value": _r(v["value"], 3)} for k, v in comps.items()})
        if "junk_bond_demand" in comps:
            js = comps["junk_bond_demand"]["series"]
            m.update(junk_spread=_r(js.iloc[-1], 3), junk_spread_chg5=_r(ind.diff_n(js, 5), 3), junk_spread_chg20=_r(ind.diff_n(js, 20), 3))
            R["junk_series"] = js
        if "put_call_options" in comps:
            m["cnn_pcr_5d"] = _r(comps["put_call_options"]["value"], 3)

    sc = scoring.evaluate(m)
    session_date = R["ndx"].last_date.isoformat() if R["ndx"].ok else "—"

    # ------------------------------------------------------------------ Follow-Through Day（獨立於階段判定，不影響 10 項條件）
    ftd_res: Optional[Dict[str, Any]] = None
    if ftd_on:
        try:
            if R["ndx_ohlc"].ok and R["qqq"].ok:
                pdf_ = R["ndx_ohlc"].extra["frame"].copy()
                pdf_["volume"] = R["qqq"].extra["frame"]["volume"].reindex(pdf_.index)
                cdf_ = R["spy"].extra["frame"] if R["spy"].ok else None
                ftd_res = ftd.evaluate(pdf_, cdf_, sc["stage"], {k: v for k, v in cfg.get("ftd", {}).items() if k != "enabled"})
                q_ = ftd_res.get("quality") or {}
                m.update(ftd_state=ftd_res["state"], ftd_day=(ftd_res["primary"].get("rally") or {}).get("day"), ftd_score=q_.get("score"))
            else:
                ftd_res = {"state": "NO_DATA", "state_label": "無資料", "error": "NDX 日線或 QQQ 成交量抓取失敗，本次無法判定 FTD"}
                m["ftd_state"] = "NO_DATA"
        except Exception as e:  # noqa: BLE001
            log.exception("FTD failed: %s", e)
            ftd_res = {"state": "NO_DATA", "state_label": "無資料", "error": f"FTD 計算錯誤：{str(e)[:120]}"}

    # ------------------------------------------------------------------ indicator rows for the table
    rows: List[Dict[str, Any]] = []
    rows.append(make_row(R["ndx"], nd=0, change_pct=True, note=(f"RSI14 {_fmt(m.get('ndx_rsi14'),1)}；50DMA {_fmt(m.get('ndx_sma50'),0)}；200DMA {_fmt(m.get('ndx_sma200'),0)}（距 200DMA {_fmt(m.get('ndx_dist200_pct'),1,'%')}）；20 日最低收盤 {_fmt(m.get('ndx_low20'),0)}（{m.get('ndx_low20_date','—')}）" if R["ndx"].ok else None)))
    rows.append(make_row(R["sox"], nd=0, change_pct=True, note=(f"SOX/NDX 比值 {_fmt(m.get('sox_ndx_ratio'),4)}，10 日變化 {_fmt(m.get('sox_ndx_ratio_chg10_pct'),2,'%')}" if R["sox"].ok else None)))
    rows.append(make_row(R["vxn"], note=(f"一年百分位 {_fmt(m.get('vxn_pctile_1y'),0)}；10 日高 {_fmt(m.get('vxn_10d_high'))}，距高點 {_fmt(m.get('vxn_from_10dhigh_pct'),1,'%')}" if R["vxn"].ok else None)))
    rows.append(make_row(R["vix"], note=(f"S&P 500 隱含波動率，作為全市場恐慌參考；一年百分位 {_fmt(m.get('vix_pctile_1y'),0)}；"
                                         f"VXN−VIX 價差 {_fmt(m.get('vxn_vix_spread'))}（5 日 {_fmt(m.get('vxn_vix_spread_chg5'))}，一年百分位 {_fmt(m.get('vxn_vix_spread_pctile_1y'),0)}），"
                                         "價差擴大代表恐慌集中在科技股" if R["vix"].ok else None)))
    if "vix_ratio" in m:
        rr = R["vix3m"]
        rows.append(make_row(rr, value=m["vix_ratio"], prev=m["vix_ratio_prev"], nd=3, name="VIX / VIX3M 期限結構比",
                             note=f"VIX3M {_fmt(m.get('vix3m'))}；比值 >1 表示近月恐慌高於 3 個月（倒掛）"))
    else:
        rows.append(make_row(R["vix3m"], name="VIX / VIX3M 期限結構比"))
    rows.append(make_row(R["vvix"], note=(f"一年百分位 {_fmt(m.get('vvix_pctile_1y'),0)}" if R["vvix"].ok else None)))
    rows.append(make_row(R["skew"], note=(f"一年百分位 {_fmt(m.get('skew_pctile_1y'),0)}" if R["skew"].ok else None)))
    rows.append(make_row(R["pcr"], note=(f"5 日均 {_fmt(m.get('pcr_equity_5d'))}，10 日均 {_fmt(m.get('pcr_equity_10d'))}；百分位 {_fmt(m.get('pcr_equity_pctile'),0)}（樣本 {m.get('pcr_equity_window_days')} 日，逐日累積中）；Total {_fmt(m.get('pcr_total'))}、Index {_fmt(m.get('pcr_index'))}" if R["pcr"].ok else None)))
    # breadth pseudo-record
    brec = fetchers.Fetched("breadth", "Nasdaq-100 成分股站上 50DMA 比例", "股票廣度", "Nasdaq 成分表 + Nasdaq/Yahoo 歷史價",
                            breadth_meta.get("source_url", "https://api.nasdaq.com/api/quote/list-type/nasdaq100"), unit="%")
    if len(breadth_df):
        brec.series = breadth_df["pct_above_50sma"]; brec.status = "ok"; brec.fetched_at = now_utc
        brec.note = (f"站上 20EMA {_fmt(m.get('breadth_pct20ema'),1,'%')}、200DMA {_fmt(m.get('breadth_pct200'),1,'%')}；漲 {m.get('advancers')} / 跌 {m.get('decliners')} 家；52 週新低 {m.get('new_52w_lows')} 家；"
                     f"成分股 {breadth_meta.get('n_fetched')}/{breadth_meta.get('n_symbols')} 檔（名單日期 {breadth_meta.get('as_of')}）由本程式自行計算，非官方廣度數據。{breadth_meta.get('note','')}")
    else:
        brec.status = "failed" if not args.no_breadth else "skipped"; brec.note = breadth_meta.get("error", "本次略過廣度計算")
    rows.append(make_row(brec, nd=1, unit="%"))
    rows.append(make_row(R["hy_oas"], unit="%", note=(f"5 日 {_fmt(m.get('hy_oas_chg5'))} pp、20 日 {_fmt(m.get('hy_oas_chg20'))} pp；一年百分位 {_fmt(m.get('hy_oas_pctile_1y'),0)}；FRED 資料較交易日落後約 1 天" if R["hy_oas"].ok else None)))
    rows.append(make_row(R["y10"], unit="%"))
    rows.append(make_row(R["real10"], unit="%"))
    rows.append(make_row(R["bei10"], unit="%"))
    rows.append(make_row(R["brent"], change_pct=True, note=(f"5 日 {_fmt(m.get('brent_chg5_pct'),2,'%')}、20 日 {_fmt(m.get('brent_chg20_pct'),2,'%')}" if R["brent"].ok else None)))
    rows.append(make_row(R["ovx"], note=(f"5 日變化 {_fmt(m.get('ovx_chg5'))}；一年百分位 {_fmt(m.get('ovx_pctile_1y'),0)}" if R["ovx"].ok else None)))
    rows.append(make_row(R["gold"], change_pct=True, note=(f"5 日 {_fmt(m.get('gold_chg5_pct'),2,'%')}、20 日 {_fmt(m.get('gold_chg20_pct'),2,'%')}" if R["gold"].ok else None)))
    rows.append(make_row(R["dxy"], nd=3, change_pct=True, note=(f"5 日 {_fmt(m.get('dxy_chg5_pct'),2,'%')}、20 日 {_fmt(m.get('dxy_chg20_pct'),2,'%')}" if R["dxy"].ok else None)))
    fng_note = None
    if R["fng"].ok:
        c = m.get("fng_components", {})
        lab = {"market_momentum_sp500": "動能", "stock_price_strength": "強弱", "stock_price_breadth": "廣度", "put_call_options": "Put/Call",
               "market_volatility_vix": "VIX", "junk_bond_demand": "垃圾債", "safe_haven_demand": "避險"}
        fng_note = (f"評級 {m.get('fng_rating')}；1 週前 {_fmt(m.get('fng_1w_ago'),1)}、1 月前 {_fmt(m.get('fng_1m_ago'),1)}；一年百分位 {_fmt(m.get('fng_pctile_1y'),0)}。"
                    "分項分數(0 極恐懼~100 極貪婪)：" + "、".join(f"{lab.get(k,k)} {_fmt(v.get('score'),0)}" for k, v in c.items()))
    rows.append(make_row(R["fng"], nd=1, note=fng_note))
    if R["fng"].ok and "junk_series" in R:
        jrec = fetchers.Fetched("junk_spread", "垃圾債 vs 投資級債殖利率差 (CNN)", "信用/利率", "CNN Business (Fear & Greed 分項)", R["fng"].source_url, unit="pp")
        jrec.series = R["junk_series"]; jrec.status = "ok"; jrec.fetched_at = R["fng"].fetched_at; jrec.last_modified = R["fng"].last_modified
        jrec.note = f"5 日 {_fmt(m.get('junk_spread_chg5'),3)}、20 日 {_fmt(m.get('junk_spread_chg20'),3)}；HY OAS 缺值時作為信用利差代理"
        rows.append(make_row(jrec, nd=3, unit=" pp"))
    rows.append(make_row(R["aaii"], nd=1, unit="%", note=(f"看多 {_fmt(m.get('aaii_bull'),1,'%')}、中立 {_fmt(m.get('aaii_neutral'),1,'%')}、多空差 {_fmt(m.get('aaii_spread'),1,'pp')}；每週四更新，調查截止 {m.get('aaii_date')}" if R["aaii"].ok else None)))

    if ftd_on:
        qv = R["qqq"]
        if qv.ok:
            vol = (qv.extra["frame"]["volume"].dropna() / 1e6)
            a50 = float(vol.iloc[-51:-1].mean()) if len(vol) > 51 else None
            vrec = fetchers.Fetched("qqq_volume", qv.name, qv.group, qv.source_name, qv.source_url, unit=" 百萬股")
            vrec.series, vrec.status, vrec.fetched_at, vrec.last_modified = vol, "ok", qv.fetched_at, qv.last_modified
            vrec.note = f"FTD 的量能條件以 QQQ 成交量代理（NDX 指數無成交量）；前 50 日均量 {_fmt(a50,1)} 百萬股"
            rows.append(make_row(vrec, nd=1, change_pct=True))
        else:
            rows.append(make_row(qv))
        rows.append(make_row(R["spy"], nd=2, change_pct=True, note=("僅用於 FTD 雙指數確認；ETF 價格在除息日會有小幅缺口" if R["spy"].ok else None)))

    # ------------------------------------------------------------------ charts
    chart_paths: List[Any] = []
    try:
        for p in CHART_DIR.glob("*.png"):
            p.unlink()
        series = {"ndx": ndx if len(ndx) else None, "vxn": R["vxn"].series if R["vxn"].ok else None, "vix": R["vix"].series if R["vix"].ok else None,
                  "hy_oas": R["hy_oas"].series if R["hy_oas"].ok else None, "brent": R["brent"].series if R["brent"].ok else None,
                  "ovx": R["ovx"].series if R["ovx"].ok else None, "gold": R["gold"].series if R["gold"].ok else None,
                  "dxy": R["dxy"].series if R["dxy"].ok else None, "sox_ndx": R.get("sox_ndx_series"),
                  "real10": R["real10"].series if R["real10"].ok else None, "y10": R["y10"].series if R["y10"].ok else None,
                  "fng": R["fng"].series if R["fng"].ok else None, "junk": R.get("junk_series")}
        if len(ndx) > 200:
            series["ndx_sma50"], series["ndx_sma200"] = ind.sma(ndx, 50), ind.sma(ndx, 200)
        chart_paths = charts.render_all(series, breadth_df if len(breadth_df) else None, days=cfg["report"]["chart_days"])
    except Exception as e:  # noqa: BLE001
        log.exception("charts failed: %s", e)

    # ------------------------------------------------------------------ history (write before LLM so LLM sees today too)
    hist_row = {**{k: m.get(k) for k in history.HISTORY_FIELDS}, "date_tw": run_date.isoformat(), "session_date": session_date,
                "stage": sc["stage"], "n_pass": sc["n_pass"]}
    try:
        history.upsert(hist_row)
    except Exception as e:  # noqa: BLE001
        log.exception("history write failed: %s", e)
    hist_rows = history.recent(cfg["history_days_for_llm"])

    # ------------------------------------------------------------------ LLM
    llm_result: Dict[str, Any] = {"text": None, "model": cfg["llm"]["model"], "error": "已以 --no-llm 略過", "usage": {}}
    if not args.no_llm:
        snapshot = {
            "generated_at": {"taiwan": now_tw.strftime("%Y-%m-%d %H:%M %Z"), "us_eastern": now_us.strftime("%Y-%m-%d %H:%M %Z")},
            "latest_us_session_date": session_date,
            "metrics": m,
            "indicator_status": [{"name": r["name"], "status": r["status"], "data_date": r["data_date"], "note": r["note"]} for r in rows],
            "rule_engine": {k: sc[k] for k in ("stage", "stage_text", "stage_reasons", "stage_checks", "n_pass", "n_auto", "quadrant", "scenarios", "danger")}
            | {"conditions": [{"name": c["name"], "status": c["status"], "evidence": c["evidence"]} for c in sc["conditions"]]},
            "follow_through_day": ({k: v for k, v in ftd_res.items() if k != "params"} if ftd_res else None),
            "history_recent_days": hist_rows,
            "breadth_recent": ([{"date": d.date().isoformat(), "pct50": round(float(r.pct_above_50sma), 1), "pct20ema": round(float(r.pct_above_20ema), 1),
                                 "pct200": round(float(r.pct_above_200sma), 1), "new_lows": int(r.new_52w_lows)} for d, r in breadth_df.tail(10).iterrows()]
                               if len(breadth_df) else None),
        }
        (DATA_DIR / "last_llm_input.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        llm_result = llm.analyze(snapshot, cfg["llm"])
        log.info("LLM done: model=%s error=%s", llm_result.get("model"), llm_result.get("error"))

    # ------------------------------------------------------------------ JSON snapshot
    out_json = {
        "generated_tw": now_tw.isoformat(), "generated_us": now_us.isoformat(), "run_date_tw": run_date.isoformat(),
        "session_date": session_date, "filename": fname, "metrics": m, "scoring": sc, "ftd": ftd_res, "rows": rows,
        "breadth_meta": breadth_meta, "llm": llm_result, "duration_sec": round(time.time() - t0, 1),
    }
    tmp = DATA_DIR / f"{fname}.json.tmp"
    tmp.write_text(json.dumps(out_json, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, DATA_DIR / f"{fname}.json")

    # ------------------------------------------------------------------ PDF
    pdf_tmp = OUTPUT_DIR / f".{fname}.pdf.tmp"
    ctx = {"title": cfg["report"]["title"], "now_tw": now_tw, "now_us": now_us, "session_date": session_date, "filename": f"{fname}.pdf",
           "scoring": sc, "rows": rows, "charts": chart_paths, "chart_days": cfg["report"]["chart_days"], "llm": llm_result,
           "model_name": llm_result.get("model"), "history_rows": hist_rows, "ftd": ftd_res}
    build_pdf(pdf_tmp, ctx)
    os.replace(pdf_tmp, OUTPUT_DIR / f"{fname}.pdf")
    failed = [r["name"] for r in rows if r["status"] == "failed"]
    log.info("=== done in %.0fs -> %s ; stage=%s pass=%d/%d ; failed=%s", time.time() - t0, OUTPUT_DIR / f"{fname}.pdf",
             sc["stage"], sc["n_pass"], sc["n_auto"], failed or "none")

    # ------------------------------------------------------------------ email
    ncfg = cfg.get("notify", {})
    if ncfg.get("enabled") and not args.no_email:
        subject = f"{cfg['report']['title']} {run_date.isoformat()} — {sc['stage']}（{sc['n_pass']}/{sc['n_auto']}）"
        body = (f"{cfg['report']['title']}\n"
                f"產出時間：{now_tw:%Y-%m-%d %H:%M} 台灣 / {now_us:%Y-%m-%d %H:%M %Z} 美東\n"
                f"資料交易日：{session_date}\n"
                f"階段判定：{sc['stage']}；條件通過 {sc['n_pass']}/{sc['n_auto']}\n"
                f"抓取失敗：{', '.join(failed) if failed else '無'}\n\n"
                f"完整報告見附件 {fname}.pdf\n")
        notifier.send_report(OUTPUT_DIR / f"{fname}.pdf", subject, body, ncfg)  # failure is logged, not fatal
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-llm", action="store_true", help="skip the OpenRouter call")
    ap.add_argument("--no-breadth", action="store_true", help="skip 100-constituent breadth fetch (faster)")
    ap.add_argument("--no-email", action="store_true", help="do not email the PDF after it is built")
    ap.add_argument("--date", help="override Taiwan run date (YYYY-MM-DD) used for the file name")
    args = ap.parse_args()
    setup_logging()
    try:
        return run(args)
    except Exception as e:  # noqa: BLE001
        log.exception("fatal: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
