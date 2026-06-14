"""Fetcher for Norwegian law text from the norwegian-laws repository.

Reads consolidated law markdown published at
https://github.com/sondreskarsten/norwegian-laws (raw ``lover/{law_id}.md``),
whose parser now has full coverage of nested sub-chapters such as
regnskapsloven kapittel 7. There is no lovdata.no scraping and no fallback:
if the law markdown cannot be fetched, this module fails loud.

Disk-caches fetched law markdown by ``law_id`` under
$XDG_CACHE_HOME/regnskapnoter/laws/.

Naive empiricism note: the caller must specify a fiscal year. The returned
text is the *current* consolidated version published by norwegian-laws —
there is no PIT reconstruction yet.
"""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

LAW_IDS = {
    "regnskapsloven": "lov/1998-07-17-56",
    "skattebetalingsloven": "lov/2005-06-17-67",
    "selskapsloven": "lov/1985-06-21-83",
    "aksjeloven": "lov/1997-06-13-44",
    "allmennaksjeloven": "lov/1997-06-13-45",
    "verdipapirhandelloven": "lov/2007-06-29-75",
    "verdipapirfondloven": "lov/2011-11-25-44",
    "finansforetaksloven": "lov/2015-04-10-17",
    "bokføringsloven": "lov/2004-11-19-73",
    "stiftelsesloven": "lov/2001-06-15-59",
    "skatteloven": "lov/1999-03-26-14",
    "OTP-loven": "lov/2005-12-21-124",
}

NL_RAW_BASE = "https://raw.githubusercontent.com/sondreskarsten/norwegian-laws/main/lover"


def cache_dir() -> Path:
    explicit = os.environ.get("REGNSKAPNOTER_LAW_CACHE")
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "regnskapnoter" / "laws"


def resolve_law_id(name_or_id: str) -> str:
    s = (name_or_id or "").strip()
    if s in LAW_IDS:
        return LAW_IDS[s]
    s_lower = s.lower()
    for k, v in LAW_IDS.items():
        if k.lower() == s_lower:
            return v
    if s.startswith("lov/") or s.startswith("forskrift/"):
        return s
    raise ValueError(f"Unknown law: {name_or_id!r}")


@dataclass(frozen=True)
class LawDocument:
    law_id: str
    markdown: str
    sist_endret: str | None


def _fetch_law_markdown(law_id: str) -> str:
    url = f"{NL_RAW_BASE}/{law_id.replace('/', '-')}.md"
    req = urllib.request.Request(url, headers={"User-Agent": "regnskapnoter-py/0.9"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise FileNotFoundError(
            f"{law_id} not found in norwegian-laws (status={e.code})"
        ) from e


def fetch_law(name_or_id: str) -> LawDocument:
    law_id = resolve_law_id(name_or_id)
    safe_name = law_id.replace("/", "-")
    cache_path = cache_dir() / f"{safe_name}.md"
    if cache_path.is_file():
        markdown = cache_path.read_text(encoding="utf-8")
    else:
        markdown = _fetch_law_markdown(law_id)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(markdown, encoding="utf-8")

    return LawDocument(law_id=law_id, markdown=markdown, sist_endret=None)


_PARA_ID_RE = re.compile(r"§\s*(\d+(?:-\d+[a-z]?))")


def _normalize_paragraph_id(s: str) -> str:
    s = s.replace("§", "").replace("\u00a7", "").strip()
    s = s.split("(", 1)[0].strip().replace(" ", "").replace("\u2010", "-")
    return s


def extract_paragraph(law: LawDocument, citation: str) -> str | None:
    target = _normalize_paragraph_id(citation)
    text = _extract_by_heading(law, target, citation)
    if text:
        return text
    # Fallback: sub-item refs like "6-2 A III 7" → try parent "6-2"
    parent = re.match(r"^(\d+-\d+[a-z]?)", target)
    if parent and parent.group(1) != target:
        parent_text = _extract_by_heading(law, parent.group(1), citation)
        if parent_text:
            return f"[context: full § {parent.group(1)}, relevant sub-item: {citation}]\n\n{parent_text}"
    return None


def _extract_by_heading(law: LawDocument, target: str, citation: str) -> str | None:
    start = re.compile(rf"^#+[ \t]+§[ \t]+{re.escape(target)}\.", re.MULTILINE)
    m = start.search(law.markdown)
    if not m:
        return None
    nxt = re.compile(r"^#+[ \t]+§[ \t]", re.MULTILINE).search(law.markdown, m.end())
    text = law.markdown[m.start() : nxt.start() if nxt else len(law.markdown)].strip()
    if not text:
        return None

    sub = _extract_subparagraph_num(citation)
    if sub is not None:
        sub_text = _slice_subparagraph(text, sub)
        if sub_text:
            return f"§ {target} ({sub})\n\n{sub_text}"

    return text


_SUBPARA_RE = re.compile(r"\((\d+)\)")


def _extract_subparagraph_num(citation: str) -> int | None:
    m = _SUBPARA_RE.search(citation)
    return int(m.group(1)) if m else None


def _slice_subparagraph(text: str, n: int) -> str | None:
    pat = re.compile(rf"^\(\s*{n}\s*\)", re.MULTILINE)
    m = pat.search(text)
    if not m:
        return None
    next_pat = re.compile(rf"^\(\s*{n + 1}\s*\)", re.MULTILINE)
    m2 = next_pat.search(text, m.end())
    end = m2.start() if m2 else len(text)
    return text[m.start() : end].strip()


def fetch_paragraph_text(
    publisher: str,
    document: str,
    paragraph: str,
    fiscal_year: int,
) -> tuple[str | None, str | None]:
    if publisher != "Stortinget":
        return None, None
    law = fetch_law(document)
    text = extract_paragraph(law, paragraph)
    source = f"norwegian-laws/{law.law_id}" if text else None
    return text, source


def fetch_paragraph_text_with_chapter_fallback(
    publisher: str,
    document: str,
    paragraph: str,
    fiscal_year: int,
) -> tuple[str | None, str | None]:
    """Resolve paragraph text from norwegian-laws.

    The whole law is a single markdown document, so there is no chapter
    pagination to work around; this delegates to ``fetch_paragraph_text``.
    """
    return fetch_paragraph_text(publisher, document, paragraph, fiscal_year)
