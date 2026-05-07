"""Tests for regnskapnoter.context."""

from __future__ import annotations

import os

import pytest

from regnskapnoter.context import (
    SingleConceptContext,
    _qualify,
    _strip_ns,
    format_context_block,
)


def test_qualify():
    assert _qualify("Skattekostnad") == "regnskap-no:Skattekostnad"
    assert _qualify("regnskap-no:Skattekostnad") == "regnskap-no:Skattekostnad"


def test_strip_ns():
    assert _strip_ns("regnskap-no:Skattekostnad") == "Skattekostnad"
    assert _strip_ns("Skattekostnad") == "Skattekostnad"


def test_format_context_block():
    ctx = SingleConceptContext(
        concept_id="Skattekostnad",
        label="Skattekostnad",
        definition="20. Skattekostnad",
        parent_ids=["Aarsresultat"],
        child_ids=[],
        sibling_ids=["ResultatForSkattekostnad"],
        law_texts=[
            {
                "publisher": "Stortinget",
                "document": "regnskapsloven",
                "paragraph": "§ 6-1",
                "text": "§ 6-1. Resultatregnskap...",
                "source": "lovdata.no/lov/1998-07-17-56",
            },
        ],
        frameworks=["[200000] Resultatregnskap"],
    )
    block = format_context_block(ctx)
    assert "Skattekostnad" in block
    assert "Aarsresultat" in block
    assert "ResultatForSkattekostnad" in block
    assert "§ 6-1. Resultatregnskap" in block
    assert "[200000]" in block


def test_format_context_block_no_law_text():
    ctx = SingleConceptContext(
        concept_id="Test",
        label=None,
        definition=None,
        parent_ids=[],
        child_ids=[],
        sibling_ids=[],
        law_texts=[
            {
                "publisher": "NRS",
                "document": "NRS 9",
                "paragraph": "punkt 4",
                "text": None,
                "source": None,
            }
        ],
        frameworks=[],
    )
    block = format_context_block(ctx)
    assert "NRS 9 punkt 4 (text not available)" in block


@pytest.mark.skipif(
    not os.environ.get("RN_LIVE_TESTS"),
    reason="live tests disabled; set RN_LIVE_TESTS=1",
)
def test_build_observation_context_live():
    from regnskapnoter.context import build_observation_context

    text = build_observation_context("Skattekostnad", fiscal_year=2024)
    assert "Skattekostnad" in text
    assert len(text) > 100
