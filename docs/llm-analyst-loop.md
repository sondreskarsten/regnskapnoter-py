# LLM analyst loop — GCS-backed annotation store

End-to-end annotation review pipeline driven by an LLM analyst. Hypothes.is has been removed; state is now an append-only event log in GCS parquet. Naive empiricism is preserved: every action is an immutable observation at its own timestamp; the current state of any annotation is composed at query time from the latest non-delete event.

## Architecture

```
        ┌─────────────────────────────────────────┐
        │   noter-extraction (Cloud Run)          │
        │   PDF → raw JSON with [[p:N]] markers   │
        │   gs://sondre_brreg_data/raw/.../*.json │
        └────────────────┬────────────────────────┘
                         │
                         ▼
            ┌──────────────────────────────────────┐
            │   noter-extraction-tidy-tables       │
            │   raw JSON → wide build_tables CSVs  │
            │   gs://...structured/{table}/{...}   │
            └────────────────┬─────────────────────┘
                             │
                             ▼
            ┌────────────────────────────────────────┐
            │   regnskapnoter.canonicalize           │
            │   wide CSV → long (concept_id, value)  │
            └────────────────┬───────────────────────┘
                             │
                             ▼
            ┌────────────────────────────────────────┐
            │   regnskapnoter.build_annotations_with_urn │
            │   long observations + raw JSON →       │
            │     WADM annotations DataFrame         │
            │     source = urn:noter:{orgnr}:{year}  │
            └────────────────┬───────────────────────┘
                             │
                             ▼
            ┌─────────────────────────────────────┐
            │   AnalystSession.post_observations  │
            │   → seq=0 'post' events appended    │
            │   to gs://sondre_brreg_data/        │
            │      annotations/noter/{org}/{yr}/  │
            │      events.parquet                 │
            └────────────────┬────────────────────┘
                             │
                             │  ┌── LLM analyst loop ──┐
                             ▼  │                       │
            ┌─────────────────────────────────────┐    │
            │   AnalystSession.review_queue()     │    │
            │   yields current-state rows where   │    │
            │   match_status = 'unmatched'        │    │
            └────────────────┬────────────────────┘    │
                             │                         │
                             ▼                         │
            ┌─────────────────────────────────────┐    │
            │   session.resolve_raw(source)       │    │
            │   urn → gs:// → JSON content        │    │
            │   session.get_pdf_bytes(source)     │    │
            └────────────────┬────────────────────┘    │
                             │                         │
                             ▼                         │
            ┌─────────────────────────────────────┐    │
            │   LLM (Gemini 2.5 Flash) decides    │    │
            └────────────────┬────────────────────┘    │
                             │                         │
                             ▼                         │
            ┌─────────────────────────────────────┐    │
            │   session.{re_anchor, reclassify,   │    │
            │            propose_concept, delete} │    │
            │   → seq=N+1 mutation event appended │    │
            └────────────────┬────────────────────┘    │
                             │                         │
                             └─────────────────────────┘
                             │
                             ▼
            ┌─────────────────────────────────────┐
            │   Taxonomy maintainer pulls         │
            │   AnalystSession.proposed_concepts()│
            │   → adds new concepts to taxonomy   │
            └─────────────────────────────────────┘
```

## Event schema

Every shard `gs://{bucket}/{prefix}/{orgnr}/{year}/events.parquet` is append-only with this schema:

| column | type | description |
|---|---|---|
| `event_id` | string | sha256(annotation_id\|seq), primary key |
| `annotation_id` | string | stable across mutations |
| `sequence` | int32 | 0 = post, 1+ = mutations (monotonic per annotation_id) |
| `event_type` | string | `post` / `re-anchor` / `reclassify` / `propose-concept` / `delete` |
| `orgnr` | string | 9-digit |
| `year` | int32 | fiscal year |
| `concept_id` | string | regnskap-no concept (current after this event) |
| `value` | string | observed value |
| `note_number` | string | from raw extraction |
| `note_title` | string | from raw extraction |
| `page` | int32 | 1-indexed PDF page |
| `selector_json` | string | TextQuoteSelector and/or FragmentSelector |
| `target_type` | string | `text` / `pdf` |
| `source` | string | `noter:{orgnr}:{year}` |
| `match_status` | string | `matched` / `unmatched` / `reviewed` / `deleted` |
| `rationale` | string | LLM reason (mutations only) |
| `citation` | string | regnskapsloven/NRS citation (propose-concept only) |
| `confidence` | float64 | LLM confidence 0..1 |
| `creator` | string | `noter-extraction-2025` / `llm-analyst-{model}` / ... |
| `created` | timestamp[us, UTC] | event timestamp |

