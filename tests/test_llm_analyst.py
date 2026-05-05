"""Tests for examples/llm_analyst.py with Gemini and AnalystSession mocked."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip(
    "google.genai",
    reason="examples/llm_analyst.py requires google-genai (install with [llm] extra)",
)

# Make the example importable
EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
sys.path.insert(0, str(EXAMPLES_DIR))


@pytest.fixture
def llm_analyst():
    """Import the example with the genai Client mocked at construction time."""
    mock_client = MagicMock()
    with patch("google.genai.Client", return_value=mock_client):
        if "llm_analyst" in sys.modules:
            del sys.modules["llm_analyst"]
        import llm_analyst  # type: ignore[import-not-found]

        llm_analyst._mock_client = mock_client
        yield llm_analyst


@pytest.fixture
def sample_annotation():
    return {
        "hypothesis_id": "h1",
        "uri": "urn:noter:811722332:2024",
        "concept_id": "regnskap-no:Skattekostnad",
        "value": "1100",
    }


@pytest.fixture
def sample_raw_json():
    return {
        "orgnr": "811722332",
        "year": 2024,
        "total_pages": 12,
        "notes": [
            {
                "note_number": "1",
                "title": "Skattekostnad",
                "page_start": 5,
                "page_end": 5,
                "full_text": "[[p:5]]Skattekostnad 1 100\nResultat før skatt...",
            },
        ],
    }


def test_format_user_prompt_includes_pdf_context(llm_analyst, sample_annotation, sample_raw_json):
    prompt = llm_analyst._format_user_prompt(sample_annotation, sample_raw_json)
    assert "regnskap-no:Skattekostnad" in prompt
    assert "[[p:5]]" in prompt
    assert "811722332" in prompt
    assert "2024" in prompt
    assert "[[p:5]]" in prompt


def test_call_gemini_returns_parsed_json(llm_analyst):
    response = MagicMock()
    response.text = json.dumps(
        {
            "action": "re-anchor",
            "exact": "1 100",
            "prefix": "Skattekostnad ",
            "suffix": "\nResultat",
            "page": 5,
            "rationale": "matches",
            "confidence": 0.95,
        }
    )
    llm_analyst._mock_client.models.generate_content.return_value = response

    result = llm_analyst._call_gemini("user text", b"%PDF-1.4 fake")
    assert result["action"] == "re-anchor"
    assert result["page"] == 5
    assert result["confidence"] == 0.95


def test_dispatch_re_anchor(llm_analyst, sample_annotation):
    session = MagicMock()
    decision = {
        "action": "re-anchor",
        "exact": "1 100",
        "prefix": "Skattekostnad ",
        "suffix": "\n",
        "page": 5,
        "confidence": 0.9,
    }
    outcome = llm_analyst._dispatch(session, sample_annotation, decision)
    assert "re-anchored" in outcome
    session.re_anchor.assert_called_once()
    kw = session.re_anchor.call_args.kwargs
    assert kw["page"] == 5
    assert kw["exact"] == "1 100"


def test_dispatch_skips_low_confidence(llm_analyst, sample_annotation):
    session = MagicMock()
    decision = {"action": "re-anchor", "exact": "x", "page": 1, "confidence": 0.3}
    outcome = llm_analyst._dispatch(session, sample_annotation, decision)
    assert "skipped_low_confidence" in outcome
    session.re_anchor.assert_not_called()


def test_dispatch_reclassify(llm_analyst, sample_annotation):
    session = MagicMock()
    decision = {
        "action": "reclassify",
        "new_concept_id": "regnskap-no:BetalbarSkattAaret",
        "rationale": "post-7 not post-6",
        "confidence": 0.85,
    }
    outcome = llm_analyst._dispatch(session, sample_annotation, decision)
    assert outcome.startswith("reclassified")
    session.reclassify.assert_called_once()


def test_dispatch_reclassify_rejects_invalid_id(llm_analyst, sample_annotation):
    session = MagicMock()
    decision = {
        "action": "reclassify",
        "new_concept_id": "bogus-id-no-prefix",
        "confidence": 0.9,
    }
    outcome = llm_analyst._dispatch(session, sample_annotation, decision)
    assert "invalid_concept_id" in outcome
    session.reclassify.assert_not_called()


def test_dispatch_propose_concept(llm_analyst, sample_annotation):
    session = MagicMock()
    decision = {
        "action": "propose-concept",
        "proposed_concept_id": "regnskap-no:NyttKonsept",
        "rationale": "novel disclosure type observed",
        "citation": "§ 7-XX (proposed)",
        "confidence": 0.7,
    }
    outcome = llm_analyst._dispatch(session, sample_annotation, decision)
    assert "proposed" in outcome
    session.propose_concept.assert_called_once()


def test_dispatch_delete(llm_analyst, sample_annotation):
    session = MagicMock()
    decision = {"action": "delete", "rationale": "spurious", "confidence": 0.8}
    outcome = llm_analyst._dispatch(session, sample_annotation, decision)
    assert outcome == "deleted"
    session.delete.assert_called_once()
