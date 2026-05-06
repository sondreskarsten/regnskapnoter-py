# regnskapnoter

Python client for the [regnskapnoter-taxonomy](https://github.com/sondreskarsten/regnskapnoter-taxonomy) — a Norwegian financial-statement-noter concept dictionary covering regnskapsloven §§ 6-1, 6-1a, 6-2, 7-21–7-46, NRS 2/6/8/13/17/21, and NRS(F) Resultatskatt.

## Why this exists

The downstream extraction pipeline groups noter content by ad-hoc tables (`skatt_aaret`, `lonn_ytelser`, `egenkapital_summary`, ...). That grouping is a workaround for what the taxonomy already encodes: every concept has `references[*]` declaring which legal/standard framework it belongs to (`§ 7-29`, `NRS 2`, `NRS Resultatskatt`, ...).

This package replaces table-shape grouping with **framework grouping**, exposing the taxonomy via concept-keyed pandas DataFrames.

## Install

```bash
pip install regnskapnoter
```

## Quick start

```python
import regnskapnoter as rn

rn.concepts()                       # 279 concepts
rn.definitions()                    # verbatim regnskapsloven/NRS prose, one row per concept
rn.list_frameworks()                # ranked: § 7-29, § 7-46, NRS Resultatskatt, ...

rn.concepts_in_framework("§ 7-29")
# ['regnskap-no:AndreForskjeller',
#  'regnskap-no:AnvendelseFremforbartUnderskudd',
#  'regnskap-no:BetalbarSkattAaret', ...]

rn.framework_for_concept("regnskap-no:Lonninger")
# ['§ 7-38', '§ 7-31']
```

## Canonicalize wide build_tables CSVs to concept-keyed long format

```python
import pandas as pd
import regnskapnoter as rn

wide = pd.read_csv("gs://.../structured/skatt_aaret/123456789.csv")
long = rn.canonicalize(wide, table="skatt_aaret")
# orgnr  report_year  value  concept_id
# 123…   2024         100    regnskap-no:BetalbarSkattAaret
# 123…   2024         120    regnskap-no:Skattekostnad
```

The canonical long output replaces the per-table CSV layout: every row is `(orgnr, year, concept_id, value)`, and downstream queries become framework-aware:

```python
skatt_concepts = set(rn.concepts_in_framework("§ 7-29"))
skatt_obs = long[long["concept_id"].isin(skatt_concepts)]
```


## Annotate raw notes with concept IDs (WADM)

Pair structured observations back to the raw text spans they were extracted from:

```python
import regnskapnoter as rn
import json

raw_json = json.loads(open("/path/to/811722332_aarsregnskap_2024_v2.json").read())
observations = rn.canonicalize(skatt_df, table="skatt_aaret")  # ... + other tables

annotations = rn.build_annotations(
    raw_json, observations,
    source_text_uri="gs://sondre_brreg_data/raw/noter_extraction_2025/raw/811722332_aarsregnskap_2024_v2.json",
    source_pdf_uri="gs://brreg-regnskap/811722332_aarsregnskap_2024.pdf",  # optional
)
rn.coverage_report(annotations)
# {'total': 105, 'matched': 102, 'unmatched': 3, 'match_rate': 0.97, 'concepts_unique': 41}
```

Each annotation conforms to the [W3C Web Annotation Data Model](https://www.w3.org/TR/annotation-model/) and pairs:
- **Target**: source URI + `TextQuoteSelector` (`prefix`/`exact`/`suffix`) on the raw note's `full_text`
- **Body**: `concept_id` + observed `value`
- **Refinement**: `note_number`, `note_title`

Two target shapes are emitted:
- **Text** target → the raw JSON file (always emitted)
- **PDF** target → the source PDF, with the `TextQuoteSelector` ready for re-resolution against OCR'd PDF text (emitted when `source_pdf_uri` is provided)

Export to WADM JSON-LD for Hypothes.is / INCEpTION import:

```python
jsonld_payload = rn.annotations_to_jsonld(annotations)
```


## GCS-backed annotation store (LLM analyst loop)

State is an append-only event log in GCS parquet — no third-party services. Every action (post, re-anchor, reclassify, propose-concept, delete) is an immutable observation; the current view is composed at query time from the latest non-delete event per `annotation_id`.

Layout: `gs://{bucket}/{prefix}/{orgnr}/{year}/events.parquet` (default `sondre_brreg_data` / `annotations/noter`).

```python
import regnskapnoter as rn

# Push initial annotations
session = rn.AnalystSession()
session.post_observations(annotations, orgnr="811722332", year=2024)

# Iterate review queue (LLM analyst loop)
for ann in session.review_queue(orgnr="811722332", year=2024):
    raw = session.resolve_raw(ann["source"])
    pdf = session.get_pdf_bytes(ann["source"])

    decision = llm_decide(ann, raw, pdf)
    if decision["action"] == "re-anchor":
        session.re_anchor(ann, exact=..., prefix=..., suffix=..., page=...,
                          rationale=..., confidence=...)
    # ... reclassify, propose_concept, delete

# Pull proposed concepts across all shards
proposed = session.proposed_concepts()
```

See [docs/llm-analyst-loop.md](docs/llm-analyst-loop.md) for the full guide including event schema, naive-empiricism property, and Cloud Run deployment.

## PDF FragmentSelector emission

When the raw JSON contains `[[p:N]]` page markers (added in `noter-extraction` per
[the page-tracking patch](https://github.com/sondreskarsten/noter-extraction/commit/897d759)),
PDF target annotations get a proper `FragmentSelector` with `page=N`:

```json
{
  "type": "FragmentSelector",
  "conformsTo": "http://tools.ietf.org/rfc/rfc3778",
  "value": "page=5",
  "refinedBy": {"type": "TextQuoteSelector", "exact": "1 100", "prefix": "...", "suffix": "..."}
}
```

Falls back to `RangeSelector` when no page metadata is present (legacy raw JSON).


## LLM analyst loop

For an end-to-end annotation review pipeline driven by an LLM analyst — no human UI, no Hypothes.is web rendering — see [docs/llm-analyst-loop.md](docs/llm-analyst-loop.md).

Highlights:

```python
import os
import regnskapnoter as rn

session = rn.AnalystSession(
    group_id=os.environ["HYPOTHESIS_GROUP"],
    api_token=os.environ["HYPOTHESIS_TOKEN"],
)

# Push annotations for one filing
raw_json, observations = rn.cli._load_raw_and_observations("811722332", 2024)
annotations = rn.build_annotations_with_urn(raw_json, observations)
session.post_observations(annotations)

# LLM analyst iterates the review queue
for ann in session.review_queue():
    raw = session.resolve_raw(ann["uri"])  # urn:noter:* -> raw JSON dict
    decision = llm_decide(ann, raw)
    if decision["action"] == "re-anchor":
        session.re_anchor(ann, exact=..., prefix=..., suffix=..., page=...)
    elif decision["action"] == "propose-concept":
        session.propose_concept(ann, new_concept_id=..., rationale=..., paragraph_citation=...)
```

URN scheme used as the Hypothes.is URI:

```
urn:noter:{orgnr}:{year}    # e.g. urn:noter:811722332:2024
```

The LLM analyst calls `rn.to_gcs_path(urn)` to get the canonical raw JSON path or `rn.to_pdf_gcs_path(urn)` for the source PDF.

## CLI

```bash
rn push  --orgnr 811722332 --year 2024     # push annotations for one filing
rn pull  --tag proposed-concept            # pull analyst contributions
rn stats                                    # group-level summary
```

Reads `HYPOTHESIS_TOKEN` and `HYPOTHESIS_GROUP` from the environment.



## Universal OCR text input

The annotation pipeline accepts every producer's output shape via adapters:

```python
import regnskapnoter as rn

# Current Gemini-on-PDF JSON
doc = rn.from_gemini_json(raw_json)

# Plain OCR text per page (ocrmypdf, tesseract text mode, Cloud Vision text-only)
doc = rn.from_text_pages(pages, orgnr="811722332", year=2024, producer="ocrmypdf")

# Single text blob (with optional [[p:N]] markers)
doc = rn.from_text_blob(text, orgnr="811722332", year=2024, producer="tesseract")

# Tesseract TSV — word-level with bounding boxes (cascade voter "tesseract_tsv")
doc = rn.from_tesseract_tsv(tsv_string, orgnr="811722332", year=2024)

# Cloud Vision per-page text + per-word bboxes
doc = rn.from_cloud_vision(pages, orgnr="811722332", year=2024)

# Docling DoclingDocument
doc = rn.from_docling(docling_doc, orgnr="811722332", year=2024)

# build_annotations works on every Document
df = rn.build_annotations(doc, observations, source_pdf_uri="gs://...")
```

When the producer supplies word-level bounding boxes, PDF annotations carry a Media Fragments `xywh=` selector chain — `page=N → xywh=x,y,w,h → TextQuoteSelector` — so viewers can highlight the exact rectangle on the rendered page.

## Version pinning

```python
rn.set_version("v1.0.2")          # pin to a specific taxonomy release
rn.available_versions()           # list all published versions
rn.version()                      # currently active version
```

Default is `latest`. Cached parquet files live under `platformdirs.user_cache_dir("regnskapnoter")`. Call `rn.clear_cache()` to invalidate.

## API surface

| Function | Returns |
|---|---|
| `concepts()` | 279 rows: concept_id, period_type, balance, data_type, status, ... |
| `definitions()` | Verbatim source prose per concept |
| `labels()` | NB + EN labels per concept (628 rows) |
| `references()` | Source citations per concept |
| `mappings()` | Cross-walks (IFRS-Full, norwegian_specific) |
| `calc_arcs()` | Calculation arcs with weights and roles |
| `axes()` / `axis_members()` | 4 dimensional axes, 31 members |
| `frameworks()` | concept_id → framework label (long form) |
| `list_frameworks()` | Distinct frameworks ranked by concept count |
| `concepts_in_framework(label)` | concept_ids for a framework |
| `framework_for_concept(cid)` | framework labels for a concept |
| `build_tables_mapping()` | 230 rows: build_tables column → concept_id |
| `concept_for_column(table, col)` | Single mapping lookup |
| `canonicalize(df, table=...)` | Wide → long concept-keyed pivot |

## Source

Artifacts fetched from `gs://regnskapnoter-taxonomy/{version}/` (publicly readable).

## License

CC-BY-4.0 (matches taxonomy license).
