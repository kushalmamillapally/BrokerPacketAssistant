"""Shared fixtures. StubLLM stands in for the model so tests never need a live model call."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.extraction import FIELD_LABELS as LABEL_KEYS  # noqa: E402
from app.llm import LLMError  # noqa: E402

SAMPLES = ROOT / "sample_data"

LABELS = {
    "business_name": "business name",
    "requested_effective_date": "requested effective date",
    "garaging_state": "garaging state",
    "operations_description": "operations",
    "declared_vehicle_count": "declared vehicle count",
    "operating_radius_miles": "operating radius miles",
    "hazardous_materials": "hazardous materials",
}


class StubLLM:
    """Deterministic test double: reads 'Label: value' / 'Label  value' lines like a well-behaved model."""

    model = "stub-model"

    def __init__(self):
        self.calls = []

    def chat_json(self, system: str, user: str, schema: dict) -> dict:
        self.calls.append(user)
        if "<facts>" in user:
            return {"summary": "Stub summary.", "email_subject": "Stub subject", "email_body": "Hello,\n- item"}
        doc = user.split("<document>", 1)[1].rsplit("</document>", 1)[0]
        lower = doc.lower()
        doc_type = ("loss_runs" if "loss runs report" in lower else
                    "application" if "application" in lower.splitlines()[1:3].__str__() else
                    "broker_email" if "subject:" in lower else "other")
        fields = {}
        for name, label in LABELS.items():
            fields[LABEL_KEYS[name]] = None
            for line in doc.splitlines():
                m = re.match(rf"\s*{label}(?: description)?\s*(?::|\s{{2,}})\s*(.+)$", line, re.I)
                if m:
                    value = m.group(1).strip().rstrip(".")
                    fields[LABEL_KEYS[name]] = "NOT PROVIDED" if value.lower() == "not provided" else value
                    break
        return {"document_type": doc_type, **fields}


class FailingLLM:
    model = "failing-model"

    def chat_json(self, system, user, schema):
        raise LLMError("Cannot reach Ollama (simulated outage in test)")


def load_packet(name: str) -> list[tuple[str, bytes]]:
    return [(p.name, p.read_bytes()) for p in sorted((SAMPLES / name).iterdir())]


@pytest.fixture
def stub_llm():
    return StubLLM()


@pytest.fixture
def failing_llm():
    return FailingLLM()
