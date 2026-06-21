"""Resolve company names from spreadsheet data or recipient email domain."""

from __future__ import annotations

import re
from urllib.parse import urlparse

FREEMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.co.uk",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "msn.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "protonmail.com",
        "proton.me",
        "pm.me",
        "mail.com",
        "zoho.com",
        "yandex.com",
        "gmx.com",
        "fastmail.com",
        "hey.com",
    }
)

MAIL_SUBDOMAINS = frozenset({"mail", "email", "smtp", "mx", "www"})


def email_domain(email: str) -> str | None:
    raw = (email or "").strip().lower()
    if "@" not in raw:
        return None
    domain = raw.rsplit("@", 1)[-1].strip().strip(".")
    if not domain or "." not in domain:
        return None
    return domain


def _domain_base(domain: str) -> str:
    parts = [p for p in domain.lower().split(".") if p]
    if len(parts) >= 3 and parts[0] in MAIL_SUBDOMAINS:
        parts = parts[1:]
    if len(parts) >= 2 and parts[-1] in {"edu", "gov", "org", "com", "io", "co", "net", "ai"}:
        return parts[-2] if len(parts) >= 2 else parts[0]
    return parts[0] if parts else ""


def infer_company_from_domain(domain: str) -> str:
    base = _domain_base(domain)
    if not base or len(base) < 2:
        return ""
    if base in FREEMAIL_DOMAINS:
        return ""

    spaced = re.sub(r"[-_]+", " ", base)
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", spaced)
    spaced = re.sub(r"(\d+)", r" \1 ", spaced)
    words = [w for w in re.split(r"\s+", spaced.strip()) if w]
    if not words:
        return ""

    formatted = " ".join(w[:1].upper() + w[1:].lower() if w.isalpha() else w.title() for w in words)
    if len(base) <= 5 and base.isalpha():
        return base.upper()
    return formatted.strip()


def infer_company_from_website(website: str) -> str:
    raw = (website or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    domain = parsed.netloc.lower().removeprefix("www.")
    if not domain:
        return ""
    return infer_company_from_domain(domain)


def resolve_company_for_row(row: dict) -> dict:
    """Return row copy with resolved company_name and flags."""
    enriched = dict(row)
    original = str(row.get("company_name") or "").strip()
    email = str(row.get("email") or "").strip()
    website = str(row.get("company_website") or "").strip()

    if original:
        enriched["company_name"] = original
        enriched["company_name_source"] = "sheet"
        enriched["company_name_missing"] = False
        return enriched

    inferred = infer_company_from_website(website)
    source = "website"
    if not inferred:
        domain = email_domain(email)
        if domain and domain not in FREEMAIL_DOMAINS:
            inferred = infer_company_from_domain(domain)
            source = "email_domain" if inferred else "none"
        else:
            source = "none"

    if inferred:
        enriched["company_name"] = inferred
        enriched["company_name_source"] = source
        enriched["company_name_missing"] = False
    else:
        enriched["company_name"] = ""
        enriched["company_name_source"] = "none"
        enriched["company_name_missing"] = True

    return enriched


def enrich_rows(rows: list[dict]) -> list[dict]:
    return [resolve_company_for_row(row) for row in rows]
