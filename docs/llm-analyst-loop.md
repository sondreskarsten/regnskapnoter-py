# LLM analyst loop — comprehensive guide

This document specifies the end-to-end LLM-driven annotation review pipeline for regnskap noter. The "analyst" is an LLM (e.g. Claude or Gemini) — there is no human UI in the loop, no Hypothes.is web rendering, no Cloud Run viewer.

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
            │     uri = urn:noter:{orgnr}:{year}     │
            └────────────────┬───────────────────────┘
                             │
                             ▼
            ┌─────────────────────────────────────┐
            │   AnalystSession.post_observations  │
            │   POST → Hypothes.is group          │
            │   target.source = urn:noter:...     │
            │   tags = [concept:..., value:...,   │
            │           note:..., page:...,       │
            │           review-needed?]           │
            └────────────────┬────────────────────┘
                             │
                             │  ┌───────── LLM analyst loop ─────────┐
                             ▼  │                                    │
            ┌─────────────────────────────────────┐                  │
            │   AnalystSession.review_queue()     │                  │
            │   yields {hypothesis_id, uri,       │                  │
            │           concept_id, value, tags}  │                  │
            └────────────────┬────────────────────┘                  │
                             │                                       │
                             ▼                                       │
            ┌─────────────────────────────────────┐                  │
            │   session.resolve_raw(uri)          │                  │
            │   urn → gs:// → JSON content        │                  │
            └────────────────┬────────────────────┘                  │
                             │                                       │
                             ▼                                       │
            ┌─────────────────────────────────────┐                  │
            │   LLM decides:                      │                  │
            │   - re-anchor with new selector     │                  │
            │   - reclassify to different concept │                  │
            │   - propose new concept             │                  │
            │   - delete (spurious)               │                  │
            └────────────────┬────────────────────┘                  │
                             │                                       │
                             ▼                                       │
            ┌─────────────────────────────────────┐                  │
            │   session.{re_anchor, reclassify,   │                  │
            │            propose_concept, delete} │                  │
            │   PATCH → Hypothes.is               │                  │
            └────────────────┬────────────────────┘                  │
                             │                                       │
                             └───────────────────────────────────────┘
                             │
                             ▼
            ┌─────────────────────────────────────┐
            │   Taxonomy maintainer pulls         │
            │   AnalystSession.fetch_all(         │
            │     tag_filter=['proposed-concept'])│
            │   → adds new concepts to taxonomy   │
            └─────────────────────────────────────┘
```

## URI scheme

Hypothes.is requires every annotation to have a URI. Since no human will visit it, we use a stable URN that encodes the (orgnr, year) tuple:

```
urn:noter:{orgnr}:{year}
```

Example: `urn:noter:811722332:2024`

The URN reverses cleanly to a GCS path:

```python
from regnskapnoter import to_urn, parse_urn, to_gcs_path, to_pdf_gcs_path

to_urn("811722332", 2024)
# 'urn:noter:811722332:2024'

parse_urn("urn:noter:811722332:2024")
# ('811722332', 2024)

to_gcs_path("urn:noter:811722332:2024")
# 'gs://sondre_brreg_data/raw/noter_extraction_2025/raw/811722332_aarsregnskap_2024_v2.json'

to_pdf_gcs_path("urn:noter:811722332:2024")
# 'gs://brreg-regnskap/811722332_aarsregnskap_2024.pdf'
```

## Setup

### 1. Get a Hypothes.is API token

Visit https://hypothes.is/account/developer (free account, no review needed) and copy the token.

### 2. Create a private group

Visit https://hypothes.is/groups/new — name it e.g. `regnskap-noter-review`. The URL after creation contains the group ID:

```
https://hypothes.is/groups/{GROUP_ID}/regnskap-noter-review
```

### 3. Set environment variables

```bash
export HYPOTHESIS_TOKEN=<your token>
export HYPOTHESIS_GROUP=<group id>
```

### 4. Push annotations for one (orgnr, year)

```bash
rn push --orgnr 811722332 --year 2024
# stderr: raw: 10 notes; observations: 98
# stderr: build_annotations: {"total": 105, "matched": 102, "unmatched": 3, "match_rate": 0.97}
# stderr: posted: 105
```

Or programmatically:

```python
import os
import regnskapnoter as rn
from regnskapnoter.cli import _load_raw_and_observations

