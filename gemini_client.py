"""Generate outreach emails via Google Gemini API."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import requests

log = logging.getLogger("email_hunter.gemini")
_api_request_seq = 0

GEMINI_MODEL = "gemini-2.5-flash-lite"
GEMINI_FALLBACK_MODEL = "gemini-2.5-flash"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_CACHE_URL = f"{GEMINI_API_ROOT}/cachedContents"
REQUEST_TIMEOUT = 120
MAX_ABOUT_CHARS = 1200
MIN_BODY_WORDS = 100
MAX_BODY_WORDS = 240
CACHE_TTL = "3600s"

# Explicit cachedContents API is paid-tier only. Free accounts get 429 (limit=0).
USE_EXPLICIT_CACHE = False

# Free tier Gemini 2.5 Flash-Lite (docs): 30 RPM, 1M TPM, 1,000 RPD — per project per model.
# Separate daily counter from gemini-2.5-flash. Google may still enforce lower caps on some projects.
GEMINI_MIN_INTERVAL_SEC = 5.0
GEMINI_RPM_LIMIT = 8
GEMINI_RPM_WINDOW_SEC = 60.0
GEMINI_POST_SUCCESS_PAUSE_SEC = 2.0
GEMINI_POST_CACHE_PAUSE_SEC = 10.0
GEMINI_COOLDOWN_ON_429_SEC = 65.0
GEMINI_MAX_RETRIES = 2
GEMINI_CACHE_MAX_RETRIES = 1
GEMINI_RETRY_BASE_SEC = 15.0

_rate_lock = threading.Lock()
_last_request_at = 0.0
_cooldown_until = 0.0
_request_times: list[float] = []


def configure_generation_logging(level: int = logging.DEBUG) -> None:
    """Send detailed Gemini / pipeline logs to the terminal."""
    root = logging.getLogger("email_hunter")
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
    )
    root.setLevel(level)
    root.addHandler(handler)
    for name in ("email_hunter.gemini", "email_hunter.groq", "email_hunter.pipeline"):
        child = logging.getLogger(name)
        child.setLevel(level)
        child.propagate = True


def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _snippet(text: str, head: int = 300, tail: int = 150) -> str:
    cleaned = text.strip()
    if len(cleaned) <= head + tail + 24:
        return cleaned
    return f"{cleaned[:head]} … [{len(cleaned)} chars total] … {cleaned[-tail:]}"


def _rate_limit_snapshot() -> dict[str, Any]:
    with _rate_lock:
        now = time.monotonic()
        recent = [t for t in _request_times if now - t < GEMINI_RPM_WINDOW_SEC]
        return {
            "rpm_in_window": len(recent),
            "rpm_limit": GEMINI_RPM_LIMIT,
            "cooldown_secs_remaining": round(max(0.0, _cooldown_until - now), 1),
            "secs_since_last_request": round(now - _last_request_at, 1) if _last_request_at else None,
            "min_interval_sec": GEMINI_MIN_INTERVAL_SEC,
        }


def _log_section(title: str) -> None:
    log.info("=" * 72)
    log.info(title)
    log.info("=" * 72)


def _log_prompt(label: str, prompt: str, *, cache_name: str | None = None) -> None:
    parts = prompt.split("\n\n---\n\n", 1)
    prefix_chars = len(parts[0]) if len(parts) == 2 else 0
    task_chars = len(parts[1]) if len(parts) == 2 else len(prompt)
    log.info(
        "%s | chars=%s est_input_tokens≈%s | prefix_chars=%s task_chars=%s cache=%s",
        label,
        len(prompt),
        _est_tokens(prompt),
        prefix_chars,
        task_chars,
        cache_name or "none (full prompt sent)",
    )
    if len(parts) == 2:
        log.debug("%s TASK:\n%s", label, _snippet(parts[1], head=400, tail=200))
    log.debug("%s PROMPT SNIPPET:\n%s", label, _snippet(prompt))


def _log_usage(label: str, payload: dict[str, Any]) -> None:
    usage = payload.get("usageMetadata") or {}
    if not usage:
        log.info("%s | usageMetadata: (not returned by API)", label)
        return
    log.info(
        "%s | tokens: prompt=%s candidates=%s total=%s cached=%s thoughts=%s",
        label,
        usage.get("promptTokenCount", "?"),
        usage.get("candidatesTokenCount", "?"),
        usage.get("totalTokenCount", "?"),
        usage.get("cachedContentTokenCount", 0),
        usage.get("thoughtsTokenCount", 0),
    )


def _log_candidate(label: str, payload: dict[str, Any]) -> None:
    candidates = payload.get("candidates") or []
    if not candidates:
        feedback = payload.get("promptFeedback") or {}
        log.warning("%s | no candidates | promptFeedback=%s", label, json.dumps(feedback))
        return
    candidate = candidates[0]
    finish = candidate.get("finishReason") or "?"
    parts = (candidate.get("content") or {}).get("parts") or []
    part_kinds = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("text"):
            part_kinds.append("text")
        elif part.get("thought"):
            part_kinds.append("thought")
        else:
            part_kinds.append("other")
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    log.info(
        "%s | finishReason=%s | response_chars=%s est_output_tokens≈%s | parts=%s",
        label,
        finish,
        len(text),
        _est_tokens(text),
        part_kinds or "none",
    )
    if text.strip():
        log.debug("%s RESPONSE SNIPPET:\n%s", label, _snippet(text, head=400, tail=200))

EMAIL_WRITING_RULES = """Rules (apply to every email):
- Sound human, confident, and curious — not desperate or salesy.
- No bullet points, dashes used as bullets, semicolons, or emojis.
- Do not open with "I hope this email finds you well" or close with "I look forward to hearing from you."
- Do not use buzzwords like leverage, synergy, passionate, dynamic, or hardworking.
- Mention naturally that the candidate's resume is attached (e.g. "I've attached my resume").
{portfolio_instruction}
- 150 to 220 words in the body, in 3 or 4 short paragraphs.
- End with the candidate's name and one contact line from the resume (email or phone)."""

