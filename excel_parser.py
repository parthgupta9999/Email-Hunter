"""Parse Excel files and detect email, person name, and company columns."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
MAX_RECIPIENTS = 50

PLACEHOLDER_PERSON = "{person name}"
PLACEHOLDER_COMPANY = "{company name}"

EMAIL_HINTS = (
    "email",
    "e-mail",
    "e mail",
    "mail",
    "email address",
    "emailaddress",
    "work email",
    "contact email",
)
NAME_HINTS = (
    "name",
    "person",
    "person name",
    "contact",
    "contact name",
    "full name",
    "fullname",
    "recruiter",
    "recruiter name",
    "first name",
    "firstname",
)
COMPANY_HINTS = (
    "company",
    "company name",
    "organization",
    "organisation",
    "org",
    "employer",
    "firm",
    "business",
    "companyname",
)
WEBSITE_HINTS = (
    "website",
    "company website",
    "company url",
    "url",
    "site",
    "domain",
    "web",
    "homepage",
)


def _normalize_header(value: object) -> str:
    text = str(value).strip().lower()
    text = text.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", text)


def _score_header(header: str, hints: tuple[str, ...]) -> int:
    if header in hints:
        return 100
    for hint in hints:
        if hint in header or header in hint:
            return 80
    for hint in hints:
        parts = hint.split()
        if all(part in header for part in parts):
            return 60
    return 0


def _detect_column(columns: list[str], hints: tuple[str, ...], exclude: set[str]) -> str | None:
    best_col = None
    best_score = 0
    for col in columns:
        if col in exclude:
            continue
        score = _score_header(col, hints)
        if score > best_score:
            best_score = score
            best_col = col
    return best_col if best_score >= 60 else None


def _detect_email_by_content(df: pd.DataFrame, exclude: set[str]) -> str | None:
    for col in df.columns:
        if col in exclude:
            continue
        sample = df[col].dropna().astype(str).head(20)
        if sample.empty:
            continue
        matches = sum(1 for v in sample if EMAIL_RE.match(v.strip()))
        if matches >= max(1, len(sample) * 0.6):
            return col
    return None


def parse_excel(path: Path, limit: int = MAX_RECIPIENTS) -> dict:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        df = pd.read_csv(path)
    elif suffix in {".xlsx", ".xls", ".xlsm"}:
        df = pd.read_excel(path)
    else:
        raise ValueError("Unsupported file type. Upload .xlsx, .xls, or .csv")

    if df.empty:
        raise ValueError("The spreadsheet is empty.")

    normalized = {_normalize_header(c): c for c in df.columns}
    norm_cols = list(normalized.keys())

    email_col = _detect_column(norm_cols, EMAIL_HINTS, set())
    person_col = _detect_column(norm_cols, NAME_HINTS, {email_col} if email_col else set())
    company_col = _detect_column(
        norm_cols,
        COMPANY_HINTS,
        {c for c in (email_col, person_col) if c},
    )
    website_col = _detect_column(
        norm_cols,
        WEBSITE_HINTS,
        {c for c in (email_col, person_col, company_col) if c},
    )

    if not email_col:
        email_col = _detect_email_by_content(df, set())

    if not email_col:
        raise ValueError(
            "Could not find an email column. Add a column named 'Email' (or similar)."
        )

    email_key = normalized[email_col]
    person_key = normalized[person_col] if person_col else None
    company_key = normalized[company_col] if company_col else None
    website_key = normalized[website_col] if website_col else None

    rows: list[dict] = []
    seen_emails: set[str] = set()
    total_valid = 0

    for _, raw in df.iterrows():
        email = str(raw.get(email_key, "")).strip().lower()
        if not email or email == "nan" or not EMAIL_RE.match(email):
            continue
        total_valid += 1
        if email in seen_emails:
            continue
        seen_emails.add(email)

        person = ""
        if person_key:
            val = raw.get(person_key, "")
            person = "" if pd.isna(val) else str(val).strip()

        company = ""
        if company_key:
            val = raw.get(company_key, "")
            company = "" if pd.isna(val) else str(val).strip()

        website = ""
        if website_key:
            val = raw.get(website_key, "")
            website = "" if pd.isna(val) else str(val).strip()

        if len(rows) < limit:
            rows.append(
                {
                    "email": email,
                    "person_name": person,
                    "company_name": company,
                    "company_website": website,
                }
            )

    if not rows:
        raise ValueError("No valid email addresses found in the spreadsheet.")

    placeholders = []
    if person_key:
        placeholders.append(
            {
                "token": PLACEHOLDER_PERSON,
                "label": "Person name",
                "description": "Inserts the contact's name from your sheet",
            }
        )
    if company_key:
        placeholders.append(
            {
                "token": PLACEHOLDER_COMPANY,
                "label": "Company name",
                "description": "Inserts the company name from your sheet",
            }
        )

    placeholder_fields = {
        "person_name": _field_meta(person_key, rows, "person_name", PLACEHOLDER_PERSON),
        "company_name": _field_meta(company_key, rows, "company_name", PLACEHOLDER_COMPANY),
    }

    return {
        "rows": rows,
        "total_rows_in_file": len(df),
        "total_valid_emails": total_valid,
        "selected_count": len(rows),
        "truncated": total_valid > limit,
        "detected_columns": {
            "email": email_key,
            "person_name": person_key,
            "company_name": company_key,
            "company_website": website_key,
        },
        "placeholders": placeholders,
        "placeholder_fields": placeholder_fields,
    }


def _count_empty(rows: list[dict], field: str) -> int:
    return sum(1 for r in rows if not str(r.get(field) or "").strip())


def _field_meta(
    column: str | None,
    rows: list[dict],
    field: str,
    token: str,
) -> dict:
    has_column = bool(column)
    empty_count = _count_empty(rows, field) if has_column else 0
    return {
        "token": token,
        "has_column": has_column,
        "column": column,
        "has_empty": empty_count > 0,
        "empty_count": empty_count,
    }


def placeholder_fields_from_data(data: dict) -> dict:
    if "placeholder_fields" in data:
        fields = data["placeholder_fields"]
        rows = data.get("rows", [])
        result = {}
        for key, token in (
            ("person_name", PLACEHOLDER_PERSON),
            ("company_name", PLACEHOLDER_COMPANY),
        ):
            meta = fields.get(key, {})
            column = meta.get("column")
            has_column = meta.get("has_column", meta.get("from_sheet", bool(column)))
            if has_column and "has_empty" not in meta and rows:
                empty_count = _count_empty(rows, key)
                result[key] = {
                    "token": token,
                    "has_column": True,
                    "column": column,
                    "has_empty": empty_count > 0,
                    "empty_count": empty_count,
                }
            else:
                result[key] = {
                    "token": token,
                    "has_column": has_column,
                    "column": column,
                    "has_empty": bool(meta.get("has_empty")) if has_column else False,
                    "empty_count": int(meta.get("empty_count", 0)) if has_column else 0,
                }
        return result

    cols = data.get("detected_columns", {})
    rows = data.get("rows", [])
    return {
        "person_name": _field_meta(cols.get("person_name"), rows, "person_name", PLACEHOLDER_PERSON),
        "company_name": _field_meta(cols.get("company_name"), rows, "company_name", PLACEHOLDER_COMPANY),
    }


def available_tokens(data: dict, fallbacks: dict | None) -> set[str]:
    del fallbacks  # fallbacks fill empty cells only; tokens depend on column presence
    fields = placeholder_fields_from_data(data)
    tokens: set[str] = set()
    if fields["person_name"]["has_column"]:
        tokens.add(PLACEHOLDER_PERSON)
    if fields["company_name"]["has_column"]:
        tokens.add(PLACEHOLDER_COMPANY)
    return tokens


def validate_template(text: str, available_tokens: set[str]) -> list[str]:
    errors = []
    if PLACEHOLDER_PERSON in text and PLACEHOLDER_PERSON not in available_tokens:
        errors.append(
            f'"{PLACEHOLDER_PERSON}" requires a name column in your spreadsheet.'
        )
    if PLACEHOLDER_COMPANY in text and PLACEHOLDER_COMPANY not in available_tokens:
        errors.append(
            f'"{PLACEHOLDER_COMPANY}" requires a company column in your spreadsheet.'
        )
    return errors


def validate_empty_fallbacks(
    subject: str,
    body: str,
    data: dict,
    fallbacks: dict | None,
) -> list[str]:
    fallbacks = fallbacks or {}
    fields = placeholder_fields_from_data(data)
    text = f"{subject}{body}"
    errors: list[str] = []

    if PLACEHOLDER_PERSON in text and fields["person_name"]["has_empty"]:
        if not str(fallbacks.get("person_name", "")).strip():
            count = fields["person_name"]["empty_count"]
            errors.append(
                f'Set a value for empty name cells ({count} row{"s" if count != 1 else ""}) in the sidebar.'
            )

    if PLACEHOLDER_COMPANY in text and fields["company_name"]["has_empty"]:
        if not str(fallbacks.get("company_name", "")).strip():
            count = fields["company_name"]["empty_count"]
            errors.append(
                f'Set a value for empty company cells ({count} row{"s" if count != 1 else ""}) in the sidebar.'
            )

    return errors


def render_template(text: str, row: dict, fallbacks: dict | None = None) -> str:
    fallbacks = fallbacks or {}
    person = row.get("person_name") or fallbacks.get("person_name", "")
    company = row.get("company_name") or fallbacks.get("company_name", "")
    result = text.replace(PLACEHOLDER_PERSON, person).replace(PLACEHOLDER_COMPANY, company)
    return result
