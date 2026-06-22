# Email Hunter

**Email Hunter** is a local web app for **personalized cold email outreach at scale**. Upload a contact list, compose once (manually or with AI), review every draft, then send through your own **Gmail or Outlook** account.

Everything runs on **your machine**. Spreadsheets, drafts, and logs stay in the local `data/` folder (gitignored). Mail and AI API keys are held in your **browser session** for the current run — not written to the repo.

---

## What this tool can do

| Capability | Description |
|------------|-------------|
| **Import contacts** | Upload Excel (`.xlsx`, `.xls`) or CSV. Auto-detects Email, Name, Company, Website, and optional **Company about** columns. |
| **Fix missing companies** | If company names are blank or the column is missing, infer names from **work email domains** (e.g. `jane@stripe.com` → Stripe), update your data in-app, and **download an updated spreadsheet**. |
| **Manual templates** | Write one subject/body with `{person name}` and `{company name}` placeholders — personalized per row. |
| **AI drafts (Groq or Gemini)** | Upload your resume; the app researches each company and writes a **separate cold email per contact**. |
| **Multi-agent orchestration (optional)** | Agent 1 finds resume ↔ company alignment; Agent 2 writes the email from that research — stronger fit, uses 2 API calls per contact. |
| **Company research** | Scrapes public About pages when needed. **Skipped** if your sheet already has a Company about column filled in. |
| **Review before send** | Approve, edit, or reject each email. Regenerate individual AI drafts if needed. |
| **Attachments** | Attach a resume (AI path) and optional PDF/DOC files on manual sends. |
| **Rate limits** | ~60–90 s between sends and **50 emails/day per connected sending address** (anti-spam). |

**What it does not do:** It is not a CRM, not an email finder, and does not send without your review. Personal emails (Gmail, Yahoo, etc.) cannot be mapped to a company name automatically.

---

## Quick start

**Requirements:** Python **3.11+**, stable internet (SMTP, web scraping, and optional cloud AI).

