"""Data-source adapters. Every adapter returns a Fetched record with a pandas Series
indexed by date, plus provenance (URL, HTTP Last-Modified, fetch time, status).

Source priority
  prices (NDX/SOX/constituents) : Nasdaq Data API (api.nasdaq.com)  -> Yahoo chart API
  prices (Brent/Gold/DXY)       : Yahoo chart API -> ETF proxy via Nasdaq API (BNO/GLD/UUP)
  volatility indices            : Cboe CDN CSV
  FRED series                   : FRED API (needs FRED_API_KEY) -> fredgraph.csv -> U.S. Treasury CSV (yields only)
"""
import io
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from . import http
from .config import CACHE_DIR

log = logging.getLogger(__name__)

FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()


@dataclass
class Fetched:
    key: str
    name: str
    group: str
    source_name: str
    source_url: str
    unit: str = ""
    series: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    last_modified: Optional[datetime] = None
    fetched_at: Optional[datetime] = None
    status: str = "failed"  # ok | stale | fallback | failed
    note: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "stale", "fallback") and len(self.series) > 0

    @property
    def last_date(self) -> Optional[date]:
        if len(self.series) == 0:
            return None
        return self.series.index[-1].date()

    @property
    def last(self) -> Optional[float]:
        return float(self.series.iloc[-1]) if len(self.series) else None

    @property
    def prev(self) -> Optional[float]:
        return float(self.series.iloc[-2]) if len(self.series) > 1 else None


def _fail(rec: Fetched, exc: Exception) -> Fetched:
    rec.status = "failed"
    rec.note = f"{type(exc).__name__}: {str(exc)[:160]}"
    rec.fetched_at = datetime.now(timezone.utc)
    log.warning("fetch failed %s (%s): %s", rec.key, rec.source_url, rec.note)
    return rec


# ---------------------------------------------------------------- Cboe index CSVs
def cboe_history(key: str, symbol: str, name: str, group: str, timeout: int = 30) -> Fetched:
    url = f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{symbol}_History.csv"
    rec = Fetched(key, name, group, "Cboe", url)
    try:
        content, headers, fetched_at = http.get(url, timeout=timeout)
        df = pd.read_csv(io.BytesIO(content))
        df.columns = [c.strip().upper() for c in df.columns]
        col = "CLOSE" if "CLOSE" in df.columns else df.columns[-1]
        s = pd.Series(pd.to_numeric(df[col], errors="coerce").values,
                      index=pd.to_datetime(df[df.columns[0]]), name=key).dropna()
        rec.series = s.sort_index()
        rec.last_modified = http.parse_last_modified(headers)
        rec.fetched_at = fetched_at
        rec.status = "ok"
    except Exception as e:  # noqa: BLE001
        _fail(rec, e)
    return rec


# ---------------------------------------------------------------- Yahoo chart API
_YAHOO_DOWN = {"until": 0.0}


def yahoo_history(key: str, symbol: str, name: str, group: str, rng: str = "2y",
                  timeout: int = 30, source_name: str = "Yahoo Finance") -> Fetched:
    from urllib.parse import quote
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}?range={rng}&interval=1d"
    rec = Fetched(key, name, group, source_name, url)
    if time.time() < _YAHOO_DOWN["until"]:
        rec.status, rec.note = "failed", "Yahoo 本次執行稍早已回應 429（限流），略過"
        rec.fetched_at = datetime.now(timezone.utc)
        return rec
    try:
        content, headers, fetched_at = http.get(url, timeout=timeout)
        rec.series = _parse_yahoo(content, key)
        rec.fetched_at = fetched_at
        rec.last_modified = http.parse_last_modified(headers)
        meta = json.loads(content)["chart"]["result"][0].get("meta", {})
        rmt = meta.get("regularMarketTime")
        if rmt:
            rec.extra["regular_market_time"] = datetime.fromtimestamp(rmt, tz=timezone.utc)
        rec.status = "ok"
    except Exception as e:  # noqa: BLE001
        if "429" in str(e):
            _YAHOO_DOWN["until"] = time.time() + 600  # stop hammering Yahoo for the rest of this run
        _fail(rec, e)
    return rec


