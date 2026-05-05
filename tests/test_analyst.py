"""AnalystSession + analyst helpers, mocked HTTP."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd

import regnskapnoter as rn


def _mock_resp(status: int = 200, payload: dict | None = None) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = payload or {}
    return m


def test_analyst_session_construct():
    s = rn.AnalystSession(group_id="g", api_token="t")
    assert s.group_id == "g"
    assert s.api_token == "t"


def test_re_anchor_via_session():
    captured = []

    def fake_get(url, headers=None, params=None, timeout=None):
        captured.append(("get", url))
        return _mock_resp(200, {"id": "h1", "tags": ["concept:regnskap-no:Old", rn.REVIEW_TAG]})

    def fake_patch(url, headers=None, json=None, timeout=None):
        captured.append(("patch", url, json))
        return _mock_resp(200, {"id": "h1"})

    with (
        patch("regnskapnoter.hypothesis.requests.get", side_effect=fake_get),
        patch("regnskapnoter.hypothesis.requests.patch", side_effect=fake_patch),
    ):
        s = rn.AnalystSession(group_id="g", api_token="t")
        s.re_anchor(
            {"hypothesis_id": "h1", "uri": "urn:noter:811722332:2024"},
            exact="1 100",
            prefix="kost ",
            suffix="\n",
            page=5,
            new_concept_id="regnskap-no:Skattekostnad",
        )

    patch_call = next(c for c in captured if c[0] == "patch")
    payload = patch_call[2]
    assert "tags" in payload
    assert any(t.startswith("concept:regnskap-no:Skattekostnad") for t in payload["tags"])
    assert rn.REVIEW_TAG not in payload["tags"]
    selectors = payload["target"][0]["selector"]
    assert any(s.get("type") == "FragmentSelector" for s in selectors)
    assert any(
        "page=5" in s.get("value", "") for s in selectors if s.get("type") == "FragmentSelector"
    )


def test_propose_concept_via_session():
    def fake_get(url, headers=None, params=None, timeout=None):
        return _mock_resp(200, {"id": "h1", "tags": ["concept:regnskap-no:X"], "text": "orig"})

    captured = {}

    def fake_patch(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _mock_resp(200, {"id": "h1"})

    with (
        patch("regnskapnoter.hypothesis.requests.get", side_effect=fake_get),
        patch("regnskapnoter.hypothesis.requests.patch", side_effect=fake_patch),
    ):
        s = rn.AnalystSession(group_id="g", api_token="t")
        s.propose_concept(
            "h1",
            new_concept_id="regnskap-no:NyttKonsept",
            rationale="Recurring disclosure pattern across 12 firms",
            paragraph_citation="§ 7-XX (proposed)",
        )

    p = captured["payload"]
    assert rn.PROPOSED_CONCEPT_TAG in p["tags"]
    assert any(t.startswith("proposed:regnskap-no:NyttKonsept") for t in p["tags"])
    assert "Rationale" in p["text"]
    assert "§ 7-XX" in p["text"]


def test_reclassify_via_session():
    def fake_get(url, headers=None, params=None, timeout=None):
        return _mock_resp(200, {"id": "h1", "tags": ["concept:regnskap-no:Wrong"], "text": "orig"})

    captured = {}

    def fake_patch(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _mock_resp(200, {"id": "h1"})

    with (
        patch("regnskapnoter.hypothesis.requests.get", side_effect=fake_get),
        patch("regnskapnoter.hypothesis.requests.patch", side_effect=fake_patch),
    ):
        s = rn.AnalystSession(group_id="g", api_token="t")
        s.reclassify("h1", new_concept_id="regnskap-no:Right", rationale="post-7 not post-6")

    p = captured["payload"]
    assert any(t == "concept:regnskap-no:Right" for t in p["tags"])
    assert not any(t == "concept:regnskap-no:Wrong" for t in p["tags"])
    assert rn.WRONG_CONCEPT_TAG in p["tags"]


def test_build_annotations_with_urn_uses_urn_as_source():
    raw = {
        "orgnr": "811722332",
        "year": 2024,
        "notes": [
            {"note_number": "1", "title": "T", "full_text": "[[p:5]]Aksjekapital 1 000 000\n"}
        ],
    }
    obs = pd.DataFrame(
        {
            "orgnr": ["811722332"],
            "report_year": [2024],
            "concept_id": ["regnskap-no:Aksjekapital"],
            "value": [1000000],
        }
    )
    df = rn.build_annotations_with_urn(raw, obs)
    text_rows = df[df["target_type"] == "text"]
    assert (text_rows["source"] == "urn:noter:811722332:2024").all()


def test_iter_review_queue_paginates():
    pages = [
        {
            "rows": [
                {
                    "id": "h1",
                    "uri": "urn:noter:1:2024",
                    "tags": ["concept:x", "value:1", rn.REVIEW_TAG],
                    "text": "",
                    "target": [],
                    "updated": "2026-05-05T00:00:00+00:00",
                },
            ]
        },
        {"rows": []},
    ]
    page_idx = [0]

    def fake_get(url, headers=None, params=None, timeout=None):
        i = page_idx[0]
        page_idx[0] += 1
        return _mock_resp(200, pages[i])

    with patch("regnskapnoter.hypothesis.requests.get", side_effect=fake_get):
        items = list(rn.iter_review_queue(group_id="g", api_token="t", batch_size=1))

    assert len(items) == 1
    assert items[0]["concept_id"] == "x"
    assert items[0]["value"] == "1"
