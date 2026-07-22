# AIMA Telegram bot

This project provides:

- standalone passport and residence-permit scripts;
- a Telegram bot with both document flows;
- temporary Supabase conversation state;
- a Railway Docker deployment.

The bot asks for each field, requests the email MFA code, and submits the AIMA
request through a serialized Playwright browser worker.

## Privacy

Personal data is stored in `aima_bot_sessions` only while a request is active.
The row is deleted after submission, `/cancel`, or after a 30-minute timeout.
An expired-row cleanup job runs every 10 minutes.

The Supabase table has RLS enabled and no client policies. Only the server-side
service-role key can access it. Never expose that key publicly.

The bot also tries to delete sensitive incoming Telegram messages. This is
best-effort: Telegram may retain messages according to its own privacy policy.

## Supabase setup

1. Create a Supabase project.
2. Open **SQL Editor**.
3. Run the contents of `supabase_schema.sql`.
4. Open **Settings → API Keys** and copy the project URL and a secret
   (`sb_secret_...`) key. A legacy `service_role` key also works.

## Telegram setup

Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.

Available commands:

- `/start` — begin a passport or residence-permit request;
- `/cancel` — delete the current temporary session.

## Railway deployment

1. Put this project in a private GitHub repository.
2. Create a Railway project from that repository.
3. Add these Railway variables:

   ```text
   TELEGRAM_BOT_TOKEN=...
   SUPABASE_URL=https://YOUR_PROJECT.supabase.co
   SUPABASE_SECRET_KEY=sb_secret_...
   BROWSER_PROFILE_DIR=/data/.pw-profile
   ```

4. Add a Railway volume mounted at `/data`. This preserves the browser profile
   between deployments.
5. Deploy. Railway uses `Dockerfile` and starts `telegram_bot.py`.

Only run one replica. The persistent browser profile is intentionally processed
serially and cannot be shared safely between concurrent replicas.

## Local bot run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Export the three required secrets, then run:

```bash
python telegram_bot.py
```

On a Linux server, the included Docker image runs the visible browser inside
Xvfb.

## Important limitation

AIMA uses reCAPTCHA Enterprise. Tokens generated from hosted datacenter
browsers may receive a low score and be rejected even when the implementation
is correct. Railway deployment cannot guarantee that AIMA will accept the
automated browser. Do not attempt to bypass interactive challenges or operate
the bot at abusive volume.