def _parse_yahoo(content: bytes, key: str) -> pd.Series:
    d = json.loads(content)["chart"]["result"][0]
    tz = ZoneInfo(d["meta"].get("exchangeTimezoneName", "America/New_York"))
    ts = d.get("timestamp", [])
    closes = d["indicators"]["quote"][0].get("close", [])
    idx, vals = [], []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        idx.append(pd.Timestamp(datetime.fromtimestamp(t, tz=tz).date()))
        vals.append(float(c))
    s = pd.Series(vals, index=pd.DatetimeIndex(idx), name=key)
    return s[~s.index.duplicated(keep="last")].sort_index()


# ---------------------------------------------------------------- Nasdaq Data API
_NASDAQ_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}


def nasdaq_history(key: str, symbol: str, name: str, group: str, assetclass: str = "index",
                   years: int = 2, timeout: int = 30) -> Fetched:
    frm = (date.today() - timedelta(days=365 * years)).isoformat()
    url = f"https://api.nasdaq.com/api/quote/{symbol}/historical?assetclass={assetclass}&fromdate={frm}&limit=9999"
    rec = Fetched(key, name, group, "Nasdaq (api.nasdaq.com)", url)
    try:
        content, headers, fetched_at = http.get(url, timeout=timeout, headers=_NASDAQ_HEADERS)
        d = json.loads(content)
        if not d.get("data") or not d["data"].get("tradesTable"):
            msg = (d.get("status") or {}).get("bCodeMessage") or d.get("message") or "no data"
            raise ValueError(str(msg)[:120])
        rows = d["data"]["tradesTable"]["rows"]
        idx, vals = [], []
        for r in rows:
            c = str(r.get("close", "")).replace(",", "").replace("$", "").strip()
            if not c or c in ("--", "N/A"):
                continue
            idx.append(pd.Timestamp(datetime.strptime(r["date"], "%m/%d/%Y").date()))
            vals.append(float(c))
        s = pd.Series(vals, index=pd.DatetimeIndex(idx), name=key)
        rec.series = s[~s.index.duplicated(keep="last")].sort_index()
        rec.fetched_at = fetched_at
        rec.last_modified = http.parse_last_modified(headers)
        rec.status = "ok"
    except Exception as e:  # noqa: BLE001
        _fail(rec, e)
    return rec


def nasdaq_ohlcv(key: str, symbol: str, name: str, group: str, assetclass: str = "index",
                 years: int = 2, timeout: int = 30) -> Fetched:
    """Daily open/high/low/close/volume from the Nasdaq Data API (FTD 偵測用).
    rec.series = close；完整日線在 rec.extra['frame']。指數（NDX、COMP）的 volume 為空。"""
    frm = (date.today() - timedelta(days=365 * years)).isoformat()
    url = f"https://api.nasdaq.com/api/quote/{symbol}/historical?assetclass={assetclass}&fromdate={frm}&limit=9999"
    rec = Fetched(key, name, group, "Nasdaq (api.nasdaq.com)", url)

    def num(x) -> float:
        t = str(x if x is not None else "").replace(",", "").replace("$", "").strip()
        return float(t) if t and t not in ("--", "N/A") else float("nan")

    try:
        content, headers, fetched_at = http.get(url, timeout=timeout, headers=_NASDAQ_HEADERS)
        d = json.loads(content)
        if not d.get("data") or not d["data"].get("tradesTable"):
            msg = (d.get("status") or {}).get("bCodeMessage") or d.get("message") or "no data"
            raise ValueError(str(msg)[:120])
        recs = [{"date": pd.Timestamp(datetime.strptime(r["date"], "%m/%d/%Y").date()),
                 **{k: num(r.get(k)) for k in ("open", "high", "low", "close", "volume")}}
                for r in d["data"]["tradesTable"]["rows"]]
        df = pd.DataFrame(recs).set_index("date")
        df = df[~df.index.duplicated(keep="last")].sort_index().dropna(subset=["close"])
        df["high"], df["low"] = df["high"].fillna(df["close"]), df["low"].fillna(df["close"])
        rec.series = df["close"].rename(key)
        rec.extra["frame"] = df
        rec.fetched_at = fetched_at
        rec.last_modified = http.parse_last_modified(headers)
        rec.status = "ok"
    except Exception as e:  # noqa: BLE001
        _fail(rec, e)
    return rec


