"""Adapters: normalize every regnskap PDF input into a common Document shape.

The annotation pipeline anchors `(concept_id, value)` observations to text spans
in a source document. The same orgnr-year-concept-value tuple can come from many
upstream producers, each with its own data shape:

  - Gemini-on-PDF JSON         : {notes: [{note_number, title, full_text}]}
  - ocrmypdf / tesseract text   : single string of OCR'd text per page
  - tesseract TSV (word-level) : rows of (page, left, top, width, height, conf, text)
  - Cloud Vision                : per-page text + per-word bounding boxes
  - Docling DoclingDocument     : structured page elements with bbox per token

This module defines a common ``Document`` that all of them normalize into. The
annotation builder then anchors values regardless of which engine produced the
input. Bounding-box-bearing inputs additionally produce
``FragmentSelector(value="page=N")`` plus ``xywh=`` Media Fragments.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

PAGE_MARKER_RE = re.compile(r"\[\[p:(\d+)\]\]")


@dataclass
class TextSpan:
    """One contiguous text region from a source document.

    A span has a page (1-indexed PDF page) and the verbatim text. Optional
    fields carry word-level provenance for richer FragmentSelectors:

    - ``words``: per-word `(text, x, y, w, h, conf)` if the engine provided
      bounding boxes. Used for Media Fragments xywh=.
    - ``note_number``, ``note_title``: when the producer knows note structure
      (Gemini, Docling), retained as refinement metadata.
    - ``producer``: name of the source engine (``gemini``, ``tesseract_tsv``,
      ``ocrmypdf``, ``cloud_vision``, ``docling``, etc.). One DGP per producer
      per the multi-DGP pattern.
    """

    text: str
    page: int | None = None
    note_number: str = ""
    note_title: str = ""
    producer: str = ""
    words: list[tuple[str, float, float, float, float, float]] = field(default_factory=list)


@dataclass
class Document:
    """Normalized view of a regnskap document. All adapters produce this."""

    orgnr: str
    year: int
    spans: list[TextSpan] = field(default_factory=list)
    total_pages: int | None = None
    producer: str = ""

    def joined_text(self) -> str:
        """Concatenate every span with [[p:N]] markers preserved between spans."""
        out: list[str] = []
        last_page = None
        for s in self.spans:
            if s.page is not None and s.page != last_page:
                out.append(f"[[p:{s.page}]]")
                last_page = s.page
            out.append(s.text)
        return "\n".join(out)


# ---------------------------------------------------------------------------
# Adapter 1: Gemini-on-PDF JSON (current production format)
# ---------------------------------------------------------------------------


def from_gemini_json(raw_json: dict) -> Document:
    """Normalize the Gemini noter-extraction JSON into a Document.

    Expects: {orgnr, year, total_pages?, notes: [{note_number, title, full_text,
              page_start?, page_end?}]}.
    Page-tracked [[p:N]] markers in full_text are honored if present.
    """
    notes = raw_json.get("notes") or []
    spans: list[TextSpan] = []
    for n in notes:
        if not isinstance(n, dict):
            continue
        text = n.get("full_text") or ""
        if not text:
            continue
        # If full_text has [[p:N]] markers, split into per-page spans
        page_start = n.get("page_start")
        if "[[p:" in text:
            cursor = 0
            current_page: int | None = page_start
            for m in PAGE_MARKER_RE.finditer(text):
                if m.start() > cursor:
                    chunk = text[cursor : m.start()].strip()
                    if chunk:
                        spans.append(
                            TextSpan(
                                text=chunk,
                                page=current_page,
                                note_number=str(n.get("note_number", "")),
                                note_title=n.get("title") or n.get("note_title", ""),
                                producer="gemini",
                            )
                        )
                current_page = int(m.group(1))
                cursor = m.end()
            tail = text[cursor:].strip()
            if tail:
                spans.append(
                    TextSpan(
                        text=tail,
                        page=current_page,
                        note_number=str(n.get("note_number", "")),
                        note_title=n.get("title") or n.get("note_title", ""),
                        producer="gemini",
                    )
                )
        else:
            spans.append(
                TextSpan(
                    text=text,
                    page=page_start,
                    note_number=str(n.get("note_number", "")),
                    note_title=n.get("title") or n.get("note_title", ""),
                    producer="gemini",
                )
            )
    return Document(
        orgnr=str(raw_json.get("orgnr") or ""),
        year=int(raw_json.get("year") or 0),
        spans=spans,
        total_pages=raw_json.get("total_pages"),
        producer="gemini",
    )


# ---------------------------------------------------------------------------
# Adapter 2: ocrmypdf / plain-text-per-page (string list)
# ---------------------------------------------------------------------------


def from_text_pages(
    pages: list[str],
    *,
    orgnr: str,
    year: int,
    producer: str = "ocrmypdf",
) -> Document:
    """Normalize a per-page text list (one string per page, 1-indexed) to a Document.

    Used for ``ocrmypdf``, ``tesseract`` (text mode), Cloud Vision (text-only),
    and any other engine that emits flat text per page.
    """
    spans = [
        TextSpan(text=text, page=i + 1, producer=producer)
        for i, text in enumerate(pages)
        if text and text.strip()
    ]
    return Document(
        orgnr=str(orgnr),
        year=int(year),
        spans=spans,
        total_pages=len(pages),
        producer=producer,
    )


def from_text_blob(
    text: str,
    *,
    orgnr: str,
    year: int,
    producer: str = "ocrmypdf",
) -> Document:
    """Normalize a single text blob (no page boundaries) into a Document.

    Page-tracked ``[[p:N]]`` markers are honored if present; otherwise the
    entire blob is one span with no page info.
    """
    if "[[p:" in text:
        spans: list[TextSpan] = []
        cursor = 0
        current_page: int | None = None
        for m in PAGE_MARKER_RE.finditer(text):
            if m.start() > cursor:
                chunk = text[cursor : m.start()].strip()
                if chunk:
                    spans.append(TextSpan(text=chunk, page=current_page, producer=producer))
            current_page = int(m.group(1))
            cursor = m.end()
        tail = text[cursor:].strip()
        if tail:
            spans.append(TextSpan(text=tail, page=current_page, producer=producer))
        return Document(orgnr=str(orgnr), year=int(year), spans=spans, producer=producer)
    return Document(
        orgnr=str(orgnr),
        year=int(year),
        spans=[TextSpan(text=text, producer=producer)],
        producer=producer,
    )


# ---------------------------------------------------------------------------
# Adapter 3: tesseract TSV (word-level with bounding boxes)
# ---------------------------------------------------------------------------

TSV_HEADER = (
    "level",
    "page_num",
    "block_num",
    "par_num",
    "line_num",
    "word_num",
    "left",
    "top",
    "width",
    "height",
    "conf",
    "text",
)


def from_tesseract_tsv(
    tsv: str | bytes | Iterable[dict],
    *,
    orgnr: str,
    year: int,
    min_conf: float = 0.0,
    producer: str = "tesseract_tsv",
) -> Document:
    """Normalize a tesseract --tsv output into a Document with word-level bboxes.

    Accepts either:
    - A TSV string/bytes whose first line is the standard tesseract header.
    - An iterable of dicts already parsed (e.g. ``pytesseract.image_to_data``
      with ``output_type=Output.DICT``).

    Words are grouped per (page, block, par, line) into spans. Each span retains
    per-word ``(text, left, top, width, height, conf)`` so downstream emitters
    can produce Media Fragments xywh= bounding-box selectors.
    """
    rows: list[dict[str, Any]] = []
    if isinstance(tsv, (str, bytes)):
        text = tsv.decode("utf-8") if isinstance(tsv, bytes) else tsv
        lines = text.strip().splitlines()
        if not lines:
            return Document(orgnr=str(orgnr), year=int(year), producer=producer)
        header = lines[0].split("\t")
        for line in lines[1:]:
            cells = line.split("\t")
            if len(cells) != len(header):
                continue
            rows.append(dict(zip(header, cells, strict=False)))
    else:
        rows = list(tsv)

    grouped: dict[tuple[int, int, int, int], list[dict]] = {}
    for r in rows:
        try:
            page = int(r.get("page_num", 0))
            level = int(r.get("level", 0))
            conf = float(r.get("conf", -1))
        except (TypeError, ValueError):
            continue
        if level != 5:  # word-level rows
            continue
        if conf < min_conf:
            continue
        word_text = (r.get("text") or "").strip()
        if not word_text:
            continue
        try:
            block = int(r.get("block_num", 0))
            par = int(r.get("par_num", 0))
            line = int(r.get("line_num", 0))
        except (TypeError, ValueError):
            continue
        grouped.setdefault((page, block, par, line), []).append(r)

    spans: list[TextSpan] = []
    for (page, _b, _p, _l), words in sorted(grouped.items()):
        words_sorted = sorted(words, key=lambda w: int(w.get("word_num", 0)))
        text = " ".join((w.get("text") or "").strip() for w in words_sorted)
        if not text:
            continue
        word_tuples = []
        for w in words_sorted:
            with contextlib.suppress(TypeError, ValueError):
                word_tuples.append(
                    (
                        (w.get("text") or "").strip(),
                        float(w.get("left", 0)),
                        float(w.get("top", 0)),
                        float(w.get("width", 0)),
                        float(w.get("height", 0)),
                        float(w.get("conf", -1)),
                    )
                )
        spans.append(
            TextSpan(
                text=text,
                page=page,
                producer=producer,
                words=word_tuples,
            )
        )

    pages_seen = {s.page for s in spans if s.page is not None}
    return Document(
        orgnr=str(orgnr),
        year=int(year),
        spans=spans,
        total_pages=max(pages_seen) if pages_seen else None,
        producer=producer,
    )


# ---------------------------------------------------------------------------
# Adapter 4: Cloud Vision (per-page text plus optional word bboxes)
# ---------------------------------------------------------------------------


def from_cloud_vision(
    pages: list[dict],
    *,
    orgnr: str,
    year: int,
    producer: str = "cloud_vision",
) -> Document:
    """Normalize Cloud Vision OCR output into a Document.

    Each item in ``pages`` is one page result with shape:
        {"page": int, "text": str, "words": [{"text": str, "x": int,
          "y": int, "w": int, "h": int, "conf": float}, ...]}
    The ``words`` list is optional; if absent, we emit a single span with the
    page text and no word-level bboxes.
    """
    spans: list[TextSpan] = []
    for p in pages:
        page_n = int(p.get("page", 0))
        text = (p.get("text") or "").strip()
        words = p.get("words") or []
        word_tuples = []
        for w in words:
            with contextlib.suppress(TypeError, ValueError):
                word_tuples.append(
                    (
                        (w.get("text") or "").strip(),
                        float(w.get("x", 0)),
                        float(w.get("y", 0)),
                        float(w.get("w", 0)),
                        float(w.get("h", 0)),
                        float(w.get("conf", -1)),
                    )
                )
        if text:
            spans.append(
                TextSpan(
                    text=text,
                    page=page_n if page_n else None,
                    producer=producer,
                    words=word_tuples,
                )
            )
    return Document(
        orgnr=str(orgnr),
        year=int(year),
        spans=spans,
        total_pages=len(pages),
        producer=producer,
    )


# ---------------------------------------------------------------------------
# Adapter 5: Docling DoclingDocument (structured layout)
# ---------------------------------------------------------------------------


def from_docling(
    doc: Any,
    *,
    orgnr: str,
    year: int,
    producer: str = "docling",
) -> Document:
    """Normalize a DoclingDocument (https://github.com/docling-project/docling)
    into our Document. Each Docling text element becomes a TextSpan with its
    page index and bounding box if available.

    This adapter is duck-typed: it expects either ``doc.export_to_text()`` /
    ``doc.export_to_dict()`` on the docling object. Optional dependency.
    """
    spans: list[TextSpan] = []
    pages_seen: set[int] = set()
    if hasattr(doc, "iterate_items"):
        for item, _level in doc.iterate_items():
            text = getattr(item, "text", None) or getattr(item, "orig", None) or ""
            text = (text or "").strip()
            if not text:
                continue
            page = None
            prov = getattr(item, "prov", None)
            if prov:
                first = prov[0] if isinstance(prov, list) and prov else prov
                page = getattr(first, "page", None) or getattr(first, "page_no", None)
                bbox = getattr(first, "bbox", None)
                bbox_tuple = None
                if bbox is not None:
                    bbox_tuple = (
                        getattr(bbox, "l", 0),
                        getattr(bbox, "t", 0),
                        getattr(bbox, "r", 0) - getattr(bbox, "l", 0),
                        getattr(bbox, "b", 0) - getattr(bbox, "t", 0),
                    )
            else:
                bbox_tuple = None
            words: list[tuple[str, float, float, float, float, float]] = []
            if bbox_tuple:
                words.append(
                    (text, bbox_tuple[0], bbox_tuple[1], bbox_tuple[2], bbox_tuple[3], 1.0)
                )
            spans.append(
                TextSpan(
                    text=text,
                    page=int(page) if page else None,
                    producer=producer,
                    words=words,
                )
            )
            if page:
                pages_seen.add(int(page))
    elif hasattr(doc, "export_to_dict"):
        d = doc.export_to_dict()
        for item in d.get("texts", []):
            text = (item.get("text") or "").strip()
            if not text:
                continue
            prov = item.get("prov") or []
            first = prov[0] if prov else {}
            page = first.get("page") or first.get("page_no")
            spans.append(
                TextSpan(
                    text=text,
                    page=int(page) if page else None,
                    producer=producer,
                )
            )
            if page:
                pages_seen.add(int(page))
    return Document(
        orgnr=str(orgnr),
        year=int(year),
        spans=spans,
        total_pages=max(pages_seen) if pages_seen else None,
        producer=producer,
    )


# ---------------------------------------------------------------------------
# Adapter 6: a list of pre-built TextSpans (escape hatch for custom shapes)
# ---------------------------------------------------------------------------


def from_spans(
    spans: list[TextSpan], *, orgnr: str, year: int, producer: str = "custom"
) -> Document:
    pages_seen = {s.page for s in spans if s.page is not None}
    return Document(
        orgnr=str(orgnr),
        year=int(year),
        spans=list(spans),
        total_pages=max(pages_seen) if pages_seen else None,
        producer=producer,
    )
