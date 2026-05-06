"""Time-aware fetcher for Norwegian law text from lovdata.no.

Scrapes individual paragraphs directly from the Lovdata website, which has
100% coverage of all paragraphs (unlike the norwegian-laws repo's bulk XML
parser which misses nested sub-chapters like regnskapsloven kapittel 7).

Disk-caches fetched law HTML by (law_id, year) under
$XDG_CACHE_HOME/regnskapnoter/laws/.

Naive empiricism note: the caller must specify a fiscal year. The returned
text is the *current* consolidated version from lovdata.no — there is no
PIT reconstruction yet. The ``sist_endret`` field records when the law was
last amended so the caller can detect staleness.
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

LOVDATA_BASE = "https://lovdata.no/dokument/NL"


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
    html: str
    sist_endret: str | None


def _fetch_law_html(law_id: str) -> str:
    url = f"{LOVDATA_BASE}/{law_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "regnskapnoter-py/0.8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise FileNotFoundError(f"{law_id} not found (status={e.code})") from e


def fetch_law(name_or_id: str) -> LawDocument:
    law_id = resolve_law_id(name_or_id)
    safe_name = law_id.replace("/", "-")
    cache_path = cache_dir() / f"{safe_name}.html"
    if cache_path.is_file():
        html = cache_path.read_text(encoding="utf-8")
    else:
        html = _fetch_law_html(law_id)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(html, encoding="utf-8")

    sist_endret = None
    m = re.search(r'id="metaField_endret"[^>]*>.*?href="[^"]*">([^<]+)</a>', html)
    if m:
        sist_endret = m.group(1).strip()

    return LawDocument(law_id=law_id, html=html, sist_endret=sist_endret)


_PARA_ID_RE = re.compile(r"§\s*(\d+(?:-\d+[a-z]?))")


def _normalize_paragraph_id(s: str) -> str:
    s = s.replace("§", "").replace("\u00a7", "").strip()
    s = s.split("(", 1)[0].strip().replace(" ", "").replace("\u2010", "-")
    return s


def extract_paragraph(law: LawDocument, citation: str) -> str | None:
    target = _normalize_paragraph_id(citation)
    text = _extract_by_anchor(law, target, citation)
    if text:
        return text
    # Fallback: sub-item refs like "6-2 A III 7" → try parent "6-2"
    parent = re.match(r"^(\d+-\d+[a-z]?)", target)
    if parent and parent.group(1) != target:
        parent_text = _extract_by_anchor(law, parent.group(1), citation)
        if parent_text:
            return f"[context: full § {parent.group(1)}, relevant sub-item: {citation}]\n\n{parent_text}"
    return None


def _extract_by_anchor(law: LawDocument, target: str, citation: str) -> str | None:
    anchor_id = f"PARAGRAF_{target}"
    pattern = re.compile(
        rf'<div[^>]*data-id="{re.escape(anchor_id)}"[^>]*class="morTag_p paragraf"[^>]*>(.*?)</div>\s*(?=<a\s+class="(?:documentPart_scrollMargin|namedAnchor|share-paragraf)"|<div\s+data-(?:id|level)=|</div>)',
        re.DOTALL,
    )
    m = pattern.search(law.html)
    if not m:
        return None

    raw = m.group(0)
    text = _html_to_text(raw)
    if not text.strip():
        return None

    sub = _extract_subparagraph_num(citation)
    if sub is not None:
        sub_text = _slice_subparagraph(text, sub)
        if sub_text:
            return f"§ {target} ({sub})\n\n{sub_text}"

    return text


def _html_to_text(html: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    text = re.sub(
        r'<[^>]*class="[^"]*fotnote[^"]*"[^>]*>.*?(?:</td>|</tr>|</table>)',
        "",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(r'<a[^>]*class="share-paragraf"[^>]*>.*?</a>', "", text, flags=re.DOTALL)
    text = re.sub(r'<a[^>]*class="namedAnchor"[^>]*></a>', "", text)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</(?:tr|table|p|div|h[1-6])>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    import html as html_mod

    text = html_mod.unescape(text)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


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
    try:
        law = fetch_law(document)
    except (ValueError, FileNotFoundError):
        return None, None
    text = extract_paragraph(law, paragraph)
    source = f"lovdata.no/{law.law_id}" if text else None
    return text, source


def _chapter_for_paragraph(para: str) -> str | None:
    """Infer chapter from paragraph number, e.g. '14-6' → 'KAPITTEL_14'."""
    m = re.match(r"(\d+)-", _normalize_paragraph_id(para))
    return f"KAPITTEL_{m.group(1)}" if m else None


def fetch_paragraph_text_with_chapter_fallback(
    publisher: str,
    document: str,
    paragraph: str,
    fiscal_year: int,
) -> tuple[str | None, str | None]:
    """Like fetch_paragraph_text but tries chapter-specific URL for paginated laws."""
    text, source = fetch_paragraph_text(publisher, document, paragraph, fiscal_year)
    if text:
        return text, source
    if publisher != "Stortinget":
        return None, None
    try:
        law_id = resolve_law_id(document)
    except ValueError:
        return None, None
    chapter = _chapter_for_paragraph(paragraph)
    if not chapter:
        return None, None
    chapter_url_id = f"{law_id}/{chapter}"
    safe_name = chapter_url_id.replace("/", "-")
    cache_path = cache_dir() / f"{safe_name}.html"
    if cache_path.is_file():
        html = cache_path.read_text(encoding="utf-8")
    else:
        url = f"{LOVDATA_BASE}/{chapter_url_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "regnskapnoter-py/0.8"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                html = resp.read().decode("utf-8")
        except urllib.error.HTTPError:
            return None, None
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(html, encoding="utf-8")

    chapter_law = LawDocument(law_id=law_id, html=html, sist_endret=None)
    text = extract_paragraph(chapter_law, paragraph)
    source = f"lovdata.no/{chapter_url_id}" if text else None
    return text, source
