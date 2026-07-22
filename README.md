# Get AIMA status

Unofficial helper for Portugal’s [AIMA contact form](https://contactenos.aima.gov.pt/contact-form).  
It requests a status update for a **passport** or **residence permit** (topic/subtopic used by the public form).

Not affiliated with AIMA. Use at your own risk and only with your own data.

## Two ways to run it

| | Telegram bot | Local one-time script |
|---|---|---|
| **For** | You or others via chat | A single run on your machine |
| **Entry** | `./run_bot.sh` | `python aima_form.py` / `aima_residence_form.py` |
| **Needs** | Telegram token, Supabase, local browser | Config JSON, local browser |

Both paths share `aima_service.py` and open a **headed Chrome/Chromium** window via Playwright. The host must be able to reach AIMA (many US cloud IPs cannot).

## Requirements

- Python 3.9+
- macOS or Linux with a display (browser is not headless)
- Network access to `contactenos.aima.gov.pt` and `api-contactenos.aima.gov.pt`

## Install

```bash
git clone https://github.com/vrnch/get-aima-status.git
cd get-aima-status
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

---

## Option A — Telegram bot

### 1. Supabase

1. Create a project at [supabase.com](https://supabase.com).
2. SQL Editor → run [`supabase_schema.sql`](supabase_schema.sql).
3. Project Settings → API → copy:
   - Project URL (`https://xxxx.supabase.co`, **without** `/rest/v1`)
   - Secret key (`sb_secret_...`)

### 2. Telegram

Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.

### 3. Environment

```bash
cp .env.example .env
```

Fill in:

```text
TELEGRAM_BOT_TOKEN=...
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SECRET_KEY=sb_secret_...
BUYMEACOFFEE_URL=          # optional tip link after success
# BROWSER_PROFILE_DIR=     # optional; default ./.pw-profile
```

### 4. Run

```bash
chmod +x run_bot.sh
./run_bot.sh
```

Leave the Chrome window open while the bot is running. Keep the machine awake.

In Telegram:

- `/start` — begin a request  
- `/cancel` — abort and delete the temporary session  

Run **one** bot process per token.

### Privacy

Session rows in `aima_bot_sessions` expire after 30 minutes and are deleted after submit, `/cancel`, or cleanup. Only the server secret key can read them. Do not commit `.env`.

---

## Option B — Local one-time script

No Telegram or Supabase. Data stays in a local JSON config (gitignored).

**Passport**

```bash
cp aima_config.example.json aima_config.json
# edit aima_config.json
source .venv/bin/activate
python aima_form.py
```

**Residence permit**

```bash
cp aima_residence_config.example.json aima_residence_config.json
# edit aima_residence_config.json
source .venv/bin/activate
python aima_residence_form.py
```

The script opens the browser, emails you an MFA code, asks for it in the terminal, then submits. On success it prints the tracking URL.

---

## Known limitation: reCAPTCHA

AIMA uses **reCAPTCHA Enterprise**. MFA often works; **form submit may still fail** with:

`A validação de reCAPTCHA falhou. Tente novamente.`

That is Google scoring the automated browser, not a bad passport/email. A persistent profile (`.pw-profile`) can help a little after repeated local use, but there is no reliable fully automatic bypass in this project.

## Project layout

| File | Role |
|------|------|
| `telegram_bot.py` | Telegram conversation + Supabase sessions |
| `aima_service.py` | Shared Playwright worker (MFA + submit) |
| `aima_form.py` | One-shot passport CLI |
| `aima_residence_form.py` | One-shot residence CLI |
| `supabase_schema.sql` | Bot session table |
| `run_bot.sh` | venv + deps + start bot |
| `requirements.txt` | Python dependencies |

## License / disclaimer

This tool automates a public government web form for convenience. It may break when AIMA changes their site. Review their terms before using it for others.
