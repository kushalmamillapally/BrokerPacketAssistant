"""Packet processing: ingest -> extract (LLM) -> merge -> rules. Stateless: every call starts from scratch."""
from __future__ import annotations

import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .documents import count_schedule, ingest
from .extraction import EXTRACTED_FIELDS, extract_document
from .llm import LLMClient
from .merge import ALL_FIELDS, classify_attachments, loss_runs_field, merge_field, schedule_field
from .normalize import display, is_absent, normalize
from .rules import evaluate


class OverrideError(ValueError):
    pass


def process_packet(files: list[tuple[str, bytes]], pasted_email: str | None, llm: LLMClient) -> dict:
    docs = ingest(files, pasted_email)
    if not any(d.kind in ("email", "pdf", "csv") for d in docs):
        raise ValueError("No usable submission content. Upload .pdf/.csv/.txt files or paste the broker email.")

    text_docs = [d for d in docs if d.kind in ("email", "pdf") and d.readable]
    # Documents are independent, so extract them concurrently. The model server must allow
    # parallel requests (Ollama: OLLAMA_NUM_PARALLEL); otherwise they simply queue.
    workers = max(1, min(len(text_docs), int(os.getenv("LLM_MAX_PARALLEL", "3"))))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda d: extract_document(d, llm), text_docs))
    extractions = {d.filename: r for d, r in zip(text_docs, results)}
    inspected = [f for f, e in extractions.items() if e.ok]
    failed = [f for f, e in extractions.items() if not e.ok]

    inventory = classify_attachments(docs, extractions)
    types = {i["filename"]: i["detected_type"] for i in inventory}

    fields: dict[str, dict] = {}
    for name in EXTRACTED_FIELDS:
        candidates = []
        for fname, ext in extractions.items():
            # A loss runs report only contributes the business name; its other numbers are not applicant answers.
            if types.get(fname) == "loss_runs" and name != "business_name":
                continue
            candidates += [c for c in ext.candidates if c.field == name]
        fields[name] = merge_field(name, candidates, inspected, failed)

    fields["schedule_vehicle_count"] = schedule_field(docs, [count_schedule(d) for d in docs if d.kind == "csv"])
    promises = [e.loss_runs_statement for e in extractions.values()
                if e.loss_runs_statement and types.get(e.filename) == "broker_email"]
    fields["loss_runs_present"] = loss_runs_field(inventory, promises)

    return {
        "packet_id": uuid.uuid4().hex[:12],
        "broker_address": broker_address(docs),
        "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": getattr(llm, "model", "unknown"),
        "inventory": inventory,
        "extraction_log": [{"source": e.filename, "ok": e.ok, "error": e.error, "document_type": e.document_type,
                            "elapsed_seconds": e.elapsed_seconds, "suspicious_lines": e.suspicious_lines,
                            "retried_fields": e.retried_fields}
                           for e in extractions.values()],
        "fields": {n: fields[n] for n in ALL_FIELDS},
    }


def broker_address(docs) -> str | None:
    """Deterministic: the sender address on the broker email's From: line, used to prefill the reply."""
    for d in docs:
        if d.kind == "email" and d.pages:
            m = re.search(r"^From:.*?([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", d.pages[0], re.MULTILINE | re.IGNORECASE)
            if m:
                return m.group(1)
    return None


def apply_overrides(fields: dict[str, dict], overrides: dict[str, dict] | None) -> dict[str, dict]:
    """Return effective fields. Originals (state, value, evidence) are kept under `original`."""
    effective = {}
    for name in ALL_FIELDS:
        original = fields.get(name) or {"name": name, "state": "MISSING", "value": None}
        ov = (overrides or {}).get(name)
        if not ov:
            effective[name] = {**original, "overridden": False}
            continue
        raw = ov.get("value")
        if is_absent(raw):
            raise OverrideError(f"{name}: override value is empty")
        n = normalize(name, raw)
        if not n.ok:
            raise OverrideError(f"{name}: {n.note}")
        effective[name] = {
            **original,
            "state": "KNOWN",
            "value": n.value,
            "display": display(n.value),
            "overridden": True,
            "override": {"value": n.value, "display": display(n.value), "note": ov.get("note", ""),
                         "at": ov.get("at") or datetime.now(timezone.utc).isoformat(timespec="seconds")},
            "original": {"state": original.get("state"), "value": original.get("value"),
                         "display": original.get("display"), "reason": original.get("reason")},
            "reason": f"Human override ({display(n.value)}); original state {original.get('state')}",
        }
    return effective


def evaluate_packet(fields: dict[str, dict], overrides: dict[str, dict] | None = None) -> dict:
    effective = apply_overrides(fields, overrides)
    return {"effective_fields": effective, **evaluate(effective)}


FIELD_LABELS = {
    "business_name": "Business name",
    "requested_effective_date": "Requested effective date",
    "garaging_state": "Garaging state",
    "operations_description": "Operations description",
    "declared_vehicle_count": "Declared vehicle count",
    "operating_radius_miles": "Operating radius (miles)",
    "hazardous_materials": "Hazardous materials",
    "loss_runs_present": "Loss runs report",
    "schedule_vehicle_count": "Vehicle schedule count",
}


def request_items(evaluation: dict) -> list[str]:
    """Deterministic, broker-facing list of what to ask for; grounds the narrative prompt."""
    items = []
    fields = evaluation["effective_fields"]
    for name, f in fields.items():
        if f.get("overridden") or name in ("loss_runs_present", "schedule_vehicle_count"):
            continue
        label = FIELD_LABELS[name]
        if f["state"] == "MISSING":
            items.append(f"{label}: not provided - please provide it.")
        elif f["state"] == "CONFLICT":
            versions = "; ".join(f'{a["display"]} in {", ".join(s.split(" (")[0] for s in a["sources"])}'
                                 for a in f["alternatives"])
            items.append(f"{label}: the documents disagree ({versions}) - please confirm the correct value.")
        elif f["state"] == "INVALID":
            items.append(f"{label}: the value could not be read clearly - please confirm it.")

    rules = {r["id"]: r for r in evaluation["rules"]}
    sched = fields["schedule_vehicle_count"]
    if rules["R2"]["status"] == "FLAG":
        items.append(f'Vehicle count: {fields["declared_vehicle_count"]["display"]} vehicles declared, but the '
                     f'vehicle schedule lists {sched["display"]} - please reconcile or send an updated schedule.')
    elif sched["state"] in ("MISSING", "INVALID") and not sched.get("overridden"):
        items.append("Vehicle schedule: please send a readable vehicle schedule (CSV).")
    if rules["R3"]["status"] == "FLAG":
        items.append(f'Operating radius: {fields["operating_radius_miles"]["display"]} miles is above the '
                     f'150-mile review threshold - please confirm the operating radius.')
    if rules["R4"]["status"] == "FLAG":
        items.append("Hazardous materials: marked Yes - please describe the hazardous materials transported.")
    if rules["R5"]["status"] == "FLAG":
        items.append("Loss runs: no loss runs / loss history report was attached - please attach the report.")
    elif rules["R5"]["status"] == "UNKNOWN":
        items.append("Loss runs: an attachment could not be read or identified - please resend the loss runs report.")
    return items
