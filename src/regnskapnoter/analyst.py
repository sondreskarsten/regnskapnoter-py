"""High-level LLM analyst loop for noter annotation review.

Designed for an LLM analyst (no human UI) that:
1. Pulls annotations needing review from Hypothes.is
2. Reads the underlying raw JSON / PDF directly from GCS
3. Decides on a re-anchor (new TextQuoteSelector) or a re-classification
4. Writes the decision back via PATCH

Typical use:

    import regnskapnoter as rn

    session = rn.AnalystSession(
        group_id="abc123",
        api_token=os.environ["HYPOTHESIS_TOKEN"],
    )

    for ann in session.review_queue():
        raw_json = session.resolve_raw(ann["uri"])
        # LLM-side decision logic
        decision = decide(raw_json, ann)
        if decision.action == "re-anchor":
            session.re_anchor(ann, exact=decision.exact, prefix=decision.prefix,
                              suffix=decision.suffix, page=decision.page)
        elif decision.action == "delete":
            session.delete(ann)
        elif decision.action == "propose-concept":
            session.propose_concept(ann, new_concept_id=decision.concept_id,
                                    rationale=decision.rationale)
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass

import pandas as pd

from regnskapnoter import hypothesis as _h
from regnskapnoter.urn import to_gcs_path, to_pdf_gcs_path, to_urn


@dataclass
class AnalystSession:
    """Stateful session bundling group_id + api_token for the LLM analyst loop."""

    group_id: str
    api_token: str
    raw_bucket: str = "sondre_brreg_data"
    raw_prefix: str = "raw/noter_extraction_2025/raw"
    pdf_bucket: str = "brreg-regnskap"

    # ------------------------------------------------------------------ pull
    def review_queue(self, batch_size: int = 50) -> Iterable[dict]:
        """Generator over annotations tagged review-needed."""
        return _h.iter_review_queue(
            group_id=self.group_id,
            api_token=self.api_token,
            batch_size=batch_size,
            tag=_h.REVIEW_TAG,
        )

    def proposed_concepts_queue(self, batch_size: int = 50) -> Iterable[dict]:
        """Generator over annotations tagged proposed-concept."""
        return _h.iter_review_queue(
            group_id=self.group_id,
            api_token=self.api_token,
            batch_size=batch_size,
            tag=_h.PROPOSED_CONCEPT_TAG,
        )

    def fetch_all(self, tag_filter: list[str] | None = None, limit: int = 1000) -> pd.DataFrame:
        """Fetch all annotations as DataFrame (uses from_hypothesis under the hood)."""
        return _h.from_hypothesis(
            group_id=self.group_id,
            api_token=self.api_token,
            tag_filter=tag_filter,
            limit=limit,
        )

    # ------------------------------------------------------------------ resolve URN -> content
    def resolve_raw(self, urn_or_uri: str) -> dict:
        """Resolve a URN to the raw extraction JSON (dict). Lazy GCS read."""
        from google.cloud import storage

        gcs_path = (
            to_gcs_path(
                urn_or_uri,
                bucket=self.raw_bucket,
                prefix=self.raw_prefix,
            )
            if urn_or_uri.startswith("urn:noter:")
            else urn_or_uri
        )

        if not gcs_path or not gcs_path.startswith("gs://"):
            raise ValueError(f"Cannot resolve to GCS path: {urn_or_uri}")
        _, _, rest = gcs_path.partition("gs://")
        bucket_name, _, blob_name = rest.partition("/")
        client = storage.Client()
        b = client.bucket(bucket_name)
        blob = b.blob(blob_name)
        return json.loads(blob.download_as_bytes())

    def resolve_pdf_uri(self, urn: str) -> str | None:
        """Return the canonical GCS URI of the source PDF for an annotation URN."""
        return to_pdf_gcs_path(urn, bucket=self.pdf_bucket)

    # ------------------------------------------------------------------ write
    def re_anchor(
        self,
        annotation: dict | str,
        *,
        exact: str,
        prefix: str = "",
        suffix: str = "",
        page: int | None = None,
        new_concept_id: str | None = None,
    ) -> dict:
        """Re-anchor an annotation with a fresh TextQuoteSelector. Removes
        review-needed tag automatically.

        ``annotation`` may be the dict yielded by ``review_queue()`` or a raw
        hypothesis_id string.
        """
        h_id = annotation["hypothesis_id"] if isinstance(annotation, dict) else annotation
        urn = annotation.get("uri") if isinstance(annotation, dict) else None
        pdf_uri = self.resolve_pdf_uri(urn) if urn and urn.startswith("urn:noter:") else None
        return _h.re_anchor(
            h_id,
            api_token=self.api_token,
            exact=exact,
            prefix=prefix,
            suffix=suffix,
            page=page,
            pdf_uri=pdf_uri,
            new_concept_id=new_concept_id,
        )

    def reclassify(
        self, annotation: dict | str, *, new_concept_id: str, rationale: str = ""
    ) -> dict:
        """Change the concept_id tag of an annotation. Adds review-wrong-concept
        tag and the analyst rationale to the text body."""
        h_id = annotation["hypothesis_id"] if isinstance(annotation, dict) else annotation
        existing = self.fetch_one(h_id)
        tags = [t for t in (existing.get("tags") or []) if not t.startswith("concept:")]
        tags.append(f"concept:{new_concept_id}")
        if _h.WRONG_CONCEPT_TAG not in tags:
            tags.append(_h.WRONG_CONCEPT_TAG)
        new_text = (existing.get("text") or "") + (
            f"\n\n[reclassified by analyst] -> {new_concept_id}"
            + (f"\nRationale: {rationale}" if rationale else "")
        )
        return _h.update_hypothesis(h_id, api_token=self.api_token, tags=tags, text=new_text)

    def propose_concept(
        self,
        annotation: dict | str,
        *,
        new_concept_id: str,
        rationale: str,
        paragraph_citation: str = "",
    ) -> dict:
        """Tag an annotation with proposed-concept and stash the proposed id +
        rationale + (optional) regnskapsloven/NRS citation into the body.
        Output of the proposed-concepts queue feeds the taxonomy maintainer."""
        h_id = annotation["hypothesis_id"] if isinstance(annotation, dict) else annotation
        existing = self.fetch_one(h_id)
        tags = list(existing.get("tags") or [])
        if _h.PROPOSED_CONCEPT_TAG not in tags:
            tags.append(_h.PROPOSED_CONCEPT_TAG)
        if not any(t.startswith("proposed:") for t in tags):
            tags.append(f"proposed:{new_concept_id}")
        new_text = (existing.get("text") or "") + (
            f"\n\n[proposed-concept] {new_concept_id}"
            + (f"\nRationale: {rationale}" if rationale else "")
            + (f"\nCitation: {paragraph_citation}" if paragraph_citation else "")
        )
        return _h.update_hypothesis(h_id, api_token=self.api_token, tags=tags, text=new_text)

    def delete(self, annotation: dict | str) -> bool:
        """Delete an annotation. Use when the LLM determines it was spurious."""
        h_id = annotation["hypothesis_id"] if isinstance(annotation, dict) else annotation
        return _h.delete_hypothesis(h_id, api_token=self.api_token)

    # ------------------------------------------------------------------ utility
    def fetch_one(self, hypothesis_id: str) -> dict:
        """Fetch a single annotation by ID."""
        import requests

        r = requests.get(
            f"{_h.API_BASE}/annotations/{hypothesis_id}",
            headers=_h._headers(self.api_token),
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def post_observations(
        self,
        annotations_df: pd.DataFrame,
        *,
        target_type_filter: str = "text",
    ) -> pd.DataFrame:
        """Push a build_annotations()-shaped DataFrame to the group, with URN
        URIs derived from the annotation_id metadata. Use when seeding a new
        (orgnr, year) into the analyst loop."""

        def url_template(row: pd.Series) -> str:
            ann_id = row.get("annotation_id", "")
            urn_hint = row.get("source") or ""
            if urn_hint.startswith("urn:noter:"):
                return urn_hint
            return urn_hint or f"urn:noter:unknown:{ann_id}"

        return _h.to_hypothesis(
            annotations_df,
            group_id=self.group_id,
            api_token=self.api_token,
            source_url_template=url_template,
            target_type_filter=target_type_filter,
        )


# ---------------------------------------------------------------------------
# Convenience: build a build_annotations() DataFrame already URN-tagged
# ---------------------------------------------------------------------------


def build_annotations_with_urn(
    raw_json: dict,
    observations: pd.DataFrame,
    *,
    pipeline_version: str = "noter-extraction-2025",
) -> pd.DataFrame:
    """Wrapper around build_annotations that sets source_text_uri to the URN
    instead of a gs:// path. Lets to_hypothesis use the URN directly."""
    from regnskapnoter.annotations import build_annotations

    orgnr = str(raw_json.get("orgnr") or "")
    year = raw_json.get("year")
    urn = to_urn(orgnr, year) if orgnr and year else None
    return build_annotations(
        raw_json,
        observations,
        source_text_uri=urn,
        source_pdf_uri=urn,  # Hypothes.is treats this only as the target source string
        pipeline_version=pipeline_version,
    )