CACHE_SYSTEM_INSTRUCTION = """You are an expert at writing personalized cold outreach emails on behalf of job candidates.

{rules}

Return ONLY the email in this exact format with no labels, notes, markdown, or extra text:

SUBJECT: Your subject here

Hi there,

First paragraph...

Sign-off
Name
email@example.com"""

RECIPIENT_PROMPT_NO_COMPANY = """Write ONE complete, send-ready general cold outreach email for this recipient.

RECIPIENT NAME: {recipient_name_line}

RECIPIENT CONTEXT:
The recipient's company name is NOT available (personal email or missing from the spreadsheet).
Write a professional general cold email — do NOT invent or guess a company name.
Do NOT use any company name in the subject or body.
Do NOT use the word "Unknown" anywhere.

{greeting_instruction}
- Subject: skill-focused, under 10 words, no company name, no "opportunity" or "application".
- Do not pretend to know what company or product they work on. Keep it general but strong."""

EMAIL_PROMPT_INFERRED_NOTE = (
    "\nNOTE: The company name was inferred from the recipient's email domain and may be approximate. "
    "Use it naturally but do not overstate specific product details unless supported by the company background below.\n"
)

RECIPIENT_PROMPT_COMPANY = """Write ONE complete, send-ready cold outreach email for someone at the company below.

TARGET COMPANY: {company_name}
{inferred_note}

COMPANY BACKGROUND:
{company_about}

RECIPIENT NAME: {recipient_name_line}

{greeting_instruction}
- Subject: specific when possible, under 10 words, no "opportunity" or "application".
- Never use the word "Unknown" in the subject or body.
- Use the company background when it is specific; if you do not know the company well, be honest and general without inventing product details."""

# Legacy full prompts — used when context cache is unavailable.
EMAIL_PROMPT_NO_COMPANY = """You are writing ONE cold outreach email on behalf of the candidate below.

RECIPIENT CONTEXT:
The recipient's company name is NOT available (personal email or missing from the spreadsheet).
Write a professional general cold email — do NOT invent or guess a company name.
Do NOT use the word "Unknown" anywhere.

CANDIDATE RESUME:
{resume}
{portfolio_block}

Write a complete, send-ready email from the candidate to this recipient.

Rules:
- Sound human, confident, and curious — not desperate or salesy.
- No bullet points, dashes used as bullets, semicolons, or emojis.
- Do not open with "I hope this email finds you well" or close with "I look forward to hearing from you."
- Do not use buzzwords like leverage, synergy, passionate, dynamic, or hardworking.
- Do not pretend to know what company or product they work on. Keep it general but strong — focus on the candidate's skills and interest in impactful engineering work.
- Mention naturally that the candidate's resume is attached (e.g. "I've attached my resume").
{portfolio_instruction}
- 150 to 220 words in the body, in 3 or 4 short paragraphs.
- Start with a brief greeting like "Hi there," — do not use a company name in the greeting.
- End with the candidate's name and one contact line from the resume (email or phone).
- Subject: skill-focused, under 10 words, no company name, no "opportunity" or "application".

Return ONLY the email in this exact format with no labels, notes, markdown, or extra text:

SUBJECT: Your subject here

Hi there,

First paragraph...

Sign-off
Name
email@example.com
"""


def _portfolio_instruction(portfolio_url: str | None) -> str:
    if portfolio_url and portfolio_url.strip():
        return "- Include the portfolio link once in the body (not in the subject)."
    return ""


def _greeting_name(person_name: str) -> str | None:
    """First name for Hi {name}, — None if unavailable."""
    raw = (person_name or "").strip()
    if not raw or "@" in raw or len(raw) > 60:
        return None
    if re.search(r"\b(recruiter|hiring|manager|team|hr|talent)\b", raw, re.I):
        return None
    parts = raw.split()
    if len(parts) >= 2:
        first = parts[0].strip(",.")
        return first if first else None
    return raw.strip(",.")


def _recipient_name_line(person_name: str) -> str:
    raw = (person_name or "").strip()
    greet = _greeting_name(raw)
    if greet and raw:
        return f'{raw} (greet them as "Hi {greet},")'
    return '(not provided — use "Hi there,")'


def _greeting_instruction(
    person_name: str,
    *,
    company_name: str = "",
    allow_company_in_greeting: bool = True,
) -> str:
    greet = _greeting_name(person_name)
    if greet:
        return f'- Start the email with exactly: Hi {greet},'
    if allow_company_in_greeting and company_name.strip():
        company = company_name.strip()
        return f'- Start with a brief greeting (e.g. "Hi there," or "Hello {company} team,").'
    return '- Start with "Hi there,".'


