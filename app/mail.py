"""Optional Gmail / Outlook send connectors.

Disabled unless ENABLE_EMAIL_SEND=true. Sending is never automatic: it requires an explicit,
confirmed request from the reviewer. OAuth tokens are held in process memory only.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from urllib.parse import urlencode

import httpx


class MailError(Exception):
    pass


@dataclass
class Provider:
    key: str
    label: str
    auth_url: str
    token_url: str
    scopes: str
    client_id_env: str
    client_secret_env: str


def _ms_tenant() -> str:
    return os.getenv("MS_TENANT", "common")


PROVIDERS = {
    "google": Provider(
        "google", "Gmail",
        "https://accounts.google.com/o/oauth2/v2/auth", "https://oauth2.googleapis.com/token",
        "openid email https://www.googleapis.com/auth/gmail.send",
        "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
    "microsoft": Provider(
        "microsoft", "Outlook",
        "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        "openid email profile offline_access https://graph.microsoft.com/Mail.Send",
        "MS_CLIENT_ID", "MS_CLIENT_SECRET"),
}

EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def send_enabled() -> bool:
    return os.getenv("ENABLE_EMAIL_SEND", "false").strip().lower() in ("1", "true", "yes")


def base_url() -> str:
    return os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")


def redirect_uri(provider: str) -> str:
    return f"{base_url()}/auth/{provider}/callback"


def configured(provider: str) -> bool:
    p = PROVIDERS[provider]
    return bool(os.getenv(p.client_id_env) and os.getenv(p.client_secret_env))


@dataclass
class Connection:
    access_token: str
    refresh_token: str | None
    expires_at: float
    account: str


@dataclass
class Session:
    pending: dict[str, dict] = field(default_factory=dict)       # state -> {provider, verifier}
    connections: dict[str, Connection] = field(default_factory=dict)
    sent_log: list[dict] = field(default_factory=list)


# Single-user local app: sessions live in memory and vanish on restart.
SESSIONS: dict[str, Session] = {}


def get_session(session_id: str | None) -> tuple[str, Session]:
    if session_id and session_id in SESSIONS:
        return session_id, SESSIONS[session_id]
    new_id = secrets.token_urlsafe(24)
    SESSIONS[new_id] = Session()
    return new_id, SESSIONS[new_id]


def _endpoint(url: str) -> str:
    return url.format(tenant=_ms_tenant())


def authorization_url(provider: str, session: Session) -> str:
    if provider not in PROVIDERS:
        raise MailError(f"Unknown provider '{provider}'")
    if not configured(provider):
        p = PROVIDERS[provider]
        raise MailError(f"{p.label} is not configured: set {p.client_id_env} and {p.client_secret_env}")
    p = PROVIDERS[provider]
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    session.pending[state] = {"provider": provider, "verifier": verifier}
    params = {
        "client_id": os.getenv(p.client_id_env), "response_type": "code", "redirect_uri": redirect_uri(provider),
        "scope": p.scopes, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    }
    if provider == "google":
        params.update({"access_type": "offline", "prompt": "consent"})
    return f"{_endpoint(p.auth_url)}?{urlencode(params)}"


def _account_from_id_token(id_token: str | None) -> str:
    """Display-only: the id_token came straight from the provider's token endpoint over TLS."""
    if not id_token:
        return "connected account"
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims.get("email") or claims.get("preferred_username") or "connected account"
    except (IndexError, ValueError):
        return "connected account"


def _token_request(provider: str, data: dict) -> dict:
    p = PROVIDERS[provider]
    data = {**data, "client_id": os.getenv(p.client_id_env), "client_secret": os.getenv(p.client_secret_env)}
    try:
        resp = httpx.post(_endpoint(p.token_url), data=data, timeout=20)
    except httpx.HTTPError as exc:
        raise MailError(f"{p.label} token request failed: {exc}") from exc
    if resp.status_code != 200:
        raise MailError(f"{p.label} token request rejected (HTTP {resp.status_code}): {resp.text[:200]}")
    return resp.json()


