"""W3C Web Annotation Data Model (WADM) producer for noter-extraction outputs.

Pairs raw text/PDF spans with concept-keyed observations. The output conforms to the
WADM (https://www.w3.org/TR/annotation-model/) so consumers like Hypothes.is or
INCEpTION can import the annotations directly.

Three target shapes:

1. **Text targets** (always emitted): the source is the raw JSON note. The selector is
   a TextQuoteSelector with prefix/exact/suffix carved from the note's ``full_text``.
   Refinement: ``{note_number, note_title}``.

2. **PDF targets with FragmentSelector** (emitted when ``source_pdf_uri`` is set AND
   the raw JSON contains ``[[p:N]]`` page markers OR ``page_start`` per note): the
   source is the source PDF GCS URI. The selector is a FragmentSelector
   ``page=N`` (PDF Open Parameters, RFC 3778) refined by a TextQuoteSelector.

3. **PDF targets without page info** (legacy fallback): RangeSelector refined by
   TextQuoteSelector when no page metadata is available.

The body always carries the regnskap-no concept_id plus the observed value.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from regnskapnoter.adapters import Document

import pandas as pd

WADM_CONTEXT = "http://www.w3.org/ns/anno.jsonld"
GENERATOR_ID = "https://github.com/sondreskarsten/regnskapnoter-py"

PREFIX_LEN = 32
SUFFIX_LEN = 32
PAGE_MARKER_RE = re.compile(r"\[\[p:(\d+)\]\]")


def _annotation_id(orgnr: str, year: int, concept_id: str, value: Any, note_number: str) -> str:
    raw = f"{orgnr}|{year}|{concept_id}|{value}|{note_number}".encode()
    h = hashlib.sha256(raw).hexdigest()[:16]
    return f"urn:regnskapnoter:annotation:{h}"


def _strip_page_markers(text: str) -> str:
    return PAGE_MARKER_RE.sub("", text)


def _page_for_offset(text_with_markers: str, offset_in_clean: int) -> int | None:
    """Walk marker-bearing text, tracking current page until the clean-text offset."""
    current_page: int | None = None
    clean_offset = 0
    i = 0
    while i < len(text_with_markers):
        m = PAGE_MARKER_RE.match(text_with_markers, i)
        if m:
            current_page = int(m.group(1))
            i = m.end()
            continue
        if clean_offset >= offset_in_clean:
            return current_page
        clean_offset += 1
        i += 1
    return current_page


def _format_value_candidates(value: Any) -> list[str]:
    """Return likely text representations of a numeric/string value as it appears."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        return [str(value)]
    abs_str = str(abs(ivalue))
    variants: list[str] = []
    for sep in (" ", "\u00a0", "."):
        s = abs_str[::-1]
        parts = [s[i : i + 3] for i in range(0, len(s), 3)]
        grouped = sep.join(parts)[::-1]
        if ivalue < 0:
            variants.append("-" + grouped)
            variants.append(f"({grouped})")
        else:
            variants.append(grouped)
    if ivalue < 0:
        variants.extend([str(ivalue), f"({abs(ivalue)})"])
    else:
        variants.append(str(ivalue))
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v not in seen and v.strip():
            seen.add(v)
            out.append(v)
    return out


def _find_quote(text: str, candidates: list[str]) -> tuple[str, str, str, int] | None:
    """Return (prefix, exact, suffix, offset) for first match, or None."""
    for cand in candidates:
        idx = text.find(cand)
        if idx < 0:
            continue
        prefix = text[max(0, idx - PREFIX_LEN) : idx]
        suffix = text[idx + len(cand) : idx + len(cand) + SUFFIX_LEN]
        return (prefix, cand, suffix, idx)
    return None


