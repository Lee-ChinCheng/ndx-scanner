"""Email the finished PDF report via SMTP over SSL (default: Gmail, smtp.gmail.com:465).

Credentials come from .env: EVAS_EMAIL (sender / login) and EVAS_PASSWORD.
Recipients come from .env NOTIFY_RECIPIENTS (comma-separated), which overrides
config.json notify.recipients.
For a Gmail / Google Workspace account EVAS_PASSWORD must be a 16-char App Password
(https://myaccount.google.com/apppasswords); the normal login password is rejected.

Standalone resend:  .venv/bin/python -m scanner.notifier output/nasdaq_10.pdf [--to a@x.com,b@y.com]
"""
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Iterable, Optional

from scanner.config import load_config  # also loads .env

log = logging.getLogger("scanner.notifier")


def _recipients(value) -> list:
    if isinstance(value, str):
        value = value.split(",")
    return [v.strip() for v in (value or []) if v and v.strip()]


def send_report(pdf_path: Path, subject: str, body: str, ncfg: dict,
                to: Optional[Iterable[str]] = None) -> bool:
    """Send pdf_path as an attachment. Returns True on success; never raises."""
    pdf_path = Path(pdf_path)
    user = os.environ.get("EVAS_EMAIL", "").strip()
    password = os.environ.get("EVAS_PASSWORD", "").strip().replace(" ", "")  # app passwords are often pasted with spaces
    rcpts = _recipients(to if to is not None else ncfg.get("recipients"))
    if not user or not password:
        log.error("email skipped: EVAS_EMAIL / EVAS_PASSWORD missing in .env")
        return False
    if not rcpts:
        log.error("email skipped: no recipients (set NOTIFY_RECIPIENTS in .env, or notify.recipients in config.json)")
        return False
    if not pdf_path.is_file():
        log.error("email skipped: report not found: %s", pdf_path)
        return False

    msg = EmailMessage()
    msg["From"] = formataddr((ncfg.get("sender_name") or "", user))
    msg["To"] = ", ".join(rcpts)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)
    msg.add_attachment(pdf_path.read_bytes(), maintype="application", subtype="pdf", filename=pdf_path.name)

    host = ncfg.get("smtp_host", "smtp.gmail.com")
    port = int(ncfg.get("smtp_port", 465))
    timeout = float(ncfg.get("timeout_sec", 60))
    retries = max(1, int(ncfg.get("retries", 3)))
    ctx = ssl.create_default_context()
    for attempt in range(1, retries + 1):
        try:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=timeout) as s:
                s.login(user, password)
                s.send_message(msg)
            log.info("email sent: %s -> %s (%d KB)", pdf_path.name, ", ".join(rcpts), pdf_path.stat().st_size // 1024)
            return True
        except smtplib.SMTPAuthenticationError as e:
            # Retrying won't help; give an actionable hint.
            log.error("email auth failed (%s %s). For Gmail/Workspace, EVAS_PASSWORD must be an App Password "
                      "(2-Step Verification on -> https://myaccount.google.com/apppasswords).",
                      e.smtp_code, (e.smtp_error or b"").decode(errors="replace")[:200])
            return False
        except (smtplib.SMTPException, OSError) as e:
            log.warning("email attempt %d/%d failed: %s", attempt, retries, e)
            if attempt < retries:
                import time
                time.sleep(10 * attempt)
    log.error("email failed after %d attempts: %s", retries, pdf_path.name)
    return False


def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Resend an existing report PDF by email.")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--to", help="comma-separated recipients (default: .env NOTIFY_RECIPIENTS / config.json notify.recipients)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    ncfg = cfg.get("notify", {})
    title = cfg["report"]["title"]
    ok = send_report(args.pdf, f"{title} — {args.pdf.stem}", f"{title}\n\n附件：{args.pdf.name}\n", ncfg,
                     to=_recipients(args.to) if args.to else None)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
