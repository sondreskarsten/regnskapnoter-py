import regnskapnoter as rn


def test_frameworks_dataframe():
    df = rn.frameworks()
    assert "framework" in df.columns
    assert "concept_id" in df.columns
    assert len(df) > 0


def test_list_frameworks_sorted():
    lf = rn.list_frameworks()
    assert list(lf.columns) == ["framework", "concept_count"]
    assert lf["concept_count"].is_monotonic_decreasing


def test_para_7_29_has_skattekostnad():
    cids = rn.concepts_in_framework("§ 7-29")
    assert any("Skatt" in c or "Forskjell" in c for c in cids)


def test_framework_for_concept_roundtrip():
    cids = rn.concepts_in_framework("§ 7-29")
    assert cids
    fws = rn.framework_for_concept(cids[0])
    assert "§ 7-29" in fws


def test_nrs_resultatskatt_listed():
    lf = rn.list_frameworks()
    assert lf["framework"].str.contains("Resultatskatt").any()