def _word_bbox(span, exact: str) -> tuple[int, int, int, int] | None:
    """Locate the bounding box of ``exact`` within a span's word list.

    Returns the bounding box of the first run of consecutive words whose
    concatenation (joined by single space) starts with ``exact``. Returns
    None if the span has no word-level info or no match is found.
    """
    if not span.words:
        return None
    needle = exact.strip()
    if not needle:
        return None
    n = len(span.words)
    for i in range(n):
        run = []
        for j in range(i, n):
            run.append(span.words[j][0])
            joined = " ".join(run)
            if joined == needle or joined.startswith(needle):
                xs = [span.words[k][1] for k in range(i, j + 1)]
                ys = [span.words[k][2] for k in range(i, j + 1)]
                rights = [span.words[k][1] + span.words[k][3] for k in range(i, j + 1)]
                bottoms = [span.words[k][2] + span.words[k][4] for k in range(i, j + 1)]
                left = int(min(xs))
                top = int(min(ys))
                right = int(max(rights))
                bottom = int(max(bottoms))
                return (left, top, right - left, bottom - top)
            if len(joined) > len(needle) + 4:
                break
    return None


def build_annotations(
    source: dict | Document,
    observations: pd.DataFrame,
    *,
    source_text_uri: str | None = None,
    source_pdf_uri: str | None = None,
    pipeline_version: str = "noter-extraction-2025",
) -> pd.DataFrame:
    """Build WADM annotations linking raw text spans to concept-keyed observations.

    Parameters
    ----------
    raw_json : Raw Gemini-extracted JSON for one (orgnr, year), with at least
        ``orgnr``, ``year``, ``notes: [{note_number, title, full_text}]``. If
        ``full_text`` contains ``[[p:N]]`` page markers OR each note has
        ``page_start``, PDF annotations get a FragmentSelector with the page.
    observations : Long-form concept-keyed DataFrame with columns ``orgnr,
        report_year, concept_id, value`` (output of ``regnskapnoter.canonicalize``).
    source_text_uri : GCS URI of the raw JSON. Used as ``target.source`` for text
        annotations. If None, omitted.
    source_pdf_uri : GCS URI of the source PDF (optional). If provided, a parallel
        PDF annotation is emitted for each text annotation, with FragmentSelector
        ``page=N`` when a page can be inferred and RangeSelector otherwise.
    pipeline_version : Recorded as the WADM ``creator``.

    Returns
    -------
    DataFrame with one row per annotation:
        annotation_id, target_type ('text'|'pdf'), source, selector_json,
        body_concept_id, body_value, note_number, note_title, page,
        match_status ('matched'|'unmatched'), created, creator
    """
    from regnskapnoter.adapters import Document as _Doc
    from regnskapnoter.adapters import from_gemini_json

    document: _Doc = source if isinstance(source, _Doc) else from_gemini_json(source)
    orgnr = str(document.orgnr or "")
    year = document.year

    obs_for_orgnr = (
        observations[
            (observations["orgnr"].astype(str) == orgnr) & (observations["report_year"] == year)
        ]
        if orgnr
        else observations
    )

    rows: list[dict] = []
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for obs in obs_for_orgnr.itertuples(index=False):
        cid = getattr(obs, "concept_id", None)
        val = getattr(obs, "value", None)
        if not cid or val is None or (isinstance(val, float) and pd.isna(val)):
            continue

        candidates = _format_value_candidates(val)
        match = None
        match_span = None
        match_page: int | None = None
        match_xywh: tuple[int, int, int, int] | None = None
        for span in document.spans:
            text_clean = _strip_page_markers(span.text)
            quote = _find_quote(text_clean, candidates)
            if quote:
                match = quote
                match_span = span
                match_page = span.page
                match_xywh = _word_bbox(span, quote[1])
                break

        nn = match_span.note_number if match_span else ""
        annotation_id = _annotation_id(orgnr, year, cid, val, nn)

        if match and match_span:
            prefix, exact, suffix, _offset = match
            text_selector = {
                "type": "TextQuoteSelector",
                "exact": exact,
                "prefix": prefix,
                "suffix": suffix,
            }
            note_title = match_span.note_title
            rows.append(
                {
                    "annotation_id": annotation_id,
                    "target_type": "text",
                    "source": source_text_uri or "",
                    "selector_json": json.dumps(text_selector, ensure_ascii=False),
                    "body_concept_id": cid,
                    "body_value": str(val),
                    "note_number": match_span.note_number,
                    "note_title": note_title,
                    "page": match_page,
                    "match_status": "matched",
                    "created": created,
                    "creator": pipeline_version,
                }
            )
            if source_pdf_uri:
                if match_page is not None and match_xywh is not None:
                    x, y, w, h = match_xywh
                    pdf_selector: dict = {
                        "type": "FragmentSelector",
                        "conformsTo": "http://tools.ietf.org/rfc/rfc3778",
                        "value": f"page={match_page}",
                        "refinedBy": {
                            "type": "FragmentSelector",
                            "conformsTo": "http://www.w3.org/TR/media-frags/",
                            "value": f"xywh={x},{y},{w},{h}",
                            "refinedBy": text_selector,
                        },
                    }
                elif match_page is not None:
                    pdf_selector = {
                        "type": "FragmentSelector",
                        "conformsTo": "http://tools.ietf.org/rfc/rfc3778",
                        "value": f"page={match_page}",
                        "refinedBy": text_selector,
                    }
                else:
                    pdf_selector = {
                        "type": "RangeSelector",
                        "refinedBy": text_selector,
                    }
                rows.append(
                    {
                        "annotation_id": annotation_id + "#pdf",
                        "target_type": "pdf",
                        "source": source_pdf_uri,
                        "selector_json": json.dumps(pdf_selector, ensure_ascii=False),
                        "body_concept_id": cid,
                        "body_value": str(val),
                        "note_number": match_span.note_number,
                        "note_title": note_title,
                        "page": match_page,
                        "match_status": "matched",
                        "created": created,
                        "creator": pipeline_version,
                    }
                )
        else:
            rows.append(
                {
                    "annotation_id": annotation_id,
                    "target_type": "text",
                    "source": source_text_uri or "",
                    "selector_json": "",
                    "body_concept_id": cid,
                    "body_value": str(val),
                    "note_number": "",
                    "note_title": "",
                    "page": None,
                    "match_status": "unmatched",
                    "created": created,
                    "creator": pipeline_version,
                }
            )

    return pd.DataFrame(rows)