def build_recipient_prompt(
    company_name: str,
    company_about: str,
    about_found: bool,
    *,
    person_name: str = "",
    company_name_missing: bool = False,
    company_name_source: str = "sheet",
) -> str:
    name_line = _recipient_name_line(person_name)
    if company_name_missing or not company_name.strip():
        return RECIPIENT_PROMPT_NO_COMPANY.format(
            recipient_name_line=name_line,
            greeting_instruction=_greeting_instruction(
                person_name, allow_company_in_greeting=False
            ),
        )

    company = company_name.strip()
    about = _format_company_about(company, company_about, about_found)
    inferred_note = EMAIL_PROMPT_INFERRED_NOTE if company_name_source in {"email_domain", "website"} else ""
    return RECIPIENT_PROMPT_COMPANY.format(
        company_name=company,
        inferred_note=inferred_note,
        company_about=about,
        recipient_name_line=name_line,
        greeting_instruction=_greeting_instruction(person_name, company_name=company),
    )


def _build_cache_payload(resume_text: str, portfolio_url: str | None) -> dict[str, Any]:
    resume = resume_text.strip()
    portfolio_block = _portfolio_block(portfolio_url)
    rules = EMAIL_WRITING_RULES.format(portfolio_instruction=_portfolio_instruction(portfolio_url))
    system_instruction = CACHE_SYSTEM_INSTRUCTION.format(rules=rules)
    contents_text = f"CANDIDATE RESUME:\n{resume}{portfolio_block}"

    return {
        "model": f"models/{GEMINI_MODEL}",
        "displayName": "email-hunter-resume",
        "systemInstruction": {
            "parts": [{"text": system_instruction}],
            "role": "system",
        },
        "contents": [
            {
                "parts": [{"text": contents_text}],
                "role": "user",
            }
        ],
        "ttl": CACHE_TTL,
    }


def _is_explicit_cache_blocked(status_code: int, payload_text: str) -> bool:
    """Explicit cachedContents is paid-tier only; free accounts get 429 limit=0."""
    if status_code not in {403, 429}:
        return False
    lowered = payload_text.lower()
    return any(
        phrase in lowered
        for phrase in (
            "cachedcontent",
            "cache",
            "limit=0",
            "totalcachedcontent",
            "context caching",
        )
    )


