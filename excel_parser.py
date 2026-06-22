"""Parse Excel files and detect email, person name, company, website, and about columns."""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

import pandas as pd

from company_resolver import resolve_company_for_row

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
ABOUT_HINTS = (
    "about",
    "about company",
    "company about",
    "about us",
    "company description",
    "company info",
    "company overview",
    "company background",
    "about the company",
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
    about_col = _detect_column(
        norm_cols,
        ABOUT_HINTS,
        {c for c in (email_col, person_col, company_col, website_col) if c},
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
    about_key = normalized[about_col] if about_col else None

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

        company_about = ""
        if about_key:
            val = raw.get(about_key, "")
            company_about = "" if pd.isna(val) else str(val).strip()

        if len(rows) < limit:
            rows.append(
                {
                    "email": email,
                    "person_name": person,
                    "company_name": company,
                    "company_website": website,
                    "company_about": company_about,
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

    payload = {
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
            "company_about": about_key,
        },
        "sheet_about_count": sum(1 for r in rows if r.get("company_about")),
        "placeholders": placeholders,
        "placeholder_fields": placeholder_fields,
    }
    refresh_upload_metadata(payload)
    return payload


def upload_payload(data: dict) -> dict:
    """JSON-serializable upload snapshot for the client."""
    return {
        "ok": True,
        "rows": data.get("rows") or [],
        "total_rows_in_file": data.get("total_rows_in_file", 0),
        "total_valid_emails": data.get("total_valid_emails", 0),
        "selected_count": data.get("selected_count", 0),
        "truncated": data.get("truncated", False),
        "detected_columns": data.get("detected_columns") or {},
        "sheet_about_count": data.get("sheet_about_count", 0),
        "placeholders": data.get("placeholders") or [],
        "placeholder_fields": data.get("placeholder_fields") or {},
        "company_fill": data.get("company_fill") or {},
        "company_names_filled": bool(data.get("company_names_filled")),
        "company_fill_stats": data.get("company_fill_stats") or {},
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


def company_fill_summary(rows: list[dict], company_column: str | None) -> dict:
    empty_count = sum(1 for row in rows if not str(row.get("company_name") or "").strip())
    inferrable_count = 0
    for row in rows:
        if str(row.get("company_name") or "").strip():
            continue
        inferred = resolve_company_for_row(row)
        if str(inferred.get("company_name") or "").strip():
            inferrable_count += 1
    return {
        "needs_fill": not company_column or empty_count > 0,
        "empty_count": empty_count,
        "missing_column": not bool(company_column),
        "inferrable_count": inferrable_count,
    }


def rows_needing_company_fill(rows: list[dict]) -> list[int]:
    return [
        index
        for index, row in enumerate(rows)
        if not str(row.get("company_name") or "").strip()
    ]


def refresh_upload_metadata(data: dict) -> None:
    """Recompute placeholders and company-fill stats after rows or columns change."""
    cols = data.setdefault("detected_columns", {})
    rows = data.get("rows") or []

    if not cols.get("company_name"):
        if any(str(row.get("company_name") or "").strip() for row in rows):
            cols["company_name"] = "Company"

    placeholders = []
    if cols.get("person_name"):
        placeholders.append(
            {
                "token": PLACEHOLDER_PERSON,
                "label": "Person name",
                "description": "Inserts the contact's name from your sheet",
            }
        )
    if cols.get("company_name"):
        placeholders.append(
            {
                "token": PLACEHOLDER_COMPANY,
                "label": "Company name",
                "description": "Inserts the company name from your sheet",
            }
        )

    data["placeholders"] = placeholders
    data["placeholder_fields"] = {
        "person_name": _field_meta(cols.get("person_name"), rows, "person_name", PLACEHOLDER_PERSON),
        "company_name": _field_meta(cols.get("company_name"), rows, "company_name", PLACEHOLDER_COMPANY),
    }
    data["sheet_about_count"] = sum(1 for row in rows if str(row.get("company_about") or "").strip())
    data["company_fill"] = company_fill_summary(rows, cols.get("company_name"))


def fill_company_names(rows: list[dict]) -> tuple[list[dict], dict]:
    """Fill blank company names from email domains / website columns."""
    updated: list[dict] = []
    stats = {
        "filled": 0,
        "already_set": 0,
        "unresolved": 0,
        "processed": 0,
    }

    for row in rows:
        stats["processed"] += 1
        item = dict(row)
        if str(item.get("company_name") or "").strip():
            stats["already_set"] += 1
            updated.append(item)
            continue

        inferred = resolve_company_for_row(item)
        company = str(inferred.get("company_name") or "").strip()
        if company:
            item["company_name"] = company
            item["company_name_source"] = inferred.get("company_name_source", "email_domain")
            item["company_name_missing"] = False
            stats["filled"] += 1
        else:
            item["company_name_missing"] = True
            stats["unresolved"] += 1
        updated.append(item)

    return updated, stats


EXPORT_COLUMNS = (
    ("email", "Email"),
    ("person_name", "Name"),
    ("company_name", "Company"),
    ("company_website", "Website"),
    ("company_about", "Company about"),
)


def export_spreadsheet(data: dict, fmt: str = "xlsx") -> tuple[bytes, str, str]:
    """Build a downloadable spreadsheet from session upload data."""
    cols = data.get("detected_columns") or {}
    rows = data.get("rows") or []

    headers: list[str] = []
    fields: list[str] = []
    for field, default_header in EXPORT_COLUMNS:
        header = cols.get(field) or default_header
        include = field == "email"
        if not include:
            if cols.get(field):
                include = True
            elif field == "company_name" and data.get("company_names_filled"):
                include = True
            elif any(str(row.get(field) or "").strip() for row in rows):
                include = True
        if include:
            headers.append(header)
            fields.append(field)

    if not fields:
        raise ValueError("Nothing to export.")

    records = [{headers[i]: row.get(fields[i], "") for i in range(len(fields))} for row in rows]
    frame = pd.DataFrame(records, columns=headers)

    stem = (data.get("original_filename") or "contacts").rsplit(".", 1)[0]
    if fmt == "csv":
        return frame.to_csv(index=False).encode("utf-8"), "text/csv", f"{stem}-with-companies.csv"

    buffer = BytesIO()
    frame.to_excel(buffer, index=False, engine="openpyxl")
    mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return buffer.getvalue(), mime, f"{stem}-with-companies.xlsx"
