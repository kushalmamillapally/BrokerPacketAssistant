"""Combine per-source candidates into one field result with state KNOWN / MISSING / CONFLICT / INVALID."""
from __future__ import annotations

from dataclasses import asdict

from .documents import ScheduleCount, SourceDoc
from .extraction import EXTRACTED_FIELDS, Candidate, DocExtraction
from .normalize import compare_key, display, normalize

ALL_FIELDS = list(EXTRACTED_FIELDS) + ["loss_runs_present", "schedule_vehicle_count"]


def _evidence(c: Candidate) -> dict:
    out = asdict(c)
    out["normalized"] = None
    out["valid"] = False
    out["invalid_reason"] = ""
    if c.status == "stated" and c.excerpt_verified:
        n = normalize(c.field, c.raw_value)
        out["normalized"], out["valid"] = n.value, n.ok
        out["invalid_reason"] = n.note
        out["display"] = display(n.value) if n.ok else c.raw_value
    else:
        out["display"] = c.raw_value
    return out


def merge_field(name: str, candidates: list[Candidate], inspected: list[str], failed: list[str]) -> dict:
    evidence = [_evidence(c) for c in candidates]
    stated = [e for e in evidence if e["status"] == "stated" and e["excerpt_verified"]]
    ungrounded = [e for e in evidence if e["status"] == "stated" and not e["excerpt_verified"]]
    valid = [e for e in stated if e["valid"]]
    invalid = [e for e in stated if not e["valid"]]

    result = {"name": name, "state": "MISSING", "value": None, "reason": "", "evidence": evidence,
              "alternatives": [], "inspected_sources": inspected, "failed_sources": failed}

    groups: dict = {}
    for e in valid:
        groups.setdefault(compare_key(name, e["normalized"]), []).append(e)

    if len(groups) > 1:
        result["state"] = "CONFLICT"
        result["alternatives"] = [
            {"value": g[0]["normalized"], "display": g[0]["display"],
             "sources": [f'{e["source_file"]} ({e["locator"]})' for e in g]}
            for g in groups.values()
        ]
        result["reason"] = "Sources disagree: " + " vs ".join(
            f'{a["display"]} [{", ".join(a["sources"])}]' for a in result["alternatives"])
    elif invalid or ungrounded:
        bad = (invalid or ungrounded)[0]
        result["state"] = "INVALID"
        result["reason"] = (f'{bad["source_file"]} ({bad["locator"]}): '
                            f'{bad["invalid_reason"] or bad["grounding_note"]}')
    elif valid:
        result["state"] = "KNOWN"
        result["value"] = valid[0]["normalized"]
        sources = ", ".join(f'{e["source_file"]} ({e["locator"]})' for e in valid)
        result["reason"] = f"Stated in {sources}"
    elif failed:
        # A source could not be read by the model, so absence is not established.
        result["state"] = "INVALID"
        result["reason"] = f"Not determinable: extraction failed for {', '.join(failed)}"
    else:
        explicit = [e for e in evidence if e["status"] == "not_provided"]
        checked = ", ".join(inspected) or "no text sources"
        result["reason"] = (f"Explicitly not provided in {', '.join(e['source_file'] for e in explicit)}; "
                            if explicit else "Not mentioned; ") + f"sources checked: {checked}"
    result["display"] = display(result["value"])
    return result


def schedule_field(docs: list[SourceDoc], counts: list[ScheduleCount]) -> dict:
    csv_names = [d.filename for d in docs if d.kind == "csv"]
    result = {"name": "schedule_vehicle_count", "state": "MISSING", "value": None, "evidence": [],
              "alternatives": [], "inspected_sources": csv_names, "failed_sources": [], "derived": True}
    for c in counts:
        result["evidence"].append({
            "source_file": c.filename, "locator": c.locator or "CSV file",
            "excerpt": c.observation if c.ok else None, "observation": c.observation or c.error,
            "status": "derived" if c.ok else "error", "display": display(c.count), "valid": c.ok,
        })
    ok = [c for c in counts if c.ok]
    if not counts:
        result["reason"] = "No vehicle schedule CSV was supplied"
    elif not ok:
        result["state"] = "INVALID"
        result["reason"] = "Vehicle schedule could not be parsed: " + "; ".join(f"{c.filename}: {c.error}" for c in counts)
    elif len({c.count for c in ok}) > 1:
        result["state"] = "CONFLICT"
        result["alternatives"] = [{"value": c.count, "display": str(c.count), "sources": [c.filename]} for c in ok]
        result["reason"] = "Multiple vehicle schedules with different counts"
    else:
        result["state"], result["value"] = "KNOWN", ok[0].count
        result["reason"] = f"Counted {ok[0].count} nonblank record(s) in {ok[0].filename} ({ok[0].locator})"
    result["display"] = display(result["value"])
    return result


