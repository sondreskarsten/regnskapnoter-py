# Calibrating `MIN_CONFIDENCE` for the LLM analyst loop

## Why this exists

The LLM analyst dispatches a decision (re-anchor / reclassify / propose-concept / delete) only when its self-reported `confidence` exceeds `MIN_CONFIDENCE` (default `0.6`). Below the threshold, the decision is logged but no event is appended.

`confidence` is what Gemini outputs in its decision JSON. **It is not a calibrated probability.** A score of 0.8 does not mean an 80% chance of being correct. The number is only useful if you measure how it correlates with actual decision quality on your data, then pick a threshold that matches the noise tolerance you can live with downstream.

This procedure produces that measurement and that recommendation. Total time required: 30–60 minutes for the reviewer, plus one Gemini call per sample annotation (~3–5 seconds each, $0.001–$0.003 per call depending on PDF size).

The procedure does **not** mutate the production event log. It only reads from `review_queue` and writes a JSONL/CSV to a working location of your choice.

---

## Prerequisites

You need:

- `pip install 'regnskapnoter[gcs,llm]'` — installs `google-genai` and `google-cloud-storage`
- `GOOGLE_APPLICATION_CREDENTIALS` pointing at a service account that has
   - read on `gs://sondre_brreg_data/` (raw JSON, structured CSVs, event log)
   - read on `gs://brreg-regnskap/` (source PDFs)
   - Vertex AI User on the GCP project (for Gemini calls)
