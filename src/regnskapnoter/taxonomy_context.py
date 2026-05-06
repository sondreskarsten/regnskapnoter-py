"""Build structured taxonomy context for LLM prompt injection.

Given a set of concept_ids (from annotations), this module loads the relevant
slices of definitions, calc_arcs, labels, and references from the taxonomy,
then formats them into a compact text block the LLM can use to ground its
reclassify / propose-concept / re-anchor / delete decisions.

Design: 100% coverage of taxonomy concepts, no upstream dependency.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pyarrow.parquet as pq

from regnskapnoter.loader import _fetch


@dataclass(frozen=True)
class ConceptContext:
    concept_id: str
    label_nb: str
    label_en: str | None
    definition: str | None
    calc_parent: str | None
    calc_siblings: list[str]
    references: list[str]


def _load_table(version: str, name: str):
    path = _fetch(version, f"{name}.parquet")
    return pq.read_table(str(path)).to_pandas()


def load_concept_contexts(
    concept_ids: Sequence[str],
    version: str = "latest",
) -> dict[str, ConceptContext]:
    """Load taxonomy context for a batch of concept_ids.

    Returns a dict keyed by concept_id.
    """
    labels = _load_table(version, "labels")
    defs = _load_table(version, "definitions")
    arcs = _load_table(version, "calc_arcs")
    refs = _load_table(version, "references")

    label_nb = dict(
        labels[(labels["role"] == "standardLabel") & (labels["lang"] == "nb")].set_index(
            "subject_id"
        )["text"]
    )
    label_en = dict(
        labels[(labels["role"] == "standardLabel") & (labels["lang"] == "en")].set_index(
            "subject_id"
        )["text"]
    )

    def_text = {}
    for _, row in defs[defs["lang"] == "nb"].iterrows():
        cid = row["concept_id"]
        if cid not in def_text:
            def_text[cid] = row["text"]

    parent_map: dict[str, str] = {}
    siblings_map: dict[str, list[str]] = {}
    for _, row in arcs.iterrows():
        child = row["child_id"]
        parent = row["parent_id"]
        parent_map[child] = parent

    for _, row in arcs.iterrows():
        parent = row["parent_id"]
        children = arcs[arcs["parent_id"] == parent]["child_id"].tolist()
        for ch in children:
            siblings_map[ch] = [c for c in children if c != ch]

    ref_map: dict[str, list[str]] = {}
    for _, row in refs.iterrows():
        cid = row["subject_id"]
        parts = [row.get("publisher", ""), row.get("document", ""), row.get("paragraph", "")]
        ref_str = " ".join(str(p) for p in parts if p and str(p) != "nan").strip()
        if ref_str:
            ref_map.setdefault(cid, []).append(ref_str)

    result = {}
    for cid in concept_ids:
        result[cid] = ConceptContext(
            concept_id=cid,
            label_nb=label_nb.get(cid, cid.split(":")[-1]),
            label_en=label_en.get(cid),
            definition=def_text.get(cid),
            calc_parent=parent_map.get(cid),
            calc_siblings=siblings_map.get(cid, []),
            references=ref_map.get(cid, []),
        )
    return result


def format_context_block(
    contexts: dict[str, ConceptContext],
    max_chars: int = 12000,
) -> str:
    """Format concept contexts into a compact text block for LLM injection."""
    lines = ["<taxonomy_context>"]
    budget = max_chars - 40

    for _cid, ctx in sorted(contexts.items(), key=lambda x: x[1].label_nb):
        entry = [f"## {ctx.label_nb}"]
        if ctx.label_en:
            entry.append(f"EN: {ctx.label_en}")
        entry.append(f"ID: {ctx.concept_id}")
        if ctx.definition:
            trunc = ctx.definition[:500] + "…" if len(ctx.definition) > 500 else ctx.definition
            entry.append(f"Definition: {trunc}")
        if ctx.calc_parent:
            parent_label = contexts.get(ctx.calc_parent)
            plbl = parent_label.label_nb if parent_label else ctx.calc_parent.split(":")[-1]
            entry.append(f"Parent: {plbl}")
        if ctx.calc_siblings:
            sib_labels = []
            for s in ctx.calc_siblings[:8]:
                sc = contexts.get(s)
                sib_labels.append(sc.label_nb if sc else s.split(":")[-1])
            entry.append(f"Siblings: {', '.join(sib_labels)}")
        if ctx.references:
            entry.append(f"Legal refs: {'; '.join(ctx.references[:4])}")

        block = "\n".join(entry)
        if len("\n".join(lines)) + len(block) + 2 > budget:
            lines.append("... (truncated, further concepts omitted)")
            break
        lines.append(block)

    lines.append("</taxonomy_context>")
    return "\n\n".join(lines)
