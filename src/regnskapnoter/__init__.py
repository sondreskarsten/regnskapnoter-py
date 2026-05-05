"""regnskapnoter — Python client for the Norwegian regnskapsnoter taxonomy.

Quick start:
    >>> import regnskapnoter as rn
    >>> rn.concepts()                       # 279 concept rows
    >>> rn.frameworks()                     # framework membership: concept_id -> [§ N, NRS X, ...]
    >>> rn.concepts_in_framework("§ 7-29")  # concepts under § 7-29 Skattekostnad
    >>> rn.canonicalize(extracted_df)       # column-keyed -> concept-keyed observations
"""

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

__version__ = "0.2.0"
__all__ = [
    "__version__",
    "annotations_to_jsonld",
    "artifact_path",
    "available_versions",
    "axes",
    "axis_members",
    "build_annotations",
    "build_tables_mapping",
    "calc_arcs",
    "canonicalize",
    "clear_cache",
    "concept_for_column",
    "concepts",
    "concepts_in_framework",
    "coverage_report",
    "definitions",
    "framework_for_concept",
    "frameworks",
    "labels",
    "list_frameworks",
    "load",
    "mappings",
    "references",
    "set_version",
    "version",
]
