# Changelog

## [0.7.0] - 2026-05-06 — Universal OCR text input

### Added

- `regnskapnoter.adapters` module: 6 adapters that normalize every regnskap document producer into a common `Document` shape:
  - `from_gemini_json(raw_json)` — current production format with `[[p:N]]` markers
  - `from_text_pages(pages, orgnr=, year=, producer=)` — per-page text (ocrmypdf, tesseract text mode, Cloud Vision text-only)
  - `from_text_blob(text, ...)` — single string with optional `[[p:N]]` markers
  - `from_tesseract_tsv(tsv, orgnr=, year=, min_conf=)` — word-level rows + bounding boxes; accepts string or pre-parsed dict iterable
  - `from_cloud_vision(pages, ...)` — per-page text + per-word bounding boxes
  - `from_docling(doc, ...)` — duck-typed for `DoclingDocument`
  - `from_spans(spans, ...)` — escape hatch for custom shapes
- `Document` and `TextSpan` dataclasses exported at top level. `Document.joined_text()` reconstructs page-marker-tagged text.
- `build_annotations()` now accepts either a Gemini-shaped dict or a `Document`. Backward-compatible.
- **Bounding-box-aware FragmentSelector**: when the producer supplies word-level bboxes (`tesseract_tsv`, `cloud_vision`, `docling`), pdf annotations now emit a refined selector chain:
  ```
  FragmentSelector(value="page=N") →
    refinedBy: FragmentSelector(value="xywh=x,y,w,h", conformsTo=Media Fragments URI 1.0) →
      refinedBy: TextQuoteSelector(exact, prefix, suffix)
  ```
  This is the W3C Media Fragments URI 1.0 spec — viewers like Hypothes.is, Pagedraw, INCEpTION can highlight the exact rectangle on the rendered page.

### Live validation (artifacts at `gs://sondre_brreg_data/raw/regnskapnoter_validation/v0_7_0_ocr_validation.{py,txt}`)

End-to-end validation against real OCR cascade output (`gs://sondre_brreg_data/raw/ocr_bench_11k/`) for orgnr 811722332/2024:

| producer | matched | match rate |
|---|---:|---:|
| gemini-on-pdf (baseline) | 92/102 | 90.2% |
| **tesseract** (full_text) | **91/102** | **89.2%** |
| **easyocr** (full_text) | **89/102** | **87.3%** |

Raw tesseract OCR text — no LLM at extraction time — comes within 1 concept of the Gemini-on-PDF baseline. Every engine in the 7-voter cascade (`ocrmypdf`, `tesseract`, `tesseract_tsv`, `paddleocr`, `doctr`, `easyocr`, `nougat`) is now a usable input.

### Tests

- 16 new adapter tests in `tests/test_adapters.py` covering all 6 input shapes plus `build_annotations` integration through each.
- 69 total passing across 9 test files.

## [0.6.0] - 2026-05-05 — GCS-backed store; Hypothes.is removed

### BREAKING

- Removed `regnskapnoter.hypothesis` module. State now lives in GCS parquet under `gs://{bucket}/{prefix}/{orgnr}/{year}/events.parquet`. No external service dependencies.
- `AnalystSession` constructor now takes `bucket` and `prefix` instead of `group_id` and `api_token`. Method signatures changed: `review_queue(orgnr=, year=)` instead of yielding from a global queue. Mutation methods (`re_anchor`, `reclassify`, `propose_concept`, `delete`) now accept `rationale=` and `confidence=` kwargs that are persisted in the event log.

### Added

- `regnskapnoter.store` module with `GCSAnnotationStore` (append-only event log), `annotations_to_post_events`, `make_mutation_event`, `next_sequence` helpers.
- Naive empiricism preserved: every action is a new immutable row; current state is composed at query time. The original auto-extraction observation (sequence=0) is never modified.
- Idempotent push: re-running `post_observations` writes 0 events when nothing has changed.
- `session.history(orgnr=, year=)` — full immutable event log for one filing.
- `session.fetch_all(orgnr=, year=)` — current state composed from latest non-delete event per annotation.
- `session.stats(orgnr=, year=)` — event-log summary with event_type counts.
- `rn shards` and `rn proposed` CLI subcommands.

### Removed

- All Hypothes.is integration code (`hypothesis.py`, tests, docs). The LLM analyst doesn't need a third-party annotation service.
- `urn` module retained as the source-string scheme.

### Validated

- Live end-to-end roundtrip against orgnr 811722332 / 2024:
  - 152 observations canonicalized from 15 build_tables CSVs
  - 194 annotations built (95% match)
  - 102 events posted; second push wrote 0 (idempotency)
  - Re-anchor → seq=1 event with `FragmentSelector page=1`, seq=0 'post' bit-identical
  - Reclassify → seq=1 event with new concept_id
  - Delete → removed from current_state; both events retained in history
- Validation script + log saved to `gs://sondre_brreg_data/raw/regnskapnoter_validation/v0_6_0_validation.{py,txt}`

### Tests

- 53 tests passing across 8 test files. GCS layer mocked with in-memory blob store; end-to-end tests run real round-trips against the in-memory fake.

## [0.5.0] - 2026-05-05

### Added
- `examples/llm_analyst.py`: Gemini 2.5 Flash via Vertex AI driving the analyst loop.
- `examples/Dockerfile` + `examples/deploy.sh`: Cloud Run Job + Cloud Scheduler.
- `find_pdf_in_gcs`, `AnalystSession.download_pdf`, `AnalystSession.get_pdf_bytes` helpers.

## [0.4.0] - 2026-05-05

### Added
- `regnskapnoter.urn` module, `AnalystSession` (Hypothes.is-backed at this version), `rn` CLI.

## [0.3.0] - 2026-05-05

### Added
- WADM PDF FragmentSelector emission from `[[p:N]]` markers.
- `regnskapnoter.hypothesis` module (removed in v0.6.0).

## [0.2.0] - 2026-05-05

### Added
- WADM annotation producer (`build_annotations`, `annotations_to_jsonld`, `coverage_report`).

## [0.1.0] - 2026-05-05

Initial release.
