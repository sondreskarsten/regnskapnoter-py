"""Tests for regnskapnoter.law_loader (lovdata.no scraper)."""

from __future__ import annotations

import os

import pytest

from regnskapnoter.law_loader import (
    LawDocument,
    _normalize_paragraph_id,
    extract_paragraph,
    fetch_paragraph_text,
    resolve_law_id,
)


def test_resolve_law_id_known():
    assert resolve_law_id("regnskapsloven") == "lov/1998-07-17-56"
    assert resolve_law_id("aksjeloven") == "lov/1997-06-13-44"


def test_resolve_law_id_passthrough():
    assert resolve_law_id("lov/2005-06-17-67") == "lov/2005-06-17-67"


def test_resolve_law_id_unknown():
    with pytest.raises(ValueError, match="Unknown law"):
        resolve_law_id("nonexistent_law")


def test_normalize_paragraph_id():
    assert _normalize_paragraph_id("§ 7-29") == "7-29"
    assert _normalize_paragraph_id("7-29 (3)") == "7-29"
    assert _normalize_paragraph_id("§ 3-5") == "3-5"


MOCK_HTML = """
<div data-id="PARAGRAF_3-5" class="morTag_p paragraf" id="PARAGRAF_3-5">
<h4 class="paragrafHeader"><span class="paragrafhode">
<span class="paragrafValue">§ 3-5.</span>
<span class="paragrafTittel"><em>Signering av årsregnskapet</em></span>
</span></h4>
<table data-id="AVSNITT_1" class="numeral morTag_an avsnitt" style="width:100%">
<tr><td><span class="avsnittNummer numeral">(1)</span> Foo bar baz.</td></tr>
</table>
<table data-id="AVSNITT_2" class="numeral morTag_an avsnitt" style="width:100%">
<tr><td><span class="avsnittNummer numeral">(2)</span> Second paragraph.</td></tr>
</table>
</div>
<a class="documentPart_scrollMargin" name="§3-6"></a>
<div data-id="PARAGRAF_3-6" class="morTag_p paragraf" id="PARAGRAF_3-6">
<h4 class="paragrafHeader"><span class="paragrafhode">
<span class="paragrafValue">§ 3-6.</span>
<span class="paragrafTittel"><em>Konsernregnskap</em></span>
</span></h4>
<table data-id="AVSNITT_1" class="numeral morTag_an avsnitt" style="width:100%">
<tr><td><span class="avsnittNummer numeral">(1)</span> Some text.</td></tr>
</table>
</div>
<a class="share-paragraf" href="#"></a>
"""


def test_extract_paragraph_from_html():
    law = LawDocument(law_id="test", html=MOCK_HTML, sist_endret=None)
    result = extract_paragraph(law, "§ 3-5")
    assert result is not None
    assert "Signering" in result
    assert "Foo bar baz" in result
    assert "§ 3-6" not in result


def test_extract_paragraph_not_found():
    law = LawDocument(law_id="test", html=MOCK_HTML, sist_endret=None)
    assert extract_paragraph(law, "§ 99-99") is None


def test_extract_subparagraph():
    law = LawDocument(law_id="test", html=MOCK_HTML, sist_endret=None)
    result = extract_paragraph(law, "§ 3-5 (2)")
    assert result is not None
    assert "Second paragraph" in result


def test_fetch_paragraph_text_non_stortinget():
    text, src = fetch_paragraph_text("NRS", "NRS 9", "punkt 4", 2024)
    assert text is None
    assert src is None


@pytest.mark.skipif(
    not os.environ.get("RN_LIVE_TESTS"),
    reason="live tests disabled; set RN_LIVE_TESTS=1",
)
def test_fetch_law_live():
    import tempfile

    from regnskapnoter.law_loader import fetch_law

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["REGNSKAPNOTER_LAW_CACHE"] = tmpdir
        try:
            law = fetch_law("regnskapsloven")
            assert law.law_id == "lov/1998-07-17-56"
            assert len(law.html) > 100000
            p = extract_paragraph(law, "§ 7-29")
            assert p is not None
            assert "Andre forpliktelser" in p
        finally:
            del os.environ["REGNSKAPNOTER_LAW_CACHE"]
