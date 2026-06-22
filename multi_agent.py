"""Multi-agent orchestration: align resume + company, then draft email."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from gemini_client import (
    EMAIL_WRITING_RULES,
    GeminiQuotaExhausted,
    GenerationCancelled,
    _build_static_prefix,
    _format_company_about,
    _greeting_instruction,
    _looks_valid_draft,
    _parse_subject_body,
    _portfolio_instruction,
    _recipient_name_line,
    _sanitize_draft,
    _word_count,
    build_recipient_prompt,
    countdown_wait,
    complete_prompt_gemini,
)
from groq_client import GroqQuotaExhausted, complete_prompt_groq, _parse_subject_body as groq_parse_subject_body

log = logging.getLogger("email_hunter.multi_agent")


class OrchestrationAgentError(ValueError):
    """An orchestration agent failed — pipeline should stop and keep partial drafts."""


def should_orchestrate(
    *,
    multi_agent: bool,
    about_found: bool,
    company_name_missing: bool,
    company_name: str,
) -> bool:
    """Multi-agent mode only when we scraped usable company background."""
    return bool(
        multi_agent
        and about_found
        and not company_name_missing
        and company_name.strip()
    )


ALIGNMENT_PROMPT = """You are preparing research for a cold outreach email.

CANDIDATE RESUME:
{resume}

TARGET COMPANY: {company_name}
{inferred_note}

COMPANY BACKGROUND (from public sources):
{company_about}

Your tasks:
1. Summarize what this company does in 3–5 concise sentences. Use only the background above — do not invent products or facts.
2. List 3–6 bullet points where the candidate's resume (skills, projects, experience) clearly aligns with this company. Each point should be something the candidate could mention naturally in a cold email.

Return EXACTLY in this format with no other text:

COMPANY SUMMARY:
<your summary paragraphs>

ALIGNMENT POINTS:
- first alignment point
- second alignment point
"""


WRITER_PROMPT_WITH_ALIGNMENT = """Write ONE complete, send-ready cold outreach email using the research below.

The email must read as one natural, flowing story — not two separate parts (a company overview, then a candidate pitch). The reader should feel why this candidate specifically belongs at this company.

TARGET COMPANY: {company_name}
{inferred_note}

RECIPIENT NAME: {recipient_name_line}

{greeting_instruction}

RESEARCH (internal reference — do not quote, bullet, or copy this verbatim):

Light context on what they do:
{company_summary}

Where the candidate's resume connects (this is the heart of the email):
{alignment_points}

CANDIDATE RESUME (facts, tone, and sign-off details):
{resume}
{portfolio_block}

{rules}

Writing priorities:
- Center the email on fit: how the candidate's skills, projects, and experience overlap with this company's work. That connection should drive every paragraph.
- You may use one brief, grounded line about what the company does if it sets up the alignment — never a full paragraph summarizing them.
- Do not paraphrase their About page, list their products or services, or sound like you are describing the company back to them.
- Do not write "I noticed you…" followed by a company essay, then a separate "As for me…" block. Blend context and alignment in the same sentences and paragraphs.
- Show fit with concrete details from the resume — name real skills, tools, or projects, not vague claims.
- Flow across 3–4 short paragraphs: hook with a specific connection → one or two proof points from the resume → clear, low-pressure ask.
- Subject: specific when possible, under 10 words, no "opportunity" or "application".
- Never use the word "Unknown" in the subject or body.
- Mention naturally that the candidate's resume is attached.

Return ONLY the email in this exact format with no labels, notes, markdown, or extra text:

SUBJECT: Your subject here

Hi there,

First paragraph...

