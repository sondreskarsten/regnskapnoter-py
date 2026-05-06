"""Adapter tests: every OCR / extraction input shape produces a Document
that build_annotations() can consume.
"""

from __future__ import annotations

import pandas as pd

import regnskapnoter as rn

# ---------------------------------------------------------------------------
# from_gemini_json
# ---------------------------------------------------------------------------


def test_from_gemini_json_no_page_markers():
    raw = {
        "orgnr": "811722332",
        "year": 2024,
        "notes": [
            {
                "note_number": "1",
                "title": "Skattekostnad",
                "page_start": 5,
                "page_end": 5,
                "full_text": "Skattekostnad 1 100\nResultat...",
            },
        ],
    }
    doc = rn.from_gemini_json(raw)
    assert doc.orgnr == "811722332"
    assert doc.year == 2024
    assert len(doc.spans) == 1
    assert doc.spans[0].page == 5
    assert doc.spans[0].note_number == "1"
    assert doc.spans[0].producer == "gemini"


def test_from_gemini_json_splits_on_page_markers():
    raw = {
        "orgnr": "1",
        "year": 2024,
        "notes": [
            {
                "note_number": "1",
                "title": "T",
                "page_start": 5,
                "full_text": "[[p:5]]Skatt 1 100\n[[p:6]]Resultat 9 999",
            }
        ],
    }
    doc = rn.from_gemini_json(raw)
    pages = [s.page for s in doc.spans]
    assert pages == [5, 6]
    assert "Skatt 1 100" in doc.spans[0].text
    assert "Resultat 9 999" in doc.spans[1].text


# ---------------------------------------------------------------------------
# from_text_pages — ocrmypdf, tesseract text mode, Cloud Vision text-only
# ---------------------------------------------------------------------------


def test_from_text_pages_basic():
    pages = [
        "Side 1 første side",
        "Skattekostnad 1 100\nResultat før skatt -873 527",
        "",  # blank page filtered
        "Side 4 noter Aksjekapital 1 000 000",
    ]
    doc = rn.from_text_pages(pages, orgnr="811722332", year=2024, producer="ocrmypdf")
    assert doc.producer == "ocrmypdf"
    assert doc.total_pages == 4
    assert len(doc.spans) == 3  # blank skipped
    assert [s.page for s in doc.spans] == [1, 2, 4]
    assert doc.spans[1].text.startswith("Skattekostnad")


# ---------------------------------------------------------------------------
# from_text_blob — single string (with or without [[p:N]] markers)
# ---------------------------------------------------------------------------


def test_from_text_blob_with_page_markers():
    text = "[[p:1]]Forside\n[[p:5]]Skattekostnad 1 100\n[[p:6]]Aksjekapital 1 000 000"
    doc = rn.from_text_blob(text, orgnr="1", year=2024, producer="ocrmypdf")
    pages = [s.page for s in doc.spans]
    assert pages == [1, 5, 6]


def test_from_text_blob_no_markers():
    doc = rn.from_text_blob("Just a plain blob", orgnr="1", year=2024)
    assert len(doc.spans) == 1
    assert doc.spans[0].page is None


# ---------------------------------------------------------------------------
# from_tesseract_tsv — word-level with bboxes
# ---------------------------------------------------------------------------


def test_from_tesseract_tsv_string_input():
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext\n"
        "5\t5\t1\t1\t1\t1\t100\t200\t90\t20\t95\tSkattekostnad\n"
        "5\t5\t1\t1\t1\t2\t200\t200\t30\t20\t98\t1\n"
        "5\t5\t1\t1\t1\t3\t240\t200\t40\t20\t97\t100\n"
        "5\t5\t1\t1\t2\t1\t100\t230\t60\t20\t96\tResultat\n"
    )
    doc = rn.from_tesseract_tsv(tsv, orgnr="811722332", year=2024)
    assert doc.producer == "tesseract_tsv"
    assert len(doc.spans) == 2  # two lines = two spans
    first = doc.spans[0]
    assert first.text == "Skattekostnad 1 100"
    assert first.page == 5
    assert len(first.words) == 3
    assert first.words[0][0] == "Skattekostnad"
    assert first.words[0][1] == 100  # left
    assert first.words[0][5] == 95  # conf


def test_from_tesseract_tsv_dict_iterable():
    rows = [
        {
            "level": 5,
            "page_num": 1,
            "block_num": 1,
            "par_num": 1,
            "line_num": 1,
            "word_num": 1,
            "left": 10,
            "top": 20,
            "width": 50,
            "height": 15,
            "conf": 88,
            "text": "Aksjekapital",
        },
        {
            "level": 5,
            "page_num": 1,
            "block_num": 1,
            "par_num": 1,
            "line_num": 1,
            "word_num": 2,
            "left": 70,
            "top": 20,
            "width": 30,
            "height": 15,
            "conf": 90,
            "text": "1",
        },
    ]
    doc = rn.from_tesseract_tsv(rows, orgnr="1", year=2024)
    assert len(doc.spans) == 1
    assert doc.spans[0].text == "Aksjekapital 1"


def test_from_tesseract_tsv_min_conf_filter():
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t10\t10\t30\t10\t20\tlowconf\n"
        "5\t1\t1\t1\t1\t2\t40\t10\t30\t10\t95\thighconf\n"
    )
    doc = rn.from_tesseract_tsv(tsv, orgnr="1", year=2024, min_conf=80.0)
    assert doc.spans[0].text == "highconf"


