"""Generate outreach emails via Groq OpenAI-compatible API."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import requests

from gemini_client import (
    GenerationCancelled,
    build_prompt,
    build_recipient_prompt,
    countdown_wait,
)

log = logging.getLogger("email_hunter.groq")

GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_API_BASE = "https://api.groq.com/openai/v1"
REQUEST_TIMEOUT = 120
GROQ_MIN_INTERVAL_SEC = 2.5
GROQ_RPM_LIMIT = 28
GROQ_RPM_WINDOW_SEC = 60.0
GROQ_MAX_RETRIES = 2
GROQ_RETRY_BASE_SEC = 5.0

_rate_lock = threading.Lock()
_last_request_at = 0.0
_request_times: list[float] = []
_quota_exhausted = False
_api_request_seq = 0


class GroqQuotaExhausted(ValueError):
    """Groq daily / token quota hit — waiting won't help until reset."""


def reset_rate_limit_tracking() -> None:
    global _last_request_at, _request_times, _quota_exhausted, _api_request_seq
    with _rate_lock:
        _last_request_at = 0.0
        _request_times = []
    _quota_exhausted = False
    _api_request_seq = 0
    log.info("Groq rate-limit tracking reset (new job)")


def _check_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check and cancel_check():
        raise GenerationCancelled()


def _record_api_request() -> int:
    global _api_request_seq, _request_times
    with _rate_lock:
        _api_request_seq += 1
        seq = _api_request_seq
        _request_times.append(time.monotonic())
    return seq


