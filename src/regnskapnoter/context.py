"""Taxonomy context builder for the LLM analyst loop.

Given a concept_id, builds a structured context block containing:
  - concept definition (from definitions.parquet)
  - calc-arc neighborhood: parent, children, siblings
  - legal references with resolved paragraph text from lovdata.no
  - framework membership

This context is injected into the LLM prompt alongside the raw observation,
giving the model the rule-book it needs to make defensible reclassify and
propose-concept decisions.
"""

from __future__ import annotations

import functools
import io
from dataclasses import dataclass

import pandas as pd

from regnskapnoter.law_loader import fetch_paragraph_text_with_chapter_fallback

NAMESPACE = "regnskap-no"


def _qualify(concept_id: str) -> str:
    if ":" in concept_id:
        return concept_id
    return f"{NAMESPACE}:{concept_id}"


def _strip_ns(concept_id: str) -> str:
    if ":" in concept_id:
        return concept_id.split(":", 1)[1]
    return concept_id


@functools.lru_cache(maxsize=1)
def _load_taxonomy(version: str = "latest") -> dict[str, pd.DataFrame]:
    import pyarrow.parquet as pq
    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket("regnskapnoter-taxonomy")
    tables = {}
    for name in ["concepts", "definitions", "calc_arcs", "references", "labels"]:
        blob = bucket.blob(f"{version}/{name}.parquet")
        if blob.exists():
            tables[name] = pq.read_table(io.BytesIO(blob.download_as_bytes())).to_pandas()
    return tables


@dataclass(frozen=True)
class ConceptContext:
    concept_id: str
    label: str | None
    definition: str | None
    parent_ids: list[str]
    child_ids: list[str]
    sibling_ids: list[str]
    law_texts: list[dict]
    frameworks: list[str]


def build_concept_context(
    concept_id: str,
    fiscal_year: int = 2024,
    taxonomy_version: str = "latest",
    resolve_law_text: bool = True,
) -> ConceptContext:
    qid = _qualify(concept_id)
    tax = _load_taxonomy(taxonomy_version)
    definitions = tax.get("definitions", pd.DataFrame())
    calc_arcs = tax.get("calc_arcs", pd.DataFrame())
    references = tax.get("references", pd.DataFrame())
    labels = tax.get("labels", pd.DataFrame())

    label = None
    if not labels.empty:
        row = labels[(labels["subject_id"] == qid) & (labels["role"] == "standardLabel")]
        if not row.empty:
            label = row.iloc[0]["text"]

    defn = None
    if not definitions.empty:
        row = definitions[definitions["concept_id"] == qid]
        if not row.empty:
            defn = row.iloc[0]["text"]

    parent_ids, child_ids, sibling_ids = [], [], []
    if not calc_arcs.empty:
        parents = calc_arcs[calc_arcs["child_id"] == qid]["parent_id"].tolist()
        parent_ids = [_strip_ns(p) for p in parents]
        children = calc_arcs[calc_arcs["parent_id"] == qid]["child_id"].tolist()
        child_ids = [_strip_ns(c) for c in children]
        if parents:
            all_siblings = calc_arcs[calc_arcs["parent_id"].isin(parents)]["child_id"].tolist()
            sibling_ids = [_strip_ns(s) for s in all_siblings if s != qid]

    law_texts = []
    if not references.empty:
        concept_refs = references[references["subject_id"] == qid]
        for _, ref in concept_refs.iterrows():
            publisher = ref.get("publisher", "")
            document = ref.get("document", "")
            paragraph = ref.get("paragraph", "")
            entry = {
                "publisher": publisher,
                "document": document,
                "paragraph": paragraph,
                "text": None,
                "source": None,
            }
            if resolve_law_text and publisher == "Stortinget":
                text, source = fetch_paragraph_text_with_chapter_fallback(
                    publisher, document, paragraph, fiscal_year
                )
                entry["text"] = text
                entry["source"] = source
            law_texts.append(entry)

    frameworks: list[str] = []
    try:
        from regnskapnoter.frameworks import concept_frameworks

        frameworks = concept_frameworks(concept_id, version=taxonomy_version)
    except Exception:
        pass

    return ConceptContext(
        concept_id=_strip_ns(qid),
        label=label,
        definition=defn,
        parent_ids=parent_ids,
        child_ids=child_ids,
        sibling_ids=sibling_ids,
        law_texts=law_texts,
        frameworks=frameworks,
    )


def format_context_block(ctx: ConceptContext) -> str:
    lines = [f"=== CONCEPT: {ctx.concept_id} ==="]
    if ctx.label:
        lines.append(f"Label: {ctx.label}")
    if ctx.definition:
        lines.append(f"Definition: {ctx.definition}")
    if ctx.frameworks:
        lines.append(f"Frameworks: {', '.join(ctx.frameworks)}")
    if ctx.parent_ids:
        lines.append(f"Parent concepts: {', '.join(ctx.parent_ids)}")
    if ctx.child_ids:
        lines.append(f"Child concepts: {', '.join(ctx.child_ids)}")
    if ctx.sibling_ids:
        lines.append(f"Sibling concepts (same parent): {', '.join(ctx.sibling_ids[:10])}")
        if len(ctx.sibling_ids) > 10:
            lines.append(f"  ... and {len(ctx.sibling_ids) - 10} more")
    for ref in ctx.law_texts:
        citation = f"{ref['document']} {ref['paragraph']}"
        if ref["text"]:
            lines.append(f"\nLegal reference ({citation}):")
            lines.append(ref["text"])
        else:
            lines.append(f"Legal reference: {citation} (text not available)")
    return "\n".join(lines)


def build_observation_context(
    concept_id: str,
    fiscal_year: int = 2024,
    taxonomy_version: str = "latest",
) -> str:
    ctx = build_concept_context(concept_id, fiscal_year, taxonomy_version)
    return format_context_block(ctx)