def price_history(key: str, name: str, group: str, yahoo_symbol: str, nasdaq_symbol: Optional[str] = None,
                  nasdaq_assetclass: str = "index", prefer: str = "nasdaq", proxy: Optional[dict] = None) -> Fetched:
    """Primary/secondary price source with an optional ETF proxy as last resort.
    proxy = {'symbol': 'GLD', 'assetclass': 'etf', 'label': 'GLD (SPDR 黃金 ETF)'}"""
    order = ["nasdaq", "yahoo"] if prefer == "nasdaq" and nasdaq_symbol else ["yahoo", "nasdaq"]
    first = None
    for src in order:
        if src == "nasdaq":
            if not nasdaq_symbol:
                continue
            rec = nasdaq_history(key, nasdaq_symbol, name, group, assetclass=nasdaq_assetclass)
        else:
            rec = yahoo_history(key, yahoo_symbol, name, group)
        if rec.ok and len(rec.series) > 60:
            if first is not None:
                rec.note = f"主要來源失敗（{first.note[:70]}），改用 {rec.source_name}"
                rec.status = "fallback"
            return rec
        first = first or rec
    if proxy:
        p = nasdaq_history(key, proxy["symbol"], name, group, assetclass=proxy.get("assetclass", "etf"))
        if p.ok and len(p.series) > 60:
            p.status = "fallback"
            p.name = f"{name} ※代理：{proxy['label']}"
            p.note = (f"Yahoo 與 Nasdaq 原始標的皆失敗（{first.note[:60] if first else ''}），改用 ETF 代理 {proxy['symbol']} 的收盤價；"
                      f"水準值不等於原指標，僅供方向與漲跌幅參考")
            return p
    return first if first is not None else Fetched(key, name, group, "—", "")


# ---------------------------------------------------------------- FRED (API -> CSV -> Treasury)
_FRED_CSV_DOWN = {"flag": False}


def fred_series(key: str, series_id: str, name: str, group: str, unit: str = "%", timeout: int = 25) -> Fetched:
    csv_url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    rec = Fetched(key, name, group, "FRED (St. Louis Fed)", csv_url, unit=unit)
    # 1) official API when a key is configured (different host, much more reliable)
    if FRED_API_KEY:
        api_url = (f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}"
                   f"&observation_start={(date.today() - timedelta(days=800)).isoformat()}&file_type=json&api_key=***")
        try:
            content, headers, fetched_at = http.get(api_url.replace("api_key=***", f"api_key={FRED_API_KEY}"), timeout=timeout)
            obs = json.loads(content)["observations"]
            idx = [pd.Timestamp(o["date"]) for o in obs if o["value"] not in (".", "")]
            vals = [float(o["value"]) for o in obs if o["value"] not in (".", "")]
            rec.series = pd.Series(vals, index=pd.DatetimeIndex(idx), name=key).sort_index()
            rec.source_url = f"https://fred.stlouisfed.org/series/{series_id}"
            rec.source_name = "FRED API (St. Louis Fed)"
            rec.last_modified = http.parse_last_modified(headers)
            rec.fetched_at = fetched_at
            rec.status = "ok"
            return rec
        except Exception as e:  # noqa: BLE001
            log.warning("FRED API failed for %s: %s", series_id, e)
    # 2) public CSV endpoint (circuit breaker: after one timeout, skip for the rest of the run)
    if _FRED_CSV_DOWN["flag"]:
        rec.note = "FRED 公開 CSV 端點本次執行稍早已逾時，略過"
        rec.fetched_at = datetime.now(timezone.utc)
        return rec
    try:
        content, headers, fetched_at = http.get(csv_url, timeout=timeout)
        df = pd.read_csv(io.BytesIO(content))
        s = pd.Series(pd.to_numeric(df.iloc[:, 1], errors="coerce").values,
                      index=pd.to_datetime(df.iloc[:, 0]), name=key).dropna()
        rec.series = s.sort_index()
        rec.last_modified = http.parse_last_modified(headers)
        rec.fetched_at = fetched_at
        rec.status = "ok"
    except Exception as e:  # noqa: BLE001
        if "timed out" in str(e).lower() or "Timeout" in type(e).__name__ or "Max retries" in str(e):
            _FRED_CSV_DOWN["flag"] = True
        _fail(rec, e)
        if not FRED_API_KEY:
            rec.note += "｜建議在 .env 設定免費的 FRED_API_KEY 以改走官方 API"
    return rec


