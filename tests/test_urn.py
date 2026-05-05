import regnskapnoter as rn
from regnskapnoter.urn import parse_urn, to_gcs_path, to_pdf_gcs_path, to_urn


def test_to_urn_pads_orgnr():
    assert to_urn(811722332, 2024) == "urn:noter:811722332:2024"
    assert to_urn("811722332", 2024) == "urn:noter:811722332:2024"
    assert to_urn(11111, 2020) == "urn:noter:000011111:2020"


def test_parse_urn_roundtrip():
    urn = "urn:noter:811722332:2024"
    assert parse_urn(urn) == ("811722332", 2024)


def test_parse_urn_rejects_malformed():
    assert parse_urn("urn:other:1:2") is None
    assert parse_urn("not a urn") is None
    assert parse_urn("urn:noter:abc:2024") is None


def test_to_gcs_path_resolves():
    urn = "urn:noter:811722332:2024"
    p = to_gcs_path(urn)
    assert (
        p
        == "gs://sondre_brreg_data/raw/noter_extraction_2025/raw/811722332_aarsregnskap_2024_v2.json"
    )


def test_to_pdf_gcs_path_resolves():
    p = to_pdf_gcs_path("urn:noter:811722332:2024")
    assert p == "gs://brreg-regnskap/811722332_aarsregnskap_2024.pdf"


def test_helpers_exposed_at_top_level():
    assert callable(rn.to_urn)
    assert callable(rn.parse_urn)
    assert callable(rn.to_gcs_path)
