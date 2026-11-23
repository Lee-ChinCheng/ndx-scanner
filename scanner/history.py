"""Append-only daily history (one row per Taiwan run date) used for trend context."""
import logging
from typing import Any, Dict, List

import pandas as pd

from .config import HISTORY_CSV

log = logging.getLogger(__name__)

HISTORY_FIELDS: List[str] = [
    "date_tw", "session_date", "ndx_close", "ndx_rsi14", "ndx_sma50", "ndx_sma200", "vxn", "vix", "vix_ratio",
    "vvix", "skew", "pcr_equity", "pcr_equity_5d", "breadth_pct20ema", "breadth_pct50", "breadth_pct200",
    "advancers", "decliners", "new_52w_lows", "hy_oas", "y10", "real10", "bei10", "dxy", "gold", "brent", "ovx",
    "sox_ndx_ratio", "aaii_bull", "aaii_bear", "fng_score", "junk_spread", "stage", "n_pass",
    "ftd_state", "ftd_day", "ftd_score",
]


def upsert(row: Dict[str, Any]) -> pd.DataFrame:
    rec = {k: row.get(k) for k in HISTORY_FIELDS}
    if HISTORY_CSV.exists():
        df = pd.read_csv(HISTORY_CSV, dtype={"date_tw": str, "session_date": str, "stage": str, "ftd_state": str})
        df = df.reindex(columns=HISTORY_FIELDS)
    else:
        df = pd.DataFrame(columns=HISTORY_FIELDS)
    df = df[df["date_tw"] != rec["date_tw"]]
    df = pd.concat([x for x in (df, pd.DataFrame([rec])) if len(x)], ignore_index=True)
    df = df.sort_values("date_tw").reset_index(drop=True)
    tmp = HISTORY_CSV.with_suffix(".tmp")
    df.to_csv(tmp, index=False)
    tmp.replace(HISTORY_CSV)
    return df


def recent(n: int = 15) -> List[Dict[str, Any]]:
    if not HISTORY_CSV.exists():
        return []
    df = pd.read_csv(HISTORY_CSV, dtype={"date_tw": str, "session_date": str, "stage": str, "ftd_state": str}).tail(n)
    out = []
    for _, r in df.iterrows():
        d = {}
        for k, v in r.items():
            if isinstance(v, float):
                d[k] = None if v != v else round(v, 4)
            else:
                d[k] = v
        out.append(d)
    return out
