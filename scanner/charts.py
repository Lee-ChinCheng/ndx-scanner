"""Small trend charts rendered with matplotlib (Agg) for embedding in the PDF."""
import logging
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
import pandas as pd  # noqa: E402

from .config import CHART_DIR  # noqa: E402

log = logging.getLogger(__name__)

_CJK_CANDIDATES = ["Noto Sans CJK TC", "Noto Sans CJK SC", "Noto Sans CJK JP", "Droid Sans Fallback", "DejaVu Sans"]


def _setup_fonts() -> None:
    avail = {f.name for f in font_manager.fontManager.ttflist}
    fams = [f for f in _CJK_CANDIDATES if f in avail] or ["DejaVu Sans"]
    plt.rcParams["font.family"] = "sans-serif"
    plt.rcParams["font.sans-serif"] = fams + ["DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


BLUE, ORANGE, GREY, RED, GREEN = "#2457a6", "#d9822b", "#8a8f98", "#c0392b", "#2e8b57"


def _style(ax, title: str):
    ax.set_title(title, fontsize=10, loc="left", pad=6)
    ax.grid(True, axis="y", alpha=0.25)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.tick_params(labelsize=7)
    ax.xaxis.set_major_locator(matplotlib.dates.AutoDateLocator(minticks=3, maxticks=6))
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%m/%d"))


def _tail(s: Optional[pd.Series], days: int) -> Optional[pd.Series]:
    if s is None or len(s) == 0:
        return None
    return s.dropna().tail(days)


def render_all(series: Dict[str, pd.Series], breadth: Optional[pd.DataFrame], days: int = 120) -> List[Path]:
    """series keys expected: ndx, ndx_sma50, ndx_sma200, vxn, vix, hy_oas, brent, ovx, gold, dxy, sox_ndx."""
    _setup_fonts()
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    out: List[Path] = []

    def save(fig, name):
        p = CHART_DIR / f"{name}.png"
        fig.tight_layout()
        fig.savefig(p, dpi=160)
        plt.close(fig)
        out.append(p)

    try:
        ndx = _tail(series.get("ndx"), days)
        if ndx is not None:
            fig, ax = plt.subplots(figsize=(4.6, 2.4))
            ax.plot(ndx.index, ndx.values, color=BLUE, lw=1.4, label="NDX")
            for k, c, lb in (("ndx_sma50", ORANGE, "50DMA"), ("ndx_sma200", GREY, "200DMA")):
                s = _tail(series.get(k), days)
                if s is not None:
                    ax.plot(s.index, s.values, color=c, lw=1.0, ls="--", label=lb)
            ax.legend(fontsize=7, frameon=False, loc="upper left")
            _style(ax, "Nasdaq-100 與 50/200 日均線")
            save(fig, "01_ndx")

        vxn, vix = _tail(series.get("vxn"), days), _tail(series.get("vix"), days)
        if vxn is not None:
            fig, ax = plt.subplots(figsize=(4.6, 2.4))
            ax.plot(vxn.index, vxn.values, color=RED, lw=1.4, label="VXN")
            if vix is not None:
                ax.plot(vix.index, vix.values, color=GREY, lw=1.0, label="VIX")
            ax.legend(fontsize=7, frameon=False, loc="upper left")
            _style(ax, "VXN 與 VIX（隱含波動率）")
            save(fig, "02_vol")

        if breadth is not None and len(breadth):
            b = breadth.tail(days)
            fig, ax = plt.subplots(figsize=(4.6, 2.4))
            ax.plot(b.index, b["pct_above_20ema"], color=BLUE, lw=1.0, label="站上 20EMA")
            ax.plot(b.index, b["pct_above_50sma"], color=ORANGE, lw=1.3, label="站上 50DMA")
            ax.plot(b.index, b["pct_above_200sma"], color=GREY, lw=1.0, label="站上 200DMA")
            ax.set_ylim(0, 100)
            ax.legend(fontsize=7, frameon=False, loc="lower left")
            _style(ax, "Nasdaq-100 成分股市場廣度（%）")
            save(fig, "03_breadth")

        oas = _tail(series.get("hy_oas"), days)
        if oas is not None:
            fig, ax = plt.subplots(figsize=(4.6, 2.4))
            ax.plot(oas.index, oas.values, color=GREEN, lw=1.4)
            _style(ax, "ICE BofA 高收益債 OAS（%）")
            save(fig, "04_hyoas")

        brent, ovx = _tail(series.get("brent"), days), _tail(series.get("ovx"), days)
        if brent is not None:
            fig, ax = plt.subplots(figsize=(4.6, 2.4))
            ax.plot(brent.index, brent.values, color=BLUE, lw=1.4, label="Brent (USD)")
            if ovx is not None:
                ax2 = ax.twinx()
                ax2.plot(ovx.index, ovx.values, color=ORANGE, lw=1.0, label="OVX")
                ax2.tick_params(labelsize=7)
                ax2.spines["top"].set_visible(False)
                ax2.legend(fontsize=7, frameon=False, loc="upper right")
            ax.legend(fontsize=7, frameon=False, loc="upper left")
            _style(ax, "Brent 原油與 OVX")
            save(fig, "05_brent")

        gold, dxy = _tail(series.get("gold"), days), _tail(series.get("dxy"), days)
        if gold is not None or dxy is not None:
            fig, ax = plt.subplots(figsize=(4.6, 2.4))
            if gold is not None:
                ax.plot(gold.index, gold.values, color=ORANGE, lw=1.4, label="黃金 (USD/oz)")
                ax.legend(fontsize=7, frameon=False, loc="upper left")
            if dxy is not None:
                ax2 = ax.twinx()
                ax2.plot(dxy.index, dxy.values, color=GREEN, lw=1.0, label="DXY")
                ax2.tick_params(labelsize=7)
                ax2.spines["top"].set_visible(False)
                ax2.legend(fontsize=7, frameon=False, loc="upper right")
            _style(ax, "黃金與美元指數 DXY")
            save(fig, "06_gold_dxy")

        rs = _tail(series.get("sox_ndx"), days)
        if rs is not None:
            fig, ax = plt.subplots(figsize=(4.6, 2.4))
            ax.plot(rs.index, rs.values, color=BLUE, lw=1.4)
            _style(ax, "SOX / NDX 相對強弱比值")
            save(fig, "07_sox_rel")

        y = _tail(series.get("real10"), days)
        if y is not None:
            fig, ax = plt.subplots(figsize=(4.6, 2.4))
            ax.plot(y.index, y.values, color=GREEN, lw=1.4, label="10Y 實質")
            n = _tail(series.get("y10"), days)
            if n is not None:
                ax.plot(n.index, n.values, color=GREY, lw=1.0, label="10Y 名目")
            ax.legend(fontsize=7, frameon=False, loc="upper left")
            _style(ax, "美國 10 年期殖利率（%）")
            save(fig, "08_yields")
        f = _tail(series.get("fng"), days)
        if f is not None:
            fig, ax = plt.subplots(figsize=(4.6, 2.4))
            ax.plot(f.index, f.values, color=RED, lw=1.4)
            ax.axhspan(0, 25, color=RED, alpha=0.07); ax.axhspan(75, 100, color=GREEN, alpha=0.07)
            ax.set_ylim(0, 100)
            _style(ax, "CNN Fear & Greed（0 極恐懼 ~ 100 極貪婪）")
            save(fig, "09_fng")
    except Exception as e:  # noqa: BLE001
        log.exception("chart rendering error: %s", e)
    return out
