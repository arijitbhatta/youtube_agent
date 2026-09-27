"""Pure (network-free) tests for agent/recommendations.py -- URL building,
payload parsing, and search-link construction. The live HTTP/LLM paths are
exercised manually, not here (matches the no-network test convention)."""
from __future__ import annotations

from agent.recommendations import (
    _CoursePlan,
    _RawCourse,
    _books_from_payload,
    _books_url,
    _course_search_link,
    Course,
)


def test_books_url_encodes_query_and_limit():
    url = _books_url("learn git", 3)
    assert url.startswith("https://www.googleapis.com/books/v1/volumes?")
    assert "q=learn+git" in url
    assert "maxResults=3" in url
    assert "printType=books" in url
    assert "key=" not in url  # keyless by default


def test_books_url_appends_key_when_configured():
    url = _books_url("learn git", 3, key="abc123")
    assert "key=abc123" in url


def test_books_from_payload_extracts_fields_and_tolerates_gaps():
    payload = {
        "items": [
            {
                "volumeInfo": {
                    "title": "Pro Git",
                    "authors": ["Scott Chacon"],
                    "infoLink": "https://books/1",
                }
            },
            {
                "volumeInfo": {
                    "title": "No Link Book",
                    "canonicalVolumeLink": "https://books/2",
                }
            },
            {"volumeInfo": {"authors": ["no title here"]}},  # skipped: no title
            {"something": "else"},  # skipped: no volumeInfo
        ]
    }
    books = _books_from_payload(payload)
    assert [b.title for b in books] == ["Pro Git", "No Link Book"]
    assert books[0].authors == ["Scott Chacon"]
    assert books[0].link == "https://books/1"
    assert books[1].link == "https://books/2"
    assert books[1].authors == []


def test_books_from_payload_empty():
    assert _books_from_payload({}) == []
    assert _books_from_payload({"items": []}) == []


def test_course_search_link_plus_encodes():
    assert _course_search_link("udemy", "React TypeScript") == (
        "https://www.udemy.com/courses/search/?q=React+TypeScript"
    )
    assert _course_search_link("coursera", "Machine Learning") == (
        "https://www.coursera.org/search?query=Machine+Learning"
    )


def test_raw_course_round_trips_to_public_course_with_link():
    raw = _RawCourse(platform="coursera", title="Machine Learning", why="theory foundations")
    plan = _CoursePlan(courses=[raw])
    public = Course(
        platform=raw.platform,
        title=raw.title,
        why=raw.why,
        search_link=_course_search_link(raw.platform, raw.title),
    )
    assert plan.courses[0].title == "Machine Learning"
    assert public.search_link == "https://www.coursera.org/search?query=Machine+Learning"