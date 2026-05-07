"""GCS-backed append-only annotation store.

Replaces the previous Hypothes.is integration. Naive empiricism: every action is
a new immutable row at its own timestamp; the current state of any annotation is
composed at query time from its history.

Layout:

    gs://{bucket}/{prefix}/{orgnr}/{year}/events.parquet

Each event row has these columns (one row per action):

    event_id           sha256(annotation_id|seq) — globally unique
    annotation_id      stable across mutations (sha of orgnr|year|concept|value|note)
    sequence           monotonic per annotation_id (0 = post, 1+ = mutations)
    event_type         'post' | 're-anchor' | 'reclassify' | 'propose-concept' | 'delete'
    orgnr              str
    year               int
    concept_id         str  (current after this event)
    value              str
    note_number        str
    note_title         str
    page               int  | null
    selector_json      str  (current TextQuoteSelector + optional FragmentSelector)
    target_type        'text' | 'pdf'
    source             URN-style: 'noter:{orgnr}:{year}'
    match_status       'matched' | 'unmatched' | 'reviewed' | 'deleted'
    rationale          str  (LLM rationale on mutation)
    citation           str  (regnskapsloven/NRS citation for propose-concept)
    confidence         float
    creator            'noter-extraction-2025' | 'llm-analyst-{model}' | ...
    taxonomy_version   taxonomy version used when producing this event (e.g. 'v1.1.0')
    created            ISO 8601 UTC

Current state of an annotation = the LATEST row for its annotation_id where
event_type != 'delete'. Helpers below compose this view; consumers can also do
it directly with DuckDB.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

DEFAULT_BUCKET = "sondre_brreg_data"
DEFAULT_PREFIX = "annotations/noter"

EVENT_SCHEMA = pa.schema(
    [
        ("event_id", pa.string()),
        ("annotation_id", pa.string()),
        ("sequence", pa.int32()),
        ("event_type", pa.string()),
        ("orgnr", pa.string()),
        ("year", pa.int32()),
        ("concept_id", pa.string()),
        ("value", pa.string()),
        ("note_number", pa.string()),
        ("note_title", pa.string()),
        ("page", pa.int32()),
        ("selector_json", pa.string()),
        ("target_type", pa.string()),
        ("source", pa.string()),
        ("match_status", pa.string()),
        ("rationale", pa.string()),
        ("citation", pa.string()),
        ("confidence", pa.float64()),
        ("creator", pa.string()),
        ("taxonomy_version", pa.string()),
        ("created", pa.timestamp("us", tz="UTC")),
    ]
)

EVENT_COLUMNS = [f.name for f in EVENT_SCHEMA]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event_id(annotation_id: str, sequence: int) -> str:
    return hashlib.sha256(f"{annotation_id}|{sequence}".encode()).hexdigest()[:16]


@dataclass
class GCSAnnotationStore:
    """Append-only annotation store backed by GCS parquet files.

    Per-(orgnr, year) shard for cheap reads and clean isolation. Append by
    reading the existing shard, concatenating, and re-uploading. Deduplication
    is by event_id; re-running a push is a no-op.
    """

    bucket: str = DEFAULT_BUCKET
    prefix: str = DEFAULT_PREFIX

    def _shard_path(self, orgnr: str, year: int) -> str:
        return f"gs://{self.bucket}/{self.prefix}/{orgnr}/{year}/events.parquet"

    def _gcs_blob(self, orgnr: str, year: int):
        from google.cloud import storage

        client = storage.Client()
        b = client.bucket(self.bucket)
        return b.blob(f"{self.prefix}/{orgnr}/{year}/events.parquet")

    # ------------------------------------------------------------------ read
    def read_shard(self, orgnr: str, year: int) -> pd.DataFrame:
        """Return the full event log for one (orgnr, year). Empty if no shard."""
        blob = self._gcs_blob(orgnr, year)
        if not blob.exists():
            return pd.DataFrame(columns=EVENT_COLUMNS)
        data = blob.download_as_bytes()
        return pq.read_table(io.BytesIO(data)).to_pandas()

    def read_many(self, orgnr_year_pairs: Iterable[tuple[str, int]]) -> pd.DataFrame:
        """Read events from multiple shards and concatenate."""
        frames = [self.read_shard(o, y) for o, y in orgnr_year_pairs]
        return (
            pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=EVENT_COLUMNS)
        )

    def list_shards(self) -> list[tuple[str, int]]:
        """Enumerate every (orgnr, year) shard present in the store."""
        from google.cloud import storage

        client = storage.Client()
        out: list[tuple[str, int]] = []
        for blob in client.list_blobs(self.bucket, prefix=f"{self.prefix}/"):
            if not blob.name.endswith("/events.parquet"):
                continue
            parts = blob.name.split("/")
            if len(parts) < 4:
                continue
            try:
                orgnr = parts[-3]
                year = int(parts[-2])
                out.append((orgnr, year))
            except (ValueError, IndexError):
                continue
        return sorted(out)

    # ------------------------------------------------------------------ append
    def append_events(self, events: pd.DataFrame) -> int:
        """Append events to their shards. Returns the number of new rows written.

        Idempotent: events whose ``event_id`` already exists in the shard are
        skipped. Each call writes one shard at most per (orgnr, year) group.
        """
        if events.empty:
            return 0

        added_total = 0
        for (orgnr, year), group in events.groupby(["orgnr", "year"], sort=False):
            existing = self.read_shard(str(orgnr), int(year))
            if not existing.empty:
                seen = set(existing["event_id"])
                new = group[~group["event_id"].isin(seen)]
            else:
                new = group
            if new.empty:
                continue
            combined = pd.concat([existing, new], ignore_index=True)
            self._write_shard(str(orgnr), int(year), combined)
            added_total += len(new)
        return added_total

    def _write_shard(self, orgnr: str, year: int, df: pd.DataFrame) -> None:
        # Coerce schema dtypes to match EVENT_SCHEMA
        for col in EVENT_COLUMNS:
            if col not in df.columns:
                df[col] = None
        df = df[EVENT_COLUMNS].copy()
        df["created"] = pd.to_datetime(df["created"], utc=True)
        df["sequence"] = pd.to_numeric(df["sequence"], errors="coerce").astype("Int32")
        df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int32")
        df["page"] = pd.to_numeric(df["page"], errors="coerce").astype("Int32")
        df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce")
        table = pa.Table.from_pandas(df, schema=EVENT_SCHEMA, preserve_index=False)
        buf = io.BytesIO()
        pq.write_table(table, buf, compression="zstd")
        buf.seek(0)
        self._gcs_blob(orgnr, year).upload_from_file(buf, content_type="application/octet-stream")

    # ------------------------------------------------------------------ compose current state
    def current_state(self, orgnr: str, year: int) -> pd.DataFrame:
        """Compose the current state per annotation_id from the event log.

        Latest row per annotation_id wins. Annotations whose latest event is
        ``delete`` are excluded.
        """
        events = self.read_shard(orgnr, year)
        if events.empty:
            return events
        events = events.sort_values(["annotation_id", "sequence"])
        latest = events.groupby("annotation_id", as_index=False).tail(1)
        return latest[latest["event_type"] != "delete"].reset_index(drop=True)

    def review_queue(self, orgnr: str, year: int) -> pd.DataFrame:
        """Annotations whose current state is ``unmatched``."""
        cur = self.current_state(orgnr, year)
        if cur.empty:
            return cur
        return cur[cur["match_status"] == "unmatched"].reset_index(drop=True)

    def proposed_concepts(
        self, orgnr_year_pairs: Iterable[tuple[str, int]] | None = None
    ) -> pd.DataFrame:
        """Return all events of type 'propose-concept' across selected shards.

        If ``orgnr_year_pairs`` is None, scans all shards.
        """
        pairs = list(orgnr_year_pairs) if orgnr_year_pairs else self.list_shards()
        events = self.read_many(pairs)
        if events.empty:
            return events
        return events[events["event_type"] == "propose-concept"].reset_index(drop=True)

    # ------------------------------------------------------------------ stats
    def stats(self, orgnr: str, year: int) -> dict[str, Any]:
        events = self.read_shard(orgnr, year)
        cur = self.current_state(orgnr, year)
        return {
            "events_total": len(events),
            "annotations_active": len(cur),
            "annotations_matched": int((cur["match_status"] == "matched").sum())
            if not cur.empty
            else 0,
            "annotations_unmatched": int((cur["match_status"] == "unmatched").sum())
            if not cur.empty
            else 0,
            "annotations_reviewed": int((cur["match_status"] == "reviewed").sum())
            if not cur.empty
            else 0,
            "events_by_type": events["event_type"].value_counts().to_dict()
            if not events.empty
            else {},
            "concepts_unique": int(cur["concept_id"].nunique()) if not cur.empty else 0,
        }


# ---------------------------------------------------------------------------
# Helpers to build event rows
# ---------------------------------------------------------------------------


def annotations_to_post_events(
    annotations: pd.DataFrame,
    *,
    orgnr: str,
    year: int,
    creator: str = "noter-extraction-2025",
    taxonomy_version: str | None = None,
) -> pd.DataFrame:
    """Convert an output of build_annotations() into seq=0 'post' events."""
    if annotations.empty:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    now = _now()
    rows = []
    for r in annotations.itertuples(index=False):
        ann_id = r.annotation_id
        seq = 0
        rows.append(
            {
                "event_id": _event_id(ann_id, seq),
                "annotation_id": ann_id,
                "sequence": seq,
                "event_type": "post",
                "orgnr": str(orgnr),
                "year": int(year),
                "concept_id": r.body_concept_id,
                "value": str(r.body_value),
                "note_number": getattr(r, "note_number", "") or "",
                "note_title": getattr(r, "note_title", "") or "",
                "page": int(r.page)
                if getattr(r, "page", None) is not None and not pd.isna(r.page)
                else None,
                "selector_json": getattr(r, "selector_json", "") or "",
                "target_type": r.target_type,
                "source": f"noter:{orgnr}:{year}",
                "match_status": r.match_status,
                "rationale": "",
                "citation": "",
                "confidence": None,
                "creator": creator,
                "taxonomy_version": taxonomy_version,
                "created": now,
            }
        )
    return pd.DataFrame(rows)


def make_mutation_event(
    *,
    base: pd.Series,
    event_type: str,
    sequence: int,
    new_concept_id: str | None = None,
    new_selector_json: str | None = None,
    new_page: int | None = None,
    new_match_status: str | None = None,
    rationale: str = "",
    citation: str = "",
    confidence: float | None = None,
    creator: str = "llm-analyst",
    taxonomy_version: str | None = None,
) -> dict[str, Any]:
    """Construct a mutation event row from a current-state base row."""
    annotation_id = base["annotation_id"]
    return {
        "event_id": _event_id(annotation_id, sequence),
        "annotation_id": annotation_id,
        "sequence": sequence,
        "event_type": event_type,
        "orgnr": base["orgnr"],
        "year": int(base["year"]),
        "concept_id": new_concept_id if new_concept_id is not None else base["concept_id"],
        "value": base["value"],
        "note_number": base.get("note_number", ""),
        "note_title": base.get("note_title", ""),
        "page": new_page if new_page is not None else base.get("page"),
        "selector_json": new_selector_json
        if new_selector_json is not None
        else base.get("selector_json", ""),
        "target_type": base.get("target_type", "text"),
        "source": base.get("source", ""),
        "match_status": new_match_status
        if new_match_status is not None
        else base.get("match_status", "matched"),
        "rationale": rationale,
        "citation": citation,
        "confidence": confidence,
        "creator": creator,
        "taxonomy_version": taxonomy_version,
        "created": _now(),
    }


def next_sequence(events: pd.DataFrame, annotation_id: str) -> int:
    """Return the next sequence number for an annotation given the existing events."""
    if events.empty:
        return 0
    rows = events[events["annotation_id"] == annotation_id]
    if rows.empty:
        return 0
    return int(rows["sequence"].max()) + 1
