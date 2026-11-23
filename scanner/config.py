import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
CHART_DIR = CACHE_DIR / "charts"
LOG_DIR = ROOT / "logs"
HISTORY_CSV = DATA_DIR / "history.csv"

TZ_TW = ZoneInfo("Asia/Taipei")
TZ_US = ZoneInfo("America/New_York")

load_dotenv(ROOT / ".env")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()

_DEFAULTS = {
    "llm": {
        "model": "anthropic/claude-sonnet-5.5",
        "fallback_model": "anthropic/claude-haiku-5.5",
        "temperature": 0.2,
        "max_tokens": 8000,
        "reasoning_effort": "low",
        "timeout_sec": 180,
    },
    "report": {
        "title": "Nasdaq-100 築底訊號掃描報告",
        "filename_prefix": "nasdaq_",
        "chart_days": 120,
    },
    "fetch": {
        "breadth_threads": 4,
        "breadth_pause_sec": 0.15,
        "pcr_backfill_days": 60,
        "constituents_cache_days": 7,
        "http_timeout_sec": 30,
    },
    "history_days_for_llm": 15,
    # email sending （.env：EVAS_EMAIL / EVAS_PASSWORD / NOTIFY_RECIPIENTS）
    "notify": {
        "enabled": True,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "recipients": [],  # 收件人請放 .env 的 NOTIFY_RECIPIENTS（逗號分隔），避免進版控
        "sender_name": "Nasdaq Scanner",
        "timeout_sec": 60,
        "retries": 3,
    },
    # Follow-Through Day 偵測參數（未列出的沿用 scanner/ftd.py 的 DEFAULTS）
    "ftd": {
        "enabled": True,
        "min_correction_pct": 3.0,
        "ftd_min_gain_pct": 1.25,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_recipients() -> list:
    """Recipients from .env NOTIFY_RECIPIENTS (comma-separated); [] when unset."""
    raw = os.environ.get("NOTIFY_RECIPIENTS", "")
    return [v.strip() for v in raw.split(",") if v.strip()]


def load_config() -> dict:
    cfg_path = ROOT / "config.json"
    cfg = dict(_DEFAULTS)
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as f:
            cfg = _merge(_DEFAULTS, json.load(f))
    # .env wins over config.json so the recipient list never needs to be committed.
    env_rcpts = _env_recipients()
    if env_rcpts:
        cfg = _merge(cfg, {"notify": {"recipients": env_rcpts}})
    return cfg


def ensure_dirs() -> None:
    for d in (OUTPUT_DIR, DATA_DIR, CACHE_DIR, CHART_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
