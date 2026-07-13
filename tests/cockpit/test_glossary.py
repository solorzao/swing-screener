"""Contract + anti-staleness guard for the cockpit glossary.

The glossary is a single JSON source (``cockpit-ui/src/lib/glossary.data.json``)
imported by TypeScript and read here. Its whole promise is that it never lies:
behavioural terms cite maintained truth (a North Star principle number and/or a
source file) instead of paraphrasing a mechanism that can silently rot. These
tests fail the build if that promise breaks -- a citation to a principle that
doesn't exist, or a file that isn't in the tree.

Deliberately import-free (pure path reads) so it runs regardless of where the
editable install points.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "cockpit-ui" / "src" / "lib" / "glossary.data.json"
NORTH_STAR = ROOT / "docs" / "NORTH_STAR.md"
SRC = ROOT / "src" / "swing_screener"

VALID_CATS = {
    "risk-metric",
    "status-lamp",
    "system-action",
    "provenance",
    "config",
    "strategy",
    "screen",
}
_PATH_RE = re.compile(r"[\w/]+\.py")
_PRINCIPLE_RE = re.compile(r"North Star #(\d+)")


def _terms() -> list[dict]:
    return json.loads(DATA.read_text(encoding="utf-8"))


def _principle_numbers() -> set[int]:
    """The North Star principle numbers, parsed from the doc (frozen at 1..9, but
    parsed rather than hard-coded so the truth stays the doc, not this test)."""
    nums: set[int] = set()
    for line in NORTH_STAR.read_text(encoding="utf-8").splitlines():
        m = re.match(r"(\d+)\.\s+\*\*", line)
        if m:
            nums.add(int(m.group(1)))
    return nums


def test_data_loads_and_is_nonempty() -> None:
    terms = _terms()
    assert isinstance(terms, list)
    assert len(terms) >= 100, f"expected the full vocabulary, got {len(terms)}"


def test_required_fields_and_categories() -> None:
    for t in _terms():
        for field in ("term", "cat", "short", "why"):
            val = t.get(field)
            assert isinstance(val, str) and val.strip(), f"{t.get('term')!r} missing {field}"
        assert t["cat"] in VALID_CATS, f"{t['term']!r} has unknown category {t['cat']!r}"
        if "risk" in t:
            assert t["risk"] in {"low", "medium", "high"}, f"{t['term']!r} bad risk {t['risk']!r}"
        if "appears" in t:
            assert isinstance(t["appears"], list) and all(isinstance(a, str) for a in t["appears"])


def test_terms_are_unique() -> None:
    terms = [t["term"] for t in _terms()]
    dupes = sorted({x for x in terms if terms.count(x) > 1})
    assert not dupes, f"duplicate glossary terms: {dupes}"


def test_source_citations_resolve() -> None:
    """Every behavioural term's ``source`` must point at something that exists: a
    real North Star principle number and/or a source file in the tree. A dangling
    citation is exactly the rot this glossary exists to avoid."""
    principles = _principle_numbers()
    assert principles, "could not parse any North Star principle numbers"

    cited = [t for t in _terms() if "source" in t]
    assert len(cited) >= 15, f"expected the behavioural terms to carry citations, got {len(cited)}"

    for t in cited:
        src = t["source"]
        anchored = False
        for n in _PRINCIPLE_RE.findall(src):
            assert int(n) in principles, f"{t['term']!r} cites North Star #{n}, which does not exist"
            anchored = True
        for rel in _PATH_RE.findall(src):
            assert (SRC / rel).exists(), f"{t['term']!r} cites {rel}, which is not in the tree"
            anchored = True
        assert anchored, f"{t['term']!r} citation has no checkable anchor: {src!r}"
