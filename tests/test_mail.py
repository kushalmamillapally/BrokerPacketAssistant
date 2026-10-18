"""Mail connector safeguards. Provider HTTP calls are mocked; nothing is ever sent."""
import base64
import email
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app import mail
from app.main import app


class FakeResponse:
    def __init__(self, status_code, data=None):
        self.status_code, self._data, self.text = status_code, data or {}, str(data)

    def json(self):
        return self._data


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_EMAIL_SEND", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("MS_CLIENT_ID", "test-client")
    monkeypatch.setenv("MS_CLIENT_SECRET", "test-secret")


@pytest.fixture
def posts(monkeypatch):
    """Record outgoing provider requests instead of performing them."""
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if "token" in url:
            return FakeResponse(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
        if "gmail" in url:
            return FakeResponse(200, {"id": "msg-1"})
        return FakeResponse(202)

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def connected_session(provider="google"):
    s = mail.Session()
    s.connections[provider] = mail.Connection("at", "rt", time.time() + 3600, "reviewer@example.com")
    return s


def test_sending_disabled_by_default(monkeypatch, posts):
    monkeypatch.delenv("ENABLE_EMAIL_SEND", raising=False)
    with pytest.raises(mail.MailError, match="disabled"):
        mail.send("google", connected_session(), "broker@example.com", "S", "B", confirmed=True)
    assert posts == []


def test_send_requires_explicit_confirmation(enabled, posts):
    with pytest.raises(mail.MailError, match="confirmed"):
        mail.send("google", connected_session(), "broker@example.com", "S", "B", confirmed=False)
    assert posts == []


def test_send_requires_connection(enabled, posts):
    with pytest.raises(mail.MailError, match="not connected"):
        mail.send("google", mail.Session(), "broker@example.com", "S", "B", confirmed=True)


@pytest.mark.parametrize("to", ["", "not-an-email", "a@example.com; bad"])
def test_invalid_recipients_rejected(enabled, posts, to):
    with pytest.raises(mail.MailError):
        mail.send("google", connected_session(), to, "S", "B", confirmed=True)
    assert posts == []


def test_gmail_send_builds_rfc822_message(enabled, posts):
    s = connected_session("google")
    record = mail.send("google", s, "broker@example.com", "Missing info", "Hello,\n- item", confirmed=True, packet_id="p1")
    url, kwargs = posts[0]
    assert url.endswith("/users/me/messages/send")
    msg = email.message_from_bytes(base64.urlsafe_b64decode(kwargs["json"]["raw"]))
    assert msg["To"] == "broker@example.com" and msg["Subject"] == "Missing info"
    assert "- item" in msg.get_payload()
    assert kwargs["headers"]["Authorization"] == "Bearer at"
    assert record["message_id"] == "msg-1" and s.sent_log[-1]["packet_id"] == "p1"


def test_outlook_send_uses_graph_sendmail(enabled, posts):
    mail.send("microsoft", connected_session("microsoft"), "a@example.com, b@example.com", "S", "Body", confirmed=True)
    url, kwargs = posts[0]
    assert url == "https://graph.microsoft.com/v1.0/me/sendMail"
    message = kwargs["json"]["message"]
    assert [r["emailAddress"]["address"] for r in message["toRecipients"]] == ["a@example.com", "b@example.com"]
    assert message["body"] == {"contentType": "Text", "content": "Body"}


def test_authorization_url_uses_pkce_and_state(enabled):
    s = mail.Session()
    url = mail.authorization_url("google", s)
    q = parse_qs(urlparse(url).query)
    assert q["code_challenge_method"] == ["S256"] and q["state"][0] in s.pending
    assert "gmail.send" in q["scope"][0]
    assert q["redirect_uri"] == ["http://localhost:8000/auth/google/callback"]


def test_callback_rejects_unknown_state(enabled, posts):
    with pytest.raises(mail.MailError, match="state"):
        mail.complete_authorization("google", mail.Session(), "forged-state", "code")
    assert posts == []


def test_full_authorization_flow(enabled, posts):
    s = mail.Session()
    state = parse_qs(urlparse(mail.authorization_url("microsoft", s)).query)["state"][0]
    conn = mail.complete_authorization("microsoft", s, state, "auth-code")
    assert conn.access_token == "at" and "microsoft" in s.connections
    assert posts[0][1]["data"]["code_verifier"]          # PKCE verifier sent with the code
    assert state not in s.pending                         # state is single-use


def test_expired_token_is_refreshed(enabled, posts):
    s = connected_session("google")
    s.connections["google"].expires_at = 0
    mail.send("google", s, "broker@example.com", "S", "B", confirmed=True)
    assert posts[0][1]["data"]["grant_type"] == "refresh_token"


def test_api_send_blocked_when_disabled(monkeypatch):
    monkeypatch.delenv("ENABLE_EMAIL_SEND", raising=False)
    client = TestClient(app)
    assert client.get("/api/mail/status").json()["send_enabled"] is False
    r = client.post("/api/mail/send", json={"provider": "google", "to": "b@example.com", "subject": "S",
                                            "body": "B", "confirmed": True})
    assert r.status_code == 400 and "disabled" in r.json()["detail"]
    assert client.get("/auth/google/start", follow_redirects=False).status_code == 403
