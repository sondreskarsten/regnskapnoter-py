# regnskapnoter

Python client for the [regnskapnoter-taxonomy](https://github.com/sondreskarsten/regnskapnoter-taxonomy) — a Norwegian financial-statement-noter concept dictionary covering regnskapsloven §§ 6-1, 6-1a, 6-2, 7-21–7-46, NRS 2/6/8/13/17/21, and NRS(F) Resultatskatt.

## Why this exists

The downstream extraction pipeline groups noter content by ad-hoc tables (`skatt_aaret`, `lonn_ytelser`, `egenkapital_summary`, ...). That grouping is a workaround for what the taxonomy already encodes: every concept has `references[*]` declaring which legal/standard framework it belongs to (`§ 7-29`, `NRS 2`, `NRS Resultatskatt`, ...).

This package replaces table-shape grouping with **framework grouping**, exposing the taxonomy via concept-keyed pandas DataFrames.

## Install

```bash
pip install regnskapnoter
```

## Quick start

```python
import regnskapnoter as rn

rn.concepts()                       # 279 concepts
rn.definitions()                    # verbatim regnskapsloven/NRS prose, one row per concept
rn.list_frameworks()                # ranked: § 7-29, § 7-46, NRS Resultatskatt, ...

rn.concepts_in_framework("§ 7-29")
# ['regnskap-no:AndreForskjeller',
#  'regnskap-no:AnvendelseFremforbartUnderskudd',
#  'regnskap-no:BetalbarSkattAaret', ...]

rn.framework_for_concept("regnskap-no:Lonninger")
# ['§ 7-38', '§ 7-31']
```

## Canonicalize wide build_tables CSVs to concept-keyed long format

```python
import pandas as pd
import regnskapnoter as rn

wide = pd.read_csv("gs://.../structured/skatt_aaret/123456789.csv")
long = rn.canonicalize(wide, table="skatt_aaret")
# orgnr  report_year  value  concept_id
# 123…   2024         100    regnskap-no:BetalbarSkattAaret
# 123…   2024         120    regnskap-no:Skattekostnad
```

The canonical long output replaces the per-table CSV layout: every row is `(orgnr, year, concept_id, value)`, and downstream queries become framework-aware:

```python
skatt_concepts = set(rn.concepts_in_framework("§ 7-29"))
skatt_obs = long[long["concept_id"].isin(skatt_concepts)]
```

## Version pinning

```python
rn.set_version("v1.0.2")          # pin to a specific taxonomy release
rn.available_versions()           # list all published versions
rn.version()                      # currently active version
```

Default is `latest`. Cached parquet files live under `platformdirs.user_cache_dir("regnskapnoter")`. Call `rn.clear_cache()` to invalidate.

## API surface

| Function | Returns |
|---|---|
| `concepts()` | 279 rows: concept_id, period_type, balance, data_type, status, ... |
| `definitions()` | Verbatim source prose per concept |
| `labels()` | NB + EN labels per concept (628 rows) |
| `references()` | Source citations per concept |
| `mappings()` | Cross-walks (IFRS-Full, norwegian_specific) |
| `calc_arcs()` | Calculation arcs with weights and roles |
| `axes()` / `axis_members()` | 4 dimensional axes, 31 members |
| `frameworks()` | concept_id → framework label (long form) |
| `list_frameworks()` | Distinct frameworks ranked by concept count |
| `concepts_in_framework(label)` | concept_ids for a framework |
| `framework_for_concept(cid)` | framework labels for a concept |
| `build_tables_mapping()` | 230 rows: build_tables column → concept_id |
| `concept_for_column(table, col)` | Single mapping lookup |
| `canonicalize(df, table=...)` | Wide → long concept-keyed pivot |

## Source

Artifacts fetched from `gs://regnskapnoter-taxonomy/{version}/` (publicly readable).

## License

CC-BY-4.0 (matches taxonomy license).
