"""Deterministic normalization of raw extracted strings into typed field values.

Every normalizer returns a Normalized result. The LLM only supplies raw text and
excerpts; all typing, comparison and validation happens here in ordinary code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

# Explicit "no value" markers. Note: "none" is deliberately absent because for
# hazardous materials "None" is ambiguous (could mean "no hazmat").
ABSENT_MARKERS = {
    "", "not provided", "not stated", "n/a", "na", "unknown", "tbd", "to be determined",
    "not applicable", "-", "--", "—", "null", "blank", "not available",
}

US_STATES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL",
    "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA",
    "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR",
    "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA",
    "WASHINGTON": "WA", "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
}
STATE_CODES = set(US_STATES.values())

NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


@dataclass
class Normalized:
    ok: bool
    value: Any = None
    note: str = ""


def collapse_ws(text: str) -> str:
    return " ".join(str(text).split())


def is_absent(raw: Any) -> bool:
    if raw is None:
        return True
    return collapse_ws(str(raw)).strip(" .").lower() in ABSENT_MARKERS


def norm_business_name(raw: str) -> Normalized:
    name = collapse_ws(raw)
    if not name:
        return Normalized(False, note="empty business name")
    return Normalized(True, name)


_DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y",
                 "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"]


def norm_date(raw: str) -> Normalized:
    text = collapse_ws(raw).rstrip(".")
    for fmt in _DATE_FORMATS:
        try:
            return Normalized(True, datetime.strptime(text, fmt).date().isoformat())
        except ValueError:
            continue
    # Looks like ISO but is not a real calendar date (e.g. 2026-02-30).
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return Normalized(False, note=f"'{text}' is not a valid calendar date")
    return Normalized(False, note=f"'{text}' is not a recognizable date")


def norm_state(raw: str) -> Normalized:
    text = collapse_ws(raw).strip(" .").upper()
    if text in STATE_CODES:
        return Normalized(True, text)
    if text in US_STATES:
        return Normalized(True, US_STATES[text])
    # A place such as "Sacramento, CA" or "Reno, Nevada": use the part after the last comma.
    if "," in text:
        tail = text.rsplit(",", 1)[1].strip(" .")
        tail = re.sub(r"\s+\d{5}(-\d{4})?$", "", tail)  # drop a ZIP code
        if tail in STATE_CODES:
            return Normalized(True, tail)
        if tail in US_STATES:
            return Normalized(True, US_STATES[tail])
    return Normalized(False, note=f"'{collapse_ws(raw)}' is not a recognized US state")


def norm_text(raw: str) -> Normalized:
    text = collapse_ws(raw)
    if not text:
        return Normalized(False, note="empty description")
    return Normalized(True, text)


def _single_number(raw: str, allowed_words: set[str]) -> tuple[float | None, str]:
    text = collapse_ws(raw).lower().replace(",", "")
    numbers = re.findall(r"-?\d+(?:\.\d+)?", text)
    leftover = re.sub(r"-?\d+(?:\.\d+)?", " ", text)
    leftover_words = {w for w in re.split(r"[\s.:;()]+", leftover) if w}
    if not numbers:
        word_hits = [NUMBER_WORDS[w] for w in leftover_words if w in NUMBER_WORDS]
        if len(word_hits) == 1 and leftover_words - set(NUMBER_WORDS) <= allowed_words:
            return float(word_hits[0]), ""
        return None, f"'{collapse_ws(raw)}' is not numeric"
    if len(numbers) > 1:
        return None, f"'{collapse_ws(raw)}' contains more than one number"
    if not leftover_words <= allowed_words:
        return None, f"'{collapse_ws(raw)}' is not a plain number"
    return float(numbers[0]), ""


_COUNT_WORDS = {"vehicle", "vehicles", "unit", "units", "truck", "trucks", "van", "vans", "total"}
_RADIUS_WORDS = {"mi", "mile", "miles", "radius", "approximately", "approx", "about", "up", "to"}


# "4 box trucks", "12 power units": one number, up to two descriptive words, then a vehicle noun.
_VEHICLE_PHRASE = re.compile(
    r"^\s*(\d+)\s+(?:[a-z-]+\s+){0,2}(vehicles?|trucks?|vans?|units?|tractors?|cars?|buses|autos?|pickups?)\s*\.?\s*$",
    re.IGNORECASE)


def norm_count(raw: str) -> Normalized:
    phrase = _VEHICLE_PHRASE.match(collapse_ws(raw))
    if phrase:
        return Normalized(True, int(phrase.group(1)))
    number, note = _single_number(raw, _COUNT_WORDS)
    if number is None:
        return Normalized(False, note=note)
    if number != int(number):
        return Normalized(False, note=f"'{collapse_ws(raw)}' is not a whole number")
    if number < 0:
        return Normalized(False, note="count cannot be negative")
    return Normalized(True, int(number))


def norm_radius(raw: str) -> Normalized:
    number, note = _single_number(raw, _RADIUS_WORDS)
    if number is None:
        return Normalized(False, note=note)
    if number < 0:
        return Normalized(False, note="radius cannot be negative")
    return Normalized(True, number)


def norm_bool(raw: str) -> Normalized:
    text = collapse_ws(str(raw)).strip(" .").lower()
    if text in {"yes", "y", "true"}:
        return Normalized(True, True)
    if text in {"no", "n", "false"}:
        return Normalized(True, False)
    return Normalized(False, note=f"'{collapse_ws(str(raw))}' is not an explicit Yes/No answer")


def compare_key(field: str, value: Any) -> Any:
    """Key used to decide whether two normalized values agree."""
    if field == "business_name":
        return value.casefold()
    if field == "operations_description":
        return re.sub(r"[^a-z0-9 ]", "", value.lower()).strip()
    return value


def display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


NORMALIZERS: dict[str, Callable[[str], Normalized]] = {
    "business_name": norm_business_name,
    "requested_effective_date": norm_date,
    "garaging_state": norm_state,
    "operations_description": norm_text,
    "declared_vehicle_count": norm_count,
    "operating_radius_miles": norm_radius,
    "hazardous_materials": norm_bool,
    "loss_runs_present": norm_bool,
    "schedule_vehicle_count": norm_count,
}


def normalize(field: str, raw: Any) -> Normalized:
    if isinstance(raw, bool):
        raw = "yes" if raw else "no"
    return NORMALIZERS[field](str(raw))