**Idempotency:** `event_id` is the dedup key. Re-running `post_observations` is a no-op.

**Naive empiricism:** the seq=0 'post' row is never modified. Mutations append seq=1, 2, … rows. To get the analyst-revised view, query: latest row per `annotation_id` where `event_type != 'delete'`. To get the original auto-extraction view, query rows where `sequence = 0`.

## Setup

```bash
pip install 'regnskapnoter[gcs,llm]'
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
export GCP_PROJECT=sondreskarsten-d7d14
export GCP_LOCATION=europe-west1
```

No Hypothes.is account or token needed. The default GCS bucket and prefix are `sondre_brreg_data` / `annotations/noter`; override via `AnalystSession(bucket=..., prefix=...)`.

## Push annotations

```bash
rn push --orgnr 811722332 --year 2024
# raw: 10 notes; observations: 152
# build_annotations: {"total": 194, "matched": 184, "unmatched": 10, "match_rate": 0.95}
# events_written: 102
```

Or programmatically:

```python
import regnskapnoter as rn
from regnskapnoter.cli import _load_raw_and_observations

raw_json, observations = _load_raw_and_observations("811722332", 2024)
annotations = rn.build_annotations_with_urn(raw_json, observations)

session = rn.AnalystSession()
session.post_observations(annotations, orgnr="811722332", year=2024)
```

## LLM analyst loop

```python
import regnskapnoter as rn

session = rn.AnalystSession()

for ann in session.review_queue(orgnr="811722332", year=2024):
    raw = session.resolve_raw(ann["source"])         # urn → GCS → JSON
    pdf = session.get_pdf_bytes(ann["source"])       # bytes for inline LLM input

    decision = llm_decide(ann, raw, pdf)             # see examples/llm_analyst.py

    if decision["action"] == "re-anchor":
        session.re_anchor(
            ann,
            exact=decision["exact"],
            prefix=decision["prefix"],
            suffix=decision["suffix"],
            page=decision.get("page"),
            rationale=decision["rationale"],
            confidence=decision["confidence"],
        )
    elif decision["action"] == "reclassify":
        session.reclassify(
            ann,
            new_concept_id=decision["new_concept_id"],
            rationale=decision["rationale"],
            confidence=decision["confidence"],
        )
    elif decision["action"] == "propose-concept":
        session.propose_concept(
            ann,
            new_concept_id=decision["proposed_concept_id"],
            rationale=decision["rationale"],
            paragraph_citation=decision.get("citation", ""),
            confidence=decision["confidence"],
        )
    elif decision["action"] == "delete":
        session.delete(ann, rationale=decision["rationale"], confidence=decision["confidence"])
```

## Reference Gemini analyst

`examples/llm_analyst.py` — Gemini 2.5 Flash via Vertex AI (`europe-west1`), `thinking_budget=0`, `temperature=0.0`, `response_mime_type=application/json`. Per-shard PDF + raw JSON caching. Confidence-gated dispatch (`MIN_CONFIDENCE=0.6` env).

```bash
python examples/llm_analyst.py --orgnr 811722332 --year 2024 --max 50 --dry-run
python examples/llm_analyst.py --orgnr 811722332 --year 2024 --max 50
python examples/llm_analyst.py --all --max 100   # iterate every shard with unmatched
```

## Pulling analyst contributions

```bash
rn proposed --format jsonl > proposed-concepts-$(date +%F).jsonl
```

```python
session = rn.AnalystSession()
proposed = session.proposed_concepts()  # all shards
# columns: event_id, annotation_id, concept_id, value, citation, rationale,
#          orgnr, year, created, ...
```

## Stats

```bash
rn stats --orgnr 811722332 --year 2024
# {
#   "events_total": 105,
#   "annotations_active": 73,
#   "annotations_matched": 68,
#   "annotations_unmatched": 3,
#   "annotations_reviewed": 2,
#   "events_by_type": {"post": 102, "re-anchor": 1, "reclassify": 1, "delete": 1},
#   "concepts_unique": 44
# }
```

## Live validation

The pipeline has been validated end-to-end against orgnr 811722332 / 2024:

- 152 observations canonicalized from 15 build_tables CSVs
- 194 annotations built (95% match rate)
- 102 text-target events posted (text-only filter)
- Idempotency: second push wrote 0 events ✓
- Re-anchor → seq=1 event appended; seq=0 'post' row bit-identical ✓
- Reclassify → seq=1 event with new concept_id ✓
- Delete → removed from current state; full history retained ✓
- All shards enumerable via `list_shards()` ✓

Validation script + log: `gs://sondre_brreg_data/raw/regnskapnoter_validation/v0_6_0_validation.{py,txt}`