raw_json, observations = _load_raw_and_observations("811722332", 2024)
annotations = rn.build_annotations_with_urn(raw_json, observations)

session = rn.AnalystSession(
    group_id=os.environ["HYPOTHESIS_GROUP"],
    api_token=os.environ["HYPOTHESIS_TOKEN"],
)
session.post_observations(annotations)
```

## LLM analyst loop

```python
import os
import regnskapnoter as rn

session = rn.AnalystSession(
    group_id=os.environ["HYPOTHESIS_GROUP"],
    api_token=os.environ["HYPOTHESIS_TOKEN"],
)

for ann in session.review_queue(batch_size=20):
    raw_json = session.resolve_raw(ann["uri"])
    notes = raw_json.get("notes") or []

    # LLM: read the notes + the unanchored (concept_id, value) and decide
    decision = llm_decide(ann, notes)

    if decision["action"] == "re-anchor":
        session.re_anchor(
            ann,
            exact=decision["exact"],
            prefix=decision["prefix"],
            suffix=decision["suffix"],
            page=decision.get("page"),
        )
    elif decision["action"] == "reclassify":
        session.reclassify(
            ann,
            new_concept_id=decision["new_concept_id"],
            rationale=decision["rationale"],
        )
    elif decision["action"] == "propose-concept":
        session.propose_concept(
            ann,
            new_concept_id=decision["proposed_concept_id"],
            rationale=decision["rationale"],
            paragraph_citation=decision.get("citation", ""),
        )
    elif decision["action"] == "delete":
        session.delete(ann)
```

## LLM prompt template

A reasonable system prompt for the analyst LLM:

```
You are an analyst reviewing automatically-extracted Norwegian financial-statement
note values that the extractor failed to anchor to a text span. For each
unanchored value:

1. Read the raw note text.
2. Find the literal substring in the note where this value appears.
3. Decide one of:
   (a) re-anchor: provide the exact substring + 32 chars of prefix/suffix
       and the page number it falls on (if a [[p:N]] marker precedes it)
   (b) reclassify: the concept_id is wrong; propose the correct one from
       the regnskapnoter taxonomy
   (c) propose-concept: the value is real but no taxonomy concept fits;
       propose a new concept_id with a rationale and a regnskapsloven/NRS citation
   (d) delete: the value is spurious (extraction artifact)

Return JSON:
{
  "action": "re-anchor" | "reclassify" | "propose-concept" | "delete",
  "exact": "...",       // required for re-anchor
  "prefix": "...",      // required for re-anchor
  "suffix": "...",      // required for re-anchor
  "page": 5,            // required for re-anchor when [[p:N]] marker is upstream
  "new_concept_id": "regnskap-no:...",          // for reclassify
  "proposed_concept_id": "regnskap-no:NyttKonsept", // for propose-concept
  "rationale": "...",   // for reclassify or propose-concept
  "citation": "§ 7-XX" or "NRS X kap. Y"        // for propose-concept
}
```

## Pulling analyst contributions

The taxonomy maintainer periodically pulls proposed-concept annotations:

```python
proposed = session.fetch_all(tag_filter=[rn.PROPOSED_CONCEPT_TAG], limit=500)
# Each row has: hypothesis_id, uri, tags, text, regnskapnoter_concept_id,
#               is_proposed_concept (True), created, updated
```

Or via CLI:

```bash
rn pull --tag proposed-concept --format jsonl > proposed-concepts-$(date +%F).jsonl
```

## Stats

```bash
rn stats
# {
#   "total": 1247,
#   "review_needed": 38,
#   "proposed_concept": 6,
#   "wrong_concept": 12,
#   "unique_concepts": 217,
#   "unique_uris": 12
# }
```
