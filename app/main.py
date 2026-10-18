"""FastAPI app: serves the single-page UI and three JSON endpoints. Holds no applicant state."""
from __future__ import annotations

import html
import json
import os
from pathlib import Path

from fastapi import Cookie, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import mail, narrative
from .llm import OllamaClient
from .pipeline import OverrideError, evaluate_packet, process_packet

STATIC = Path(__file__).parent / "static"


def _load_dotenv(path: Path = Path(__file__).parent.parent / ".env") -> None:
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"'))


_load_dotenv()
app = FastAPI(title="Submission Review Assistant")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def get_llm() -> OllamaClient:
    return OllamaClient()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict:
    return get_llm().health()


@app.post("/api/process")
async def process(files: list[UploadFile] = File(default=[]), pasted_email: str = Form(default="")) -> dict:
    payload = [(f.filename or "unnamed", await f.read()) for f in files if f.filename]
    try:
        packet = process_packet(payload, pasted_email, get_llm())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    packet["evaluation"] = evaluate_packet(packet["fields"])
    return packet


class EvaluateRequest(BaseModel):
    fields: dict
    overrides: dict = {}


@app.post("/api/evaluate")
def evaluate(req: EvaluateRequest) -> dict:
    try:
        return evaluate_packet(req.fields, req.overrides)
    except OverrideError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


class NarrativeRequest(BaseModel):
    fields: dict
    overrides: dict = {}
    inventory: list = []


@app.post("/api/narrative")
def make_narrative(req: NarrativeRequest) -> dict:
    try:
        evaluation = evaluate_packet(req.fields, req.overrides)
    except OverrideError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return narrative.generate(evaluation, req.inventory, get_llm())


# ---------- optional mail connectors (disabled unless ENABLE_EMAIL_SEND=true) ----------

SESSION_COOKIE = "review_session"


def _with_cookie(response, session_id: str):
    response.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="lax")
    return response


@app.get("/api/mail/status")
def mail_status(review_session: str | None = Cookie(default=None)):
    sid, session = mail.get_session(review_session)
    return _with_cookie(JSONResponse(mail.status(session)), sid)


@app.get("/auth/{provider}/start")
def mail_connect(provider: str, review_session: str | None = Cookie(default=None)):
    sid, session = mail.get_session(review_session)
    if not mail.send_enabled():
        raise HTTPException(status_code=403, detail="Sending is disabled (draft-only mode)")
    try:
        url = mail.authorization_url(provider, session)
    except mail.MailError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _with_cookie(RedirectResponse(url), sid)


def _popup_result(message: str, ok: bool) -> HTMLResponse:
    """Sign-in runs in a popup so the review in the main window is not lost."""
    payload = json.dumps({"type": "mail-auth", "ok": ok, "message": message}).replace("<", "\\u003c")
    text = html.escape(message)
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8"><title>Mail connection</title>
<p style="font-family:sans-serif">{text}</p><p style="font-family:sans-serif">You can close this window.</p>
<script>if (window.opener) {{ window.opener.postMessage({payload}, window.location.origin); window.close(); }}</script>""")


@app.get("/auth/{provider}/callback")
def mail_callback(provider: str, state: str = "", code: str = "", error: str = "",
                  review_session: str | None = Cookie(default=None)):
    sid, session = mail.get_session(review_session)
    if error or not code:
        return _with_cookie(_popup_result(f"Sign-in failed: {error or 'cancelled'}", False), sid)
    try:
        conn = mail.complete_authorization(provider, session, state, code)
    except mail.MailError as exc:
        return _with_cookie(_popup_result(str(exc), False), sid)
    return _with_cookie(_popup_result(f"Connected {mail.PROVIDERS[provider].label} as {conn.account}", True), sid)


@app.post("/auth/{provider}/disconnect")
def mail_disconnect(provider: str, review_session: str | None = Cookie(default=None)):
    sid, session = mail.get_session(review_session)
    session.connections.pop(provider, None)
    return _with_cookie(JSONResponse(mail.status(session)), sid)


class SendRequest(BaseModel):
    provider: str
    to: str
    subject: str
    body: str
    confirmed: bool = False
    packet_id: str | None = None


@app.post("/api/mail/send")
def mail_send(req: SendRequest, review_session: str | None = Cookie(default=None)):
    sid, session = mail.get_session(review_session)
    try:
        record = mail.send(req.provider, session, req.to, req.subject, req.body, req.confirmed, req.packet_id)
    except mail.MailError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _with_cookie(JSONResponse(record), sid)
