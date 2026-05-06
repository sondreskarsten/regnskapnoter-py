"""Command-line entry points for the regnskapnoter package.

rn push  --orgnr 811722332 --year 2024     # post initial annotations to GCS
rn pull  --orgnr 811722332 --year 2024     # fetch current state as JSONL
rn stats --orgnr 811722332 --year 2024     # event-log summary
rn proposed                                 # all proposed-concept events across shards
rn shards                                   # enumerate (orgnr, year) shards present
"""

from __future__ import annotations

import argparse
import io
import json
import sys

import pandas as pd

import regnskapnoter as rn


def _load_raw_and_observations(orgnr: str, year: int) -> tuple[dict, pd.DataFrame]:
    """Pull raw JSON + run canonicalize over all build_tables CSVs for one orgnr."""
    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket("sondre_brreg_data")

    urn = rn.to_urn(orgnr, year)
    raw_path = rn.to_gcs_path(urn)
    if not raw_path:
        raise ValueError(f"Cannot resolve URN {urn}")
    _, _, rest = raw_path.partition("gs://")
    bucket_name, _, blob_name = rest.partition("/")
    raw_bytes = client.bucket(bucket_name).blob(blob_name).download_as_bytes()
    raw_json = json.loads(raw_bytes)

    observations: list[pd.DataFrame] = []
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
    annotations = rn.build_annotations_with_urn(raw_json, obs)
    cov = rn.coverage_report(annotations)
    print(json.dumps({"build_annotations": cov}, indent=2), file=sys.stderr)

    session = rn.AnalystSession()
    written = session.post_observations(annotations, orgnr=args.orgnr, year=args.year)
    print(
        json.dumps(
            {
                "events_written": written,
                "shard": f"gs://{session.bucket}/{session.prefix}/{args.orgnr}/{args.year}/events.parquet",
            },
            indent=2,
        ),
        file=sys.stderr,
    )
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    session = rn.AnalystSession()
    if args.history:
        df = session.history(orgnr=args.orgnr, year=args.year)
    else:
        df = session.fetch_all(orgnr=args.orgnr, year=args.year)
    if args.tag:
        df = df[df["match_status"] == args.tag]
    if args.format == "jsonl":
        for row in df.to_dict(orient="records"):
            print(json.dumps(row, ensure_ascii=False, default=str))
    elif args.format == "csv":
        df.to_csv(sys.stdout, index=False)
    else:
        print(df.to_string(index=False))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    session = rn.AnalystSession()
    s = session.stats(orgnr=args.orgnr, year=args.year)
    print(json.dumps(s, indent=2, default=str))
    return 0


def cmd_proposed(args: argparse.Namespace) -> int:
    session = rn.AnalystSession()
    df = session.proposed_concepts()
    if args.format == "jsonl":
        for row in df.to_dict(orient="records"):
            print(json.dumps(row, ensure_ascii=False, default=str))
    elif args.format == "csv":
        df.to_csv(sys.stdout, index=False)
    else:
        print(df.to_string(index=False))
    return 0


def cmd_shards(args: argparse.Namespace) -> int:
    session = rn.AnalystSession()
    for orgnr, year in session.store.list_shards():
        print(f"{orgnr}\t{year}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rn", description="regnskapnoter CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_push = sub.add_parser("push", help="Post annotations for one (orgnr, year) to GCS store")
    p_push.add_argument("--orgnr", required=True)
    p_push.add_argument("--year", required=True, type=int)
    p_push.set_defaults(func=cmd_push)

    p_pull = sub.add_parser("pull", help="Pull current state for one (orgnr, year)")
    p_pull.add_argument("--orgnr", required=True)
    p_pull.add_argument("--year", required=True, type=int)
    p_pull.add_argument("--tag", choices=["matched", "unmatched", "reviewed", "deleted"])
    p_pull.add_argument(
        "--history", action="store_true", help="Show full event log instead of current state"
    )
    p_pull.add_argument("--format", choices=["jsonl", "csv", "table"], default="jsonl")
    p_pull.set_defaults(func=cmd_pull)

    p_stats = sub.add_parser("stats", help="Stats summary for one (orgnr, year)")
    p_stats.add_argument("--orgnr", required=True)
    p_stats.add_argument("--year", required=True, type=int)
    p_stats.set_defaults(func=cmd_stats)

    p_proposed = sub.add_parser("proposed", help="All propose-concept events across shards")
    p_proposed.add_argument("--format", choices=["jsonl", "csv", "table"], default="jsonl")
    p_proposed.set_defaults(func=cmd_proposed)

    p_shards = sub.add_parser("shards", help="Enumerate (orgnr, year) shards in the store")
    p_shards.set_defaults(func=cmd_shards)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
