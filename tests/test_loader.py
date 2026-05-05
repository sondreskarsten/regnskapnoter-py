"""Smoke tests against the live public bucket."""

import pandas as pd

import regnskapnoter as rn


def test_concepts_loads():
    df = rn.concepts()
    assert isinstance(df, pd.DataFrame)
    assert len(df) >= 250
    assert "concept_id" in df.columns


def test_definitions_loads():
    df = rn.definitions()
    assert len(df) == len(rn.concepts())


def test_labels_have_nb_and_en():
    lab = rn.labels()
    nb = lab[
        (lab["lang"] == "nb")
        & (lab["role"] == "standardLabel")
        & (lab["subject_kind"] == "concept")
    ]
    en = lab[
        (lab["lang"] == "en")
        & (lab["role"] == "standardLabel")
        & (lab["subject_kind"] == "concept")
    ]
    assert len(nb) == len(rn.concepts())
    assert len(en) == len(rn.concepts())


def test_calc_arcs_nonempty():
    assert len(rn.calc_arcs()) > 50


def test_axes_count():
    assert len(rn.axes()) == 4


def test_version_default_is_latest():
    assert rn.version() == "latest"


def test_set_version_v102():
    rn.set_version("v1.0.2")
    df = rn.concepts()
    assert len(df) == 279
    rn.set_version("latest")
