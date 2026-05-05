"""URN scheme for noter annotation targets.

Hypothes.is requires every annotation to have a URI. For an LLM analyst loop the
URI is opaque (no browser ever visits it), so we use a stable URN that encodes
the (orgnr, year) tuple unambiguously and reverses cleanly to a GCS path.

Format:
    urn:noter:{orgnr}:{year}

Example:
    urn:noter:811722332:2024  ->  gs://sondre_brreg_data/raw/noter_extraction_2025/raw/811722332_aarsregnskap_2024_v2.json
"""

from __future__ import annotations

import re

URN_RE = re.compile(r"^urn:noter:(\d{9}):(\d{4})$")
DEFAULT_RAW_BUCKET = "sondre_brreg_data"
DEFAULT_RAW_PREFIX = "raw/noter_extraction_2025/raw"
DEFAULT_VERSION_SUFFIX = "v2"


def to_urn(orgnr: str | int, year: int) -> str:
    """Return a URN for an (orgnr, year) tuple. Pads orgnr to 9 digits."""
    orgnr_str = str(orgnr).zfill(9)
    return f"urn:noter:{orgnr_str}:{year}"


def parse_urn(urn: str) -> tuple[str, int] | None:
    """Parse a URN back to (orgnr, year). Returns None if malformed."""
    m = URN_RE.match(urn.strip())
    if not m:
        return None
    return m.group(1), int(m.group(2))


def to_gcs_path(
    urn: str,
    *,
    bucket: str = DEFAULT_RAW_BUCKET,
    prefix: str = DEFAULT_RAW_PREFIX,
    version_suffix: str = DEFAULT_VERSION_SUFFIX,
) -> str | None:
    """Convert a URN to a canonical GCS path for the raw extraction JSON."""
    parsed = parse_urn(urn)
    if parsed is None:
        return None
    orgnr, year = parsed
    suffix = f"_{version_suffix}" if version_suffix else ""
    return f"gs://{bucket}/{prefix}/{orgnr}_aarsregnskap_{year}{suffix}.json"


def to_pdf_gcs_path(
    urn: str,
    *,
    bucket: str = "brreg-regnskap",
) -> str | None:
    """Convert a URN to a canonical GCS path for the source PDF."""
    parsed = parse_urn(urn)
    if parsed is None:
        return None
    orgnr, year = parsed
    return f"gs://{bucket}/{orgnr}_aarsregnskap_{year}.pdf"


def find_pdf_in_gcs(
    urn: str,
    *,
    bucket: str = "brreg-regnskap",
    fallback_buckets: tuple[str, ...] = ("publicpannelbrreg",),
) -> str | None:
    """Locate the source PDF for an annotation URN in GCS.

    The canonical layout is ``gs://brreg-regnskap/{orgnr}_aarsregnskap_{year}.pdf``,
    but the actual filename may vary (timestamp suffixes, language variants).
    This helper tries the canonical path first, then prefix-scans the bucket for
    any PDF whose name starts with ``{orgnr}`` and contains ``{year}``.
    """
    from google.cloud import storage

    parsed = parse_urn(urn)
    if parsed is None:
        return None
    orgnr, year = parsed

    client = storage.Client()
    canonical = f"{orgnr}_aarsregnskap_{year}.pdf"

    for bucket_name in (bucket, *fallback_buckets):
        b = client.bucket(bucket_name)
        canonical_blob = b.blob(canonical)
        if canonical_blob.exists():
            return f"gs://{bucket_name}/{canonical}"
        for blob in client.list_blobs(b, prefix=orgnr, max_results=20):
            if str(year) in blob.name and blob.name.lower().endswith(".pdf"):
                return f"gs://{bucket_name}/{blob.name}"
    return None
