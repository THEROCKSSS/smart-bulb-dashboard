"""In-dashboard documentation library.

Serves the project's own Markdown to the dashboard so the docs are readable
where the thing they describe actually lives, rather than only on GitHub.

Two design points worth keeping:

  * **Discovery, not a hand-written list.** Docs are found by scanning known
    roots, so a new file appears in the dashboard the moment it's written.
    A registry would have gone stale the first time somebody added a doc and
    forgot the second edit.

  * **Slugs, never client paths.** The API takes an opaque slug and looks it
    up in a table built from the scan. A path from the client is never joined
    to a directory anywhere in this module, which is what makes
    `../../backend/config.json` structurally impossible rather than merely
    filtered. Anything outside the configured roots simply has no slug.
"""

import os
import re
import threading
import time

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BACKEND_DIR)

# (directory, recursive, category, order) -- `order` groups the sidebar so the
# things a person actually opens sit above the archive material.
DOC_ROOTS = [
    (os.path.join(PROJECT_ROOT, "docs"), False, None, 10),
    (PROJECT_ROOT, False, "Project", 20),
    (os.path.join(PROJECT_ROOT, "iterations"), True, "Build history", 30),
]

# Docs whose category is more useful than the directory they happen to sit in.
CATEGORY_BY_SLUG = {
    "audio-modes": "Audio",
    "audio-latency": "Audio",
    "music-reactive-lighting": "Audio",
    "remote-access-security": "Security",
    "pin-gate-threat-model": "Security",
    "security-secrets": "Security",
    "security": "Security",
    "backup-restore": "Operations",
    "deployment": "Operations",
    "observability": "Operations",
    "network-discovery": "Operations",
    "setup": "Getting started",
    "readme": "Getting started",
    "api": "Reference",
    "features": "Reference",
    "changelog": "Reference",
    "roadmap": "Reference",
}

# Not shown: agent-facing scaffolding and superseded proposals. They are still
# in the repo, they just aren't documentation for the person using the app.
HIDDEN_SLUGS = {"agents", "handoff", "feature-proposal-v2"}

CATEGORY_ORDER = ["Getting started", "Audio", "Operations", "Security",
                  "Reference", "Project", "Build history"]

MAX_SEARCH_RESULTS = 60
SNIPPET_CHARS = 160

_lock = threading.Lock()
_index = None          # slug -> entry
_indexed_at = 0.0
INDEX_TTL_S = 5.0      # re-scan at most this often; docs change rarely


def _slug_for(path):
    """A stable id derived from the filename, or the parent directory for the
    `iterations/*/README.md` shape where every file is called README."""
    rel = os.path.relpath(path, PROJECT_ROOT).replace("\\", "/")
    name = os.path.basename(path)
    if name.lower() == "readme.md" and "/" in rel:
        parent = os.path.basename(os.path.dirname(path))
        if parent and parent != os.path.basename(PROJECT_ROOT):
            return _slugify(parent)
    return _slugify(os.path.splitext(name)[0])


def _slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _title_of(text, fallback):
    """First H1, else the filename. Docs here all start with one."""
    for line in text.split("\n", 40)[:40]:
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _summary_of(text):
    """First real paragraph, for the list view. Skips the title, badges and
    horizontal rules so the blurb is a sentence rather than punctuation."""
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block or block.startswith(("#", "---", "|", "```", ">")):
            continue
        flat = re.sub(r"\s+", " ", re.sub(r"[*`_]", "", block))
        return flat[:220] + ("…" if len(flat) > 220 else "")
    return ""


def _scan():
    entries = {}
    for root, recursive, forced_category, order in DOC_ROOTS:
        if not os.path.isdir(root):
            continue
        walker = os.walk(root) if recursive else [(root, [], os.listdir(root))]
        for dirpath, _dirnames, filenames in walker:
            for name in filenames:
                if not name.lower().endswith(".md"):
                    continue
                path = os.path.join(dirpath, name)
                if not os.path.isfile(path):
                    continue
                slug = _slug_for(path)
                if slug in HIDDEN_SLUGS or slug in entries:
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        text = f.read()
                except OSError:
                    continue
                category = CATEGORY_BY_SLUG.get(slug) or forced_category or "Guides"
                entries[slug] = {
                    "slug": slug,
                    "title": _title_of(text, name),
                    "category": category,
                    "summary": _summary_of(text),
                    "path": os.path.relpath(path, PROJECT_ROOT).replace("\\", "/"),
                    "words": len(text.split()),
                    "modified": os.path.getmtime(path),
                    "_order": order,
                    "_text": text,
                }
    return entries


