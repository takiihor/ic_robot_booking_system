"""Regression tests for borrower identity colors."""
from __future__ import annotations

from app.models import borrower_text_hex


def test_known_borrowers_have_distinct_text_colors():
    colors = {
        borrower_text_hex("applicant:4"),
        borrower_text_hex("applicant:6"),
        borrower_text_hex("applicant:14"),
    }

    assert len(colors) == 3
