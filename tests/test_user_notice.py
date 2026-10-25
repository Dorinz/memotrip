"""Which build warnings reach the user's banner (webapp.user_notice): only the
ones they can act on, in Hebrew; document conflicts and date checks stay in
the build log."""

from __future__ import annotations

import webapp


def test_conflicts_and_date_checks_never_reach_the_user():
    warnings = [
        "sources disagree: the PDF has Pico on 9 Aug, the DOCX has Velas; kept the PDF",
        "no lodging for 1 night(s) 2026-08-11 -> 2026-08-12 (between Velas and Pedro Miguel)",
        "ferry on 2026-08-10 falls in the middle of the Velas stay (2026-08-09 -> 2026-08-11)",
    ]
    assert webapp.user_notice(warnings) is None


def test_no_warnings_means_no_banner():
    assert webapp.user_notice([]) is None


def test_failed_extraction_tells_the_user_parts_may_be_missing():
    notice = webapp.user_notice(["the model's itinerary couldn't be used (see ...)"])
    assert "חסרים בדף חלקים מהטיול" in notice


def test_both_copy_failures_collapse_into_one_line():
    notice = webapp.user_notice(["trip-level copy failed: x", "day copy failed: y"])
    assert notice == "חלק מהטקסטים בדף לא נכתבו. אפשר לנסות לבנות את הדף שוב."


def test_user_lines_keep_order_and_skip_internal_ones():
    notice = webapp.user_notice(
        ["sources disagree: a", "no coordinates for: Velas", "day copy failed: z"]
    )
    assert notice.splitlines() == [
        "לא מצאנו את המיקום של חלק מהמקומות, ולכן הם לא מופיעים במפה.",
        "חלק מהטקסטים בדף לא נכתבו. אפשר לנסות לבנות את הדף שוב.",
    ]
