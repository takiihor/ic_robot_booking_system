"""End-to-end HTTP tests over the real routes — SPEC 24 acceptance criteria."""
from __future__ import annotations

import re

import pytest

from tests.conftest import SAMPLE_EMAIL

TIMETABLE = (
    "Date\tCourse\tTime\tVenue\n"
    "2026-09-01\tME3101\t09:30-12:20\tRoom FG601\n"
)


@pytest.fixture
def seeded(client):
    """Five UR10e robots created through the resources page."""
    for index in range(1, 6):
        response = client.post(
            "/resources",
            data={
                "name": f"UR10e ({index:02d})",
                "model": "UR10e",
                "resource_group": "Collaborative Robots",
                "location": "Industrial Robot Lab",
                "status": "Out of Service" if index == 4 else "Active",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
    return client


def submit(client, path: str, data: dict):
    """POST without following the redirect, so the 303 and Location stay visible."""
    return client.post(path, data=data, follow_redirects=False)


def created_request_url(response) -> str:
    assert response.status_code == 303, response.text[:2000]
    return response.headers["location"].split("?")[0]


def _resource_ids(client) -> dict[str, int]:
    from app.models import Resource

    with client.db_factory() as db:
        return {r.name: r.id for r in db.query(Resource).order_by(Resource.name)}


# --- pages load ------------------------------------------------------------

@pytest.mark.parametrize(
    "path", ["/", "/calendar", "/requests", "/requests/new", "/lessons", "/resources", "/search"]
)
def test_every_page_loads(seeded, path):
    response = seeded.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_calendar_shows_every_configured_robot(seeded):
    body = seeded.get("/calendar?date=2026-08-31").text
    for index in range(1, 6):
        assert f"UR10e ({index:02d})" in body


def test_unknown_page_renders_the_error_template(client):
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert "Page not found" in response.text


# --- the acceptance workflow ----------------------------------------------

def test_full_booking_workflow(seeded):
    ids = _resource_ids(seeded)

    # 4-5. Paste and parse the email.
    parsed = seeded.post("/requests/parse", data={"raw_text": SAMPLE_EMAIL})
    assert parsed.status_code == 200
    assert "DING Changwen" in parsed.text
    assert "25104512r" in parsed.text
    assert "2026-08-31" in parsed.text
    # The preferred robot found in Remarks is pre-selected in the dropdown.
    assert re.search(
        rf'value="{ids["UR10e (03)"]}"\s+selected', parsed.text
    ), "preferred robot should be pre-selected"

    # 6-7. Save the reviewed preview and check availability.
    saved = submit(
        seeded,
        "/requests",
        {
            "raw_text": SAMPLE_EMAIL,
            "name": "DING Changwen",
            "sid_netid": "25104512r",
            "department": "Mechanical Engineering",
            "email": "changwen.ding@connect.polyu.hk",
            "phone": "5123 4567",
            "response_id": "R-2026-0142",
            "facility": "Industrial Robot Laboratory",
            "booking_type": "Research",
            "start_date": "2026-08-31",
            "end_date": "2026-09-04",
            "sessions": ["AM", "PM"],
            "preferred_resource_id": str(ids["UR10e (03)"]),
            "purpose": "Robotic grasping experiments",
            "remarks": "Prefer UR10e (03)",
            "action": "check",
        },
    )
    request_url = created_request_url(saved)

    # 8. Every requested slot is listed.
    detail = seeded.get(f"{request_url}?check=1")
    assert "AVAILABLE" in detail.text
    assert "31 Aug AM" in detail.text
    assert "04 Sep PM" in detail.text

    # 10. Accept.
    approved = seeded.post(
        f"{request_url}/approve", data={"resource_id": str(ids["UR10e (03)"])}
    )
    assert "APPROVED" in approved.text

    # 11. The applicant name shows on the selected robot's calendar.
    assert "DING Changwen" in seeded.get("/calendar?date=2026-08-31").text

    # 12. Clicking the booking shows the applicant and request details.
    from app.models import Reservation

    with seeded.db_factory() as db:
        reservation_id = db.query(Reservation).first().id
    modal = seeded.get(f"/reservations/{reservation_id}/detail")
    assert modal.status_code == 200
    assert "changwen.ding@connect.polyu.hk" in modal.text
    assert "Mechanical Engineering" in modal.text
    assert "Robotic grasping experiments" in modal.text

    # 13. Search by name and by SID, then open the applicant history.
    assert "DING Changwen" in seeded.get("/search?q=DING").text
    search = seeded.get("/search?q=25104512r")
    assert "DING Changwen" in search.text
    applicant_id = re.search(r'/applicants/(\d+)', search.text).group(1)
    history = seeded.get(f"/applicants/{applicant_id}")
    assert "changwen.ding@connect.polyu.hk" in history.text
    assert "Robotic grasping experiments" in history.text


def test_conflicting_request_shows_conflicts_and_free_alternatives(seeded):
    ids = _resource_ids(seeded)
    common = {
        "start_date": "2026-08-31",
        "end_date": "2026-08-31",
        "sessions": ["AM"],
        "preferred_resource_id": str(ids["UR10e (03)"]),
        "department": "ME",
        "email": "",
        "phone": "",
        "facility": "",
        "booking_type": "Research",
        "purpose": "",
        "remarks": "",
        "raw_text": "",
        "action": "save",
    }

    first_url = created_request_url(
        submit(
            seeded,
            "/requests",
            {**common, "name": "DING Changwen", "sid_netid": "25104512r", "response_id": "R-1"},
        )
    )
    seeded.post(f"{first_url}/approve", data={"resource_id": str(ids["UR10e (03)"])})

    second_url = created_request_url(
        submit(
            seeded,
            "/requests",
            {**common, "name": "CHAN Tai Man", "sid_netid": "20000000a", "response_id": "R-2"},
        )
    )

    checked = seeded.post(f"{second_url}/check", data={"resource_id": str(ids["UR10e (03)"])})
    assert "CONFLICT" in checked.text
    assert "DING Changwen" in checked.text          # what is blocking it
    assert "Fully Available" in checked.text        # alternatives offered
    assert "UR10e (04)" not in checked.text.split("Alternative robots")[1]  # never suggested

    # Approving onto the taken robot must be refused.
    refused = seeded.post(f"{second_url}/approve", data={"resource_id": str(ids["UR10e (03)"])})
    assert "no longer free" in refused.text

    # Approving onto a free alternative works.
    ok = seeded.post(f"{second_url}/approve", data={"resource_id": str(ids["UR10e (01)"])})
    assert "APPROVED" in ok.text


def test_lesson_import_then_conflict(seeded):
    ids = _resource_ids(seeded)

    parsed = seeded.post("/lessons/parse", data={"raw_text": TIMETABLE})
    assert "ME3101" in parsed.text
    assert "09:30" in parsed.text

    imported = submit(
        seeded,
        "/lessons/import",
        {
            "raw_text": TIMETABLE,
            "keep": ["0"],
            "course": ["ME3101"],
            "date": ["2026-09-01"],
            "start_time": ["09:30"],
            "end_time": ["12:20"],
            "location": ["Room FG601"],
            "notes": [""],
            "resource_ids": [str(ids["UR10e (01)"]), str(ids["UR10e (03)"])],
        },
    )
    assert imported.status_code == 303

    # 15. Lessons appear on the calendar.
    calendar = seeded.get("/calendar?date=2026-09-01").text
    assert "ME3101" in calendar

    # 16. A later booking conflicts with the imported lesson.
    url = created_request_url(
        submit(
            seeded,
            "/requests",
            {
                "name": "CHAN Tai Man",
                "sid_netid": "20000000a",
                "start_date": "2026-09-01",
                "end_date": "2026-09-01",
                "sessions": ["AM"],
                "preferred_resource_id": str(ids["UR10e (03)"]),
                "action": "save",
            },
        )
    )
    checked = seeded.post(f"{url}/check", data={"resource_id": str(ids["UR10e (03)"])})
    assert "CONFLICT" in checked.text
    assert "ME3101" in checked.text


def test_lesson_import_requires_confirmation_when_it_clashes(seeded):
    ids = _resource_ids(seeded)
    payload = {
        "raw_text": TIMETABLE,
        "keep": ["0"],
        "course": ["ME3101"],
        "date": ["2026-09-01"],
        "start_time": ["09:30"],
        "end_time": ["12:20"],
        "location": [""],
        "notes": [""],
        "resource_ids": [str(ids["UR10e (01)"])],
    }
    assert submit(seeded, "/lessons/import", payload).status_code == 303

    # Importing the same lesson again now clashes with itself.
    blocked = submit(seeded, "/lessons/import", payload)
    assert blocked.status_code == 200
    assert "clash with existing reservations" in blocked.text

    confirmed = submit(seeded, "/lessons/import", {**payload, "confirm_conflicts": "1"})
    assert confirmed.status_code == 303


def test_lesson_import_rejects_incomplete_rows(seeded):
    ids = _resource_ids(seeded)
    response = submit(
        seeded,
        "/lessons/import",
        {
            "raw_text": TIMETABLE,
            "keep": ["0"],
            "course": [""],
            "date": ["2026-09-01"],
            "start_time": ["09:30"],
            "end_time": ["12:20"],
            "location": [""],
            "notes": [""],
            "resource_ids": [str(ids["UR10e (01)"])],
        },
    )
    assert response.status_code == 200
    assert "course/module is required" in response.text


# --- reject / cancel -------------------------------------------------------

def test_reject_and_cancel_keep_history(seeded):
    ids = _resource_ids(seeded)
    base = {
        "start_date": "2026-08-31",
        "end_date": "2026-08-31",
        "sessions": ["AM"],
        "preferred_resource_id": str(ids["UR10e (05)"]),
        "action": "save",
    }

    rejected_url = created_request_url(
        submit(
            seeded,
            "/requests",
            {**base, "name": "A Person", "sid_netid": "111", "response_id": "R-R"},
        )
    )
    page = seeded.post(f"{rejected_url}/reject", data={"rejection_reason": "Teaching week"})
    assert "REJECTED" in page.text
    assert "Teaching week" in page.text

    approved_url = created_request_url(
        submit(
            seeded,
            "/requests",
            {**base, "name": "B Person", "sid_netid": "222", "response_id": "R-C"},
        )
    )
    seeded.post(f"{approved_url}/approve", data={"resource_id": str(ids["UR10e (05)"])})
    cancelled = seeded.post(f"{approved_url}/cancel", data={"cancel_reason": "Withdrawn"})
    assert "CANCELLED" in cancelled.text

    # Both remain in the requests list — nothing is deleted.
    listing = seeded.get("/requests").text
    assert "A Person" in listing and "B Person" in listing
    # The slot is free again.
    assert "B Person" not in seeded.get("/calendar?date=2026-08-31").text


def test_requests_list_filters_by_status(seeded):
    ids = _resource_ids(seeded)
    seeded.post(
        "/requests",
        data={
            "name": "Pending Person",
            "sid_netid": "333",
            "start_date": "2026-08-31",
            "end_date": "2026-08-31",
            "sessions": ["AM"],
            "preferred_resource_id": str(ids["UR10e (05)"]),
            "action": "save",
        },
    )
    assert "Pending Person" in seeded.get("/requests?status=PENDING").text
    assert "Pending Person" not in seeded.get("/requests?status=APPROVED").text


def test_invalid_request_is_returned_to_the_preview_with_errors(seeded):
    response = seeded.post("/requests", data={"name": "", "start_date": "", "action": "save"})
    assert response.status_code == 200
    assert "Applicant name is required." in response.text
    assert "At least one session" in response.text


# --- manual reservations and resources -------------------------------------

def test_maintenance_block_from_the_calendar(seeded):
    ids = _resource_ids(seeded)
    response = seeded.post(
        "/reservations",
        data={
            "resource_id": str(ids["UR10e (02)"]),
            "source_type": "MAINTENANCE",
            "title": "Annual calibration",
            "start_at": "2026-08-31T08:00",
            "end_at": "2026-08-31T18:00",
            "details": "Vendor on site",
            "redirect_to": "/calendar?date=2026-08-31",
        },
        follow_redirects=True,
    )
    assert "Annual calibration" in response.text


def test_resource_can_be_taken_out_of_service(seeded):
    ids = _resource_ids(seeded)
    response = seeded.post(
        f"/resources/{ids['UR10e (05)']}/update",
        data={
            "name": "UR10e (05)",
            "model": "UR10e",
            "resource_group": "Collaborative Robots",
            "status": "Out of Service",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Out of Service" in response.text


def test_duplicate_resource_name_is_rejected(seeded):
    response = seeded.post(
        "/resources",
        data={"name": "UR10e (01)", "resource_group": "Collaborative Robots", "status": "Active"},
        follow_redirects=True,
    )
    assert "already exists" in response.text


# --- persistence -----------------------------------------------------------

def test_data_persists_across_an_application_restart(seeded):
    from fastapi.testclient import TestClient

    from app.main import app

    ids = _resource_ids(seeded)
    seeded.post(
        "/requests",
        data={
            "name": "Persistent Person",
            "sid_netid": "444",
            "start_date": "2026-08-31",
            "end_date": "2026-08-31",
            "sessions": ["AM"],
            "preferred_resource_id": str(ids["UR10e (05)"]),
            "action": "save",
        },
    )

    with TestClient(app) as restarted:  # a fresh lifespan against the same file
        assert "Persistent Person" in restarted.get("/requests").text
        assert "UR10e (01)" in restarted.get("/resources").text


# --- same-origin protection (SPEC 20) --------------------------------------

def test_cross_site_post_is_rejected(seeded):
    response = seeded.post(
        "/resources",
        data={"name": "Sneaky", "resource_group": "X", "status": "Active"},
        headers={"origin": "http://evil.example.com"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "Sneaky" not in seeded.get("/resources").text


def test_same_origin_post_is_allowed(seeded):
    response = seeded.post(
        "/resources",
        data={"name": "UR10e (06)", "resource_group": "Collaborative Robots", "status": "Active"},
        headers={"origin": "http://testserver"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "UR10e (06)" in response.text


def test_pasted_text_is_escaped_not_rendered(seeded):
    payload = "Name: <script>alert('xss')</script>\nDate: 2026-08-31\nSession: AM"
    response = seeded.post("/requests/parse", data={"raw_text": payload})
    assert "<script>alert" not in response.text
    assert "&lt;script&gt;" in response.text
