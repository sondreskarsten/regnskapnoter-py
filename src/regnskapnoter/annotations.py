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
from typing import Any

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


def build_annotations(
    raw_json: dict,
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
    notes = raw_json.get("notes") or []
    orgnr = str(raw_json.get("orgnr") or "")
    year = raw_json.get("year")

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
        match_note: dict | None = None
        match_page: int | None = None
        for n in notes:
            if not isinstance(n, dict):
                continue
            full_text_raw = n.get("full_text") or ""
            full_text_clean = _strip_page_markers(full_text_raw)
            quote = _find_quote(full_text_clean, candidates)
            if quote:
                match = quote
                match_note = n
                if "[[p:" in full_text_raw:
                    match_page = _page_for_offset(full_text_raw, quote[3])
                if match_page is None and n.get("page_start"):
                    match_page = n.get("page_start")
                break

        nn = (match_note or {}).get("note_number", "") if match_note else ""
        annotation_id = _annotation_id(orgnr, year, cid, val, nn)

        if match and match_note:
            prefix, exact, suffix, _offset = match
            text_selector = {
                "type": "TextQuoteSelector",
                "exact": exact,
                "prefix": prefix,
                "suffix": suffix,
            }
            note_title = match_note.get("note_title") or match_note.get("title", "")
            rows.append(
                {
                    "annotation_id": annotation_id,
                    "target_type": "text",
                    "source": source_text_uri or "",
                    "selector_json": json.dumps(text_selector, ensure_ascii=False),
                    "body_concept_id": cid,
                    "body_value": str(val),
                    "note_number": match_note.get("note_number", ""),
                    "note_title": note_title,
                    "page": match_page,
                    "match_status": "matched",
                    "created": created,
                    "creator": pipeline_version,
                }
            )
            if source_pdf_uri:
                if match_page is not None:
                    pdf_selector: dict = {
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
                        "note_number": match_note.get("note_number", ""),
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
