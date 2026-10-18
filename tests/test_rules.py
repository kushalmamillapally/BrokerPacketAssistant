"""Rule logic tests on hand-built field states (no documents, no model)."""
import pytest

from app.rules import evaluate, rule_r1, rule_r2, rule_r3, rule_r4, rule_r5


def known(value, **extra):
    return {"state": "KNOWN", "value": value, **extra}


def st(state):
    return {"state": state, "value": None}


ALL_PASS = {
    "business_name": known("Acme Example LLC"),
    "requested_effective_date": known("2026-11-01"),
    "declared_vehicle_count": known(3),
    "schedule_vehicle_count": known(3),
    "operating_radius_miles": known(150.0),
    "hazardous_materials": known(False),
    "loss_runs_present": known(True),
}


def test_all_pass_is_ready_for_review():
    result = evaluate(ALL_PASS)
    assert [r["status"] for r in result["rules"]] == ["PASS"] * 5
    assert result["overall_status"] == "READY_FOR_REVIEW"


# --- R1: missing information and precedence ---

def test_r1_missing_effective_date_flags():
    r = rule_r1({**ALL_PASS, "requested_effective_date": st("MISSING")})
    assert r["status"] == "FLAG"
    assert "requested_effective_date" in r["reason"]


def test_r1_missing_takes_precedence_over_conflict():
    r = rule_r1({"business_name": st("CONFLICT"), "requested_effective_date": st("MISSING")})
    assert r["status"] == "FLAG"


@pytest.mark.parametrize("bad_state", ["CONFLICT", "INVALID"])
def test_r1_conflicting_or_invalid_without_absence_is_unknown(bad_state):
    r = rule_r1({**ALL_PASS, "requested_effective_date": st(bad_state)})
    assert r["status"] == "UNKNOWN"


# --- R2: vehicle counts ---

def test_r2_unequal_counts_flag():
    r = rule_r2({"declared_vehicle_count": known(4), "schedule_vehicle_count": known(3)})
    assert r["status"] == "FLAG"
    assert "4" in r["reason"] and "3" in r["reason"]


def test_r2_zero_vehicles_is_a_valid_equal_count():
    assert rule_r2({"declared_vehicle_count": known(0), "schedule_vehicle_count": known(0)})["status"] == "PASS"


@pytest.mark.parametrize("declared,schedule", [
    (st("MISSING"), known(3)),
    (known(3), st("INVALID")),   # CSV could not be parsed
    (st("CONFLICT"), known(3)),
])
def test_r2_unknown_when_a_count_is_unresolved(declared, schedule):
    assert rule_r2({"declared_vehicle_count": declared, "schedule_vehicle_count": schedule})["status"] == "UNKNOWN"


# --- R3: the 150-mile threshold ---

@pytest.mark.parametrize("radius,expected", [
    (150, "PASS"), (150.0, "PASS"), (149.9, "PASS"), (0, "PASS"),
    (150.1, "FLAG"), (180, "FLAG"),
])
def test_r3_threshold(radius, expected):
    assert rule_r3({"operating_radius_miles": known(radius)})["status"] == expected


@pytest.mark.parametrize("field", [st("MISSING"), st("CONFLICT"), st("INVALID"), known(-5), known("far")])
def test_r3_unknown_for_unresolved_negative_or_nonnumeric(field):
    assert rule_r3({"operating_radius_miles": field})["status"] == "UNKNOWN"


def test_r3_conflict_is_unknown_even_if_both_values_would_pass():
    # Unresolved conflicts produce UNKNOWN before thresholds are tested.
    assert rule_r3({"operating_radius_miles": {"state": "CONFLICT", "value": None}})["status"] == "UNKNOWN"


# --- R4 / R5 ---

@pytest.mark.parametrize("field,expected", [
    (known(False), "PASS"), (known(True), "FLAG"), (st("MISSING"), "UNKNOWN"), (st("CONFLICT"), "UNKNOWN"),
])
def test_r4_hazmat(field, expected):
    assert rule_r4({"hazardous_materials": field})["status"] == expected


@pytest.mark.parametrize("field,expected", [
    (known(True), "PASS"), (known(False), "FLAG"), (st("INVALID"), "UNKNOWN"),
])
def test_r5_loss_runs(field, expected):
    assert rule_r5({"loss_runs_present": field})["status"] == expected


def test_any_flag_or_unknown_needs_review():
    assert evaluate({**ALL_PASS, "hazardous_materials": st("MISSING")})["overall_status"] == "NEEDS_REVIEW"


def test_rule_inputs_report_override_marker():
    r = rule_r3({"operating_radius_miles": known(80.0, overridden=True)})
    assert r["status"] == "PASS"
    assert r["inputs"][0]["overridden"] is True