def complete_authorization(provider: str, session: Session, state: str, code: str) -> Connection:
    pending = session.pending.pop(state, None)
    if not pending or pending["provider"] != provider:
        raise MailError("Invalid or expired sign-in state; please connect again")
    tokens = _token_request(provider, {"grant_type": "authorization_code", "code": code,
                                       "redirect_uri": redirect_uri(provider), "code_verifier": pending["verifier"]})
    conn = Connection(tokens["access_token"], tokens.get("refresh_token"),
                      time.time() + int(tokens.get("expires_in", 3600)) - 60,
                      _account_from_id_token(tokens.get("id_token")))
    session.connections[provider] = conn
    return conn


def _access_token(provider: str, session: Session) -> str:
    conn = session.connections.get(provider)
    if not conn:
        raise MailError(f"{PROVIDERS[provider].label} is not connected")
    if time.time() >= conn.expires_at:
        if not conn.refresh_token:
            session.connections.pop(provider, None)
            raise MailError(f"{PROVIDERS[provider].label} session expired; please connect again")
        tokens = _token_request(provider, {"grant_type": "refresh_token", "refresh_token": conn.refresh_token})
        conn.access_token = tokens["access_token"]
        conn.refresh_token = tokens.get("refresh_token", conn.refresh_token)
        conn.expires_at = time.time() + int(tokens.get("expires_in", 3600)) - 60
    return conn.access_token


def parse_recipients(raw: str) -> list[str]:
    recipients = [r.strip() for r in re.split(r"[,;]", raw or "") if r.strip()]
    if not recipients:
        raise MailError("At least one recipient is required")
    bad = [r for r in recipients if not EMAIL_RE.match(r)]
    if bad:
        raise MailError(f"Invalid recipient address: {', '.join(bad)}")
    return recipients


def gmail_payload(to: list[str], subject: str, body: str) -> dict:
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    return {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}


def graph_payload(to: list[str], subject: str, body: str) -> dict:
    return {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        },
        "saveToSentItems": True,
    }


def send(provider: str, session: Session, to_raw: str, subject: str, body: str, confirmed: bool,
         packet_id: str | None = None) -> dict:
    if not send_enabled():
        raise MailError("Sending is disabled (draft-only mode). Set ENABLE_EMAIL_SEND=true to enable.")
    if provider not in PROVIDERS:
        raise MailError(f"Unknown provider '{provider}'")
    if not confirmed:
        raise MailError("Send must be explicitly confirmed by the reviewer")
    if not subject.strip() or not body.strip():
        raise MailError("Subject and body are required")
    to = parse_recipients(to_raw)
    token = _access_token(provider, session)
    headers = {"Authorization": f"Bearer {token}"}

    try:
        if provider == "google":
            resp = httpx.post("https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                              json=gmail_payload(to, subject, body), headers=headers, timeout=30)
            ok, message_id = resp.status_code == 200, (resp.json().get("id") if resp.status_code == 200 else None)
        else:
            resp = httpx.post("https://graph.microsoft.com/v1.0/me/sendMail",
                              json=graph_payload(to, subject, body), headers=headers, timeout=30)
            ok, message_id = resp.status_code == 202, None
    except httpx.HTTPError as exc:
        raise MailError(f"{PROVIDERS[provider].label} send failed: {exc}") from exc
    if not ok:
        raise MailError(f"{PROVIDERS[provider].label} rejected the send (HTTP {resp.status_code}): {resp.text[:200]}")

    record = {"provider": PROVIDERS[provider].label, "from": session.connections[provider].account, "to": to,
              "subject": subject, "packet_id": packet_id, "message_id": message_id,
              "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    session.sent_log.append(record)
    return record


def status(session: Session) -> dict:
    return {
        "send_enabled": send_enabled(),
        "providers": {
            key: {"label": p.label, "configured": configured(key),
                  "connected": key in session.connections,
                  "account": session.connections[key].account if key in session.connections else None}
            for key, p in PROVIDERS.items()
        },
        "sent_log": session.sent_log[-10:],
    }
