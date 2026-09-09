# Slack connection

Per-user Slack OAuth for Jeen. Each seller opens `/connect` once. The app stores
their `xoxp-` token. Langflow later calls `/token?email=...` with
`X-App-Secret`.

## Local

```bash
cp .env.example .env
# fill SLACK_CLIENT_ID, SLACK_CLIENT_SECRET, TOKEN_LOOKUP_SECRET
python3 -m pip install -r requirements.txt
set -a && source .env && set +a
python3 app.py
```

Open http://localhost:8765/connect

## Render

1. New Web Service from this repo.
2. Start command: `python3 app.py` (or use the Procfile).
3. Environment:

   - `HOST=0.0.0.0`
   - `SLACK_CLIENT_ID`
   - `SLACK_CLIENT_SECRET`
   - `SLACK_REDIRECT_URI=https://YOUR-SERVICE.onrender.com/callback`
   - `TOKEN_LOOKUP_SECRET` (long random string)
   - leave `PORT` unset

4. In Slack → OAuth & Permissions → Redirect URLs, add that callback URL.
5. Sellers open `https://YOUR-SERVICE.onrender.com/connect`.

Do not commit `.env` or `tokens.json`. Render disk is ephemeral; tokens can
disappear on redeploy until you store them in a database.


## Postgres

Set `DATABASE_URL` to Jeen Postgres. Run `schema.sql` once:

```sql
CREATE TABLE IF NOT EXISTS slack_user_tokens (
  email          TEXT PRIMARY KEY,
  slack_user_id  TEXT NOT NULL,
  access_token   TEXT NOT NULL,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Render `/callback` upserts the row. Langflow reads the same table by email.
Local without Postgres: `ALLOW_FILE_TOKEN_STORE=1`.
