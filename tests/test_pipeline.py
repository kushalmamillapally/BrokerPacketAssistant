"""End-to-end pipeline on the supplied sample packets, using a stub model (no live calls)."""
import pytest

from app import narrative
from app.pipeline import OverrideError, evaluate_packet, process_packet
from conftest import load_packet


def run(name, llm, overrides=None):
    packet = process_packet(load_packet(name), None, llm)
    return packet, evaluate_packet(packet["fields"], overrides)


def statuses(evaluation):
    return {r["id"]: r["status"] for r in evaluation["rules"]}


def test_sub_a_complete_packet(stub_llm):
    packet, ev = run("SUB-A", stub_llm)
    assert statuses(ev) == {"R1": "PASS", "R2": "PASS", "R3": "PASS", "R4": "PASS", "R5": "PASS"}
    assert ev["overall_status"] == "READY_FOR_REVIEW"
    f = packet["fields"]
    assert f["garaging_state"]["value"] == "CA"          # "California" in PDF, "CA" in email
    assert f["operating_radius_miles"]["value"] == 150   # boundary passes
    assert f["schedule_vehicle_count"]["evidence"][0]["locator"] == "CSV data rows 1-3 (header excluded)"


def test_sub_b_missing_information(stub_llm):
    packet, ev = run("SUB-B", stub_llm)
    assert statuses(ev) == {"R1": "FLAG", "R2": "PASS", "R3": "UNKNOWN", "R4": "UNKNOWN", "R5": "FLAG"}
    f = packet["fields"]
    assert f["requested_effective_date"]["state"] == "MISSING"
    assert f["hazardous_materials"]["value"] is None      # absence is not "No"
    # Email promise to send loss runs later does not count as an attachment.
    assert f["loss_runs_present"]["value"] is False
    assert f["loss_runs_present"]["email_statements"]


def test_sub_c_conflicts_and_unequal_counts(stub_llm):
    packet, ev = run("SUB-C", stub_llm)
    assert statuses(ev) == {"R1": "PASS", "R2": "FLAG", "R3": "UNKNOWN", "R4": "UNKNOWN", "R5": "FLAG"}
    radius = packet["fields"]["operating_radius_miles"]
    assert radius["state"] == "CONFLICT" and radius["value"] is None
    assert {a["display"] for a in radius["alternatives"]} == {"80", "180"}


def test_override_resolves_conflict_and_keeps_original(stub_llm):
    packet, ev = run("SUB-C", stub_llm, {"operating_radius_miles": {"value": "80", "note": "confirmed by phone"}})
    radius = ev["effective_fields"]["operating_radius_miles"]
    assert statuses(ev)["R3"] == "PASS"
    assert radius["overridden"] is True and radius["value"] == 80.0
    assert radius["original"]["state"] == "CONFLICT"
    assert len(radius["evidence"]) == 2                      # original evidence retained
    assert packet["fields"]["operating_radius_miles"]["state"] == "CONFLICT"  # extracted data untouched


def test_invalid_override_is_rejected(stub_llm):
    packet = process_packet(load_packet("SUB-C"), None, stub_llm)
    with pytest.raises(OverrideError):
        evaluate_packet(packet["fields"], {"operating_radius_miles": {"value": "far away"}})


def test_packets_are_independent(stub_llm):
    a = process_packet(load_packet("SUB-A"), None, stub_llm)
    b = process_packet(load_packet("SUB-B"), None, stub_llm)
    assert b["fields"]["business_name"]["value"] == "Canyon Parcel Example LLC"
    assert "Pacific Harbor" not in str(b)
    assert a["packet_id"] != b["packet_id"]


def test_pasted_email_only(stub_llm):
    email = (load_packet("SUB-A")[1][1]).decode()  # broker_email.txt
    packet = process_packet([], email, stub_llm)
    ev = evaluate_packet(packet["fields"])
    assert packet["fields"]["business_name"]["value"] == "Pacific Harbor Delivery Example LLC"
    assert statuses(ev)["R2"] == "UNKNOWN"   # no CSV supplied
    assert statuses(ev)["R5"] == "FLAG"      # email says attached, but nothing was uploaded


def test_model_failure_keeps_deterministic_results(failing_llm):
    packet, ev = run("SUB-A", failing_llm)
    assert all(not log["ok"] for log in packet["extraction_log"])
    assert packet["fields"]["schedule_vehicle_count"]["value"] == 3       # CSV still counted
    assert packet["fields"]["loss_runs_present"]["value"] is True         # title text still classified
    # Not determinable is not the same as absent: R1 is UNKNOWN rather than FLAG.
    assert packet["fields"]["business_name"]["state"] == "INVALID"
    assert statuses(ev)["R1"] == "UNKNOWN"


def test_narrative_failure_is_reported_not_simulated(failing_llm, stub_llm):
    packet, ev = run("SUB-B", stub_llm)
    out = narrative.generate(ev, packet["inventory"], failing_llm)
    assert out["ok"] is False and "summary" not in out
    assert "Requested effective date" in out["fallback_email_body"]


def test_narrative_prompt_contains_request_items(stub_llm):
    packet, ev = run("SUB-C", stub_llm)
    out = narrative.generate(ev, packet["inventory"], stub_llm)
    assert out["ok"] is True
    assert any("Operating radius" in i for i in out["items_to_request"])
    assert any(i.startswith("Vehicle count: 4 vehicles declared") for i in out["items_to_request"])
    assert any(i.startswith("Loss runs:") for i in out["items_to_request"])


def test_empty_upload_is_rejected(stub_llm):
    with pytest.raises(ValueError):
        process_packet([], "   ", stub_llm)


def test_broker_address_prefill_comes_from_email_header(stub_llm):
    packet = process_packet(load_packet("SUB-A"), None, stub_llm)
    assert packet["broker_address"] == "broker@example.com"


def test_targeted_retry_recovers_field_the_model_skipped():
    from app.documents import SourceDoc
    from app.extraction import extract_document

    class SkipsCountFirst:
        model = "stub"

        def __init__(self):
            self.calls = 0

        def chat_json(self, system, user, schema):
            self.calls += 1
            if self.calls == 1:
                return {"document_type": "broker_email", "Business name": "Acme LLC", "Declared vehicle count": None}
            assert list(schema["properties"]) == ["Declared vehicle count"]   # retry asks only for the missed key
            return {"Declared vehicle count": "2"}

    doc = SourceDoc("email.txt", "email", 1, pages=["Business name: Acme LLC\nDeclared vehicle count: 2"])
    llm = SkipsCountFirst()
    ext = extract_document(doc, llm)
    assert llm.calls == 2 and ext.retried_fields == ["declared_vehicle_count"]
    count = next(c for c in ext.candidates if c.field == "declared_vehicle_count")
    assert count.raw_value == "2" and count.excerpt == "Declared vehicle count: 2"