def treasury_yield(key: str, kind: str, name: str, group: str, timeout: int = 40) -> Fetched:
    """kind = 'nominal' -> daily_treasury_yield_curve ('10 Yr'); 'real' -> daily_treasury_real_yield_curve ('10 YR')."""
    typ = "daily_treasury_yield_curve" if kind == "nominal" else "daily_treasury_real_yield_curve"
    year = date.today().year
    base = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{y}/all"
            "?type={t}&field_tdr_date_value={y}&page&_format=csv")
    url = base.format(y=year, t=typ)
    rec = Fetched(key, name, group, "U.S. Treasury", url, unit="%")
    try:
        frames = []
        lm, fetched_at = None, None
        for y in (year - 1, year):
            content, headers, fetched_at = http.get(base.format(y=y, t=typ), timeout=timeout)
            frames.append(pd.read_csv(io.BytesIO(content)))
            lm = http.parse_last_modified(headers) or lm
        df = pd.concat([f for f in frames if len(f)], ignore_index=True)
        col = next(c for c in df.columns if c.strip().upper() in ("10 YR", "10YR"))
        s = pd.Series(pd.to_numeric(df[col], errors="coerce").values,
                      index=pd.to_datetime(df["Date"]), name=key).dropna()
        rec.series = s.sort_index()
        rec.last_modified = lm
        rec.fetched_at = fetched_at
        rec.status = "ok"
    except Exception as e:  # noqa: BLE001
        _fail(rec, e)
    return rec


def fred_with_treasury_fallback(key, series_id, kind, name, group):
    rec = fred_series(key, series_id, name, group)
    if rec.ok:
        return rec
    fb = treasury_yield(key, kind, name, group)
    if fb.ok:
        fb.status = "fallback"
        fb.note = f"FRED 無法連線（{rec.note[:60]}），改用美國財政部每日殖利率曲線"
    else:
        fb.note = f"FRED: {rec.note[:70]} | Treasury: {fb.note[:70]}"
    return fb


# ---------------------------------------------------------------- Nasdaq-100 constituents
def nasdaq_constituents(cache_days: int = 7, timeout: int = 30) -> dict:
    """Returns {'symbols': [...], 'as_of': str, 'source_url': ..., 'from_cache': bool}."""
    url = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
    cache = CACHE_DIR / "ndx_constituents.json"
    cached = None
    if cache.exists():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if (date.today() - date.fromisoformat(cached["fetched_date"])).days < cache_days:
                cached["from_cache"] = True
                return cached
        except Exception:  # noqa: BLE001
            cached = None
    try:
        content, headers, fetched_at = http.get(url, timeout=timeout, headers=_NASDAQ_HEADERS)
        d = json.loads(content)
        rows = d["data"]["data"]["rows"]
        syms = sorted({r["symbol"].strip() for r in rows if r.get("symbol")})
        if len(syms) < 90:
            raise ValueError(f"only {len(syms)} symbols returned")
        out = {"symbols": syms, "as_of": d["data"].get("date"), "source_url": url,
               "fetched_date": date.today().isoformat(), "from_cache": False}
        cache.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("constituents fetch failed: %s", e)
        if cached:
            cached["from_cache"] = True
            cached["note"] = f"使用快取成分股（{cached['fetched_date']}）: {e}"
            return cached
        raise