def _get_index(force=False):
    global _index, _indexed_at
    with _lock:
        if force or _index is None or (time.time() - _indexed_at) > INDEX_TTL_S:
            _index = _scan()
            _indexed_at = time.time()
        return _index


def _public(entry):
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def list_docs():
    """Every doc, grouped into ordered categories for the sidebar."""
    index = _get_index()
    by_cat = {}
    for entry in index.values():
        by_cat.setdefault(entry["category"], []).append(_public(entry))
    for docs in by_cat.values():
        docs.sort(key=lambda d: d["title"].lower())

    def cat_key(name):
        return (CATEGORY_ORDER.index(name) if name in CATEGORY_ORDER else 99, name)

    return {
        "categories": [
            {"name": name, "docs": by_cat[name]}
            for name in sorted(by_cat, key=cat_key)
        ],
        "total": len(index),
    }


def get_doc(slug):
    """Full text for one doc, or None. `slug` is only ever used as a dict key,
    so it cannot escape the configured roots."""
    entry = _get_index().get(slug)
    if not entry:
        return None
    return {**_public(entry), "content": entry["_text"]}


def _headings(text):
    return [
        {"level": len(m.group(1)), "text": m.group(2).strip()}
        for m in re.finditer(r"^(#{1,4})\s+(.+)$", text, re.M)
    ]


def outline(slug):
    entry = _get_index().get(slug)
    return _headings(entry["_text"]) if entry else None


def _plain(fragment):
    """Markdown syntax stripped out of a search snippet.

    A snippet cut out of a table row is mostly pipes and dashes, which tells
    the reader nothing about why the document matched. This keeps the words
    and drops the punctuation that only means something to a renderer."""
    text = fragment.replace("\n", " ")
    text = re.sub(r"`{3,}", " ", text)
    text = re.sub(r"\|[\s:|-]{3,}\|?", " ", text)      # table separator rows
    text = text.replace("|", " \u00b7 ")                # remaining cell borders
    text = re.sub(r"^#{1,6}\s*|\s#{1,6}\s", " ", text)
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"(?<!\w)[*_](?!\w)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)   # links -> label
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"(?:\s\u00b7\s)+", " \u00b7 ", text)
    return text.strip(" \u00b7-")


def search(query, limit=MAX_SEARCH_RESULTS):
    """Case-insensitive substring search across every doc.

    Returns a snippet per match with the query highlighted by offset rather
    than by embedding markup, so the caller decides how to render it and the
    API never emits HTML.
    """
    query = (query or "").strip()
    if len(query) < 2:
        return {"query": query, "results": [], "truncated": False}

    needle = query.lower()
    results = []
    for entry in _get_index().values():
        text = entry["_text"]
        haystack = text.lower()
        # Title matches rank above body matches -- someone typing "backup"
        # wants the Backup guide, not every doc that mentions backups.
        title_hit = needle in entry["title"].lower()
        positions = []
        start = 0
        while len(positions) < 5:
            i = haystack.find(needle, start)
            if i < 0:
                break
            positions.append(i)
            start = i + len(needle)
        if not positions and not title_hit:
            continue

        snippets = []
        for pos in positions:
            lo = max(0, pos - SNIPPET_CHARS // 2)
            hi = min(len(text), pos + len(query) + SNIPPET_CHARS // 2)
            raw = _plain(text[lo:hi])
            if not raw:
                continue
            snippets.append({
                "text": ("…" if lo > 0 else "") + raw + ("…" if hi < len(text) else ""),
                "offset": pos,
            })
        results.append({
            "slug": entry["slug"],
            "title": entry["title"],
            "category": entry["category"],
            "hits": haystack.count(needle),
            "title_match": title_hit,
            "snippets": snippets,
        })

    results.sort(key=lambda r: (not r["title_match"], -r["hits"], r["title"].lower()))
    truncated = len(results) > limit
    return {"query": query, "results": results[:limit], "truncated": truncated}
