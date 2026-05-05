import json

import pandas as pd

import regnskapnoter as rn


def _make_raw_json():
    return {
        "orgnr": "811722332",
        "year": 2024,
        "notes": [
            {
                "note_number": "1",
                "title": "Skattekostnad",
                "full_text": (
                    "Note 1 Skattekostnad\n"
                    "Resultat før skatt -873 527\n"
                    "Permanente forskjeller 12 702\n"
                    "Endring midlertidige forskjeller -192 794\n"
                ),
            },
        ],
    }


def _make_observations():
    return pd.DataFrame(
        {
            "orgnr": ["811722332", "811722332", "811722332"],
            "report_year": [2024, 2024, 2024],
            "concept_id": [
                "regnskap-no:ResultatForSkattSpesifisering",
                "regnskap-no:PermanenteSkatteforskjeller",
                "regnskap-no:EndringMidlertidigeForskjeller",
            ],
            "value": [-873527, 12702, -192794],
        }
    )


def test_build_annotations_matches_text():
    raw = _make_raw_json()
    obs = _make_observations()
    df = rn.build_annotations(raw, obs, source_text_uri="gs://example/raw.json")
    matched = df[df["match_status"] == "matched"]
    assert len(matched) >= 2
    assert "regnskap-no:PermanenteSkatteforskjeller" in matched["body_concept_id"].tolist()


def test_build_annotations_emits_pdf_target_when_pdf_uri_provided():
    df = rn.build_annotations(
        _make_raw_json(),
        _make_observations(),
        source_text_uri="gs://example/raw.json",
        source_pdf_uri="gs://example/source.pdf",
    )
    text_targets = df[df["target_type"] == "text"]
    pdf_targets = df[df["target_type"] == "pdf"]
    assert len(text_targets) > 0
    assert len(pdf_targets) > 0
    assert len(pdf_targets) <= len(text_targets)


def test_text_quote_selector_has_prefix_exact_suffix():
    df = rn.build_annotations(_make_raw_json(), _make_observations())
    matched = df[df["match_status"] == "matched"].iloc[0]
    sel = json.loads(matched["selector_json"])
    assert sel["type"] == "TextQuoteSelector"
    assert "exact" in sel and "prefix" in sel and "suffix" in sel
    assert sel["exact"]


def test_jsonld_export_is_wadm_compliant():
    df = rn.build_annotations(_make_raw_json(), _make_observations())
    jsonld = rn.annotations_to_jsonld(df)
    assert all(a["@context"] == "http://www.w3.org/ns/anno.jsonld" for a in jsonld)
    assert all(a["type"] == "Annotation" for a in jsonld)
    assert all("body" in a and "target" in a for a in jsonld)


def test_coverage_report():
    df = rn.build_annotations(_make_raw_json(), _make_observations())
    rep = rn.coverage_report(df)
    assert rep["total"] >= 3
    assert 0.0 <= rep["match_rate"] <= 1.0
    assert rep["matched"] + rep["unmatched"] == rep["total"]


def test_unmatched_observation_emits_unmatched_row():
    raw = _make_raw_json()
    obs = pd.DataFrame(
        {
            "orgnr": ["811722332"],
            "report_year": [2024],
            "concept_id": ["regnskap-no:Aksjekapital"],
            "value": [99999999],
        }
    )
    df = rn.build_annotations(raw, obs)
    assert (df["match_status"] == "unmatched").any()
