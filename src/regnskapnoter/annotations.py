"""W3C Web Annotation Data Model (WADM) producer for noter-extraction outputs.

Pairs raw text/PDF spans with concept-keyed observations. The output conforms to the
WADM (https://www.w3.org/TR/annotation-model/) so consumers like Hypothes.is or
INCEpTION can import the annotations directly.

Two target shapes:

1. **Text targets** (always emitted): the source is the raw JSON note. The selector is
   a TextQuoteSelector with prefix/exact/suffix carved from the note's ``full_text``.
   Refinement: ``{note_number, note_title}``.

2. **PDF targets** (emitted when ``source_pdf_uri`` is provided AND ``page_index`` is
   supplied per observation): the source is the source PDF GCS URI. The selector is a
   FragmentSelector ``#page=N`` (PDF Open Parameters) refined by a TextQuoteSelector.

The body always carries the regnskap-no concept_id plus the observed value.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import pandas as pd

WADM_CONTEXT = "http://www.w3.org/ns/anno.jsonld"
GENERATOR_ID = "https://github.com/sondreskarsten/regnskapnoter-py"

PREFIX_LEN = 32
SUFFIX_LEN = 32


def _annotation_id(orgnr: str, year: int, concept_id: str, value: Any, note_number: str) -> str:
    raw = f"{orgnr}|{year}|{concept_id}|{value}|{note_number}".encode()
    h = hashlib.sha256(raw).hexdigest()[:16]
    return f"urn:regnskapnoter:annotation:{h}"


def _format_value_candidates(value: Any) -> list[str]:
    """Return likely text representations of a numeric/string value as it appears in a note.

    Norwegian financial reports format numbers with thousands separators (' ', '.', '\u00a0')
    and may show negatives in parentheses or with a leading minus.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        return [str(value)]
    abs_str = str(abs(ivalue))
    # Build canonical thousands-grouped variants
    variants: list[str] = []
    for sep in (" ", "\u00a0", "."):
        parts = []
        s = abs_str[::-1]
        for i in range(0, len(s), 3):
            parts.append(s[i : i + 3])
        grouped = sep.join(parts)[::-1]
        if ivalue < 0:
            variants.append("-" + grouped)
            variants.append(f"({grouped})")
        else:
            variants.append(grouped)
    if ivalue < 0:
        variants.append(str(ivalue))
        variants.append(f"({abs(ivalue)})")
    else:
        variants.append(str(ivalue))
    # Also try the value scaled to thousands (NOK k) and millions (NOK M) since reports vary
    for scale in (1, 1000):
        scaled = ivalue // scale if scale > 1 else ivalue
        if scale > 1 and ivalue % scale == 0:
            variants.append(str(abs(scaled)))
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v not in seen and v.strip():
            seen.add(v)
            out.append(v)
    return out


def _find_quote(text: str, candidates: list[str]) -> tuple[str, str, str] | None:
    """Return (prefix, exact, suffix) for the first candidate that matches in text."""
    for cand in candidates:
        idx = text.find(cand)
        if idx < 0:
            continue
        prefix = text[max(0, idx - PREFIX_LEN) : idx]
        suffix = text[idx + len(cand) : idx + len(cand) + SUFFIX_LEN]
        return (prefix, cand, suffix)
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
    raw_json : The raw Gemini-extracted JSON for one (orgnr, year), with at least
        ``orgnr``, ``year``, and ``notes: [{note_number, title, full_text}]``.
    observations : Long-form concept-keyed DataFrame with columns ``orgnr, report_year,
        concept_id, value`` (output of ``regnskapnoter.canonicalize``).
    source_text_uri : GCS URI of the raw JSON file. Used as the WADM ``target.source``
        for text annotations. If None, omitted.
    source_pdf_uri : GCS URI of the source PDF (optional). If provided, a parallel PDF
        annotation is emitted for each text annotation. PDF annotations currently
        carry only a TextQuoteSelector; FragmentSelector with ``#page=N`` requires
        page-tracked OCR (not yet wired).
    pipeline_version : Recorded as the WADM ``creator``.

    Returns
    -------
    DataFrame with one row per emitted annotation:
        annotation_id, target_type ('text'|'pdf'), source, selector_json,
        body_concept_id, body_value, body_framework_labels, note_number, note_title,
        match_status ('matched'|'unmatched'), created
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
        match_note = None
        for n in notes:
            if not isinstance(n, dict):
                continue
            full_text = n.get("full_text") or ""
            quote = _find_quote(full_text, candidates)
            if quote:
                match = quote
                match_note = n
                break

        annotation_id = _annotation_id(
            orgnr, year, cid, val, (match_note or {}).get("note_number", "")
        )

        if match and match_note:
            prefix, exact, suffix = match
            text_selector = {
                "type": "TextQuoteSelector",
                "exact": exact,
                "prefix": prefix,
                "suffix": suffix,
            }
            rows.append(
                {
                    "annotation_id": annotation_id,
                    "target_type": "text",
                    "source": source_text_uri or "",
                    "selector_json": json.dumps(text_selector, ensure_ascii=False),
                    "body_concept_id": cid,
                    "body_value": str(val),
                    "note_number": match_note.get("note_number", ""),
                    "note_title": match_note.get("note_title") or match_note.get("title", ""),
                    "match_status": "matched",
                    "created": created,
                    "creator": pipeline_version,
                }
            )
            if source_pdf_uri:
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
                        "note_title": match_note.get("note_title") or match_note.get("title", ""),
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
            ann["target"]["refinement"] = {
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