# ---------------------------------------------------------------- Cboe daily Put/Call ratios
def cboe_put_call(backfill_days: int = 60, timeout: int = 25) -> Fetched:
    """Daily Cboe market statistics JSON. Caches per-date values in data/cache/pcr_history.csv."""
    cache = CACHE_DIR / "pcr_history.csv"
    url_tpl = "https://cdn.cboe.com/data/us/options/market_statistics/daily/{d}_daily_options"
    rec = Fetched("pcr_equity", "Equity Put/Call Ratio", "選擇權情緒", "Cboe",
                  "https://www.cboe.com/us/options/market_statistics/daily/")
    hist = pd.DataFrame(columns=["date", "equity", "total", "index", "last_modified"])
    if cache.exists():
        try:
            hist = pd.read_csv(cache, parse_dates=["date"])
        except Exception:  # noqa: BLE001
            pass
    known = set(hist["date"].dt.date) if len(hist) else set()
    now_us = datetime.now(ZoneInfo("America/New_York"))
    today_us = now_us.date()
    cands: List[date] = []
    d = today_us
    while len(cands) < backfill_days:
        if d.weekday() < 5 and d not in known:
            cands.append(d)
        d -= timedelta(days=1)
    new_rows, lm, fetched_at = [], None, datetime.now(timezone.utc)
    requests_made = 0
    for d in sorted(cands, reverse=True):  # newest first so the latest session is never starved by the cap
        if requests_made >= 45:
            break
        try:
            content, headers, fetched_at = http.get(url_tpl.format(d=d.isoformat()), timeout=timeout)
            requests_made += 1
            ratios = {r["name"].upper(): float(r["value"]) for r in json.loads(content).get("ratios", [])}
            dlm = http.parse_last_modified(headers)
            new_rows.append({"date": pd.Timestamp(d), "equity": ratios.get("EQUITY PUT/CALL RATIO"),
                             "total": ratios.get("TOTAL PUT/CALL RATIO"), "index": ratios.get("INDEX PUT/CALL RATIO"),
                             "last_modified": dlm.isoformat() if dlm else None})
        except Exception:  # noqa: BLE001
            requests_made += 1
            # the most recent session may simply not be published yet: do not cache it as a holiday
            if d == today_us or (d == today_us - timedelta(days=1) and now_us.hour < 8):
                continue
            new_rows.append({"date": pd.Timestamp(d), "equity": None, "total": None, "index": None})
        time.sleep(0.2)
    if new_rows:
        parts = [x for x in (hist, pd.DataFrame(new_rows)) if len(x)]
        hist = pd.concat(parts, ignore_index=True).drop_duplicates("date", keep="last").sort_values("date")
        hist.to_csv(cache, index=False)
    valid = hist.dropna(subset=["equity"])
    if len(valid) == 0:
        rec.status = "failed"
        rec.note = "無法取得任何 Put/Call 資料"
        return rec
    rec.series = pd.Series(valid["equity"].astype(float).values, index=pd.DatetimeIndex(valid["date"]), name="pcr_equity")
    rec.extra["total"] = pd.Series(valid["total"].astype(float).values, index=pd.DatetimeIndex(valid["date"]))
    rec.extra["index"] = pd.Series(valid["index"].astype(float).values, index=pd.DatetimeIndex(valid["date"]))
    # Last-Modified of the file for the most recent session (stored per date in the cache)
    last_lm = valid["last_modified"].iloc[-1] if "last_modified" in valid.columns else None
    rec.last_modified = pd.Timestamp(last_lm).to_pydatetime() if isinstance(last_lm, str) and last_lm else None
    rec.fetched_at = fetched_at
    rec.source_url = url_tpl.format(d=rec.series.index[-1].date().isoformat())
    rec.status = "ok"
    return rec


