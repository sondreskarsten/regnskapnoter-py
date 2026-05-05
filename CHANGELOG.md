# Changelog

## [0.4.0] - 2026-05-05

### Added
- `regnskapnoter.urn` module: `to_urn`, `parse_urn`, `to_gcs_path`, `to_pdf_gcs_path` for the canonical `urn:noter:{orgnr}:{year}` annotation target scheme.
- `regnskapnoter.analyst.AnalystSession`: stateful LLM-analyst-loop session with `review_queue()`, `proposed_concepts_queue()`, `resolve_raw()`, `resolve_pdf_uri()`, `re_anchor()`, `reclassify()`, `propose_concept()`, `delete()`, `post_observations()`.
- `regnskapnoter.analyst.build_annotations_with_urn()` wrapper that sets `source_text_uri` to the URN.
- `regnskapnoter.hypothesis.update_hypothesis()`, `delete_hypothesis()`, `re_anchor()`, `iter_review_queue()` low-level primitives.
- `rn` CLI: `rn push`, `rn pull`, `rn stats` subcommands.
- `docs/llm-analyst-loop.md`: comprehensive end-to-end guide for the LLM-driven loop.

### Tests
- 39 passing (URN: 6, analyst: 6, hypothesis: 4, annotations: 7, frameworks/loader/canonicalize: 16).

## [0.3.0] - 2026-05-05

### Added
- WADM PDF FragmentSelector emission when raw JSON has `[[p:N]]` markers or `note.page_start`.
- `regnskapnoter.hypothesis` module: `to_hypothesis`, `from_hypothesis`, `proposed_concepts`, `review_queue` for analyst review integration.

## [0.2.0] - 2026-05-05

### Added
- WADM annotation producer (`build_annotations`, `annotations_to_jsonld`, `coverage_report`).

## [0.1.0] - 2026-05-05

Initial release. Concept-keyed access to the regnskapnoter-taxonomy with framework grouping primitives.