def create_outreach_cache(
    api_key: str,
    resume_text: str,
    portfolio_url: str | None = None,
    on_status: Callable[[str], None] | None = None,
    post_cache_pause: float | None = GEMINI_POST_CACHE_PAUSE_SEC,
    cancel_check: Callable[[], bool] | None = None,
) -> str | None:
    """Cache resume + rules once. Returns None if unavailable (common on free tier)."""
    if not resume_text.strip():
        log.warning("Cache skipped — empty resume")
        return None

    _check_cancelled(cancel_check)
    if on_status:
        on_status("Caching resume and instructions…")

    payload = _build_cache_payload(resume_text, portfolio_url)
    resume_block = payload["contents"][0]["parts"][0]["text"]
    sys_block = payload["systemInstruction"]["parts"][0]["text"]
    _log_section("CREATE CONTEXT CACHE")
    log.info(
        "POST %s | model=%s | resume_chars=%s est_tokens≈%s | rules_chars=%s",
        GEMINI_CACHE_URL,
        payload.get("model"),
        len(resume_block),
        _est_tokens(resume_block),
        len(sys_block),
    )

    _wait_for_rate_limit(on_status, cancel_check)
    started = time.monotonic()
    try:
        response = requests.post(
            GEMINI_CACHE_URL,
            params={"key": api_key},
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.error("Cache request failed: %s", exc)
        if on_status:
            on_status("Cache unavailable — writing without cache…")
        return None

    elapsed = time.monotonic() - started
    req_num = _record_api_request()
    log.info(
        "Cache response #%s | HTTP %s | %.2fs | body=%s",
        req_num,
        response.status_code,
        elapsed,
        _snippet(response.text, head=500, tail=200),
    )

    if response.status_code >= 400:
        body = response.text
        if _is_explicit_cache_blocked(response.status_code, body):
            log.warning("Explicit cache blocked (paid tier required)")
            if on_status:
                on_status(
                    "Context cache needs a paid Gemini plan — using standard mode…"
                )
            return None
        if _is_rate_limited(response.status_code, body):
            log.warning("Cache skipped due to rate limit")
            if on_status:
                on_status("Skipping cache — starting email generation…")
            return None
        log.warning("Cache create failed — falling back to full prompt mode")
        if on_status:
            on_status("Cache unavailable — writing without cache…")
        return None

    data = response.json()
    cache_name = data.get("name")
    log.info("Cache created: %s | ttl=%s", cache_name, CACHE_TTL)
    if cache_name and post_cache_pause and on_status:
        countdown_wait(post_cache_pause, on_status, "Cache ready — starting in", cancel_check)
    return cache_name


def delete_outreach_cache(api_key: str, cache_name: str) -> None:
    if not cache_name:
        return
    try:
        requests.delete(
            f"{GEMINI_API_ROOT}/{cache_name}",
            params={"key": api_key},
            timeout=25,
        )
    except requests.RequestException:
        pass


EMAIL_PROMPT_TEMPLATE = """You are writing ONE cold outreach email on behalf of the candidate below.

TARGET COMPANY: {company_name}
{inferred_note}

CANDIDATE RESUME:
{resume}

COMPANY BACKGROUND:
{company_about}
{portfolio_block}

Write a complete, send-ready email from the candidate to someone at {company_name}.

Rules:
- Sound human, confident, and curious — not desperate or salesy.
- No bullet points, dashes used as bullets, semicolons, or emojis.
- Do not open with "I hope this email finds you well" or close with "I look forward to hearing from you."
- Do not use buzzwords like leverage, synergy, passionate, dynamic, or hardworking.
- Use the company background when it is specific; if you do not know the company well, be honest and general without inventing product details.
- Never use the word "Unknown" in the subject or body.
- Mention naturally that the candidate's resume is attached (e.g. "I've attached my resume").
{portfolio_instruction}
- 150 to 220 words in the body, in 3 or 4 short paragraphs.
- Start the body with a brief greeting (e.g. "Hi there," or "Hello {company_name} team,").
- End with the candidate's name and one contact line from the resume (email or phone).
- Subject: specific when possible, under 10 words, no "opportunity" or "application".

Return ONLY the email in this exact format with no labels, notes, markdown, or extra text:

SUBJECT: Your subject here

Hi there,

First paragraph...

Sign-off
Name
email@example.com
"""


def _trim_text(text: str, max_chars: int) -> str:
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rsplit(" ", 1)[0] + "…"


def _format_company_about(company_name: str, about_text: str, about_found: bool) -> str:
    if about_found and about_text.strip():
        return _trim_text(about_text.strip(), MAX_ABOUT_CHARS)
    if company_name.strip():
        return (
            f"No website/about text available for {company_name.strip()}. "
            "Use your knowledge only if you genuinely know this company; otherwise write a professional generic email."
        )
    return "No company background available."


def _portfolio_block(portfolio_url: str | None) -> str:
    if portfolio_url and portfolio_url.strip():
        return f"\nCANDIDATE PORTFOLIO (mention once naturally in the body): {portfolio_url.strip()}"
    return ""


def _generation_config(model: str) -> dict[str, Any]:
    """Email writing needs visible output — disable 2.5 thinking that eats the token budget."""
    config: dict[str, Any] = {
        "temperature": 0.75,
        "maxOutputTokens": 2048,
    }
    if "2.5" in model:
        config["thinkingConfig"] = {"thinkingBudget": 0}
    return config


def _build_static_prefix(resume_text: str, portfolio_url: str | None) -> str:
    """Stable prefix reused across emails — Gemini 2.5 implicit caching can hit this block."""
    portfolio_instruction = _portfolio_instruction(portfolio_url)
    rules = EMAIL_WRITING_RULES.format(portfolio_instruction=portfolio_instruction)
    resume = resume_text.strip()
    return f"""You are an expert at writing personalized cold outreach emails on behalf of job candidates.

{rules}

CANDIDATE RESUME:
{resume}{_portfolio_block(portfolio_url)}

Return ONLY the email in this exact format with no labels, notes, markdown, or extra text:

SUBJECT: Your subject here

Hi there,

First paragraph...

Sign-off
Name
email@example.com"""


def build_prompt(
    resume_text: str,
    company_name: str,
    company_about: str,
    about_found: bool,
    portfolio_url: str | None = None,
    *,
    person_name: str = "",
    company_name_missing: bool = False,
    company_name_source: str = "sheet",
) -> str:
    prefix = _build_static_prefix(resume_text, portfolio_url)
    task = build_recipient_prompt(
        company_name,
        company_about,
        about_found,
        person_name=person_name,
        company_name_missing=company_name_missing,
        company_name_source=company_name_source,
    )
    return f"{prefix}\n\n---\n\n{task}"


def _sanitize_draft(subject: str, body: str) -> tuple[str, str]:
    subject = re.sub(r"\bfor Unknown\b", "", subject, flags=re.I).strip(" ,:-")
    subject = re.sub(r"\bUnknown\b", "", subject, flags=re.I).strip(" ,:-")
    body = re.sub(r"\bUnknown company\b", "your team", body, flags=re.I)
    body = re.sub(r"\bat Unknown\b", "", body, flags=re.I)
    return subject.strip(), body.strip()


_INPUT_ECHO_RE = re.compile(
    r"^\s*(?:COMPANY|RESUME|COMPANY ABOUT|CANDIDATE|TARGET COMPANY)\s*:.*$",
    re.MULTILINE | re.IGNORECASE,
)


def _strip_input_echo(text: str) -> str:
    match = _INPUT_ECHO_RE.search(text)
    if match:
        return text[: match.start()].strip()
    return text.strip()


def _parse_subject_body(text: str) -> tuple[str, str]:
    cleaned = _strip_input_echo(text.strip())
    if not cleaned:
        raise ValueError("Gemini returned an empty response.")

    subject_match = re.search(r"^SUBJECT:\s*(.+)$", cleaned, re.MULTILINE | re.IGNORECASE)
    if not subject_match:
        raise ValueError("Gemini did not return a SUBJECT line.")

    subject = subject_match.group(1).strip()
    subject = re.sub(r"[*_`#]+", "", subject).strip()
    if not subject or len(subject) > 120:
        raise ValueError("Gemini returned an invalid subject line.")

    body = cleaned[subject_match.end() :].strip()
    body = _strip_input_echo(body)
    if not body:
        raise ValueError("Gemini returned a subject but no email body.")

    if re.match(r"^(paragraph|subject|closing|opening)\b", subject, re.I):
        raise ValueError("Gemini returned instruction text instead of a real subject.")

    return subject, body


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text))