# ---------------------------------------------------------------- AAII sentiment (weekly xls)
def aaii_sentiment(timeout: int = 60) -> Fetched:
    url = "https://www.aaii.com/files/surveys/sentiment.xls"
    rec = Fetched("aaii_bear", "AAII 散戶看空比例", "情緒調查", "AAII", url, unit="%")
    try:
        content, headers, fetched_at = http.get(url, timeout=timeout)
        if content[:2] != b"\xd0\xcf":
            rec.status = "failed"
            rec.fetched_at = fetched_at
            rec.note = "AAII 回傳的不是 Excel 檔（網站啟用防爬驗證），本次無法取得"
            return rec
        import xlrd  # lazy import
        wb = xlrd.open_workbook(file_contents=content)
        sh = wb.sheets()[0]
        header_row, cols = None, {}
        for i in range(min(sh.nrows, 12)):
            vals = [str(c.value).strip().lower() for c in sh.row(i)]
            if vals and vals[0] == "date" and "bearish" in vals:
                header_row = i
                cols = {"bull": vals.index("bullish"), "bear": vals.index("bearish")}
                if "neutral" in vals:
                    cols["neutral"] = vals.index("neutral")
                break
        if header_row is None:
            raise ValueError("無法辨識 AAII 試算表欄位")
        dates, bull, neu, bear = [], [], [], []
        for i in range(header_row + 1, sh.nrows):
            row = sh.row(i)
            if row[0].ctype != 3:
                continue
            try:
                d = xlrd.xldate_as_datetime(row[0].value, wb.datemode).date()
                b = float(row[cols["bull"]].value)
                be = float(row[cols["bear"]].value)
                n = float(row[cols["neutral"]].value) if "neutral" in cols and row[cols["neutral"]].ctype == 2 else float("nan")
            except Exception:  # noqa: BLE001
                continue
            scale = 100.0 if b <= 1.0 else 1.0
            dates.append(pd.Timestamp(d)); bull.append(b * scale); neu.append(n * scale); bear.append(be * scale)
        idx = pd.DatetimeIndex(dates)
        rec.series = pd.Series(bear, index=idx, name="aaii_bear").sort_index()
        rec.extra["bull"] = pd.Series(bull, index=idx).sort_index()
        rec.extra["neutral"] = pd.Series(neu, index=idx).sort_index()
        rec.last_modified = http.parse_last_modified(headers)
        rec.fetched_at = fetched_at
        rec.status = "ok"
    except Exception as e:  # noqa: BLE001
        _fail(rec, e)
    return rec


# ---------------------------------------------------------------- Breadth: all constituents
def constituent_closes(symbols: List[str], threads: int = 4, pause: float = 0.15,
                       timeout: int = 30) -> Dict[str, pd.Series]:
    """1y of daily closes per constituent. Nasdaq API first (handles bursts), Yahoo as per-ticker fallback."""
    def one(sym: str):
        time.sleep(pause)
        r = nasdaq_history(sym, sym, sym, "breadth", assetclass="stocks", years=1, timeout=timeout)
        if r.ok and len(r.series) > 150:
            return sym, r.series
        time.sleep(0.5)
        r = yahoo_history(sym, sym.replace(".", "-"), sym, "breadth", rng="1y", timeout=timeout)
        if r.ok and len(r.series) > 150:
            return sym, r.series
        return sym, None

    out: Dict[str, pd.Series] = {}
    with ThreadPoolExecutor(max_workers=threads) as ex:
        for sym, s in ex.map(one, symbols):
            if s is not None:
                out[sym] = s
    log.info("breadth: %d/%d constituents fetched", len(out), len(symbols))
    return out