- `GCP_PROJECT=sondreskarsten-d7d14` (or whichever project you're using)
- `GCP_LOCATION=europe-west1` (Vertex AI region; do NOT use europe-north2 — Gemini PDF inline doesn't work there)
- At least one filing already pushed to the event log:

  ```bash
  rn push --orgnr 811722332 --year 2024
  ```

  Confirm with:

  ```bash
  rn stats --orgnr 811722332 --year 2024
  ```

  You need `annotations_unmatched >= 30` to do useful calibration. If a single filing has too few unmatched annotations, push another and the calibration script can sample across both.

---

## Stage 1 — collect a sample (10–20 minutes wall-clock)

Run the calibration script to fetch a sample of unmatched annotations, ask Gemini for a decision on each, and write the results to a labelling worksheet. **No event-log writes happen here.**

```bash
python examples/calibration/calibrate.py sample \
    --orgnr 811722332 --year 2024 \
    --n 50 \
    --out gs://sondre_brreg_data/raw/regnskapnoter_calibration/2026-05-06_run1.jsonl
```

Arguments:

- `--orgnr` / `--year`: which filing to sample from. The annotations in the review queue (those with `match_status='unmatched'`) are sampled in the order they appear.
- `--n`: sample size. **50 is the recommended minimum**; smaller samples give wide confidence bands and noisy thresholds. 100 is better if you have the time.
- `--out`: where to write the labelling worksheet. Accepts `gs://...` (JSONL or CSV) or a local path. CSV is friendlier for spreadsheet review; JSONL preserves nested decision fields.

Output: a file with one row per sampled annotation, each containing the original `(annotation_id, concept_id, value)`, the LLM's decision, and the LLM's `confidence`. Two empty columns at the end — `ground_truth` and `reviewer_note` — are for the reviewer to fill in.

The script prints to stderr a per-decision progress log and a summary like:

```
sampling 50 annotations from review_queue (size=10)
  [5/50] decisions collected
  [10/50] decisions collected
  ...
collected 50 decisions in 187.4s (~3.7s/decision)
wrote gs://sondre_brreg_data/raw/regnskapnoter_calibration/2026-05-06_run1.jsonl
```

If the review queue has fewer than `--n` annotations, the script samples all of them and prints a warning. **In that case you need a second filing.** Run:

```bash
rn shards | head
# pick another orgnr+year that has unmatched annotations
python examples/calibration/calibrate.py sample \
    --orgnr <other_orgnr> --year <other_year> --n 50 \
    --out gs://sondre_brreg_data/raw/regnskapnoter_calibration/2026-05-06_run2.jsonl
```

Then concatenate the two JSONL files before scoring (`cat run1.jsonl run2.jsonl > merged.jsonl`).

---

## Stage 2 — review and label (30 minutes for 50 rows)

This is the only manual step. The reviewer needs **a Norwegian financial-statement reader** — someone who can recognize whether a value/concept pairing matches what's on the page. This is typically you or a credit analyst, not the developer who wrote the pipeline.

For each row in the worksheet, the reviewer checks the LLM's decision against the source PDF and sets `ground_truth` to one of:

| value | meaning |
|---|---|
| `correct` | The LLM's action and content are right. Examples: re-anchor pointed at the actual occurrence of the value in the PDF; reclassify named the right concept_id; propose-concept's id and citation are sensible; delete identified an actual extraction artifact. |
| `wrong` | The LLM's action or content is incorrect. Examples: re-anchor's `exact` doesn't appear in the PDF; the value is real but reclassify named the wrong concept; delete dropped a value that's actually in the document. |
| `skip` | The reviewer cannot tell. Most commonly: the value isn't in the rendered PDF at all (OCR garbage from the source extraction), so neither the LLM nor the reviewer can verify. These rows are excluded from the precision math. |

The `reviewer_note` column is free-form — useful for capturing edge cases the team should discuss.

### Reviewer workflow

1. Download the file:

   ```bash
   gsutil cp gs://sondre_brreg_data/raw/regnskapnoter_calibration/2026-05-06_run1.jsonl /tmp/cal.jsonl
   # or for CSV:
   gsutil cp gs://sondre_brreg_data/raw/regnskapnoter_calibration/2026-05-06_run1.csv /tmp/cal.csv
   ```

2. Open the source PDF for each unique `source` URN. Helper:

   ```bash
   python -c "
   import regnskapnoter as rn, sys
   urn = sys.argv[1]
   print(rn.AnalystSession().resolve_pdf_uri(urn))" \
   urn:noter:811722332:2024
   # gs://brreg-regnskap/811722332_aarsregnskap_2024.pdf
   gsutil cp gs://brreg-regnskap/811722332_aarsregnskap_2024.pdf /tmp/
   ```

3. Open `/tmp/cal.csv` in a spreadsheet (or `/tmp/cal.jsonl` in any text editor with JSON formatting).

4. For each row:
   - Read the LLM decision: `decision_action`, `decision_exact`, `decision_page`, `decision_new_concept_id`, `decision_proposed_concept_id`.
   - Open the PDF to `decision_page` (or whatever page the value should be on).
   - Look for the value at the location the LLM proposed.
   - Set `ground_truth` to `correct`, `wrong`, or `skip`.
   - Optionally fill `reviewer_note`.

5. Save the file with the same name (or upload back to GCS):

   ```bash
   gsutil cp /tmp/cal.jsonl gs://sondre_brreg_data/raw/regnskapnoter_calibration/2026-05-06_run1.labelled.jsonl
   ```

### Decision rules to reduce reviewer drift

- Re-anchor: `correct` only if the `decision_exact` substring **appears verbatim** on `decision_page` of the rendered PDF (allowing visual whitespace differences). If the value is on a different page or the substring doesn't appear, mark `wrong`.
- Reclassify: `correct` only if the new concept_id is the canonically right one for that disclosure under regnskapsloven / NRS. If multiple concepts could plausibly fit, mark `correct` if the LLM picked one of the plausible ones.
- Propose-concept: `correct` if the proposed concept_id is a plausible new addition AND the citation is real (a real `regnskapsloven §` or NRS reference, not invented). Either failure → `wrong`.
- Delete: `correct` only if the value really is spurious (not in the PDF, OCR garbage, scaling artifact, etc). When in doubt, mark `wrong` — false-positive deletes are the most expensive failure mode.

---

## Stage 3 — score and pick a threshold (1 minute)

Run:

```bash
python examples/calibration/calibrate.py score \
    --in gs://sondre_brreg_data/raw/regnskapnoter_calibration/2026-05-06_run1.labelled.jsonl
```

Sample output:

```
total rows           : 50
  labelled correct   : 32
  labelled wrong     : 8
  skipped (can't tell): 7
  unlabelled         : 3
  WARNING: 3 rows still unlabelled - results partial

confidence band        n  correct  wrong  precision
---------------------------------------------------
[0.00, 0.50)           4        0      4      0.000
[0.50, 0.60)           5        2      3      0.400
[0.60, 0.70)           7        5      2      0.714
[0.70, 0.80)          11        9      2      0.818
[0.80, 0.90)          10       10      0      1.000
[0.90, 1.01)           3        3      0      1.000

cumulative precision at threshold (T = at least this confidence):
threshold     kept  rejected  correct@kept   precision
------------------------------------------------------
T=0.50           36         4            29       0.806
T=0.55           33         7            27       0.818
T=0.60           28        12            27       0.964
T=0.65           24        16            24       1.000
T=0.70           21        19            22       1.000
...

precision by action type:
action                     n  correct   precision
-------------------------------------------------
delete                     3        1       0.333
propose-concept            5        4       0.800
re-anchor                 27       23       0.852
reclassify                 5        4       0.800
```

### How to read the table

**Confidence band:** straightforward. Each row is the precision of LLM decisions whose self-reported confidence fell in that band. If the bands are roughly monotonic (precision goes up as confidence goes up), the LLM's confidence has signal. If they're flat or non-monotonic, confidence is uninformative on this data — set `MIN_CONFIDENCE=1.01` and don't dispatch any decisions automatically.

**Cumulative precision at threshold:** the operationally useful column. For each `T`, this is the precision you'd get if you set `MIN_CONFIDENCE=T`. The `kept` column tells you how many decisions would be dispatched at that threshold.

**Precision by action type:** which action types the LLM is reliable on. In the example above, `delete` precision is 33% — the LLM is bad at distinguishing real spurious values from real values it just couldn't anchor. Strong signal that you should disable `delete` dispatching even when confidence is high. (You can do that by patching `_dispatch` in `examples/llm_analyst.py` to refuse `delete` actions, or by adding a separate `MIN_CONFIDENCE_DELETE` env var.)

### Picking the threshold

The recommendation depends on **what you intend to do with the dispatched decisions downstream.**

- **Dispatched decisions feed a downstream automated process (no human review):** require high precision. Choose the lowest `T` where cumulative precision is `>=0.95` (or higher if the downstream process has expensive failure modes).
- **Dispatched decisions are a backlog for the taxonomy maintainer or analyst:** lower bar; precision `>=0.8` is fine. The maintainer's review will catch errors. Use a lower `T` to maximize coverage.
- **Decisions affect external reporting (regulatory disclosures, etc.):** require near-perfect precision. Set `T=0.95` and accept low coverage; manually review the rejected band.

Once you have a threshold, set it in the analyst's environment:

```bash
export MIN_CONFIDENCE=0.65
```

For Cloud Run deployments, update the Cloud Run Job env var:

```bash
gcloud run jobs update rn-analyst \
    --region europe-north1 \
    --update-env-vars MIN_CONFIDENCE=0.65
```

---

## Recalibration cadence

Calibration is not one-and-done. Plan to re-run when any of these change:

- **Gemini model version.** When `ANALYST_MODEL` changes (e.g. `gemini-2.5-flash` → `gemini-3-flash`), recalibrate. New model versions can shift confidence behavior dramatically.
- **System prompt.** Any edit to `SYSTEM_PROMPT` in `examples/llm_analyst.py` invalidates the calibration. Recalibrate before deploying.
- **Input shape.** If you switch the analyst from Gemini-extracted JSON input to OCR-text input (via `from_text_pages` etc), recalibrate — the input distribution changed.
- **Quarterly anyway.** Document drift, taxonomy changes, and Gemini's behavioral changes can shift the precision curve. A quarterly run with `--n 50` is cheap insurance.

Persist each run's labelled file under `gs://sondre_brreg_data/raw/regnskapnoter_calibration/{date}_{tag}.labelled.jsonl` so you can plot the precision curves over time.

---

## Cost ballpark

Per Stage-1 sample of N=50:

| component | cost | notes |
|---|---|---|
| Gemini 2.5 Flash inputs | ~$0.05–$0.15 | depends on PDF size; PDFs are ~200KB–2MB; budget $1 max for the first run, much less in practice |
| Gemini 2.5 Flash output | <$0.01 | decisions are ~200 tokens each |
| GCS read | negligible | <1 MB total |
| Reviewer time | 30 min | the actual cost |

Two runs per quarter is roughly 4 reviewer-hours per year and <$1 in compute. The downside risk of running un-calibrated is that the LLM persistently dispatches wrong decisions into `current_state` and degrades the value of the entire annotation store. Calibration is cheap insurance.

---

## Troubleshooting

**`review_queue empty for orgnr=X year=Y`**
You haven't pushed observations for that filing yet. Run `rn push --orgnr X --year Y` first.

**`google-genai not installed`**
You're missing the LLM extra. `pip install 'regnskapnoter[llm]'`.

**`PermissionDenied: Vertex AI User`**
The service account doesn't have the right IAM role. Add `roles/aiplatform.user` to the SA on the project.

**`No PDF found in GCS for urn:noter:...`**
The source PDF for that filing isn't in `gs://brreg-regnskap/`. Either skip the filing for calibration purposes, or use a different orgnr+year where the PDF exists.

**The Gemini call times out**
PDFs over ~5MB can fail. The script doesn't currently chunk PDFs. As a workaround, pick a smaller filing for calibration; the precision curve generalizes across filings of similar shape.

**The cumulative precision table is non-monotonic (precision drops as T goes up)**
This means a few high-confidence decisions are wrong while several low-confidence ones are right. The most common cause is sample size (50 is small). Re-run with `--n 100` or merge two runs together.

**All bands have precision 1.0 and you have <30 labelled rows**
Sample too small for any conclusion. Increase `--n`.

---

## Where the artifacts live

| artifact | location |
|---|---|
| Calibration script | `examples/calibration/calibrate.py` |
| This document | `examples/calibration/CALIBRATION.md` |
| Sample worksheets (per run) | `gs://sondre_brreg_data/raw/regnskapnoter_calibration/{date}_{tag}.jsonl` |
| Labelled worksheets (per run) | `gs://sondre_brreg_data/raw/regnskapnoter_calibration/{date}_{tag}.labelled.jsonl` |
| Production event log being protected | `gs://sondre_brreg_data/annotations/noter/{orgnr}/{year}/events.parquet` |
| Reference: LLM analyst loop | `docs/llm-analyst-loop.md` |
