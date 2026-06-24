"""Localhost web UI for Email Hunter outreach."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from io import BytesIO
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file, session
from werkzeug.utils import secure_filename

from company_resolver import enrich_rows, resolve_company_for_row
from company_scraper import fetch_or_use_sheet_about, unique_companies
from gemini_client import (
    GenerationCancelled,
    GeminiQuotaExhausted,
    GEMINI_MODEL,
    USE_EXPLICIT_CACHE,
    configure_generation_logging,
    countdown_wait,
    create_outreach_cache,
    delete_outreach_cache,
    generate_outreach_email as generate_outreach_email_gemini,
    reset_rate_limit_tracking as reset_gemini_rate_limit_tracking,
    verify_api_key as verify_gemini_api_key,
)
from groq_client import (
    GROQ_DAILY_USER_MESSAGE,
    GROQ_MODEL,
    GroqQuotaExhausted,
    daily_exhaustion_record,
    generate_outreach_email as generate_outreach_email_groq,
    groq_block_from_stored,
    looks_like_groq_daily_error,
    record_groq_daily_exhaustion,
    reset_rate_limit_tracking as reset_groq_rate_limit_tracking,
    verify_api_key as verify_groq_api_key,
)
from multi_agent import OrchestrationAgentError, generate_outreach_orchestrated, should_orchestrate

AI_PROVIDERS = frozenset({"gemini", "groq"})
QUOTA_EXPIRED_MESSAGE = "Your daily quota is expired. Review the emails generated so far."


def _normalize_ai_provider(provider: str | None) -> str:
    value = (provider or "groq").strip().lower()
    return value if value in AI_PROVIDERS else "groq"


def _provider_model(provider: str) -> str:
    return GROQ_MODEL if provider == "groq" else GEMINI_MODEL


def _reset_provider_tracking(provider: str) -> None:
    if provider == "groq":
        reset_groq_rate_limit_tracking()
    else:
        reset_gemini_rate_limit_tracking()


def _verify_provider_key(provider: str, api_key: str) -> None:
    if provider == "groq":
        verify_groq_api_key(api_key)
    else:
        verify_gemini_api_key(api_key)


def _generate_outreach_email(
    provider: str,
    api_key: str,
    resume_text: str,
    company_name: str,
    company_about: str,
    about_found: bool,
    portfolio_url: str | None = None,
    *,
    person_name: str = "",
    company_name_missing: bool = False,
    company_name_source: str = "sheet",
    cache_name: str | None = None,
    on_status=None,
    cancel_check=None,
    multi_agent: bool = False,
    secondary_api_key: str | None = None,
) -> dict[str, str]:
    if should_orchestrate(
        multi_agent=multi_agent,
        about_found=about_found,
        company_name_missing=company_name_missing,
        company_name=company_name,
    ):
        align_key = api_key
        write_key = (secondary_api_key or "").strip() or api_key
        return generate_outreach_orchestrated(
            provider,
            align_key,
            write_key,
            resume_text,
            company_name,
            company_about,
            about_found,
            portfolio_url,
            person_name=person_name,
            company_name_missing=company_name_missing,
            company_name_source=company_name_source,
            on_status=on_status,
            cancel_check=cancel_check,
        )
    if provider == "groq":
        return generate_outreach_email_groq(
            api_key,
            resume_text,
            company_name,
            company_about,
            about_found,
            portfolio_url,
            person_name=person_name,
            company_name_missing=company_name_missing,
            company_name_source=company_name_source,
            cache_name=cache_name,
            on_status=on_status,
            cancel_check=cancel_check,
        )
    return generate_outreach_email_gemini(
        api_key,
        resume_text,
        company_name,
        company_about,
        about_found,
        portfolio_url,
        person_name=person_name,
        company_name_missing=company_name_missing,
        company_name_source=company_name_source,
        cache_name=cache_name,
        on_status=on_status,
        cancel_check=cancel_check,
    )


def _store_provider_key(provider: str, api_key: str) -> None:
    if provider == "groq":
        session["groq_api_key"] = api_key
    else:
        session["gemini_api_key"] = api_key
    session["ai_provider"] = provider
    session.modified = True


def _provider_api_key(provider: str) -> str:
    if provider == "groq":
        return (session.get("groq_api_key") or "").strip()
    return (session.get("gemini_api_key") or "").strip()


def _secondary_api_key(provider: str) -> str:
    stored = session.get("llm_api_key_secondary") or {}
    if not isinstance(stored, dict):
        return ""
    if stored.get("provider") != provider:
        return ""
    return (stored.get("api_key") or "").strip()


def _store_secondary_key(provider: str, api_key: str) -> None:
    key = api_key.strip()
    if not key:
        session.pop("llm_api_key_secondary", None)
    else:
        session["llm_api_key_secondary"] = {"provider": provider, "api_key": key}
    session.modified = True


def _draft_stats(ctx: dict) -> dict[str, int]:
    drafts = ctx.get("drafts") or {}
    generated_ok = sum(1 for item in drafts.values() if item.get("ok"))
    processed = sum(
        1 for item in drafts.values() if item.get("ok") or item.get("llm_processed")
    )
    return {"generated_ok": generated_ok, "drafts_ready": processed}


def _remaining_row_count(ctx: dict, upload_data: dict | None) -> int:
    rows = (upload_data or {}).get("rows") or []
    drafts = ctx.get("drafts") or {}
    ok_emails = {email for email, draft in drafts.items() if (draft or {}).get("ok")}
    ok_lower = {email.lower() for email in ok_emails}
    return sum(
        1
        for row in rows
        if (row.get("email") or "").lower() not in ok_lower
    )


def _generation_status_payload(job_id: str | None, context_id: str | None) -> dict:
    ctx = _load_ai_context_by_id(context_id) if context_id else _load_ai_context()
    snapshot = dict(_gen_snapshot(job_id, context_id) if job_id else (ctx.get("generation") or {}))
    if not snapshot:
        snapshot = dict(_default_ai_context()["generation"])
    stats = _draft_stats(ctx)
    snapshot.update(stats)
    snapshot["quota_exhausted"] = bool(
        snapshot.get("quota_exhausted") or (ctx.get("generation") or {}).get("quota_exhausted")
    )
    snapshot["agent_abort"] = bool(
        snapshot.get("agent_abort") or (ctx.get("generation") or {}).get("agent_abort")
    )
    if not snapshot.get("error"):
        snapshot["error"] = (ctx.get("generation") or {}).get("error") or ""
    if not snapshot.get("quota_message"):
        snapshot["quota_message"] = (ctx.get("generation") or {}).get("quota_message") or ""
    upload_data = _load_session_data()
    snapshot["remaining_count"] = _remaining_row_count(ctx, upload_data)
    provider = ctx.get("ai_provider") or session.get("ai_provider") or "groq"
    groq_block = None
    if provider == "groq":
        pq = (ctx.get("provider_quota") or {}).get("groq")
        groq_block = groq_block_from_stored(pq)
    snapshot["groq_daily_blocked"] = bool(groq_block and groq_block.get("blocked"))
    if groq_block:
        snapshot["groq_hours_until_retry"] = groq_block.get("hours_until_retry")
        snapshot["groq_block_message"] = groq_block.get("message")
    return snapshot


def _persist_groq_daily_quota(context_id: str, record: dict | None) -> None:
    if not record:
        return
    ctx = _load_ai_context_by_id(context_id)
    ctx.setdefault("provider_quota", {})["groq"] = record
    _save_ai_context_by_id(context_id, ctx)


def _groq_generate_block(ctx: dict) -> dict | None:
    pq = (ctx.get("provider_quota") or {}).get("groq")
    return groq_block_from_stored(pq)


def _mark_skipped_drafts(
    context_id: str,
    job_id: str,
    rows: list[dict],
    start_index: int,
    message: str,
) -> None:
    ctx = _load_ai_context_by_id(context_id)
    for row in rows[start_index:]:
        email = row.get("email", "")
        ctx.setdefault("drafts", {})[email] = {
            "subject": "",
            "body": "",
            "ok": False,
            "error": message,
            "skipped": True,
            "llm_processed": True,
        }
    failed = int(ctx["generation"].get("failed", 0)) + max(len(rows) - start_index, 0)
    ctx["generation"]["failed"] = failed
    ctx["generation"]["completed"] = len(rows)
    ctx["generation"]["quota_exhausted"] = True
    ctx["generation"]["quota_message"] = message
    ctx["generation"]["status_note"] = message
    ctx["generation"]["generated_ok"] = _draft_stats(ctx)["generated_ok"]
    _save_ai_context_by_id(context_id, ctx)
    with _gen_lock:
        if job_id in _gen_jobs:
            _gen_jobs[job_id].update(
                {
                    "failed": failed,
                    "completed": len(rows),
                    "quota_exhausted": True,
                    "quota_message": message,
                    "status_note": message,
                    "generated_ok": ctx["generation"]["generated_ok"],
                }
            )
from email_sender import (
    DAILY_LIMIT,
    append_sent_log,
    load_daily_state,
    load_settings,
    remaining_today,
    save_settings,
    send_batch,
    send_single,
    test_gmail_connection,
    test_mail_connection,
)
from excel_parser import (
    MAX_RECIPIENTS,
    available_tokens,
    export_remaining_spreadsheet,
    export_spreadsheet,
    parse_excel,
    refresh_upload_metadata,
    render_template as render_email_template,
    rows_needing_company_fill,
    upload_payload,
    validate_empty_fallbacks,
    validate_template,
)
from resume_parser import extract_resume_text

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).expanduser()
UPLOAD_DIR = DATA_DIR / "uploads"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
AI_CONTEXT_DIR = DATA_DIR / "ai_context"
SECRET_FILE = DATA_DIR / ".flask_secret"

ALLOWED_SHEET_EXT = {".xlsx", ".xls", ".xlsm", ".csv"}
ALLOWED_ATTACHMENT_EXT = {".pdf", ".doc", ".docx"}
ALLOWED_RESUME_EXT = {".pdf", ".doc", ".docx"}
ATTACHMENT_KEYWORDS = re.compile(r"\battach\w*\b", re.IGNORECASE)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
configure_generation_logging()
pipeline_log = logging.getLogger("email_hunter.pipeline")

_send_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

_campaigns: dict[str, dict] = {}
_campaign_lock = threading.Lock()

_gen_jobs: dict[str, dict] = {}
_gen_lock = threading.Lock()

_regen_jobs: dict[str, dict] = {}
_regen_lock = threading.Lock()

_fill_jobs: dict[str, dict] = {}
_fill_lock = threading.Lock()
SCRAPE_DELAY = 0.6
FILL_COMPANY_DELAY = 0.45


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    ATTACHMENTS_DIR.mkdir(exist_ok=True)
    AI_CONTEXT_DIR.mkdir(exist_ok=True)


def _is_production() -> bool:
    return os.environ.get("FLASK_ENV", "").lower() == "production" or bool(
        os.environ.get("RENDER") or os.environ.get("RAILWAY_ENVIRONMENT")
    )


def _get_secret() -> str:
    env_secret = os.environ.get("FLASK_SECRET_KEY", "").strip()
    if env_secret:
        return env_secret
    _ensure_dirs()
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text(encoding="utf-8").strip()
    secret = uuid.uuid4().hex
    SECRET_FILE.write_text(secret, encoding="utf-8")
    return secret


app.secret_key = _get_secret()
if _is_production():
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

if os.environ.get("BEHIND_PROXY", "1" if _is_production() else "0").lower() in {
    "1",
    "true",
    "yes",
}:
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def mentions_attachment(subject: str, body: str) -> bool:
    return bool(ATTACHMENT_KEYWORDS.search(f"{subject} {body}"))


def _session_upload_path() -> Path | None:
    upload_id = session.get("upload_id")
    if not upload_id:
        return None
    path = UPLOAD_DIR / f"{upload_id}.json"
    return path if path.exists() else None


def _load_session_data_by_id(upload_id: str) -> dict | None:
    path = UPLOAD_DIR / f"{upload_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _ai_context_file(context_id: str) -> Path:
    return AI_CONTEXT_DIR / f"{context_id}.json"


def _load_ai_context_by_id(context_id: str) -> dict:
    path = _ai_context_file(context_id)
    if not path.exists():
        return _default_ai_context()
    data = json.loads(path.read_text(encoding="utf-8"))
    merged = _default_ai_context()
    merged.update(data)
    return merged


def _save_ai_context_by_id(context_id: str, data: dict) -> None:
    _ai_context_file(context_id).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_session_data() -> dict | None:
    path = _session_upload_path()
    if not path:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save_session_data_by_id(upload_id: str, data: dict) -> None:
    _ensure_dirs()
    path = UPLOAD_DIR / f"{upload_id}.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _save_session_data(data: dict) -> None:
    _ensure_dirs()
    upload_id = session.get("upload_id")
    if not upload_id:
        upload_id = uuid.uuid4().hex
        session["upload_id"] = upload_id
        session.modified = True
    _save_session_data_by_id(upload_id, data)


def _attachment_session_dir() -> Path:
    _ensure_dirs()
    session_id = session.get("attachment_session_id")
    if not session_id:
        session_id = uuid.uuid4().hex
        session["attachment_session_id"] = session_id
    path = ATTACHMENTS_DIR / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _attachment_manifest_path() -> Path:
    return _attachment_session_dir() / "manifest.json"


def _load_attachment_manifest() -> dict:
    path = _attachment_manifest_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_attachment_manifest(manifest: dict) -> None:
    _attachment_manifest_path().write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _save_attachment_file(file) -> tuple[bool, str]:
    if not file.filename:
        return False, "No file selected."

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_ATTACHMENT_EXT:
        return False, "Upload a PDF or Word file (.pdf, .doc, .docx)."

    original_name = secure_filename(file.filename)
    stored_name = f"attachment{ext}"
    dest = _attachment_session_dir() / stored_name
    file.save(dest)

    manifest = {"file": {"stored_name": stored_name, "original_name": original_name}}
    _save_attachment_manifest(manifest)
    return True, original_name


def _get_attachment_file() -> tuple[Path, str] | None:
    manifest = _load_attachment_manifest()
    entry = manifest.get("file")
    if not entry:
        for legacy in ("resume", "document"):
            entry = manifest.get(legacy)
            if entry:
                break
    if not entry:
        return None
    path = _attachment_session_dir() / entry["stored_name"]
    if not path.exists():
        return None
    return path, entry["original_name"]


def _attachment_status() -> dict:
    entry = _get_attachment_file()
    return {
        "has_attachment": entry is not None,
        "attachment_name": entry[1] if entry else None,
    }


def _store_mail_credentials(provider: str, email_address: str, app_password: str) -> None:
    mail_provider = (provider or "gmail").strip().lower()
    if mail_provider not in {"gmail", "outlook"}:
        mail_provider = "gmail"
    session["mail_app_password"] = app_password.strip()
    session["mail_provider"] = mail_provider
    session["mail_email_address"] = email_address.strip()
    session.modified = True


def _smtp_settings() -> dict:
    settings = load_settings()
    if session.get("mail_provider"):
        settings["mail_provider"] = session["mail_provider"]
    if session.get("mail_email_address"):
        settings["email_address"] = session["mail_email_address"]
    settings["app_password"] = (session.get("mail_app_password") or "").strip()
    settings["gmail_address"] = settings["email_address"]
    settings["gmail_app_password"] = settings["app_password"]
    return settings


def _is_gmail_verified() -> bool:
    return bool(session.get("gmail_verified"))


def _require_gmail_verified():
    if not _is_gmail_verified():
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Connect and verify your email account before continuing.",
                    "code": "mail_required",
                }
            ),
            401,
        )
    return None


def _clear_session_data() -> None:
    upload_path = _session_upload_path()
    if upload_path:
        upload_path.unlink(missing_ok=True)

    attach_id = session.get("attachment_session_id")
    if attach_id:
        attach_dir = ATTACHMENTS_DIR / attach_id
        if attach_dir.exists():
            shutil.rmtree(attach_dir, ignore_errors=True)

    ai_id = session.get("ai_context_id")
    if ai_id:
        (AI_CONTEXT_DIR / f"{ai_id}.json").unlink(missing_ok=True)
        for path in AI_CONTEXT_DIR.glob(f"{ai_id}_resume.*"):
            path.unlink(missing_ok=True)

    gen_job_id = session.get("ai_gen_job_id")
    if gen_job_id:
        with _gen_lock:
            _gen_jobs.pop(gen_job_id, None)

    session.pop("upload_id", None)
    session.pop("upload_fill_job_id", None)
    session.pop("attachment_session_id", None)
    session.pop("ai_context_id", None)
    session.pop("gemini_api_key", None)
    session.pop("groq_api_key", None)
    session.pop("ai_provider", None)
    session.pop("llm_api_key_secondary", None)
    session.pop("ai_gen_job_id", None)
    session.pop("mail_app_password", None)
    session.pop("mail_email_address", None)
    session.pop("gmail_verified", None)
    session.pop("mail_provider", None)
    campaign_id = session.pop("campaign_id", None)
    if campaign_id:
        with _campaign_lock:
            _campaigns.pop(campaign_id, None)

    session.modified = True


def _ai_context_path() -> Path:
    _ensure_dirs()
    context_id = session.get("ai_context_id")
    if not context_id:
        context_id = uuid.uuid4().hex
        session["ai_context_id"] = context_id
        session.modified = True
    return AI_CONTEXT_DIR / f"{context_id}.json"


def _attach_resume_copy(source: Path, original_name: str, ext: str) -> None:
    stored_name = f"attachment{ext}"
    dest = _attachment_session_dir() / stored_name
    shutil.copy(source, dest)
    _save_attachment_manifest({"file": {"stored_name": stored_name, "original_name": original_name}})


def _default_ai_context() -> dict:
    return {
        "compose_mode": None,
        "resume_filename": None,
        "resume_text": None,
        "portfolio_url": None,
        "companies_fetched": False,
        "companies": [],
        "drafts": {},
        "ai_provider": None,
        "multi_agent": False,
        "generation": {
            "status": "idle",
            "phase": "idle",
            "scrape_total": 0,
            "scrape_completed": 0,
            "total": 0,
            "completed": 0,
            "failed": 0,
            "generated_ok": 0,
            "current": "",
            "current_company": "",
            "status_note": "",
            "cancelled": False,
            "quota_exhausted": False,
            "agent_abort": False,
            "error": "",
        },
    }


def _company_about_for(company_name: str, companies: list[dict]) -> tuple[str, bool]:
    key = company_name.strip().lower()
    for item in companies:
        if item.get("company_name", "").strip().lower() != key:
            continue
        if item.get("found") and item.get("text"):
            return str(item["text"]), True
        return str(item.get("message") or ""), False
    if company_name.strip():
        return "", False
    return "", False


def _about_for_recipient(row: dict, profiles: list[dict]) -> tuple[str, bool]:
    """Prefer per-row spreadsheet about, then scraped/shared profile."""
    row_about = str(row.get("company_about") or "").strip()
    if row_about:
        return row_about, True
    company = str(row.get("company_name") or "").strip()
    return _company_about_for(company, profiles)


def _get_gen_job_id() -> str:
    job_id = session.get("ai_gen_job_id")
    if not job_id:
        job_id = uuid.uuid4().hex
        session["ai_gen_job_id"] = job_id
        session.modified = True
    return job_id


def _gen_snapshot(job_id: str, context_id: str | None = None) -> dict:
    with _gen_lock:
        job = _gen_jobs.get(job_id)
    if job:
        return {
            "status": job.get("status", "idle"),
            "phase": job.get("phase", "idle"),
            "scrape_total": job.get("scrape_total", 0),
            "scrape_completed": job.get("scrape_completed", 0),
            "total": job.get("total", 0),
            "completed": job.get("completed", 0),
            "failed": job.get("failed", 0),
            "current": job.get("current", ""),
            "current_company": job.get("current_company", ""),
            "status_note": job.get("status_note", ""),
            "cancelled": job.get("cancelled", False),
            "quota_exhausted": job.get("quota_exhausted", False),
            "agent_abort": job.get("agent_abort", False),
            "generated_ok": job.get("generated_ok", 0),
            "error": job.get("error", ""),
        }
    if context_id:
        ctx = _load_ai_context_by_id(context_id)
        return ctx.get("generation") or _default_ai_context()["generation"]
    ctx = _load_ai_context()
    return ctx.get("generation") or _default_ai_context()["generation"]


def _is_pipeline_cancelled(job_id: str, context_id: str) -> bool:
    with _gen_lock:
        job = _gen_jobs.get(job_id)
        if job and job.get("cancelled"):
            return True
    ctx = _load_ai_context_by_id(context_id)
    return bool((ctx.get("generation") or {}).get("cancelled"))


def _raise_if_cancelled(job_id: str, context_id: str) -> None:
    if _is_pipeline_cancelled(job_id, context_id):
        raise GenerationCancelled()


def _sleep_cancellable(seconds: float, job_id: str, context_id: str) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        _raise_if_cancelled(job_id, context_id)
        time.sleep(min(0.25, end - time.monotonic()))


def _patch_gen_status(job_id: str, context_id: str, **fields) -> None:
    with _gen_lock:
        if job_id in _gen_jobs:
            _gen_jobs[job_id].update(fields)
    ctx = _load_ai_context_by_id(context_id)
    ctx["generation"].update(fields)
    _save_ai_context_by_id(context_id, ctx)



def _run_ai_pipeline(
    job_id: str,
    api_key: str,
    upload_id: str,
    context_id: str,
    provider: str,
    secondary_api_key: str | None = None,
) -> None:
    try:
        _run_ai_pipeline_inner(
            job_id, api_key, upload_id, context_id, provider, secondary_api_key
        )
    except GenerationCancelled:
        pipeline_log.warning("PIPELINE CANCELLED job=%s", job_id)
        ctx = _load_ai_context_by_id(context_id)
        generated_ok = _draft_stats(ctx)["generated_ok"]
        ctx["generation"]["status"] = "cancelled"
        ctx["generation"]["phase"] = "cancelled"
        ctx["generation"]["status_note"] = "Cancelled."
        ctx["generation"]["generated_ok"] = generated_ok
        ctx["generation"]["current"] = ""
        ctx["generation"]["current_company"] = ""
        _save_ai_context_by_id(context_id, ctx)
        with _gen_lock:
            if job_id in _gen_jobs:
                _gen_jobs[job_id]["status"] = "cancelled"
                _gen_jobs[job_id]["phase"] = "cancelled"
                _gen_jobs[job_id]["status_note"] = "Cancelled."
                _gen_jobs[job_id]["generated_ok"] = generated_ok
                _gen_jobs[job_id]["current"] = ""
                _gen_jobs[job_id]["current_company"] = ""
    except GroqQuotaExhausted as exc:
        pipeline_log.error("PIPELINE GROQ QUOTA job=%s: %s", job_id, exc)
        ctx = _load_ai_context_by_id(context_id)
        record = daily_exhaustion_record() or record_groq_daily_exhaustion(
            getattr(exc, "hint", str(exc)), kind=getattr(exc, "kind", "daily")
        )
        _persist_groq_daily_quota(context_id, record)
        generated_ok = _draft_stats(ctx)["generated_ok"]
        msg = str(exc)
        ctx["generation"]["status"] = "done"
        ctx["generation"]["phase"] = "done"
        ctx["generation"]["quota_exhausted"] = True
        ctx["generation"]["quota_message"] = msg
        ctx["generation"]["error"] = msg
        ctx["generation"]["status_note"] = msg
        ctx["generation"]["generated_ok"] = generated_ok
        _save_ai_context_by_id(context_id, ctx)
        with _gen_lock:
            if job_id in _gen_jobs:
                _gen_jobs[job_id].update(
                    {
                        "status": "done",
                        "phase": "done",
                        "quota_exhausted": True,
                        "quota_message": msg,
                        "error": msg,
                        "status_note": msg,
                        "generated_ok": generated_ok,
                    }
                )
    except GeminiQuotaExhausted as exc:
        pipeline_log.error("PIPELINE GEMINI QUOTA job=%s: %s", job_id, exc)
        ctx = _load_ai_context_by_id(context_id)
        generated_ok = _draft_stats(ctx)["generated_ok"]
        msg = str(exc)
        ctx["generation"]["status"] = "done"
        ctx["generation"]["phase"] = "done"
        ctx["generation"]["quota_exhausted"] = True
        ctx["generation"]["quota_message"] = msg
        ctx["generation"]["error"] = msg
        ctx["generation"]["status_note"] = msg
        ctx["generation"]["generated_ok"] = generated_ok
        _save_ai_context_by_id(context_id, ctx)
        with _gen_lock:
            if job_id in _gen_jobs:
                _gen_jobs[job_id].update(
                    {
                        "status": "done",
                        "phase": "done",
                        "quota_exhausted": True,
                        "quota_message": msg,
                        "error": msg,
                        "status_note": msg,
                        "generated_ok": generated_ok,
                    }
                )
    except Exception as exc:
        pipeline_log.exception("PIPELINE ERROR job=%s: %s", job_id, exc)
        ctx = _load_ai_context_by_id(context_id)
        generated_ok = _draft_stats(ctx)["generated_ok"]
        ctx["generation"]["status"] = "error"
        ctx["generation"]["error"] = str(exc)
        ctx["generation"]["generated_ok"] = generated_ok
        _save_ai_context_by_id(context_id, ctx)
        with _gen_lock:
            if job_id in _gen_jobs:
                _gen_jobs[job_id]["status"] = "error"
                _gen_jobs[job_id]["error"] = str(exc)
                _gen_jobs[job_id]["generated_ok"] = generated_ok


def _run_ai_pipeline_inner(
    job_id: str,
    api_key: str,
    upload_id: str,
    context_id: str,
    provider: str,
    secondary_api_key: str | None = None,
) -> None:
    provider = _normalize_ai_provider(provider)
    _reset_provider_tracking(provider)
    data = _load_session_data_by_id(upload_id)
    ctx = _load_ai_context_by_id(context_id)
    ctx["ai_provider"] = provider
    multi_agent = bool(ctx.get("multi_agent"))
    _save_ai_context_by_id(context_id, ctx)
    rows = enrich_rows(data["rows"] if data else [])
    resume = ctx.get("resume_text") or ""
    portfolio_url = (ctx.get("portfolio_url") or "").strip() or None
    companies_list = unique_companies(rows)
    scrape_total = len(companies_list)

    pipeline_log.info("=" * 72)
    pipeline_log.info(
        "PIPELINE START job=%s context=%s provider=%s | recipients=%s companies=%s | resume_chars=%s | explicit_cache=%s | model=%s",
        job_id,
        context_id,
        provider,
        len(rows),
        scrape_total,
        len(resume),
        USE_EXPLICIT_CACHE and provider == "gemini",
        _provider_model(provider),
    )
    pipeline_log.info("portfolio=%s", portfolio_url or "(none)")
    pipeline_log.info("recipients: %s", [r.get("email") for r in rows])

    generation = {
        "status": "running",
        "phase": "scraping",
        "scrape_total": scrape_total,
        "scrape_completed": 0,
        "total": len(rows),
        "completed": 0,
        "failed": 0,
        "current": "",
        "current_company": "",
        "status_note": "",
        "cancelled": False,
        "quota_exhausted": False,
        "agent_abort": False,
        "generated_ok": 0,
        "error": "",
    }

    ctx["drafts"] = {}
    ctx["companies"] = []
    ctx["companies_fetched"] = False
    ctx["generation"] = generation
    _save_ai_context_by_id(context_id, ctx)

    with _gen_lock:
        _gen_jobs[job_id] = dict(generation)

    profiles: list[dict] = []
    for index, entry in enumerate(companies_list):
        _raise_if_cancelled(job_id, context_id)
        if index:
            _sleep_cancellable(SCRAPE_DELAY, job_id, context_id)
        company_name = entry["company_name"]
        pipeline_log.info(
            "SCRAPE %s/%s company=%r website=%r",
            index + 1,
            scrape_total,
            company_name,
            entry.get("company_website"),
        )
        with _gen_lock:
            if job_id in _gen_jobs:
                _gen_jobs[job_id]["current_company"] = company_name

        ctx = _load_ai_context_by_id(context_id)
        ctx["generation"]["current_company"] = company_name
        _save_ai_context_by_id(context_id, ctx)

        profiles.append(
            fetch_or_use_sheet_about(
                company_name,
                entry.get("company_website") or None,
                rows,
            )
        )
        profile = profiles[-1]
        pipeline_log.info(
            "SCRAPE DONE %s | found=%s | source=%s | chars=%s",
            company_name,
            bool(profile.get("found")),
            profile.get("source_url") or "none",
            len(profile.get("text") or ""),
        )
        scrape_completed = index + 1
        with _gen_lock:
            if job_id in _gen_jobs:
                _gen_jobs[job_id]["scrape_completed"] = scrape_completed

        ctx = _load_ai_context_by_id(context_id)
        ctx["companies"] = profiles
        ctx["generation"]["scrape_completed"] = scrape_completed
        ctx["generation"]["current_company"] = company_name
        _save_ai_context_by_id(context_id, ctx)

    _raise_if_cancelled(job_id, context_id)

    ctx = _load_ai_context_by_id(context_id)
    ctx["companies_fetched"] = True
    ctx["companies"] = profiles
    ctx["generation"]["phase"] = "writing"
    ctx["generation"]["current_company"] = ""
    ctx["generation"]["status_note"] = "Starting email generation…"
    _save_ai_context_by_id(context_id, ctx)
    with _gen_lock:
        if job_id in _gen_jobs:
            _gen_jobs[job_id]["phase"] = "writing"
            _gen_jobs[job_id]["current_company"] = ""
            _gen_jobs[job_id]["status_note"] = "Starting email generation…"

    pipeline_log.info("SCRAPING COMPLETE — starting writing phase for %s emails", len(rows))

    def on_status(message: str) -> None:
        pipeline_log.info("STATUS: %s", message)
        _patch_gen_status(job_id, context_id, status_note=message)

    def cancel_check() -> bool:
        return _is_pipeline_cancelled(job_id, context_id)

    cache_name: str | None = None
    try:
        if USE_EXPLICIT_CACHE and provider == "gemini" and not multi_agent:
            pipeline_log.info("Creating explicit context cache…")
            cache_name = create_outreach_cache(
                api_key,
                resume,
                portfolio_url,
                on_status,
                cancel_check=cancel_check,
            )
        else:
            if multi_agent:
                on_status("Multi-agent mode — alignment agent, then writing agent…")
            else:
                provider_label = "Groq" if provider == "groq" else "Gemini"
                pipeline_log.info("%s — sending full prompt (resume+rules+task) per email", provider_label)
                on_status(f"Preparing email generation with {provider_label}…")

        quota_abort = False
        agent_abort = False
        agent_abort_message = ""
        quota_message = GROQ_DAILY_USER_MESSAGE if provider == "groq" else QUOTA_EXPIRED_MESSAGE

        for index, row in enumerate(rows):
            _raise_if_cancelled(job_id, context_id)
            if agent_abort:
                break
            email = row.get("email", "")

            company = row.get("company_name", "")
            pipeline_log.info(
                "EMAIL %s/%s recipient=%r company=%r source=%s",
                index + 1,
                len(rows),
                email,
                company,
                row.get("company_name_source") or "sheet",
            )
            with _gen_lock:
                if job_id in _gen_jobs:
                    _gen_jobs[job_id]["current"] = email

            _patch_gen_status(
                job_id,
                context_id,
                current=email,
                status_note=f"Writing email for {email}…",
            )

            about_text, about_found = _about_for_recipient(row, profiles)
            pipeline_log.info(
                "EMAIL %s context | about_found=%s about_chars=%s sheet_about=%s company_name_missing=%s",
                index + 1,
                about_found,
                len(about_text),
                bool(str(row.get("company_about") or "").strip()),
                bool(row.get("company_name_missing")),
            )
            draft = None
            last_exc: Exception | None = None

            for email_attempt in range(2):
                if email_attempt:
                    pipeline_log.warning(
                        "EMAIL %s pipeline retry attempt %s/2 after error",
                        index + 1,
                        email_attempt + 1,
                    )
                try:
                    draft = _generate_outreach_email(
                        provider,
                        api_key,
                        resume,
                        company,
                        about_text,
                        about_found,
                        portfolio_url,
                        person_name=str(row.get("person_name") or ""),
                        company_name_missing=bool(row.get("company_name_missing")),
                        company_name_source=str(row.get("company_name_source") or "sheet"),
                        cache_name=cache_name,
                        on_status=on_status,
                        cancel_check=cancel_check,
                        multi_agent=multi_agent,
                        secondary_api_key=secondary_api_key,
                    )
                    break
                except GenerationCancelled:
                    raise
                except OrchestrationAgentError as exc:
                    last_exc = exc
                    if provider == "groq" and looks_like_groq_daily_error(str(exc)):
                        quota_abort = True
                        quota_message = GROQ_DAILY_USER_MESSAGE
                        _persist_groq_daily_quota(
                            context_id,
                            record_groq_daily_exhaustion(str(exc)),
                        )
                        pipeline_log.error("GROQ QUOTA (orchestration) at email %s", index + 1)
                        break
                    agent_abort = True
                    agent_abort_message = str(exc)
                    pipeline_log.error("ORCHESTRATION ABORT at email %s: %s", index + 1, exc)
                    break
                except (GeminiQuotaExhausted, GroqQuotaExhausted) as exc:
                    last_exc = exc
                    quota_abort = True
                    quota_message = str(exc)
                    if isinstance(exc, GroqQuotaExhausted):
                        record = daily_exhaustion_record() or record_groq_daily_exhaustion(
                            getattr(exc, "hint", quota_message),
                            kind=getattr(exc, "kind", "daily"),
                        )
                        _persist_groq_daily_quota(context_id, record)
                    pipeline_log.error("QUOTA EXHAUSTED at email %s — stopping pipeline", index + 1)
                    break
                except Exception as exc:
                    last_exc = exc
                    if provider == "groq" and looks_like_groq_daily_error(str(exc)):
                        quota_abort = True
                        quota_message = GROQ_DAILY_USER_MESSAGE
                        _persist_groq_daily_quota(
                            context_id,
                            record_groq_daily_exhaustion(str(exc)),
                        )
                    pipeline_log.error("EMAIL %s attempt %s failed: %s", index + 1, email_attempt + 1, exc)
                    break

            if draft:
                pipeline_log.info(
                    "EMAIL %s/%s OK | subject=%r | body_words≈%s",
                    index + 1,
                    len(rows),
                    draft["subject"][:60],
                    len(draft["body"].split()),
                )
                ctx = _load_ai_context_by_id(context_id)
                ctx.setdefault("drafts", {})[email] = {
                    "subject": draft["subject"],
                    "body": draft["body"],
                    "ok": True,
                    "llm_processed": True,
                }
                ctx["generation"]["completed"] = index + 1
                ctx["generation"]["status_note"] = ""
                ctx["generation"]["generated_ok"] = _draft_stats(ctx)["generated_ok"]
                _save_ai_context_by_id(context_id, ctx)
                with _gen_lock:
                    if job_id in _gen_jobs:
                        _gen_jobs[job_id]["completed"] = index + 1
                        _gen_jobs[job_id]["status_note"] = ""
                        _gen_jobs[job_id]["generated_ok"] = ctx["generation"]["generated_ok"]
            else:
                pipeline_log.error(
                    "EMAIL %s/%s FAILED | error=%s",
                    index + 1,
                    len(rows),
                    last_exc or "Generation failed.",
                )
                ctx = _load_ai_context_by_id(context_id)
                ctx.setdefault("drafts", {})[email] = {
                    "subject": "",
                    "body": "",
                    "ok": False,
                    "error": quota_message if quota_abort else (str(last_exc) if last_exc else "Generation failed."),
                    "llm_processed": True,
                }
                ctx["generation"]["failed"] = int(ctx["generation"].get("failed", 0)) + 1
                ctx["generation"]["completed"] = index + 1
                ctx["generation"]["status_note"] = ""
                if quota_abort:
                    ctx["generation"]["quota_exhausted"] = True
                    ctx["generation"]["quota_message"] = quota_message
                    ctx["generation"]["status_note"] = quota_message
                _save_ai_context_by_id(context_id, ctx)
                with _gen_lock:
                    if job_id in _gen_jobs:
                        _gen_jobs[job_id]["failed"] = int(_gen_jobs[job_id].get("failed", 0)) + 1
                        _gen_jobs[job_id]["completed"] = index + 1
                        if quota_abort:
                            _gen_jobs[job_id]["quota_exhausted"] = True
                            _gen_jobs[job_id]["quota_message"] = quota_message
                            _gen_jobs[job_id]["status_note"] = quota_message
                        else:
                            _gen_jobs[job_id]["status_note"] = ""

            if quota_abort:
                _mark_skipped_drafts(context_id, job_id, rows, index + 1, quota_message)
                break

            if agent_abort:
                ctx = _load_ai_context_by_id(context_id)
                stats = _draft_stats(ctx)
                msg = agent_abort_message or "Multi-agent generation stopped."
                if stats["generated_ok"]:
                    msg = (
                        f"{agent_abort_message} "
                        f"Review the {stats['generated_ok']} email(s) prepared so far."
                    )
                ctx["generation"]["status"] = "error"
                ctx["generation"]["error"] = msg
                ctx["generation"]["agent_abort"] = True
                ctx["generation"]["current"] = ""
                ctx["generation"]["current_company"] = ""
                ctx["generation"]["status_note"] = msg
                _save_ai_context_by_id(context_id, ctx)
                with _gen_lock:
                    if job_id in _gen_jobs:
                        _gen_jobs[job_id]["status"] = "error"
                        _gen_jobs[job_id]["error"] = msg
                        _gen_jobs[job_id]["agent_abort"] = True
                        _gen_jobs[job_id]["current"] = ""
                        _gen_jobs[job_id]["current_company"] = ""
                        _gen_jobs[job_id]["status_note"] = msg
                break
    finally:
        if cache_name and provider == "gemini":
            pipeline_log.info("Deleting context cache %s", cache_name)
            delete_outreach_cache(api_key, cache_name)

    _raise_if_cancelled(job_id, context_id)

    ctx = _load_ai_context_by_id(context_id)
    gen = ctx.get("generation") or {}
    stats = _draft_stats(ctx)
    if gen.get("agent_abort") or (
        gen.get("status") == "error" and gen.get("error") and not gen.get("quota_exhausted")
    ):
        pipeline_log.info(
            "PIPELINE STOPPED EARLY job=%s | generated_ok=%s | error=%s",
            job_id,
            stats["generated_ok"],
            gen.get("error"),
        )
        with _gen_lock:
            if job_id in _gen_jobs:
                _gen_jobs[job_id]["generated_ok"] = stats["generated_ok"]
        return

    pipeline_log.info(
        "PIPELINE DONE job=%s | generated_ok=%s completed=%s failed=%s total=%s quota=%s",
        job_id,
        stats["generated_ok"],
        gen.get("completed"),
        gen.get("failed"),
        gen.get("total"),
        gen.get("quota_exhausted"),
    )
    ctx["generation"]["status"] = "done"
    ctx["generation"]["phase"] = "done"
    ctx["generation"]["current"] = ""
    ctx["generation"]["current_company"] = ""
    if gen.get("quota_exhausted"):
        note = gen.get("quota_message") or (GROQ_DAILY_USER_MESSAGE if provider == "groq" else QUOTA_EXPIRED_MESSAGE)
        ctx["generation"]["status_note"] = note
        ctx["generation"]["quota_message"] = note
    else:
        ctx["generation"]["status_note"] = ""
    ctx["generation"]["generated_ok"] = stats["generated_ok"]
    if stats["generated_ok"] >= len(rows) and provider == "groq":
        ctx.setdefault("provider_quota", {}).pop("groq", None)
    _save_ai_context_by_id(context_id, ctx)
    with _gen_lock:
        if job_id in _gen_jobs:
            _gen_jobs[job_id]["status"] = "done"
            _gen_jobs[job_id]["phase"] = "done"
            _gen_jobs[job_id]["current"] = ""
            _gen_jobs[job_id]["current_company"] = ""
            _gen_jobs[job_id]["status_note"] = ""
            _gen_jobs[job_id]["generated_ok"] = stats["generated_ok"]


def _load_ai_context() -> dict:
    path = _ai_context_path()
    if not path.exists():
        return _default_ai_context()
    data = json.loads(path.read_text(encoding="utf-8"))
    merged = _default_ai_context()
    merged.update(data)
    return merged


def _save_ai_context(data: dict) -> None:
    path = _ai_context_path()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _ai_context_summary() -> dict:
    ctx = _load_ai_context()
    gen = ctx.get("generation") or {}
    drafts = ctx.get("drafts") or {}
    return {
        "compose_mode": ctx.get("compose_mode"),
        "has_resume": bool(ctx.get("resume_text")),
        "resume_filename": ctx.get("resume_filename"),
        "portfolio_url": ctx.get("portfolio_url"),
        "companies_fetched": bool(ctx.get("companies_fetched")),
        "company_count": len(ctx.get("companies") or []),
        "generation_status": gen.get("status", "idle"),
        "generation_completed": gen.get("completed", 0),
        "generation_total": gen.get("total", 0),
        "has_drafts": bool(drafts),
        "draft_count": len(drafts),
        "ai_provider": ctx.get("ai_provider") or session.get("ai_provider") or "groq",
        "has_gemini_key": bool(session.get("gemini_api_key")),
        "has_groq_key": bool(session.get("groq_api_key")),
    }


def _get_campaign_id() -> str:
    cid = session.get("campaign_id")
    if not cid:
        cid = uuid.uuid4().hex
        session["campaign_id"] = cid
    with _campaign_lock:
        if cid not in _campaigns:
            _campaigns[cid] = {
                "approved_count": 0,
                "sent": 0,
                "failed": 0,
                "in_flight": 0,
                "status": "idle",
                "last_email": "",
                "seen": set(),
            }
    return cid


def _campaign_snapshot(campaign_id: str) -> dict:
    with _campaign_lock:
        c = _campaigns.get(campaign_id)
        if not c:
            return {
                "status": "idle",
                "approved": 0,
                "sent": 0,
                "failed": 0,
                "in_flight": 0,
                "queued": 0,
                "completed": 0,
                "last_email": "",
            }
        approved = c["approved_count"]
        sent = c["sent"]
        failed = c["failed"]
        in_flight = c["in_flight"]
        completed = sent + failed
        queued = max(approved - completed - in_flight, 0)
        status = c["status"]
        if approved > 0 and in_flight == 0 and completed >= approved:
            status = "done"
            c["status"] = "done"
        return {
            "status": status,
            "approved": approved,
            "sent": sent,
            "failed": failed,
            "in_flight": in_flight,
            "queued": queued,
            "completed": completed,
            "last_email": c.get("last_email", ""),
        }


_FAILED_GENERATION_SUBJECTS = frozenset({"(generation failed)", "generation failed"})


def _approve_send_blocked(body: dict) -> str | None:
    """Return an error message if this draft must not be sent."""
    if body.get("generation_failed"):
        return "This email failed to generate. Regenerate or edit it before sending."
    subject = (body.get("subject") or "").strip()
    body_text = (body.get("body") or "").strip()
    if subject.lower() in _FAILED_GENERATION_SUBJECTS:
        return "This email failed to generate. Regenerate or edit it before sending."
    if body_text.lower().startswith("could not generate"):
        return "This email failed to generate. Regenerate or edit it before sending."
    return None


def _prepare_send_context(body: dict) -> tuple[dict | None, str | None, list | None, dict | None, list[tuple[Path, str]] | None]:
    data = _load_session_data()
    if not data:
        return None, "Upload a spreadsheet first.", None, None, None

    subject = (body.get("subject") or "").strip()
    body_text = (body.get("body") or "").strip()
    fallbacks = _parse_fallbacks(body)
    customized = bool(body.get("customized"))

    if not subject or not body_text:
        return None, "Subject and body are required.", None, None, None

    if not customized:
        tokens = available_tokens(data, fallbacks)
        errors = (
            validate_template(subject, tokens)
            + validate_template(body_text, tokens)
            + validate_empty_fallbacks(subject, body_text, data, fallbacks)
        )
        if errors:
            return None, None, errors, None, None

    attachments: list[tuple[Path, str]] = []
    file_entry = _get_attachment_file()
    if file_entry:
        attachments.append(file_entry)

    if mentions_attachment(subject, body_text) and not attachments:
        return (
            None,
            'Your email mentions attaching something — upload a file or remove words like "attach".',
            None,
            None,
            None,
        )

    return data, None, None, fallbacks, attachments or None


def _send_approved_email(
    campaign_id: str,
    row: dict,
    subject: str,
    body_text: str,
    fallbacks: dict,
    attachments: list[tuple[Path, str]] | None,
    mail_settings: dict,
    *,
    customized: bool = False,
) -> None:
    email = row["email"]
    with _campaign_lock:
        c = _campaigns[campaign_id]
        c["in_flight"] += 1
        c["status"] = "sending"
        c["last_email"] = email

    if customized:
        render_fn = lambda tmpl, r: tmpl
    else:
        render_fn = lambda tmpl, r: render_email_template(tmpl, r, fallbacks)

    try:
        send_single(
            row,
            subject,
            body_text,
            attachments,
            render_fn,
            mail_settings=mail_settings,
        )
        with _campaign_lock:
            _campaigns[campaign_id]["sent"] += 1
    except Exception as exc:
        append_sent_log(email, "failed", str(exc))
        with _campaign_lock:
            _campaigns[campaign_id]["failed"] += 1
    finally:
        with _campaign_lock:
            c = _campaigns[campaign_id]
            c["in_flight"] = max(c["in_flight"] - 1, 0)
            if c["in_flight"] == 0 and c["sent"] + c["failed"] >= c["approved_count"]:
                c["status"] = "done"


def _parse_fallbacks(body: dict) -> dict:
    raw = body.get("fallbacks") or {}
    fallbacks = {}
    if "person_name" in raw:
        fallbacks["person_name"] = str(raw["person_name"])
    if "company_name" in raw:
        fallbacks["company_name"] = str(raw["company_name"])
    return fallbacks


def _render_recipient_preview(row: dict, subject: str, body_text: str, fallbacks: dict | None = None) -> dict:
    return {
        "email": row["email"],
        "person_name": row.get("person_name") or (fallbacks or {}).get("person_name", ""),
        "company_name": row.get("company_name") or (fallbacks or {}).get("company_name", ""),
        "subject": render_email_template(subject, row, fallbacks),
        "body": render_email_template(body_text, row, fallbacks),
    }


@app.route("/")
def index():
    return render_template("index.html", daily_limit=DAILY_LIMIT, max_recipients=MAX_RECIPIENTS)


@app.route("/api/status")
def api_status():
    settings = load_settings()
    smtp = _smtp_settings()
    sender = smtp.get("email_address", "")
    state = load_daily_state(sender)
    data = _load_session_data()

    return jsonify(
        {
            "gmail_configured": bool(smtp.get("email_address") and smtp.get("app_password")),
            "mail_configured": bool(smtp.get("email_address") and smtp.get("app_password")),
            "gmail_verified": _is_gmail_verified(),
            "mail_verified": _is_gmail_verified(),
            "mail_provider": settings.get("mail_provider", "gmail"),
            "gmail_address": settings.get("email_address", ""),
            "email_address": settings.get("email_address", ""),
            "daily_limit": DAILY_LIMIT,
            "recommended_daily_limit": DAILY_LIMIT,
            "sent_today": state["count"],
            "remaining_today": remaining_today(sender),
            "over_recommended_daily_limit": state["count"] >= DAILY_LIMIT,
            "has_upload": data is not None,
            "upload_summary": {
                "selected_count": data["selected_count"],
                "total_valid_emails": data["total_valid_emails"],
                "truncated": data["truncated"],
                "detected_columns": data["detected_columns"],
                "placeholders": data["placeholders"],
                "placeholder_fields": data.get("placeholder_fields"),
                "sheet_about_count": data.get("sheet_about_count", 0),
                "company_fill": data.get("company_fill"),
                "company_names_filled": bool(data.get("company_names_filled")),
                "company_fill_stats": data.get("company_fill_stats"),
                "rows": data["rows"][:10],
                "row_count": len(data["rows"]),
            }
            if data
            else None,
            **_attachment_status(),
            "ai_context": _ai_context_summary(),
        }
    )


@app.route("/api/session/reset", methods=["POST"])
def api_session_reset():
    _clear_session_data()
    return jsonify({"ok": True})


@app.route("/api/ai/mode", methods=["POST"])
def api_ai_mode():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    body = request.get_json(force=True) or {}
    mode = (body.get("mode") or "").strip().lower()
    if mode not in {"manual", "ai"}:
        return jsonify({"ok": False, "error": "Mode must be manual or ai."}), 400

    ctx = _load_ai_context()
    ctx["compose_mode"] = mode
    _save_ai_context(ctx)
    return jsonify({"ok": True, "mode": mode})


@app.route("/api/ai/context", methods=["GET"])
def api_ai_context_get():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    ctx = _load_ai_context()
    return jsonify({"ok": True, **ctx})


@app.route("/api/ai/resume", methods=["POST"])
def api_ai_resume():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No resume uploaded."}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"ok": False, "error": "No file selected."}), 400

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_RESUME_EXT:
        return jsonify({"ok": False, "error": "Upload a PDF or Word resume (.pdf, .docx)."}), 400

    _ensure_dirs()
    context_id = session.get("ai_context_id") or uuid.uuid4().hex
    session["ai_context_id"] = context_id
    session.modified = True

    temp_path = AI_CONTEXT_DIR / f"{context_id}_resume{ext}"
    file.save(temp_path)

    try:
        resume_text = extract_resume_text(temp_path)
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        return jsonify({"ok": False, "error": str(exc)}), 400

    ctx = _load_ai_context()
    ctx["resume_filename"] = secure_filename(file.filename)
    ctx["resume_text"] = resume_text
    ctx["companies_fetched"] = False
    ctx["companies"] = []
    ctx["drafts"] = {}
    ctx["generation"] = _default_ai_context()["generation"]
    _save_ai_context(ctx)

    _attach_resume_copy(temp_path, ctx["resume_filename"], ext)

    return jsonify(
        {
            "ok": True,
            "resume_filename": ctx["resume_filename"],
            **_attachment_status(),
        }
    )


@app.route("/api/ai/portfolio", methods=["POST"])
def api_ai_portfolio():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    body = request.get_json(force=True) or {}
    portfolio_url = (body.get("portfolio_url") or "").strip()
    ctx = _load_ai_context()
    ctx["portfolio_url"] = portfolio_url or None
    _save_ai_context(ctx)
    return jsonify({"ok": True, "portfolio_url": ctx["portfolio_url"]})


@app.route("/api/ai/companies", methods=["POST"])
def api_ai_companies():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    data = _load_session_data()
    if not data:
        return jsonify({"ok": False, "error": "Upload a spreadsheet first."}), 400

    ctx = _load_ai_context()
    if not ctx.get("resume_text"):
        return jsonify({"ok": False, "error": "Upload your resume first."}), 400

    companies = unique_companies(enrich_rows(data["rows"]))
    if not companies:
        ctx["companies_fetched"] = True
        ctx["companies"] = []
        _save_ai_context(ctx)
        return jsonify(
            {
                "ok": True,
                "companies": [],
                "message": "No company column found in your spreadsheet.",
            }
        )

    rows = enrich_rows(data["rows"])
    profiles: list[dict] = []
    for index, entry in enumerate(companies):
        if index:
            time.sleep(SCRAPE_DELAY)
        profiles.append(
            fetch_or_use_sheet_about(
                entry["company_name"],
                entry.get("company_website") or None,
                rows,
            )
        )

    ctx["companies_fetched"] = True
    ctx["companies"] = profiles
    ctx["drafts"] = {}
    ctx["generation"] = _default_ai_context()["generation"]
    _save_ai_context(ctx)

    found_count = sum(1 for item in profiles if item.get("found"))
    return jsonify(
        {
            "ok": True,
            "companies": profiles,
            "found_count": found_count,
            "total_count": len(profiles),
        }
    )


@app.route("/api/ai/llm-key/verify", methods=["POST"])
def api_ai_llm_key_verify():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    body = request.get_json(force=True) or {}
    provider = _normalize_ai_provider(body.get("provider"))
    api_key = (body.get("api_key") or _provider_api_key(provider) or "").strip()
    if not api_key:
        label = "Groq" if provider == "groq" else "Gemini"
        return jsonify({"ok": False, "error": f"Enter your {label} API key."}), 400

    multi_agent = bool(body.get("multi_agent"))
    secondary_key = (body.get("api_key_secondary") or "").strip()

    try:
        _verify_provider_key(provider, api_key)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    if secondary_key:
        try:
            _verify_provider_key(provider, secondary_key)
        except ValueError as exc:
            return jsonify({"ok": False, "error": f"Second API key: {exc}"}), 400

    _store_provider_key(provider, api_key)
    _store_secondary_key(provider, secondary_key if multi_agent else "")
    ctx = _load_ai_context()
    ctx["ai_provider"] = provider
    ctx["multi_agent"] = multi_agent
    _save_ai_context(ctx)
    return jsonify(
        {
            "ok": True,
            "provider": provider,
            "multi_agent": multi_agent,
            "has_secondary_key": bool(secondary_key) if multi_agent else False,
            "message": "API key verified.",
        }
    )


@app.route("/api/ai/llm-key", methods=["GET"])
def api_ai_llm_key_status():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    provider = _normalize_ai_provider(request.args.get("provider") or session.get("ai_provider"))
    ctx = _load_ai_context()
    groq_block = _groq_generate_block(ctx) if provider == "groq" else None
    return jsonify(
        {
            "ok": True,
            "provider": provider,
            "has_key": bool(_provider_api_key(provider)),
            "has_gemini_key": bool(session.get("gemini_api_key")),
            "has_groq_key": bool(session.get("groq_api_key")),
            "multi_agent": bool(ctx.get("multi_agent")),
            "has_secondary_key": bool(_secondary_api_key(provider)),
            "groq_daily_blocked": bool(groq_block and groq_block.get("blocked")),
            "groq_block_message": (groq_block or {}).get("message"),
            "groq_hours_until_retry": (groq_block or {}).get("hours_until_retry"),
        }
    )


@app.route("/api/ai/gemini-key/verify", methods=["POST"])
def api_ai_gemini_key_verify():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    body = request.get_json(force=True) or {}
    api_key = (body.get("api_key") or session.get("gemini_api_key") or "").strip()
    if not api_key:
        return jsonify({"ok": False, "error": "Enter your Gemini API key."}), 400

    try:
        _verify_provider_key("gemini", api_key)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    _store_provider_key("gemini", api_key)
    ctx = _load_ai_context()
    ctx["ai_provider"] = "gemini"
    _save_ai_context(ctx)
    return jsonify({"ok": True, "provider": "gemini", "message": "API key verified."})


@app.route("/api/ai/gemini-key", methods=["POST"])
def api_ai_gemini_key():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    body = request.get_json(force=True) or {}
    api_key = (body.get("api_key") or session.get("gemini_api_key") or "").strip()
    if not api_key:
        session.pop("gemini_api_key", None)
        session.modified = True
        return jsonify({"ok": True, "has_key": False})

    _store_provider_key("gemini", api_key)
    return jsonify({"ok": True, "has_key": True})


@app.route("/api/ai/gemini-key", methods=["GET"])
def api_ai_gemini_key_status():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked
    return jsonify({"ok": True, "has_key": bool(session.get("gemini_api_key"))})


@app.route("/api/ai/generate", methods=["POST"])
def api_ai_generate():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    data = _load_session_data()
    if not data:
        return jsonify({"ok": False, "error": "Upload a spreadsheet first."}), 400

    ctx = _load_ai_context()
    if not ctx.get("resume_text"):
        return jsonify({"ok": False, "error": "Upload your resume first."}), 400

    body = request.get_json(force=True) or {}
    portfolio_url = (body.get("portfolio_url") or ctx.get("portfolio_url") or "").strip()
    if portfolio_url:
        ctx["portfolio_url"] = portfolio_url
        _save_ai_context(ctx)

    provider = _normalize_ai_provider(body.get("provider") or ctx.get("ai_provider") or session.get("ai_provider"))
    api_key = (body.get("api_key") or _provider_api_key(provider)).strip()
    if not api_key:
        label = "Groq" if provider == "groq" else "Gemini"
        return jsonify({"ok": False, "error": f"Enter your {label} API key."}), 400

    if provider == "groq":
        groq_block = _groq_generate_block(ctx)
        if groq_block:
            remaining = _remaining_row_count(ctx, data)
            return jsonify(
                {
                    "ok": False,
                    "error": groq_block["message"],
                    "code": "groq_daily_exhausted",
                    "hours_until_retry": groq_block.get("hours_until_retry"),
                    "remaining_count": remaining,
                    "can_download_remaining": remaining > 0,
                }
            ), 429

    multi_agent = bool(body.get("multi_agent") if "multi_agent" in body else ctx.get("multi_agent"))
    secondary_key = (body.get("api_key_secondary") or _secondary_api_key(provider)).strip()

    _store_provider_key(provider, api_key)
    ctx["ai_provider"] = provider
    ctx["multi_agent"] = multi_agent
    if multi_agent and (body.get("api_key_secondary") or "").strip():
        _store_secondary_key(provider, secondary_key)
    elif not multi_agent:
        _store_secondary_key(provider, "")
    _save_ai_context(ctx)

    upload_id = session.get("upload_id")
    context_id = session.get("ai_context_id")
    if not upload_id or not context_id:
        return jsonify({"ok": False, "error": "Session expired. Re-upload your spreadsheet and resume."}), 400

    job_id = uuid.uuid4().hex
    session["ai_gen_job_id"] = job_id
    session.modified = True

    pipeline_log.info(
        "API /api/ai/generate → job=%s context=%s recipients=%s multi_agent=%s",
        job_id,
        context_id,
        len(data["rows"]),
        multi_agent,
    )

    with _gen_lock:
        _gen_jobs.pop(job_id, None)

    threading.Thread(
        target=_run_ai_pipeline,
        args=(
            job_id,
            api_key,
            upload_id,
            context_id,
            provider,
            secondary_key if multi_agent else None,
        ),
        daemon=True,
    ).start()
    companies_list = unique_companies(enrich_rows(data["rows"]))
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "provider": provider,
            "total": len(data["rows"]),
            "scrape_total": len(companies_list),
        }
    )


@app.route("/api/ai/generate/status", methods=["GET"])
def api_ai_generate_status():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    job_id = session.get("ai_gen_job_id")
    context_id = session.get("ai_context_id")
    if not job_id:
        ctx = _load_ai_context()
        gen = ctx.get("generation") or _default_ai_context()["generation"]
        stats = _draft_stats(ctx)
        return jsonify(
            {
                "ok": True,
                **gen,
                **stats,
                "drafts_ready": stats["drafts_ready"],
            }
        )

    payload = _generation_status_payload(job_id, context_id)
    return jsonify({"ok": True, **payload, "drafts_ready": payload.get("drafts_ready", 0)})


@app.route("/api/ai/generate/cancel", methods=["POST"])
def api_ai_generate_cancel():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    job_id = session.get("ai_gen_job_id")
    context_id = session.get("ai_context_id")
    if not job_id or not context_id:
        return jsonify({"ok": False, "error": "No generation in progress."}), 400

    with _gen_lock:
        job = _gen_jobs.get(job_id)
        if job and job.get("status") in {"done", "cancelled", "error", "idle"}:
            return jsonify({"ok": True, "already_stopped": True})

    _patch_gen_status(
        job_id,
        context_id,
        cancelled=True,
        status_note="Cancelling…",
    )
    return jsonify({"ok": True})


@app.route("/api/mail", methods=["POST"])
def api_mail():
    body = request.get_json(force=True)
    provider = (body.get("provider") or "gmail").strip().lower()
    address = (body.get("email_address") or body.get("gmail_address") or "").strip()
    password = (body.get("app_password") or body.get("gmail_app_password") or "").strip()

    if not address or not password:
        return jsonify({"ok": False, "error": "Email address and app password are required."}), 400

    ok, message = test_mail_connection(provider, address, password)
    if not ok:
        return jsonify({"ok": False, "error": message}), 400

    save_settings(address, provider)
    _store_mail_credentials(provider, address, password)
    session["gmail_verified"] = True
    return jsonify({"ok": True, "message": message, "provider": provider})


@app.route("/api/mail/test", methods=["POST"])
def api_mail_test():
    body = request.get_json(force=True) or {}
    provider = (body.get("provider") or session.get("mail_provider") or "gmail").strip().lower()
    address = (body.get("email_address") or body.get("gmail_address") or "").strip()
    password = (body.get("app_password") or body.get("gmail_app_password") or "").strip()

    if not address or not password:
        smtp = _smtp_settings()
        address = address or smtp.get("email_address", "")
        password = password or smtp.get("app_password", "")
        provider = provider or smtp.get("mail_provider", "gmail")

    ok, message = test_mail_connection(provider, address, password)
    return jsonify({"ok": ok, "message": message}), (200 if ok else 400)


@app.route("/api/gmail", methods=["POST"])
def api_gmail():
    body = request.get_json(force=True) or {}
    address = (body.get("gmail_address") or body.get("email_address") or "").strip()
    password = (body.get("gmail_app_password") or body.get("app_password") or "").strip()

    if not address or not password:
        return jsonify({"ok": False, "error": "Gmail address and App Password are required."}), 400

    ok, message = test_mail_connection("gmail", address, password)
    if not ok:
        return jsonify({"ok": False, "error": message}), 400

    save_settings(address, "gmail")
    _store_mail_credentials("gmail", address, password)
    session["gmail_verified"] = True
    return jsonify({"ok": True, "message": message, "provider": "gmail"})


@app.route("/api/gmail/test", methods=["POST"])
def api_gmail_test():
    body = request.get_json(force=True) or {}
    address = (body.get("gmail_address") or body.get("email_address") or "").strip()
    password = (body.get("gmail_app_password") or body.get("app_password") or "").strip()

    if not address or not password:
        smtp = _smtp_settings()
        address = address or smtp.get("email_address", "")
        password = password or smtp.get("app_password", "")

    ok, message = test_mail_connection("gmail", address, password)
    return jsonify({"ok": ok, "message": message}), (200 if ok else 400)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file uploaded."}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"ok": False, "error": "No file selected."}), 400

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_SHEET_EXT:
        return jsonify({"ok": False, "error": "Upload .xlsx, .xls, or .csv only."}), 400

    _ensure_dirs()
    temp_path = UPLOAD_DIR / f"temp_{uuid.uuid4().hex}{ext}"
    file.save(temp_path)

    try:
        parsed = parse_excel(temp_path, limit=MAX_RECIPIENTS)
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        temp_path.unlink(missing_ok=True)

    parsed["original_filename"] = secure_filename(file.filename)
    session["upload_id"] = uuid.uuid4().hex
    session.pop("upload_fill_job_id", None)
    session.modified = True
    _save_session_data(parsed)
    return jsonify({"ok": True, **parsed})


@app.route("/api/upload/data", methods=["GET"])
def api_upload_data():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    data = _load_session_data()
    if not data:
        return jsonify({"ok": False, "error": "Upload a spreadsheet first."}), 400

    return jsonify(upload_payload(data))


def _run_fill_companies_job(job_id: str, upload_id: str) -> None:
    try:
        _run_fill_companies_job_inner(job_id, upload_id)
    except Exception as exc:
        pipeline_log.exception("FILL COMPANIES ERROR job=%s: %s", job_id, exc)
        with _fill_lock:
            if job_id in _fill_jobs:
                _fill_jobs[job_id]["status"] = "error"
                _fill_jobs[job_id]["error"] = str(exc)


def _run_fill_companies_job_inner(job_id: str, upload_id: str) -> None:
    data = _load_session_data_by_id(upload_id)
    if not data:
        with _fill_lock:
            if job_id in _fill_jobs:
                _fill_jobs[job_id]["status"] = "error"
                _fill_jobs[job_id]["error"] = "Upload session expired."
        return

    rows = list(data.get("rows") or [])
    fill_indices = rows_needing_company_fill(rows)
    total = len(fill_indices)
    filled_total = 0
    skipped: list[dict] = []

    with _fill_lock:
        if job_id in _fill_jobs:
            _fill_jobs[job_id]["total"] = total

    if not total:
        cols = data.setdefault("detected_columns", {})
        if not cols.get("company_name"):
            cols["company_name"] = "Company"
        data["rows"] = rows
        data["company_names_filled"] = True
        data["company_fill_stats"] = {"filled": 0, "processed": 0, "skipped": 0}
        refresh_upload_metadata(data)
        _save_session_data_by_id(upload_id, data)
        _finish_fill_companies_job(job_id, data, filled_total, skipped)
        return

    for step, index in enumerate(fill_indices):
        with _fill_lock:
            job = _fill_jobs.get(job_id)
            if not job or job.get("cancelled"):
                with _fill_lock:
                    if job_id in _fill_jobs:
                        _fill_jobs[job_id]["status"] = "cancelled"
                return

        row = rows[index]
        email = str(row.get("email") or "")

        with _fill_lock:
            if job_id in _fill_jobs:
                _fill_jobs[job_id]["current"] = email
                _fill_jobs[job_id]["current_company"] = ""
                _fill_jobs[job_id]["status_note"] = (
                    f"Reading domain for {email}…" if email else "Processing…"
                )

        inferred = resolve_company_for_row(row)
        company = str(inferred.get("company_name") or "").strip()
        if company:
            row["company_name"] = company
            row["company_name_source"] = inferred.get("company_name_source", "email_domain")
            row["company_name_missing"] = False
            filled_total += 1
        else:
            row["company_name_missing"] = True
            skipped.append(
                {
                    "email": email,
                    "message": "I cannot determine with full confidence so skipped.",
                }
            )

        completed = step + 1
        with _fill_lock:
            if job_id in _fill_jobs:
                _fill_jobs[job_id]["completed"] = completed
                _fill_jobs[job_id]["filled"] = filled_total

        if step + 1 < total:
            time.sleep(FILL_COMPANY_DELAY)

    cols = data.setdefault("detected_columns", {})
    if not cols.get("company_name"):
        cols["company_name"] = "Company"

    data["rows"] = rows
    data["company_names_filled"] = True
    data["company_fill_stats"] = {
        "filled": filled_total,
        "processed": total,
        "skipped": len(skipped),
    }
    refresh_upload_metadata(data)
    _save_session_data_by_id(upload_id, data)
    _finish_fill_companies_job(job_id, data, filled_total, skipped)


def _finish_fill_companies_job(
    job_id: str,
    data: dict,
    filled_total: int,
    skipped: list[dict],
) -> None:
    with _fill_lock:
        if job_id not in _fill_jobs:
            return
        _fill_jobs[job_id]["filled"] = filled_total
        _fill_jobs[job_id]["skipped"] = skipped
        _fill_jobs[job_id]["completed"] = _fill_jobs[job_id].get("total", 0)
        _fill_jobs[job_id]["result"] = {
            "filled": filled_total,
            "processed": _fill_jobs[job_id].get("total", 0),
            "skipped": skipped,
            "company_fill": data.get("company_fill"),
            "detected_columns": data.get("detected_columns"),
            "placeholder_fields": data.get("placeholder_fields"),
            "placeholders": data.get("placeholders"),
            "rows": data.get("rows") or [],
            "selected_count": data.get("selected_count"),
            "total_valid_emails": data.get("total_valid_emails"),
            "truncated": data.get("truncated"),
            "sheet_about_count": data.get("sheet_about_count"),
            "company_names_filled": True,
            "company_fill_stats": data.get("company_fill_stats"),
        }
        _fill_jobs[job_id]["status"] = "done"
        _fill_jobs[job_id]["phase"] = "done"
        _fill_jobs[job_id]["status_note"] = ""
        _fill_jobs[job_id]["current"] = ""
        _fill_jobs[job_id]["current_company"] = ""


@app.route("/api/upload/fill-companies", methods=["POST"])
def api_upload_fill_companies():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    data = _load_session_data()
    if not data:
        return jsonify({"ok": False, "error": "Upload a spreadsheet first."}), 400

    company_fill = data.get("company_fill") or {}
    if not company_fill.get("needs_fill"):
        return jsonify({"ok": False, "error": "All contacts already have company names."}), 400
    if not company_fill.get("inferrable_count"):
        return jsonify(
            {
                "ok": False,
                "error": "No work email domains found to infer company names (e.g. Gmail addresses).",
            }
        ), 400

    upload_id = session.get("upload_id")
    if not upload_id:
        return jsonify({"ok": False, "error": "Upload session expired."}), 400

    rows = data.get("rows") or []
    to_fill = len(rows_needing_company_fill(rows))

    job_id = uuid.uuid4().hex
    session["upload_fill_job_id"] = job_id
    session.modified = True

    with _fill_lock:
        _fill_jobs[job_id] = {
            "status": "running",
            "phase": "scraping",
            "total": to_fill,
            "completed": 0,
            "filled": 0,
            "skipped": [],
            "current": "",
            "current_company": "",
            "status_note": "Starting…",
            "cancelled": False,
            "error": "",
            "result": None,
        }

    threading.Thread(
        target=_run_fill_companies_job,
        args=(job_id, upload_id),
        daemon=True,
    ).start()

    return jsonify({"ok": True, "job_id": job_id, "total": to_fill})


@app.route("/api/upload/fill-companies/status", methods=["GET"])
def api_upload_fill_companies_status():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    job_id = session.get("upload_fill_job_id")
    if not job_id:
        return jsonify({"ok": False, "error": "No fill job in progress."}), 400

    with _fill_lock:
        job = _fill_jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Fill job not found."}), 404

    payload = {
        "ok": True,
        "status": job.get("status", "idle"),
        "phase": job.get("phase", "scraping"),
        "total": job.get("total", 0),
        "completed": job.get("completed", 0),
        "filled": job.get("filled", 0),
        "current": job.get("current", ""),
        "current_company": job.get("current_company", ""),
        "status_note": job.get("status_note", ""),
        "error": job.get("error", ""),
        "skipped": job.get("skipped", []),
        "result": job.get("result"),
    }
    return jsonify(payload)


@app.route("/api/upload/fill-companies/cancel", methods=["POST"])
def api_upload_fill_companies_cancel():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    job_id = session.get("upload_fill_job_id")
    if not job_id:
        return jsonify({"ok": False, "error": "No fill job in progress."}), 400

    with _fill_lock:
        if job_id in _fill_jobs:
            _fill_jobs[job_id]["cancelled"] = True
            _fill_jobs[job_id]["status_note"] = "Cancelling…"
    return jsonify({"ok": True})


@app.route("/api/upload/download-remaining", methods=["GET"])
def api_upload_download_remaining():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    data = _load_session_data()
    if not data:
        return jsonify({"ok": False, "error": "Upload a spreadsheet first."}), 400

    ctx = _load_ai_context()
    drafts = ctx.get("drafts") or {}

    fmt = (request.args.get("format") or "xlsx").strip().lower()
    if fmt not in {"xlsx", "csv"}:
        fmt = "xlsx"

    try:
        content, mime, filename = export_remaining_spreadsheet(data, drafts, fmt)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    return send_file(
        BytesIO(content),
        mimetype=mime,
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/ai/quota-status", methods=["GET"])
def api_ai_quota_status():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    ctx = _load_ai_context()
    data = _load_session_data()
    provider = _normalize_ai_provider(request.args.get("provider") or ctx.get("ai_provider"))
    payload = {
        "ok": True,
        "provider": provider,
        "remaining_count": _remaining_row_count(ctx, data),
        "generated_ok": _draft_stats(ctx)["generated_ok"],
    }
    if provider == "groq":
        block = _groq_generate_block(ctx)
        payload["groq_daily_blocked"] = bool(block and block.get("blocked"))
        if block:
            payload["message"] = block.get("message")
            payload["hours_until_retry"] = block.get("hours_until_retry")
    return jsonify(payload)


@app.route("/api/upload/download", methods=["GET"])
def api_upload_download():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    data = _load_session_data()
    if not data:
        return jsonify({"ok": False, "error": "Upload a spreadsheet first."}), 400

    fmt = (request.args.get("format") or "xlsx").strip().lower()
    if fmt not in {"xlsx", "csv"}:
        fmt = "xlsx"

    try:
        content, mime, filename = export_spreadsheet(data, fmt)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    return send_file(
        BytesIO(content),
        mimetype=mime,
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/preview", methods=["POST"])
def api_preview():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    data = _load_session_data()
    if not data:
        return jsonify({"ok": False, "error": "Upload a spreadsheet first."}), 400

    body = request.get_json(force=True)
    subject = body.get("subject", "")
    body_text = body.get("body", "")

    fallbacks = _parse_fallbacks(body)
    tokens = available_tokens(data, fallbacks)
    errors = (
        validate_template(subject, tokens)
        + validate_template(body_text, tokens)
        + validate_empty_fallbacks(subject, body_text, data, fallbacks)
    )
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    samples = [_render_recipient_preview(row, subject, body_text, fallbacks) for row in data["rows"][:3]]
    return jsonify({"ok": True, "samples": samples})


@app.route("/api/review", methods=["POST"])
def api_review():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    data = _load_session_data()
    if not data:
        return jsonify({"ok": False, "error": "Upload a spreadsheet first."}), 400

    body = request.get_json(force=True) or {}
    use_ai_drafts = bool(body.get("use_ai_drafts"))

    ctx = _load_ai_context()
    if use_ai_drafts or (
        ctx.get("compose_mode") == "ai"
        and (ctx.get("generation") or {}).get("status") in {"done", "cancelled", "error"}
        and ctx.get("drafts")
    ):
        drafts = ctx.get("drafts") or {}
        gen = ctx.get("generation") or {}
        partial_stop = (
            bool(gen.get("quota_exhausted"))
            or gen.get("status") == "cancelled"
            or bool(gen.get("agent_abort"))
            or _draft_stats(ctx)["generated_ok"] < len(data["rows"])
        )
        recipients = []
        for row in data["rows"]:
            email = row.get("email", "")
            draft = drafts.get(email)
            if partial_stop:
                if not draft or draft.get("skipped"):
                    continue
            if draft is None:
                draft = {}
            if not draft.get("ok"):
                recipients.append(
                    {
                        **row,
                        "subject": draft.get("subject") or "(generation failed)",
                        "body": draft.get("error") or "Could not generate this email.",
                        "customized": True,
                        "ai_generated": True,
                        "generation_failed": True,
                    }
                )
                continue
            recipients.append(
                {
                    **row,
                    "subject": draft.get("subject", ""),
                    "body": draft.get("body", ""),
                    "customized": True,
                    "ai_generated": True,
                }
            )
        return jsonify(
            {
                "ok": True,
                "recipients": recipients,
                "total": len(recipients),
                "ai_mode": True,
                "partial_review": partial_stop,
                "agent_abort": bool(gen.get("agent_abort")),
                "quota_exhausted": bool(gen.get("quota_exhausted")),
                "quota_message": gen.get("quota_message") or "",
                "generated_ok": _draft_stats(ctx)["generated_ok"],
                "remaining_count": _remaining_row_count(ctx, data),
            }
        )

    subject = (body.get("subject") or "").strip()
    body_text = (body.get("body") or "").strip()
    fallbacks = _parse_fallbacks(body)

    if not subject or not body_text:
        return jsonify({"ok": False, "error": "Subject and body are required."}), 400

    tokens = available_tokens(data, fallbacks)
    errors = (
        validate_template(subject, tokens)
        + validate_template(body_text, tokens)
        + validate_empty_fallbacks(subject, body_text, data, fallbacks)
    )
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    recipients = [_render_recipient_preview(row, subject, body_text, fallbacks) for row in data["rows"]]
    return jsonify({"ok": True, "recipients": recipients, "total": len(recipients)})


def _run_regenerate_job(
    job_id: str,
    api_key: str,
    row: dict,
    email_key: str,
    resume: str,
    portfolio_url: str | None,
    profiles: list[dict],
    context_id: str,
    provider: str,
    *,
    multi_agent: bool = False,
    secondary_api_key: str | None = None,
) -> None:
    provider = _normalize_ai_provider(provider)
    company = row.get("company_name", "")
    pipeline_log.info(
        "REGENERATE START job=%s provider=%s email=%r company=%r resume_chars=%s",
        job_id,
        provider,
        email_key,
        company,
        len(resume),
    )

    def cancel_check() -> bool:
        with _regen_lock:
            job = _regen_jobs.get(job_id)
            return bool(job and job.get("cancelled"))

    def on_status(message: str) -> None:
        pipeline_log.info("REGEN STATUS: %s", message)
        with _regen_lock:
            if job_id in _regen_jobs:
                _regen_jobs[job_id]["status_note"] = message

    cache_name: str | None = None
    try:
        _reset_provider_tracking(provider)
        about_text, about_found = _about_for_recipient(row, profiles)
        provider_label = "Groq" if provider == "groq" else "Gemini"
        if USE_EXPLICIT_CACHE and provider == "gemini" and not multi_agent:
            cache_name = create_outreach_cache(
                api_key,
                resume,
                portfolio_url,
                on_status,
                cancel_check=cancel_check,
            )
        else:
            if multi_agent:
                on_status("Multi-agent rewrite — alignment, then draft…")
            else:
                on_status(f"Rewriting with {provider_label}…")

        draft = _generate_outreach_email(
            provider,
            api_key,
            resume,
            company,
            about_text,
            about_found,
            portfolio_url,
            person_name=str(row.get("person_name") or ""),
            company_name_missing=bool(row.get("company_name_missing")),
            company_name_source=str(row.get("company_name_source") or "sheet"),
            cache_name=cache_name,
            on_status=on_status,
            cancel_check=cancel_check,
            multi_agent=multi_agent,
            secondary_api_key=secondary_api_key,
        )

        ctx = _load_ai_context_by_id(context_id)
        ctx.setdefault("drafts", {})[email_key] = {
            "subject": draft["subject"],
            "body": draft["body"],
            "ok": True,
            "llm_processed": True,
        }
        _save_ai_context_by_id(context_id, ctx)

        recipient = _render_recipient_preview(row, draft["subject"], draft["body"])
        recipient["customized"] = True
        recipient["ai_generated"] = True
        recipient["generation_failed"] = False

        pipeline_log.info(
            "REGENERATE OK job=%s email=%r subject=%r",
            job_id,
            email_key,
            draft["subject"][:60],
        )

        with _regen_lock:
            if job_id in _regen_jobs:
                _regen_jobs[job_id]["status"] = "done"
                _regen_jobs[job_id]["recipient"] = recipient
                _regen_jobs[job_id]["status_note"] = ""
    except GenerationCancelled:
        pipeline_log.warning("REGENERATE CANCELLED job=%s email=%r", job_id, email_key)
        with _regen_lock:
            if job_id in _regen_jobs:
                _regen_jobs[job_id]["status"] = "cancelled"
                _regen_jobs[job_id]["status_note"] = "Cancelled."
    except Exception as exc:
        pipeline_log.error("REGENERATE FAILED job=%s email=%r: %s", job_id, email_key, exc)
        with _regen_lock:
            if job_id in _regen_jobs:
                _regen_jobs[job_id]["status"] = "error"
                _regen_jobs[job_id]["error"] = str(exc)
                _regen_jobs[job_id]["status_note"] = ""
    finally:
        if cache_name and provider == "gemini":
            delete_outreach_cache(api_key, cache_name)


@app.route("/api/ai/regenerate", methods=["POST"])
def api_ai_regenerate():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    ctx = _load_ai_context()
    provider = _normalize_ai_provider(ctx.get("ai_provider") or session.get("ai_provider"))
    api_key = _provider_api_key(provider)
    if not api_key:
        label = "Groq" if provider == "groq" else "Gemini"
        return jsonify({"ok": False, "error": f"{label} API key not found. Go back and reconnect AI compose."}), 400

    body = request.get_json(force=True) or {}
    email_search = (body.get("email") or "").strip().lower()
    if not email_search:
        return jsonify({"ok": False, "error": "Email address is required."}), 400

    data = _load_session_data()
    if not data:
        return jsonify({"ok": False, "error": "Upload a spreadsheet first."}), 400

    row = next(
        (r for r in data["rows"] if (r.get("email") or "").strip().lower() == email_search),
        None,
    )
    if not row:
        return jsonify({"ok": False, "error": "Recipient not found in your spreadsheet."}), 404

    ctx = _load_ai_context()
    if ctx.get("compose_mode") != "ai":
        return jsonify({"ok": False, "error": "Regenerate is only available for AI-generated emails."}), 400

    resume = ctx.get("resume_text") or ""
    if not resume.strip():
        return jsonify({"ok": False, "error": "Resume not found. Re-run AI compose."}), 400

    portfolio_url = (ctx.get("portfolio_url") or "").strip() or None
    profiles = ctx.get("companies") or []
    email_key = row.get("email", "")
    context_id = session.get("ai_context_id")
    if not context_id:
        return jsonify({"ok": False, "error": "Session expired. Re-run AI compose."}), 400

    job_id = uuid.uuid4().hex
    session["ai_regen_job_id"] = job_id
    session.modified = True

    with _regen_lock:
        _regen_jobs[job_id] = {
            "status": "running",
            "status_note": "Starting…",
            "cancelled": False,
            "email": email_key,
            "error": "",
            "recipient": None,
        }

    multi_agent = bool(ctx.get("multi_agent"))
    secondary_key = _secondary_api_key(provider) if multi_agent else None

    threading.Thread(
        target=_run_regenerate_job,
        args=(job_id, api_key, row, email_key, resume, portfolio_url, profiles, context_id, provider),
        kwargs={"multi_agent": multi_agent, "secondary_api_key": secondary_key or None},
        daemon=True,
    ).start()

    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/ai/regenerate/status", methods=["GET"])
def api_ai_regenerate_status():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    job_id = request.args.get("job_id") or session.get("ai_regen_job_id")
    if not job_id:
        return jsonify({"ok": False, "error": "No regenerate job in progress."}), 400

    with _regen_lock:
        job = _regen_jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Regenerate job not found."}), 404

    payload = {
        "ok": True,
        "status": job.get("status", "running"),
        "status_note": job.get("status_note", ""),
        "error": job.get("error", ""),
    }
    if job.get("status") == "done" and job.get("recipient"):
        payload["recipient"] = job["recipient"]
    return jsonify(payload)


@app.route("/api/ai/regenerate/cancel", methods=["POST"])
def api_ai_regenerate_cancel():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    body = request.get_json(force=True) or {}
    job_id = body.get("job_id") or session.get("ai_regen_job_id")
    if not job_id:
        return jsonify({"ok": False, "error": "No regenerate job in progress."}), 400

    with _regen_lock:
        if job_id in _regen_jobs:
            _regen_jobs[job_id]["cancelled"] = True
            _regen_jobs[job_id]["status_note"] = "Cancelling…"
    return jsonify({"ok": True})


@app.route("/api/attachment", methods=["POST"])
def api_attachment():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file uploaded."}), 400

    ok, message = _save_attachment_file(request.files["file"])
    if not ok:
        return jsonify({"ok": False, "error": message}), 400
    return jsonify({"ok": True, "filename": message})


@app.route("/api/attachment", methods=["DELETE"])
def api_attachment_delete():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    manifest_path = _attachment_manifest_path()
    if manifest_path.exists():
        manifest_path.unlink()
    return jsonify({"ok": True})


@app.route("/api/campaign/approve", methods=["POST"])
def api_campaign_approve():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    body = request.get_json(force=True)
    email = (body.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "error": "Email is required."}), 400

    block_reason = _approve_send_blocked(body)
    if block_reason:
        return jsonify({"ok": False, "error": block_reason, "code": "generation_failed"}), 400

    data, err, errors, fallbacks, attachments = _prepare_send_context(body)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    if err:
        return jsonify({"ok": False, "error": err}), 400

    row = next((r for r in data["rows"] if r["email"] == email), None)
    if not row:
        return jsonify({"ok": False, "error": "Recipient not found in upload."}), 400

    subject = (body.get("subject") or "").strip()
    body_text = (body.get("body") or "").strip()
    customized = bool(body.get("customized"))
    campaign_id = _get_campaign_id()

    with _campaign_lock:
        c = _campaigns[campaign_id]
        seen = c.setdefault("seen", set())
        if email in seen:
            return jsonify({"ok": False, "error": "This email was already approved."}), 400
        seen.add(email)
        c["approved_count"] += 1
        c["status"] = "sending"

    threading.Thread(
        target=_send_approved_email,
        args=(campaign_id, row, subject, body_text, fallbacks, attachments, _smtp_settings()),
        kwargs={"customized": customized},
        daemon=True,
    ).start()

    return jsonify({"ok": True, "campaign_id": campaign_id, **_campaign_snapshot(campaign_id)})


@app.route("/api/campaign/reset", methods=["POST"])
def api_campaign_reset():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked
    campaign_id = session.pop("campaign_id", None)
    if campaign_id:
        with _campaign_lock:
            _campaigns.pop(campaign_id, None)
    return jsonify({"ok": True})


@app.route("/api/campaign/status")
def api_campaign_status():
    campaign_id = session.get("campaign_id")
    if not campaign_id:
        return jsonify({"ok": True, "status": "idle", "approved": 0, "sent": 0, "failed": 0, "in_flight": 0, "queued": 0, "completed": 0, "last_email": ""})
    return jsonify({"ok": True, **_campaign_snapshot(campaign_id)})


@app.route("/api/send", methods=["POST"])
def api_send():
    blocked = _require_gmail_verified()
    if blocked:
        return blocked

    data = _load_session_data()
    if not data:
        return jsonify({"ok": False, "error": "Upload a spreadsheet first."}), 400

    body = request.get_json(force=True)
    subject = (body.get("subject") or "").strip()
    body_text = (body.get("body") or "").strip()

    fallbacks = _parse_fallbacks(body)

    if not subject or not body_text:
        return jsonify({"ok": False, "error": "Subject and body are required."}), 400

    tokens = available_tokens(data, fallbacks)
    errors = (
        validate_template(subject, tokens)
        + validate_template(body_text, tokens)
        + validate_empty_fallbacks(subject, body_text, data, fallbacks)
    )
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    attachments: list[tuple[Path, str]] = []
    file_entry = _get_attachment_file()
    if file_entry:
        attachments.append(file_entry)

    if mentions_attachment(subject, body_text) and not attachments:
        return jsonify(
            {
                "ok": False,
                "error": 'Your email mentions attaching something — upload a file or remove words like "attach".',
            }
        ), 400

    approved_emails = [e.strip().lower() for e in body.get("approved_emails", []) if e]
    if not approved_emails:
        return jsonify({"ok": False, "error": "Approve at least one recipient before sending."}), 400

    approved_set = set(approved_emails)
    rows_to_send = [row for row in data["rows"] if row["email"] in approved_set]
    if not rows_to_send:
        return jsonify({"ok": False, "error": "No matching approved recipients found."}), 400

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _send_jobs[job_id] = {
            "status": "running",
            "current": 0,
            "total": len(rows_to_send),
            "last_email": "",
            "result": None,
        }

    smtp_settings = _smtp_settings()

    def run_job():
        def on_progress(current, total, email, status):
            with _jobs_lock:
                _send_jobs[job_id].update(
                    {"current": current, "total": total, "last_email": email, "last_status": status}
                )

        try:
            result = send_batch(
                rows_to_send,
                subject,
                body_text,
                attachments or None,
                lambda tmpl, row: render_email_template(tmpl, row, fallbacks),
                on_progress=on_progress,
                mail_settings=smtp_settings,
            )
            with _jobs_lock:
                _send_jobs[job_id].update({"status": "done", "result": result})
        except Exception as exc:
            with _jobs_lock:
                _send_jobs[job_id].update({"status": "error", "error": str(exc)})

    threading.Thread(target=run_job, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/send/<job_id>")
def api_send_status(job_id: str):
    with _jobs_lock:
        job = _send_jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found."}), 404
    return jsonify({"ok": True, **job})


if __name__ == "__main__":
    _ensure_dirs()
    port = int(os.environ.get("PORT", "5001"))
    host = os.environ.get("HOST", "127.0.0.1")
    scheme = "https" if _is_production() else "http"
    print(f"\n  Email Hunter — {scheme}://{host}:{port}")
    print("  Verbose Gemini logging ON — watch this terminal during AI generation.")
    if host == "127.0.0.1":
        print("  (Use 127.0.0.1, not localhost — macOS AirPlay uses port 5000)\n")
    app.run(host=host, port=port, debug=False)