```bash
git clone git@github.com:parthgupta9999/Email-Hunter.git
cd Email-Hunter

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5001** in your browser.

> **macOS:** Port **5001** is used (5000 is often taken by AirPlay). Prefer `127.0.0.1:5001`.

### Optional: `.env`

```bash
cp .env.example .env
```

You can set mail defaults there, or enter everything in the web UI. **Do not commit `.env`.**

---

## Usage flow

1. **Connect** — Gmail or Outlook + app password (accept the responsibility disclaimer)
2. **Import** — upload spreadsheet; optionally **fill company names from emails** and download the updated file
3. **Compose** — manual template **or** **Customise with AI** (your own Groq or Gemini API key)
4. **Review** — approve or edit each email, then send

---

## Spreadsheet format

| Email | Name | Company | Company about |
|-------|------|---------|---------------|
| jane@acme.com | Jane Smith | Acme Corp | Acme builds developer tools for… |

Supported headers include `Email`, `Name`, `Person Name`, `Company`, `Organization`, `Website`, `About`, `Company about`, etc.

### Fill company names from emails

After upload, if the **Company** column is missing or has blank cells:

1. Click **Fill company names from emails** (only rows that need a name are processed; existing entries are left unchanged).
2. Work domains are inferred (e.g. `@acme.com` → Acme). Personal domains (Gmail, Outlook, Yahoo, …) are **skipped** — you’ll see a dialog listing those addresses.
3. Download **.xlsx** or **.csv** with the updated sheet, or continue to compose without re-uploading.

### Company about in your sheet

If you provide a **Company about** column with text, the app **does not crawl** the web for that company — it uses your copy for AI drafting (faster and more accurate when you’ve already done the research).

---

## Customise with AI

1. Choose **Customise with AI** and connect a **Groq** or **Gemini** API key (free tiers available; limits apply per provider).
2. Upload your **resume** (PDF or DOCX).
3. Optional **portfolio URL** is woven into drafts when set.
4. The pipeline **gathers company context** (scrape or sheet about), then **writes one email per row**.

### Single-agent (default)

One model call per contact: resume + company background → full cold email.

### Multi-agent orchestration (optional)

Enable **Multi-agent orchestration** when connecting your API key:

| Step | Agent | Role |
|------|--------|------|
| 1 | **Alignment** | Reads company background + your resume → short company summary + bullet points where your experience aligns with the company. |
| 2 | **Writing** | Drafts the email using that research — fit-first tone, not a generic company recap. |

- Uses **two API requests per email** (alignment + writing). On free tiers this can hit RPM limits quickly.
- Optional **second API key** (same provider, different account) splits load: primary key → alignment, second key → writing.
- Orchestration runs only when **scraped or sheet “about” text exists**. If there is no company background, the app uses a **general cold email** with the company name only (no “couldn’t find info” language).
- If either agent fails, generation **stops**; emails already drafted are kept for partial review.

---

## Email account setup

### Gmail

1. Enable **2-Step Verification**
2. Create an **App Password**: https://myaccount.google.com/apppasswords
3. Enter address + app password in Step 1 of the app

Do **not** use your regular Gmail password.

### Outlook (personal)

1. Enable **two-step verification**
2. Create an **app password**: https://account.microsoft.com/security
3. Enter your `@outlook.com` / `@hotmail.com` address and app password

Work/school accounts may need SMTP enabled by IT.

---

## Limits

The daily cap applies to the **connected sending address**, not a separate “user account” in the app.

| Limit | Value |
|-------|-------|
| Per upload | 50 contacts |
| Per day | 50 emails **per connected address** (resets midnight local time) |
| Delay between sends | ~60–90 seconds |

AI limits depend on your **Groq/Gemini** plan (requests per minute/day). Multi-agent mode uses roughly **twice** as many calls per contact.

---

## System requirements (localhost)

Email Hunter is lightweight locally: a small Flask app, pandas, and HTTP scraping. **AI runs in the cloud** (Groq/Gemini) — no GPU or local LLM required.

| | Minimum | Recommended |
|---|---------|-------------|
| **CPU** | 2 cores, 1.6 GHz+ | 4+ cores |
| **RAM** | 4 GB system RAM (~1 GB free for the app) | 8 GB+ system RAM |
| **Storage** | 500 MB free | 1 GB+ free |
| **Display** | 1280×720 | 1920×1080 (side-by-side import/analysis panel) |
| **Network** | Stable broadband | Stable broadband |
| **Python** | 3.11+ | 3.12 or 3.13 |

**By platform**

- **Windows / Linux (Intel or AMD):** 64-bit OS; Core i3 / Ryzen 3 (2018+) or newer at minimum; Core i5 / Ryzen 5 (2020+) recommended for smoother browser + app together.
- **Mac (Intel):** 2017+ MacBook or iMac, **8 GB RAM**, macOS 12+.
- **Mac (Apple Silicon):** M1 or newer, **8 GB RAM** minimum (16 GB if you run many browser tabs alongside the app).

The heaviest work is **waiting on SMTP delays and AI/scrape network calls**, not CPU. A slow or offline network will block sending and AI features regardless of hardware.

---

## Project structure

```
Email-Hunter/
  app.py              # Flask server & API
  email_sender.py     # SMTP, rate limits, daily caps
  excel_parser.py     # Spreadsheet parse, export, company fill
  company_resolver.py # Infer company from email domain
  company_scraper.py  # Public About-page research
  multi_agent.py      # Multi-agent alignment + writing pipeline
  gemini_client.py    # Gemini drafting
  groq_client.py      # Groq drafting
  templates/          # HTML
  static/             # CSS, JS, assets
  data/               # Created at runtime (gitignored)
```

---

## Security

- Never commit `.env`, `data/settings.json`, or anything under `data/uploads/`
- App passwords and AI API keys live in your **browser session** for the current run
- Send from a **trusted, warmed-up address** — new inboxes are more likely to hit spam

---

## License

MIT License — free to use, modify, and distribute. See [LICENSE](LICENSE).

---

## Disclaimer

For lawful personal outreach only. You are responsible for complying with CAN-SPAM, GDPR, and provider terms of service. The authors accept no liability for misuse.
