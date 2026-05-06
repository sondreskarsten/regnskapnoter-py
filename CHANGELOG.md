# Changelog

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
