"""Page through an OpenFEMA dataset and store it in SQLite.

Collection is resumable and idempotent: progress is written to a run-state
table after every page, and rows are keyed by the source's own id so that
re-running the collector never creates duplicates.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:  # keeps storage and checks importable without the HTTP stack
    from .http_client import PoliteClient

log = logging.getLogger(__name__)

RECORDS_DDL = """
CREATE TABLE IF NOT EXISTS nfip_claims (
    record_id     TEXT PRIMARY KEY,
    state         TEXT,
    county_code   TEXT,
    year_of_loss  INTEGER,
    date_of_loss  TEXT,
    building_paid REAL,
    contents_paid REAL,
    payload       TEXT NOT NULL,
    collected_at  TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

RUNS_DDL = """
CREATE TABLE IF NOT EXISTS collection_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint      TEXT NOT NULL,
    filter_text   TEXT,
    rows_seen     INTEGER NOT NULL DEFAULT 0,
    rows_written  INTEGER NOT NULL DEFAULT 0,
    last_skip     INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'running',
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT
)
"""


@dataclass(frozen=True)
class CollectionConfig:
    endpoint: str = "https://www.fema.gov/api/open/v2/NfipClaims"
    data_key: str = "NfipClaims"
    filter_text: str | None = "state eq 'NY' and yearOfLoss ge 2015"
    page_size: int = 1000
    max_pages: int = 20


# The same idea is spelled differently across FEMA datasets, so each typed
# column accepts a short list of names and takes the first one present.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "state": ("state", "propertyState", "stateAbbreviation"),
    "county_code": ("countyCode", "county", "countyFips"),
    "year_of_loss": ("yearOfLoss", "policyEffectiveYear", "year"),
    "date_of_loss": ("dateOfLoss", "dateOfLossUtc", "policyEffectiveDate"),
    "building_paid": ("amountPaidOnBuildingClaim", "buildingPropertyValue", "buildingDamageAmount"),
    "contents_paid": ("amountPaidOnContentsClaim", "contentsPropertyValue", "contentsDamageAmount"),
}


def pick_field(row: dict[str, Any], column: str) -> Any:
    """Return the first aliased field present in the row, else None."""
    for key in FIELD_ALIASES.get(column, ()):
        if key in row and row[key] is not None:
            return row[key]
    return None


def _record_id(row: dict[str, Any]) -> str:
    """Prefer the source's own id; otherwise hash the row so reruns are stable."""
    for key in ("id", "ID", "_id"):
        if row.get(key):
            return str(row[key])
    blob = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def iter_pages(client: "PoliteClient", config: CollectionConfig, start_skip: int = 0) -> Iterator[list[dict[str, Any]]]:
    skip = start_skip
    for page in range(config.max_pages):
        params: dict[str, Any] = {"$top": config.page_size, "$skip": skip, "$format": "json"}
        if config.filter_text:
            params["$filter"] = config.filter_text
        payload = client.get_json(config.endpoint, params=params)
        rows = payload.get(config.data_key) or []
        if not rows:
            log.info("No more rows at skip=%s; stopping", skip)
            return
        log.info("Page %s: %s rows (skip=%s)", page + 1, len(rows), skip)
        yield rows
        if len(rows) < config.page_size:
            return
        skip += config.page_size


def collect(conn: sqlite3.Connection, client: "PoliteClient", config: CollectionConfig) -> dict[str, int]:
    conn.execute(RECORDS_DDL)
    conn.execute(RUNS_DDL)
    cursor = conn.execute(
        "INSERT INTO collection_runs (endpoint, filter_text) VALUES (?, ?)",
        (config.endpoint, config.filter_text),
    )
    run_id = cursor.lastrowid
    conn.commit()

    seen = written = skip = 0
    try:
        for rows in iter_pages(client, config):
            prepared = [
                (
                    _record_id(row),
                    pick_field(row, "state"),
                    pick_field(row, "county_code"),
                    _as_int(pick_field(row, "year_of_loss")),
                    pick_field(row, "date_of_loss"),
                    _as_float(pick_field(row, "building_paid")),
                    _as_float(pick_field(row, "contents_paid")),
                    json.dumps(row, separators=(",", ":"), default=str),
                )
                for row in rows
            ]
            before = conn.execute("SELECT COUNT(*) FROM nfip_claims").fetchone()[0]
            conn.executemany(
                """
                INSERT INTO nfip_claims
                    (record_id, state, county_code, year_of_loss, date_of_loss,
                     building_paid, contents_paid, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_id) DO NOTHING
                """,
                prepared,
            )
            after = conn.execute("SELECT COUNT(*) FROM nfip_claims").fetchone()[0]

            seen += len(rows)
            written += after - before
            skip += len(rows)
            conn.execute(
                "UPDATE collection_runs SET rows_seen = ?, rows_written = ?, last_skip = ? WHERE run_id = ?",
                (seen, written, skip, run_id),
            )
            conn.commit()
    except Exception:
        conn.execute("UPDATE collection_runs SET status = 'failed', finished_at = datetime('now') WHERE run_id = ?", (run_id,))
        conn.commit()
        raise

    conn.execute(
        "UPDATE collection_runs SET status = 'succeeded', finished_at = datetime('now') WHERE run_id = ?",
        (run_id,),
    )
    conn.commit()
    duplicates = seen - written
    log.info("Collected %s rows (%s new, %s duplicates skipped)", seen, written, duplicates)
    return {"rows_seen": seen, "rows_written": written, "duplicates_skipped": duplicates}
