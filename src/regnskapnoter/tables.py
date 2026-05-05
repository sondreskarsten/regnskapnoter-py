"""Typed accessors for taxonomy tables and the canonicalize() pivot."""

from __future__ import annotations

from functools import lru_cache

import pandas as pd

from regnskapnoter.loader import artifact_path, load


def concepts() -> pd.DataFrame:
    """279 rows: concept_id, namespace, period_type, balance, data_type, status, ..."""
    return load("concepts.parquet")


def labels() -> pd.DataFrame:
    """628 rows: subject_id, subject_kind, lang, role, text."""
    return load("labels.parquet")


def definitions() -> pd.DataFrame:
    """279 rows: concept_id, lang, role, text, source_publisher, source_document, source_paragraph."""
    return load("definitions.parquet")


def references() -> pd.DataFrame:
    """References table: subject_id, publisher, document, paragraph, applicable_from/to."""
    return load("references.parquet")


def mappings() -> pd.DataFrame:
    """Cross-walk to other taxonomies: subject_id, target, quality."""
    return load("mappings.parquet")


def calc_arcs() -> pd.DataFrame:
    """Calculation arcs: parent_id, child_id, weight, order, role."""
    return load("calc_arcs.parquet")


def axes() -> pd.DataFrame:
    """Dimensional axes: axis_id, label, dimension_type."""
    return load("axes.parquet")


def axis_members() -> pd.DataFrame:
    """Axis members: axis_id, member_id, label, order."""
    return load("axis_members.parquet")


@lru_cache(maxsize=1)
def build_tables_mapping() -> pd.DataFrame:
    """The 230-row CSV mapping build_tables CSV columns to concept_ids.

    Columns: build_table, build_table_column, regnskap_no_concept_id, note.
    Fetched from the regnskapnoter-taxonomy repo's mappings/ directory at the active
    version tag (falls back to main if version-tagged copy missing).
    """
    try:
        p = artifact_path("to-build-tables.csv")
    except Exception:
        import requests

        url = "https://raw.githubusercontent.com/sondreskarsten/regnskapnoter-taxonomy/main/mappings/to-build-tables.csv"
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        from io import StringIO

        return pd.read_csv(StringIO(r.text))
    return pd.read_csv(p)


def concept_for_column(table: str, column: str) -> str | None:
    """Return the concept_id mapped from a (build_table, build_table_column) pair, or None."""
    df = build_tables_mapping()
    hit = df[(df["build_table"] == table) & (df["build_table_column"] == column)]
    if hit.empty:
        return None
    cid = hit.iloc[0]["regnskap_no_concept_id"]
    return cid if isinstance(cid, str) and cid else None


def canonicalize(
    df: pd.DataFrame,
    *,
    table: str,
    id_columns: tuple[str, ...] = ("orgnr", "report_year"),
    drop_unmapped: bool = True,
) -> pd.DataFrame:
    """Pivot a wide build_tables-shaped DataFrame to long concept-keyed observations.

    Input  : wide DataFrame with columns matching build_tables_mapping().build_table_column.
    Output : long DataFrame with id_columns + (concept_id, value).

    Parameters
    ----------
    df : DataFrame produced by noter-extraction-tidy-tables (one table's CSV).
    table : The build_tables table name (e.g. "skatt_aaret").
    id_columns : Identity columns to retain on every output row.
    drop_unmapped : If True, columns with no concept_id mapping are excluded from output.
                   If False, they remain with concept_id == NaN.
    """
    m = build_tables_mapping()
    table_map = (
        m[m["build_table"] == table]
        .set_index("build_table_column")["regnskap_no_concept_id"]
        .to_dict()
    )
    value_cols = [c for c in df.columns if c not in id_columns]
    long = df.melt(
        id_vars=list(id_columns),
        value_vars=value_cols,
        var_name="build_table_column",
        value_name="value",
    )
    long["concept_id"] = long["build_table_column"].map(table_map)
    if drop_unmapped:
        long = long[long["concept_id"].notna() & (long["concept_id"] != "")]
    return long.drop(columns=["build_table_column"]).reset_index(drop=True)
