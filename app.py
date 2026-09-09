"""Jeen Slack per-user connect app.

Each person opens /connect, signs in with Slack, and we store their xoxp token.
The Langflow Slack tool reads that token from Jeen Postgres by email.

Run:
  export SLACK_CLIENT_ID=...
  export SLACK_CLIENT_SECRET=...
  export SLACK_REDIRECT_URI=http://localhost:8765/callback
  export DATABASE_URL=postgresql://user:pass@host:5432/dbname
  python3 app.py

Create the Slack app from slack_manifest.yaml at https://api.slack.com/apps
→ Create New App → From an app manifest.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from oauth import (
    DEFAULT_USER_SCOPES,
    PostgresTokenStore,
    SLACK_OAUTH_ACCESS_URL,
    SlackOAuthConfig,
    SlackOAuthError,
    TokenStore,
    build_authorization_url,
    generate_oauth_state,
    parse_oauth_response,
)


TOKEN_FILE = HERE / "tokens.json"
PENDING_STATES: set[str] = set()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def load_token_store():
    dsn = _env("DATABASE_URL")
    if dsn:
        return PostgresTokenStore(dsn)
    if _env("ALLOW_FILE_TOKEN_STORE") == "1":
        return TokenStore(TOKEN_FILE)
    raise SlackOAuthError(
        "DATABASE_URL is required. Set it to Jeen Postgres, or set "
        "ALLOW_FILE_TOKEN_STORE=1 for a local tokens.json fallback."
    )


def load_config() -> SlackOAuthConfig:
    client_id = _env("SLACK_CLIENT_ID")
    client_secret = _env("SLACK_CLIENT_SECRET")
    redirect_uri = _env("SLACK_REDIRECT_URI", "http://localhost:8765/callback")
    missing = [n for n, v in (
        ("SLACK_CLIENT_ID", client_id),
        ("SLACK_CLIENT_SECRET", client_secret),
    ) if not v]
    if missing:
        raise SlackOAuthError("Missing " + ", ".join(missing))
    return SlackOAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        user_scopes=DEFAULT_USER_SCOPES,
    )


def exchange_code(config: SlackOAuthConfig, code: str) -> dict:
    response = httpx.post(
        SLACK_OAUTH_ACCESS_URL,
        data={
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "code": code,
            "redirect_uri": config.redirect_uri,
        },
        timeout=30,
    )
    response.raise_for_status()
    return parse_oauth_response(response.json())


def slack_user_email(user_token: str, slack_user_id: str) -> str:
    response = httpx.get(
        "https://slack.com/api/users.info",
        params={"user": slack_user_id},
        headers={"Authorization": f"Bearer {user_token}"},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise SlackOAuthError(f"users.info failed: {data.get('error')}")
    profile = (data.get("user") or {}).get("profile") or {}
    email = (profile.get("email") or "").strip().lower()
    if not email:
        raise SlackOAuthError(
            "Slack did not return an email. Add the users:read.email user scope."
        )
    return email


def html_page(title: str, body: str, status: int = 200) -> tuple[int, bytes]:
    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: Arial, sans-serif; max-width: 40rem; margin: 4rem auto; color: #111; }}
a.button {{ display: inline-block; background: #4A154B; color: #fff; padding: 0.75rem 1.2rem;
  text-decoration: none; border-radius: 6px; }}
</style></head><body>{body}</body></html>"""
    return status, page.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print("%s - %s" % (self.address_string(), format % args))

    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send(status, body, "application/json")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path == "/":
            status, body = html_page(
                "Jeen Slack connect",
                "<h1>Connect Slack</h1>"
                "<p>Each Jeen user signs in once. The Langflow tool then reads "
                "<strong>that user's</strong> channels.</p>"
                '<p><a class="button" href="/connect">Connect my Slack</a></p>',
            )
            self._send(status, body)
            return

        if path == "/connect":
            try:
                config = load_config()
            except SlackOAuthError as exc:
                status, body = html_page("Config error", f"<p>{exc}</p>", 500)
                self._send(status, body)
                return
            state = generate_oauth_state()
            PENDING_STATES.add(state)
            self.send_response(302)
            self.send_header("Location", build_authorization_url(config, state))
            self.end_headers()
            return

        if path == "/callback":
            error = (query.get("error") or [""])[0]
            if error:
                status, body = html_page("Denied", f"<p>Slack said: {error}</p>", 400)
                self._send(status, body)
                return
            state = (query.get("state") or [""])[0]
            code = (query.get("code") or [""])[0]
            if state not in PENDING_STATES:
                status, body = html_page("Invalid state", "<p>Retry from /connect.</p>", 400)
                self._send(status, body)
                return
            PENDING_STATES.discard(state)
            try:
                config = load_config()
                parsed_token = exchange_code(config, code)
                email = slack_user_email(
                    parsed_token["access_token"], parsed_token["slack_user_id"]
                )
                load_token_store().save(
                    email=email,
                    slack_user_id=parsed_token["slack_user_id"],
                    access_token=parsed_token["access_token"],
                )
            except Exception as exc:
                status, body = html_page("Connect failed", f"<p>{exc}</p>", 400)
                self._send(status, body)
                return
            status, body = html_page(
                "Connected",
                f"<h1>Slack connected</h1><p>Saved for <strong>{email}</strong>. "
                "You can close this tab. The Langflow agent will use your channels.</p>",
            )
            self._send(status, body)
            return

        if path == "/token":
            expected = _env("TOKEN_LOOKUP_SECRET")
            provided = self.headers.get("X-App-Secret", "")
            if not expected or provided != expected:
                self._send_json(401, {"error": "Unauthorized token lookup"})
                return
            email = (query.get("email") or [""])[0]
            found = load_token_store().get_by_email(email)
            if not found:
                self._send_json(404, {"error": f"No Slack token for {email}. Open /connect."})
                return
            self._send_json(
                200,
                {
                    "access_token": found["access_token"],
                    "slack_user_id": found["slack_user_id"],
                    "email": found["email"],
                },
            )
            return

        status, body = html_page("Not found", "<p>Not found.</p>", 404)
        self._send(status, body)


def main() -> None:
    host = _env("HOST", "127.0.0.1")
    port = int(_env("PORT") or "8765")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Jeen Slack connect app on http://{host}:{port}/connect")
    server.serve_forever()


if __name__ == "__main__":
    main()
