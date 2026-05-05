"""LLM analyst loop driven by Gemini 2.5 Flash via Vertex AI.

Iterates the review queue, feeds the full source PDF + raw JSON + the unanchored
(concept_id, value) to the LLM, parses the structured decision, dispatches to the
appropriate AnalystSession method.

Usage:

    python examples/llm_analyst.py --max 50

Environment:

    HYPOTHESIS_TOKEN   - personal API token from hypothes.is/account/developer
    HYPOTHESIS_GROUP   - group ID from hypothes.is/groups/...
    GOOGLE_APPLICATION_CREDENTIALS - service account JSON for Vertex AI
    GCP_PROJECT        - GCP project (default: sondreskarsten-d7d14)
    GCP_LOCATION       - Vertex AI region (default: europe-west1)

Decision JSON schema (LLM output):

    {
      "action": "re-anchor" | "reclassify" | "propose-concept" | "delete",
      "exact": "1 100",                              # re-anchor only
      "prefix": "Skattekostnad ",                    # re-anchor only
      "suffix": "\\nResultat",                       # re-anchor only
      "page": 5,                                     # re-anchor only (PDF page)
      "new_concept_id": "regnskap-no:Skattekostnad", # reclassify only
      "proposed_concept_id": "regnskap-no:NyttKonsept", # propose-concept only
      "rationale": "...",                            # reclassify or propose
      "citation": "§ 7-29 (3)",                      # propose only
      "confidence": 0.0 - 1.0
    }
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any

from google import genai
from google.genai import types

import regnskapnoter as rn

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
- The full source PDF of the årsregnskap (årsoppgjør / annual report).
- The raw extracted JSON of the noter (notes section), including any [[p:N]]
  page-break markers in note full_text.
- The unanchored observation: a (concept_id, value) tuple from the regnskapnoter
  taxonomy (https://github.com/sondreskarsten/regnskapnoter-taxonomy) and the
  framework labels for the concept.

Decide one of four actions:

(a) re-anchor: the value IS in the document. Provide the literal substring as it
    appears (preserving Norwegian thousands separators like '\\u00a0' or space,
    parentheses for negatives, etc.) plus 32 characters of prefix/suffix. If the
    raw JSON full_text has [[p:N]] markers upstream of the match, include the
    page number. If the value appears only in the PDF (e.g. in a primary
    statement table the noter extractor skipped), still emit re-anchor with the
    PDF page number.

(b) reclassify: the value is in the document but assigned to the wrong concept_id.
    Propose the correct concept_id from the taxonomy (must start with
    'regnskap-no:'). Provide a rationale.

(c) propose-concept: the value is real, the concept does not exist in the
    taxonomy, and a new concept should be added. Provide a CamelCase
    proposed_concept_id, a rationale, and a regnskapsloven (e.g. '§ 7-XX (N)')
    or NRS citation. The taxonomy maintainer will review.

(d) delete: the value is spurious — extraction artifact, OCR garbage,
    misattributed scaling factor, etc.

Return ONLY a JSON object matching this schema (no prose, no markdown):

{
  "action": "re-anchor" | "reclassify" | "propose-concept" | "delete",
  "exact": "string",
  "prefix": "string",
  "suffix": "string",
  "page": integer | null,
  "new_concept_id": "regnskap-no:..." | null,
  "proposed_concept_id": "regnskap-no:..." | null,
  "rationale": "string",
  "citation": "string",
  "confidence": 0.0 - 1.0
}

Use null for fields not relevant to your action. Set confidence below 0.6 if you
are uncertain; the system will skip low-confidence decisions for human review.
"""


def _format_user_prompt(ann: dict, raw: dict) -> str:
    """Build the user-turn text describing one annotation needing review."""
    framework_labels = []
    cid = ann.get("concept_id") or ""
    if cid:
        framework_labels = rn.framework_for_concept(cid)
    notes = raw.get("notes") or []
    notes_summary = "\n".join(
        f"  Note {n.get('note_number', '?')} '{n.get('title', n.get('note_title', ''))}'"
        f" (pages {n.get('page_start', '?')}-{n.get('page_end', '?')})"
        for n in notes
        if isinstance(n, dict)
    )
    return f"""Unanchored observation:
  concept_id   : {cid}
  value        : {ann.get("value", "")}
  frameworks   : {framework_labels}
  hypothesis_id: {ann.get("hypothesis_id", "")}
  uri          : {ann.get("uri", "")}

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

The complete source PDF is attached. Read both. Decide and emit the JSON
decision per the system prompt schema.
"""


