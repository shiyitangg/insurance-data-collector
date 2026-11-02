"""Tests for the data-quality checks, using an in-memory database.

Each test injects one specific defect and asserts that the matching check
fails, so the suite proves the checks can actually catch bad data rather
than just running without error.
"""

from __future__ import annotations

import sqlite3

import pytest

from collector import checks
from collector.collect import RECORDS_DDL, RUNS_DDL, _record_id


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(RECORDS_DDL)
    connection.execute(RUNS_DDL)
    yield connection
    connection.close()


def insert(connection: sqlite3.Connection, **overrides) -> None:
    row = {
        "record_id": "r1",
        "state": "NY",
        "county_code": "36061",
        "year_of_loss": 2020,
        "date_of_loss": "2020-08-04T00:00:00.000Z",
        "building_paid": 1000.0,
        "contents_paid": 0.0,
        "payload": "{}",
    }
    row.update(overrides)
    connection.execute(
        """
        INSERT OR REPLACE INTO nfip_claims
            (record_id, state, county_code, year_of_loss, date_of_loss, building_paid, contents_paid, payload)
        VALUES (:record_id, :state, :county_code, :year_of_loss, :date_of_loss, :building_paid, :contents_paid, :payload)
        """,
        row,
    )
    connection.commit()


def record_run(connection: sqlite3.Connection, rows_written: int) -> None:
    connection.execute(
        "INSERT INTO collection_runs (endpoint, rows_written, status) VALUES ('test', ?, 'succeeded')",
        (rows_written,),
    )
    connection.commit()


def test_clean_table_passes_every_check(conn: sqlite3.Connection) -> None:
    insert(conn, record_id="r1")
    insert(conn, record_id="r2")
    record_run(conn, rows_written=2)
    results = checks.run_all(conn)
    failed = [r.name for r in results if not r.passed]
    assert failed == []


def test_empty_table_fails_row_count(conn: sqlite3.Connection) -> None:
    record_run(conn, rows_written=0)
    assert checks.check_row_count(conn).passed is False


def test_missing_state_fails_null_rate(conn: sqlite3.Connection) -> None:
    for i in range(10):
        insert(conn, record_id=f"r{i}", state=None if i < 2 else "NY")
    assert checks.check_null_rate(conn, "state", max_rate=0.05).passed is False


def test_future_year_fails_year_range(conn: sqlite3.Connection) -> None:
    insert(conn, record_id="r1", year_of_loss=2999)
    assert checks.check_year_range(conn).passed is False


def test_negative_payment_fails(conn: sqlite3.Connection) -> None:
    insert(conn, record_id="r1", building_paid=-5.0)
    assert checks.check_non_negative_payments(conn).passed is False


def test_more_rows_than_reported_fails_reconciliation(conn: sqlite3.Connection) -> None:
    insert(conn, record_id="r1")
    insert(conn, record_id="r2")
    record_run(conn, rows_written=1)
    assert checks.check_run_reconciliation(conn).passed is False


def test_record_id_prefers_source_id() -> None:
    assert _record_id({"id": "abc", "state": "NY"}) == "abc"


def test_record_id_is_stable_hash_without_id() -> None:
    row = {"state": "NY", "yearOfLoss": 2020}
    assert _record_id(row) == _record_id(dict(reversed(list(row.items()))))


def test_record_id_differs_for_different_rows() -> None:
    assert _record_id({"state": "NY"}) != _record_id({"state": "NJ"})


def test_pick_field_uses_the_first_present_alias() -> None:
    from collector.collect import pick_field

    assert pick_field({"state": "NY"}, "state") == "NY"
    assert pick_field({"propertyState": "FL"}, "state") == "FL"
    assert pick_field({"state": None, "propertyState": "FL"}, "state") == "FL"
    assert pick_field({"nothing": 1}, "state") is None


def test_negative_payment_warns_but_does_not_block(conn: sqlite3.Connection) -> None:
    insert(conn, record_id="r1", building_paid=-8627.72)
    result = checks.check_non_negative_payments(conn)
    assert result.passed is False
    assert result.severity == checks.WARNING
    assert result.blocking is False
    assert result.label == "WARN"
    assert "r1" in result.detail


def test_only_error_severity_failures_block(conn: sqlite3.Connection) -> None:
    insert(conn, record_id="r1", building_paid=-1.0)
    record_run(conn, rows_written=1)
    results = checks.run_all(conn)
    assert [r.name for r in results if not r.passed] == ["non_negative_payments"]
    assert checks.is_blocking(results) is False
    assert checks.is_blocking(results, strict=True) is True


def test_a_real_defect_still_blocks(conn: sqlite3.Connection) -> None:
    insert(conn, record_id="r1", year_of_loss=2999)
    record_run(conn, rows_written=1)
    assert checks.is_blocking(checks.run_all(conn)) is True


def test_lookup_dataset_returns_discovered_endpoint(conn: sqlite3.Connection) -> None:
    from collector.discover import DatasetRecord, lookup_dataset, save_catalog

    save_catalog(conn, [DatasetRecord("NfipClaims", "2", "NFIP Claims", "", 10, "openfema_api")])
    endpoint, key = lookup_dataset(conn, "nfipclaims")
    assert endpoint.endswith("/api/open/v2/NfipClaims")
    assert key == "NfipClaims"


def test_lookup_dataset_lists_known_names_when_missing(conn: sqlite3.Connection) -> None:
    from collector.discover import DatasetNotInCatalog, DatasetRecord, lookup_dataset, save_catalog

    save_catalog(conn, [DatasetRecord("NfipClaims", "2", "NFIP Claims", "", 10, "openfema_api")])
    with pytest.raises(DatasetNotInCatalog) as err:
        lookup_dataset(conn, "Nope")
    assert "NfipClaims" in str(err.value)