def _looks_valid_draft(subject: str, body: str) -> bool:
    words = _word_count(body)
    if words < MIN_BODY_WORDS:
        return False
    if words > MAX_BODY_WORDS + 40:
        return False
    if re.search(r"paragraph\s*\d|low-pressure CTA|STRUCTURE TO FOLLOW", body, re.I):
        return False
    if re.search(
        r"\[[^\]]{6,}\]|placeholder|otherwise skip|mention a specific|if known",
        body,
        re.I,
    ):
        return False
    if not re.search(r"^(hi|hello|dear|hey)\b", body, re.I | re.MULTILINE):
        return False
    if re.search(r"\bunknown\b", subject, re.I):
        return False
    return True


def _extract_text(payload: dict[str, Any]) -> tuple[str, str]:
    candidates = payload.get("candidates") or []
    if not candidates:
        block_reason = (payload.get("promptFeedback") or {}).get("blockReason")
        if block_reason:
            raise ValueError(f"Gemini blocked the prompt: {block_reason}")
        raise ValueError("Gemini returned no candidates.")

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
    finish_reason = candidate.get("finishReason") or ""
    if not text.strip():
        part_keys = [list(p.keys()) if isinstance(p, dict) else str(type(p)) for p in parts]
        log.warning(
            "Empty text in response | finishReason=%s | part_keys=%s",
            finish_reason,
            part_keys,
        )
        raise ValueError("Gemini returned empty text.")
    return text, finish_reason


class GenerationCancelled(Exception):
    """Raised when the user cancels AI generation."""


class GeminiQuotaExhausted(Exception):
    """Gemini free-tier / daily quota hit — waiting won't help until reset."""


_quota_exhausted = False
_had_429_this_job = False


def _check_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check and cancel_check():
        raise GenerationCancelled()


def reset_rate_limit_tracking() -> None:
    """Clear client-side pacing state at the start of each generation job."""
    global _last_request_at, _cooldown_until, _request_times, _api_request_seq
    global _quota_exhausted, _had_429_this_job
    with _rate_lock:
        _last_request_at = 0.0
        _cooldown_until = 0.0
        _request_times = []
    _api_request_seq = 0
    _quota_exhausted = False
    _had_429_this_job = False
    log.info("Rate-limit tracking reset (new job)")


def _parse_quota_violation(payload_text: str) -> dict[str, Any]:
    """Parse Google RPC quota details from a 429 response body."""
    result: dict[str, Any] = {
        "kind": "unknown",
        "limit": None,
        "metric": None,
        "quota_id": None,
        "model": None,
        "retry_seconds": None,
        "retryable": True,
        "violations": [],
    }
    try:
        data = json.loads(payload_text)
    except json.JSONDecodeError:
        return result

    error = data.get("error") or {}
    message = error.get("message") or ""

    # Message often includes: "limit: 20, model: gemini-2.5-flash"
    model_match = re.search(r"model:\s*([\w.\-]+)", message, re.I)
    if model_match:
        result["model"] = model_match.group(1)

    for detail in error.get("details") or []:
        dtype = detail.get("@type", "")
        if dtype.endswith("RetryInfo"):
            raw = detail.get("retryDelay", "")
            if isinstance(raw, str) and raw.endswith("s"):
                try:
                    result["retry_seconds"] = float(raw.rstrip("s"))
                except ValueError:
                    pass
        if dtype.endswith("QuotaFailure"):
            for v in detail.get("violations") or []:
                if not isinstance(v, dict):
                    continue
                result["violations"].append(v)
                result["metric"] = result["metric"] or v.get("quotaMetric")
                result["quota_id"] = result["quota_id"] or v.get("quotaId")
                dims = v.get("quotaDimensions") or {}
                if isinstance(dims, dict) and dims.get("model"):
                    result["model"] = result["model"] or dims["model"]
                try:
                    result["limit"] = int(v.get("quotaValue", 0) or 0)
                except (TypeError, ValueError):
                    pass

    qid = (result["quota_id"] or "").lower()
    metric = (result["metric"] or "").lower()
    message_lower = message.lower()

    if "perday" in qid or "per_day" in qid:
        result["kind"] = "rpd"
        result["retryable"] = False
    elif "perminute" in qid or "per_minute" in qid:
        result["kind"] = "rpm"
    elif any(x in message_lower for x in ("per day", "daily", "requests per day")):
        result["kind"] = "rpd"
        result["retryable"] = False
    elif "token" in metric and "minute" in metric:
        result["kind"] = "tpm"
    elif "generate_content" in metric or "free_tier" in metric:
        # Without quotaId, assume daily when limit is small (Google's per-model daily caps).
        if result["limit"] and result["limit"] <= 250:
            result["kind"] = "rpd"
            result["retryable"] = False

    return result