def _call_gemini(user_text: str, pdf_bytes: bytes) -> dict[str, Any]:
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

    if action == "re-anchor":
        session.re_anchor(
            ann,
            exact=decision["exact"],
            prefix=decision.get("prefix", ""),
            suffix=decision.get("suffix", ""),
            page=decision.get("page"),
        )
        return f"re-anchored(page={decision.get('page')})"

    if action == "reclassify":
        new_cid = decision.get("new_concept_id")
        if not new_cid or not new_cid.startswith("regnskap-no:"):
            return f"invalid_concept_id({new_cid!r})"
        session.reclassify(
            ann,
            new_concept_id=new_cid,
            rationale=decision.get("rationale", ""),
        )
        return f"reclassified -> {new_cid}"

    if action == "propose-concept":
        proposed = decision.get("proposed_concept_id")
        if not proposed:
            return "missing_proposed_concept_id"
        session.propose_concept(
            ann,
            new_concept_id=proposed,
            rationale=decision.get("rationale", ""),
            paragraph_citation=decision.get("citation", ""),
        )
        return f"proposed -> {proposed}"

    if action == "delete":
        session.delete(ann)
        return "deleted"

    return f"unknown_action({action!r})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM analyst loop")
    parser.add_argument("--max", type=int, default=50, help="Max annotations to process")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true", help="Decide but don't dispatch")
    args = parser.parse_args(argv)

    group = os.environ.get("HYPOTHESIS_GROUP")
    token = os.environ.get("HYPOTHESIS_TOKEN")
    if not group or not token:
        LOG.error("Set HYPOTHESIS_GROUP and HYPOTHESIS_TOKEN")
        return 2

    session = rn.AnalystSession(group_id=group, api_token=token)

    pdf_cache: dict[str, bytes] = {}
    raw_cache: dict[str, dict] = {}

    processed = 0
    outcomes: dict[str, int] = {}
    t0 = time.time()

    for ann in session.review_queue(batch_size=args.batch_size):
        if processed >= args.max:
            break
        urn = ann.get("uri") or ""
        try:
            if urn not in raw_cache:
                raw_cache[urn] = session.resolve_raw(urn)
            raw = raw_cache[urn]
            if urn not in pdf_cache:
                pdf_cache[urn] = session.get_pdf_bytes(urn)
            pdf_bytes = pdf_cache[urn]
        except Exception as e:
            LOG.warning("resolve_failed urn=%s err=%s", urn, e)
            outcomes["resolve_failed"] = outcomes.get("resolve_failed", 0) + 1
            processed += 1
            continue

        user_prompt = _format_user_prompt(ann, raw)
        try:
            decision = _call_gemini(user_prompt, pdf_bytes)
        except Exception as e:
            LOG.error("llm_call_failed h_id=%s err=%s", ann.get("hypothesis_id"), e)
            outcomes["llm_call_failed"] = outcomes.get("llm_call_failed", 0) + 1
            processed += 1
            continue

        if args.dry_run:
            print(
                json.dumps(
                    {
                        "hypothesis_id": ann.get("hypothesis_id"),
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
                LOG.error("dispatch_failed h_id=%s err=%s", ann.get("hypothesis_id"), e)
                outcome = f"dispatch_error:{type(e).__name__}"
            LOG.info(
                "h_id=%s concept=%s value=%s -> %s",
                ann.get("hypothesis_id"),
                ann.get("concept_id"),
                ann.get("value"),
                outcome,
            )
            outcomes[outcome.split("(")[0].strip()] = (
                outcomes.get(outcome.split("(")[0].strip(), 0) + 1
            )

        processed += 1

    elapsed = time.time() - t0
    LOG.info("processed=%d elapsed=%.1fs outcomes=%s", processed, elapsed, outcomes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
