"""regnskapnoter — Python client for the Norwegian regnskapsnoter taxonomy.

Quick start:
    >>> import regnskapnoter as rn
    >>> rn.concepts()                       # 279 concept rows
    >>> rn.frameworks()                     # framework membership
    >>> rn.concepts_in_framework("§ 7-29")
    >>> rn.canonicalize(extracted_df, table="skatt_aaret")
    >>> rn.build_annotations_with_urn(raw_json, observations)
    >>> session = rn.AnalystSession(); session.review_queue(orgnr=..., year=...)
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
from regnskapnoter.loader import (
    artifact_path,
    available_versions,
    clear_cache,
    load,
    set_version,
    version,
)
from regnskapnoter.store import (
    GCSAnnotationStore,
    annotations_to_post_events,
    make_mutation_event,
    next_sequence,
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

__version__ = "0.6.0"

__all__ = [
    "AnalystSession",
    "GCSAnnotationStore",
    "__version__",
    "annotations_to_jsonld",
    "annotations_to_post_events",
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
    "framework_for_concept",
    "frameworks",
    "labels",
    "list_frameworks",
    "load",
    "make_mutation_event",
    "mappings",
    "next_sequence",
    "parse_urn",
    "references",
    "set_version",
    "to_gcs_path",
    "to_pdf_gcs_path",
    "to_urn",
    "version",
]
