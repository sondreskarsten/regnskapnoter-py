"""Hypothes.is integration tests with HTTP mocked via responses."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import regnskapnoter as rn


@pytest.fixture
def matched_annotations() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "annotation_id": "urn:regnskapnoter:annotation:abc",
                "target_type": "text",
                "source": "https://viewer.example.com/123/2024",
                "selector_json": json.dumps(
                    {
                        "type": "TextQuoteSelector",
                        "exact": "1 100",
                        "prefix": "Skattekostnad ",
                        "suffix": "\nResultat",
                    }
                ),
                "body_concept_id": "regnskap-no:Skattekostnad",
                "body_value": "1100",
                "note_number": "1",
                "note_title": "Skatt",
                "page": 5,
                "match_status": "matched",
                "created": "2026-05-05T00:00:00+00:00",
                "creator": "test",
            },
            {
                "annotation_id": "urn:regnskapnoter:annotation:def",
                "target_type": "text",
                "source": "https://viewer.example.com/123/2024",
                "selector_json": "",
                "body_concept_id": "regnskap-no:Aksjekapital",
                "body_value": "100000",
                "note_number": "",
                "note_title": "",
                "page": None,
                "match_status": "unmatched",
                "created": "2026-05-05T00:00:00+00:00",
                "creator": "test",
            },
        ]
    )


def test_to_hypothesis_posts_payloads(matched_annotations):
    captured = []

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.append({"url": url, "headers": headers, "json": json})
        m = MagicMock()
        m.status_code = 201
        m.json.return_value = {"id": f"ann-{len(captured)}"}
        return m

    with patch("regnskapnoter.hypothesis.requests.post", side_effect=fake_post):
        result = rn.to_hypothesis(
            matched_annotations,
            group_id="testgroup",
            api_token="testtoken",
        )

    assert len(captured) == 2
    assert all("regnskapnoter-token" not in str(c) for c in captured)
    assert captured[0]["headers"]["Authorization"] == "Bearer testtoken"
    p1 = captured[0]["json"]
    assert p1["uri"] == "https://viewer.example.com/123/2024"
    assert p1["group"] == "testgroup"
    assert any(t.startswith("concept:regnskap-no:Skattekostnad") for t in p1["tags"])
    assert any(t == "page:5" for t in p1["tags"])
    p2 = captured[1]["json"]
    assert any(t == rn.REVIEW_TAG for t in p2["tags"])
    assert (result["hypothesis_status"] == "created").all()
    assert (result["hypothesis_id"] != "").all()


def test_to_hypothesis_uses_source_url_template(matched_annotations):
    captured = []

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.append(json)
        m = MagicMock()
        m.status_code = 201
        m.json.return_value = {"id": "x"}
        return m

    template = lambda row: f"https://my-viewer.com/{row['body_concept_id']}"
    with patch("regnskapnoter.hypothesis.requests.post", side_effect=fake_post):
        rn.to_hypothesis(
            matched_annotations,
            group_id="g",
            api_token="t",
            source_url_template=template,
        )
    assert captured[0]["uri"].startswith("https://my-viewer.com/regnskap-no:Skattekostnad")


def test_from_hypothesis_pulls_and_classifies():
    fake_resp = {
        "rows": [
            {
                "id": "h1",
                "uri": "https://viewer/123",
                "user": "acct:analyst@hypothes.is",
                "tags": ["concept:regnskap-no:NewConcept", "value:42", rn.PROPOSED_CONCEPT_TAG],
                "text": "Found a new disclosure type",
                "target": [{"source": "https://viewer/123"}],
                "created": "2026-05-05T00:00:00+00:00",
                "updated": "2026-05-05T00:00:00+00:00",
            },
            {
                "id": "h2",
                "uri": "https://viewer/123",
                "user": "acct:analyst@hypothes.is",
                "tags": ["concept:regnskap-no:Skattekostnad", "value:1100", rn.WRONG_CONCEPT_TAG],
                "text": "This is actually betalbar skatt",
                "target": [{"source": "https://viewer/123"}],
                "created": "2026-05-05T00:00:00+00:00",
                "updated": "2026-05-05T00:00:00+00:00",
            },
        ]
    }

    def fake_get(url, headers=None, params=None, timeout=None):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = fake_resp
        return m

    with patch("regnskapnoter.hypothesis.requests.get", side_effect=fake_get):
        df = rn.from_hypothesis(group_id="g", api_token="t", limit=2)

    assert len(df) == 2
    assert df["is_proposed_concept"].sum() == 1
    assert df["is_wrong_concept"].sum() == 1
    assert "regnskap-no:NewConcept" in df["regnskapnoter_concept_id"].tolist()


def test_proposed_concepts_filter():
    df = pd.DataFrame(
        [
            {
                "is_proposed_concept": True,
                "is_review_needed": False,
                "is_wrong_concept": False,
                "x": 1,
            },
            {
                "is_proposed_concept": False,
                "is_review_needed": True,
                "is_wrong_concept": False,
                "x": 2,
            },
        ]
    )
    assert len(rn.proposed_concepts(df)) == 1
    assert len(rn.review_queue(df)) == 1
