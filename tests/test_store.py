"""GCSAnnotationStore: tests with the GCS layer mocked."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd

from regnskapnoter.store import (
    EVENT_COLUMNS,
    GCSAnnotationStore,
    annotations_to_post_events,
    make_mutation_event,
    next_sequence,
)


class _FakeBlob:
    """In-memory stand-in for google.cloud.storage.Blob."""

    def __init__(self):
        self._data: bytes | None = None

    def exists(self):
        return self._data is not None

    def download_as_bytes(self):
        if self._data is None:
            raise FileNotFoundError("blob does not exist")
        return self._data

    def upload_from_file(self, fp, content_type=None):
        self._data = fp.read()


class _FakeClient:
    def __init__(self):
        self._blobs: dict[str, _FakeBlob] = {}

    def bucket(self, name):
        c = self

        class _B:
            def blob(self, key):
                full = f"{name}/{key}"
                if full not in c._blobs:
                    c._blobs[full] = _FakeBlob()
                return c._blobs[full]

        return _B()

    def list_blobs(self, *args, **kwargs):
        prefix = kwargs.get("prefix") or (args[1] if len(args) > 1 else "")
        bucket_name = args[0] if args else ""
        if hasattr(bucket_name, "name"):
            bucket_name = bucket_name.name
        out = []
        for k, b in self._blobs.items():
            if not b.exists():
                continue
            if k.startswith(f"{bucket_name}/{prefix}"):
                rel = k[len(bucket_name) + 1 :]
                m = MagicMock()
                m.name = rel
                out.append(m)
        return out


def _patch_storage(client_factory):
    return patch("google.cloud.storage.Client", side_effect=client_factory)


def _sample_annotations() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "annotation_id": "a1",
                "target_type": "text",
                "selector_json": '{"type":"TextQuoteSelector","exact":"x"}',
                "body_concept_id": "regnskap-no:Test",
                "body_value": "100",
                "note_number": "1",
                "note_title": "T",
                "page": 5,
                "match_status": "matched",
            },
            {
                "annotation_id": "a2",
                "target_type": "text",
                "selector_json": "",
                "body_concept_id": "regnskap-no:Other",
                "body_value": "200",
                "note_number": "",
                "note_title": "",
                "page": None,
                "match_status": "unmatched",
            },
        ]
    )


def test_annotations_to_post_events_shape():
    df = _sample_annotations()
    events = annotations_to_post_events(df, orgnr="123", year=2024)
    assert len(events) == 2
    assert set(events.columns) == set(EVENT_COLUMNS)
    assert (events["sequence"] == 0).all()
    assert (events["event_type"] == "post").all()
    assert events.iloc[0]["concept_id"] == "regnskap-no:Test"


def test_append_and_read_roundtrip():
    fake = _FakeClient()
    with _patch_storage(lambda *a, **k: fake):
        store = GCSAnnotationStore(bucket="b", prefix="p")
        events = annotations_to_post_events(_sample_annotations(), orgnr="123", year=2024)
        added = store.append_events(events)
        assert added == 2
        roundtrip = store.read_shard("123", 2024)
        assert len(roundtrip) == 2
        assert set(roundtrip["annotation_id"]) == {"a1", "a2"}


def test_append_is_idempotent():
    fake = _FakeClient()
    with _patch_storage(lambda *a, **k: fake):
        store = GCSAnnotationStore(bucket="b", prefix="p")
        events = annotations_to_post_events(_sample_annotations(), orgnr="123", year=2024)
        first = store.append_events(events)
        second = store.append_events(events)
        assert first == 2
        assert second == 0


def test_next_sequence_initial_and_after_mutation():
    events = pd.DataFrame(
        [
            {"annotation_id": "a1", "sequence": 0, "event_type": "post"},
            {"annotation_id": "a1", "sequence": 1, "event_type": "re-anchor"},
            {"annotation_id": "a2", "sequence": 0, "event_type": "post"},
        ]
    )
    assert next_sequence(events, "a1") == 2
    assert next_sequence(events, "a2") == 1
    assert next_sequence(events, "a3") == 0
    assert next_sequence(pd.DataFrame(columns=EVENT_COLUMNS), "anything") == 0


def test_current_state_picks_latest_per_annotation():
    fake = _FakeClient()
    with _patch_storage(lambda *a, **k: fake):
        store = GCSAnnotationStore(bucket="b", prefix="p")
        post = annotations_to_post_events(_sample_annotations(), orgnr="123", year=2024)
        store.append_events(post)

        base = store.read_shard("123", 2024).iloc[1]
        mut = make_mutation_event(
            base=base,
            event_type="re-anchor",
            sequence=1,
            new_match_status="reviewed",
            rationale="LLM found a quote",
            confidence=0.9,
            creator="llm-analyst",
        )
        store.append_events(pd.DataFrame([mut]))

        cur = store.current_state("123", 2024)
        a2_row = cur[cur["annotation_id"] == "a2"].iloc[0]
        assert a2_row["match_status"] == "reviewed"
        assert a2_row["sequence"] == 1


def test_delete_excludes_from_current_state():
    fake = _FakeClient()
    with _patch_storage(lambda *a, **k: fake):
        store = GCSAnnotationStore(bucket="b", prefix="p")
        post = annotations_to_post_events(_sample_annotations(), orgnr="123", year=2024)
        store.append_events(post)
        base = store.read_shard("123", 2024).iloc[0]
        del_event = make_mutation_event(
            base=base,
            event_type="delete",
            sequence=1,
            new_match_status="deleted",
            creator="llm-analyst",
        )
        store.append_events(pd.DataFrame([del_event]))
        cur = store.current_state("123", 2024)
        assert "a1" not in cur["annotation_id"].tolist()
        assert "a2" in cur["annotation_id"].tolist()


def test_review_queue_filters_unmatched():
    fake = _FakeClient()
    with _patch_storage(lambda *a, **k: fake):
        store = GCSAnnotationStore(bucket="b", prefix="p")
        post = annotations_to_post_events(_sample_annotations(), orgnr="123", year=2024)
        store.append_events(post)
        rq = store.review_queue("123", 2024)
        assert len(rq) == 1
        assert rq.iloc[0]["annotation_id"] == "a2"


def test_stats_counts():
    fake = _FakeClient()
    with _patch_storage(lambda *a, **k: fake):
        store = GCSAnnotationStore(bucket="b", prefix="p")
        store.append_events(
            annotations_to_post_events(_sample_annotations(), orgnr="123", year=2024)
        )
        s = store.stats("123", 2024)
        assert s["events_total"] == 2
        assert s["annotations_active"] == 2
        assert s["annotations_matched"] == 1
        assert s["annotations_unmatched"] == 1
