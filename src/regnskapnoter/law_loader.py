"""Time-aware fetcher for Norwegian law text.

Pulls law markdown from the norwegian-laws repo at the git tag corresponding
to a given fiscal year. Caches on disk by (tag, law_id) under
$XDG_CACHE_HOME/regnskapnoter/laws/ (or ~/.cache/...).

The repo (sondreskarsten/norwegian-laws, NLOD 2.0) ships annual tags
``v2001`` ... ``v2026``, each containing every active Norwegian formal law as
markdown under ``lover/{law_id}.md``. ``law_id`` is the Lovdata-style id, e.g.
``lov-1998-07-17-56`` for regnskapsloven.

Naive empiricism note: this module never returns "the latest version" without
an explicit caller decision. The caller must say which fiscal year to anchor
against, and the returned text is recorded as having come from that specific
tag. No retroactive re-interpretation of past events.
"""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO = "sondreskarsten/norwegian-laws"
RAW_BASE = "https://raw.githubusercontent.com"

EARLIEST_TAG_YEAR = 2001
LATEST_KNOWN_TAG_YEAR = 2026

# Concrete law ids for the citations the regnskapnoter taxonomy uses
LAW_IDS = {
    "regnskapsloven": "lov-1998-07-17-56",
    "skattebetalingsloven": "lov-2005-06-17-67",
    "selskapsloven": "lov-1985-06-21-83",
    "aksjeloven": "lov-1997-06-13-44",
    "allmennaksjeloven": "lov-1997-06-13-45",
    "verdipapirhandelloven": "lov-2007-06-29-75",
    "verdipapirfondloven": "lov-2011-11-25-44",
    "finansforetaksloven": "lov-2015-04-10-17",
    "bokføringsloven": "lov-2004-11-19-73",
    "stiftelsesloven": "lov-2001-06-15-59",
}


def cache_dir() -> Path:
    """Disk cache root. Override with $REGNSKAPNOTER_LAW_CACHE."""
    explicit = os.environ.get("REGNSKAPNOTER_LAW_CACHE")
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "regnskapnoter" / "laws"


def tag_for_fiscal_year(year: int) -> str:
    """Resolve a fiscal year to a git tag in norwegian-laws.

    Policy: the tag whose name matches the fiscal year. Pre-2001 filings fall
    back to ``v2001`` (earliest tag); future years beyond
    LATEST_KNOWN_TAG_YEAR fall back to that latest known tag with a warning
    suppressed at this layer (caller can detect the clamp from the returned
    tag string differing from ``v{year}``).
    """
    clamped = max(EARLIEST_TAG_YEAR, min(int(year), LATEST_KNOWN_TAG_YEAR))
    return f"v{clamped}"


def resolve_law_id(name_or_id: str) -> str:
    """Map a colloquial law name to its Lovdata id, or return the id as-is."""
    s = (name_or_id or "").strip().lower()
    if s in LAW_IDS:
        return LAW_IDS[s]
    if s.startswith("lov-") or s.startswith("forskrift-"):
        return s
    raise ValueError(f"Unknown law name: {name_or_id!r}; pass an explicit lov-* id")


@dataclass(frozen=True)
class LawDocument:
    """One law's full markdown at a specific tag."""

    law_id: str
    tag: str
    text: str
    sist_endret: str | None
    ikrafttredelse: str | None


def fetch_law(name_or_id: str, fiscal_year: int) -> LawDocument:
    """Fetch a law's markdown at the contemporaneous tag, with disk caching."""
    law_id = resolve_law_id(name_or_id)
    tag = tag_for_fiscal_year(fiscal_year)
    cache_path = cache_dir() / tag / f"{law_id}.md"
    if cache_path.is_file():
        text = cache_path.read_text(encoding="utf-8")
    else:
        url = f"{RAW_BASE}/{REPO}/{tag}/lover/{law_id}.md"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                text = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raise FileNotFoundError(
                f"law not found: {law_id} at {tag} (url={url}, status={e.code})"
            ) from e
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(text, encoding="utf-8")

    sist_endret = None
    ikraft = None
    m = re.search(r'sist-endret:\s*"([^"]+)"', text)
    if m:
        sist_endret = m.group(1)
    m2 = re.search(r'sist-endret-ikrafttredelse:\s*"([^"]+)"', text)
    if m2:
        ikraft = m2.group(1)

    return LawDocument(
        law_id=law_id,
        tag=tag,
        text=text,
        sist_endret=sist_endret,
        ikrafttredelse=ikraft,
    )


