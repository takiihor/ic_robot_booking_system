"""Lesson timetable parsing and import — SPEC 13, 23."""
from __future__ import annotations

from datetime import date, datetime, time

import pytest

from app.models import SourceType
from app.services import booking_service as svc
from app.services.lesson_parser import parse_time_range, parse_timetable

TAB_TIMETABLE = (
    "Date\tCourse\tTime\tVenue\n"
    "2026-09-01\tME3101\t09:30-12:20\tRoom FG601\n"
    "2026-09-03\tME3101\t13:30 - 17:00\tRoom FG601\tLab session\n"
)


def test_tab_separated_timetable_parses():
    parsed = parse_timetable(TAB_TIMETABLE)

    assert len(parsed.rows) == 2
    first = parsed.rows[0]
    assert first.course == "ME3101"
    assert first.day == date(2026, 9, 1)
    assert first.start_time == time(9, 30)
    assert first.end_time == time(12, 20)
    assert first.location == "Room FG601"
    assert first.is_complete


def test_header_row_is_dropped():
    assert all(r.course != "Date" for r in parse_timetable(TAB_TIMETABLE).rows)


def test_multi_space_columns_parse():
    parsed = parse_timetable("2026-09-08   ME4202   14:00-16:00   Lab W301")
    assert parsed.rows[0].course == "ME4202"
    assert parsed.rows[0].location == "Lab W301"


def test_markdown_timetable_keeps_polyu_venue_codes_as_locations():
    parsed = parse_timetable(
        "| Module | Time | Locations |\n"
        "| ------ | ---- | --------- |\n"
        "| **SEHS2371** | 15:30–18:30 | W402C(016)(ICT); W402-Z2(016)(ICT) |\n"
        "| **MM3462** | 13:50–14:10 | W402E-Z14(020)(BCO); U401-Z4(016)(BCO) |"
    )

    assert [row.course for row in parsed.rows] == ["SEHS2371", "MM3462"]
    assert [(row.start_time, row.end_time) for row in parsed.rows] == [
        (time(15, 30), time(18, 30)),
        (time(13, 50), time(14, 10)),
    ]
    assert [row.location for row in parsed.rows] == [
        "W402C(016)(ICT); W402-Z2(016)(ICT)",
        "W402E-Z14(020)(BCO); U401-Z4(016)(BCO)",
    ]
    assert not any("skipped" in warning for warning in parsed.warnings)
    assert all("No date found" in row.warnings for row in parsed.rows)


def test_iso_date_is_not_mistaken_for_a_time_range():
    parsed = parse_timetable("2026-09-01\tME3101\t09:30-12:20\tRoom FG601")
    assert parsed.rows[0].start_time == time(9, 30)


def test_notes_are_kept():
    parsed = parse_timetable(TAB_TIMETABLE)
    assert parsed.rows[1].notes == "Lab session"


def test_row_without_a_date_or_time_is_skipped_but_reported():
    parsed = parse_timetable("2026-09-01\tME3101\t09:30-12:20\nsome unrelated sentence")
    assert len(parsed.rows) == 1
    assert any("skipped" in w for w in parsed.warnings)


def test_incomplete_row_is_kept_and_flagged():
    parsed = parse_timetable("2026-09-01\t\t09:30-12:20")
    assert len(parsed.rows) == 1
    assert not parsed.rows[0].is_complete
    assert any("incomplete" in w for w in parsed.warnings)


def test_empty_paste_warns():
    assert parse_timetable("  ") .warnings == ["Nothing to parse — the pasted text is empty."]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("09:30-12:20", (time(9, 30), time(12, 20))),
        ("9.30 am - 12.20 pm", (time(9, 30), time(12, 20))),
        ("9am - 12pm", (time(9, 0), time(12, 0))),
        ("13:30 to 17:00", (time(13, 30), time(17, 0))),
        ("no times here", (None, None)),
    ],
)
def test_time_range_parsing(text, expected):
    assert parse_time_range(text) == expected


# --- import ----------------------------------------------------------------

def _lessons():
    return [
        {
            "course": "ME3101",
            "start_at": datetime(2026, 9, 1, 9, 30),
            "end_at": datetime(2026, 9, 1, 12, 20),
            "location": "Room FG601",
            "notes": "",
        }
    ]


def test_import_creates_a_lesson_per_selected_robot(db, robots):
    ids = [robots[0].id, robots[1].id, robots[2].id]
    created = svc.import_lessons(db, _lessons(), ids)
    db.commit()

    assert len(created) == 3
    assert {r.resource_id for r in created} == set(ids)
    assert all(r.source_type == SourceType.LESSON for r in created)
    assert all(r.title == "ME3101" for r in created)
    assert "Room FG601" in created[0].details


def test_imported_lesson_blocks_a_later_booking_request(db, robots, make_request):
    from app.services import availability as av

    svc.import_lessons(db, _lessons(), [robots[2].id])
    db.commit()

    booking = make_request(start="2026-09-01", end="2026-09-01", sessions=("AM", "PM"))
    report = av.check_request(db, booking, robots[2])

    assert report.status == av.CONFLICT
    blocked = [s for s in report.preferred.slots if not s.available]
    assert [s.slot.label for s in blocked] == ["01 Sep AM"]


def test_lesson_conflicts_are_reported_before_import(db, robots, make_reservation):
    make_reservation(
        robots[0],
        "2026-09-01T08:30",
        "2026-09-01T12:00",
        source_type=SourceType.BOOKING,
        title="DING Changwen",
    )
    conflicts = svc.check_lesson_conflicts(db, _lessons(), [robots[0].id, robots[1].id])

    assert len(conflicts) == 1
    assert conflicts[0].resource.id == robots[0].id
    assert "DING Changwen" in conflicts[0].label


def test_import_does_not_overwrite_the_existing_reservation(db, robots, make_reservation):
    existing = make_reservation(
        robots[0],
        "2026-09-01T08:30",
        "2026-09-01T12:00",
        source_type=SourceType.BOOKING,
        title="DING Changwen",
    )
    svc.import_lessons(db, _lessons(), [robots[0].id])
    db.commit()
    db.refresh(existing)

    assert existing.status == "ACTIVE"
    assert existing.title == "DING Changwen"


def test_import_requires_rows_and_resources(db, robots):
    with pytest.raises(svc.ValidationError):
        svc.import_lessons(db, [], [robots[0].id])
    with pytest.raises(svc.ValidationError):
        svc.import_lessons(db, _lessons(), [])


def test_room_code_is_not_mistaken_for_a_course_code():
    parsed = parse_timetable("2026-09-10\t\t09:30-12:20\tRoom FG601")
    row = parsed.rows[0]
    assert row.course == "", "a venue column must never become the course code"
    assert row.location == "Room FG601"
    assert "No course code found" in row.warnings
    assert not row.is_complete


def test_notes_survive_when_there_is_no_venue_column():
    parsed = parse_timetable("2026-09-01\tME3101\t09:30-12:20\tbring safety glasses")
    assert parsed.rows[0].location == ""
    assert parsed.rows[0].notes == "bring safety glasses"


def test_single_line_free_text_still_parses():
    parsed = parse_timetable("ME3101 on 2026-09-01 from 09:30 to 12:20 in Room FG601")
    row = parsed.rows[0]
    assert row.course == "ME3101"
    assert row.day == date(2026, 9, 1)
    assert (row.start_time, row.end_time) == (time(9, 30), time(12, 20))
