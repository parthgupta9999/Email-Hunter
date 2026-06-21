# Email Hunter

A **local-only** web app for personalized cold email outreach from an Excel/CSV file via **Gmail or Outlook**.

Runs on your machine. Mail credentials, uploads, and logs stay in the local `data/` folder (not committed to git).

## Features

- **Gmail or Outlook** with an app password (2FA required)
- Upload **Excel (.xlsx, .xls) or CSV** — auto-detects email, name, and company columns
- **Single template** with `{person name}` and `{company name}` placeholders, or **Customise with AI** (company research + resume-based drafts)
- Attach a **resume** and optional documents (PDF, DOC, DOCX)
- **Max 50 recipients per upload** and **50 emails per day per connected sending address**
- ~60–90 second delay between sends (anti-spam)
- Review each email before it sends

## Quick start

**Requirements:** Python **3.11+**, stable internet (for SMTP, company research, and optional AI drafting).

```bash
git clone git@github.com:parthgupta9999/Email-Hunter.git
cd Email-Hunter

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5001** in your browser.

> **macOS:** This app uses port **5001** (5000 is often taken by AirPlay). Use `127.0.0.1`, not `localhost:5000`.

### Optional: `.env`

```bash
cp .env.example .env
```

You can set mail defaults there, or enter everything in the web UI. **Do not commit `.env`.**

## Usage flow

1. **Connect** — Gmail or Outlook + app password (accept the responsibility disclaimer)
2. **Import** — upload your spreadsheet
3. **Compose** — single template **or** Customise with AI (requires your own Groq or Gemini API key)
4. **Review** — approve or edit each email, then send

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

## Spreadsheet format

| Email | Name | Company |
|-------|------|---------|
| jane@acme.com | Jane Smith | Acme Corp |

Supported headers include `Email`, `Name`, `Person Name`, `Company`, `Organization`, etc.

## Limits

The daily cap applies to the **connected sending address**, not to a “user account” in the app. Each Gmail/Outlook address has its own 50/day counter (resets at midnight local time).

| Limit | Value |
|-------|-------|
| Per upload | 50 emails |
| Per day | 50 emails **per connected address** |
| Delay | ~60–90 sec between sends |

## System requirements (localhost)

Drafting uses **Groq or Gemini in the cloud** — no GPU or local AI model required.

| | Minimum | Recommended |
|---|---------|-------------|
| **CPU** | 2 cores | 4+ cores |
| **RAM** | 4 GB free | 8 GB+ |
| **Storage** | ~500 MB | 1 GB+ |
| **Python** | 3.11+ | 3.12 / 3.13 |

**Intel (Windows/Linux):** Core i3/i5 6th gen+ (min) · i5/i7 8th gen+ (recommended)  
**AMD Ryzen:** Ryzen 3/5 2000 series+ (min) · Ryzen 5/7 3000 series+ (recommended)  
**Mac:** Intel 2015+ with 8 GB RAM, or Apple Silicon M1+ with 8 GB+ · macOS 12+

## Project structure

```
Email-Hunter/
  app.py              # Flask server & API
  email_sender.py     # SMTP, rate limits, daily caps
  excel_parser.py     # Spreadsheet parsing
  gemini_client.py    # Gemini drafting
  groq_client.py      # Groq drafting
  company_scraper.py  # Company research
  templates/          # HTML
  static/             # CSS, JS, assets
  data/               # Created at runtime (gitignored)
```

## Security

- Never commit `.env`, `data/settings.json`, or anything under `data/uploads/`
- App passwords and API keys are kept in your **browser session** for the mail/AI steps
- Use an **established sending address** — new inboxes are more likely to hit spam

## License

MIT License — free to use, modify, and distribute. See [LICENSE](LICENSE).

## Disclaimer

For lawful personal outreach only. You are responsible for complying with CAN-SPAM, GDPR, and provider terms of service. The authors accept no liability for misuse.
