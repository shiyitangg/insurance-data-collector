"""Data-quality checks that gate the collected table.

Each check returns a pass/fail row, and the suite exits non-zero when any
check fails, so a bad collection run cannot quietly become the input to an
analysis.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict
from datetime import date
from typing import Any, Callable

TABLE = "nfip_claims"

EXPECTED_COLUMNS = {
    "record_id",
    "state",
    "county_code",
    "year_of_loss",
    "date_of_loss",
    "building_paid",
    "contents_paid",
    "payload",
    "collected_at",
}


ERROR = "error"
WARNING = "warning"


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    severity: str = ERROR

    @property
    def blocking(self) -> bool:
        """A failed error-severity check should stop downstream work."""
        return not self.passed and self.severity == ERROR

    @property
    def label(self) -> str:
        if self.passed:
            return "PASS"
        return "FAIL" if self.severity == ERROR else "WARN"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _scalar(conn: sqlite3.Connection, sql: str, *params: Any) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def check_schema(conn: sqlite3.Connection) -> CheckResult:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")}
    missing = EXPECTED_COLUMNS - columns
    return CheckResult(
        "schema",
        not missing,
        "all expected columns present" if not missing else f"missing columns: {sorted(missing)}",
    )


def check_row_count(conn: sqlite3.Connection, minimum: int = 1) -> CheckResult:
    count = _scalar(conn, f"SELECT COUNT(*) FROM {TABLE}") or 0
    return CheckResult("row_count", count >= minimum, f"{count} rows (minimum {minimum})")


def check_duplicate_ids(conn: sqlite3.Connection) -> CheckResult:
    dupes = _scalar(
        conn,
        f"SELECT COUNT(*) FROM (SELECT record_id FROM {TABLE} GROUP BY record_id HAVING COUNT(*) > 1)",
    ) or 0
    return CheckResult("duplicate_ids", dupes == 0, f"{dupes} duplicated record_id values")


def check_null_rate(conn: sqlite3.Connection, column: str = "state", max_rate: float = 0.05) -> CheckResult:
    total = _scalar(conn, f"SELECT COUNT(*) FROM {TABLE}") or 0
    if total == 0:
        return CheckResult(f"null_rate:{column}", False, "table is empty")
    nulls = _scalar(conn, f"SELECT COUNT(*) FROM {TABLE} WHERE {column} IS NULL OR TRIM({column}) = ''") or 0
    rate = nulls / total
    return CheckResult(
        f"null_rate:{column}",
        rate <= max_rate,
        f"{nulls}/{total} null ({rate:.2%}, limit {max_rate:.0%})",
    )


def check_year_range(conn: sqlite3.Connection, earliest: int = 1970) -> CheckResult:
    latest_allowed = date.today().year
    bad = _scalar(
        conn,
        f"SELECT COUNT(*) FROM {TABLE} WHERE year_of_loss IS NOT NULL AND (year_of_loss < ? OR year_of_loss > ?)",
        earliest,
        latest_allowed,
    ) or 0
    return CheckResult(
        "year_range",
        bad == 0,
        f"{bad} rows outside {earliest}-{latest_allowed}",
    )


def check_non_negative_payments(conn: sqlite3.Connection) -> CheckResult:
    """Negative payments are rare but real in this dataset, so this warns.

    NFIP claim records can carry a negative paid amount where a payment was
    later reversed or adjusted: the net payment field moves with it and the
    damage amount still shows a loss. That is a posted correction, not a
    collection fault, so it is surfaced for review instead of blocking the
    run. `--strict` turns warnings into failures.
    """
    bad = _scalar(
        conn,
        f"SELECT COUNT(*) FROM {TABLE} WHERE building_paid < 0 OR contents_paid < 0",
    ) or 0
    detail = f"{bad} rows with negative payment amounts"
    if bad:
        example = conn.execute(
            f"""
            SELECT record_id, building_paid, contents_paid FROM {TABLE}
            WHERE building_paid < 0 OR contents_paid < 0
            ORDER BY record_id LIMIT 1
            """
        ).fetchone()
        detail += f" (e.g. record {example[0]}: building {example[1]}, contents {example[2]})"
    return CheckResult("non_negative_payments", bad == 0, detail, severity=WARNING)


def check_run_reconciliation(conn: sqlite3.Connection) -> CheckResult:
    """Rows stored must not exceed rows the collector reported writing."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "collection_runs" not in tables:
        return CheckResult("run_reconciliation", False, "collection_runs table missing")
    written = _scalar(conn, "SELECT COALESCE(SUM(rows_written), 0) FROM collection_runs WHERE status = 'succeeded'") or 0
    stored = _scalar(conn, f"SELECT COUNT(*) FROM {TABLE}") or 0
    return CheckResult(
        "run_reconciliation",
        stored <= written,
        f"{stored} rows stored vs {written} reported written by successful runs",
    )


CHECKS: tuple[Callable[[sqlite3.Connection], CheckResult], ...] = (
    check_schema,
    check_row_count,
    check_duplicate_ids,
    check_null_rate,
    check_year_range,
    check_non_negative_payments,
    check_run_reconciliation,
)


def run_all(conn: sqlite3.Connection) -> list[CheckResult]:
    return [check(conn) for check in CHECKS]


def format_report(results: list[CheckResult]) -> str:
    width = max(len(r.name) for r in results)
    lines = [f"{r.label}  {r.name:<{width}}  {r.detail}" for r in results]
    passed = sum(r.passed for r in results)
    warned = sum(1 for r in results if not r.passed and r.severity == WARNING)
    summary = f"\n{passed}/{len(results)} checks passed"
    if warned:
        summary += f", {warned} warning{'s' if warned > 1 else ''}"
    lines.append(summary)
    return "\n".join(lines)


def is_blocking(results: list[CheckResult], strict: bool = False) -> bool:
    """True when the run should be treated as failed."""
    if strict:
        return any(not r.passed for r in results)
    return any(r.blocking for r in results)