# ---------------------------------------------------------------- AAII fallback: HTML results table
_MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def aaii_sentiment_html(timeout: int = 30) -> Fetched:
    """Parse the public results table on aaii.com/sentimentsurvey/sent_results (no year in dates -> inferred)."""
    import re
    url = "https://www.aaii.com/sentimentsurvey/sent_results"
    rec = Fetched("aaii_bear", "AAII 散戶看空比例", "情緒調查", "AAII (網頁表格)", url, unit="%")
    try:
        content, headers, fetched_at = http.get(url, timeout=timeout)
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", content.decode("utf-8", "replace")))
        if "Reported Date" not in txt:
            raise ValueError("頁面不含調查結果表格（可能是防爬頁面）")
        pat = re.compile(r"([A-Z][a-z]{2}) (\d{1,2}) (\d{1,2}\.\d)% (\d{1,2}\.\d)% (\d{1,2}\.\d)%")
        today = datetime.now(ZoneInfo("America/New_York")).date()
        dates, bull, neu, bear = [], [], [], []
        year = today.year
        prev_month = None
        for mon, day, b, n, be in pat.findall(txt):
            mi = _MONTHS.get(mon)
            if not mi:
                continue
            if prev_month is not None and mi > prev_month:  # table is newest-first; month increase => previous year
                year -= 1
            elif prev_month is None and mi > today.month:
                year -= 1
            prev_month = mi
            try:
                d = date(year, mi, int(day))
            except ValueError:
                continue
            dates.append(pd.Timestamp(d)); bull.append(float(b)); neu.append(float(n)); bear.append(float(be))
        if not dates:
            raise ValueError("表格內找不到百分比資料")
        idx = pd.DatetimeIndex(dates)
        rec.series = pd.Series(bear, index=idx, name="aaii_bear").sort_index()
        rec.extra["bull"] = pd.Series(bull, index=idx).sort_index()
        rec.extra["neutral"] = pd.Series(neu, index=idx).sort_index()
        rec.last_modified = http.parse_last_modified(headers)
        rec.fetched_at = fetched_at
        rec.status = "ok"
    except Exception as e:  # noqa: BLE001
        _fail(rec, e)
    return rec


def aaii_sentiment_any() -> Fetched:
    """Official xls first (long history), public HTML table second."""
    a = aaii_sentiment()
    if a.ok:
        return a
    b = aaii_sentiment_html()
    if b.ok:
        b.status = "fallback"
        b.note = f"官方 sentiment.xls 失敗（{a.note[:60]}），改用 AAII 網頁結果表格（約一年歷史）"
        return b
    a.note = f"xls: {a.note[:70]} | html: {b.note[:70]}"
    return a


# ---------------------------------------------------------------- CNN Fear & Greed (composite + 7 components)
def cnn_fear_greed(timeout: int = 30) -> Fetched:
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    rec = Fetched("fng", "CNN Fear & Greed 指數", "情緒調查", "CNN Business", url)
    try:
        content, headers, fetched_at = http.get(url, timeout=timeout, headers={
            "Accept": "application/json", "Referer": "https://www.cnn.com/markets/fear-and-greed"})
        d = json.loads(content)
        hist = d["fear_and_greed_historical"]["data"]
        # CNN stamps each daily point at 00:00 UTC of its own date (plus a live intraday point), so map by UTC date
        idx = [pd.Timestamp(datetime.fromtimestamp(p["x"] / 1000, tz=timezone.utc).date()) for p in hist]
        s = pd.Series([float(p["y"]) for p in hist], index=pd.DatetimeIndex(idx), name="fng")
        rec.series = s[~s.index.duplicated(keep="last")].sort_index()
        fg = d["fear_and_greed"]
        rec.extra["rating"] = fg.get("rating")
        rec.extra["previous_1_week"] = fg.get("previous_1_week")
        rec.extra["previous_1_month"] = fg.get("previous_1_month")
        comps = {}
        for k in ("market_momentum_sp500", "stock_price_strength", "stock_price_breadth", "put_call_options",
                  "market_volatility_vix", "junk_bond_demand", "safe_haven_demand"):
            c = d.get(k) or {}
            if c.get("data"):
                cs = pd.Series([float(p["y"]) for p in c["data"]],
                               index=pd.DatetimeIndex([pd.Timestamp(datetime.fromtimestamp(p["x"] / 1000, tz=timezone.utc).date()) for p in c["data"]]))
                cs = cs[~cs.index.duplicated(keep="last")].sort_index()
                comps[k] = {"score": c.get("score"), "rating": c.get("rating"), "value": float(cs.iloc[-1]), "series": cs}
        rec.extra["components"] = comps
        rec.last_modified = http.parse_last_modified(headers)
        rec.fetched_at = fetched_at
        rec.status = "ok"
    except Exception as e:  # noqa: BLE001
        _fail(rec, e)
    return rec
