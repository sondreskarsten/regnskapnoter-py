"""Calibrate MIN_CONFIDENCE for the LLM analyst loop.

The threshold ``MIN_CONFIDENCE`` (default 0.6) gates which LLM decisions get
appended to the GCS event log. Gemini's self-reported confidence is not a
calibrated probability, so before relying on it in production you measure how
its reported confidence corresponds to actual decision quality on a labelled
sample, and pick the threshold that matches your tolerance for noise.

Workflow:

    Stage 1 (one-time, no GCS writes):
        rn-calibrate sample --orgnr 811722332 --year 2024 --n 50 \
            --out gs://sondre_brreg_data/raw/regnskapnoter_calibration/sample.jsonl

    Stage 2 (offline, the reviewer fills in ground_truth column):
        Open the generated CSV/JSONL. For each row, decide whether the LLM's
        decision was correct. Set ground_truth to one of:
            "correct"     -- action and content are right
            "wrong"       -- LLM was wrong
            "skip"        -- can't tell (e.g. value not actually in PDF)

    Stage 3 (compute the calibration table + recommended threshold):
        rn-calibrate score --in sample.jsonl

The output table shows precision (correct / decided) at each confidence band.
A threshold T gives you the precision of decisions where confidence >= T. Pick
T so that the LLM noise admitted into current_state matches your tolerance.

This script does NOT mutate the production event log. It only reads from
review_queue, calls Gemini, and writes a JSONL file for offline review.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import regnskapnoter as rn

# Lazy import: only fail if user runs the LLM stage without [llm] extra
try:
    from google import genai
    from google.genai import types

    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False


PROJECT = os.environ.get("GCP_PROJECT", "sondreskarsten-d7d14")
LOCATION = os.environ.get("GCP_LOCATION", "europe-west1")
MODEL = os.environ.get("ANALYST_MODEL", "gemini-2.5-flash")


SYSTEM_PROMPT = """You are an analyst reviewing automatically-extracted Norwegian
financial-statement note values that the extractor failed to anchor to a text span.

For each unanchored value, you receive:
- The full source PDF of the årsregnskap.
- The raw extracted JSON of the noter, including [[p:N]] page-break markers.
- The unanchored observation: a (concept_id, value) tuple from the regnskapnoter
  taxonomy and the framework labels for the concept.

Decide one of four actions:

(a) re-anchor: the value IS in the document. Provide the literal substring as
    it appears (preserving Norwegian thousands separators) plus 32 chars of
    prefix/suffix and the page number.
(b) reclassify: the value is in the document but assigned to the wrong
    concept_id. Propose the correct concept_id (must start with 'regnskap-no:').
(c) propose-concept: the value is real, no taxonomy concept fits. Provide a
    CamelCase proposed_concept_id, rationale, and a regnskapsloven (e.g.
    '§ 7-XX (N)') or NRS citation.
(d) delete: the value is spurious - extraction artifact, OCR garbage, etc.

Return ONLY a JSON object:

{
  "action": "re-anchor" | "reclassify" | "propose-concept" | "delete",
  "exact": string, "prefix": string, "suffix": string, "page": int | null,
  "new_concept_id": "regnskap-no:..." | null,
  "proposed_concept_id": "regnskap-no:..." | null,
  "rationale": string, "citation": string,
  "confidence": 0.0 - 1.0
}

Use null for fields not relevant. Set confidence honestly: how likely is your
chosen action to be correct in expert review?
"""


def _format_user_prompt(ann: dict, raw: dict) -> str:
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

Raw extraction summary:
  orgnr        : {raw.get("orgnr", "")}
  year         : {raw.get("year", "")}
  total_pages  : {raw.get("total_pages", "?")}
  notes:
{notes_summary}

Full raw JSON of noter:
```json
{json.dumps({k: v for k, v in raw.items() if k != "_source_path"}, ensure_ascii=False, indent=1)}
```

The complete source PDF is attached. Decide and emit JSON per the schema.
"""


