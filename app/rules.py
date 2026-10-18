"""Deterministic evaluation of rules R1-R5 (see docs/rules.json). No model calls here."""
from __future__ import annotations

from typing import Any

from .normalize import display

RADIUS_THRESHOLD_MILES = 150

RULE_NAMES = {
    "R1": "Required identity and effective date",
    "R2": "Vehicle count reconciliation",
    "R3": "Operating radius",
    "R4": "Hazardous materials",
    "R5": "Loss runs attachment",
}


def _f(fields: dict[str, dict], name: str) -> dict:
    return fields.get(name) or {"state": "MISSING", "value": None}


def _known(field: dict) -> bool:
    return field.get("state") == "KNOWN"


def _describe(name: str, field: dict) -> str:
    state = field.get("state", "MISSING")
    if state == "KNOWN":
        return f"{name} = {display(field.get('value'))}" + (" (human override)" if field.get("overridden") else "")
    return f"{name} is {state.lower()}"


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _result(rule_id: str, status: str, reason: str, fields: dict, inputs: list[str]) -> dict:
    return {
        "id": rule_id,
        "name": RULE_NAMES[rule_id],
        "status": status,
        "reason": reason,
        "inputs": [{"field": n, "state": _f(fields, n).get("state"), "value": _f(fields, n).get("value"),
                    "display": display(_f(fields, n).get("value")),
                    "overridden": bool(_f(fields, n).get("overridden"))} for n in inputs],
    }


def rule_r1(fields: dict) -> dict:
    inputs = ["business_name", "requested_effective_date"]
    states = {n: _f(fields, n) for n in inputs}
    missing = [n for n, f in states.items() if f.get("state") == "MISSING"]
    unresolved = [n for n, f in states.items() if f.get("state") in ("CONFLICT", "INVALID")]
    if missing:  # absence takes precedence over invalid / conflicting values
        return _result("R1", "FLAG", "Required value absent: " + ", ".join(missing), fields, inputs)
    if unresolved:
        return _result("R1", "UNKNOWN", "; ".join(_describe(n, states[n]) for n in unresolved), fields, inputs)
    return _result("R1", "PASS", "; ".join(_describe(n, f) for n, f in states.items()), fields, inputs)


def rule_r2(fields: dict) -> dict:
    inputs = ["declared_vehicle_count", "schedule_vehicle_count"]
    declared, schedule = _f(fields, inputs[0]), _f(fields, inputs[1])
    bad = [n for n, f in zip(inputs, (declared, schedule)) if not (_known(f) and _is_count(f.get("value")))]
    if bad:
        return _result("R2", "UNKNOWN", "; ".join(_describe(n, _f(fields, n)) for n in bad), fields, inputs)
    if declared["value"] == schedule["value"]:
        return _result("R2", "PASS", f"Declared count {declared['value']} equals schedule count {schedule['value']}",
                       fields, inputs)
    return _result("R2", "FLAG", f"Declared count {declared['value']} differs from schedule count {schedule['value']}",
                   fields, inputs)


def rule_r3(fields: dict) -> dict:
    inputs = ["operating_radius_miles"]
    radius = _f(fields, inputs[0])
    value = radius.get("value")
    if not _known(radius):
        return _result("R3", "UNKNOWN", _describe(inputs[0], radius), fields, inputs)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _result("R3", "UNKNOWN", "Radius is not numeric", fields, inputs)
    if value < 0:
        return _result("R3", "UNKNOWN", "Radius is negative", fields, inputs)
    if value > RADIUS_THRESHOLD_MILES:
        return _result("R3", "FLAG", f"Radius {display(value)} miles is greater than {RADIUS_THRESHOLD_MILES}",
                       fields, inputs)
    return _result("R3", "PASS", f"Radius {display(value)} miles is at or below {RADIUS_THRESHOLD_MILES}",
                   fields, inputs)


def rule_r4(fields: dict) -> dict:
    inputs = ["hazardous_materials"]
    hazmat = _f(fields, inputs[0])
    if not _known(hazmat) or not isinstance(hazmat.get("value"), bool):
        return _result("R4", "UNKNOWN", _describe(inputs[0], hazmat), fields, inputs)
    if hazmat["value"]:
        return _result("R4", "FLAG", "Hazardous materials explicitly Yes", fields, inputs)
    return _result("R4", "PASS", "Hazardous materials explicitly No", fields, inputs)


def rule_r5(fields: dict) -> dict:
    inputs = ["loss_runs_present"]
    loss = _f(fields, inputs[0])
    reason = loss.get("reason") or _describe(inputs[0], loss)
    if not _known(loss) or not isinstance(loss.get("value"), bool):
        return _result("R5", "UNKNOWN", reason, fields, inputs)
    if loss.get("overridden"):
        reason = _describe(inputs[0], loss)
    return _result("R5", "PASS" if loss["value"] else "FLAG", reason, fields, inputs)


def evaluate(fields: dict[str, dict]) -> dict:
    """fields: name -> {"state", "value", optional "overridden", optional "reason"}."""
    rules = [rule_r1(fields), rule_r2(fields), rule_r3(fields), rule_r4(fields), rule_r5(fields)]
    overall = "READY_FOR_REVIEW" if all(r["status"] == "PASS" for r in rules) else "NEEDS_REVIEW"
    return {"rules": rules, "overall_status": overall}
