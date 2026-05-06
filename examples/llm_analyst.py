"""LLM analyst loop driven by Gemini 2.5 Flash via Vertex AI.

Iterates the GCS-backed review queue for one (orgnr, year), feeds the full
source PDF + raw JSON + the unanchored (concept_id, value) to the LLM, parses
the structured decision, dispatches to the appropriate AnalystSession method.

State is persisted as immutable events in
gs://sondre_brreg_data/annotations/noter/{orgnr}/{year}/events.parquet —
every action appends a new row; the original 'post' event is never modified.

Usage:

    # Process a specific filing
    python examples/llm_analyst.py --orgnr 811722332 --year 2024 --max 50

    # Dry-run: print decisions, don't append events
    python examples/llm_analyst.py --orgnr 811722332 --year 2024 --max 5 --dry-run

    # Process all shards needing review
    python examples/llm_analyst.py --all

Environment:

    GOOGLE_APPLICATION_CREDENTIALS - service account JSON for Vertex AI + GCS
    GCP_PROJECT        - default: sondreskarsten-d7d14
    GCP_LOCATION       - default: europe-west1
    ANALYST_MODEL      - default: gemini-2.5-flash
    MIN_CONFIDENCE     - default: 0.6
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from google import genai
from google.genai import types

import regnskapnoter as rn
from regnskapnoter.taxonomy_context import format_context_block, load_concept_contexts

LOG = logging.getLogger("rn.analyst")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PROJECT = os.environ.get("GCP_PROJECT", "sondreskarsten-d7d14")
LOCATION = os.environ.get("GCP_LOCATION", "europe-west1")
MODEL = os.environ.get("ANALYST_MODEL", "gemini-2.5-flash")
MIN_CONFIDENCE = float(os.environ.get("MIN_CONFIDENCE", "0.6"))

CLIENT = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)


SYSTEM_PROMPT = """You are an analyst reviewing automatically-extracted Norwegian
financial-statement note values that the extractor failed to anchor to a text span.

For each unanchored value, you receive:
- The full source PDF of the årsregnskap.
- The raw extracted JSON of the noter, including [[p:N]] page-break markers.
- The unanchored observation: a (concept_id, value) tuple from the regnskapnoter
  taxonomy and the framework labels for the concept.
- Taxonomy context: the concept's official definition, its calc-arc parent and
  sibling concepts, and its legal references. Use this to distinguish between
  similar concepts (e.g. Skattekostnad vs BetalbarSkattAaret).

Decide one of four actions. Each action REQUIRES a justification object that
cites specific evidence. This constrains the trajectory: even marginal decisions
must document what grounded them.

(a) re-anchor: the value IS in the document. Provide the literal substring as
    it appears (preserving Norwegian thousands separators like '\\u00a0' or
    space, parentheses for negatives) plus 32 chars of prefix/suffix. Include
    the page number if a [[p:N]] marker precedes it OR if the value is in the
    PDF (e.g. in a primary statement table the noter extractor skipped).

(b) reclassify: the value is in the document but assigned to the wrong
    concept_id. Propose the correct concept_id (must start with 'regnskap-no:').

(c) propose-concept: the value is real, no taxonomy concept fits. Provide a
    CamelCase proposed_concept_id, rationale, and a regnskapsloven (e.g.
    '§ 7-XX (N)') or NRS citation.

(d) delete: the value is spurious — extraction artifact, OCR garbage,
    misattributed scaling factor.

Return ONLY a JSON object matching this schema (no prose, no markdown):

{
  "action": "re-anchor" | "reclassify" | "propose-concept" | "delete",
  "exact": "string",
  "prefix": "string",
  "suffix": "string",
  "page": integer | null,
  "new_concept_id": "regnskap-no:..." | null,
  "proposed_concept_id": "regnskap-no:..." | null,
  "citation": "string",
  "confidence": 0.0 - 1.0,
  "justification": {
    "action_reason": "why this action and not the other three",
    "evidence_location": "where in the PDF or JSON the grounding evidence is",
    "concept_fit": "why this concept_id is correct (or why it's wrong, for reclassify/propose)",
    "risk": "what could be wrong with this decision"
  }
}

JUSTIFICATION RULES:
- The justification object is MANDATORY for all actions. Omitting it = invalid.
- action_reason: must name at least one alternative action considered and why
  it was rejected. E.g. "re-anchor chosen over reclassify because the value
  matches the current concept's definition exactly."
