"""Fetch public company About text from the web (best-effort, no API keys)."""

from __future__ import annotations

import logging
import re
import time
from html import unescape
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from excel_parser import MAX_RECIPIENTS

log = logging.getLogger("email_hunter.pipeline")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 6
COMPANY_SCRAPE_BUDGET = 18.0
MAX_SCRAPE_ATTEMPTS = 5
MAX_COMPANIES = MAX_RECIPIENTS  # match spreadsheet limit (50)
MAX_TEXT_CHARS = 4000
ABOUT_PATHS = ("/about", "/about-us", "/company", "/")
# Known short names where {name}.com is wrong — try these first.
COMPANY_URL_ALIASES: dict[str, list[str]] = {
    "aws": ["https://aws.amazon.com"],
}
SKIP_DOMAINS = (
    "duckduckgo.com",
    "google.com",
    "bing.com",
    "yahoo.com",
    "reddit.com",
    "linkedin.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "youtube.com",
    "wikipedia.org",
    "glassdoor.com",
    "indeed.com",
    "crunchbase.com",
    "zoominfo.com",
    "bloomberg.com",
)


def unique_companies(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    companies: list[dict] = []
    for row in rows:
        name = str(row.get("company_name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        website = str(row.get("company_website") or "").strip()
        companies.append({"company_name": name, "company_website": website})
        if len(companies) >= MAX_COMPANIES:
            break
    return companies


def gather_company_profiles(companies: list[dict], delay_seconds: float = 0.6) -> list[dict]:
    results: list[dict] = []
    for index, entry in enumerate(companies):
        if index:
            time.sleep(delay_seconds)
        results.append(fetch_company_about(entry["company_name"], entry.get("company_website") or None))
    return results


def sheet_about_for_company(company_name: str, rows: list[dict]) -> str:
    """First non-empty about text from the sheet for this company name."""
    key = company_name.strip().lower()
    if not key:
        return ""
    for row in rows:
        if str(row.get("company_name") or "").strip().lower() != key:
            continue
        about = str(row.get("company_about") or "").strip()
        if about:
            return about
    return ""


def profile_from_sheet_about(company_name: str, about_text: str) -> dict:
    text = about_text.strip()
    return {
        "company_name": company_name,
        "found": bool(text),
        "source_url": "spreadsheet",
        "text": text,
        "message": None if text else "Couldn't find company info.",
        "source": "sheet",
    }


def fetch_or_use_sheet_about(
    company_name: str,
    website: str | None,
    rows: list[dict],
) -> dict:
    """Use spreadsheet about when present; otherwise crawl the web."""
    sheet_about = sheet_about_for_company(company_name, rows)
    if sheet_about:
        log.info(
            "Skip crawl company=%r — using spreadsheet about (%s chars)",
            company_name,
            len(sheet_about),
        )
        return profile_from_sheet_about(company_name, sheet_about)
    return fetch_company_about(company_name, website)


def fetch_company_about(company_name: str, website: str | None = None) -> dict:
    base = {
        "company_name": company_name,
        "found": False,
        "source_url": None,
        "text": "",
        "message": "Couldn't find company info.",
    }

    deadline = time.monotonic() + COMPANY_SCRAPE_BUDGET
    tried: list[str] = []

    for root_url in _iter_candidate_roots(company_name, website, deadline):
        if len(tried) >= MAX_SCRAPE_ATTEMPTS:
            break
        if time.monotonic() >= deadline:
            break
        tried.append(root_url)
        about = _fetch_about_from_site(root_url, deadline)
        if not about:
            continue
        if not _content_matches_company(about["text"], company_name):
            log.warning(
                "Scrape rejected company=%r source=%s — page text does not mention company name",
                company_name,
                about["url"],
            )
            continue
        log.info(
            "Scrape OK company=%r source=%s chars=%s",
            company_name,
            about["url"],
            len(about["text"]),
        )
        return {
            "company_name": company_name,
            "found": True,
            "source_url": about["url"],
            "text": about["text"],
            "message": None,
        }

    log.info("Scrape miss company=%r tried=%s", company_name, tried)
    return base


def _iter_candidate_roots(
    company_name: str,
    website: str | None,
    deadline: float,
) -> list[str]:
    """Yield candidate site roots: explicit website → guesses → web search."""
    seen: set[str] = set()
    ordered: list[str] = []

    def add(url: str) -> None:
        root = _site_root(url)
        if not root or root in seen or _should_skip_url(url):
            return
        seen.add(root)
        ordered.append(root)

    if website:
        normalized = _normalize_website(website)
        if normalized:
            add(normalized)
        return ordered

    key = company_name.strip().lower()
    for url in COMPANY_URL_ALIASES.get(key, []):
        add(url)
    for url in _guess_company_urls(company_name):
        add(url)

    for url in _search_company_urls(company_name, deadline):
        add(url)

    return ordered


def _normalize_website(value: str) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _site_root(url: str) -> str | None:
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _guess_company_urls(company_name: str) -> list[str]:
    """Best-effort official site guess before search (amazon → amazon.com)."""
    words = re.findall(r"[a-z0-9]+", company_name.lower())
    if not words:
        return []
    urls: list[str] = []
    slug = "".join(words)
    if len(slug) >= 2:
        urls.append(f"https://www.{slug}.com")
        urls.append(f"https://{slug}.com")
    first = words[0]
    if len(first) >= 3 and first != slug:
        urls.append(f"https://www.{first}.com")
        urls.append(f"https://{first}.com")
    return urls


def _content_matches_company(text: str, company_name: str) -> bool:
    """Reject pages that don't mention the company (e.g. DuckDuckGo for Amazon)."""
    haystack = text.lower()
    tokens = re.findall(r"[a-z0-9]{3,}", company_name.lower())
    if not tokens:
        return True
    return any(token in haystack for token in tokens)


def _search_company_urls(company_name: str, deadline: float) -> list[str]:
    if time.monotonic() >= deadline:
        return []
    query = f"{company_name} company official website"
    remaining = max(1.0, deadline - time.monotonic())
    try:
        response = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": USER_AGENT},
            timeout=min(REQUEST_TIMEOUT, remaining),
        )
        response.raise_for_status()
    except requests.RequestException:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    urls: list[str] = []
    for anchor in soup.select("a.result__a"):
        href = anchor.get("href") or ""
        resolved = _resolve_ddg_href(href)
        if not resolved:
            continue
        if _should_skip_url(resolved):
            continue
        parsed = urlparse(resolved)
        root = f"{parsed.scheme}://{parsed.netloc}"
        if root not in urls:
            urls.append(root)
        if len(urls) >= 3:
            break
    return urls


def _resolve_ddg_href(href: str) -> str | None:
    if href.startswith("//duckduckgo.com/l/?"):
        href = "https:" + href
    if "uddg=" in href:
        parsed = urlparse(href)
        params = parse_qs(parsed.query)
        uddg = params.get("uddg", [None])[0]
        if uddg:
            return unquote(uddg)
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return None


def _should_skip_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(domain in host for domain in SKIP_DOMAINS)


def _fetch_about_from_site(root_url: str, deadline: float) -> dict | None:
    best: dict | None = None
    for path in ABOUT_PATHS:
        if time.monotonic() >= deadline:
            break
        url = urljoin(root_url.rstrip("/") + "/", path.lstrip("/"))
        html = _fetch_html(url, deadline)
        if not html:
            continue
        text = _extract_page_text(html)
        if len(text) < 120:
            continue
        if not best or len(text) > len(best["text"]):
            best = {"url": url, "text": text[:MAX_TEXT_CHARS]}
        if len(text) >= 180:
            return best
    return best


def _fetch_html(url: str, deadline: float | None = None) -> str | None:
    if deadline is not None and time.monotonic() >= deadline:
        return None
    timeout = REQUEST_TIMEOUT
    if deadline is not None:
        timeout = max(1.0, min(REQUEST_TIMEOUT, deadline - time.monotonic()))
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            allow_redirects=True,
        )
        if response.status_code >= 400:
            return None
        content_type = response.headers.get("Content-Type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            return None
        return response.text
    except requests.RequestException:
        return None


def _extract_page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "form"]):
        tag.decompose()

    for selector in ("main", "article", "[role='main']", ".about", "#about", ".content"):
        node = soup.select_one(selector)
        if node:
            text = _clean_text(node.get_text("\n", strip=True))
            if len(text) >= 120:
                return text

    title = soup.title.get_text(strip=True) if soup.title else ""
    description = ""
    meta = soup.find("meta", attrs={"name": re.compile(r"description", re.I)})
    if meta and meta.get("content"):
        description = meta["content"].strip()

    body_text = _clean_text(soup.body.get_text("\n", strip=True) if soup.body else soup.get_text("\n", strip=True))
    combined = "\n\n".join(part for part in (title, description, body_text) if part)
    return combined[:MAX_TEXT_CHARS]


def _clean_text(text: str) -> str:
    text = unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()