# ---------------------------------------------------------------------------
# from_cloud_vision
# ---------------------------------------------------------------------------


def test_from_cloud_vision_with_word_bboxes():
    pages = [
        {
            "page": 1,
            "text": "Skattekostnad 1 100",
            "words": [
                {"text": "Skattekostnad", "x": 100, "y": 200, "w": 90, "h": 20, "conf": 0.95},
                {"text": "1", "x": 200, "y": 200, "w": 30, "h": 20, "conf": 0.98},
                {"text": "100", "x": 240, "y": 200, "w": 40, "h": 20, "conf": 0.97},
            ],
        },
    ]
    doc = rn.from_cloud_vision(pages, orgnr="1", year=2024)
    assert doc.producer == "cloud_vision"
    assert doc.spans[0].page == 1
    assert len(doc.spans[0].words) == 3


def test_from_cloud_vision_text_only():
    pages = [{"page": 1, "text": "page text", "words": []}]
    doc = rn.from_cloud_vision(pages, orgnr="1", year=2024)
    assert doc.spans[0].words == []


# ---------------------------------------------------------------------------
# from_spans escape hatch
# ---------------------------------------------------------------------------


def test_from_spans():
    spans = [rn.TextSpan(text="X", page=1), rn.TextSpan(text="Y", page=3)]
    doc = rn.from_spans(spans, orgnr="1", year=2024, producer="custom")
    assert doc.total_pages == 3
    assert doc.producer == "custom"
    assert len(doc.spans) == 2


# ---------------------------------------------------------------------------
# Integration: build_annotations works on every adapter output
# ---------------------------------------------------------------------------


def _obs_skattekostnad():
    return pd.DataFrame(
        {
            "orgnr": ["811722332"],
            "report_year": [2024],
            "concept_id": ["regnskap-no:Skattekostnad"],
            "value": [1100],
        }
    )


def test_build_annotations_from_gemini_json():
    raw = {
        "orgnr": "811722332",
        "year": 2024,
        "notes": [
            {
                "note_number": "1",
                "title": "Skatt",
                "page_start": 5,
                "full_text": "[[p:5]]Skattekostnad 1 100\nResultat...",
            }
        ],
    }
    df = rn.build_annotations(raw, _obs_skattekostnad())
    assert (df["match_status"] == "matched").any()
    text_rows = df[df["target_type"] == "text"]
    assert any("1 100" in s for s in text_rows["selector_json"])


def test_build_annotations_from_text_pages():
    pages = ["", "", "", "", "Skattekostnad 1 100\nResultat før skatt -873 527"]
    doc = rn.from_text_pages(pages, orgnr="811722332", year=2024, producer="ocrmypdf")
    df = rn.build_annotations(doc, _obs_skattekostnad())
    matched = df[df["match_status"] == "matched"]
    assert len(matched) >= 1
    assert (matched["page"] == 5).any()


def test_build_annotations_from_tesseract_tsv():
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext\n"
        "5\t5\t1\t1\t1\t1\t100\t200\t90\t20\t95\tSkattekostnad\n"
        "5\t5\t1\t1\t1\t2\t200\t200\t30\t20\t98\t1\n"
        "5\t5\t1\t1\t1\t3\t240\t200\t40\t20\t97\t100\n"
    )
    doc = rn.from_tesseract_tsv(tsv, orgnr="811722332", year=2024)
    df = rn.build_annotations(doc, _obs_skattekostnad(), source_pdf_uri="gs://x/y.pdf")
    matched = df[df["match_status"] == "matched"]
    assert len(matched) >= 1
    pdf_rows = df[df["target_type"] == "pdf"]
    assert any("xywh=" in s for s in pdf_rows["selector_json"])


def test_build_annotations_from_cloud_vision():
    pages = [
        {
            "page": 5,
            "text": "Skattekostnad 1 100",
            "words": [
                {"text": "Skattekostnad", "x": 100, "y": 200, "w": 90, "h": 20, "conf": 0.95},
                {"text": "1", "x": 200, "y": 200, "w": 30, "h": 20, "conf": 0.98},
                {"text": "100", "x": 240, "y": 200, "w": 40, "h": 20, "conf": 0.97},
            ],
        }
    ]
    doc = rn.from_cloud_vision(pages, orgnr="811722332", year=2024)
    df = rn.build_annotations(doc, _obs_skattekostnad(), source_pdf_uri="gs://x/y.pdf")
    matched = df[df["match_status"] == "matched"]
    assert len(matched) >= 1
    assert (matched["page"] == 5).any()


def test_document_joined_text_inserts_page_markers():
    spans = [
        rn.TextSpan(text="page 1 text", page=1, producer="ocrmypdf"),
        rn.TextSpan(text="page 5 text", page=5, producer="ocrmypdf"),
    ]
    doc = rn.from_spans(spans, orgnr="1", year=2024)
    joined = doc.joined_text()
    assert "[[p:1]]" in joined
    assert "[[p:5]]" in joined
    assert joined.index("[[p:1]]") < joined.index("page 1")
    assert joined.index("[[p:5]]") < joined.index("page 5")
