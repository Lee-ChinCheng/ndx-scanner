"""Pure computations on price series: RSI, moving averages, percentiles, breadth."""
from typing import Dict, Optional

import numpy as np
import pandas as pd


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(100.0).where(avg_loss != 0, 100.0)


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def pct_change_n(s: pd.Series, n: int) -> Optional[float]:
    if len(s) <= n:
        return None
    a, b = float(s.iloc[-1]), float(s.iloc[-1 - n])
    return (a / b - 1.0) * 100.0 if b else None


def diff_n(s: pd.Series, n: int) -> Optional[float]:
    if len(s) <= n:
        return None
    return float(s.iloc[-1] - s.iloc[-1 - n])


def value_n_ago(s: pd.Series, n: int) -> Optional[float]:
    if len(s) <= n:
        return None
    return float(s.iloc[-1 - n])


def percentile_rank(s: pd.Series, window: int = 252) -> Optional[float]:
    """Percentile (0-100) of the latest value within the trailing window."""
    w = s.dropna().tail(window)
    if len(w) < 20:
        return None
    last = float(w.iloc[-1])
    return float((w < last).mean() * 100.0)


def rolling_max_n(s: pd.Series, n: int, exclude_last: bool = False) -> Optional[float]:
    w = s.iloc[:-1] if exclude_last else s
    w = w.tail(n)
    return float(w.max()) if len(w) else None


def rolling_min_n(s: pd.Series, n: int, exclude_last: bool = False) -> Optional[float]:
    w = s.iloc[:-1] if exclude_last else s
    w = w.tail(n)
    return float(w.min()) if len(w) else None


def breadth_table(closes: Dict[str, pd.Series], days: int = 90) -> pd.DataFrame:
    """Daily breadth statistics across constituents for the last `days` sessions.
    Columns: pct_above_20ema, pct_above_50sma, pct_above_200sma, advancers, decliners, new_52w_lows, n."""
    if not closes:
        return pd.DataFrame()
    df = pd.DataFrame(closes).sort_index()
    df = df.ffill(limit=3)
    e20 = df.ewm(span=20, adjust=False, min_periods=20).mean()
    s50 = df.rolling(50, min_periods=50).mean()
    s200 = df.rolling(200, min_periods=200).mean()
    low252 = df.rolling(252, min_periods=120).min()
    chg = df.diff()
    out = pd.DataFrame(index=df.index)
    out["pct_above_20ema"] = (df > e20).sum(axis=1) / e20.notna().sum(axis=1).replace(0, np.nan) * 100
    out["pct_above_50sma"] = (df > s50).sum(axis=1) / s50.notna().sum(axis=1).replace(0, np.nan) * 100
    out["pct_above_200sma"] = (df > s200).sum(axis=1) / s200.notna().sum(axis=1).replace(0, np.nan) * 100
    out["advancers"] = (chg > 0).sum(axis=1)
    out["decliners"] = (chg < 0).sum(axis=1)
    out["new_52w_lows"] = (df <= low252).sum(axis=1)
    out["n"] = df.notna().sum(axis=1)
    return out.dropna(subset=["pct_above_50sma"]).tail(days)
