#!/usr/bin/env python3
"""Command line entry point: discover, collect, check."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

from collector import checks
from collector.collect import CollectionConfig, collect
from collector.discover import (
    DatasetNotInCatalog,
    discover_via_api,
    discover_via_html,
    lookup_dataset,
    save_catalog,
)
from collector.http_client import PoliteClient

DEFAULT_DB = Path("data/insurance.db")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def cmd_discover(args: argparse.Namespace) -> int:
    client = PoliteClient(delay_seconds=args.delay)
    datasets = discover_via_api(client)
    if not args.skip_html:
        datasets += discover_via_html(client)
    with _connect(args.db) as conn:
        saved = save_catalog(conn, datasets)
    for d in datasets:
        count = f"{d.record_count:,}" if d.record_count else "unknown"
        print(f"[{d.source}] {d.name}  ({count} records)\n    {d.title}")
    print(f"\nSaved {saved} catalogue rows to {args.db}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    with _connect(args.db) as conn:
        endpoint, data_key = args.endpoint, args.data_key
        if args.dataset:
            # Take the endpoint from discovery: API versions differ per dataset.
            try:
                endpoint, data_key = lookup_dataset(conn, args.dataset)
            except DatasetNotInCatalog as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            print(f"Using {endpoint}")

        config = CollectionConfig(
            endpoint=endpoint,
            data_key=data_key,
            filter_text=args.filter or None,
            page_size=args.page_size,
            max_pages=args.max_pages,
        )
        client = PoliteClient(delay_seconds=args.delay)
        stats = collect(conn, client, config)
    print(json.dumps(stats, indent=2))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    with _connect(args.db) as conn:
        results = checks.run_all(conn)
    report = checks.format_report(results)
    print(report)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(report + "\n", encoding="utf-8")
    return 1 if checks.is_blocking(results, strict=args.strict) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect public insurance data from OpenFEMA.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--delay", type=float, default=1.0, help="Minimum seconds between requests")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="List insurance-related public datasets")
    p_discover.add_argument("--skip-html", action="store_true", help="Use the API only")
    p_discover.set_defaults(func=cmd_discover)

    p_collect = sub.add_parser("collect", help="Download records into SQLite")
    p_collect.add_argument("--dataset", help="Dataset name as `discover` reports it, e.g. NfipClaims")
    p_collect.add_argument("--endpoint", default=CollectionConfig.endpoint)
    p_collect.add_argument("--data-key", default=CollectionConfig.data_key)
    p_collect.add_argument("--filter", default=CollectionConfig.filter_text)
    p_collect.add_argument("--page-size", type=int, default=CollectionConfig.page_size)
    p_collect.add_argument("--max-pages", type=int, default=CollectionConfig.max_pages)
    p_collect.set_defaults(func=cmd_collect)

    p_check = sub.add_parser("check", help="Run data-quality checks")
    p_check.add_argument("--report", help="Write the report to this file as well")
    p_check.add_argument("--strict", action="store_true", help="Treat warnings as failures")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
