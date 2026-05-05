"""Hypothes.is integration for analyst review of WADM annotations.

Hypothes.is (https://web.hypothes.is/) is a WADM-native annotation service. This
module pushes ``regnskapnoter`` annotations into a Hypothes.is group for analyst
review (especially of unmatched annotations and proposed-concept additions),
and pulls analyst-authored annotations back as structured DataFrames.

Workflow
--------

1. Bank-side: ``rn.canonicalize`` → ``rn.build_annotations`` → ``rn.to_hypothesis(df, ...)``
   posts each annotation. Unmatched annotations become "review needed" tags.
2. Analyst-side: opens the source URL in browser, sees Hypothes.is overlays, can
   - confirm / re-anchor unmatched annotations (sets a TextQuoteSelector manually)
   - propose new concepts via the ``proposed-concept:`` tag
   - flag misclassifications via the ``review:wrong-concept`` tag
3. Bank-side: ``rn.from_hypothesis(group_id, ...)`` pulls back the analyst layer.

The Hypothes.is API requires a real HTTP(S) URL for the source. Two options:
- ``source_url_template``: a function that maps each annotation row to a public URL
  (e.g. ``f"https://noter-viewer.example.com/{orgnr}/{year}"``)
- ``source_url`` per annotation, set in the DataFrame's ``source`` column already.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

import pandas as pd
import requests

API_BASE = "https://api.hypothes.is/api"
PROPOSED_CONCEPT_TAG = "proposed-concept"
REVIEW_TAG = "review-needed"
WRONG_CONCEPT_TAG = "review-wrong-concept"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _payload_for_row(
    row: pd.Series,
    *,
    group_id: str,
    source_url_template: Callable[[pd.Series], str] | None,
) -> dict[str, Any]:
    url = source_url_template(row) if source_url_template is not None else (row.get("source") or "")
    if not url:
        raise ValueError(
            f"annotation {row.get('annotation_id')} has no source URL; pass source_url_template"
        )

    selectors: list[dict] = []
    sel_json = row.get("selector_json") or ""
    if sel_json:
        sel = json.loads(sel_json)
        # Hypothes.is wants TextQuoteSelector at top level when text-anchored
        if sel.get("type") == "TextQuoteSelector":
            selectors.append(sel)
        else:
            refined = sel.get("refinedBy")
            if isinstance(refined, dict) and refined.get("type") == "TextQuoteSelector":
                selectors.append(refined)
            selectors.append(sel)

    tags = [
        f"concept:{row['body_concept_id']}",
        f"value:{row['body_value']}",
    ]
    if row.get("note_number"):
        tags.append(f"note:{row['note_number']}")
    if row.get("page") is not None and not pd.isna(row.get("page")):
        tags.append(f"page:{int(row['page'])}")
    if row.get("match_status") == "unmatched":
        tags.append(REVIEW_TAG)

    return {
        "uri": url,
        "group": group_id,
        "tags": tags,
        "text": (
            f"**{row['body_concept_id']}** = {row['body_value']}"
            + (
                f"\n\nNote {row['note_number']}: {row.get('note_title', '')}"
                if row.get("note_number")
                else ""
            )
            + (
                f"\n\n[{REVIEW_TAG}] auto-extraction could not anchor this value to a text span; please re-anchor."
                if row.get("match_status") == "unmatched"
                else ""
            )
        ),
        "target": [{"source": url, "selector": selectors}] if selectors else [{"source": url}],
        "extra": {
            "regnskapnoter_annotation_id": row["annotation_id"],
            "regnskapnoter_concept_id": row["body_concept_id"],
            "regnskapnoter_value": row["body_value"],
            "regnskapnoter_match_status": row.get("match_status", ""),
        },
    }


def to_hypothesis(
    df: pd.DataFrame,
    *,
    group_id: str,
    api_token: str,
    source_url_template: Callable[[pd.Series], str] | None = None,
    target_type_filter: str = "text",
    raise_on_error: bool = False,
) -> pd.DataFrame:
    """POST each annotation row to Hypothes.is. Returns the input DataFrame
    extended with ``hypothesis_id`` and ``hypothesis_status`` columns.

    Parameters
    ----------
    df : Output of ``rn.build_annotations``.
    group_id : Hypothes.is group ID (the short string after the ``/groups/`` URL
        segment, NOT the full URL). Use ``__world__`` for public.
    api_token : Personal API token from https://hypothes.is/account/developer
    source_url_template : Callable mapping a row to a public URL. If None, uses
        the row's ``source`` column verbatim (must be HTTP(S), not gs://).
    target_type_filter : Only post rows where ``target_type`` matches. Default
        ``'text'`` because Hypothes.is anchors to web pages, not raw PDF GCS URIs.
    raise_on_error : If True, raise on the first failed POST; else log per-row.
    """
    work = df[df["target_type"] == target_type_filter].copy() if target_type_filter else df.copy()
    work["hypothesis_id"] = ""
    work["hypothesis_status"] = ""

    h = _headers(api_token)
    h["Content-Type"] = "application/json"

    for idx, row in work.iterrows():
        try:
            payload = _payload_for_row(
                row, group_id=group_id, source_url_template=source_url_template
            )
            r = requests.post(f"{API_BASE}/annotations", headers=h, json=payload, timeout=15)
            if r.status_code in (200, 201):
                resp = r.json()
                work.at[idx, "hypothesis_id"] = resp.get("id", "")
                work.at[idx, "hypothesis_status"] = "created"
            else:
                work.at[idx, "hypothesis_status"] = f"http_{r.status_code}"
                if raise_on_error:
                    r.raise_for_status()
        except Exception as e:
            work.at[idx, "hypothesis_status"] = f"error:{type(e).__name__}"
            if raise_on_error:
                raise
    return work


def from_hypothesis(
    *,
    group_id: str,
    api_token: str,
    tag_filter: Iterable[str] | None = None,
    limit: int = 200,
    page_size: int = 200,
) -> pd.DataFrame:
    """Pull annotations back from a Hypothes.is group as a DataFrame.

    Parameters
    ----------
    group_id : Hypothes.is group ID.
    api_token : Personal API token.
    tag_filter : If provided, only annotations with at least one matching tag are
        returned. Useful values: ``[PROPOSED_CONCEPT_TAG]``, ``[REVIEW_TAG]``,
        ``[WRONG_CONCEPT_TAG]``.
    limit : Max annotations to fetch total.
    page_size : Per-request page size (Hypothes.is max is 200).
    """
    h = _headers(api_token)
    out: list[dict] = []
    search_after = None
    while len(out) < limit:
        params: dict[str, Any] = {
            "group": group_id,
            "limit": min(page_size, limit - len(out)),
            "sort": "updated",
            "order": "asc",
        }
        if search_after:
            params["search_after"] = search_after
        if tag_filter:
            params["tags"] = ",".join(tag_filter)
        r = requests.get(f"{API_BASE}/search", headers=h, params=params, timeout=15)
        r.raise_for_status()
        rows = r.json().get("rows") or []
        if not rows:
            break
        out.extend(rows)
        search_after = rows[-1].get("updated")

    if not out:
        return pd.DataFrame(
            columns=[
                "hypothesis_id",
                "uri",
                "user",
                "tags",
                "text",
                "target",
                "created",
                "updated",
            ]
        )

    flat = []
    for a in out:
        flat.append(
            {
                "hypothesis_id": a.get("id"),
                "uri": a.get("uri"),
                "user": a.get("user"),
                "tags": a.get("tags") or [],
                "text": a.get("text"),
                "target": json.dumps(a.get("target") or []),
                "created": a.get("created"),
                "updated": a.get("updated"),
                "regnskapnoter_concept_id": next(
                    (t.split(":", 1)[1] for t in (a.get("tags") or []) if t.startswith("concept:")),
                    None,
                ),
                "regnskapnoter_value": next(
                    (t.split(":", 1)[1] for t in (a.get("tags") or []) if t.startswith("value:")),
                    None,
                ),
                "is_proposed_concept": PROPOSED_CONCEPT_TAG in (a.get("tags") or []),
                "is_review_needed": REVIEW_TAG in (a.get("tags") or []),
                "is_wrong_concept": WRONG_CONCEPT_TAG in (a.get("tags") or []),
            }
        )
    return pd.DataFrame(flat)


def proposed_concepts(df: pd.DataFrame) -> pd.DataFrame:
    """Filter a from_hypothesis() DataFrame to analyst-proposed new concepts."""
    return df[df["is_proposed_concept"]].copy()


def review_queue(df: pd.DataFrame) -> pd.DataFrame:
    """Filter a from_hypothesis() DataFrame to items needing analyst attention."""
    return df[df["is_review_needed"] | df["is_wrong_concept"]].copy()