def annotations_to_jsonld(df: pd.DataFrame) -> list[dict]:
    """Render an annotations DataFrame as a list of WADM JSON-LD objects."""
    out = []
    for r in df.itertuples(index=False):
        target: dict[str, Any] = {"source": getattr(r, "source", "")}
        sel_json = getattr(r, "selector_json", "")
        if sel_json:
            target["selector"] = json.loads(sel_json)
        body = {
            "type": "SpecificResource",
            "concept_id": r.body_concept_id,
            "value": r.body_value,
        }
        ann = {
            "@context": WADM_CONTEXT,
            "id": r.annotation_id,
            "type": "Annotation",
            "motivation": "tagging",
            "target": target,
            "body": body,
            "creator": getattr(r, "creator", ""),
            "generator": GENERATOR_ID,
            "created": getattr(r, "created", ""),
        }
        if getattr(r, "note_number", ""):
            target["refinement"] = {
                "note_number": r.note_number,
                "note_title": r.note_title,
            }
        out.append(ann)
    return out


def coverage_report(df: pd.DataFrame) -> dict[str, Any]:
    """Summary stats for an annotations DataFrame."""
    if df.empty:
        return {"total": 0, "matched": 0, "unmatched": 0, "match_rate": 0.0}
    total = len(df)
    matched = int((df["match_status"] == "matched").sum())
    return {
        "total": total,
        "matched": matched,
        "unmatched": total - matched,
        "match_rate": matched / total if total else 0.0,
        "concepts_unique": int(df["body_concept_id"].nunique()),
    }
