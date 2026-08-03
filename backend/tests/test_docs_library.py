"""In-dashboard documentation browser.

The security-relevant part is that a slug can never escape the configured
roots. That is a structural property here -- slugs are dict keys, never joined
to a path -- and these tests exist to keep it structural.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import docs_library as dl  # noqa: E402


def test_discovers_the_projects_real_docs():
    listing = dl.list_docs()
    assert listing["total"] >= 15
    slugs = {d["slug"] for c in listing["categories"] for d in c["docs"]}
    # The doc this browser was built to surface.
    assert "audio-modes" in slugs


def test_categories_come_back_in_a_deliberate_order():
    names = [c["name"] for c in dl.list_docs()["categories"]]
    known = [n for n in names if n in dl.CATEGORY_ORDER]
    assert known == sorted(known, key=dl.CATEGORY_ORDER.index)


def test_every_listed_doc_can_actually_be_opened():
    """A slug in the sidebar that 404s on click is the obvious failure here."""
    for cat in dl.list_docs()["categories"]:
        for entry in cat["docs"]:
            doc = dl.get_doc(entry["slug"])
            assert doc is not None, entry["slug"]
            assert doc["content"].strip()


def test_listing_never_ships_document_bodies():
    """The sidebar payload must stay small -- shipping every doc's full text
    on page load would be megabytes for a list of titles."""
    for cat in dl.list_docs()["categories"]:
        for entry in cat["docs"]:
            assert "content" not in entry
            assert not any(k.startswith("_") for k in entry)


TRAVERSAL_ATTEMPTS = [
    "../backend/config",
    "../../etc/passwd",
    "..%2F..%2Fbackend%2Fconfig",
    "....//config",
    "/etc/passwd",
    "backend/config",
    "config",
    "",
]


def test_traversal_and_unknown_slugs_return_nothing(check_all):
    """Not 'is sanitised' -- there is no path to sanitise. Anything without a
    discovered slug simply has no entry.

    All 8 attempts in one test: if slug lookup ever regresses into path
    resolution, you want the full list of what got through in one report.
    """
    def _returns_nothing(attempt):
        got = dl.get_doc(attempt)
        assert got is None, f"resolved to {got!r}"

    check_all(TRAVERSAL_ATTEMPTS, _returns_nothing, label="traversal attempt",
              name=repr)


def test_hidden_docs_stay_hidden():
    slugs = {d["slug"] for c in dl.list_docs()["categories"] for d in c["docs"]}
    assert not (slugs & dl.HIDDEN_SLUGS)


# ----------------------------------------------------------------- search


def test_search_finds_a_known_phrase():
    res = dl.search("dwell")
    assert res["results"]
    assert any(r["slug"] == "audio-modes" for r in res["results"])


def test_search_ranks_title_matches_first():
    res = dl.search("backup")
    assert res["results"][0]["title_match"] is True


def test_search_is_case_insensitive():
    assert len(dl.search("DWELL")["results"]) == len(dl.search("dwell")["results"])


def test_search_ignores_a_too_short_query():
    """One character would match nearly every document and return noise."""
    assert dl.search("a")["results"] == []
    assert dl.search("")["results"] == []


def test_snippets_are_readable_prose_not_markdown_scaffolding():
    """A snippet cut from a table used to come back as mostly pipes and
    dashes, which says nothing about why the doc matched."""
    res = dl.search("party")
    snippets = [s["text"] for r in res["results"] for s in r["snippets"]]
    assert snippets
    for text in snippets:
        assert "|---" not in text
        assert "**" not in text
        assert "`" not in text


def test_search_reports_offsets_not_markup():
    """The API must never emit HTML -- the caller decides how to highlight."""
    for r in dl.search("party")["results"]:
        for s in r["snippets"]:
            assert "<" not in s["text"]
            assert isinstance(s["offset"], int)


def test_outline_returns_headings_for_a_real_doc():
    headings = dl.outline("audio-modes")
    assert headings and headings[0]["level"] == 1
    assert dl.outline("no-such-doc") is None
