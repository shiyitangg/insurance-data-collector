"""Discover which public FEMA datasets are worth collecting.

Two independent paths are used so the catalogue does not depend on a single
source: the OpenFEMA DataSets API, and (optionally) the HTML listing page,
parsed with BeautifulSoup. Both are checked against robots.txt first.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from bs4 import BeautifulSoup

from .http_client import PoliteClient, RobotsDisallowed

log = logging.getLogger(__name__)

DATASETS_API = "https://www.fema.gov/api/open/v1/DataSets"
DATASETS_HTML = "https://www.fema.gov/about/openfema/data-sets"

# Keywords that mark a dataset as insurance-relevant for this project.
INSURANCE_KEYWORDS = ("nfip", "flood insurance", "insurance", "claims", "policies")


@dataclass(frozen=True)
class DatasetRecord:
    name: str
    version: str
    title: str
    description: str
    record_count: int | None
    source: str

    @property
    def endpoint(self) -> str:
        return f"https://www.fema.gov/api/open/v{self.version}/{self.name}"


def _looks_insurance_related(*fields: str) -> bool:
    haystack = " ".join(f.lower() for f in fields if f)
    return any(keyword in haystack for keyword in INSURANCE_KEYWORDS)


def discover_via_api(client: PoliteClient) -> list[DatasetRecord]:
    payload = client.get_json(DATASETS_API)
    rows: Iterable[dict[str, Any]] = payload.get("DataSets", [])
    found: list[DatasetRecord] = []
    for row in rows:
        name = str(row.get("name", ""))
        title = str(row.get("title", ""))
        description = str(row.get("description", ""))
        if not name or not _looks_insurance_related(name, title, description):
            continue
        count = row.get("recordCount")
        found.append(
            DatasetRecord(
                name=name,
                version=str(row.get("version", "2")),
                title=title or name,
                description=description[:500],
                record_count=int(count) if isinstance(count, (int, float)) else None,
                source="openfema_api",
            )
        )
    log.info("API discovery found %s insurance-related datasets", len(found))
    return found


def discover_via_html(client: PoliteClient) -> list[DatasetRecord]:
    """Parse the public listing page. Skipped quietly if robots.txt says no."""
    try:
        response = client.get(DATASETS_HTML)
    except RobotsDisallowed:
        log.warning("robots.txt disallows %s; skipping HTML discovery", DATASETS_HTML)
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    found: list[DatasetRecord] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=True):
        text = link.get_text(strip=True)
        href = link["href"]
        if "/openfema-dataset" not in href and "data-sets/" not in href:
            continue
        if not _looks_insurance_related(text, href):
            continue
        slug = href.rstrip("/").split("/")[-1]
        if slug in seen:
            continue
        seen.add(slug)
        found.append(
            DatasetRecord(
                name=slug,
                version="2",
                title=text or slug,
                description=href,
                record_count=None,
                source="openfema_html",
            )
        )
    log.info("HTML discovery found %s insurance-related links", len(found))
    return found


CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS dataset_catalog (
    name         TEXT NOT NULL,
    version      TEXT NOT NULL,
    title        TEXT,
    description  TEXT,
    record_count INTEGER,
    source       TEXT NOT NULL,
    endpoint     TEXT,
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (name, source)
)
"""


class DatasetNotInCatalog(LookupError):
    """Raised when a dataset name has not been discovered yet."""


def lookup_dataset(conn: sqlite3.Connection, name: str) -> tuple[str, str]:
    """Return (endpoint, data_key) for a dataset recorded by `discover`.

    The API version differs between datasets, so the endpoint is taken from
    what discovery reported rather than assumed.
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "dataset_catalog" not in tables:
        raise DatasetNotInCatalog("No catalogue yet. Run `discover` first.")

    row = conn.execute(
        """
        SELECT endpoint, name FROM dataset_catalog
        WHERE source = 'openfema_api' AND LOWER(name) = LOWER(?)
        """,
        (name,),
    ).fetchone()
    if row is None:
        known = [r[0] for r in conn.execute(
            "SELECT name FROM dataset_catalog WHERE source = 'openfema_api' ORDER BY name"
        )]
        raise DatasetNotInCatalog(
            f"{name!r} is not in the catalogue. Known datasets: {', '.join(known) or 'none'}"
        )
    return row[0], row[1]


def save_catalog(conn: sqlite3.Connection, datasets: list[DatasetRecord]) -> int:
    conn.execute(CATALOG_DDL)
    rows = [
        (d.name, d.version, d.title, d.description, d.record_count, d.source, d.endpoint)
        for d in datasets
    ]
    conn.executemany(
        """
        INSERT INTO dataset_catalog (name, version, title, description, record_count, source, endpoint)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name, source) DO UPDATE SET
            version = excluded.version,
            title = excluded.title,
            description = excluded.description,
            record_count = excluded.record_count,
            endpoint = excluded.endpoint,
            discovered_at = datetime('now')
        """,
        rows,
    )
    conn.commit()
    return len(rows)
