"""GCS-backed LLM analyst loop for noter annotation review.

Replaces the previous Hypothes.is-backed implementation. Naive empiricism is
preserved: every action (post, re-anchor, reclassify, propose-concept, delete)
is an immutable event row at its own timestamp. The current state of any
annotation is composed at query time from the latest non-delete event.

Storage:

    gs://sondre_brreg_data/annotations/noter/{orgnr}/{year}/events.parquet

Typical use:

    import regnskapnoter as rn
    session = rn.AnalystSession()             # uses default GCS bucket/prefix

    # Push initial annotations for one filing
    raw_json, observations = rn.cli._load_raw_and_observations("811722332", 2024)
    annotations = rn.build_annotations_with_urn(raw_json, observations)
    session.post_observations(annotations, orgnr="811722332", year=2024)

    # LLM analyst iterates the review queue
    for ann in session.review_queue(orgnr="811722332", year=2024):
        raw = session.resolve_raw(ann["source"])
        decision = llm_decide(ann, raw)
        if decision["action"] == "re-anchor":
            session.re_anchor(ann, exact=..., prefix=..., suffix=..., page=...)
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from regnskapnoter.store import (
    GCSAnnotationStore,
    annotations_to_post_events,
    make_mutation_event,
    next_sequence,
)
from regnskapnoter.urn import parse_urn, to_gcs_path, to_pdf_gcs_path, to_urn


def _parse_source(source: str) -> tuple[str, int] | None:
    """Parse a 'noter:{orgnr}:{year}' source string back to (orgnr, year)."""
    if source.startswith("noter:"):
        parts = source.split(":")
        if len(parts) == 3:
            try:
                return parts[1], int(parts[2])
            except ValueError:
                return None
    if source.startswith("urn:noter:"):
        return parse_urn(source)
    return None


@dataclass
class AnalystSession:
    """Stateful session for the LLM analyst loop, backed by GCS parquet."""

    bucket: str = "sondre_brreg_data"
    prefix: str = "annotations/noter"
    raw_bucket: str = "sondre_brreg_data"
    raw_prefix: str = "raw/noter_extraction_2025/raw"
    pdf_bucket: str = "brreg-regnskap"
    creator: str = "llm-analyst"
    store: GCSAnnotationStore = field(init=False)

    def __post_init__(self) -> None:
        self.store = GCSAnnotationStore(bucket=self.bucket, prefix=self.prefix)

    # ------------------------------------------------------------------ post
    def post_observations(
        self,
        annotations: pd.DataFrame,
        *,
        orgnr: str,
        year: int,
        target_type_filter: str = "text",
    ) -> int:
        """Convert a build_annotations() DataFrame to seq=0 'post' events and
        append them to the GCS shard for (orgnr, year). Returns rows added.
        Idempotent: re-running with the same input is a no-op.
        """
        df = (
            annotations[annotations["target_type"] == target_type_filter]
            if target_type_filter
            else annotations
        )
        events = annotations_to_post_events(df, orgnr=orgnr, year=year, creator=self.creator)
        return self.store.append_events(events)

    # ------------------------------------------------------------------ pull
    def review_queue(self, *, orgnr: str, year: int) -> Iterable[dict]:
        """Yield annotations whose current state is unmatched."""
        df = self.store.review_queue(str(orgnr), int(year))
        yield from df.to_dict(orient="records")

    def fetch_all(self, *, orgnr: str, year: int) -> pd.DataFrame:
        """Return current state for one filing as a DataFrame."""
        return self.store.current_state(str(orgnr), int(year))

    def history(self, *, orgnr: str, year: int) -> pd.DataFrame:
        """Return the full immutable event log for one filing."""
        return self.store.read_shard(str(orgnr), int(year))

    def proposed_concepts(
        self,
        *,
        orgnr_year_pairs: Iterable[tuple[str, int]] | None = None,
    ) -> pd.DataFrame:
        """Return all 'propose-concept' events across selected (or all) shards."""
        return self.store.proposed_concepts(orgnr_year_pairs)

    def stats(self, *, orgnr: str, year: int) -> dict:
        return self.store.stats(str(orgnr), int(year))

    # ------------------------------------------------------------------ resolve
    def resolve_raw(self, urn_or_source: str) -> dict:
        """Resolve a URN or source string to the raw extraction JSON dict."""
        from google.cloud import storage

        # Allow either 'noter:{orgnr}:{year}' or full 'urn:noter:{orgnr}:{year}'
        parsed = _parse_source(urn_or_source)
        if parsed is None:
            raise ValueError(f"Cannot parse source: {urn_or_source}")
        orgnr, year = parsed
        urn = to_urn(orgnr, year)
        gcs_path = to_gcs_path(urn, bucket=self.raw_bucket, prefix=self.raw_prefix)
        if not gcs_path:
            raise ValueError(f"Cannot resolve URN to GCS path: {urn}")
        _, _, rest = gcs_path.partition("gs://")
        bucket_name, _, blob_name = rest.partition("/")
        return json.loads(storage.Client().bucket(bucket_name).blob(blob_name).download_as_bytes())

    def resolve_pdf_uri(self, urn_or_source: str) -> str | None:
        parsed = _parse_source(urn_or_source)
        if parsed is None:
            return None
        return to_pdf_gcs_path(to_urn(*parsed), bucket=self.pdf_bucket)

    def get_pdf_bytes(self, urn_or_source: str) -> bytes:
        from google.cloud import storage

        from regnskapnoter.urn import find_pdf_in_gcs

        parsed = _parse_source(urn_or_source)
        if parsed is None:
            raise ValueError(f"Cannot parse source: {urn_or_source}")
        urn = to_urn(*parsed)
        gcs_uri = find_pdf_in_gcs(urn, bucket=self.pdf_bucket)
        if gcs_uri is None:
            raise FileNotFoundError(f"No PDF found in GCS for {urn}")
        _, _, rest = gcs_uri.partition("gs://")
        bucket_name, _, blob_name = rest.partition("/")
        return storage.Client().bucket(bucket_name).blob(blob_name).download_as_bytes()

    # ------------------------------------------------------------------ mutations
    def _append_mutation(
        self,
        base: pd.Series,
        *,
        event_type: str,
        new_concept_id: str | None = None,
        new_selector_json: str | None = None,
        new_page: int | None = None,
        new_match_status: str | None = None,
        rationale: str = "",
        citation: str = "",
        confidence: float | None = None,
    ) -> dict[str, Any]:
        """Read shard, compute next sequence for this annotation_id, append event."""
        orgnr = str(base["orgnr"])
        year = int(base["year"])
        events = self.store.read_shard(orgnr, year)
        seq = next_sequence(events, base["annotation_id"])
        event = make_mutation_event(
            base=base,
            event_type=event_type,
            sequence=seq,
            new_concept_id=new_concept_id,
            new_selector_json=new_selector_json,
            new_page=new_page,
            new_match_status=new_match_status,
            rationale=rationale,
            citation=citation,
            confidence=confidence,
            creator=self.creator,
        )
        new_events = pd.DataFrame([event])
        self.store.append_events(new_events)
        return event

    def re_anchor(
        self,
        annotation: dict | pd.Series,
        *,
        exact: str,
        prefix: str = "",
        suffix: str = "",
        page: int | None = None,
        new_concept_id: str | None = None,
        rationale: str = "",
        confidence: float | None = None,
    ) -> dict:
        """Append a re-anchor event with a fresh TextQuoteSelector + optional FragmentSelector."""
        base = annotation if isinstance(annotation, pd.Series) else pd.Series(annotation)
        text_selector = {
            "type": "TextQuoteSelector",
            "exact": exact,
            "prefix": prefix,
            "suffix": suffix,
        }
        if page is not None:
            selector = {
                "type": "FragmentSelector",
                "conformsTo": "http://tools.ietf.org/rfc/rfc3778",
                "value": f"page={page}",
                "refinedBy": text_selector,
            }
        else:
            selector = text_selector
        return self._append_mutation(
            base,
            event_type="re-anchor",
            new_concept_id=new_concept_id,
            new_selector_json=json.dumps(selector, ensure_ascii=False),
            new_page=page,
            new_match_status="reviewed",
            rationale=rationale,
            confidence=confidence,
        )

    def reclassify(
        self,
        annotation: dict | pd.Series,
        *,
        new_concept_id: str,
        rationale: str = "",
        confidence: float | None = None,
    ) -> dict:
        base = annotation if isinstance(annotation, pd.Series) else pd.Series(annotation)
        return self._append_mutation(
            base,
            event_type="reclassify",
            new_concept_id=new_concept_id,
            new_match_status="reviewed",
            rationale=rationale,
            confidence=confidence,
        )

    def propose_concept(
        self,
        annotation: dict | pd.Series,
        *,
        new_concept_id: str,
        rationale: str,
        paragraph_citation: str = "",
        confidence: float | None = None,
    ) -> dict:
        base = annotation if isinstance(annotation, pd.Series) else pd.Series(annotation)
        return self._append_mutation(
            base,
            event_type="propose-concept",
            new_concept_id=new_concept_id,
            new_match_status="reviewed",
            rationale=rationale,
            citation=paragraph_citation,
            confidence=confidence,
        )

    def delete(
        self,
        annotation: dict | pd.Series,
        *,
        rationale: str = "",
        confidence: float | None = None,
    ) -> dict:
        base = annotation if isinstance(annotation, pd.Series) else pd.Series(annotation)
        return self._append_mutation(
            base,
            event_type="delete",
            new_match_status="deleted",
            rationale=rationale,
            confidence=confidence,
        )


def build_annotations_with_urn(
    raw_json: dict,
    observations: pd.DataFrame,
    *,
    pipeline_version: str = "noter-extraction-2025",
) -> pd.DataFrame:
    """Wrapper around build_annotations that sets source_text_uri to the URN
    and source_pdf_uri to the canonical PDF GCS path."""
    from regnskapnoter.annotations import build_annotations

    orgnr = str(raw_json.get("orgnr") or "")
    year = raw_json.get("year")
    text_uri = to_urn(orgnr, year) if orgnr and year else None
    pdf_uri = to_pdf_gcs_path(text_uri) if text_uri else None
    return build_annotations(
        raw_json,
        observations,
        source_text_uri=text_uri,
        source_pdf_uri=pdf_uri,
        pipeline_version=pipeline_version,
    )
