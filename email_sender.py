"""SMTP sending (Gmail & Outlook) with rate limits and logging."""

from __future__ import annotations

import csv
import json
import os
import random
import smtplib
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).expanduser()
SENT_LOG = DATA_DIR / "sent_log.csv"
DAILY_STATE = DATA_DIR / "daily_state.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

_daily_state_lock = threading.Lock()

_SECRET_SETTING_KEYS = ("app_password", "gmail_app_password")

RECOMMENDED_DAILY_LIMIT = 50
DAILY_LIMIT = RECOMMENDED_DAILY_LIMIT  # advisory — not enforced on send
DEFAULT_DELAY_SECONDS = 60.0
DEFAULT_DELAY_JITTER = 30.0

SUPPORTED_PROVIDERS = frozenset({"gmail", "outlook"})

OUTLOOK_CONSUMER_DOMAINS = frozenset(
    {
        "outlook.com",
        "outlook.co.uk",
        "hotmail.com",
        "hotmail.co.uk",
        "live.com",
        "live.co.uk",
        "msn.com",
    }
)


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(exist_ok=True)


def _normalize_provider(provider: str | None) -> str:
    value = (provider or "gmail").strip().lower()
    return value if value in SUPPORTED_PROVIDERS else "gmail"


def smtp_host_for(provider: str, email_address: str) -> str:
    provider = _normalize_provider(provider)
    if provider == "gmail":
        return "smtp.gmail.com"
    domain = email_address.rsplit("@", 1)[-1].lower()
    if domain in OUTLOOK_CONSUMER_DOMAINS:
        return "smtp-mail.outlook.com"
    return "smtp.office365.com"


def _strip_secrets_from_settings_file() -> None:
    """Remove any persisted passwords from disk (session-only going forward)."""
    if not SETTINGS_FILE.exists():
        return
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    dirty = any(key in data for key in _SECRET_SETTING_KEYS)
    if not dirty:
        return
    for key in _SECRET_SETTING_KEYS:
        data.pop(key, None)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_settings() -> dict:
    ensure_data_dir()
    load_dotenv(ROOT / ".env")
    _strip_secrets_from_settings_file()

    settings = {
        "mail_provider": _normalize_provider(os.getenv("MAIL_PROVIDER", "gmail")),
        "email_address": os.getenv("EMAIL_ADDRESS", os.getenv("GMAIL_ADDRESS", "")).strip(),
        "app_password": os.getenv("APP_PASSWORD", os.getenv("GMAIL_APP_PASSWORD", "")).strip(),
        "delay_seconds": float(os.getenv("DELAY_SECONDS", str(DEFAULT_DELAY_SECONDS))),
        "delay_jitter": float(os.getenv("DELAY_JITTER_SECONDS", str(DEFAULT_DELAY_JITTER))),
    }

    if SETTINGS_FILE.exists():
        file_settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if file_settings.get("mail_provider"):
            settings["mail_provider"] = _normalize_provider(file_settings["mail_provider"])
        for new_key, legacy_key in (
            ("email_address", "gmail_address"),
            ("delay_seconds", "delay_seconds"),
            ("delay_jitter", "delay_jitter"),
        ):
            if file_settings.get(new_key):
                settings[new_key] = file_settings[new_key]
            elif file_settings.get(legacy_key):
                settings[new_key] = file_settings[legacy_key]

    settings["gmail_address"] = settings["email_address"]
    settings["gmail_app_password"] = settings["app_password"]
    return settings


def save_settings(email_address: str, mail_provider: str = "gmail") -> None:
    """Persist mail preferences only — never write app passwords to disk."""
    ensure_data_dir()
    _strip_secrets_from_settings_file()
    existing: dict = {}
    if SETTINGS_FILE.exists():
        try:
            existing = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    for key in _SECRET_SETTING_KEYS:
        existing.pop(key, None)
    provider = _normalize_provider(mail_provider)
    existing["mail_provider"] = provider
    existing["email_address"] = email_address.strip()
    existing["gmail_address"] = existing["email_address"]
    SETTINGS_FILE.write_text(json.dumps(existing, indent=2), encoding="utf-8")


@contextmanager
def smtp_session(provider: str, email_address: str, app_password: str):
    host = smtp_host_for(provider, email_address)
    with smtplib.SMTP(host, 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(email_address.strip(), app_password.strip())
        yield smtp


def test_mail_connection(
    mail_provider: str,
    email_address: str,
    app_password: str,
) -> tuple[bool, str]:
    provider = _normalize_provider(mail_provider)
    address = email_address.strip()
    password = app_password.strip()
    if not address or not password:
        return False, "Email address and app password are required."

    try:
        with smtp_session(provider, address, password):
            pass
        label = "Gmail" if provider == "gmail" else "Outlook"
        return True, f"{label} connected successfully."
    except smtplib.SMTPAuthenticationError:
        if provider == "gmail":
            return False, (
                "Authentication failed. Use a Gmail App Password (not your regular password). "
                "Use the “Generate App Password” link above."
            )
        return False, (
            "Authentication failed. Use a Microsoft app password (not your regular password). "
            "For work/school accounts, SMTP must be enabled by your admin."
        )
    except smtplib.SMTPException as exc:
        return False, f"Connection failed: {exc}"


def test_gmail_connection(gmail_address: str, gmail_app_password: str) -> tuple[bool, str]:
    return test_mail_connection("gmail", gmail_address, gmail_app_password)


def load_sent_log() -> dict[str, dict]:
    if not SENT_LOG.exists():
        return {}
    sent: dict[str, dict] = {}
    with SENT_LOG.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sent[row["email"].lower()] = row
    return sent


def append_sent_log(email: str, status: str, detail: str = "") -> None:
    ensure_data_dir()
    write_header = not SENT_LOG.exists()
    with SENT_LOG.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["email", "sent_at", "status", "detail"])
        if write_header:
            writer.writeheader()
        writer.writerow(
            {
                "email": email,
                "sent_at": datetime.now(timezone.utc).isoformat(),
                "status": status,
                "detail": detail,
            }
        )


