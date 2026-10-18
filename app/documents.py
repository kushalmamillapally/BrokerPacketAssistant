"""Ingest uploaded packet files into readable source documents, and count the CSV schedule."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from pypdf import PdfReader

MAX_FILE_BYTES = 10 * 1024 * 1024
EMAIL_EXTENSIONS = {".txt", ".eml", ".msg.txt"}
SCHEDULE_HEADER = ["vehicle_id", "vin", "year", "make", "model"]


@dataclass
class SourceDoc:
    filename: str
    kind: str  # "email" | "pdf" | "csv" | "unsupported"
    size: int
    readable: bool = True
    pages: list[str] = field(default_factory=list)
    error: str | None = None

    def locator(self, page_index: int) -> str:
        return f"PDF page {page_index + 1}" if self.kind == "pdf" else "email body"

    def inventory_entry(self) -> dict:
        return {
            "filename": self.filename,
            "kind": self.kind,
            "size_bytes": self.size,
            "readable": self.readable,
            "pages": len(self.pages) if self.kind == "pdf" else None,
            "error": self.error,
        }


def _clean_pdf_text(text: str) -> str:
    # Layout extraction keeps table rows on one line; collapse padding and blank lines.
    lines = [re.sub(r"[ \t]{2,}", "  ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def read_pdf(data: bytes) -> tuple[list[str], str | None]:
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [_clean_pdf_text(p.extract_text(extraction_mode="layout") or "") for p in reader.pages]
    except Exception as exc:  # pypdf raises many exception types for corrupt files
        return [], f"PDF could not be parsed: {exc.__class__.__name__}"
    if not any(pages):
        return pages, "PDF contains no selectable text"
    return pages, None


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _unique_name(name: str, seen: set[str]) -> str:
    candidate, n = name, 2
    while candidate in seen:
        stem, dot, ext = name.rpartition(".")
        candidate = f"{stem} ({n}).{ext}" if dot else f"{name} ({n})"
        n += 1
    seen.add(candidate)
    return candidate


def ingest(files: list[tuple[str, bytes]], pasted_email: str | None = None) -> list[SourceDoc]:
    docs: list[SourceDoc] = []
    seen: set[str] = set()
    if pasted_email and pasted_email.strip():
        name = _unique_name("pasted_email.txt", seen)
        docs.append(SourceDoc(name, "email", len(pasted_email.encode()), pages=[pasted_email.strip()]))

    for raw_name, data in files:
        name = _unique_name(raw_name.replace("\\", "/").rsplit("/", 1)[-1] or "unnamed", seen)
        lower = name.lower()
        if len(data) > MAX_FILE_BYTES:
            docs.append(SourceDoc(name, "unsupported", len(data), readable=False, error="file exceeds 10 MB limit"))
        elif lower.endswith(".pdf"):
            pages, error = read_pdf(data)
            docs.append(SourceDoc(name, "pdf", len(data), readable=error is None, pages=pages, error=error))
        elif lower.endswith(".csv"):
            docs.append(SourceDoc(name, "csv", len(data), pages=[_decode(data)]))
        elif any(lower.endswith(ext) for ext in EMAIL_EXTENSIONS):
            text = _decode(data).strip()
            docs.append(SourceDoc(name, "email", len(data), readable=bool(text), pages=[text],
                                  error=None if text else "empty text file"))
        else:
            docs.append(SourceDoc(name, "unsupported", len(data), readable=False,
                                  error="unsupported file type (expected .pdf, .csv, .txt or .eml)"))
    return docs


@dataclass
class ScheduleCount:
    filename: str
    ok: bool
    count: int | None = None
    locator: str = ""
    observation: str = ""
    error: str | None = None


def count_schedule(doc: SourceDoc) -> ScheduleCount:
    """Count nonblank vehicle records after the header. No VIN decoding or dedup (per spec)."""
    text = doc.pages[0] if doc.pages else ""
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        return ScheduleCount(doc.filename, False, error=f"CSV could not be parsed: {exc}")
    if not rows:
        return ScheduleCount(doc.filename, False, error="CSV is empty (no header)")

    header = [c.strip().lower() for c in rows[0]]
    if header != SCHEDULE_HEADER:
        return ScheduleCount(doc.filename, False,
                             error=f"unexpected header {rows[0]!r}; expected {','.join(SCHEDULE_HEADER)}")

    records = [r for r in rows[1:] if any(cell.strip() for cell in r)]
    n = len(records)
    if n == 0:
        return ScheduleCount(doc.filename, True, 0, "CSV data rows (header only)",
                             "Header present; 0 nonblank vehicle records")
    ids = ", ".join(r[0].strip() for r in records if r and r[0].strip())
    return ScheduleCount(doc.filename, True, n, f"CSV data rows 1-{n} (header excluded)",
                         f"{n} nonblank vehicle record(s): {ids}")
