"""GCS-backed parquet loader with local cache.

The taxonomy artifacts live at gs://regnskapnoter-taxonomy/{version}/. The bucket is
publicly readable; this module fetches via HTTPS (no GCS credentials required) and
caches under platformdirs.user_cache_dir.
"""

from __future__ import annotations

import json
import shutil
from functools import lru_cache
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import requests
from platformdirs import user_cache_dir

PUBLIC_BUCKET_BASE = "https://storage.googleapis.com/regnskapnoter-taxonomy"
DEFAULT_VERSION = "latest"

ARTIFACTS = (
    "concepts.parquet",
    "labels.parquet",
    "definitions.parquet",
    "references.parquet",
    "mappings.parquet",
    "calc_arcs.parquet",
    "axes.parquet",
    "axis_members.parquet",
    "concept_hypercube.parquet",
    "taxonomy.ttl",
    "taxonomy.jsonld",
    "manifest.json",
)

_active_version: str = DEFAULT_VERSION


def _cache_root() -> Path:
    p = Path(user_cache_dir("regnskapnoter", "sondreskarsten"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _version_dir(v: str) -> Path:
    d = _cache_root() / v
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fetch(v: str, name: str) -> Path:
    target = _version_dir(v) / name
    if target.exists() and target.stat().st_size > 0:
        return target
    url = f"{PUBLIC_BUCKET_BASE}/{v}/{name}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    target.write_bytes(resp.content)
    return target


def version() -> str:
    """Return the currently-active taxonomy version (default: 'latest')."""
    return _active_version


def set_version(v: str) -> None:
    """Switch the active taxonomy version. Use 'latest' or a specific tag like 'v1.0.2'."""
    global _active_version
    _active_version = v
    _load.cache_clear()


def available_versions() -> list[str]:
    """Return the list of versions known to GCS by parsing the bucket XML listing."""
    r = requests.get(f"{PUBLIC_BUCKET_BASE}/?prefix=v&delimiter=/", timeout=15)
    r.raise_for_status()
    import re

    return sorted({m for m in re.findall(r"<Prefix>(v[^/<]+)/</Prefix>", r.text)})


def artifact_path(name: str, v: str | None = None) -> Path:
    """Return the local cached path for an artifact, fetching if needed."""
    return _fetch(v or _active_version, name)


@lru_cache(maxsize=64)
def _load(name: str, v: str) -> pd.DataFrame:
    p = _fetch(v, name)
    if p.suffix == ".parquet":
        return pq.read_table(p).to_pandas()
    if p.suffix == ".json":
        return pd.DataFrame([json.loads(p.read_text(encoding="utf-8"))])
    raise ValueError(f"Unsupported artifact extension: {p.suffix}")


def load(name: str) -> pd.DataFrame:
    """Load a taxonomy artifact by file name (e.g. 'concepts.parquet')."""
    return _load(name, _active_version)


def clear_cache(version_filter: str | None = None) -> None:
    """Wipe the local cache. Pass a version to wipe only that version."""
    root = _cache_root()
    if version_filter:
        target = root / version_filter
        if target.exists():
            shutil.rmtree(target)
    else:
        for p in root.iterdir():
            if p.is_dir():
                shutil.rmtree(p)
    _load.cache_clear()