def _enforce_rpm_limit(
    on_status: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    global _request_times
    with _rate_lock:
        now = time.monotonic()
        _request_times = [t for t in _request_times if now - t < GROQ_RPM_WINDOW_SEC]
        if len(_request_times) < GROQ_RPM_LIMIT:
            return
        wait = (_request_times[0] + GROQ_RPM_WINDOW_SEC) - now

    if wait > 0.5:
        countdown_wait(wait, on_status, "Spacing Groq calls —", cancel_check)


def _wait_for_rate_limit(
    on_status: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    global _last_request_at
    _enforce_rpm_limit(on_status, cancel_check)
    with _rate_lock:
        elapsed = time.monotonic() - _last_request_at
        wait = GROQ_MIN_INTERVAL_SEC - elapsed
    if wait > 0.05:
        time.sleep(wait)
    with _rate_lock:
        _last_request_at = time.monotonic()


def _parse_rate_limit_headers(response: requests.Response) -> dict[str, Any]:
    def _int_header(name: str) -> int | None:
        raw = response.headers.get(name, "").strip()
        if raw.isdigit():
            return int(raw)
        return None

    return {
        "remaining_requests": _int_header("x-ratelimit-remaining-requests"),
        "limit_requests": _int_header("x-ratelimit-limit-requests"),
        "remaining_tokens": _int_header("x-ratelimit-remaining-tokens"),
        "limit_tokens": _int_header("x-ratelimit-limit-tokens"),
        "retry_after": _int_header("retry-after"),
    }


def _quota_hint(response: requests.Response) -> str:
    text = response.text.lower()
    headers = _parse_rate_limit_headers(response)
    if "tokens per day" in text or "tpd" in text:
        return (
            "Groq daily token limit reached for this model. "
            "Resets on a rolling 24h window — check console.groq.com/settings/limits."
        )
    if "requests per day" in text or "rpd" in text:
        limit = headers.get("limit_requests")
        base = "Groq daily request limit reached"
        if limit:
            base += f" ({limit}/day for {GROQ_MODEL})"
        return f"{base}. Resets on a rolling 24h window."
    if "tokens per minute" in text or "tpm" in text:
        return "Groq tokens-per-minute limit — waiting before retry."
    if "requests per minute" in text or "rpm" in text:
        return "Groq requests-per-minute limit — waiting before retry."
    return "Groq rate limit reached — check console.groq.com/settings/limits."


def _is_daily_quota_response(response: requests.Response) -> bool:
    text = response.text.lower()
    if "tokens per day" in text or "requests per day" in text:
        return True
    headers = _parse_rate_limit_headers(response)
    if headers.get("remaining_requests") == 0:
        return True
    return False


def _mark_quota_exhausted(hint: str) -> None:
    global _quota_exhausted
    _quota_exhausted = True
    log.error("Groq quota exhausted — stopping further API calls | %s", hint)


def _raise_if_quota_exhausted() -> None:
    if _quota_exhausted:
        raise GroqQuotaExhausted(
            "Your daily quota is expired. Review the emails generated so far."
        )


def _call_model(
    api_key: str,
    prompt: str,
    on_status: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    call_label: str = "groq",
) -> str:
    _raise_if_quota_exhausted()
    url = f"{GROQ_API_BASE}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2048,
        "temperature": 0.7,
    }

    last_error = "Groq could not generate a response."
    for attempt in range(GROQ_MAX_RETRIES + 1):
        _check_cancelled(cancel_check)
        _wait_for_rate_limit(on_status, cancel_check)
        seq = _record_api_request()
        log.info("Groq API request #%s | %s | prompt_chars=%s", seq, call_label, len(prompt))

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            last_error = f"Network error talking to Groq: {exc}"
            if attempt >= GROQ_MAX_RETRIES:
                raise ValueError(last_error) from exc
            countdown_wait(GROQ_RETRY_BASE_SEC * (attempt + 1), on_status, "Retrying Groq —", cancel_check)
            continue

        if response.status_code in {401, 403}:
            raise ValueError("Invalid Groq API key. Check your key at console.groq.com/keys.")

        if response.status_code == 429:
            hint = _quota_hint(response)
            if _is_daily_quota_response(response) or attempt >= GROQ_MAX_RETRIES:
                _mark_quota_exhausted(hint)
                raise GroqQuotaExhausted("Your daily quota is expired. Review the emails generated so far.")
            pause = _parse_rate_limit_headers(response).get("retry_after") or (GROQ_RETRY_BASE_SEC * (attempt + 1))
            if on_status:
                on_status(f"Groq busy — retrying in {int(pause)}s…")
            countdown_wait(float(pause), on_status, "Groq rate limit —", cancel_check)
            continue

        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message", response.text)
            except Exception:
                detail = response.text
            raise ValueError(detail or f"Groq API error ({response.status_code}).")

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise ValueError("Groq returned no choices.")
        message = choices[0].get("message") or {}
        text = (message.get("content") or "").strip()
        if not text:
            raise ValueError("Groq returned an empty response.")
        log.info("Groq response OK | %s | chars=%s", call_label, len(text))
        return text

    raise ValueError(last_error)


def verify_api_key(api_key: str) -> None:
    url = f"{GROQ_API_BASE}/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    response = requests.get(url, headers=headers, timeout=30)
    if response.status_code in {401, 403}:
        raise ValueError("Invalid Groq API key. Check your key at console.groq.com/keys.")
    if response.status_code >= 400:
        try:
            detail = response.json().get("error", {}).get("message", response.text)
        except Exception:
            detail = response.text
        raise ValueError(detail or f"Groq API error ({response.status_code}).")


def _parse_subject_body(text: str) -> tuple[str, str]:
    from gemini_client import _sanitize_draft, _strip_input_echo  # noqa: PLC0415

    cleaned = _strip_input_echo(text.strip())
    if not cleaned:
        raise ValueError("Groq returned an empty response.")

    subject_match = re.search(r"^SUBJECT:\s*(.+)$", cleaned, re.MULTILINE | re.IGNORECASE)
    if not subject_match:
        raise ValueError("Groq did not return a SUBJECT line.")

    subject = subject_match.group(1).strip()
    subject = re.sub(r"[*_`#]+", "", subject).strip()
    if not subject or len(subject) > 120:
        raise ValueError("Groq returned an invalid subject line.")

    body = cleaned[subject_match.end() :].strip()
    body = _strip_input_echo(body)
    if not body:
        raise ValueError("Groq returned a subject but no email body.")

    if re.match(r"^(paragraph|subject|closing|opening)\b", subject, re.I):
        raise ValueError("Groq returned instruction text instead of a real subject.")

    return _sanitize_draft(subject, body)


def generate_outreach_email(
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
    on_status: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, str]:
    del cache_name  # Groq has no explicit cache in this app
    log.info(
        "GROQ GENERATE — company=%r person=%r | resume_chars=%s",
        company_name or "(none)",
        person_name or "(none)",
        len(resume_text),
    )

    prompt = build_prompt(
        resume_text,
        company_name,
        company_about,
        about_found,
        portfolio_url,
        person_name=person_name,
        company_name_missing=company_name_missing,
        company_name_source=company_name_source,
    )

    last_error = "Could not generate a complete email."

    for attempt in range(2):
        _check_cancelled(cancel_check)
        if attempt:
            if on_status:
                on_status("First draft was incomplete — rewriting…")
            countdown_wait(3, on_status, "Retrying —", cancel_check)
        extra = ""
        if attempt:
            extra = (
                "\n\nIMPORTANT: Your previous reply was incomplete or malformed. "
                "Write the FULL email now — greeting, 3-4 paragraphs, sign-off, and contact info. "
                "Do not repeat instructions. Start with SUBJECT: on the first line."
            )

        raw = _call_model(
            api_key,
            prompt + extra,
            on_status,
            cancel_check=cancel_check,
            call_label=f"{company_name or 'email'} draft {attempt + 1}/2",
        )

        from gemini_client import _looks_valid_draft, _word_count  # noqa: PLC0415

        try:
            subject, body = _parse_subject_body(raw)
        except ValueError as exc:
            last_error = str(exc)
            continue

        valid = _looks_valid_draft(subject, body)
        word_count = _word_count(body)
        if not valid:
            last_error = f"Groq returned an incomplete email ({word_count} words)."
            if on_status and not attempt:
                on_status("Response incomplete — rewriting…")
            continue

        log.info("Groq SUCCESS | subject=%r | words=%s", subject[:80], word_count)
        return {"subject": subject, "body": body}

    raise ValueError(last_error)
