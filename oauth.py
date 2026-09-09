"""Slack user-OAuth helpers for the Jeen per-user connect app."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode


SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
SLACK_OAUTH_ACCESS_URL = "https://slack.com/api/oauth.v2.access"
DEFAULT_USER_SCOPES = [
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "users:read",
    "users:read.email",
]


class SlackOAuthError(RuntimeError):
    """Slack OAuth flow failed."""


class SlackOAuthConfig:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        user_scopes: list[str],
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.user_scopes = user_scopes


def generate_oauth_state() -> str:
    return secrets.token_urlsafe(32)


def build_authorization_url(config: SlackOAuthConfig, state: str) -> str:
    query = urlencode(
        {
            "client_id": config.client_id,
            "user_scope": ",".join(config.user_scopes),
            "redirect_uri": config.redirect_uri,
            "state": state,
        }
    )
    return f"{SLACK_AUTHORIZE_URL}?{query}"


def parse_oauth_response(data: dict) -> dict:
    if not data.get("ok"):
        raise SlackOAuthError(
            f"Slack OAuth token exchange failed: {data.get('error') or 'unknown_error'}"
        )
    authed_user = data.get("authed_user")
    if not isinstance(authed_user, dict):
        raise SlackOAuthError("Slack OAuth response is missing authed_user.")
    access_token = str(authed_user.get("access_token") or "")
    slack_user_id = str(authed_user.get("id") or "")
    if not access_token.startswith("xoxp-"):
        raise SlackOAuthError("Slack OAuth response did not include an xoxp user token.")
    if not slack_user_id:
        raise SlackOAuthError("Slack OAuth response is missing the Slack user id.")
    return {
        "access_token": access_token,
        "slack_user_id": slack_user_id,
        "scope": authed_user.get("scope", ""),
    }


class TokenStore:
    """JSON file of per-user Slack tokens. Restrict file mode; do not commit it."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict:
        if not self.path.exists():
            return {"tokens": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"tokens": {}}
        tokens = payload.get("tokens")
        return {"tokens": tokens if isinstance(tokens, dict) else {}}

    def save(self, email: str, slack_user_id: str, access_token: str) -> None:
        key = (email or "").strip().lower()
        if not key:
            raise SlackOAuthError("Cannot store a Slack token without an email.")
        payload = self._load()
        payload["tokens"][key] = {
            "email": key,
            "slack_user_id": slack_user_id,
            "access_token": access_token,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def get_by_email(self, email: str) -> dict | None:
        key = (email or "").strip().lower()
        if not key:
            return None
        found = self._load()["tokens"].get(key)
        return found if isinstance(found, dict) else None


UPSERT_SQL = """
INSERT INTO slack_user_tokens (email, slack_user_id, access_token, updated_at)
VALUES (%s, %s, %s, NOW())
ON CONFLICT (email) DO UPDATE SET
  slack_user_id = EXCLUDED.slack_user_id,
  access_token = EXCLUDED.access_token,
  updated_at = NOW()
"""
SELECT_SQL = (
    "SELECT email, slack_user_id, access_token FROM slack_user_tokens WHERE email = %s"
)


def _require_xoxp(access_token: str) -> str:
    token = (access_token or "").strip()
    if not token.startswith("xoxp-"):
        raise SlackOAuthError("Slack user token must start with xoxp-.")
    return token


def _psycopg_connect(dsn: str):
    try:
        import psycopg
    except ImportError as exc:
        raise SlackOAuthError(
            "psycopg is required when DATABASE_URL is set. pip install psycopg[binary]"
        ) from exc
    return psycopg.connect(dsn)


class PostgresTokenStore:
    """Jeen Postgres table slack_user_tokens. Inject connect= for tests."""

    def __init__(self, dsn: str, connect=None):
        self.dsn = (dsn or "").strip()
        if not self.dsn:
            raise SlackOAuthError("DATABASE_URL is empty.")
        self._connect = connect or _psycopg_connect

    def save(self, email: str, slack_user_id: str, access_token: str) -> None:
        key = (email or "").strip().lower()
        if not key:
            raise SlackOAuthError("Cannot store a Slack token without an email.")
        token = _require_xoxp(access_token)
        user_id = (slack_user_id or "").strip()
        if not user_id:
            raise SlackOAuthError("Cannot store a Slack token without a Slack user id.")
        with self._connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(UPSERT_SQL, (key, user_id, token))
            conn.commit()

    def get_by_email(self, email: str) -> dict | None:
        key = (email or "").strip().lower()
        if not key:
            return None
        with self._connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(SELECT_SQL, (key,))
                row = cur.fetchone()
        if not row:
            return None
        found_email, slack_user_id, access_token = row[0], row[1], row[2]
        token = str(access_token or "").strip()
        if not token.startswith("xoxp-"):
            return None
        return {
            "email": str(found_email or key).strip().lower(),
            "slack_user_id": str(slack_user_id or ""),
            "access_token": token,
        }
