"""Framework grouping primitives.

A "framework" is a (publisher, document) pair found in a concept's references[*].
Examples:
  ("Stortinget", "regnskapsloven") with paragraph "§ 7-29" -> framework label "§ 7-29"
  ("NRS", "Resultatskatt") with paragraph "kap. 5"        -> framework label "NRS Resultatskatt"
  ("NRS", "NRS 2") with paragraph "kap. 4"                -> framework label "NRS 2"

This replaces the table-shape grouping currently used in noter-extraction-tidy-tables:
the taxonomy already encodes which legal/standard framework each concept belongs to.
"""

from __future__ import annotations

import re
from collections import defaultdict
from functools import lru_cache

import pandas as pd

from regnskapnoter.tables import references

_PARA_RE = re.compile(r"§\s*\d+-\d+[a-z]?")


def _label_for(publisher: str, document: str, paragraph: str) -> str:
    """Synthesize a framework label from a reference triple.

    For regnskapsloven, prefer the paragraph (e.g. '§ 7-29') because the law subdivides
    into many distinct disclosure regimes. For NRS, use the document name directly.
    """
    if publisher == "Stortinget" and document == "regnskapsloven":
        m = _PARA_RE.search(paragraph or "")
        return m.group(0) if m else "regnskapsloven"
    if publisher == "NRS":
        return f"NRS {document}" if not document.lower().startswith("nrs") else document
    return f"{publisher}/{document}"


@lru_cache(maxsize=1)
def _framework_index() -> dict[str, list[str]]:
    """Build concept_id -> [framework_label, ...] from the references parquet."""
    refs = references()
    idx: dict[str, list[str]] = defaultdict(list)
    for r in refs.itertuples(index=False):
        cid = getattr(r, "subject_id", None)
        if not cid:
            continue
        publisher = getattr(r, "publisher", "") or ""
        document = getattr(r, "document", "") or ""
        paragraph = getattr(r, "paragraph", "") or ""
        label = _label_for(publisher, document, paragraph)
        if label not in idx[cid]:
            idx[cid].append(label)
    return dict(idx)


def frameworks() -> pd.DataFrame:
    """Return a tidy DataFrame: concept_id, framework, publisher, document, paragraph.

    One row per (concept, reference). A concept with two references appears twice.
    """
    refs = references().copy()
    refs["framework"] = refs.apply(
        lambda r: _label_for(r.get("publisher", ""), r.get("document", ""), r.get("paragraph", "")),
        axis=1,
    )
    return refs[["subject_id", "framework", "publisher", "document", "paragraph"]].rename(
        columns={"subject_id": "concept_id"}
    )


def list_frameworks() -> pd.DataFrame:
    """Return distinct frameworks with their concept count, sorted by count desc."""
    f = frameworks()
    out = f.groupby("framework").size().reset_index(name="concept_count")
    return out.sort_values("concept_count", ascending=False).reset_index(drop=True)


def framework_for_concept(concept_id: str) -> list[str]:
    """Return the list of framework labels this concept belongs to."""
    return list(_framework_index().get(concept_id, []))


def concepts_in_framework(framework: str) -> list[str]:
    """Return concept_ids belonging to a given framework label.

    The match is exact on the framework label produced by _label_for(); see frameworks()
    for the canonical labels.
    """
    f = frameworks()
    return sorted(f[f["framework"] == framework]["concept_id"].unique().tolist())