- evidence_location: must cite a page number, note title, or JSON path.
- concept_fit: must reference the taxonomy context provided (definition,
  parent/sibling concepts, or legal reference).
- risk: must state at least one concrete scenario where this decision is wrong.

Use null for fields not relevant to your action. Set confidence below 0.6 if
uncertain; the system will skip low-confidence decisions for human review.
"""


def _format_user_prompt(ann: dict, raw: dict, taxonomy_block: str) -> str:
    """Build the user-turn text describing one annotation needing review."""
    cid = ann.get("concept_id") or ""
    framework_labels = rn.framework_for_concept(cid) if cid else []
    notes = raw.get("notes") or []
    notes_summary = "\n".join(
        f"  Note {n.get('note_number', '?')} '{n.get('title', '')}'"
        f" (pages {n.get('page_start', '?')}-{n.get('page_end', '?')})"
        for n in notes
        if isinstance(n, dict)
    )
    return f"""Unanchored observation:
  concept_id    : {cid}
  value         : {ann.get("value", "")}
  frameworks    : {framework_labels}
  annotation_id : {ann.get("annotation_id", "")}
  source        : {ann.get("source", "")}

{taxonomy_block}

Raw extraction summary:
  orgnr        : {raw.get("orgnr", "")}
  year         : {raw.get("year", "")}
  total_pages  : {raw.get("total_pages", "?")}
  notes:
{notes_summary}

Full raw JSON of noter (note full_text bodies follow, page markers preserved):

```json
{json.dumps({k: v for k, v in raw.items() if k != "_source_path"}, ensure_ascii=False, indent=1)}
```

