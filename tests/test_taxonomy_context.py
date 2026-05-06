"""Tests for regnskapnoter.taxonomy_context."""

from __future__ import annotations

import regnskapnoter as rn


def test_load_concept_contexts_returns_dict():
    ids = ["regnskap-no:Skattekostnad", "regnskap-no:Lonnskostnad"]
    result = rn.load_concept_contexts(ids)
    assert isinstance(result, dict)
    assert len(result) == 2
    for cid in ids:
        assert cid in result


def test_concept_context_has_label():
    ctx = rn.load_concept_contexts(["regnskap-no:Skattekostnad"])
    cc = ctx["regnskap-no:Skattekostnad"]
    assert cc.label_nb
    assert cc.concept_id == "regnskap-no:Skattekostnad"


def test_concept_context_has_calc_parent():
    ctx = rn.load_concept_contexts(["regnskap-no:Lonninger"])
    cc = ctx["regnskap-no:Lonninger"]
    assert cc.calc_parent == "regnskap-no:Lonnskostnad"


def test_concept_context_has_siblings():
    ctx = rn.load_concept_contexts(["regnskap-no:Folketrygdavgift"])
    cc = ctx["regnskap-no:Folketrygdavgift"]
    assert len(cc.calc_siblings) > 0
    assert "regnskap-no:Lonninger" in cc.calc_siblings


def test_concept_context_has_references():
    ctx = rn.load_concept_contexts(["regnskap-no:Skattekostnad"])
    cc = ctx["regnskap-no:Skattekostnad"]
    assert len(cc.references) > 0


def test_format_context_block_has_tags():
    ctx = rn.load_concept_contexts(["regnskap-no:Skattekostnad"])
    block = rn.format_context_block(ctx)
    assert block.startswith("<taxonomy_context>")
    assert block.endswith("</taxonomy_context>")


def test_format_context_block_truncates():
    ids = [
        "regnskap-no:Skattekostnad",
        "regnskap-no:Lonnskostnad",
        "regnskap-no:Lonninger",
    ]
    ctx = rn.load_concept_contexts(ids)
    block = rn.format_context_block(ctx, max_chars=200)
    assert len(block) <= 250  # some slack for closing tag
    assert "truncated" in block


def test_unknown_concept_gets_fallback_label():
    ctx = rn.load_concept_contexts(["regnskap-no:NonexistentConcept123"])
    cc = ctx["regnskap-no:NonexistentConcept123"]
    assert cc.label_nb == "NonexistentConcept123"
    assert cc.definition is None
    assert cc.calc_parent is None
