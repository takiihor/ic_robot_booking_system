"""Parser tests — SPEC 23."""
from __future__ import annotations

from datetime import date

import pytest

from app.services.booking_parser import (
    find_preferred_robot,
    parse_booking_email,
    parse_date,
    parse_sessions,
)
from tests.conftest import SAMPLE_EMAIL


def test_standard_booking_email_parses_every_field():
    parsed = parse_booking_email(SAMPLE_EMAIL)

    assert parsed.name == "DING Changwen"
    assert parsed.sid_netid == "25104512r"
    assert parsed.department == "Department of Mechanical Engineering"
    assert parsed.email == "changwen.ding@connect.polyu.hk"
    assert parsed.phone == "5123 4567"
    assert parsed.facility == "Industrial Robot Laboratory"
    assert parsed.booking_type == "Research"
    assert parsed.response_id == "R-2026-0142"
    assert parsed.start_date == date(2026, 8, 31)
    assert parsed.end_date == date(2026, 9, 4)
    assert parsed.sessions == ["AM", "PM"]
    assert parsed.preferred_robot == "UR10e (03)"
    assert parsed.purpose.startswith("Robotic grasping")
    assert parsed.warnings == []


def test_markdown_bold_is_stripped_and_labels_are_case_insensitive():
    parsed = parse_booking_email("**NAME:** WONG Ka Ming\ndate: 2026-08-31\nsession: am")
    assert parsed.name == "WONG Ka Ming"
    assert parsed.sessions == ["AM"]


def test_multiline_session_field():
    parsed = parse_booking_email(
        "Name: A\nDate: 2026-08-31\nSession:\nMorning 08:30-12:00\nAfternoon 13:30-17:00\n"
    )
    assert parsed.sessions == ["AM", "PM"]


def test_multiline_remarks_captured_until_next_label():
    parsed = parse_booking_email(
        "Remarks:\nline one\nline two\n\nline three\nName: A\nDate: 2026-08-31\nSession: AM"
    )
    assert parsed.remarks == "line one\nline two\n\nline three"
    assert parsed.name == "A"


def test_missing_optional_field_is_blank_not_an_error():
    parsed = parse_booking_email("Name: A\nDate: 2026-08-31\nSession: AM")
    assert parsed.phone is None
    assert parsed.department is None
    assert parsed.start_date == date(2026, 8, 31)


def test_invalid_date_warns_and_leaves_field_blank():
    parsed = parse_booking_email("Name: A\nDate: next Tuesday\nSession: AM")
    assert parsed.start_date is None
    assert any("start date" in w.lower() for w in parsed.warnings)


def test_missing_required_fields_produce_warnings():
    parsed = parse_booking_email("Department: ME")
    assert any("Applicant name" in w for w in parsed.warnings)
    assert any("Start date" in w for w in parsed.warnings)
    assert any("session" in w.lower() for w in parsed.warnings)


def test_end_date_defaults_to_start_date():
    parsed = parse_booking_email("Name: A\nDate: 2026-08-31\nSession: AM")
    assert parsed.end_date == date(2026, 8, 31)


def test_end_before_start_warns():
    parsed = parse_booking_email(
        "Name: A\nDate: 2026-09-04\nEnd Date: 2026-08-31\nSession: AM"
    )
    assert any("before the start" in w for w in parsed.warnings)


def test_empty_paste_warns():
    parsed = parse_booking_email("   \n  ")
    assert parsed.warnings == ["Nothing to parse — the pasted text is empty."]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("UR10e (03)", "UR10e (03)"),
        ("UR10e(03)", "UR10e (03)"),
        ("please use ur10e ( 5 ) thanks", "ur10e (05)"),
        ("no robot mentioned", None),
    ],
)
def test_preferred_robot_detection(text, expected):
    assert find_preferred_robot(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-08-31", date(2026, 8, 31)),
        ("31/08/2026", date(2026, 8, 31)),
        ("31 Aug 2026", date(2026, 8, 31)),
        ("August 31, 2026", date(2026, 8, 31)),
        ("2026-13-45", None),
        ("", None),
    ],
)
def test_date_parsing(text, expected):
    assert parse_date(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("AM", ["AM"]),
        ("PM only", ["PM"]),
        ("AM 08:30-12:00 and PM 13:30-17:00", ["AM", "PM"]),
        ("whole day", ["AM", "PM"]),
        ("morning", ["AM"]),
        ("", []),
    ],
)
def test_session_detection(text, expected):
    assert parse_sessions(text) == expected


# --- labels carrying a parenthetical qualifier (the real ICT form) ----------

ICT_EMAIL = """
Department: PolyU - ISE - Department of Industrial and Systems Engineering (FENG-ISE)
Facilities to Use: ["Collaborative Robots (Cobot)"]

Response ID: ICT-352-STD
Booking Type: [FREQUENT USE] - More than one day (Consecutive Days Booking)
Date: 2026-09-14
End Date (Consecutive Booking only): 2026-09-25
Session: ["AM Session, 3.5 hours (08.30 - 12.00)","PM Session, 3.5 hours (13.30 - 17.00)"]

Name: Ding Changwen
SID/NetID: 25104512r
Tel: 69430095
Email: 25104512r@connect.polyu.hk
Booking for: Research Activities, Dexterous Hand Project, Prof. Lik-Hang Lee
Remarks (if any): I would be grateful to resume using the UR10e (03) from 14 September.
"""


def test_end_date_label_with_a_parenthetical_qualifier_is_read():
    """A 12-day booking used to silently collapse to a single day."""
    parsed = parse_booking_email(ICT_EMAIL)
    assert parsed.start_date == date(2026, 9, 14)
    assert parsed.end_date == date(2026, 9, 25)


def test_remarks_label_with_a_qualifier_does_not_leak_into_purpose():
    parsed = parse_booking_email(ICT_EMAIL)
    assert parsed.remarks is not None
    assert "resume using the UR10e" in parsed.remarks
    assert "Remarks" not in (parsed.purpose or "")
    assert parsed.purpose.startswith("Research Activities")


def test_qualified_labels_still_find_the_rest_of_the_form():
    parsed = parse_booking_email(ICT_EMAIL)
    assert parsed.name == "Ding Changwen"
    assert parsed.sid_netid == "25104512r"
    assert parsed.response_id == "ICT-352-STD"
    assert parsed.sessions == ["AM", "PM"]
    assert parsed.preferred_robot == "UR10e (03)"


def test_square_and_curly_qualifiers_are_stripped_too():
    parsed = parse_booking_email(
        "Name: A Person\nDate: 2026-03-02\nEnd Date [optional]: 2026-03-06\nSession: AM\n"
    )
    assert parsed.start_date == date(2026, 3, 2)
    assert parsed.end_date == date(2026, 3, 6)


def test_a_single_day_booking_still_defaults_the_end_date_silently():
    parsed = parse_booking_email("Name: A Person\nDate: 2026-03-02\nSession: AM\n")
    assert parsed.start_date == parsed.end_date == date(2026, 3, 2)
    assert not any("end date" in w.lower() for w in parsed.warnings)


def test_an_unknown_label_swallowing_a_date_is_reported_not_silent():
    """The guard for the next form variant we have not seen yet."""
    parsed = parse_booking_email(
        "Name: A Person\nDate: 2026-03-02\nFinish On Or About: 2026-03-19\nSession: AM\n"
    )
    assert parsed.start_date == date(2026, 3, 2)
    assert parsed.end_date == date(2026, 3, 2)  # still defaults, but loudly
    assert any("2026-03-19" in w for w in parsed.warnings)
    assert any("check the end date" in w.lower() for w in parsed.warnings)
