CREATE TABLE IF NOT EXISTS slack_user_tokens (
  email          TEXT PRIMARY KEY,
  slack_user_id  TEXT NOT NULL,
  access_token   TEXT NOT NULL,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
