"""Thin HTTP helper with retries, a browser-like User-Agent and header capture."""
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def make_session(retries: int = 2) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"})
    return s


SESSION = make_session()


def get(url: str, timeout: int = 30, headers: Optional[dict] = None) -> Tuple[bytes, dict, datetime]:
    """GET a URL. Returns (content, headers, fetched_at_utc). Raises on HTTP error."""
    fetched_at = datetime.now(timezone.utc)
    r = SESSION.get(url, timeout=timeout, headers=headers)
    r.raise_for_status()
    return r.content, dict(r.headers), fetched_at


def parse_last_modified(headers: dict) -> Optional[datetime]:
    lm = headers.get("Last-Modified") or headers.get("last-modified")
    if not lm:
        return None
    try:
        return parsedate_to_datetime(lm)
    except Exception:  # noqa: BLE001
        return None