# ---------------------------------------------------------------------------
# Paragraph extractor
# ---------------------------------------------------------------------------

# A heading like '#### § 7-29. Skattekostnad' starts a section; we capture
# everything up to the next section heading.
_SECTION_HEADING_RE = re.compile(
    r"^####\s*§\s*(?P<num>\d+(?:[\u2010-]\d+)?(?:[a-z])?)(?:\.|\b)\s*(?P<title>[^\n]*)$",
    re.MULTILINE,
)


def _normalize_paragraph_id(s: str) -> str:
    """Strip noise from a paragraph reference like '§ 7-29' -> '7-29'."""
    s = s.replace("§", "").replace("\u00a7", "").strip()
    s = s.split("(", 1)[0].strip()  # drop subparagraph hint
    s = s.replace(" ", "")
    s = s.replace("\u2010", "-")
    return s


def extract_paragraph(law: LawDocument, citation: str) -> str | None:
    """Return the markdown of a paragraph in a law, e.g. '§ 7-29' or '7-29'.

    Returns None if not found. If ``citation`` includes a subparagraph marker
    like '§ 7-29 (3)', the returned text is the subparagraph (3) only —
    or, if the subparagraph cannot be located, the full paragraph is returned
    so the caller still has context.
    """
    target = _normalize_paragraph_id(citation)
    matches = list(_SECTION_HEADING_RE.finditer(law.text))
    if not matches:
        return None

    for i, m in enumerate(matches):
        if _normalize_paragraph_id(m.group("num")) != target:
            continue
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(law.text)
        section = law.text[start:end].strip()

        sub = _extract_subparagraph(citation)
        if sub is not None:
            sub_text = _slice_subparagraph(section, sub)
            if sub_text:
                return f"§ {target} ({sub}) [extracted from {law.law_id}@{law.tag}]\n\n{sub_text}"
        return section

    return None


_SUBPARA_RE = re.compile(r"\((\d+)\)")


def _extract_subparagraph(citation: str) -> int | None:
    """Pull the subparagraph number from a citation like '§ 7-29 (3)'."""
    m = _SUBPARA_RE.search(citation)
    return int(m.group(1)) if m else None


def _slice_subparagraph(section_text: str, n: int) -> str | None:
    """Within a section, find subparagraph (n) and return until (n+1) or end."""
    pat = re.compile(rf"^\(\s*{n}\s*\)", re.MULTILINE)
    m = pat.search(section_text)
    if not m:
        return None
    next_pat = re.compile(rf"^\(\s*{n + 1}\s*\)", re.MULTILINE)
    m2 = next_pat.search(section_text, m.end())
    end = m2.start() if m2 else len(section_text)
    return section_text[m.start() : end].strip()


# ---------------------------------------------------------------------------
# Higher-level helper: get the text for a (publisher, document, paragraph) ref
# ---------------------------------------------------------------------------


def fetch_paragraph_text(
    publisher: str,
    document: str,
    paragraph: str,
    fiscal_year: int,
) -> tuple[str | None, str | None]:
    """Fetch the contemporaneous paragraph text for a taxonomy reference.

    Returns (text, source_tag) where source_tag is e.g. 'v2024' so the caller
    can record it. Returns (None, None) if the publisher is not Stortinget
    (the only publisher this loader currently covers — NRS is out of scope).
    """
    if publisher != "Stortinget":
        return None, None
    try:
        law = fetch_law(document, fiscal_year)
    except (ValueError, FileNotFoundError):
        return None, None
    text = extract_paragraph(law, paragraph)
    return text, law.tag
