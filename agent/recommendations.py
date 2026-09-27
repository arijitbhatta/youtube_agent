"""UI-only "while you waited" fallback recommendations (HLD.md §5.2
neighbour): when a frontend run has taken longer than FALLBACK_DELAY_SECONDS
(see ui/app.py), the frontend asks this module for relevant books (real, from
the Google Books API -- keyless by default, but see config.GOOGLE_BOOKS_API_KEY
for the opt-in quota raise) and Udemy/Coursera courses (LLM-suggested, since
neither site has a free public search API; links are platform *search* URLs
built here in code, never invented by the model).

Not pipeline logic and not persisted to the trace -- a UI convenience surface,
like agent/run_cache.py. A network/LLM failure here degrades to an empty list,
never an exception that would crash the UI thread after the pipeline has
already succeeded.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Literal

from pydantic import BaseModel, Field

from agent.config import GOOGLE_BOOKS_API_KEY, api_key_present
from agent.llm_client import call_structured

BOOKS_ENDPOINT = "https://www.googleapis.com/books/v1/volumes"


class Book(BaseModel):
    title: str
    authors: list[str] = Field(default_factory=list)
    link: str


class Course(BaseModel):
    platform: Literal["udemy", "coursera"]
    title: str
    why: str
    search_link: str


class Recommendations(BaseModel):
    query: str
    books: list[Book] = Field(default_factory=list)
    courses: list[Course] = Field(default_factory=list)


# --- books: real, from the Google Books API --------------------------------


def _books_url(query: str, limit: int, key: str = "") -> str:
    params = {
        "q": query,
        "maxResults": str(limit),
        "printType": "books",
        "country": "US",
    }
    # Keyless use falls on a tiny anonymous per-IP daily quota (seen: 429
    # "Queries per day" in this environment); an optional per-project
    # GOOGLE_BOOKS_API_KEY lifts that ceiling without changing the response.
    if key:
        params["key"] = key
    return f"{BOOKS_ENDPOINT}?{urllib.parse.urlencode(params)}"


def _books_from_payload(payload: dict) -> list[Book]:
    """Parse just what the UI needs out of a Google Books volumes response --
    tolerant of missing fields (a real response is inconsistent about
    authors/infoLink), skipping any item without a title."""
    books: list[Book] = []
    for item in payload.get("items") or []:
        volume = item.get("volumeInfo") or {}
        title = volume.get("title")
        if not title:
            continue
        link = (
            volume.get("infoLink")
            or volume.get("canonicalVolumeLink")
            or item.get("selfLink")
            or ""
        )
        books.append(Book(title=title, authors=volume.get("authors") or [], link=link))
    return books


def _google_books(query: str, limit: int) -> list[Book]:
    # Google rejects requests without a User-Agent; plain urllib defaults to
    # one, but set it explicitly so the intent is clear and not UA-dependent.
    req = urllib.request.Request(
        _books_url(query, limit, GOOGLE_BOOKS_API_KEY),
        headers={"User-Agent": "Mozilla/5.0 (curriculum-builder)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    return _books_from_payload(payload)


# --- courses: LLM-suggested, code-built search links -------------------------


class _RawCourse(BaseModel):
    platform: Literal["udemy", "coursera"]
    title: str
    why: str


class _CoursePlan(BaseModel):
    courses: list[_RawCourse] = Field(min_length=1, max_length=6)


_COURSE_SYSTEM = """You recommend a few well-known online courses to a learner \
as a "while you were waiting" suggestion. Name only real, well-established \
Udemy or Coursera courses that plausibly exist and match the topic. Prefer \
courses you are confident are real; if unsure, describe the general course \
someone would look for rather than inventing a specific fabricated name. Do \
NOT invent a URL -- the caller builds its own search link."""

_COURSE_PROMPT = """Topic the learner is working on: {query}

Suggest up to {limit} courses (a mix of udemy and coursera). For each, give \
platform ("udemy" or "coursera"), a concise title, and a one-line `why` of \
what it covers and why it fits this learner.
"""


def _course_search_link(platform: str, title: str) -> str:
    q = urllib.parse.quote_plus(title)
    if platform == "udemy":
        return f"https://www.udemy.com/courses/search/?q={q}"
    return f"https://www.coursera.org/search?query={q}"


def _llm_courses(query: str, limit: int) -> list[Course]:
    if not api_key_present():
        return []
    try:
        plan = call_structured(
            _COURSE_PROMPT.format(query=query, limit=limit),
            _CoursePlan,
            tier="cheap",
            system=_COURSE_SYSTEM,
            max_tokens=1024,
        )
    except Exception:
        return []
    courses: list[Course] = []
    for raw in plan.courses[:limit]:
        courses.append(
            Course(
                platform=raw.platform,
                title=raw.title,
                why=raw.why,
                search_link=_course_search_link(raw.platform, raw.title),
            )
        )
    return courses


def get_recommendations(goal: str, topics: list[str], *, limit: int = 3) -> Recommendations:
    """Entrypoint ui/app.py calls after a slow run. `query` is the goal plus
    the learner's unknown topics -- enough for a topical book search. The
    course half is best-effort (empty if the API key is absent or the LLM
    call fails); the book half is real whenever the network is reachable."""
    query = " ".join([goal, *topics]).strip()
    return Recommendations(
        query=query,
        books=_google_books(query, limit),
        courses=_llm_courses(query, limit),
    )