def _call_gemini(client, user_text: str, pdf_bytes: bytes) -> dict:
    response = client.models.generate_content(
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


def _ensure_genai() -> Any:
    if not _GENAI_AVAILABLE:
        sys.exit("google-genai not installed. pip install 'regnskapnoter[llm]'")
    return genai.Client(vertexai=True, project=PROJECT, location=LOCATION)


# ---------------------------------------------------------------------------
# Stage 1: sample
# ---------------------------------------------------------------------------


def cmd_sample(args: argparse.Namespace) -> int:
    client = _ensure_genai()
    session = rn.AnalystSession()

    pdf_cache: dict[str, bytes] = {}
    raw_cache: dict[str, dict] = {}

    queue = list(session.review_queue(orgnr=args.orgnr, year=args.year))
    if not queue:
        print(f"review_queue empty for orgnr={args.orgnr} year={args.year}", file=sys.stderr)
        print(
            "Run `rn push --orgnr {orgnr} --year {year}` first to seed observations",
            file=sys.stderr,
        )
        return 1

    sample = queue[: args.n]
    print(
        f"sampling {len(sample)} annotations from review_queue (size={len(queue)})", file=sys.stderr
    )

    rows: list[dict[str, Any]] = []
    t0 = time.time()
    for i, ann in enumerate(sample, 1):
        try:
            urn = ann["source"]
            if urn not in raw_cache:
                raw_cache[urn] = session.resolve_raw(urn)
            if urn not in pdf_cache:
                pdf_cache[urn] = session.get_pdf_bytes(urn)
            raw = raw_cache[urn]
            pdf_bytes = pdf_cache[urn]
        except Exception as e:
            print(f"  [{i}/{len(sample)}] resolve_failed: {e}", file=sys.stderr)
            continue

        user_prompt = _format_user_prompt(ann, raw)
        try:
            decision = _call_gemini(client, user_prompt, pdf_bytes)
        except Exception as e:
            print(f"  [{i}/{len(sample)}] llm_call_failed: {e}", file=sys.stderr)
            continue

        rows.append(
            {
                "annotation_id": ann.get("annotation_id"),
                "concept_id": ann.get("concept_id"),
                "value": ann.get("value"),
                "note_number": ann.get("note_number"),
                "page": ann.get("page"),
                "source": ann.get("source"),
                "decision_action": decision.get("action"),
                "decision_exact": decision.get("exact"),
                "decision_page": decision.get("page"),
                "decision_new_concept_id": decision.get("new_concept_id"),
                "decision_proposed_concept_id": decision.get("proposed_concept_id"),
                "decision_rationale": decision.get("rationale"),
                "decision_citation": decision.get("citation"),
                "confidence": decision.get("confidence"),
                "ground_truth": "",
                "reviewer_note": "",
            }
        )
        if i % 5 == 0:
            print(f"  [{i}/{len(sample)}] decisions collected", file=sys.stderr)

    elapsed = time.time() - t0
    print(
        f"collected {len(rows)} decisions in {elapsed:.1f}s "
        f"(~{elapsed / max(len(rows), 1):.1f}s/decision)",
        file=sys.stderr,
    )

    _write_output(rows, args.out)
    print(f"wrote {args.out}", file=sys.stderr)
    print("\nNext: open the file, set ground_truth on each row to one of:", file=sys.stderr)
    print("  correct | wrong | skip", file=sys.stderr)
    print("Then run: rn-calibrate score --in {file}", file=sys.stderr)
    return 0


def _write_output(rows: list[dict], path: str) -> None:
    if path.startswith("gs://"):
        from google.cloud import storage

        _, _, rest = path.partition("gs://")
        bucket_name, _, blob_name = rest.partition("/")
        if path.endswith(".csv"):
            buf = io.StringIO()
            if rows:
                w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            data = buf.getvalue().encode()
        else:
            data = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode()
        storage.Client().bucket(bucket_name).blob(blob_name).upload_from_string(
            data,
            content_type="text/csv" if path.endswith(".csv") else "application/x-ndjson",
        )
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if path.endswith(".csv"):
            with open(path, "w", newline="", encoding="utf-8") as f:
                if rows:
                    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                    w.writeheader()
                    w.writerows(rows)
        else:
            with open(path, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _read_input(path: str) -> list[dict]:
    if path.startswith("gs://"):
        from google.cloud import storage

        _, _, rest = path.partition("gs://")
        bucket_name, _, blob_name = rest.partition("/")
        data = storage.Client().bucket(bucket_name).blob(blob_name).download_as_text()
    else:
        data = Path(path).read_text(encoding="utf-8")

    rows: list[dict] = []
    if path.endswith(".csv"):
        rows.extend(iter(csv.DictReader(io.StringIO(data))))
    else:
        for line in data.splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# Stage 3: score
# ---------------------------------------------------------------------------


def cmd_score(args: argparse.Namespace) -> int:
    rows = _read_input(args.input)
    if not rows:
        print("no rows", file=sys.stderr)
        return 1

    labelled = [r for r in rows if str(r.get("ground_truth", "")).strip() in ("correct", "wrong")]
    skipped = [r for r in rows if str(r.get("ground_truth", "")).strip() == "skip"]
    unlabelled = [r for r in rows if not str(r.get("ground_truth", "")).strip()]

    print(f"total rows           : {len(rows)}")
    print(f"  labelled correct   : {sum(1 for r in labelled if r['ground_truth'] == 'correct')}")
    print(f"  labelled wrong     : {sum(1 for r in labelled if r['ground_truth'] == 'wrong')}")
    print(f"  skipped (can't tell): {len(skipped)}")
    print(f"  unlabelled         : {len(unlabelled)}")
    if unlabelled:
        print(
            f"  WARNING: {len(unlabelled)} rows still unlabelled - results partial", file=sys.stderr
        )
    print()

    bands = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
    print(f"{'confidence band':<18}{'n':>6}{'correct':>9}{'wrong':>7}{'precision':>11}")
    print("-" * 51)
    for lo, hi in bands:
        in_band = [r for r in labelled if lo <= float(r.get("confidence") or 0) < hi]
        correct = sum(1 for r in in_band if r["ground_truth"] == "correct")
        wrong = len(in_band) - correct
        precision = correct / len(in_band) if in_band else float("nan")
        print(
            f"[{lo:.2f}, {hi:.2f}){'':<6}{len(in_band):>6}{correct:>9}{wrong:>7}{precision:>11.3f}"
        )
    print()

    print("cumulative precision at threshold (T = at least this confidence):")
    print(f"{'threshold':<12}{'kept':>6}{'rejected':>10}{'correct@kept':>14}{'precision':>12}")
    print("-" * 54)
    for t in [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]:
        kept = [r for r in labelled if float(r.get("confidence") or 0) >= t]
        rejected = len(labelled) - len(kept)
        correct = sum(1 for r in kept if r["ground_truth"] == "correct")
        precision = correct / len(kept) if kept else float("nan")
        print(f"T={t:.2f}{'':<7}{len(kept):>6}{rejected:>10}{correct:>14}{precision:>12.3f}")
    print()

    # Per-action breakdown
    actions = {r["decision_action"] for r in labelled if r.get("decision_action")}
    if actions:
        print("precision by action type:")
        print(f"{'action':<22}{'n':>6}{'correct':>9}{'precision':>12}")
        print("-" * 49)
        for a in sorted(actions):
            sub = [r for r in labelled if r["decision_action"] == a]
            correct = sum(1 for r in sub if r["ground_truth"] == "correct")
            precision = correct / len(sub) if sub else float("nan")
            print(f"{a:<22}{len(sub):>6}{correct:>9}{precision:>12.3f}")

    print()
    print("recommendation: pick T so precision matches your tolerance for")
    print("LLM noise admitted into current_state. e.g. if you want >=0.95")
    print("precision on dispatched decisions, choose the lowest T column above")
    print("at which precision is >=0.95.")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rn-calibrate",
        description="Calibrate MIN_CONFIDENCE for the LLM analyst loop",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser(
        "sample", help="Stage 1: collect LLM decisions on a sample, no event-log writes"
    )
    s.add_argument("--orgnr", required=True)
    s.add_argument("--year", required=True, type=int)
    s.add_argument("--n", type=int, default=50, help="Sample size (default 50)")
    s.add_argument(
        "--out",
        default="calibration_sample.jsonl",
        help="Output path (.jsonl or .csv, local or gs://)",
    )
    s.set_defaults(func=cmd_sample)

    sc = sub.add_parser("score", help="Stage 3: read labelled file and print calibration table")
    sc.add_argument(
        "--input",
        "--in",
        dest="input",
        required=True,
        help="Labelled file (the file produced by `sample` with ground_truth filled)",
    )
    sc.set_defaults(func=cmd_score)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