Sign-off
Name
email@example.com"""


def _inferred_note(company_name_source: str) -> str:
    if company_name_source in {"email_domain", "website"}:
        return (
            "NOTE: The company name may be approximate (inferred from email domain). "
            "Do not overstate specific product details unless supported above.\n"
        )
    return ""


def build_alignment_prompt(
    resume_text: str,
    company_name: str,
    company_about: str,
    about_found: bool,
    *,
    company_name_source: str = "sheet",
) -> str:
    about = _format_company_about(company_name, company_about, about_found)
    return ALIGNMENT_PROMPT.format(
        resume=resume_text.strip(),
        company_name=company_name.strip() or "(not provided)",
        inferred_note=_inferred_note(company_name_source),
        company_about=about,
    )


def parse_alignment_response(raw: str) -> tuple[str, str]:
    text = raw.strip()
    if not text:
        raise OrchestrationAgentError("Alignment agent returned an empty response.")

    summary_match = re.search(
        r"COMPANY\s+SUMMARY:\s*(.+?)(?=ALIGNMENT\s+POINTS:|\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    points_match = re.search(r"ALIGNMENT\s+POINTS:\s*(.+)\Z", text, re.IGNORECASE | re.DOTALL)

    if not summary_match:
        raise OrchestrationAgentError("Alignment agent did not return a COMPANY SUMMARY section.")
    if not points_match:
        raise OrchestrationAgentError("Alignment agent did not return ALIGNMENT POINTS.")

    summary = summary_match.group(1).strip()
    points_block = points_match.group(1).strip()

    if len(summary) < 40:
        raise OrchestrationAgentError("Alignment agent returned a company summary that was too short.")
    if not re.search(r"^\s*[-•*]\s+\S", points_block, re.MULTILINE):
        raise OrchestrationAgentError("Alignment agent did not return bullet alignment points.")

    bullet_count = len(re.findall(r"^\s*[-•*]\s+\S", points_block, re.MULTILINE))
    if bullet_count < 2:
        raise OrchestrationAgentError("Alignment agent returned too few alignment points (need at least 2).")

    return summary, points_block


def build_writer_prompt(
    resume_text: str,
    company_name: str,
    company_summary: str,
    alignment_points: str,
    portfolio_url: str | None,
    *,
    person_name: str = "",
    company_name_missing: bool = False,
    company_name_source: str = "sheet",
) -> str:
    from gemini_client import _portfolio_block  # noqa: PLC0415

    if company_name_missing or not company_name.strip():
        task = build_recipient_prompt(
            "",
            company_summary,
            True,
            person_name=person_name,
            company_name_missing=True,
            company_name_source=company_name_source,
        )
        rules = EMAIL_WRITING_RULES.format(portfolio_instruction=_portfolio_instruction(portfolio_url))
        return (
            f"{task}\n\n"
            f"WHERE THE CANDIDATE'S BACKGROUND CONNECTS (weave 2–4 threads naturally — do not list as bullets):\n"
            f"{alignment_points}\n\n"
            f"{rules}\n\n"
            "Writing priority: focus on fit, not company overview. "
            "Blend any context and alignment into one flowing story — not separate 'about them' and 'about me' blocks."
        )

    company = company_name.strip()
    rules = EMAIL_WRITING_RULES.format(portfolio_instruction=_portfolio_instruction(portfolio_url))
    return WRITER_PROMPT_WITH_ALIGNMENT.format(
        company_name=company,
        inferred_note=_inferred_note(company_name_source),
        recipient_name_line=_recipient_name_line(person_name),
        greeting_instruction=_greeting_instruction(person_name, company_name=company),
        company_summary=company_summary,
        alignment_points=alignment_points,
        resume=resume_text.strip(),
        portfolio_block=_portfolio_block(portfolio_url),
        rules=rules,
    )


def _complete(
    provider: str,
    api_key: str,
    prompt: str,
    *,
    on_status: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    call_label: str = "agent",
) -> str:
    if provider == "groq":
        return complete_prompt_groq(
            api_key,
            prompt,
            on_status=on_status,
            cancel_check=cancel_check,
            call_label=call_label,
        )
    return complete_prompt_gemini(
        api_key,
        prompt,
        on_status=on_status,
        cancel_check=cancel_check,
        call_label=call_label,
    )


def _parse_draft(provider: str, raw: str) -> tuple[str, str]:
    if provider == "groq":
        return groq_parse_subject_body(raw)
    return _parse_subject_body(raw)


def generate_outreach_orchestrated(
    provider: str,
    align_api_key: str,
    write_api_key: str,
    resume_text: str,
    company_name: str,
    company_about: str,
    about_found: bool,
    portfolio_url: str | None = None,
    *,
    person_name: str = "",
    company_name_missing: bool = False,
    company_name_source: str = "sheet",
    on_status: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, str]:
    """Agent 1: align resume + company. Agent 2: draft email from that research."""
    if not should_orchestrate(
        multi_agent=True,
        about_found=about_found,
        company_name_missing=company_name_missing,
        company_name=company_name,
    ):
        raise OrchestrationAgentError(
            "Orchestration requires scraped company background."
        )

    log.info(
        "ORCHESTRATED generate | company=%r | align_key=%s write_key=%s",
        company_name or "(none)",
        "set" if align_api_key else "missing",
        "set" if write_api_key else "missing",
    )

    if on_status:
        on_status("Analyzing how your resume fits this company…")

    align_prompt = build_alignment_prompt(
        resume_text,
        company_name,
        company_about,
        about_found,
        company_name_source=company_name_source,
    )

    try:
        align_raw = _complete(
            provider,
            align_api_key,
            align_prompt,
            on_status=on_status,
            cancel_check=cancel_check,
            call_label=f"{company_name or 'company'} alignment",
        )
        company_summary, alignment_points = parse_alignment_response(align_raw)
    except GenerationCancelled:
        raise
    except (GroqQuotaExhausted, GeminiQuotaExhausted):
        raise
    except OrchestrationAgentError:
        raise
    except Exception as exc:
        raise OrchestrationAgentError(f"Alignment step failed: {exc}") from exc

    if on_status:
        on_status("Drafting email from alignment research…")

    writer_prompt = build_writer_prompt(
        resume_text,
        company_name,
        company_summary,
        alignment_points,
        portfolio_url,
        person_name=person_name,
        company_name_missing=company_name_missing,
        company_name_source=company_name_source,
    )

    last_error = "Writing agent could not produce a complete email."
    for attempt in range(2):
        if cancel_check and cancel_check():
            raise GenerationCancelled()
        if attempt:
            if on_status:
                on_status("First draft was incomplete — rewriting…")
            countdown_wait(3, on_status, "Retrying —", cancel_check)

        extra = ""
        if attempt:
            extra = (
                "\n\nIMPORTANT: Your previous reply was incomplete. "
                "Write the FULL email now. Start with SUBJECT: on the first line."
            )

        try:
            raw = _complete(
                provider,
                write_api_key,
                writer_prompt + extra,
                on_status=on_status,
                cancel_check=cancel_check,
                call_label=f"{company_name or 'email'} draft {attempt + 1}/2",
            )
            subject, body = _parse_draft(provider, raw)
        except GenerationCancelled:
            raise
        except (GroqQuotaExhausted, GeminiQuotaExhausted):
            raise
        except ValueError as exc:
            last_error = str(exc)
            continue
        except Exception as exc:
            raise OrchestrationAgentError(f"Writing step failed: {exc}") from exc

        subject, body = _sanitize_draft(subject, body)
        if _looks_valid_draft(subject, body):
            log.info("Orchestrated SUCCESS | subject=%r | words=%s", subject[:80], _word_count(body))
            return {"subject": subject, "body": body}

        last_error = f"Writing agent returned an incomplete email ({_word_count(body)} words)."
        if on_status and not attempt:
            on_status("Response incomplete — rewriting…")

    raise OrchestrationAgentError(last_error)
