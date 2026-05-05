"""Command-line entry points for the regnskapnoter package.

Examples
--------

    # Push annotations for one (orgnr, year) to a Hypothes.is group
    rn-push --orgnr 811722332 --year 2024 --group <group_id> --token <token>

    # Pull all proposed-concept annotations as JSONL
    rn-pull --group <group_id> --token <token> --tag proposed-concept

    # Stats for a group
    rn-stats --group <group_id> --token <token>
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys

import pandas as pd

import regnskapnoter as rn
from regnskapnoter.analyst import AnalystSession, build_annotations_with_urn
from regnskapnoter.urn import to_gcs_path, to_urn


def _load_raw_and_observations(orgnr: str, year: int) -> tuple[dict, pd.DataFrame]:
    """Pull raw JSON + run canonicalize over all build_tables CSVs for one orgnr."""
    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket("sondre_brreg_data")

    urn = to_urn(orgnr, year)
    raw_path = to_gcs_path(urn)
    if not raw_path:
        raise ValueError(f"Cannot resolve URN {urn}")
    _, _, rest = raw_path.partition("gs://")
    bucket_name, _, blob_name = rest.partition("/")
    raw_bytes = client.bucket(bucket_name).blob(blob_name).download_as_bytes()
    raw_json = json.loads(raw_bytes)

    observations = []
    for blob in client.list_blobs(bucket, prefix="raw/noter_extraction_2025/structured/"):
        if not blob.name.endswith(f"/{orgnr}.csv"):
            continue
        table = blob.name.split("/")[3]
        if table in ("_build", "documents"):
            continue
        try:
            df = pd.read_csv(io.BytesIO(blob.download_as_bytes()))
            long = rn.canonicalize(df, table=table)
            if not long.empty:
                observations.append(long)
        except Exception:
            continue

    obs = pd.concat(observations, ignore_index=True) if observations else pd.DataFrame()
    return raw_json, obs


def cmd_push(args: argparse.Namespace) -> int:
    raw_json, obs = _load_raw_and_observations(args.orgnr, args.year)
    print(
        f"raw: {len(raw_json.get('notes') or [])} notes; observations: {len(obs)}", file=sys.stderr
    )

    annotations = build_annotations_with_urn(raw_json, obs)
    rep = rn.coverage_report(annotations)
    print(json.dumps({"build_annotations": rep}, indent=2), file=sys.stderr)

    session = AnalystSession(group_id=args.group, api_token=args.token)
    posted = session.post_observations(annotations)
    posted_count = (posted["hypothesis_status"] == "created").sum()
    print(
        json.dumps(
            {
                "posted": int(posted_count),
                "errors": int((posted["hypothesis_status"] != "created").sum()),
            },
            indent=2,
        ),
        file=sys.stderr,
    )
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    session = AnalystSession(group_id=args.group, api_token=args.token)
    tag_filter = [args.tag] if args.tag else None
    df = session.fetch_all(tag_filter=tag_filter, limit=args.limit)
    if args.format == "jsonl":
        for row in df.to_dict(orient="records"):
            print(json.dumps(row, ensure_ascii=False, default=str))
    elif args.format == "csv":
        df.to_csv(sys.stdout, index=False)
    else:
        print(df.to_string(index=False))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    session = AnalystSession(group_id=args.group, api_token=args.token)
    df = session.fetch_all(limit=args.limit)
    stats = {
        "total": len(df),
        "review_needed": int(df["is_review_needed"].sum()) if not df.empty else 0,
        "proposed_concept": int(df["is_proposed_concept"].sum()) if not df.empty else 0,
        "wrong_concept": int(df["is_wrong_concept"].sum()) if not df.empty else 0,
        "unique_concepts": int(df["regnskapnoter_concept_id"].nunique()) if not df.empty else 0,
        "unique_uris": int(df["uri"].nunique()) if not df.empty else 0,
    }
    print(json.dumps(stats, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rn", description="regnskapnoter CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_push = sub.add_parser("push", help="Push annotations for one (orgnr, year)")
    p_push.add_argument("--orgnr", required=True)
    p_push.add_argument("--year", required=True, type=int)
    p_push.add_argument("--group", default=os.environ.get("HYPOTHESIS_GROUP", ""))
    p_push.add_argument("--token", default=os.environ.get("HYPOTHESIS_TOKEN", ""))
    p_push.set_defaults(func=cmd_push)

    p_pull = sub.add_parser("pull", help="Pull annotations from a group")
    p_pull.add_argument("--group", default=os.environ.get("HYPOTHESIS_GROUP", ""))
    p_pull.add_argument("--token", default=os.environ.get("HYPOTHESIS_TOKEN", ""))
    p_pull.add_argument("--tag", help="Filter by tag (e.g. proposed-concept, review-needed)")
    p_pull.add_argument("--limit", type=int, default=200)
    p_pull.add_argument("--format", choices=["jsonl", "csv", "table"], default="jsonl")
    p_pull.set_defaults(func=cmd_pull)

    p_stats = sub.add_parser("stats", help="Stats summary for a group")
    p_stats.add_argument("--group", default=os.environ.get("HYPOTHESIS_GROUP", ""))
    p_stats.add_argument("--token", default=os.environ.get("HYPOTHESIS_TOKEN", ""))
    p_stats.add_argument("--limit", type=int, default=1000)
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