def _sender_key(email_address: str) -> str:
    return email_address.strip().lower()


def _load_daily_store() -> dict:
    ensure_data_dir()
    today = str(date.today())
    if not DAILY_STATE.exists():
        return {"date": today, "accounts": {}}
    raw = json.loads(DAILY_STATE.read_text(encoding="utf-8"))
    if raw.get("date") != today:
        return {"date": today, "accounts": {}}
    accounts = raw.get("accounts")
    if isinstance(accounts, dict):
        return {"date": today, "accounts": {str(k).lower(): int(v) for k, v in accounts.items()}}
    return {"date": today, "accounts": {}}


def load_daily_state(email_address: str = "") -> dict:
    """Daily send count for one connected sending address (resets at midnight local time)."""
    key = _sender_key(email_address)
    store = _load_daily_store()
    count = store["accounts"].get(key, 0) if key else 0
    return {"date": store["date"], "count": count, "email_address": key}


def save_daily_state(email_address: str, count: int) -> None:
    key = _sender_key(email_address)
    if not key:
        return
    with _daily_state_lock:
        store = _load_daily_store()
        store["accounts"][key] = int(count)
        DAILY_STATE.write_text(json.dumps(store, indent=2), encoding="utf-8")


def increment_daily_sent(email_address: str) -> int:
    """Atomically increment today's send count for one address."""
    key = _sender_key(email_address)
    if not key:
        return 0
    with _daily_state_lock:
        store = _load_daily_store()
        count = int(store["accounts"].get(key, 0)) + 1
        store["accounts"][key] = count
        DAILY_STATE.write_text(json.dumps(store, indent=2), encoding="utf-8")
        return count


def remaining_today(email_address: str = "") -> int:
    """Emails left until the recommended daily cap (0 if already at or over it)."""
    state = load_daily_state(email_address)
    return max(DAILY_LIMIT - state["count"], 0)


def over_recommended_daily_limit(email_address: str = "") -> bool:
    state = load_daily_state(email_address)
    return state["count"] >= DAILY_LIMIT


def build_message(
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    attachments: list[tuple[Path, str]] | None = None,
) -> MIMEMultipart:
    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    for path, filename in attachments or []:
        if not path.exists():
            continue
        with path.open("rb") as f:
            part = MIMEApplication(f.read(), Name=filename)
        part["Content-Disposition"] = f'attachment; filename="{filename}"'
        msg.attach(part)

    return msg


def send_batch(
    rows: list[dict],
    subject_template: str,
    body_template: str,
    attachments: list[tuple[Path, str]] | None,
    render_fn,
    on_progress=None,
    mail_settings: dict | None = None,
) -> dict:
    settings = mail_settings or load_settings()
    address = settings["email_address"]
    password = settings["app_password"]
    provider = settings["mail_provider"]
    delay = float(settings.get("delay_seconds", DEFAULT_DELAY_SECONDS))
    jitter = float(settings.get("delay_jitter", DEFAULT_DELAY_JITTER))

    if not address or not password:
        raise ValueError("Email account is not configured. Connect your account first.")

    batch = rows
    results = {"sent": 0, "failed": 0, "skipped_limit": 0, "details": []}

    with smtp_session(provider, address, password) as smtp:
        for i, row in enumerate(batch):
            email = row["email"]
            subject = render_fn(subject_template, row)
            body = render_fn(body_template, row)
            msg = build_message(address, email, subject, body, attachments)

            try:
                smtp.send_message(msg)
                append_sent_log(email, "sent")
                increment_daily_sent(address)
                results["sent"] += 1
                results["details"].append({"email": email, "status": "sent"})
            except smtplib.SMTPException as exc:
                append_sent_log(email, "failed", str(exc))
                results["failed"] += 1
                results["details"].append({"email": email, "status": "failed", "error": str(exc)})

            if on_progress:
                on_progress(i + 1, len(batch), email, results["details"][-1]["status"])

            if i < len(batch) - 1:
                time.sleep(delay + random.uniform(0, max(jitter, 0)))

    return results


def send_single(
    row: dict,
    subject_template: str,
    body_template: str,
    attachments: list[tuple[Path, str]] | None,
    render_fn,
    mail_settings: dict | None = None,
) -> dict:
    """Send one email. Raises ValueError on config/limit issues."""
    settings = mail_settings or load_settings()
    address = settings["email_address"]
    password = settings["app_password"]
    provider = _normalize_provider(settings["mail_provider"])

    if not address or not password:
        raise ValueError("Email account is not configured. Connect your account first.")

    email = row["email"]
    subject = render_fn(subject_template, row)
    body = render_fn(body_template, row)
    msg = build_message(address, email, subject, body, attachments)

    with smtp_session(provider, address, password) as smtp:
        smtp.send_message(msg)

    append_sent_log(email, "sent")
    increment_daily_sent(address)
    return {"email": email, "status": "sent"}
