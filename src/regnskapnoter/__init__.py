"""regnskapnoter — Python client for the Norwegian regnskapsnoter taxonomy.

Quick start:
    >>> import regnskapnoter as rn
    >>> rn.concepts()                       # 279 concept rows
    >>> rn.frameworks()                     # framework membership: concept_id -> [§ N, NRS X, ...]
    >>> rn.concepts_in_framework("§ 7-29")  # concepts under § 7-29 Skattekostnad
    >>> rn.canonicalize(extracted_df)       # column-keyed -> concept-keyed observations
"""

from regnskapnoter.analyst import AnalystSession, build_annotations_with_urn
from regnskapnoter.annotations import (
    annotations_to_jsonld,
    build_annotations,
    coverage_report,
)
from regnskapnoter.frameworks import (
    concepts_in_framework,
    framework_for_concept,
    frameworks,
    list_frameworks,
)
from regnskapnoter.hypothesis import (
    PROPOSED_CONCEPT_TAG,
    REVIEW_TAG,
    WRONG_CONCEPT_TAG,
    delete_hypothesis,
    from_hypothesis,
    iter_review_queue,
    proposed_concepts,
    re_anchor,
    review_queue,
    to_hypothesis,
    update_hypothesis,
)
from regnskapnoter.loader import (
    artifact_path,
    available_versions,
    clear_cache,
    load,
    set_version,
    version,
)
from regnskapnoter.tables import (
    axes,
    axis_members,
    build_tables_mapping,
    calc_arcs,
    canonicalize,
    concept_for_column,
    concepts,
    definitions,
    labels,
    mappings,
    references,
)
from regnskapnoter.urn import (
    parse_urn,
    to_gcs_path,
    to_pdf_gcs_path,
    to_urn,
)

__version__ = "0.5.0"
__all__ = [
    "PROPOSED_CONCEPT_TAG",
    "REVIEW_TAG",
    "WRONG_CONCEPT_TAG",
    "AnalystSession",
    "__version__",
    "annotations_to_jsonld",
    "artifact_path",
    "available_versions",
    "axes",
    "axis_members",
    "build_annotations",
    "build_annotations_with_urn",
    "build_tables_mapping",
    "calc_arcs",
    "canonicalize",
    "clear_cache",
    "concept_for_column",
    "concepts",
    "concepts_in_framework",
    "coverage_report",
    "definitions",
    "delete_hypothesis",
    "framework_for_concept",
    "frameworks",
    "from_hypothesis",
    "iter_review_queue",
    "labels",
    "list_frameworks",
    "load",
    "mappings",
    "parse_urn",
    "proposed_concepts",
    "re_anchor",
    "references",
    "review_queue",
    "set_version",
    "to_gcs_path",
    "to_hypothesis",
    "to_pdf_gcs_path",
    "to_urn",
    "update_hypothesis",
    "version",
]