LOSS_RUNS_TITLE_TERMS = ("loss run", "loss history", "loss-run")


def classify_attachments(docs: list[SourceDoc], extractions: dict[str, DocExtraction]) -> list[dict]:
    """Determine each attachment's document type. Title text first, then the model's classification."""
    out = []
    for d in docs:
        entry = d.inventory_entry()
        entry["detected_type"], entry["type_basis"] = None, ""
        if d.kind == "email":
            entry["detected_type"], entry["type_basis"] = "broker_email", "email text"
        elif d.kind == "csv":
            entry["detected_type"], entry["type_basis"] = "vehicle_schedule", "CSV file"
        elif d.kind == "pdf" and d.readable:
            title = " ".join(d.pages[0].splitlines()[:4]).lower() if d.pages else ""
            ext = extractions.get(d.filename)
            if any(t in title for t in LOSS_RUNS_TITLE_TERMS):
                entry["detected_type"], entry["type_basis"] = "loss_runs", "document title text"
                entry["title_excerpt"] = d.pages[0].splitlines()[0] if d.pages[0] else ""
                for line in d.pages[0].splitlines()[:4]:
                    if any(t in line.lower() for t in LOSS_RUNS_TITLE_TERMS):
                        entry["title_excerpt"] = line
                        break
            elif ext and ext.ok:
                entry["detected_type"], entry["type_basis"] = ext.document_type, "model classification"
            else:
                entry["detected_type"], entry["type_basis"] = "undetermined", "model classification failed"
        elif d.kind == "pdf":
            entry["detected_type"], entry["type_basis"] = "unreadable", d.error or "unreadable"
        else:
            entry["detected_type"], entry["type_basis"] = "unsupported", d.error or ""
        out.append(entry)
    return out


def loss_runs_field(inventory: list[dict], promises: list[dict]) -> dict:
    attachments = [i for i in inventory if i["kind"] != "email"]
    inventory_text = ", ".join(f'{i["filename"]} ({i["detected_type"]})' for i in attachments) or "no attachments"
    result = {"name": "loss_runs_present", "state": "KNOWN", "value": None, "evidence": [], "alternatives": [],
              "inspected_sources": [i["filename"] for i in attachments], "failed_sources": [], "derived": True,
              "email_statements": promises}
    found = [i for i in attachments if i["detected_type"] == "loss_runs"]
    uncertain = [i for i in attachments if i["kind"] == "pdf" and i["detected_type"] in ("unreadable", "undetermined")]
    if found:
        result["value"] = True
        for i in found:
            result["evidence"].append({"source_file": i["filename"], "locator": "PDF page 1",
                                       "excerpt": i.get("title_excerpt"), "status": "derived", "valid": True,
                                       "observation": f"Readable loss runs report (basis: {i['type_basis']})",
                                       "display": "Yes"})
        result["reason"] = f"Loss runs report attached: {', '.join(i['filename'] for i in found)}"
    elif uncertain:
        result["state"] = "INVALID"
        result["reason"] = ("Potential loss runs attachment could not be read or classified: "
                            + ", ".join(f'{i["filename"]} ({i["type_basis"]})' for i in uncertain))
        result["failed_sources"] = [i["filename"] for i in uncertain]
    else:
        result["value"] = False
        result["reason"] = f"No loss runs / loss history document in attachment inventory: {inventory_text}"
        if promises:
            result["reason"] += ". An email statement about loss runs is not an attachment."
    result["evidence"].append({"source_file": "uploaded attachment inventory", "locator": "uploaded attachment inventory",
                               "excerpt": None, "observation": inventory_text, "status": "derived",
                               "valid": True, "display": display(result["value"])})
    result["display"] = display(result["value"])
    return result