def _quota_error_hint(payload_text: str) -> str:
    q = _parse_quota_violation(payload_text)
    limit = q.get("limit")
    model = q.get("model") or GEMINI_MODEL
    qid = q.get("quota_id") or ""

    if q["kind"] == "rpd":
        base = f"Daily limit hit for {model}"
        if limit:
            base += f" ({limit} requests/day on your project"
            if limit == 20:
                base += " — docs say 250/day but Google assigned 20/day to some free projects; check AI Studio → Rate limits"
            base += ")"
        else:
            base += " (check AI Studio → Rate limits for your actual quota)"
        return f"{base}. Resets midnight Pacific. Each retry counts as another request."

    if q["kind"] == "rpm":
        if limit:
            return f"Too many requests for {model} ({limit}/min) — waiting to retry."
        return f"Too many requests per minute for {model} — waiting to retry."

    if q["kind"] == "tpm":
        return "Too many tokens per minute — waiting before retry."

    lowered = payload_text.lower()
    if any(p in lowered for p in ("per day", "daily", "requests per day")):
        return "Daily limit reached — resets midnight Pacific. Check AI Studio → Rate limits."
    if "too many requests" in lowered or "resource exhausted" in lowered:
        if qid:
            return f"Gemini quota exceeded ({qid}). Check AI Studio → Rate limits."
        return "Too many requests — check AI Studio → Rate limits (may be 10/min or daily cap)."
    if "token" in lowered and "minute" in lowered:
        return "Too many tokens per minute — waiting before retry."
    return "Too many requests — check AI Studio → Rate limits."


def _retry_pause_seconds(response: requests.Response, quota: dict[str, Any]) -> float:
    if not quota.get("retryable", True):
        return 0.0
    delay = quota.get("retry_seconds")
    if delay is None:
        raw = response.headers.get("Retry-After", "").strip()
        if raw.isdigit():
            delay = float(raw)
    if delay is not None:
        return min(max(delay + 1.0, 5.0), 120.0)
    if quota.get("kind") == "rpm":
        return 15.0
    return GEMINI_COOLDOWN_ON_429_SEC


