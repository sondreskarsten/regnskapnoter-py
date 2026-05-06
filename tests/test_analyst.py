"""AnalystSession backed by GCS store: full mutation cycle."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd

import regnskapnoter as rn


class _FakeBlob:
    def __init__(self):
        self._data = None

    def exists(self):
        return self._data is not None

    def download_as_bytes(self):
        return self._data

    def upload_from_file(self, fp, content_type=None):
        self._data = fp.read()


class _FakeClient:
    def __init__(self):
        self._blobs = {}

    def bucket(self, name):
        c = self

        class _B:
            def blob(self, key):
                full = f"{name}/{key}"
                if full not in c._blobs:
                    c._blobs[full] = _FakeBlob()
                return c._blobs[full]

        return _B()

    def list_blobs(self, *a, **k):
        return []


def _sample_anns():
    return pd.DataFrame(
        [
            {
                "annotation_id": "a1",
                "target_type": "text",
                "selector_json": "",
                "body_concept_id": "regnskap-no:Skattekostnad",
                "body_value": "1100",
                "note_number": "1",
                "note_title": "Skatt",
                "page": 5,
                "match_status": "unmatched",
            },
        ]
    )


def _seed_session():
    fake = _FakeClient()
    p = patch("google.cloud.storage.Client", side_effect=lambda *a, **k: fake)
    p.start()
    session = rn.AnalystSession(bucket="b", prefix="p")
    session.post_observations(_sample_anns(), orgnr="123", year=2024)
    return session, fake, p


def test_post_observations_creates_events():
    session, _fake, p = _seed_session()
    try:
        history = session.history(orgnr="123", year=2024)
        assert len(history) == 1
        assert history.iloc[0]["event_type"] == "post"
        assert history.iloc[0]["match_status"] == "unmatched"
    finally:
        p.stop()


def test_re_anchor_appends_immutable_event():
    session, _fake, p = _seed_session()
    try:
        ann = next(iter(session.review_queue(orgnr="123", year=2024)))
        session.re_anchor(
            ann,
            exact="1 100",
            prefix="kost ",
            suffix="\n",
            page=5,
            rationale="found in note 1",
            confidence=0.95,
        )
        history = session.history(orgnr="123", year=2024)
        assert len(history) == 2
        assert (history["event_type"] == "post").sum() == 1
        assert (history["event_type"] == "re-anchor").sum() == 1
        post_row = history[history["sequence"] == 0].iloc[0]
        assert post_row["match_status"] == "unmatched"
        assert post_row["selector_json"] == ""
        cur = session.fetch_all(orgnr="123", year=2024)
        assert cur.iloc[0]["match_status"] == "reviewed"
        assert "FragmentSelector" in cur.iloc[0]["selector_json"]
        assert "page=5" in cur.iloc[0]["selector_json"]
    finally:
        p.stop()


def test_reclassify_changes_concept_id():
    session, _fake, p = _seed_session()
    try:
        ann = next(iter(session.review_queue(orgnr="123", year=2024)))
        session.reclassify(
            ann,
            new_concept_id="regnskap-no:BetalbarSkattAaret",
            rationale="post-7 not post-6",
            confidence=0.85,
        )
        cur = session.fetch_all(orgnr="123", year=2024)
        assert cur.iloc[0]["concept_id"] == "regnskap-no:BetalbarSkattAaret"
        assert cur.iloc[0]["match_status"] == "reviewed"
    finally:
        p.stop()


def test_propose_concept_records_citation():
    session, _fake, p = _seed_session()
    try:
        ann = next(iter(session.review_queue(orgnr="123", year=2024)))
        session.propose_concept(
            ann,
            new_concept_id="regnskap-no:NyttKonsept",
            rationale="recurring disclosure",
            paragraph_citation="§ 7-XX (proposed)",
            confidence=0.7,
        )
        proposed = session.proposed_concepts(orgnr_year_pairs=[("123", 2024)])
        assert len(proposed) == 1
        assert proposed.iloc[0]["concept_id"] == "regnskap-no:NyttKonsept"
        assert proposed.iloc[0]["citation"] == "§ 7-XX (proposed)"
    finally:
        p.stop()


def test_delete_removes_from_current_state():
    session, _fake, p = _seed_session()
    try:
        ann = next(iter(session.review_queue(orgnr="123", year=2024)))
        session.delete(ann, rationale="spurious", confidence=0.9)
        cur = session.fetch_all(orgnr="123", year=2024)
        assert len(cur) == 0
        # but the history retains BOTH events
        history = session.history(orgnr="123", year=2024)
        assert len(history) == 2
        assert "delete" in history["event_type"].tolist()
    finally:
        p.stop()


def test_naive_empiricism_post_row_never_modified():
    """The original post event must remain bit-identical after mutations."""
    session, _fake, p = _seed_session()
    try:
        ann = next(iter(session.review_queue(orgnr="123", year=2024)))
        history_before = session.history(orgnr="123", year=2024).copy()
        session.re_anchor(ann, exact="x", prefix="", suffix="", page=1, confidence=0.9)
        session.reclassify(ann, new_concept_id="regnskap-no:Y", confidence=0.9)
        history_after = session.history(orgnr="123", year=2024)
        post_before = history_before[history_before["sequence"] == 0].iloc[0]
        post_after = history_after[
            (history_after["sequence"] == 0)
            & (history_after["annotation_id"] == post_before["annotation_id"])
        ].iloc[0]
        for col in [
            "concept_id",
            "value",
            "selector_json",
            "match_status",
            "event_type",
            "creator",
        ]:
            assert post_before[col] == post_after[col], f"col {col} mutated!"
    finally:
        p.stop()


def test_build_annotations_with_urn_uses_urn():
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


def test_resolve_raw_via_session():
    fake = _FakeClient()
    fake._blobs[
        "sondre_brreg_data/raw/noter_extraction_2025/raw/811722332_aarsregnskap_2024_v2.json"
    ] = MagicMock(
        exists=lambda: True,
        download_as_bytes=lambda: b'{"orgnr":"811722332","year":2024,"notes":[]}',
    )
    with patch("google.cloud.storage.Client", side_effect=lambda *a, **k: fake):
        session = rn.AnalystSession()
        raw = session.resolve_raw("urn:noter:811722332:2024")
        assert raw["orgnr"] == "811722332"
        assert raw["year"] == 2024