The complete source PDF is attached. Read the taxonomy context, the raw JSON,
and the PDF. Use the concept definition and sibling concepts to decide whether
the current concept assignment is correct. Emit the JSON decision per the
system prompt schema. The justification object is mandatory.
"""


def _call_gemini(user_text: str, pdf_bytes: bytes) -> dict:
    """Call Gemini with the system prompt + PDF + user-turn observation context."""
    response = CLIENT.models.generate_content(
        model=MODEL,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        inline_data=types.Blob(
                            mime_type="application/pdf",
                            data=pdf_bytes,
                        )
                    ),
                    types.Part(text=user_text),
                ],
            ),
        ],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.0,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            response_mime_type="application/json",
        ),
    )
    return json.loads(response.text)


def _dispatch(session: rn.AnalystSession, ann: dict, decision: dict) -> str:
    """Apply the LLM decision via session methods. Returns an outcome label."""
    action = decision.get("action")
    confidence = float(decision.get("confidence") or 0.0)
    if confidence < MIN_CONFIDENCE:
        return f"skipped_low_confidence({confidence:.2f})"

    justification = decision.get("justification") or {}
    rationale = justification.get("action_reason", decision.get("rationale", ""))
    rationale_full = json.dumps(justification, ensure_ascii=False) if justification else rationale

    if action == "re-anchor":
        session.re_anchor(
            ann,
            exact=decision["exact"],
            prefix=decision.get("prefix", ""),
            suffix=decision.get("suffix", ""),
            page=decision.get("page"),
            rationale=rationale_full,
            confidence=confidence,
        )
        return f"re-anchored(page={decision.get('page')})"

    if action == "reclassify":
        new_cid = decision.get("new_concept_id")
        if not new_cid or not new_cid.startswith("regnskap-no:"):
            return f"invalid_concept_id({new_cid!r})"
        session.reclassify(
            ann,
            new_concept_id=new_cid,
            rationale=rationale_full,
            confidence=confidence,
        )
        return f"reclassified -> {new_cid}"

    if action == "propose-concept":
        proposed = decision.get("proposed_concept_id")
        if not proposed:
            return "missing_proposed_concept_id"
        session.propose_concept(
            ann,
            new_concept_id=proposed,
            rationale=rationale_full,
            paragraph_citation=decision.get("citation", ""),
            confidence=confidence,
        )
        return f"proposed -> {proposed}"

    if action == "delete":
        session.delete(ann, rationale=rationale_full, confidence=confidence)
        return "deleted"

    return f"unknown_action({action!r})"


def _process_one_filing(
    session: rn.AnalystSession, orgnr: str, year: int, max_anns: int, dry_run: bool
) -> dict:
    """Process unmatched annotations for one filing. Returns outcome counts."""
    raw = None
    pdf_bytes = None
    taxonomy_block = None
    outcomes: dict[str, int] = {}
    processed = 0

    for ann in session.review_queue(orgnr=orgnr, year=year):
        if processed >= max_anns:
            break
        try:
            if raw is None:
                raw = session.resolve_raw(ann["source"])
            if pdf_bytes is None:
                pdf_bytes = session.get_pdf_bytes(ann["source"])
        except Exception as e:
            LOG.warning("resolve_failed orgnr=%s year=%s err=%s", orgnr, year, e)
            return {"resolve_failed": 1}

        if taxonomy_block is None:
            cid = ann.get("concept_id", "")
            queue_cids = [cid] if cid else []
            try:
                all_anns = list(session.review_queue(orgnr=orgnr, year=year))
                queue_cids = list(
                    {a.get("concept_id", "") for a in all_anns if a.get("concept_id")}
                )
            except Exception:
                pass
            if queue_cids:
                try:
                    contexts = load_concept_contexts(queue_cids)
                    taxonomy_block = format_context_block(contexts)
                except Exception as e:
                    LOG.warning("taxonomy_context_load_failed: %s", e)
                    taxonomy_block = "<taxonomy_context>\n(unavailable)\n</taxonomy_context>"
            else:
                taxonomy_block = "<taxonomy_context>\n(no concept_id)\n</taxonomy_context>"

        user_prompt = _format_user_prompt(ann, raw, taxonomy_block)
        try:
            decision = _call_gemini(user_prompt, pdf_bytes)
        except Exception as e:
            LOG.error("llm_call_failed ann_id=%s err=%s", ann.get("annotation_id"), e)
            outcomes["llm_call_failed"] = outcomes.get("llm_call_failed", 0) + 1
            processed += 1
            continue

        if dry_run:
            print(
                json.dumps(
                    {
                        "annotation_id": ann.get("annotation_id"),
                        "concept_id": ann.get("concept_id"),
                        "value": ann.get("value"),
                        "decision": decision,
                    },
                    ensure_ascii=False,
                )
            )
            outcomes["dry_run"] = outcomes.get("dry_run", 0) + 1
        else:
            try:
                outcome = _dispatch(session, ann, decision)
            except Exception as e:
                outcome = f"dispatch_error:{type(e).__name__}"
                LOG.error("dispatch_failed ann_id=%s err=%s", ann.get("annotation_id"), e)
            LOG.info(
                "ann_id=%s concept=%s value=%s -> %s",
                ann.get("annotation_id"),
                ann.get("concept_id"),
                ann.get("value"),
                outcome,
            )
            outcomes[outcome.split("(")[0].strip()] = (
                outcomes.get(outcome.split("(")[0].strip(), 0) + 1
            )

        processed += 1
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM analyst loop")
    parser.add_argument("--orgnr", help="Process a single (orgnr, year) shard")
    parser.add_argument("--year", type=int)
    parser.add_argument(
        "--all", action="store_true", help="Iterate every shard with unmatched annotations"
    )
    parser.add_argument("--max", type=int, default=50, help="Max annotations to process per shard")
    parser.add_argument("--dry-run", action="store_true", help="Decide but don't append mutations")
    args = parser.parse_args(argv)

    session = rn.AnalystSession()
    t0 = time.time()
    total_outcomes: dict[str, int] = {}

    if args.all:
        shards = session.store.list_shards()
        LOG.info("scanning %d shards", len(shards))
        for orgnr, year in shards:
            review = session.store.review_queue(orgnr, year)
            if review.empty:
                continue
            LOG.info("processing orgnr=%s year=%s n_unmatched=%d", orgnr, year, len(review))
            o = _process_one_filing(session, orgnr, year, args.max, args.dry_run)
            for k, v in o.items():
                total_outcomes[k] = total_outcomes.get(k, 0) + v
    else:
        if not (args.orgnr and args.year):
            LOG.error("either --all or both --orgnr and --year required")
            return 2
        total_outcomes = _process_one_filing(
            session,
            args.orgnr,
            args.year,
            args.max,
            args.dry_run,
        )

    elapsed = time.time() - t0
    LOG.info("elapsed=%.1fs outcomes=%s", elapsed, total_outcomes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