def countdown_wait(
    seconds: float,
    on_status: Callable[[str], None] | None,
    prefix: str,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    """Sleep while updating status every second (60, 59, 58…)."""
    log.info("Waiting %.1fs — %s", seconds, prefix.strip())
    total = max(1, int(seconds))
    for remaining in range(total, 0, -1):
        _check_cancelled(cancel_check)
        if on_status:
            on_status(f"{prefix} {remaining}s…")
        time.sleep(1)
    _check_cancelled(cancel_check)
    fraction = seconds - total
    if fraction > 0:
        time.sleep(fraction)


def _record_api_request() -> int:
    global _api_request_seq, _request_times
    with _rate_lock:
        _api_request_seq += 1
        seq = _api_request_seq
        _request_times.append(time.monotonic())
        rpm = len([t for t in _request_times if time.monotonic() - t < GEMINI_RPM_WINDOW_SEC])
    log.info("API request #%s recorded | rpm_in_window=%s/%s", seq, rpm, GEMINI_RPM_LIMIT)
    return seq


def _enforce_rpm_limit(
    on_status: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    global _request_times
    with _rate_lock:
        now = time.monotonic()
        _request_times = [t for t in _request_times if now - t < GEMINI_RPM_WINDOW_SEC]
        if len(_request_times) < GEMINI_RPM_LIMIT:
            return
        wait = (_request_times[0] + GEMINI_RPM_WINDOW_SEC) - now

    if wait > 0.5:
        log.info(
            "RPM limit wait %.1fs (window=%ss limit=%s) | snapshot=%s",
            wait,
            GEMINI_RPM_WINDOW_SEC,
            GEMINI_RPM_LIMIT,
            _rate_limit_snapshot(),
        )
        countdown_wait(wait, on_status, "Spacing API calls —", cancel_check)


def _wait_for_rate_limit(
    on_status: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    global _last_request_at, _cooldown_until
    _enforce_rpm_limit(on_status, cancel_check)

    with _rate_lock:
        now = time.monotonic()
        cooldown_wait = _cooldown_until - now
        spacing_wait = GEMINI_MIN_INTERVAL_SEC - (now - _last_request_at)
        wait = max(0.0, cooldown_wait, spacing_wait)
        is_gemini_cooldown = cooldown_wait >= spacing_wait and cooldown_wait > 0.5

    if wait > 0:
        log.info(
            "Rate-limit pause %.1fs (cooldown=%s spacing=%s) | snapshot=%s",
            wait,
            is_gemini_cooldown,
            not is_gemini_cooldown,
            _rate_limit_snapshot(),
        )
        if wait > 1.5 and on_status:
            if is_gemini_cooldown:
                countdown_wait(wait, on_status, "Gemini rate limit — retry in", cancel_check)
            else:
                countdown_wait(wait, on_status, "Spacing API calls —", cancel_check)
        else:
            _check_cancelled(cancel_check)
            time.sleep(wait)

    with _rate_lock:
        _last_request_at = time.monotonic()


def _clear_rate_cooldown() -> None:
    global _cooldown_until
    with _rate_lock:
        _cooldown_until = 0.0


def _mark_quota_exhausted(hint: str) -> None:
    global _quota_exhausted, _had_429_this_job
    _quota_exhausted = True
    _had_429_this_job = True
    _clear_rate_cooldown()
    log.error("Gemini quota exhausted — stopping further API calls | %s", hint)


def _quota_is_exhausted() -> bool:
    return _quota_exhausted


def _set_rate_cooldown(seconds: float) -> None:
    global _cooldown_until
    with _rate_lock:
        _cooldown_until = max(_cooldown_until, time.monotonic() + seconds)
    log.warning("429 cooldown set for %.0fs", seconds)


def _raise_if_quota_exhausted() -> None:
    if _quota_exhausted:
        raise GeminiQuotaExhausted(
            "Daily Gemini limit reached. Resets midnight Pacific — or enable billing for more requests."
        )


def _handle_rate_limit_response(
    response: requests.Response,
    on_status: Callable[[str], None] | None,
) -> tuple[str, float, bool]:
    """Returns (user_hint, pause_seconds, retryable)."""
    quota = _parse_quota_violation(response.text)
    hint = _quota_error_hint(response.text)
    pause = _retry_pause_seconds(response, quota)
    retryable = bool(quota.get("retryable", True))

    global _had_429_this_job
    if _had_429_this_job:
        retryable = False
        log.warning("Second 429 in same job — stopping retries")
    _had_429_this_job = True

    log.warning(
        "Quota violation | kind=%s limit=%s model=%s quota_id=%s retryable=%s retry_in=%ss | metric=%s",
        quota["kind"],
        quota["limit"],
        quota.get("model"),
        quota["quota_id"],
        retryable,
        pause,
        quota["metric"],
    )
    if on_status:
        on_status(hint)
    return hint, pause, retryable


def _is_rate_limited(status_code: int, payload_text: str) -> bool:
    if status_code in {429, 503}:
        return True
    lowered = payload_text.lower()
    return any(
        phrase in lowered
        for phrase in (
            "rate limit",
            "too many requests",
            "resource exhausted",
            "quota exceeded",
            "exceeded your current quota",
        )
    )


def _call_model(
    api_key: str,
    prompt: str,
    model: str,
    on_status: Callable[[str], None] | None = None,
    cache_name: str | None = None,
    cancel_check: Callable[[], bool] | None = None,
    *,
    call_label: str = "generateContent",
) -> tuple[str, str]:
    url = f"{GEMINI_API_BASE}/{model}:generateContent"
    last_rate_error = "Gemini rate limit reached. Generation paused — try again in a few minutes."
    gen_config = _generation_config(model)

    for attempt in range(GEMINI_MAX_RETRIES):
        _check_cancelled(cancel_check)
        _raise_if_quota_exhausted()
        _wait_for_rate_limit(on_status, cancel_check)
        if on_status:
            on_status("Writing with Gemini…")

        request_body: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}], "role": "user"}],
            "generationConfig": gen_config,
        }
        if cache_name:
            request_body["cachedContent"] = cache_name

        _log_section(f"{call_label} — attempt {attempt + 1}/{GEMINI_MAX_RETRIES}")
        log.info("POST %s", url)
        log.info("model=%s | generationConfig=%s", model, json.dumps(gen_config))
        _log_prompt(call_label, prompt, cache_name=cache_name)
        log.info("rate_limit before request: %s", _rate_limit_snapshot())

        started = time.monotonic()
        try:
            response = requests.post(
                url,
                params={"key": api_key},
                json=request_body,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            elapsed = time.monotonic() - started
            log.error("Request failed after %.2fs: %s", elapsed, exc)
            if attempt + 1 >= GEMINI_MAX_RETRIES:
                raise ValueError(f"Could not reach Gemini: {exc}") from exc
            wait = GEMINI_RETRY_BASE_SEC * (2**attempt)
            countdown_wait(wait, on_status, "Connection issue — retrying in", cancel_check)
            continue

        elapsed = time.monotonic() - started
        req_num = _record_api_request()
        log.info(
            "Response #%s | HTTP %s | %.2fs | rate_limit after: %s",
            req_num,
            response.status_code,
            elapsed,
            _rate_limit_snapshot(),
        )

        if response.status_code == 404 and model != GEMINI_FALLBACK_MODEL:
            log.warning("Model %s not found — falling back to %s", model, GEMINI_FALLBACK_MODEL)
            return _call_model(
                api_key,
                prompt,
                GEMINI_FALLBACK_MODEL,
                on_status,
                cache_name,
                cancel_check,
                call_label=call_label,
            )

        if _is_rate_limited(response.status_code, response.text):
            hint, pause, retryable = _handle_rate_limit_response(response, on_status)
            if not retryable:
                _mark_quota_exhausted(hint)
                raise GeminiQuotaExhausted(f"{hint} Try again tomorrow or enable billing.")
            _set_rate_cooldown(pause)
            if attempt + 1 >= GEMINI_MAX_RETRIES:
                _mark_quota_exhausted(hint)
                raise GeminiQuotaExhausted(f"{hint} Try again in a few minutes.")
            countdown_wait(pause, on_status, "Gemini rate limit — retry in", cancel_check)
            continue

        if response.status_code in {401, 403}:
            log.error("Auth error HTTP %s", response.status_code)
            raise ValueError("Invalid Gemini API key. Check your key in Google AI Studio.")
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message", response.text)
            except Exception:
                detail = response.text
            log.error("API error HTTP %s: %s", response.status_code, _snippet(detail or "", 600, 200))
            if _is_rate_limited(response.status_code, detail or ""):
                hint, pause, retryable = _handle_rate_limit_response(response, on_status)
                if not retryable:
                    _mark_quota_exhausted(hint)
                    raise GeminiQuotaExhausted(f"{hint} Try again tomorrow or enable billing.")
                _set_rate_cooldown(pause)
                if attempt + 1 >= GEMINI_MAX_RETRIES:
                    _mark_quota_exhausted(hint)
                    raise GeminiQuotaExhausted(f"{hint} Try again in a few minutes.")
                countdown_wait(pause, on_status, "Gemini rate limit — retry in", cancel_check)
                continue
            raise ValueError(detail or f"Gemini API error ({response.status_code}).")

        _check_cancelled(cancel_check)
        log.info("Post-success pause %.1fs", GEMINI_POST_SUCCESS_PAUSE_SEC)
        time.sleep(GEMINI_POST_SUCCESS_PAUSE_SEC)
        payload = response.json()
        _log_usage(call_label, payload)
        _log_candidate(call_label, payload)
        return _extract_text(payload)

    raise ValueError(last_rate_error)


def verify_api_key(api_key: str) -> None:
    """Validate API key via models list (no generation — avoids rate limits)."""
    url = f"{GEMINI_API_ROOT}/models"
    last_busy = "Could not verify right now. Wait a minute and try again."

    for attempt in range(4):
        try:
            response = requests.get(
                url,
                params={"key": api_key},
                timeout=25,
            )
        except requests.RequestException:
            raise ValueError("Could not reach Gemini. Check your internet connection and try again.")

        if response.status_code == 200:
            return

        if response.status_code in {401, 403}:
            raise ValueError("Invalid Gemini API key. Check your key in Google AI Studio.")

        if response.status_code == 429 or _is_rate_limited(response.status_code, response.text):
            if attempt + 1 >= 4:
                raise ValueError(last_busy)
            time.sleep(3 + attempt * 5)
            continue

        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message", response.text)
            except Exception:
                detail = response.text
            raise ValueError(detail or f"Gemini API error ({response.status_code}).")

    raise ValueError(last_busy)


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
    _log_section(f"GENERATE EMAIL — company={company_name or '(none)'} person={person_name or '(none)'}")
    log.info(
        "inputs | about_found=%s | company_name_missing=%s | source=%s | person=%r | resume_chars=%s | about_chars=%s | cache=%s",
        about_found,
        company_name_missing,
        company_name_source,
        person_name or "",
        len(resume_text),
        len(company_about),
        cache_name or "none",
    )

    if cache_name:
        prompt = build_recipient_prompt(
            company_name,
            company_about,
            about_found,
            person_name=person_name,
            company_name_missing=company_name_missing,
            company_name_source=company_name_source,
        )
        log.info("prompt mode: cached (recipient task only, %s chars)", len(prompt))
    else:
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
        log.info("prompt mode: full (resume + rules + task, %s chars)", len(prompt))

    last_error = "Could not generate a complete email."

    for attempt in range(2):
        _check_cancelled(cancel_check)
        attempt_label = f"draft {attempt + 1}/2"
        log.info("--- %s ---", attempt_label)
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
            log.info("retry extra appended (%s chars)", len(extra))

        raw, finish_reason = _call_model(
            api_key,
            prompt + extra,
            GEMINI_MODEL,
            on_status,
            cache_name=cache_name,
            cancel_check=cancel_check,
            call_label=f"{company_name or 'email'} {attempt_label}",
        )
        try:
            subject, body = _parse_subject_body(raw)
        except ValueError as exc:
            last_error = str(exc)
            log.warning(
                "%s parse failed: %s | finishReason=%s",
                attempt_label,
                exc,
                finish_reason,
            )
            if finish_reason == "MAX_TOKENS" and on_status and not attempt:
                on_status("Response cut off — rewriting…")
            continue

        valid = _looks_valid_draft(subject, body)
        word_count = _word_count(body)
        log.info(
            "%s parsed | subject=%r | body_words=%s | valid=%s | finishReason=%s",
            attempt_label,
            subject[:80],
            word_count,
            valid,
            finish_reason,
        )

        if finish_reason == "MAX_TOKENS" and not valid:
            last_error = "Gemini cut off the email before it finished."
            log.warning("%s MAX_TOKENS with invalid draft (words=%s)", attempt_label, word_count)
            if on_status and not attempt:
                on_status("Response cut off — rewriting…")
            continue

        if not valid:
            last_error = f"Gemini returned an incomplete email ({word_count} words)."
            log.warning("%s validation failed: %s", attempt_label, last_error)
            continue

        subject, body = _sanitize_draft(subject, body)
        log.info("%s SUCCESS | subject=%r | final_words=%s", attempt_label, subject, _word_count(body))
        return {"subject": subject, "body": body}

    log.error("All draft attempts failed for %s: %s", company_name or "email", last_error)
    raise ValueError(last_error)
