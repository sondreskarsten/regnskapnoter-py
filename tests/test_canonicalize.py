import pandas as pd

import regnskapnoter as rn


def test_concept_for_column_known():
    cid = rn.concept_for_column("skatt_aaret", "betalbar_skatt")
    assert cid == "regnskap-no:BetalbarSkattAaret"


def test_concept_for_column_unknown():
    assert rn.concept_for_column("skatt_aaret", "nonexistent_column") is None


def test_canonicalize_skatt_aaret():
    wide = pd.DataFrame(
        {
            "orgnr": ["123456789", "987654321"],
            "report_year": [2024, 2024],
            "betalbar_skatt": [100, 200],
            "skattekostnad_total": [120, 220],
            "irrelevant_column": ["x", "y"],
        }
    )
    long = rn.canonicalize(wide, table="skatt_aaret")
    assert set(long.columns) == {"orgnr", "report_year", "value", "concept_id"}
    assert "regnskap-no:BetalbarSkattAaret" in long["concept_id"].tolist()
    assert "regnskap-no:Skattekostnad" in long["concept_id"].tolist()
    assert (long["concept_id"] != "irrelevant_column").all()
    assert len(long) == 4  # 2 orgnrs × 2 mapped columns


def test_canonicalize_keeps_unmapped_when_requested():
    wide = pd.DataFrame({"orgnr": ["1"], "report_year": [2024], "unmapped": [42]})
    long = rn.canonicalize(wide, table="nonexistent_table", drop_unmapped=False)
    assert long["concept_id"].isna().all()
