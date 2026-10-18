# Submission Review Assistant

A locally runnable app that helps an underwriter review a fictional commercial auto submission packet. It:

- extracts the business fields with a local LLM and keeps source evidence for each one
- derives the vehicle count from the CSV
- applies rules R1–R5 in deterministic code
- shows an AI-generated summary and an editable draft broker email, which is never sent

## Quick start

Requirements: Python 3.11+ and [Ollama](https://ollama.com). It was built and tested on an 8 GB Apple M1.

```bash
# 1. Local model (one-time, ~1.9 GB)
OLLAMA_NUM_PARALLEL=3 ollama serve &   # parallel slots let the documents be extracted concurrently
ollama pull qwen2.5:3b

# 2. App
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env           # optional; the defaults match .env.example
.venv/bin/uvicorn app.main:app --port 8000
```

Open http://localhost:8000. To process a packet:

1. Upload the files from one folder under `sample_data/` (`SUB-A`, `SUB-B` or `SUB-C`), or paste the broker email.
2. Click **Process packet**.

Processing a packet takes about **7–12 seconds** with `qwen2.5:3b` on an 8 GB M1, plus about 10 seconds for the summary.

**Performance notes.** About 90% of a local model call is spent generating output tokens, at around 28 tokens/s. Two changes cut processing time from 24–33 s to 7–12 s:

1. **Compact model output.** The model returns only verbatim values, about 95 tokens per document instead of 341. Code finds the evidence line itself.
2. **Concurrent extraction.** The documents in a packet are extracted at the same time (`OLLAMA_NUM_PARALLEL`).

RAG would not help here: the documents are about 500 tokens and are read in about a second.

### Tests (no model needed)

```bash
.venv/bin/python -m pytest -q
```

Result: **104 passed**. The tests cover:

- missing information
- source conflicts
- unequal vehicle counts
- the 150 / 150.0 / 150.1 mile threshold
- R1 precedence
- excerpt grounding
- CSV counting
- overrides
- packet isolation
- model-failure handling
- mail-connector safeguards (provider calls are mocked)

The tests use a stub model (`tests/conftest.py`), so they never call a live model.

## Model / provider

- **Provider:** Ollama, local. No paid API is used and no API keys are needed.
- **Model:** `qwen2.5:3b` (Qwen2.5 3B Instruct, Q4). It fits an 8 GB laptop and supports Ollama's JSON-schema structured output. You can change it with `OLLAMA_MODEL`; `qwen2.5:7b` is more accurate if you have more RAM.
- **LLM calls:**
  1. One extraction call per text document (email, each PDF).
  2. One narrative call that writes the underwriter summary and the email draft.
- Temperature is 0 for all calls.

## Architecture

```
Browser (app/static: vanilla HTML/JS)        holds the current packet only; reset on every new packet
   │  POST /api/process   (multipart files + pasted email)
   │  POST /api/evaluate  (fields + overrides)  -> rerun rules, no model call
   │  POST /api/narrative (fields + overrides)  -> AI summary + draft email
FastAPI (app/main.py)                         stateless, no applicant data stored server-side
   ├─ documents.py   ingest: PDF text (pypdf layout mode), email text, CSV; deterministic schedule count
   ├─ extraction.py  LLM extraction -> raw value + verbatim excerpt per field; code-side grounding check
   ├─ normalize.py   deterministic typing/normalization (dates, CA, numbers, Yes/No)
   ├─ merge.py       per-field state KNOWN / MISSING / CONFLICT / INVALID; attachment classification; loss runs
   ├─ rules.py       R1–R5 + overall status, pure functions
   ├─ pipeline.py    orchestration, human overrides, broker request items
   ├─ mail.py        optional Gmail / Outlook send connectors (off by default)
   ├─ narrative.py   AI summary + email draft (labelled template fallback if the model fails)
   └─ llm.py         Ollama /api/chat client with JSON-schema output and explicit error states
```

### Key design decisions

- **The LLM extracts; code decides.**
  - The model returns only the verbatim value for each field, `"NOT PROVIDED"`, or `null` for not mentioned. The schema keys mirror the documents' own labels ("Requested effective date"), which a small model maps more reliably than snake_case names. Code maps them back to the shared field names.
  - **Targeted retry.** If the model returns `null` for a field whose label clearly appears in the document, a second, small call asks for just those fields. This recovers occasional small-model misses without code ever extracting values itself.
  - Code then normalizes each value and detects conflicts.
  - Code also evaluates the rules, so the rule outcomes are deterministic.
- **Grounding.**
  - Code locates the evidence for every model value: the labelled source line that contains the value as a whole word, or, in free text, the single line that contains it.
  - **Prose Yes/No answers:** "They do not haul hazardous materials" supports "No", and "They transport hazardous materials" supports "Yes". "None", "Not provided" and "unknown" are never read as "No" (the spec says absence is not No).
  - **Reworded operations descriptions** are matched to the closest source line, which needs at least 75% word overlap.
  - **Normalization:** places like "Sacramento, CA" normalize to the state, and counts like "4 box trucks" to 4.
  - **Rejected values:** a value that cannot be located is shown with a warning but never fed to the rules, so hallucinated values cannot affect results.
  - **"Not provided" answers** are only recorded if the document actually has a "Not provided" line for that field.
  - **Free-text check:** verified live on three extra, made-up free-text broker emails (not included in the repo), plus the supplied samples.
- **Evidence.** Each value records the source file, a locator (`email body`, `PDF page N`, `CSV data rows 1-3 (header excluded)`, `uploaded attachment inventory`) and an excerpt or observation.
  - **Missing values** record the sources checked and any explicit "Not provided" line.
- **Conflicts.**
  - When the email and PDF give explicit values that still differ after normalization, the field becomes `CONFLICT` with value `null`, and all alternatives and their evidence are kept.
  - Harmless differences (150 vs 150.0, California vs CA, letter case in names) are not conflicts.
  - A source that doesn't mention a field is not a conflict.
- **Loss runs.**
  - Determined only from the actual attachments: first from the document's title text, then from the model's document-type classification.
  - An email statement about loss runs (located by code) is shown but doesn't count as an attachment.
  - An unreadable or unclassifiable PDF gives `INVALID` (R5 UNKNOWN).
- **Overrides.**
  - Each override is validated and normalized by the same code as extracted values.
  - Overridden fields are labelled `HUMAN OVERRIDE`.
  - The original state, value and evidence are kept and shown.
  - **Rerun checks** re-evaluates the rules without calling the model.
- **Model failures.**
  - Failures are shown in the UI and never replaced by simulated output.
  - The deterministic results remain available: the CSV count, the attachment inventory and loss-runs detection by title.
  - A field whose source couldn't be extracted is `INVALID` ("not determinable"), not `MISSING`, so it doesn't falsely trigger an absence FLAG.
  - If the narrative fails, the UI shows the error and a draft built from a template, clearly labelled as *not* AI-generated.
- **Prompt injection.**
  - Document text is wrapped in `<document>` tags, and the system prompt marks it as untrusted data. Rules never depend on model judgement.
  - Lines that address an AI ("ignore previous instructions", "system note to AI", ...) are detected in code. Values taken from those lines are rejected, even though they appear verbatim, and the UI shows a warning that names the line.
  - Tested live: an email with an injected instruction to set hazmat to "No", radius to 50 and a date did not change the rules. Without this guard it had turned R1, R3 and R4 to PASS.
- **Packet isolation.**
  - The server stores no applicant state.
  - The UI clears all results before each new packet and ignores late responses from an earlier packet.

## Optional: send the reviewed draft (Gmail / Outlook)

The brief requires draft-only behaviour, so **sending is disabled by default**. With `ENABLE_EMAIL_SEND=false`, the UI shows the draft only, every send endpoint refuses requests, and the app works exactly as the brief specifies.

When it is enabled, the reviewer:

1. Connects Gmail or Outlook in a popup sign-in window, using OAuth 2.0 authorization code with PKCE. Only the send permission is requested: `gmail.send` or Graph `Mail.Send`.
2. Reviews and edits the draft. The **To** field is pre-filled from the broker email's `From:` line.
3. Clicks **Review & send…**.
4. Sees a confirmation panel showing the sender, recipients, subject and body, and clicks **Confirm & send**.

Safeguards:

- **Confirmation is enforced on the server too.** Requests without `confirmed: true` are rejected.
- **Nothing is ever sent automatically.**
- **One send per packet.** The Send button is disabled after a successful send for the current packet.
- **Audit log.** Each send records the sender, recipients, subject, packet id and time, and is shown in the UI.
- **Tokens stay in server memory.** They are never written to disk or exposed to the browser, which only holds an HttpOnly session cookie. Restarting the server disconnects all accounts.

Setup:

1. **Google:** in Google Cloud Console, enable the Gmail API, create an OAuth client of type *Web application* with redirect URI `http://localhost:8000/auth/google/callback`, and add yourself as a test user.
2. **Microsoft:** in Entra ID, create an app registration with platform *Web* and redirect URI `http://localhost:8000/auth/microsoft/callback`. Create a client secret and add the delegated `Mail.Send` permission.
3. Put the client IDs and secrets in `.env`, set `ENABLE_EMAIL_SEND=true`, and restart the app.

The sample broker addresses use `example.com`, so a real send to them will not be delivered. Change the **To** field to your own address to test.

## Expected results on the supplied samples (verified with the live model)

| Packet | R1 | R2 | R3 | R4 | R5 | Overall |
|---|---|---|---|---|---|---|
| SUB-A | PASS | PASS (3 = 3) | PASS (150) | PASS | PASS (loss_runs.pdf, 0 claims) | READY_FOR_REVIEW |
| SUB-B | FLAG (no effective date) | PASS (2 = 2) | UNKNOWN (missing) | UNKNOWN (missing) | FLAG (only promised) | NEEDS_REVIEW |
| SUB-C | PASS | FLAG (4 declared vs 3 rows) | UNKNOWN (email 180 vs PDF 80) | UNKNOWN (email Yes vs PDF No) | FLAG | NEEDS_REVIEW |

## Known limitations

- **Small-model accuracy.** Extraction quality is limited by a 3B model. Code compensates with label-style keys, a targeted retry for skipped fields, and grounding of every value. Free text where a value appears on several lines without a label, or ambiguous phrasing such as "3 tractors and 3 trailers", is deliberately not resolved. In that case the field shows MISSING or INVALID with the sources checked, and the reviewer can override it. A larger model (`qwen2.5:7b`) is a one-line config change.
- **Operations description.** Two descriptions are compared after lowercasing and removing punctuation, so the same meaning in different words would show as a CONFLICT. No rule depends on this field.
- **Loss runs locator.** Detected loss-runs reports are cited as "PDF page 1" (the title page).
- **Multiple vehicle schedules.** If several schedule CSVs disagree, the field is a CONFLICT; their counts are not summed.
- **Mail connectors:** tested with mocked provider APIs only; a live send needs your own OAuth app registrations. Connected accounts and the send log are in-memory, single-user and lost on restart. There are no HTML emails, attachments or CC.
- **Not implemented:** no OCR (out of scope), no bounding-box viewer, no persistence, no authentication. Overrides live in the browser session for the current packet.
- **Injection detection is pattern-based.** Novel phrasings may not be caught. When a value is rejected because of an injected line, the field becomes INVALID (rule UNKNOWN), even if a legitimate "Not provided" line would have made it MISSING. This is the safe direction, and the reviewer sees the warning.
- **UI testing.** The UI was smoke-tested manually in a desktop browser; there are no automated browser tests.

## AI coding tools used

Claude Code (Claude Opus 5.5) assisted with scaffolding, implementation and tests. All code has been reviewed and can be explained and modified by the author.

## Time spent

_Fill in before submission._

## Data

`sample_data/` contains the supplied fictional packets, unchanged. `docs/` contains the supplied `field_specification.json` and `rules.json`.
